"""
Remote Script tests for Drum Rack pad listing and nested-device addressing
(get_drum_pads, get_nested_device_parameters, set_nested_device_parameter),
run against fake Live objects.

Run from the repo root:
    uv run pytest -v
"""

import pytest

from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class Param:
    def __init__(self, name, value=0.0, lo=0.0, hi=1.0, quantized=False):
        self.name, self.value, self.min, self.max = name, value, lo, hi
        self.is_quantized = quantized

    def str_for_value(self, v):
        return "%.2f" % v


class Device:
    def __init__(self, name, params=(), chains=None, class_name="OriginalSimpler", drum_pads=None):
        self.name, self.class_name = name, class_name
        self.class_display_name = class_name
        self.parameters = [Param("Device On", 1.0)] + list(params)
        self.can_have_chains = chains is not None
        self.can_have_drum_pads = drum_pads is not None
        self.chains = chains or []
        if drum_pads is not None:
            self.drum_pads = drum_pads


class Chain:
    def __init__(self, name, devices, in_note=None):
        self.name, self.devices = name, devices
        if in_note is not None:
            self.in_note = in_note


class Pad:
    def __init__(self, note, name="", chains=()):
        self.note, self.name, self.chains = note, name, list(chains)
        self.mute = self.solo = False


class Track:
    def __init__(self, name, devices):
        self.name, self.devices = name, devices


def drum_kit():
    """A stock-kit shape: each pad's chain holds an Instrument Rack (with
    macros) wrapping a Simpler; pad 40 is layered with two chains."""
    def pad_rack(name):
        simpler = Device("Simpler", [Param("Transpose", 0.0, -48.0, 48.0, quantized=True)])
        return Device(name, [Param("Decay", 0.5), Param("Tone", 0.5)],
                      chains=[Chain(name, [simpler])], class_name="InstrumentGroupDevice")

    kick = Chain("Kick", [pad_rack("Kick")], in_note=36)
    snare = Chain("Snare", [pad_rack("Snare")], in_note=38)
    layer_a = Chain("Tom A", [pad_rack("Tom A")], in_note=40)
    layer_b = Chain("Tom B", [pad_rack("Tom B")], in_note=40)
    pads = [Pad(n) for n in range(128)]
    pads[36] = Pad(36, "Kick", [kick])
    pads[38] = Pad(38, "Snare", [snare])
    pads[40] = Pad(40, "Tom", [layer_a, layer_b])
    return Device("808 Kit", chains=[kick, snare, layer_a, layer_b],
                  class_name="DrumGroupDevice", drum_pads=pads)


@pytest.fixture
def live(script_module):  # noqa: F811
    inst = make_instance(script_module)
    fx_rack = Device("FX Rack", chains=[
        Chain("Dry", []),
        Chain("Wet", [Device("Reverb", [Param("Decay Time", 2.0, 0.2, 60.0)], class_name="Reverb")]),
    ], class_name="AudioEffectGroupDevice")

    class Song:
        tracks = [Track("Drums", [drum_kit(), fx_rack]), Track("Keys", [Device("Operator")])]

    inst._song = Song()
    return inst


def test_note_names_follow_live_octaves(live):
    assert [live._note_name(n) for n in (0, 36, 38, 60, 127)] == ["C-2", "C1", "D1", "C3", "G8"]


def test_drum_pads_lists_only_filled_pads(live):
    out = live._get_drum_pads(0)
    assert out["device_index"] == 0 and out["rack_name"] == "808 Kit"
    assert [(p["note"], p["note_name"], p["name"]) for p in out["pads"]] == [
        (36, "C1", "Kick"), (38, "D1", "Snare"), (40, "E1", "Tom")]
    kick = out["pads"][0]["chains"][0]
    assert kick["chain_index"] == 0
    assert kick["devices"][0]["name"] == "Kick" and kick["devices"][0]["is_rack"]
    assert [c["chain_index"] for c in out["pads"][2]["chains"]] == [2, 3]


def test_drum_pads_refuses_non_drum_rack_and_missing_rack(live):
    with pytest.raises(ValueError, match="not a Drum Rack"):
        live._get_drum_pads(0, 1)
    with pytest.raises(ValueError, match="0 top-level Drum Racks"):
        live._get_drum_pads(1)


def test_pad_note_reaches_the_pad_rack_macros(live):
    out = live._get_nested_device_parameters(0, 0, pad_note=36)
    assert out["location"] == "808 Kit > Kick (C1) > Kick"
    assert [p["name"] for p in out["device"]["parameters"]] == ["Device On", "Decay", "Tone"]


def test_path_goes_into_the_nested_rack(live):
    out = live._get_nested_device_parameters(0, 0, pad_note=36, path=[0, 0])
    assert out["location"] == "808 Kit > Kick (C1) > Kick > chain 0 'Kick' > Simpler"


def test_chain_index_works_for_audio_effect_racks(live):
    res = live._set_nested_device_parameter(0, 1, 8.0, chain_index=1, parameter_name="decay time")
    assert res["location"] == "FX Rack > chain 1 'Wet' > Reverb"
    assert (res["old_value"], res["value"]) == (2.0, 8.0)


def test_set_by_name_quantizes(live):
    res = live._set_nested_device_parameter(0, 0, 7.4, pad_note=38, path=[0, 0],
                                            parameter_name="Transpose")
    assert res["value"] == 7.0


@pytest.mark.parametrize("kwargs,error", [
    ({}, "exactly one of chain_index or pad_note"),
    ({"chain_index": 0, "pad_note": 36}, "exactly one of chain_index or pad_note"),
    ({"pad_note": 37}, "is empty"),
    ({"pad_note": 40}, "layered chains"),
    ({"chain_index": 9}, "out of range"),
    ({"pad_note": 36, "path": [0]}, "pairs"),
    ({"pad_note": 36, "path": [0, 0, 0, 0]}, "not a rack"),
])
def test_ambiguous_or_bad_addresses_are_refused(live, kwargs, error):
    with pytest.raises(Exception, match=error):
        live._resolve_nested_device(0, 0, **kwargs)


def test_pad_note_refused_on_non_drum_rack(live):
    with pytest.raises(ValueError, match="only applies to Drum Racks"):
        live._resolve_nested_device(0, 1, pad_note=36)


def test_set_refuses_out_of_range_and_unknown_names(live):
    with pytest.raises(ValueError, match="takes 0.0..1.0"):
        live._set_nested_device_parameter(0, 0, 2.0, pad_note=36, parameter_name="Decay")
    with pytest.raises(ValueError, match="0 parameters named 'Tune'"):
        live._set_nested_device_parameter(0, 0, 0.5, pad_note=36, parameter_name="Tune")
    with pytest.raises(ValueError, match="exactly one of parameter_index or parameter_name"):
        live._set_nested_device_parameter(0, 0, 0.5, pad_note=36)
