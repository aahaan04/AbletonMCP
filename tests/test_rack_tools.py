"""
Server-side tests for the rack tools (get_drum_pads,
get_nested_device_parameters, set_nested_device_parameter) with the Ableton
connection mocked.

Run from the repo root:
    uv run pytest -v
"""

import json

import MCP_Server.server as server
from tests.test_clip_notes import call, fake_conn  # noqa: F401  (fixture)
from tests.test_clip_notes import _silence_telemetry, _no_consent_prompt  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401


def test_get_drum_pads_forwards_and_returns_json(fake_conn):
    payload = {"rack_name": "808", "pads": [{"note": 36, "note_name": "C1", "name": "Kick"}]}
    conn = fake_conn(response=payload)
    assert json.loads(call(server.get_drum_pads, None, 2)) == payload
    assert conn.sent == [("get_drum_pads", {"track_index": 2, "device_index": None})]


def test_nested_get_forwards_full_address(fake_conn):
    conn = fake_conn(response={"location": "808 > Kick (C1) > Kick", "device": {}})
    call(server.get_nested_device_parameters, None, 1, 0, pad_note=36, path=[0, 0])
    assert conn.sent == [("get_nested_device_parameters", {
        "track_index": 1, "device_index": 0, "chain_index": None, "pad_note": 36,
        "nested_device_index": 0, "path": [0, 0]})]


def test_nested_set_reports_location_and_change(fake_conn):
    conn = fake_conn(response={"location": "808 > Kick (C1) > Kick", "name": "Decay",
                               "old_value": 0.5, "value": 0.8, "display": "80 %"})
    out = call(server.set_nested_device_parameter, None, 1, 0, 0.8,
               pad_note=36, parameter_name="Decay")
    assert out == "Set 808 > Kick (C1) > Kick > Decay 0.5 → 0.8 (80 %)"
    assert conn.sent[0][1]["parameter_name"] == "Decay"
    assert conn.sent[0][1]["parameter_index"] is None


def test_rack_tools_are_gated(monkeypatch, fake_conn):
    import MCP_Server.script_handshake as sh
    monkeypatch.setattr(sh, "require_capability", lambda name, send_command=None: f"missing {name}")
    conn = fake_conn()
    assert call(server.get_drum_pads, None, 0) == "missing get_drum_pads"
    assert call(server.get_nested_device_parameters, None, 0, 0, chain_index=0) == \
        "missing get_nested_device_parameters"
    assert call(server.set_nested_device_parameter, None, 0, 0, 0.5, chain_index=0,
                parameter_index=1) == "missing set_nested_device_parameter"
    assert conn.sent == []
