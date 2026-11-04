"""
Compact note writing: the pure helpers in MCP_Server.note_tools, and the
tools built on them (create_clip with notes, add_notes_to_clip with compact
notes and repeats, write_drum_pattern, get_clip_notes compact) against a
scripted fake of the Remote Script that keeps clip state.

Run from the repo root:
    uv run pytest -v
"""

import json

import pytest

import MCP_Server.note_tools as nt
import MCP_Server.server as server
from tests.test_clip_notes import call, _silence_telemetry  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401


PADS = [{"note": 36, "note_name": "C1", "name": "Kick"},
        {"note": 38, "note_name": "D1", "name": "Snare"},
        {"note": 39, "note_name": "D#1", "name": "Hand Clap"},
        {"note": 42, "note_name": "F#1", "name": "Closed Hi Hat"},
        {"note": 46, "note_name": "A#1", "name": "Open Hi Hat"}]


# -- pure helpers --------------------------------------------------------------

def test_compact_and_object_notes_normalise_the_same():
    assert nt.normalize_notes([[60, 0, 1, 100], {"pitch": 60, "start_time": 0, "duration": 1,
                                                 "velocity": 100}]) == [
        {"pitch": 60, "start_time": 0.0, "duration": 1.0, "velocity": 100.0, "mute": False}] * 2
    assert nt.normalize_note([62, 1, 0.5, 90, True])["mute"] is True


@pytest.mark.parametrize("note,error", [
    ([128, 0, 1, 100], "pitch 128"), ([60, -1, 1, 100], "before beat 0"),
    ([60, 0, 0, 100], "duration"), ([60, 0, 1, 0], "velocity 0"),
    ([60, 0, 1], "expected"), (["C3", 0, 1, 100], "must be numbers"),
])
def test_bad_notes_name_the_index(note, error):
    with pytest.raises(ValueError, match=f"note 2: .*{error}"):
        nt.normalize_notes([[60, 0, 1, 100], [60, 1, 1, 100], note])


def test_tile_fills_and_refuses_overflowing_patterns():
    bar = nt.normalize_notes([[36, 0, 0.25, 100], [36, 2, 0.25, 100]])
    out = nt.tile_notes(bar, 4, 16)
    assert [n["start_time"] for n in out] == [0, 2, 4, 6, 8, 10, 12, 14]
    assert [n["start_time"] for n in nt.tile_notes(bar, 4, 6)] == [0, 2, 4]   # stops at `until`
    with pytest.raises(ValueError, match="must fit in one repeat"):
        nt.tile_notes(nt.normalize_notes([[36, 4, 1, 100]]), 4, 16)


def test_pad_resolution():
    assert nt.resolve_pad("kick", PADS) == 36
    assert nt.resolve_pad("Clap", PADS) == 39            # unique substring
    assert nt.resolve_pad("38", PADS) == 38
    assert nt.resolve_pad("A#1", PADS) == 46 and nt.resolve_pad("Bb1", PADS) == 46
    with pytest.raises(ValueError, match="matches several pads"):
        nt.resolve_pad("Hi Hat", PADS)
    with pytest.raises(ValueError, match="No pad 'Cowbell'.*Kick \\(36/C1\\)"):
        nt.resolve_pad("Cowbell", PADS)
    with pytest.raises(ValueError, match="No pad '40'"):
        nt.resolve_pad("40", PADS)


def test_step_strings():
    hits, n = nt.parse_steps("x.X. o..9 | 1...", 100, 127, 60)
    assert n == 12 and hits == [(0, 100), (2, 127), (4, 60), (7, 127), (8, 14)]
    hits, n = nt.parse_steps({"steps": "x.x.", "velocities": [110, 0, 70, 0]}, 100, 127, 60)
    assert hits == [(0, 110), (2, 70)]
    with pytest.raises(ValueError, match="unknown character 'k'"):
        nt.parse_steps("x.k.", 100, 127, 60)


def test_drum_rows_repeat_independently():
    notes, rows = nt.drum_pattern_notes({"Kick": "x...", "Closed Hi Hat": "xxx"}, PADS, 0.25, 4.0)
    kicks = [n["start_time"] for n in notes if n["pitch"] == 36]
    hats = [n["start_time"] for n in notes if n["pitch"] == 42]
    assert kicks == [0, 1, 2, 3] and len(hats) == 16       # 3-step row tiles across 16 steps
    assert rows[1]["note_name"] == "F#1" and rows[0]["notes"] == 4


# -- tools against a scripted Remote Script -------------------------------------

class Live:
    """Answers the commands the note tools use and keeps per-slot clips."""

    def __init__(self, pads=PADS, groove=None):
        self.pads, self.groove = pads, groove
        self.clips = {}           # slot -> {"length", "name", "notes"}
        self.sent = []

    def send_command(self, cmd, params=None):
        params = params or {}
        self.sent.append(cmd)
        slot = params.get("clip_index")
        if cmd == "get_track_info":
            return {"clip_slots": [
                {"index": i, "has_clip": i in self.clips,
                 "clip": ({"name": self.clips[i]["name"], "length": self.clips[i]["length"],
                           "groove": self.groove} if i in self.clips else None)}
                for i in range(8)]}
        if cmd == "create_clip":
            assert slot not in self.clips
            self.clips[slot] = {"length": params["length"], "name": "", "notes": []}
            return {}
        if cmd == "set_clip_name":
            self.clips[slot]["name"] = params["name"]
            return {}
        if cmd == "add_notes_to_clip":
            self.clips[slot]["notes"] += params["notes"]
            return {"note_count": len(params["notes"])}
        if cmd == "clear_notes_from_clip":
            self.clips[slot]["notes"] = []
            return {}
        if cmd == "get_drum_pads":
            return {"pads": self.pads}
        if cmd == "get_clip_notes":
            c = self.clips[slot]
            return {"clip_name": c["name"], "length": c["length"], "note_count": len(c["notes"]),
                    "notes": [dict(n, note_id=i + 1) for i, n in enumerate(c["notes"])]}
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def live(monkeypatch):
    conn = Live()
    monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
    return conn


def test_create_clip_names_and_fills_in_one_call(live):
    out = call(server.create_clip, None, 0, 1, length=16, name="Bass",
               notes=[[36, 0, 0.5, 100], [36, 1.5, 0.5, 90], [39, 3, 1, 100]], repeat_every_beats=4)
    clip = live.clips[1]
    assert clip["name"] == "Bass" and len(clip["notes"]) == 12
    assert "12 notes (3-note pattern repeated)" in out


def test_create_clip_reports_an_inherited_groove(monkeypatch):
    conn = Live(groove="Swing 16ths 66")
    monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
    assert "groove 'Swing 16ths 66' is assigned" in call(server.create_clip, None, 0, 0, length=4)


def test_add_notes_warns_about_notes_past_the_end(live):
    live.clips[0] = {"length": 4.0, "name": "", "notes": []}
    out = call(server.add_notes_to_clip, None, 0, 0, [[60, 0, 1, 100], [60, 4, 1, 100], [60, 5, 1, 100]])
    assert "WARNING: 2 note(s) start at or after the clip end (beat 4)" in out


def test_add_notes_repeat_until_and_bad_input(live):
    live.clips[0] = {"length": 16.0, "name": "", "notes": []}
    call(server.add_notes_to_clip, None, 0, 0, [[42, 0, 0.25, 80], [42, 0.5, 0.25, 60]],
         repeat_every_beats=1, repeat_until_beats=8)
    assert len(live.clips[0]["notes"]) == 16
    out = call(server.add_notes_to_clip, None, 0, 0, [[60, 0, 1, 200]])
    assert out.startswith("Error adding notes to clip:") and "velocity 200" in out
    assert len(live.clips[0]["notes"]) == 16                # nothing written on error


def test_drum_pattern_creates_a_clip_from_the_longest_row(live):
    out = call(server.write_drum_pattern, None, 0, 2,
               {"Kick": "x...x...x...x...", "Clap": "....x.......x...", "Closed": "x.x.x.x.x.x.x.X."},
               name="Drums A")
    clip = live.clips[2]
    assert clip["length"] == 4.0 and clip["name"] == "Drums A"
    by_pitch = {}
    for n in clip["notes"]:
        by_pitch.setdefault(n["pitch"], []).append((n["start_time"], n["velocity"]))
    assert [t for t, _ in by_pitch[36]] == [0, 1, 2, 3]
    assert [t for t, _ in by_pitch[39]] == [1, 3]
    assert by_pitch[42][-1] == (3.5, 127.0)               # accent
    assert "Kick -> 36 (C1)" in out and "Clap -> 39 (D#1)" in out


def test_drum_pattern_replaces_existing_notes_and_fills_the_clip(live):
    live.clips[0] = {"length": 16.0, "name": "Old", "notes": [{"pitch": 38, "start_time": 0}]}
    call(server.write_drum_pattern, None, 0, 0, {"Kick": "x..."}, step_beats=0.25)
    assert [n["pitch"] for n in live.clips[0]["notes"]] == [36] * 16
    call(server.write_drum_pattern, None, 0, 0, {"Snare": "..x."}, replace=False)
    assert len(live.clips[0]["notes"]) == 32


def test_drum_pattern_rejects_unknown_pads_without_writing(live):
    out = call(server.write_drum_pattern, None, 0, 3, {"Kick": "x...", "Cowbell": "x..."})
    assert "No pad 'Cowbell'" in out
    assert "add_notes_to_clip" not in live.sent


def test_compact_read_back(live):
    live.clips[0] = {"length": 4.0, "name": "Hook",
                     "notes": nt.normalize_notes([[67, 1, 0.5, 90], [65, 0, 1, 100, True]])}
    out = json.loads(call(server.get_clip_notes, None, 0, 0, compact=True))
    assert out["notes"] == [[65, 0.0, 1.0, 100, True], [67, 1.0, 0.5, 90]]


def test_descriptions_steer_to_compact_tools():
    assert "write_drum_pattern" in server.add_notes_to_clip.__doc__
    assert "repeat_every_beats" in server.add_notes_to_clip.__doc__
    assert "compact" in server.create_clip.__doc__
    assert "write_drum_pattern" in server.mcp.instructions
