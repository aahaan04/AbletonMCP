"""
save_set: the parts that can be tested without driving Live's window:
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
                                ("relative", "x", "absolute")):
        with pytest.raises(ss.SaveError, match=error):
            ss.target_paths(folder, name)


@pytest.fixture
def roots(monkeypatch, tmp_path):
    music, docs = tmp_path / "Music", tmp_path / "Documents"
    music.mkdir()
    docs.mkdir()
    monkeypatch.setattr(ss, "allowed_create_roots", lambda: [music, docs])
    return music, docs


def test_missing_folder_inside_allowed_roots_is_planned(roots):
    music, docs = roots
    assert ss.folders_to_create(str(music)) == []
    assert ss.folders_to_create(str(docs / "Tracks" / "Run 3")) == [docs / "Tracks", docs / "Tracks" / "Run 3"]


@pytest.mark.parametrize("where", ["outside", "root_sibling", "dotdot"])
def test_missing_folder_elsewhere_is_refused(roots, tmp_path, where):
    music, _ = roots
    folder = {"outside": tmp_path / "Elsewhere" / "x",
              "root_sibling": tmp_path / "MusicX",               # prefix match is not containment
              "dotdot": music / ".." / "Escaped"}[where]
    with pytest.raises(ss.SaveError, match="only creates folders inside"):
        ss.folders_to_create(str(folder))
    assert not (tmp_path / "Elsewhere").exists() and not (tmp_path / "Escaped").exists()


def test_existing_folder_anywhere_is_fine(tmp_path, roots):
    assert ss.folders_to_create(str(tmp_path)) == []


def test_refusal_happens_before_live_is_touched(monkeypatch, roots, tmp_path):
    sent = _fake_live(monkeypatch, "Untitled* - Ableton Live 12 Trial")
    monkeypatch.setattr(ss, "find_live_window", lambda: sent.append("find") or (1, 2, "x"), raising=False)
    with pytest.raises(ss.SaveError, match="only creates folders inside"):
        ss.save_set(str(tmp_path / "Nope"), "Song")
    assert sent == []


def _fake_save_as(monkeypatch, succeed):
    sent = _fake_live(monkeypatch, "Untitled* - Ableton Live 12 Trial")
    state = {"title": "Untitled* - Ableton Live 12 Trial"}

    def fill(dialog, path):
        if not succeed:
            raise ss.SaveError("dialog trouble")
        from pathlib import Path
        p = Path(path)
        (p.parent / f"{p.stem} Project").mkdir()
        (p.parent / f"{p.stem} Project" / p.name).write_bytes(b"als")
        state["title"] = f"{p.stem} - Ableton Live 12 Trial"

    monkeypatch.setattr(ss, "wait_for_save_dialog", lambda pid: 7, raising=False)
    monkeypatch.setattr(ss, "fill_and_confirm", fill, raising=False)
    monkeypatch.setattr(ss, "close_dialog", lambda d: sent.append("closed"), raising=False)
    monkeypatch.setattr(ss, "_title", lambda h: state["title"], raising=False)
    return sent


def test_save_as_creates_the_folder(monkeypatch, roots):
    _, docs = roots
    _fake_save_as(monkeypatch, succeed=True)
    out = ss.save_set(str(docs / "AbletonMCP" / "Run 3"), "Song")
    assert out["path"] == str(docs / "AbletonMCP" / "Run 3" / "Song Project" / "Song.als")
    assert out["created_folder"] == str(docs / "AbletonMCP")


def test_failed_save_removes_the_folders_it_created(monkeypatch, roots):
    _, docs = roots
    sent = _fake_save_as(monkeypatch, succeed=False)
    with pytest.raises(ss.SaveError, match="dialog trouble"):
        ss.save_set(str(docs / "AbletonMCP" / "Run 3"), "Song")
    assert "closed" in sent and not (docs / "AbletonMCP").exists()


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
