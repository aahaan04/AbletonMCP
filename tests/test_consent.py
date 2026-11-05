"""
Dataset consent never interrupts a tool call: with the question unanswered,
tool results come back unchanged (no appended prompt, no client dialog), and
the opt-in is mentioned only in the server's instructions.

Run from the repo root:
    uv run pytest -v
"""

import json

import pytest

import MCP_Server.dataset.consent as consent
import MCP_Server.server as server
from tests.test_clip_notes import call, fake_conn  # noqa: F401  (fixture)
from tests.test_clip_notes import _silence_telemetry  # noqa: F401


@pytest.fixture(autouse=True)
def _unanswered(monkeypatch, tmp_path):
    monkeypatch.setattr(consent, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(consent, "_STATE_FILE", tmp_path / "consent.json")
    monkeypatch.delenv("ABLETON_MCP_ENABLE_DATASET", raising=False)
    consent.reset_for_tests()
    assert consent.consent_state() == consent.UNKNOWN
    yield
    consent.reset_for_tests()


class ElicitingCtx:
    """A client that would show a dialog if asked."""

    def __init__(self):
        self.asked = False

    async def elicit(self, *a, **k):
        self.asked = True
        raise AssertionError("tool call opened a consent dialog")


def test_results_are_untouched_while_consent_is_unanswered(fake_conn):
    fake_conn(response={"tempo": 124.0, "track_count": 4})
    ctx = ElicitingCtx()
    out = call(server.get_session_info, ctx)
    assert json.loads(out)["tempo"] == 124.0          # pure JSON, nothing appended
    assert not ctx.asked
    assert consent.consent_state() == consent.UNKNOWN  # still not recording


def test_opt_in_is_mentioned_once_in_instructions_not_as_an_order():
    text = server.mcp.instructions
    assert text.count("set_dataset_consent") == 1
    assert "Never ask about it unprompted" in text
    assert "Ask the user this now" not in text
