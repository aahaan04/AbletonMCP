"""
Delete commands: Remote Script handlers against fake Live objects, and the
server tools with the connection mocked.

Run from the repo root:
    uv run pytest -v
"""

import pytest

import MCP_Server.server as server
from tests.test_clip_notes import call, fake_conn  # noqa: F401  (fixture)
from tests.test_clip_notes import _silence_telemetry, _no_consent_prompt  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401
from tests.test_racks import Device, drum_kit
from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class Clip:
    def __init__(self, name, start_time=0.0):
        self.name, self.start_time = name, start_time


class Slot:
    def __init__(self, clip=None):
        self.clip = clip

    @property
    def has_clip(self):
        return self.clip is not None

    def delete_clip(self):
        self.clip = None


class Track:
    def __init__(self, name, devices=(), slots=(), arrangement=()):
        self.name, self.devices = name, list(devices)
        self.clip_slots = list(slots)
        self.arrangement_clips = list(arrangement)

    def delete_device(self, index):
        del self.devices[index]

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)


class Song:
    def __init__(self):
        self.tracks = [
            Track("Drums", [drum_kit(), Device("Glue")],
                  slots=[Slot(Clip("Beat")), Slot()],
                  arrangement=[Clip("Intro", 0.0), Clip("Drop", 16.0)]),
            Track("Bass", [Device("Operator")]),
        ]
        self.return_tracks = [Track("A-Reverb"), Track("B-Delay")]
        self.master_track = Track("Main", [Device("Limiter")])

    def delete_track(self, index):
        del self.tracks[index]

    def delete_return_track(self, index):
        del self.return_tracks[index]


@pytest.fixture
def live(script_module):  # noqa: F811
    inst = make_instance(script_module)
    inst._song = Song()
    return inst


def test_delete_session_clip_reports_name(live):
    assert live._delete_clip(0, 0) == {"deleted": True, "view": "session",
                                       "name": "Beat", "track_name": "Drums"}
    assert not live._song.tracks[0].clip_slots[0].has_clip
    assert live._delete_clip(0, 1)["deleted"] is False


def test_delete_arrangement_clip_by_position(live):
    out = live._delete_clip(0, 1, view="arrangement")
    assert (out["name"], out["start_time"]) == ("Drop", 16.0)
    assert [c.name for c in live._song.tracks[0].arrangement_clips] == ["Intro"]


def test_delete_clip_rejects_unknown_view(live):
    with pytest.raises(ValueError, match="view must be"):
        live._delete_clip(0, 0, view="both")


def test_delete_track_and_return_track(live):
    assert live._delete_track(1)["name"] == "Bass"
    assert [t.name for t in live._song.tracks] == ["Drums"]
    assert live._delete_return_track(0)["name"] == "A-Reverb"
    assert [t.name for t in live._song.return_tracks] == ["B-Delay"]


def test_delete_device_on_track_and_master(live):
    assert live._delete_device(0, 1)["name"] == "Glue"
    assert live._delete_device(0, 0, track_type="master")["name"] == "Limiter"
    assert live._song.master_track.devices == []


def test_delete_nested_device_by_pad_and_path(live):
    out = live._delete_nested_device(0, 0, pad_note=36, path=[0, 0])
    assert out["name"] == "Simpler"
    assert out["location"] == "808 Kit > Kick (C1) > Kick > chain 0 'Kick' > Simpler"
    kick_rack = live._song.tracks[0].devices[0].drum_pads[36].chains[0].devices[0]
    assert kick_rack.chains[0].devices == []


@pytest.mark.parametrize("call_args,error", [
    (("_delete_track", 1, "Keys"), "track 1 is named 'Bass', not 'Keys'"),
    (("_delete_return_track", 1, "A-Reverb"), "named 'B-Delay'"),
    (("_delete_device", 0, 1, "track", "Reverb"), "named 'Glue'"),
])
def test_expected_name_mismatch_refuses_and_keeps_target(live, call_args, error):
    method, *args = call_args
    with pytest.raises(ValueError, match=error):
        getattr(live, method)(*args)
    assert len(live._song.tracks) == 2 and len(live._song.return_tracks) == 2
    assert len(live._song.tracks[0].devices) == 2


@pytest.mark.parametrize("method,args", [
    ("_delete_track", (5,)),
    ("_delete_return_track", (-1,)),
    ("_delete_device", (1, 3)),
    ("_delete_clip", (0, 2, "arrangement")),
])
def test_out_of_range_indexes_are_refused(live, method, args):
    with pytest.raises(IndexError):
        getattr(live, method)(*args)


def test_nested_delete_needs_exactly_one_address(live):
    with pytest.raises(ValueError, match="exactly one of chain_index or pad_note"):
        live._delete_nested_device(0, 0)


# -- server tools -----------------------------------------------------------

def test_delete_track_tool_message(fake_conn):
    conn = fake_conn(response={"deleted": True, "name": "Bass", "track_count": 3})
    assert call(server.delete_track, None, 1, expected_name="Bass") == "Deleted track 'Bass'"
    assert conn.sent == [("delete_track", {"track_index": 1, "expected_name": "Bass"})]


def test_delete_nested_device_tool_message(fake_conn):
    fake_conn(response={"deleted": True, "name": "Simpler", "location": "808 > Kick (C1) > Simpler"})
    out = call(server.delete_nested_device, None, 0, 0, pad_note=36)
    assert out == "Deleted device 'Simpler' from 808 > Kick (C1) > Simpler"


def test_arrangement_delete_gates_on_its_own_capability(monkeypatch, fake_conn):
    """An older script ignores view and would delete the Session clip in that
    slot, so view="arrangement" must check the dedicated capability."""
    import MCP_Server.script_handshake as sh
    seen = []
    monkeypatch.setattr(sh, "require_capability",
                        lambda name, send_command=None: seen.append(name) or "missing")
    conn = fake_conn()
    call(server.delete_clip, None, 0, 1, view="arrangement")
    call(server.delete_clip, None, 0, 1)
    assert seen == ["delete_arrangement_clip", "delete_clip"]
    assert conn.sent == []
