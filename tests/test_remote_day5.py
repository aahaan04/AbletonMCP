"""
Remote Script additions for E2E run 1: device parameters on returns and the
master, shallow device reads, get_meters, in-Live browser search, the global
groove amount in session info, and clear_clip_groove via a neutral groove.

Run from the repo root:
    uv run pytest -v
"""

import pytest

from tests.test_racks import Param, Device, Chain
from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class Mixer:
    volume = Param("Volume", 0.85)
    panning = Param("Pan", 0.0, -1, 1)


class Track:
    def __init__(self, name, devices=(), level=0.0, slots=()):
        self.name, self.devices, self.clip_slots = name, list(devices), list(slots)
        self.mixer_device = Mixer()
        self.output_meter_level = self.output_meter_left = self.output_meter_right = level


class Groove:
    def __init__(self, name, timing=100.0):
        self.name = name
        self.timing_amount, self.random_amount = timing, 0.0
        self.velocity_amount, self.quantization_amount = 0.0, 0.0


class Item:
    def __init__(self, name, children=(), loadable=True, uri=None):
        self.name, self.children, self.uri = name, list(children), uri or name
        self.is_folder, self.is_loadable = bool(children), loadable


class Browser:
    def __init__(self, song):
        self.song = song
        utility = Item("Utility", [Item("Quantize 16.agr"), Item("Quantize 16T.agr")], loadable=False)
        core = Item("Core Library", [Item("Grooves", [utility], loadable=False)], loadable=False)
        self.packs = Item("Packs", [core], loadable=False)
        self.user_library = Item("User Library", [], loadable=False)
        bass = Item("Bass", [Item(f"Bass {i}.adg") for i in range(40)] + [Item("Sub Bass Deep.adg")], loadable=False)
        self.sounds = Item("Sounds", [bass, Item("Pad", [Item("Warm Pad.adg")], loadable=False)], loadable=False)
        self.drums = Item("Drums", [Item("808 Core Kit.adg")], loadable=False)
        for attr in ("instruments", "audio_effects", "midi_effects"):
            setattr(self, attr, Item(attr, [], loadable=False))

    def load_item(self, item):
        self.song.groove_pool.grooves.append(Groove(item.name[:-4]))


class RenameRefused(Groove):
    def __setattr__(self, key, value):
        if key == "name" and "name" in self.__dict__:
            raise RuntimeError("read-only")
        super().__setattr__(key, value)


class Song:
    signature_numerator, signature_denominator, tempo = 4, 4, 124.0
    is_playing, current_song_time, groove_amount = True, 33.0, 0.0
    song_length, loop, loop_start, loop_length = 256.0, False, 0.0, 16.0

    def __init__(self):
        rack = Device("909 Kit", chains=[Chain(f"Pad {i}", [Device("Simpler", [Param("Decay", 0.5)] * 50)])
                                         for i in range(16)], class_name="DrumGroupDevice")
        clip = type("Clip", (), {"name": "Hats", "groove": None})()
        slot = type("Slot", (), {"has_clip": True, "clip": clip})()
        self.tracks = [Track("Drums", [rack], 0.6, [slot]), Track("Bass", [], 0.3)]
        self.return_tracks = [Track("A-Reverb", [Device("Reverb", [Param("Decay Time", 2.0, 0.2, 60)],
                                                        class_name="Reverb")], 0.1)]
        self.master_track = Track("Main", [Device("Limiter", [Param("Ceiling", 0.0, -24.0, 0.0)],
                                                  class_name="Limiter")], 0.7)
        self.groove_pool = type("Pool", (), {})()
        self.groove_pool.grooves = [Groove("Swing 16ths 66")]


@pytest.fixture
def live(script_module):  # noqa: F811
    song = Song()
    inst = make_instance(script_module, application=type("App", (), {"browser": Browser(song)})())
    inst._song = song
    return inst


def test_master_and_return_device_parameters(live):
    out = live._get_device_parameters(0, 0, "master", include_chains=False)
    assert out["track_name"] == "Main" and out["device"]["name"] == "Limiter"
    res = live._set_device_parameter(0, 0, None, -0.3, track_type="master", parameter_name="ceiling")
    assert (res["name"], res["old_value"], res["value"]) == ("Ceiling", 0.0, -0.3)
    res = live._set_device_parameter(0, 0, None, 8.0, track_type="return", parameter_name="Decay Time")
    assert live._song.return_tracks[0].devices[0].parameters[1].value == 8.0
    with pytest.raises(ValueError, match="takes -24.0..0.0"):
        live._set_device_parameter(0, 0, None, 3.0, track_type="master", parameter_name="Ceiling")
    with pytest.raises(IndexError, match="return index 4 out of range"):
        live._get_device_parameters(4, 0, "return")


def test_rack_reads_are_shallow_on_request(live):
    shallow = live._get_device_parameters(0, 0, include_chains=False)["device"]
    deep = live._get_device_parameters(0, 0)["device"]
    assert "chains" not in shallow and shallow["chain_count"] == 16
    assert len(deep["chains"]) == 16 and len(str(deep)) > 20 * len(str(shallow))


def test_meters_in_one_call(live):
    m = live._get_meters()
    assert (m["song_time"], m["is_playing"]) == (33.0, True)
    assert [(t["name"], t["level"]) for t in m["tracks"]] == [("Drums", 0.6), ("Bass", 0.3)]
    assert m["returns"][0]["level"] == 0.1 and m["master"]["level"] == 0.7
    assert "clip_slots" not in m["tracks"][0]


def test_session_info_reports_groove_amount(live):
    assert live._get_session_info()["groove_amount"] == 0.0


def test_search_browser(live):
    out = live._search_browser("sub deep", ["sounds"])
    assert [r["path"] for r in out["results"]] == ["sounds/Bass/Sub Bass Deep.adg"]
    assert live._search_browser("bass", ["sounds"], limit=5)["count"] == 5
    assert live._search_browser("808", None)["results"][0]["name"] == "808 Core Kit.adg"
    assert live._search_browser("x", ["nope"])["unknown_roots"] == ["nope"]
    with pytest.raises(ValueError, match="query is empty"):
        live._search_browser("  ", None)


def test_clear_clip_groove_builds_one_neutral_groove(live):
    out = live._clear_clip_groove(0, 0)
    pool = live._song.groove_pool.grooves
    assert out["groove"] == "AbletonMCP No Groove" and len(pool) == 2
    assert live._is_neutral(pool[1]) and pool[0].timing_amount == 100.0   # default groove untouched
    live._clear_clip_groove(0, 0)
    assert len(pool) == 2                                                  # reused, not reloaded


def test_clear_clip_groove_when_live_refuses_the_rename(live, monkeypatch):
    browser = live.application().browser
    monkeypatch.setattr(browser, "load_item",
                        lambda item: live._song.groove_pool.grooves.append(RenameRefused(item.name[:-4])))
    assert live._clear_clip_groove(0, 0)["groove"] == "Quantize 16"
    live._clear_clip_groove(0, 0)
    assert len(live._song.groove_pool.grooves) == 2                       # found again by zero amounts


def test_clear_clip_groove_refuses_a_tampered_neutral_groove(live):
    live._clear_clip_groove(0, 0)
    live._song.groove_pool.grooves[1].timing_amount = 50.0
    with pytest.raises(Exception, match="no longer has all amounts at 0"):
        live._clear_clip_groove(0, 0)
