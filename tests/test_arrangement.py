"""
Arrangement building (fill_arrangement, fill_arrangement_section,
set_arrangement_loop, get_arrangement_layout, locator primitives) against
fake Live objects, plus the server's locator sequencing with the connection
mocked.

Run from the repo root:
    uv run pytest -v
"""

import pytest

import MCP_Server.server as server
from tests.test_clip_notes import call, fake_conn  # noqa: F401  (fixture)
from tests.test_clip_notes import _silence_telemetry, _no_consent_prompt  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401
from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class ArrClip:
    def __init__(self, name, start, length):
        self.name, self.start_time, self.end_time = name, float(start), float(start) + length


class SessionClip:
    def __init__(self, name, length):
        self.name, self.length = name, float(length)


class Slot:
    def __init__(self, clip=None):
        self.clip, self.has_clip = clip, clip is not None


class Track:
    def __init__(self, name, clips=(), arrangement=()):
        self.name = name
        self.clip_slots = [Slot(c) for c in clips] + [Slot()]
        self.arrangement_clips = list(arrangement)

    def duplicate_clip_to_arrangement(self, clip, t):
        self.arrangement_clips.append(ArrClip(clip.name, t, clip.length))

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)


class Cue:
    def __init__(self, time, name=""):
        self.time, self.name = time, name


class Song:
    signature_numerator, signature_denominator = 4, 4
    loop, loop_start, loop_length = False, 0.0, 16.0
    is_playing = False

    def __init__(self):
        self.tracks = [Track("Drums", [SessionClip("Beat", 16)]),          # 4 bars
                       Track("Bass", [SessionClip("Line", 8)],              # 2 bars
                             arrangement=[ArrClip("Old", 64, 16)]),         # bars 17-20
                       Track("Pad", [SessionClip("Swell", 12)])]            # 3 bars
        self.cue_points = []
        self.current_song_time = 0.0

    def set_or_delete_cue(self):
        hit = next((c for c in self.cue_points if c.time == self.current_song_time), None)
        if hit:
            self.cue_points.remove(hit)
        else:
            self.cue_points.append(Cue(self.current_song_time))


@pytest.fixture
def live(script_module):  # noqa: F811
    inst = make_instance(script_module)
    inst._song = Song()
    return inst


def starts(track):
    return sorted(c.start_time for c in track.arrangement_clips)


def test_fill_places_whole_repeats_at_bar_positions(live):
    out = live._fill_arrangement(0, 0, 17, 32)          # 4-bar clip over 16 bars
    assert out["copies"] == 4 and out["start_bars"] == [17, 21, 25, 29]
    assert starts(live._song.tracks[0]) == [64, 80, 96, 112]


def test_fill_refuses_partial_repeats(live):
    with pytest.raises(ValueError, match="not a whole number of repeats"):
        live._fill_arrangement(2, 0, 1, 4)              # 3-bar clip in 4 bars


def test_fill_refuses_clips_in_the_way_unless_replace(live):
    with pytest.raises(ValueError, match="already has clips in bars 17-24"):
        live._fill_arrangement(1, 0, 17, 24)
    out = live._fill_arrangement(1, 0, 17, 24, replace=True)
    assert out["replaced"] == 1 and starts(live._song.tracks[1]) == [64, 72, 80, 88]


def test_replace_never_cuts_a_clip_sticking_out(live):
    with pytest.raises(ValueError, match="stick out"):
        live._fill_arrangement(1, 0, 19, 20, replace=True)
    assert [c.name for c in live._song.tracks[1].arrangement_clips] == ["Old"]


def test_section_is_all_or_nothing(live):
    with pytest.raises(ValueError, match="not a whole number"):
        live._fill_arrangement_section(1, 8, [{"track_index": 0, "clip_index": 0},
                                              {"track_index": 2, "clip_index": 0}])
    assert live._song.tracks[0].arrangement_clips == []
    out = live._fill_arrangement_section(1, 8, [{"track_index": 0, "clip_index": 0},
                                                {"track_index": 1, "clip_index": 0}])
    assert [p["copies"] for p in out["placed"]] == [2, 4]


def test_section_refuses_a_track_twice_and_empty_slots(live):
    with pytest.raises(ValueError, match="once per section"):
        live._fill_arrangement_section(1, 4, [{"track_index": 0, "clip_index": 0}] * 2)
    with pytest.raises(ValueError, match="No clip in slot 1"):
        live._fill_arrangement_section(1, 4, [{"track_index": 0, "clip_index": 1}])


def test_bars_follow_the_time_signature(live):
    live._song.signature_numerator, live._song.signature_denominator = 6, 8   # 3 beats/bar
    out = live._fill_arrangement(2, 0, 5, 12)            # 12-beat clip = 4 bars of 6/8
    assert out["start_bars"] == [5, 9] and starts(live._song.tracks[2]) == [12, 24]


def test_bad_bar_ranges(live):
    with pytest.raises(ValueError, match="Bars start at 1"):
        live._fill_arrangement(0, 0, 0, 4)
    with pytest.raises(ValueError, match="before start_bar"):
        live._fill_arrangement(0, 0, 9, 5)


def test_loop_in_bars(live):
    out = live._set_arrangement_loop(9, 16)
    assert (live._song.loop_start, live._song.loop_length, live._song.loop) == (32.0, 32.0, True)
    assert (out["start_bar"], out["end_bar"]) == (9.0, 16.0)


def test_layout_reports_bars_gaps_and_overlaps(live):
    live._fill_arrangement(0, 0, 1, 4)
    live._fill_arrangement(0, 0, 9, 12)
    live._song.tracks[0].arrangement_clips.append(ArrClip("Stray", 40, 16))   # bars 11-14
    live._song.cue_points.append(Cue(32.0, "Drop"))
    out = live._get_arrangement_layout([0])
    drums = out["tracks"][0]
    assert [(c["start_bar"], c["end_bar"]) for c in drums["clips"]] == [(1, 4), (9, 12), (11, 14)]
    assert drums["gaps"] == [{"from_bar": 5, "to_bar": 8}]
    assert drums["overlaps"] == [{"clips": ["Beat", "Stray"], "from_bar": 11, "to_bar": 12}]
    assert out["locators"][0]["bar"] == 9 and out["locators"][0]["name"] == "Drop"


def test_cue_toggle_refuses_while_playing(live):
    live._song.is_playing = True
    with pytest.raises(ValueError, match="Stop playback"):
        live._toggle_cue_at_playhead()


# -- server: locator sequencing ----------------------------------------------

class LocatorConn:
    """Simulates Live: a playhead move is visible to the next command, and
    toggle/rename act on the cue list."""

    def __init__(self, cues=(), playing=False):
        self.cues = [dict(c) for c in cues]
        self.playhead, self.playing = 0.0, playing
        self.sent = []

    def send_command(self, cmd, params=None):
        params = params or {}
        self.sent.append(cmd)
        if cmd == "get_locators":
            return {"beats_per_bar": 4.0, "locators": [dict(c) for c in self.cues]}
        if cmd == "get_session_info":
            return {"current_song_time": self.playhead, "is_playing": self.playing}
        if cmd == "set_current_song_time":
            self.playhead = params["time"]
            return {}
        if cmd == "toggle_cue_at_playhead":
            hit = next((c for c in self.cues if c["time"] == self.playhead), None)
            if hit:
                self.cues.remove(hit)
            else:
                self.cues.append({"name": "", "time": self.playhead})
            return {"playhead": self.playhead}
        if cmd == "rename_locator":
            cue = next(c for c in self.cues if c["time"] == params["time"])
            cue["name"] = params["name"]
            return {"name": cue["name"], "bar": cue["time"] / 4 + 1}
        raise AssertionError(cmd)


@pytest.fixture
def locator_conn(monkeypatch):
    monkeypatch.setattr(server, "_LOCATOR_SETTLE_SEC", 0)

    def _make(**kw):
        conn = LocatorConn(**kw)
        monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
        return conn
    return _make


def test_set_locator_moves_toggles_renames_and_restores(locator_conn):
    conn = locator_conn()
    conn.playhead = 10.0
    assert call(server.set_locator, None, "Drop", 17) == "Added locator 'Drop' at bar 17"
    assert conn.cues == [{"name": "Drop", "time": 64.0}] and conn.playhead == 10.0


def test_set_locator_renames_existing_without_toggling(locator_conn):
    conn = locator_conn(cues=[{"name": "old", "time": 64.0}])
    assert call(server.set_locator, None, "Drop", 17) == "Renamed locator 'Drop' at bar 17"
    assert "toggle_cue_at_playhead" not in conn.sent


def test_set_locator_refuses_while_playing(locator_conn):
    conn = locator_conn(playing=True)
    assert "Stop playback first" in call(server.set_locator, None, "Drop", 17)
    assert conn.cues == []


def test_delete_locator_checks_name(locator_conn):
    conn = locator_conn(cues=[{"name": "Intro", "time": 0.0}])
    assert "Refusing to delete" in call(server.delete_locator, None, 1, expected_name="Drop")
    assert call(server.delete_locator, None, 1, expected_name="Intro") == "Deleted locator 'Intro' at bar 1"
    assert conn.cues == []


def test_fill_tool_message(fake_conn):
    conn = fake_conn(response={"track_name": "Drums", "clip_name": "Beat", "copies": 4,
                               "start_bars": [17, 21, 25, 29], "replaced": 0})
    out = call(server.fill_arrangement, None, 0, 0, 17, 32)
    assert out == "Placed 'Beat' on 'Drums' 4x at bars 17, 21, 25, 29"
    assert conn.sent[0] == ("fill_arrangement", {"track_index": 0, "clip_index": 0,
                                                 "start_bar": 17, "end_bar": 32, "replace": False})


def test_tool_descriptions_explain_bars():
    for tool in (server.fill_arrangement, server.fill_arrangement_section):
        assert "inclusive" in tool.__doc__ and "get_arrangement_layout" in tool.__doc__
    assert "bar 17 = beat 64" in server.set_locator.__doc__
