"""Save the open Live Set by driving Live's own Save command (Windows only).

Live's API has no save, so this is UI automation. It is deliberately narrow:

* Live's main window is found by its window class AND its process's
  executable path, never by title alone.
* Live is brought to the foreground without sending any key: we attach to
  the foreground thread's input queue and call SetForegroundWindow. Keys are
  only sent after GetForegroundWindow() is verified to be that exact window,
  and the check is repeated immediately before every SendInput.
* Save As never types: the path goes into the dialog's file-name box with
  WM_SETTEXT and the dialog's own Save button is pressed with BM_CLICK, after
  checking the dialog belongs to Live's process.
* Anything unexpected (Live not found, focus not obtained, dialog missing,
  target already exists) returns an error instead of guessing.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

LIVE_WINDOW_CLASS = "Ableton Live Window Class"
DIALOG_CLASS = "#32770"
_TITLE = re.compile(r"^(?P<name>.*?)(?P<dirty>\*)? - Ableton Live\b")


class SaveError(Exception):
    pass


def parse_title(title: str) -> tuple[str, bool]:
    """('Untitled', True) from 'Untitled* - Ableton Live 12 Trial'."""
    m = _TITLE.match(title or "")
    if not m:
        raise SaveError(f"Unexpected Live window title {title!r}")
    return m.group("name"), bool(m.group("dirty"))


def allowed_create_roots() -> list[Path]:
    """Folders under which save_set may create a missing target folder."""
    home = Path.home()
    return [home / "Music", home / "Documents"]


def _norm(p) -> str:
    return os.path.normcase(os.path.realpath(p))


def folders_to_create(folder: str) -> list[Path]:
    """The missing folders (outermost first) that Save As into `folder` needs.

    An existing folder is fine anywhere. A missing one is only created inside
    one of allowed_create_roots(); anything else raises before Live is touched.
    """
    if not folder or not os.path.isabs(folder):
        raise SaveError("folder must be an absolute path")
    if os.path.isdir(folder):
        return []
    if os.path.exists(folder):
        raise SaveError(f"{folder!r} exists but is not a folder")
    target = _norm(folder)
    roots = [r for r in allowed_create_roots() if os.path.isdir(r)]
    inside = any(target != _norm(r) and os.path.commonpath([target, _norm(r)]) == _norm(r)
                 for r in roots)
    if not inside:
        allowed = " or ".join(str(r) for r in allowed_create_roots())
        raise SaveError(f"folder {folder!r} does not exist, and save_set only creates folders "
                        f"inside {allowed}; create it yourself or choose a folder there")
    missing, p = [], Path(os.path.abspath(folder))
    while not p.exists():
        missing.append(p)
        p = p.parent
    return list(reversed(missing))


def create_folders(missing: list[Path]) -> None:
    for p in missing:
        try:
            p.mkdir()
        except OSError as e:
            remove_folders(missing)
            raise SaveError(f"could not create folder {str(p)!r}: {e.strerror or e}")


def remove_folders(created: list[Path]) -> None:
    """Undo create_folders after a failed save (only folders left empty)."""
    for p in reversed(created):
        try:
            p.rmdir()
        except OSError:
            pass


def target_paths(folder: str, filename: str) -> tuple[Path, list[Path]]:
    """The path to type into Save As, and where Live may put the file (it
    creates a '<name> Project' folder when saving outside a project)."""
    if not folder or not os.path.isabs(folder):
        raise SaveError("folder must be an absolute path")
    name = filename[:-4] if filename.lower().endswith(".als") else filename
    if not name or re.search(r'[\\/:*?"<>|]', name):
        raise SaveError(f"invalid file name {filename!r}")
    base = Path(folder)
    typed = base / f"{name}.als"
    return typed, [typed, base / f"{name} Project" / f"{name}.als"]


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    for _fn in ("IsWindowVisible", "IsIconic", "BringWindowToTop", "SetForegroundWindow"):
        getattr(user32, _fn).argtypes = [wintypes.HWND]
        getattr(user32, _fn).restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WM_SETTEXT, WM_GETTEXT, BM_CLICK = 0x000C, 0x000D, 0x00F5
    SW_RESTORE, IDOK = 9, 1
    INPUT_KEYBOARD, KEYEVENTF_KEYUP = 1, 0x0002
    VK_CONTROL, VK_SHIFT, VK_S = 0x11, 0x10, 0x53

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    def _class(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _title(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, buf, 1024)
        return buf.value

    def _pid(hwnd) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _exe(pid: int) -> str:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            kernel32.CloseHandle(handle)

    def _is_live_exe(path: str) -> bool:
        name = os.path.basename(path)
        return name.lower().startswith("ableton live") and name.lower().endswith(".exe")

    def _top_windows(pred) -> list:
        found = []

        @WNDENUMPROC
        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd) and pred(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        return found

    def _children(parent) -> list:
        found = []

        @WNDENUMPROC
        def cb(hwnd, _):
            found.append(hwnd)
            return True

        user32.EnumChildWindows(parent, cb, 0)
        return found

    def find_live_window():
        """(hwnd, pid, title) of Live's main window, or raise."""
        wins = _top_windows(lambda h: _class(h) == LIVE_WINDOW_CLASS)
        live = [(h, _pid(h)) for h in wins]
        live = [(h, p) for h, p in live if _is_live_exe(_exe(p))]
        if not live:
            raise SaveError("Ableton Live's main window was not found (is Live running?)")
        if len(live) > 1:
            raise SaveError("More than one Ableton Live window found; refusing to guess")
        hwnd, pid = live[0]
        return hwnd, pid, _title(hwnd)

    def _is_foreground(hwnd) -> bool:
        return user32.GetForegroundWindow() == hwnd

    def bring_to_front(hwnd) -> None:
        """Foreground Live without sending any keystroke anywhere."""
        if _is_foreground(hwnd):
            return
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)
        for _ in range(20):
            if _is_foreground(hwnd):
                return
            time.sleep(0.05)
        raise SaveError("Could not bring Ableton Live to the foreground; no keys were sent")

    def _key(vk: int, up: bool) -> "INPUT":
        return INPUT(type=INPUT_KEYBOARD,
                     u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                                                 time=0, dwExtraInfo=0)))

    def send_save_shortcut(hwnd, save_as: bool) -> None:
        """Ctrl+S (or Ctrl+Shift+S), only while Live's window is foreground."""
        if not _is_foreground(hwnd) or not _is_live_exe(_exe(_pid(user32.GetForegroundWindow()))):
            raise SaveError("Live is not the foreground window; no keys were sent")
        mods = [VK_CONTROL] + ([VK_SHIFT] if save_as else [])
        seq = [_key(m, False) for m in mods] + [_key(VK_S, False), _key(VK_S, True)] \
            + [_key(m, True) for m in reversed(mods)]
        arr = (INPUT * len(seq))(*seq)
        if user32.SendInput(len(seq), ctypes.byref(arr), ctypes.sizeof(INPUT)) != len(seq):
            raise SaveError("SendInput was blocked")

    def wait_for_save_dialog(pid: int, timeout: float = 6.0):
        end = time.time() + timeout
        while time.time() < end:
            dialogs = _top_windows(lambda h: _class(h) == DIALOG_CLASS and _pid(h) == pid)
            if len(dialogs) == 1:
                return dialogs[0]
            if len(dialogs) > 1:
                raise SaveError("Several Live dialogs are open; refusing to guess")
            time.sleep(0.1)
        raise SaveError("Live's Save dialog did not appear")

    def fill_and_confirm(dialog, path: str) -> None:
        """Put the path into the file-name box and press Save, no typing."""
        kids = [h for h in _children(dialog) if user32.IsWindowVisible(h)]
        # The file-name box: in the Explorer-style dialog Live uses, an Edit
        # with control id 1001 inside a ComboBox; in the older layout, the
        # Edit inside the ComboBoxEx32 with control id 1148.
        edits = [h for h in kids if _class(h) == "Edit" and user32.GetDlgCtrlID(h) == 1001
                 and _class(user32.GetParent(h)) == "ComboBox"]
        if not edits:
            combo = [h for h in kids if _class(h) == "ComboBoxEx32" and user32.GetDlgCtrlID(h) == 1148]
            edits = [h for h in kids if _class(h) == "Edit" and any(_is_descendant(h, c) for c in combo)]
        save = [h for h in kids if _class(h) == "Button" and user32.GetDlgCtrlID(h) == IDOK]
        if len(edits) != 1 or len(save) != 1:
            raise SaveError("Could not identify the Save dialog's file name box and Save button")
        if _pid(dialog) != _pid(edits[0]):
            raise SaveError("Save dialog controls belong to another process")
        # Keep both buffers referenced for the duration of the calls.
        text = ctypes.create_unicode_buffer(path)
        user32.SendMessageW(edits[0], WM_SETTEXT, 0, ctypes.addressof(text))
        buf = ctypes.create_unicode_buffer(1024)
        user32.SendMessageW(edits[0], WM_GETTEXT, 1024, ctypes.addressof(buf))
        if buf.value != path:
            raise SaveError(f"Could not set the file name in Live's Save dialog (it reads {buf.value!r})")
        user32.SendMessageW(save[0], BM_CLICK, 0, 0)

    def _is_descendant(hwnd, ancestor) -> bool:
        h = user32.GetParent(hwnd)
        while h:
            if h == ancestor:
                return True
            h = user32.GetParent(h)
        return False

    def close_dialog(dialog) -> None:
        WM_CLOSE = 0x0010
        user32.SendMessageW(dialog, WM_CLOSE, 0, 0)


def save_set(folder: Optional[str] = None, filename: Optional[str] = None, timeout: float = 15.0) -> dict:
    """Save the open Set. Without filename: Ctrl+S (the Set must have been
    saved before). With folder+filename: Save As to that new file."""
    if sys.platform != "win32":
        raise SaveError("save_set is only implemented on Windows")
    if (folder is None) != (filename is None):
        raise SaveError("give both folder and filename for Save As, or neither for Save")
    missing: list[Path] = []
    if folder is not None:
        missing = folders_to_create(folder)     # refuses before Live is touched
        target_paths(folder, filename)          # validates the name, too
    hwnd, pid, title = find_live_window()
    name, dirty = parse_title(title)
    if filename is None:
        if name == "Untitled":
            raise SaveError("This Set has never been saved; give folder and filename")
        if not dirty:
            return {"saved": True, "set": name, "note": "no unsaved changes"}
        bring_to_front(hwnd)
        send_save_shortcut(hwnd, save_as=False)
        end = time.time() + timeout
        while time.time() < end:
            if not parse_title(_title(hwnd))[1]:
                return {"saved": True, "set": name}
            time.sleep(0.2)
        raise SaveError("Live did not finish saving (title still shows unsaved changes)")

    typed, candidates = target_paths(folder, filename)
    existing = [p for p in candidates if p.exists()]
    if existing:
        raise SaveError(f"{existing[0]} already exists; choose another name (no overwrite)")
    create_folders(missing)
    try:
        bring_to_front(hwnd)
        send_save_shortcut(hwnd, save_as=(name != "Untitled"))
        dialog = wait_for_save_dialog(pid)
        try:
            fill_and_confirm(dialog, str(typed))
        except SaveError:
            close_dialog(dialog)
            raise
        end = time.time() + timeout
        while time.time() < end:
            hit = next((p for p in candidates if p.exists()), None)
            if hit is not None and not parse_title(_title(hwnd))[1]:
                result = {"saved": True, "set": parse_title(_title(hwnd))[0], "path": str(hit)}
                if missing:
                    result["created_folder"] = str(missing[0])
                return result
            time.sleep(0.2)
        raise SaveError("Save As did not produce the file; check Live for a message")
    except Exception:
        remove_folders(missing)
        raise
