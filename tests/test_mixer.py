"""
Server-side tests for the mixer tools (get_mixer, set_track_volume,
set_track_panning, set_send, set_track_mute, set_track_solo) with the Ableton
connection mocked.

Run from the repo root:
    uv run pytest -v
"""

import json

import pytest

import MCP_Server.server as server
from tests.test_clip_notes import call, FakeConnection, fake_conn  # noqa: F401  (fixture)
from tests.test_clip_notes import _silence_telemetry, _no_consent_prompt  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401


def test_set_volume_forwards_db_and_track_type(fake_conn):
    conn = fake_conn(response={"name": "Bass", "display": "-6.0 dB"})
    out = call(server.set_track_volume, None, 2, db=-6.0, track_type="return")
    assert conn.sent == [("set_track_volume", {"track_index": 2, "db": -6.0,
                                                "value": None, "track_type": "return"})]
    assert out == "Set volume of 'Bass' to -6.0 dB"


def test_set_volume_needs_db_or_value(fake_conn):
    conn = fake_conn()
    assert call(server.set_track_volume, None, 0).startswith("Error: provide either db or value")
    assert conn.sent == []


@pytest.mark.parametrize("tool,flag,on,word", [
    ("set_track_mute", "mute", True, "Muted"),
    ("set_track_mute", "mute", False, "Unmuted"),
    ("set_track_solo", "solo", True, "Soloed"),
    ("set_track_solo", "solo", False, "Unsoloed"),
])
def test_mute_and_solo(fake_conn, tool, flag, on, word):
    conn = fake_conn(response={"name": "Drums", flag: on})
    out = call(getattr(server, tool), None, 3, on)
    assert conn.sent == [(tool, {"track_index": 3, flag: on, "track_type": "track"})]
    assert out == f"{word} 'Drums'"


def test_get_mixer_returns_json(fake_conn):
    payload = {"name": "Keys", "mute": False, "solo": False, "sends": []}
    fake_conn(response=payload)
    assert json.loads(call(server.get_mixer, None, 1)) == payload


def test_mixer_tools_are_gated(monkeypatch, fake_conn):
    import MCP_Server.script_handshake as sh
    seen = []
    monkeypatch.setattr(sh, "require_capability",
                        lambda name, send_command=None: seen.append(name) or "missing")
    conn = fake_conn()
    for tool, args in (("get_mixer", (0,)), ("set_track_volume", (0, -3.0)),
                       ("set_track_panning", (0, 0.1)), ("set_send", (0, 0, -3.0)),
                       ("set_track_mute", (0, True)), ("set_track_solo", (0, True))):
        assert call(getattr(server, tool), None, *args) == "missing"
    assert seen == ["get_mixer", "set_track_volume", "set_track_panning",
                    "set_send", "set_track_mute", "set_track_solo"]
    assert conn.sent == []
