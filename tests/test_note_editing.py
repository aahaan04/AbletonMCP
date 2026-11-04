"""
In-place note editing (modify_notes, delete_notes) against a fake clip that
behaves like Live's note API: get_notes_extended returns note objects with
note_id, apply_note_modifications writes them back by id, and
remove_notes_by_id deletes by id.

Run from the repo root:
    uv run pytest -v
"""

import pytest

from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class Note:
    def __init__(self, note_id, pitch, start, duration=0.25, velocity=100, mute=False):
        self.note_id, self.pitch, self.start_time = note_id, pitch, start
        self.duration, self.velocity, self.mute = duration, velocity, mute

    def copy(self):
        return Note(self.note_id, self.pitch, self.start_time, self.duration, self.velocity, self.mute)


class Clip:
    name, is_midi_clip, length = "Hats", True, 4.0

    def __init__(self, notes):
        self.notes = {n.note_id: n for n in notes}
        self.applied = 0

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return [n.copy() for n in sorted(self.notes.values(), key=lambda n: (n.start_time, n.pitch))]

    def apply_note_modifications(self, notes):
        self.applied += 1
        for n in notes:
            self.notes[n.note_id] = n.copy()

    def remove_notes_by_id(self, ids):
        for i in ids:
            del self.notes[i]


def pattern():
    # kick 36 on beats 0 and 2, hat 42 on every 8th
    notes = [Note(1, 36, 0.0), Note(2, 36, 2.0)]
    notes += [Note(10 + i, 42, i * 0.5, velocity=80) for i in range(8)]
    return Clip(notes)


@pytest.fixture
def live(script_module):  # noqa: F811
    inst = make_instance(script_module)
    clip = pattern()

    class Slot:
        has_clip = True

    Slot.clip = clip

    class Track:
        name, clip_slots = "Drums", [Slot()]

    class Song:
        tracks = [Track()]

    inst._song = Song()
    inst.clip = clip
    return inst


def test_transpose_by_ids_keeps_others(live):
    out = live._modify_notes(0, 0, {"note_ids": [1, 2]}, {"transpose": 2})
    assert out["modified"] == 2 and [n["pitch"] for n in out["notes"]] == [38, 38]
    assert live.clip.notes[10].pitch == 42 and live.clip.applied == 1


def test_velocity_on_offbeat_hats_by_range(live):
    live._modify_notes(0, 0, {"pitch_min": 42, "pitch_max": 42, "time_min": 0.5, "time_max": 4},
                       {"velocity_delta": -30})
    vel = {n.start_time: n.velocity for n in live.clip.notes.values() if n.pitch == 42}
    assert vel[0.0] == 80 and vel[0.5] == 50 and vel[3.5] == 50


def test_shift_duration_mute_together(live):
    out = live._modify_notes(0, 0, {"note_ids": [11]}, {"shift": 0.1, "duration": 0.125, "mute": True})
    n = out["notes"][0]
    assert (n["start_time"], n["duration"], n["mute"]) == (pytest.approx(0.6), 0.125, True)


@pytest.mark.parametrize("selection,changes,error", [
    ({"note_ids": [1]}, {"transpose": 100}, "pitch 136"),
    ({"note_ids": [1]}, {"velocity_delta": -100}, "velocity 0"),
    ({"note_ids": [1]}, {"shift": -1}, "before the clip"),
    ({"note_ids": [1]}, {"duration": 0}, "duration 0"),
    ({"note_ids": [1]}, {"pitch": 40, "transpose": 1}, "pitch or transpose"),
    ({"note_ids": [1]}, {"swing": 1}, "Unknown change"),
    ({"note_ids": [1]}, {}, "changes is empty"),
    ({"note_ids": [99]}, {"transpose": 1}, r"No notes with id\(s\) \[99\]"),
    ({"note_ids": [1], "pitch_min": 36}, {"transpose": 1}, "not both or neither"),
    ({}, {"transpose": 1}, "selection is required"),
    ({"pitch_min": 60, "pitch_max": 70}, {"transpose": 1}, "matched no notes"),
])
def test_invalid_edits_change_nothing(live, selection, changes, error):
    before = {i: (n.pitch, n.velocity, n.start_time) for i, n in live.clip.notes.items()}
    with pytest.raises(ValueError, match=error):
        live._modify_notes(0, 0, selection, changes)
    assert {i: (n.pitch, n.velocity, n.start_time) for i, n in live.clip.notes.items()} == before
    assert live.clip.applied == 0


def test_one_bad_note_blocks_the_whole_batch(live):
    # The kick at beat 0 would go negative, so the kick at beat 2 must not move either.
    with pytest.raises(ValueError, match="before the clip"):
        live._modify_notes(0, 0, {"pitch_min": 36, "pitch_max": 36}, {"shift": -0.5})
    assert live.clip.notes[2].start_time == 2.0


def test_delete_by_id_and_by_range(live):
    out = live._delete_notes(0, 0, {"note_ids": [2]})
    assert out["deleted"] == 1 and out["remaining"] == 9 and 2 not in live.clip.notes
    out = live._delete_notes(0, 0, {"pitch_min": 42, "pitch_max": 42, "time_min": 2, "time_max": 4})
    assert out["deleted"] == 4 and out["remaining"] == 5


def test_delete_refuses_empty_selection_and_no_match(live):
    with pytest.raises(ValueError, match="selection is required"):
        live._delete_notes(0, 0, {})
    with pytest.raises(ValueError, match="matched no notes"):
        live._delete_notes(0, 0, {"time_min": 10})
