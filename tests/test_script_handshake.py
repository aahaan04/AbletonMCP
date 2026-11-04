"""
Capability gating when the startup handshake did not succeed.

The MCP host launches the server at app start, often before Live is open, so
the one-shot handshake in server_lifespan fails and no script info is cached.
Gated tools (clear_notes_from_clip, delete_clip, get_clip_notes, ...) then
refused with "missing capability" for the rest of the session, even against
an up-to-date Remote Script. These tests pin the re-handshake that fixes it.

Run from the repo root:
    uv run pytest -v
"""

import asyncio
import inspect

import pytest

import MCP_Server.script_handshake as sh
import MCP_Server.server as server
from MCP_Server.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION


CURRENT_INFO = {
    "name": "AbletonMCP",
    "script_version": EXPECTED_REMOTE_SCRIPT_VERSION,
    "protocol_version": 1,
    "capabilities": ["get_script_info", "get_clip_notes", "clear_notes_from_clip"],
}


class FakeConnection:
    """Answers get_script_info with ``info`` (or Unknown command when None)
    and every other command with ``response``."""

    def __init__(self, info=CURRENT_INFO, response=None):
        self.info = info
        self.response = {} if response is None else response
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append(command_type)
        if command_type == "get_script_info":
            if self.info is None:
                raise Exception("Unknown command: get_script_info")
            return dict(self.info)
        return self.response


def call(tool, *args, **kwargs):
    result = tool(*args, **kwargs)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


@pytest.fixture(autouse=True)
def _no_handshake_yet(monkeypatch):
    """Start every test in the state a server launched before Live is in."""
    monkeypatch.setattr(sh, "_script_info", None)
    monkeypatch.setattr(sh, "_handshake_done", False)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    class _Dummy:
        def record_event(self, *a, **k):
            pass
    import MCP_Server.telemetry_decorator as td
    import MCP_Server.dataset.consent as consent
    monkeypatch.setattr(td, "get_telemetry", lambda: _Dummy(), raising=False)
    monkeypatch.setattr(consent, "maybe_consent_notice", lambda: "")


def test_without_handshake_capability_is_refused():
    """The bug's precondition: nothing cached, no way to re-check."""
    out = sh.require_capability("clear_notes_from_clip")
    assert out is not None and "missing capability" in out


def test_rehandshake_unlocks_capability_on_current_script():
    conn = FakeConnection()
    assert sh.require_capability("clear_notes_from_clip", conn.send_command) is None
    assert conn.sent == ["get_script_info"]
    assert sh.script_version_ok()


def test_rehandshake_only_when_needed():
    conn = FakeConnection()
    sh.handshake(conn.send_command)
    conn.sent.clear()
    assert sh.require_capability("get_clip_notes", conn.send_command) is None
    assert conn.sent == []


def test_capability_absent_from_live_script_is_still_refused():
    conn = FakeConnection(info=dict(CURRENT_INFO, script_version="1.6.0",
                                    capabilities=["get_script_info"]))
    out = sh.require_capability("clear_notes_from_clip", conn.send_command)
    assert "missing capability 'clear_notes_from_clip'" in out
    assert "loaded='1.6.0'" in out


def test_features_count_as_capabilities():
    """New params on existing commands are advertised as features, not
    commands, and gate the same way."""
    conn = FakeConnection(info=dict(CURRENT_INFO, features=["delete_arrangement_clip"]))
    assert sh.require_capability("delete_arrangement_clip", conn.send_command) is None
    assert "missing capability 'load_on_return_or_master'" in \
        sh.require_capability("load_on_return_or_master", conn.send_command)


def test_legacy_script_is_still_refused():
    conn = FakeConnection(info=None)
    out = sh.require_capability("clear_notes_from_clip", conn.send_command)
    assert "missing capability" in out
    assert "loaded='legacy'" in out


def test_clear_notes_tool_works_when_server_started_before_live(monkeypatch):
    """End to end through the tool: no startup handshake, current script."""
    conn = FakeConnection(response={"clip_name": "Pad", "cleared_count": 3})
    monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
    out = call(server.clear_notes_from_clip, None, 0, 0)
    assert out.startswith("Cleared 3 note"), out
    assert conn.sent == ["get_script_info", "clear_notes_from_clip"]
