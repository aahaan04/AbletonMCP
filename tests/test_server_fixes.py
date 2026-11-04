"""
Server-side fixes from E2E run 1: playhead read-back, shallow device reads,
set_device_parameter by name / track_type, browser search and listing limits,
and the groove tools (several clips, 0% warning, clear_clip_groove).

Run from the repo root:
    uv run pytest -v
"""

import json

import pytest

import MCP_Server.server as server
from tests.test_clip_notes import call, _silence_telemetry  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401


class Scripted:
    """Answers commands from a dict of handlers; records what was sent."""

    def __init__(self, **handlers):
        self.handlers, self.sent = handlers, []

    def send_command(self, cmd, params=None):
        self.sent.append((cmd, params or {}))
        handler = self.handlers.get(cmd)
        if handler is None:
            raise AssertionError(f"unexpected {cmd}")
        return handler(params or {}) if callable(handler) else handler


@pytest.fixture
def use(monkeypatch):
    monkeypatch.setattr(server, "_sleep", lambda s: None)

    def _use(conn):
        monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
        return conn
    return _use


def gate(monkeypatch, missing=()):
    import MCP_Server.script_handshake as sh
    monkeypatch.setattr(sh, "require_capability",
                        lambda name, send_command=None: f"missing {name}" if name in missing else None)


# -- playhead -----------------------------------------------------------------

def test_set_arrangement_time_reports_the_applied_position(use):
    state = {"t": 240.0, "reads": 0}

    def session(_):
        state["reads"] += 1
        if state["reads"] >= 2:            # Live applies the move a tick later
            state["t"] = 136.0
        return {"current_song_time": state["t"], "is_playing": False}

    use(Scripted(set_current_song_time={"current_song_time": 240.0}, get_session_info=session))
    assert call(server.set_arrangement_time, None, 136) == "Playhead moved to beat 136"


# -- device parameters --------------------------------------------------------

RACK = {"track_index": 0, "device": {
    "name": "909 Kit", "parameters": [{"name": "Device On"}, {"name": "Macro 1"}, {"name": "Drive Amount"}],
    "chains": [{"devices": [{"parameters": [{"name": "x"}] * 200}]}] * 16}}


def test_device_read_is_shallow_by_default(use, monkeypatch):
    gate(monkeypatch)
    conn = use(Scripted(get_device_parameters=lambda p: json.loads(json.dumps(RACK))))
    out = json.loads(call(server.get_device_parameters, None, 0, 0))
    assert "chains" not in out["device"] and out["device"]["chain_count"] == 16
    assert conn.sent[0][1]["include_chains"] is False
    deep = json.loads(call(server.get_device_parameters, None, 0, 0, include_nested=True))
    assert len(deep["device"]["chains"]) == 16


def test_device_read_name_filter_and_master(use, monkeypatch):
    gate(monkeypatch)
    conn = use(Scripted(get_device_parameters=lambda p: json.loads(json.dumps(RACK))))
    out = json.loads(call(server.get_device_parameters, None, 0, 1, track_type="master", name_filter="drive"))
    assert [p["name"] for p in out["device"]["parameters"]] == ["Drive Amount"]
    assert conn.sent[0][1]["track_type"] == "master"


def test_master_and_named_params_need_the_feature(use, monkeypatch):
    gate(monkeypatch, missing={"device_params_anywhere"})
    conn = use(Scripted())
    assert call(server.get_device_parameters, None, 0, 0, track_type="return") == "missing device_params_anywhere"
    assert call(server.set_device_parameter, None, 0, 0, value=1.0, parameter_name="Ceiling",
                track_type="master") == "missing device_params_anywhere"
    assert conn.sent == []


def test_set_device_parameter_by_name_on_master(use, monkeypatch):
    gate(monkeypatch)
    conn = use(Scripted(set_device_parameter={"name": "Ceiling", "old_value": 0.0, "value": -0.3,
                                              "display": "-0.3 dB"}))
    out = call(server.set_device_parameter, None, 0, 1, value=-0.3, parameter_name="Ceiling", track_type="master")
    assert out == "Set Ceiling 0.0 → -0.3 (-0.3 dB)"
    assert conn.sent[0][1] == {"track_index": 0, "device_index": 1, "value": -0.3,
                               "track_type": "master", "parameter_name": "Ceiling"}


def test_set_device_parameter_positional_call_still_works(use):
    conn = use(Scripted(set_device_parameter={"name": "Gain", "old_value": 0, "value": 1}))
    call(server.set_device_parameter, None, 2, 0, 3, 1.0)
    assert conn.sent[0][1] == {"track_index": 2, "device_index": 0, "value": 1.0, "parameter_index": 3}
    assert "exactly one of" in call(server.set_device_parameter, None, 2, 0, 3, 1.0, parameter_name="Gain")


# -- browser -----------------------------------------------------------------

TREE = {
    "sounds": [{"name": "Bass", "is_folder": True}, {"name": "Pad", "is_folder": True}],
    "sounds/Bass": [{"name": f"Bass Preset {i}.adg", "is_loadable": True, "uri": f"u{i}"} for i in range(300)]
    + [{"name": "Sub Bass Deep.adg", "is_loadable": True, "uri": "sub"}],
    "sounds/Pad": [{"name": "Warm Pad.adg", "is_loadable": True, "uri": "pad"}],
}


def listing(p):
    return {"path": p["path"], "items": TREE.get(p["path"], [])}


def test_listing_is_capped_and_filterable(use):
    use(Scripted(get_browser_items_at_path=listing))
    out = json.loads(call(server.get_browser_items_at_path, None, "sounds/Bass"))
    assert len(out["items"]) == 100 and out["total_items"] == 301 and "narrow with" in out["truncated"]
    out = json.loads(call(server.get_browser_items_at_path, None, "sounds/Bass", name_filter="sub deep"))
    assert [i["name"] for i in out["items"]] == ["Sub Bass Deep.adg"]


def test_search_falls_back_to_walking_folders(use, monkeypatch):
    gate(monkeypatch, missing={"search_browser"})
    use(Scripted(get_browser_items_at_path=listing))
    out = json.loads(call(server.search_browser, None, "deep sub", category="sounds"))
    assert [r["uri"] for r in out["results"]] == ["sub"] and out["results"][0]["path"] == "sounds/Bass/Sub Bass Deep.adg"
    assert json.loads(call(server.search_browser, None, "preset", category="sounds", limit=5))["count"] == 5


def test_search_uses_the_remote_command_when_available(use, monkeypatch):
    gate(monkeypatch)
    conn = use(Scripted(search_browser={"results": [{"name": "Warm Pad.adg"}], "count": 1}))
    call(server.search_browser, None, "warm pad")
    assert conn.sent[0] == ("search_browser", {"query": "warm pad", "limit": 25, "loadable_only": True,
                                               "roots": list(server._SEARCH_ROOTS)})


# -- grooves -----------------------------------------------------------------

def test_assign_groove_to_several_clips_warns_at_zero(use, monkeypatch):
    gate(monkeypatch)
    conn = use(Scripted(assign_groove=lambda p: {"clip_name": f"Drums {p['clip_index']}", "groove": "Swing 16ths 54"},
                        get_groove_pool={"global_amount": 0.0, "grooves": []}))
    out = call(server.assign_groove, None, 0, 1, clip_indexes=[0, 2, 3])
    assert out.count("groove: Swing 16ths 54") == 3 and "global groove amount is 0%" in out
    assert [p["clip_index"] for c, p in conn.sent if c == "assign_groove"] == [0, 2, 3]
    assert "exactly one of clip_index" in call(server.assign_groove, None, 0, 1)


def test_assign_groove_no_warning_when_audible(use, monkeypatch):
    gate(monkeypatch)
    use(Scripted(assign_groove={"clip_name": "Hats", "groove": "Swing"}, get_groove_pool={"global_amount": 1.0}))
    assert "WARNING" not in call(server.assign_groove, None, 0, 0, clip_index=1)


def test_clear_clip_groove_is_gated_and_loops(use, monkeypatch):
    gate(monkeypatch, missing={"clear_clip_groove"})
    use(Scripted())
    assert call(server.clear_clip_groove, None, 0, clip_index=0) == "missing clear_clip_groove"
    gate(monkeypatch)
    conn = use(Scripted(clear_clip_groove=lambda p: {"clip_name": "Bass", "groove": "AbletonMCP No Groove"}))
    out = call(server.clear_clip_groove, None, 1, clip_indexes=[0, 1])
    assert out.count("now plays straight") == 2 and len(conn.sent) == 2
