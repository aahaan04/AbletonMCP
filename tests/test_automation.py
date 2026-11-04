"""
Clip automation envelopes (get/write/clear_clip_automation): Remote Script
handlers against fake Live objects, and the server tools with the connection
mocked.

Run from the repo root:
    uv run pytest -v
"""

import pytest

import MCP_Server.server as server
from tests.test_clip_notes import call, fake_conn  # noqa: F401  (fixture)
from tests.test_clip_notes import _silence_telemetry, _no_consent_prompt  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401
from tests.test_racks import Param, Device, drum_kit
from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class Envelope:
    """Records insert_step calls; value_at_time returns the latest step
    covering t, like Live's flat step segments."""

    def __init__(self):
        self.steps = []

    def insert_step(self, time, length, value):
        self.steps.append((time, length, value))

    def value_at_time(self, t):
        hits = [v for (s, l, v) in self.steps if s <= t < s + l]
        return hits[-1] if hits else self.steps[-1][2]


class Clip:
    def __init__(self, length=32.0, arrangement=False):
        self.name, self.length = "Pad", length
        self.envelopes = {}
        self.arrangement = arrangement

    def automation_envelope(self, param):
        return self.envelopes.get(id(param))

    def create_automation_envelope(self, param):
        if self.arrangement:
            raise RuntimeError("Clip envelopes are not supported for arrangement clips")
        self.envelopes[id(param)] = Envelope()
        return self.envelopes[id(param)]

    def clear_envelope(self, param):
        self.envelopes.pop(id(param), None)

    def clear_all_envelopes(self):
        self.envelopes.clear()


class Slot:
    def __init__(self, clip=None):
        self.clip = clip
        self.has_clip = clip is not None


class Mixer:
    def __init__(self):
        self.volume = Param("Track Volume", 0.85, 0.0, 1.0)
        self.panning = Param("Track Panning", 0.0, -1.0, 1.0)
        self.sends = [Param("A-Reverb", 0.0)]


class Track:
    def __init__(self):
        self.name = "Synth"
        self.mixer_device = Mixer()
        self.devices = [Device("Auto Filter", [Param("Frequency", 0.5), Param("Mode", 0, 0, 3, quantized=True)]),
                        drum_kit()]
        self.clip_slots = [Slot(Clip()), Slot()]
        self.arrangement_clips = [Clip(arrangement=True)]


@pytest.fixture
def live(script_module):  # noqa: F811
    inst = make_instance(script_module)

    class Song:
        tracks = [Track()]

    inst._song = Song()
    return inst


CUTOFF = {"device_index": 0, "parameter_name": "Frequency"}


def test_rising_ramp_is_drawn_and_reads_back_monotonic(live):
    out = live._write_clip_automation(0, 0, CUTOFF, [{"time": 0, "normalized": 0.1},
                                                     {"time": 32, "normalized": 0.9}], resolution=1.0)
    assert out["location"] == "Auto Filter > Frequency"
    assert out["steps"] == 32 + 1   # 32 one-beat ramp steps + tail
    got = live._get_clip_automation(0, 0, CUTOFF, times=[0, 8, 16, 24, 31])
    values = [s["value"] for s in got["samples"]]
    assert got["has_envelope"] and values == sorted(values)
    assert values[0] == pytest.approx(0.1) and values[2] == pytest.approx(0.5)


def test_holds_before_first_and_after_last_point(live):
    live._write_clip_automation(0, 0, CUTOFF, [{"time": 8, "value": 0.2}, {"time": 16, "value": 0.6}],
                                curve="step")
    steps = live._song.tracks[0].clip_slots[0].clip.automation_envelope(
        live._song.tracks[0].devices[0].parameters[1]).steps
    assert steps == [(0.0, 8.0, 0.2), (8.0, 8.0, 0.2), (16.0, 16.0, 0.6)]


def test_mixer_volume_fade_and_quantized_param_rounds(live):
    out = live._write_clip_automation(0, 0, {"mixer": "volume"},
                                      [{"time": 0, "value": 0.85}, {"time": 16, "value": 0.0}])
    assert out["location"] == "Mixer > Volume"
    live._write_clip_automation(0, 0, {"device_index": 0, "parameter_name": "Mode"},
                                [{"time": 0, "value": 1.4}, {"time": 4, "value": 2.0}])
    env = live._song.tracks[0].clip_slots[0].clip.automation_envelope(
        live._song.tracks[0].devices[0].parameters[2])
    assert [v for (_, _, v) in env.steps] == [1.0, 2.0]


def test_nested_rack_parameter_target(live):
    out = live._write_clip_automation(0, 0, {"device_index": 1, "pad_note": 36, "parameter_name": "Decay"},
                                      [{"time": 0, "value": 0.2}])
    assert out["location"] == "808 Kit > Kick (C1) > Kick > Decay"


def test_clear_one_then_all(live):
    live._write_clip_automation(0, 0, CUTOFF, [{"time": 0, "value": 0.3}])
    live._write_clip_automation(0, 0, {"mixer": "panning"}, [{"time": 0, "value": -0.5}])
    live._clear_clip_automation(0, 0, target=CUTOFF)
    assert live._get_clip_automation(0, 0, CUTOFF)["has_envelope"] is False
    assert live._get_clip_automation(0, 0, {"mixer": "panning"})["has_envelope"] is True
    live._clear_clip_automation(0, 0, all_parameters=True)
    assert live._get_clip_automation(0, 0, {"mixer": "panning"})["has_envelope"] is False


@pytest.mark.parametrize("args,kwargs,error", [
    ((CUTOFF, [{"time": 0, "value": 2.0}]), {}, "takes 0.0..1.0"),
    ((CUTOFF, [{"time": 0, "normalized": 1.5}]), {}, "normalized must be 0..1"),
    ((CUTOFF, [{"time": 0, "value": 0.1, "normalized": 0.1}]), {}, "exactly one of value or normalized"),
    ((CUTOFF, [{"time": 40, "value": 0.1}]), {}, "outside the clip"),
    ((CUTOFF, [{"time": 4, "value": 0.1}, {"time": 4, "value": 0.2}]), {}, "Two points at time"),
    ((CUTOFF, []), {}, "At least one point"),
    (({"mixer": "send", "send_index": 3}, [{"time": 0, "value": 0.1}]), {}, "send_index 3 out of range"),
    (({"mixer": "volume", "device_index": 0}, [{"time": 0, "value": 0.1}]), {}, "both a mixer control and a device"),
    ((CUTOFF, [{"time": 0, "value": 0.1}]), {"curve": "exp"}, "curve must be"),
])
def test_bad_writes_are_refused(live, args, kwargs, error):
    with pytest.raises(Exception, match=error):
        live._write_clip_automation(0, 0, *args, **kwargs)


def test_empty_slot_and_clear_without_target_refused(live):
    with pytest.raises(ValueError, match="No clip in slot 1"):
        live._get_clip_automation(0, 1, CUTOFF)
    with pytest.raises(ValueError, match="either target or all_parameters"):
        live._clear_clip_automation(0, 0)


def test_arrangement_refusal_is_reported(live):
    with pytest.raises(Exception, match="refused to create an envelope .* arrangement clip"):
        live._write_clip_automation(0, 0, CUTOFF, [{"time": 0, "value": 0.1}], view="arrangement")


# -- server tools -----------------------------------------------------------

def test_write_tool_forwards_and_summarises(fake_conn):
    conn = fake_conn(response={"location": "Auto Filter > Frequency", "clip_name": "Pad", "points": 2,
                               "start": 0.0, "end": 32.0, "first_display": "200 Hz",
                               "last_display": "8.00 kHz", "steps": 513})
    pts = [{"time": 0, "normalized": 0.1}, {"time": 32, "normalized": 0.9}]
    out = call(server.write_clip_automation, None, 0, 0, CUTOFF, pts)
    assert conn.sent[0] == ("write_clip_automation", {
        "track_index": 0, "clip_index": 0, "target": CUTOFF, "points": pts, "curve": "linear",
        "resolution": 0.0625, "clear_existing": True, "view": "session"})
    assert out == ("Automated Auto Filter > Frequency on clip 'Pad': 2 points over beats 0.0–32.0, "
                   "200 Hz → 8.00 kHz (513 steps)")


def test_automation_tools_are_gated(monkeypatch, fake_conn):
    import MCP_Server.script_handshake as sh
    monkeypatch.setattr(sh, "require_capability", lambda name, send_command=None: f"missing {name}")
    conn = fake_conn()
    assert call(server.get_clip_automation, None, 0, 0, CUTOFF) == "missing get_clip_automation"
    assert call(server.write_clip_automation, None, 0, 0, CUTOFF, [{"time": 0, "value": 0.1}]) == \
        "missing write_clip_automation"
    assert call(server.clear_clip_automation, None, 0, 0, all_parameters=True) == \
        "missing clear_clip_automation"
    assert conn.sent == []


def test_tool_descriptions_explain_units():
    doc = server.write_clip_automation.__doc__
    assert "BEATS" in doc and "normalized" in doc and '{"mixer": "volume"}' in doc
