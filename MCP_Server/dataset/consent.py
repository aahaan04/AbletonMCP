"""Persistent consent state for dataset recording.

The env-var opt-in (``ABLETON_MCP_ENABLE_DATASET``) is invisible to anyone who
does not read the docs, so in practice nobody was ever asked. This module lets
the question be asked in the chat itself, once, and remembers the answer.

Three states, and the distinction matters:

    UNKNOWN:  never answered. Recording is OFF. Nothing prompts for an
               answer; the user opts in by saying so.
    GRANTED:  the user said yes, in their own words. Recording is ON.
    DENIED:   the user said no. Recording is OFF, permanently, and the question
               is never asked again.

Default-off for UNKNOWN makes this opt-in: nothing is recorded until the user
actively says yes, in the chat (set_dataset_consent) or with
``ABLETON_MCP_ENABLE_DATASET``. A client that cannot render the prompt, or a
user who never reads it, contributes nothing. Dataset rows contain prompts,
MIDI, and device state, so silence is treated as no.

``ABLETON_MCP_DISABLE_DATASET`` still overrides everything, and the env-var
opt-in works for headless/CI use where no one can answer a chat prompt.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ableton-mcp-dataset")

UNKNOWN = "unknown"
GRANTED = "granted"
DENIED = "denied"

# Bump ONLY when what gets collected materially changes: a new category of
# data, not a reworded prompt. A bump re-opens the question for people who
# already granted, because they consented to the older, narrower scope and
# cannot be assumed to accept the new one.
#
# A bump deliberately does NOT re-ask anyone who said no. Re-prompting a denial
# with the same question is nagging; the only honest reason to return to a
# declined user is to tell them the scope changed, and that is a different
# message than this one. Until that message exists, DENIED is final.
CONSENT_VERSION = 1

# Consent is per-install, not per-project: the same person answering once should
# not be re-asked because they opened a different Live set.
_STATE_DIR = Path(
    os.environ.get("ABLETON_MCP_STATE_DIR", "")
    or (Path.home() / ".ableton-mcp")
)
_STATE_FILE = _STATE_DIR / "consent.json"

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _read_state() -> dict[str, Any]:
    """Load persisted consent, tolerating a missing or corrupt file."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("consent state is not an object")
        _cache = data
    except FileNotFoundError:
        _cache = {}
    except Exception as e:
        # A damaged file must not wedge the server into a state where it can
        # neither record nor re-ask. Treat it as never-asked.
        logger.debug("Consent state unreadable (%s); treating as unasked", e)
        _cache = {}
    return _cache


def _write_state(state: dict[str, Any]) -> None:
    global _cache
    _cache = state
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        # Losing the answer means re-asking next session: annoying, not fatal.
        logger.warning("Could not persist dataset consent: %s", e)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def consent_state() -> str:
    """Return UNKNOWN / GRANTED / DENIED, honouring env overrides.

    The kill switch wins outright. The env opt-in counts as a grant so headless
    and CI setups keep working without anyone to answer a prompt.

    UNKNOWN is reported as-is rather than collapsed into DENIED: the prompting
    logic needs to tell "never answered" from "said no" so it knows to still
    ask. Whether UNKNOWN records is a separate question, answered by
    ``recording_allowed``.
    """
    if _env_flag("ABLETON_MCP_DISABLE_DATASET"):
        return DENIED
    if _env_flag("ABLETON_MCP_ENABLE_DATASET"):
        return GRANTED
    with _lock:
        state = _read_state()
        answer = state.get("state", UNKNOWN)
        if answer != GRANTED:
            return answer
        # A grant is only valid for the scope it was given under. If collection
        # has widened since, fall back to UNKNOWN: recording stops and the
        # question is asked again. A stale grant must never authorise data the
        # user was never told about.
        #
        # A grant written before this field existed is treated as version 1,
        # not as invalid. Those users were asked about, and agreed to, exactly
        # the scope version 1 describes; the field's absence is our bookkeeping
        # gap, not a gap in their consent, and re-asking them would be nagging.
        stored = state.get("consent_version", 1)
        if not isinstance(stored, int) or stored < CONSENT_VERSION:
            return UNKNOWN
        return GRANTED


def recording_allowed() -> bool:
    """True when consent permits recording. Opt-in: only GRANTED counts as yes.

    An explicit grant (the user agreeing in chat or in the client dialog, or
    ``ABLETON_MCP_ENABLE_DATASET``) is required. Never having answered means
    no recording, so an unanswered or unrenderable prompt contributes nothing.
    """
    return consent_state() == GRANTED


def record_consent(granted: bool, quote: str | None = None) -> str:
    """Persist the user's answer. ``quote`` is what they actually said.

    Storing the raw phrasing matters: consent relayed through a model is only
    as good as the model's faithfulness, so keep the evidence for later audit.
    """
    state = GRANTED if granted else DENIED
    with _lock:
        payload = dict(_read_state())
        payload.update({
            "state": state,
            "answered_at": time.time(),
            "user_said": (quote or "").strip()[:500] or None,
            # Pin the answer to the scope it was given under, so a later
            # widening of what is collected re-opens the question.
            "consent_version": CONSENT_VERSION,
            # A fresh answer clears the ask-throttle. It is only there to stop
            # an unanswered prompt repeating within a session, and this is an
            # answer.
            "last_prompted_at": None,
        })
        _write_state(payload)
    logger.info("Dataset consent recorded: %s (v%d)", state, CONSENT_VERSION)
    return state


def needs_prompt() -> bool:
    """True when the question is open: never answered, or answered under an
    older collection scope that has since widened."""
    return consent_state() == UNKNOWN


# Shown once, in the server's MCP instructions, never inside a tool result.
# Tool output that tells the agent to stop and ask the user derails autonomous
# runs and reads like prompt injection, so the question is never pushed: the
# user opts in by saying so, and the agent records it with set_dataset_consent.
CONSENT_INFO = (
    "Dataset recording is off. Only if the user asks to contribute their "
    "sessions to the open Ableton MCP dataset, call set_dataset_consent with "
    "their answer (uploads prompts, MIDI, track/clip names and device settings; "
    "email addresses and file paths are stripped first). Never ask about it "
    "unprompted."
)

def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
