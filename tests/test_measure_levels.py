"""
measure_levels: windowed meter sampling with a fake clock and a fake Live
whose meters show a stale held peak for the first seconds of playback.

Run from the repo root:
    uv run pytest -v
"""

import json
import threading

import pytest

import MCP_Server.server as server
from tests.test_clip_notes import call, _silence_telemetry  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Live:
    """Two tracks. 'Hook' is silent but shows a stale 0.74 for 2.5 s after
    playback starts; 'Drums' reads 0.5 in bars 1-8 (Intro) and 0.8 after
    (Drop). 120 BPM, 4/4: song time advances 2 beats per second."""

    def __init__(self, clock, meters_command=False):
        self.clock, self.meters_command = clock, meters_command
        self.playing, self.song_time, self.started_at = False, 0.0, None
        self.sent = []

    def _now_time(self):
        if not self.playing:
            return self.song_time
        return self.song_time + 2.0 * (self.clock.t - self.started_at)

    def _levels(self):
        t = self._now_time()
        stale = self.playing and self.clock.t - self.started_at < 2.5
        drums = 0.0 if not self.playing else (0.5 if t < 32 else 0.8)
        hook = 0.74 if stale else 0.0
        return drums, hook, max(drums, hook) if self.playing else 0.0

    def send_command(self, cmd, params=None):
        params = params or {}
        self.sent.append(cmd)
        drums, hook, master = self._levels()
        if cmd == "get_session_info":
            return {"is_playing": self.playing, "current_song_time": self._now_time(), "tempo": 120.0,
                    "signature_numerator": 4, "signature_denominator": 4, "track_count": 2,
                    "master_track": {"output_meter_level": master}}
        if cmd == "get_track_info":
            i = params["track_index"]
            return {"name": ["Drums", "Hook"][i], "output_meter_level": [drums, hook][i]}
        if cmd == "get_meters" and self.meters_command:
            return {"song_time": self._now_time(), "is_playing": self.playing,
                    "tracks": [{"index": 0, "name": "Drums", "level": drums},
                               {"index": 1, "name": "Hook", "level": hook}],
                    "returns": [{"index": 0, "name": "A-Reverb", "level": drums / 2}],
                    "master": {"level": master}}
        if cmd == "start_playback":
            self.playing, self.started_at = True, self.clock.t
            return {}
        if cmd == "stop_playback":
            self.song_time, self.playing = self._now_time(), False
            return {}
        if cmd == "set_current_song_time":
            self.song_time = params["time"]
            return {}
        if cmd == "get_locators":
            return {"beats_per_bar": 4.0, "locators": [{"name": "Intro", "time": 0.0},
                                                        {"name": "Drop", "time": 32.0}]}
        raise AssertionError(f"unexpected {cmd}")


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(server, "_now", c.now)
    monkeypatch.setattr(server, "_sleep", c.sleep)
    return c


def run(monkeypatch, live, **kw):
    monkeypatch.setattr(server, "get_ableton_connection", lambda: live)
    return json.loads(call(server.measure_levels, None, **kw))


def test_stale_peak_is_discarded_and_playback_restored(monkeypatch, clock):
    import MCP_Server.script_handshake as sh
    monkeypatch.setattr(sh, "require_capability", lambda name, send_command=None: "missing")
    live = Live(clock)
    out = run(monkeypatch, live, seconds=4, start_bar=1)
    tracks = {t["name"]: t for t in out["tracks"]}
    assert tracks["Hook"]["peak"] == 0.0                     # stale 0.74 never counted
    assert tracks["Drums"]["peak"] == 0.5 and tracks["Drums"]["mean"] == 0.5
    assert out["source"] == "track info polling" and out["samples"] >= 39
    assert not live.playing and live.song_time == 0.0        # stopped and playhead restored


def test_without_settling_the_stale_peak_shows(monkeypatch, clock):
    import MCP_Server.script_handshake as sh
    monkeypatch.setattr(sh, "require_capability", lambda name, send_command=None: "missing")
    out = run(monkeypatch, Live(clock), seconds=4, start_bar=1, settle_seconds=0)
    assert {t["name"]: t for t in out["tracks"]}["Hook"]["peak"] == 0.74


def test_sections_and_fast_meters(monkeypatch, clock):
    live = Live(clock, meters_command=True)
    out = run(monkeypatch, live, seconds=20, start_bar=5, by_section=True)
    assert out["source"] == "get_meters" and out["returns"][0]["name"] == "A-Reverb"
    intro = {t["name"]: t for t in out["sections"]["Intro"]["tracks"]}
    drop = {t["name"]: t for t in out["sections"]["Drop"]["tracks"]}
    assert intro["Drums"]["peak"] == 0.5 and drop["Drums"]["peak"] == 0.8
    assert out["master"]["peak"] == 0.8 and out["master"]["clipping"] is False
    assert "get_track_info" not in live.sent


def test_refuses_absurd_windows(monkeypatch, clock):
    monkeypatch.setattr(server, "get_ableton_connection", lambda: Live(clock, meters_command=True))
    out = call(server.measure_levels, None, seconds=6000)
    assert out.startswith("Error measuring levels: seconds must be")
    out = call(server.measure_levels, None, bars=1000)
    assert "1000 bars is 2000 s at this tempo" in out


@pytest.fixture
def no_jobs(monkeypatch):
    monkeypatch.setattr(server, "_level_jobs", {})
    yield
    for job in list(server._level_jobs.values()):
        job.cancelled = True


def test_whole_song_in_background_is_one_result(monkeypatch, clock, no_jobs):
    """64 bars at 120 BPM = 128 s: far over one call, measured in a thread
    (instant with the fake clock) and returned as one combined result."""
    live = Live(clock, meters_command=True)
    out = run(monkeypatch, live, start_bar=1, bars=64, by_section=True)
    assert out["seconds"] == 128.0 and out["samples"] >= 1270
    assert out["sections"]["Drop"]["master"]["peak"] == 0.8
    assert not live.playing and live.song_time == 0.0
    assert server._level_jobs == {}


def _gated(monkeypatch, clock):
    """Make the job thread wait on `gate` at every sleep, like real playback."""
    gate = threading.Event()

    def sleep(s):
        gate.wait()
        clock.t += s
    monkeypatch.setattr(server, "_sleep", sleep)
    monkeypatch.setattr(server, "_CALL_BUDGET_SEC", 0.05)
    return gate


def test_long_measurement_returns_a_job_and_is_collected_later(monkeypatch, clock, no_jobs):
    gate = _gated(monkeypatch, clock)
    live = Live(clock, meters_command=True)
    first = run(monkeypatch, live, start_bar=1, seconds=60)
    assert first["status"] == "running" and "measure_levels(job_id=" in first["next"]
    busy = call(server.measure_levels, None, seconds=5)
    assert "still running" in busy and first["job_id"] in busy
    gate.set()
    monkeypatch.setattr(server, "_CALL_BUDGET_SEC", 5.0)
    out = json.loads(call(server.measure_levels, None, job_id=first["job_id"]))
    assert out["calls"] == 2 and out["seconds"] == 60 and out["samples"] >= 590
    assert not live.playing and server._level_jobs == {}
    assert "no running measurement" in call(server.measure_levels, None, job_id=first["job_id"])


def test_cancel_returns_what_was_measured(monkeypatch, clock, no_jobs):
    gate = _gated(monkeypatch, clock)
    live = Live(clock, meters_command=True)
    first = run(monkeypatch, live, start_bar=1, seconds=60)
    monkeypatch.setattr(server, "_CALL_BUDGET_SEC", 5.0)
    threading.Timer(0.05, gate.set).start()
    out = json.loads(call(server.measure_levels, None, job_id=first["job_id"], cancel=True))
    assert "cancelled_after_seconds" in out and out["cancelled_after_seconds"] < 60
    assert not live.playing
