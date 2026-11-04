"""
save_set: the parts that can be tested without driving Live's window —
title parsing, path checks, the refusal rules, and the tool's error text.
The keystroke path is exercised by `ableton_cli.py selftest-save` in Live.

Run from the repo root:
    uv run pytest -v
"""

import sys

import pytest

import MCP_Server.save_set as ss
import MCP_Server.server as server
from tests.test_clip_notes import call, _silence_telemetry  # noqa: F401


@pytest.mark.parametrize("title,expected", [
    ("Untitled* - Ableton Live 12 Trial", ("Untitled", True)),
    ("My Track - Ableton Live 12 Suite", ("My Track", False)),
    ("A - B* - Ableton Live 12 Trial", ("A - B", True)),
])
def test_parse_title(title, expected):
    assert ss.parse_title(title) == expected


def test_parse_title_refuses_other_windows():
    with pytest.raises(ss.SaveError, match="Unexpected Live window title"):
        ss.parse_title("Untitled - Notepad")


def test_target_paths(tmp_path):
    typed, candidates = ss.target_paths(str(tmp_path), "My Track.als")
    assert typed == tmp_path / "My Track.als"
    assert candidates[1] == tmp_path / "My Track Project" / "My Track.als"
    for folder, name, error in ((str(tmp_path), "a/b", "invalid file name"),
                                ("relative", "x", "absolute"),
                                (str(tmp_path / "missing"), "x", "does not exist")):
        with pytest.raises(ss.SaveError, match=error):
            ss.target_paths(folder, name)


def _fake_live(monkeypatch, title):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ss, "find_live_window", lambda: (1, 2, title), raising=False)
    sent = []
    monkeypatch.setattr(ss, "bring_to_front", lambda h: sent.append("front"), raising=False)
    monkeypatch.setattr(ss, "send_save_shortcut", lambda h, save_as: sent.append(("keys", save_as)), raising=False)
    return sent


def test_untitled_set_needs_a_name(monkeypatch):
    sent = _fake_live(monkeypatch, "Untitled* - Ableton Live 12 Trial")
    with pytest.raises(ss.SaveError, match="never been saved"):
        ss.save_set()
    assert sent == []


def test_clean_set_is_not_touched(monkeypatch):
    sent = _fake_live(monkeypatch, "Song - Ableton Live 12 Trial")
    assert ss.save_set()["note"] == "no unsaved changes" and sent == []


def test_existing_target_is_never_overwritten(monkeypatch, tmp_path):
    (tmp_path / "Song Project").mkdir()
    (tmp_path / "Song Project" / "Song.als").write_bytes(b"x")
    sent = _fake_live(monkeypatch, "Untitled* - Ableton Live 12 Trial")
    with pytest.raises(ss.SaveError, match="already exists"):
        ss.save_set(str(tmp_path), "Song")
    assert sent == []


def test_half_given_save_as_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ss.SaveError, match="both folder and filename"):
        ss.save_set(folder="C:\\x")


def test_tool_reports_errors_as_text(monkeypatch):
    monkeypatch.setattr(ss, "save_set", lambda folder, filename: (_ for _ in ()).throw(ss.SaveError("nope")))
    assert call(server.save_set, None) == "Error saving Set: nope"
