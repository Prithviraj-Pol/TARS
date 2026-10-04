"""
app_verifier.py — Application verification and idempotency layer for TARS.

Provides:
  - Exact process and window state verification on Windows.
  - Idempotent app launching (detects already-running apps and open windows).
  - Open folder and open file detection (File Explorer, PDF viewers, Office apps).
  - Thread-safe application launch locks.
  - Post-launch verification to ensure actions are never reported as successful
    unless the underlying process or window is verified.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

_OS = platform.system()

# Action Result States
STATE_SUCCESS            = "SUCCESS"
STATE_FAILED             = "FAILED"
STATE_NOT_FOUND          = "NOT_FOUND"
STATE_ALREADY_OPEN       = "APP_ALREADY_OPEN"
STATE_FOLDER_OPEN        = "FOLDER_ALREADY_OPEN"
STATE_FILE_OPEN          = "FILE_ALREADY_OPEN"
STATE_ALREADY_EXISTS     = "ALREADY_EXISTS"
STATE_ALREADY_EXECUTED   = "ALREADY_EXECUTED"
STATE_DUPLICATE_IGNORED  = "DUPLICATE_IGNORED"
STATE_NOT_VERIFIED       = "NOT_VERIFIED"
STATE_CANCELLED          = "CANCELLED"
STATE_LAUNCH_IN_PROGRESS = "LAUNCH_ALREADY_IN_PROGRESS"


# ── Known Application Profiles ──────────────────────────────────────────────
# Maps normalized app keywords to:
#   - canonical_name: Human-friendly name
#   - exes: List of lowercase executable names to inspect in running processes
#   - window_keywords: Substrings expected in top-level window titles
_APP_PROFILES: dict[str, dict[str, Any]] = {
    "chrome": {
        "canonical": "Google Chrome",
        "exes": ["chrome.exe"],
        "window_keywords": ["google chrome", "chrome"],
    },
    "google chrome": {
        "canonical": "Google Chrome",
        "exes": ["chrome.exe"],
        "window_keywords": ["google chrome", "chrome"],
    },
    "vscode": {
        "canonical": "Visual Studio Code",
        "exes": ["code.exe"],
        "window_keywords": ["visual studio code", " - code"],
    },
    "code": {
        "canonical": "Visual Studio Code",
        "exes": ["code.exe"],
        "window_keywords": ["visual studio code", " - code"],
    },
    "visual studio code": {
        "canonical": "Visual Studio Code",
        "exes": ["code.exe"],
        "window_keywords": ["visual studio code", " - code"],
    },
    "android studio": {
        "canonical": "Android Studio",
        "exes": ["studio64.exe", "studio.exe"],
        "window_keywords": ["android studio"],
    },
    "explorer": {
        "canonical": "File Explorer",
        "exes": ["explorer.exe"],
        "window_keywords": ["file explorer", "explorer"],
        "is_shell": True,
    },
    "file explorer": {
        "canonical": "File Explorer",
        "exes": ["explorer.exe"],
        "window_keywords": ["file explorer", "explorer"],
        "is_shell": True,
    },
    "word": {
        "canonical": "Microsoft Word",
        "exes": ["winword.exe"],
        "window_keywords": [" - word", "microsoft word"],
    },
    "winword": {
        "canonical": "Microsoft Word",
        "exes": ["winword.exe"],
        "window_keywords": [" - word", "microsoft word"],
    },
    "excel": {
        "canonical": "Microsoft Excel",
        "exes": ["excel.exe"],
        "window_keywords": [" - excel", "microsoft excel"],
    },
    "powerpoint": {
        "canonical": "Microsoft PowerPoint",
        "exes": ["powerpnt.exe"],
        "window_keywords": [" - powerpoint", "powerpoint"],
    },
    "powerpnt": {
        "canonical": "Microsoft PowerPoint",
        "exes": ["powerpnt.exe"],
        "window_keywords": [" - powerpoint", "powerpoint"],
    },
    "notepad": {
        "canonical": "Notepad",
        "exes": ["notepad.exe"],
        "window_keywords": [" - notepad", "notepad"],
    },
    "terminal": {
        "canonical": "Terminal",
        "exes": ["windowsterminal.exe", "wt.exe"],
        "window_keywords": ["terminal", "windows terminal"],
    },
    "wt": {
        "canonical": "Terminal",
        "exes": ["windowsterminal.exe", "wt.exe"],
        "window_keywords": ["terminal", "windows terminal"],
    },
    "powershell": {
        "canonical": "PowerShell",
        "exes": ["powershell.exe", "pwsh.exe"],
        "window_keywords": ["powershell", "pwsh"],
    },
    "cmd": {
        "canonical": "Command Prompt",
        "exes": ["cmd.exe"],
        "window_keywords": ["command prompt", "cmd.exe"],
    },
    "edge": {
        "canonical": "Microsoft Edge",
        "exes": ["msedge.exe"],
        "window_keywords": ["microsoft edge", " - edge"],
    },
    "msedge": {
        "canonical": "Microsoft Edge",
        "exes": ["msedge.exe"],
        "window_keywords": ["microsoft edge", " - edge"],
    },
    "firefox": {
        "canonical": "Mozilla Firefox",
        "exes": ["firefox.exe"],
        "window_keywords": ["mozilla firefox", "firefox"],
    },
    "spotify": {
        "canonical": "Spotify",
        "exes": ["spotify.exe"],
        "window_keywords": ["spotify"],
    },
    "discord": {
        "canonical": "Discord",
        "exes": ["discord.exe"],
        "window_keywords": ["discord"],
    },
    "slack": {
        "canonical": "Slack",
        "exes": ["slack.exe"],
        "window_keywords": ["slack"],
    },
    "teams": {
        "canonical": "Microsoft Teams",
        "exes": ["ms-teams.exe", "teams.exe"],
        "window_keywords": ["microsoft teams", "teams"],
    },
    "zoom": {
        "canonical": "Zoom",
        "exes": ["zoom.exe"],
        "window_keywords": ["zoom meeting", "zoom"],
    },
    "telegram": {
        "canonical": "Telegram",
        "exes": ["telegram.exe"],
        "window_keywords": ["telegram"],
    },
    "whatsapp": {
        "canonical": "WhatsApp",
        "exes": ["whatsapp.exe"],
        "window_keywords": ["whatsapp"],
    },
    "calculator": {
        "canonical": "Calculator",
        "exes": ["calculatorapp.exe", "calculator.exe", "calc.exe"],
        "window_keywords": ["calculator"],
    },
    "calc": {
        "canonical": "Calculator",
        "exes": ["calculatorapp.exe", "calculator.exe", "calc.exe"],
        "window_keywords": ["calculator"],
    },
    "paint": {
        "canonical": "Paint",
        "exes": ["mspaint.exe"],
        "window_keywords": ["paint", "mspaint"],
    },
    "postman": {
        "canonical": "Postman",
        "exes": ["postman.exe"],
        "window_keywords": ["postman"],
    },
    "blender": {
        "canonical": "Blender",
        "exes": ["blender.exe"],
        "window_keywords": ["blender"],
    },
    "figma": {
        "canonical": "Figma",
        "exes": ["figma.exe"],
        "window_keywords": ["figma"],
    },
    "vlc": {
        "canonical": "VLC Media Player",
        "exes": ["vlc.exe"],
        "window_keywords": ["vlc media player"],
    },
    "obsidian": {
        "canonical": "Obsidian",
        "exes": ["obsidian.exe"],
        "window_keywords": ["obsidian"],
    },
    "notion": {
        "canonical": "Notion",
        "exes": ["notion.exe"],
        "window_keywords": ["notion"],
    },
    "capcut": {
        "canonical": "CapCut",
        "exes": ["capcut.exe"],
        "window_keywords": ["capcut"],
    },
    "steam": {
        "canonical": "Steam",
        "exes": ["steam.exe"],
        "window_keywords": ["steam"],
    },
    "epic": {
        "canonical": "Epic Games",
        "exes": ["epicgameslauncher.exe"],
        "window_keywords": ["epic games"],
    },
    "epic games": {
        "canonical": "Epic Games",
        "exes": ["epicgameslauncher.exe"],
        "window_keywords": ["epic games"],
    },
    "task manager": {
        "canonical": "Task Manager",
        "exes": ["taskmgr.exe"],
        "window_keywords": ["task manager"],
    },
    "pdf": {
        "canonical": "PDF Reader",
        "exes": ["acrord32.exe", "acrobat.exe", "sumatrapdf.exe", "foxitreader.exe"],
        "window_keywords": [".pdf", "adobe acrobat", "acrobat reader", "sumatrapdf"],
    },
}


def normalize_app(raw_name: str) -> tuple[str, list[str], list[str], bool]:
    """Returns (canonical_name, target_exes, window_keywords, is_shell)."""
    clean = raw_name.lower().strip()
    if clean.endswith(".exe"):
        clean = clean[:-4]

    if clean in _APP_PROFILES:
        p = _APP_PROFILES[clean]
        return p["canonical"], p["exes"], p["window_keywords"], p.get("is_shell", False)

    for k, p in _APP_PROFILES.items():
        if k in clean or clean in k:
            return p["canonical"], p["exes"], p["window_keywords"], p.get("is_shell", False)

    # Unknown generic app
    default_exe = f"{clean}.exe"
    return raw_name.strip().title(), [default_exe], [clean], False


# ── Process & Window Inspection ──────────────────────────────────────────────

def get_running_process(target_exes: list[str]) -> tuple[bool, str, int]:
    """Check if any of the target executables are running via psutil.
    Returns (found, process_name, pid).
    """
    try:
        import psutil
    except ImportError:
        return False, "", 0

    lower_targets = {e.lower() for e in target_exes}
    try:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                pname = (p.info.get("name") or "").lower()
                if pname in lower_targets:
                    return True, p.info.get("name") or pname, p.info.get("pid", 0)
            except Exception:
                continue
    except Exception:
        pass
    return False, "", 0


def get_visible_windows() -> list[tuple[int, str]]:
    """Enumerate visible titled windows safely without raising pywintypes error 122."""
    if _OS != "Windows":
        return []

    windows: list[tuple[int, str]] = []
    try:
        import win32gui

        def _enum_cb(hwnd: int, _):
            try:
                if win32gui.IsWindowVisible(hwnd):
                    text = win32gui.GetWindowText(hwnd)
                    if text and text.strip():
                        windows.append((hwnd, text.strip()))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_enum_cb, None)
    except Exception:
        pass
    return windows


def find_app_window(window_keywords: list[str]) -> tuple[bool, str, int]:
    """Search for a top-level window matching any keyword.
    Returns (found, window_title, hwnd).
    """
    lower_kw = [k.lower() for k in window_keywords]
    for hwnd, title in get_visible_windows():
        t_lower = title.lower()
        if any(kw in t_lower for kw in lower_kw):
            return True, title, hwnd
    return False, "", 0


def activate_window(hwnd: int) -> bool:
    """Bring an already open window to foreground."""
    if _OS != "Windows" or not hwnd:
        return False
    try:
        import win32gui
        import win32con
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def get_open_explorer_folders() -> list[str]:
    """Query open File Explorer windows via Shell.Application COM interface."""
    if _OS != "Windows":
        return []

    folders = []
    try:
        import win32com.client
        shell = win32com.client.Dispatch("Shell.Application")
        shell_windows = shell.Windows()
        for i in range(shell_windows.Count):
            try:
                w = shell_windows.Item(i)
                loc_url = getattr(w, "LocationURL", "") or ""
                loc_name = getattr(w, "LocationName", "") or ""
                if loc_url:
                    # Convert file:///C:/path/to/folder -> C:\path\to\folder
                    path_str = loc_url.replace("file:///", "").replace("/", "\\")
                    folders.append(path_str.lower())
                if loc_name:
                    folders.append(loc_name.lower())
            except Exception:
                continue
    except Exception:
        pass
    return folders


def is_folder_open(target_path: Path | str) -> tuple[bool, str]:
    """Check if a folder is already open in File Explorer or as a window."""
    try:
        p = Path(target_path).resolve()
        p_name = p.name.lower()
        p_str = str(p).lower()

        # 1. Check Shell.Application windows
        open_folders = get_open_explorer_folders()
        for f in open_folders:
            if p_str in f or f in p_str or p_name == f:
                return True, f"File Explorer window for '{p.name}' is already open."

        # 2. Check window titles
        for _, title in get_visible_windows():
            t_lower = title.lower()
            if p_name and p_name in t_lower:
                return True, f"Window '{title}' displaying folder is already open."
    except Exception:
        pass
    return False, ""


def is_file_open(target_path: Path | str) -> tuple[bool, str]:
    """Check if a file (PDF, docx, pptx, etc.) is already open in any window."""
    try:
        p = Path(target_path).resolve()
        name_lower = p.name.lower()
        stem_lower = p.stem.lower()

        for _, title in get_visible_windows():
            t_lower = title.lower()
            if name_lower in t_lower or (len(stem_lower) > 3 and stem_lower in t_lower):
                return True, f"Window '{title}' displaying '{p.name}' is already open."
    except Exception:
        pass
    return False, ""


def is_app_already_open(app_name: str) -> tuple[bool, str, dict]:
    """High-level check whether an application is already open and running.
    Checks BOTH:
      1. Window existence
      2. Process existence (excluding system shell explorer)
    Returns (already_open, reason, metadata).
    """
    canonical, exes, window_keywords, is_shell = normalize_app(app_name)

    # 1. Window check
    w_found, w_title, hwnd = find_app_window(window_keywords)
    if w_found:
        activate_window(hwnd)
        return True, f"Window '{w_title}' detected", {
            "canonical": canonical,
            "type": "window",
            "title": w_title,
            "hwnd": hwnd,
        }

    # 2. Special handling for File Explorer: explorer.exe is the Windows shell
    if is_shell:
        open_folders = get_open_explorer_folders()
        if open_folders:
            return True, "File Explorer window detected", {
                "canonical": canonical,
                "type": "folder_window",
            }
        return False, "", {}

    # 3. Process check
    p_found, pname, pid = get_running_process(exes)
    if p_found:
        return True, f"Process '{pname}' detected (PID {pid})", {
            "canonical": canonical,
            "type": "process",
            "name": pname,
            "pid": pid,
        }

    return False, "", {}


# ── Thread-Safe Launch Lock ──────────────────────────────────────────────────

_LAUNCH_LOCK = threading.RLock()
_ACTIVE_LAUNCHES: set[str] = set()


class AppLaunchGuard:
    """Context manager for thread-safe application launches."""

    def __init__(self, canonical_name: str):
        self.canonical_name = canonical_name.lower().strip()
        self.acquired = False

    def __enter__(self):
        with _LAUNCH_LOCK:
            if self.canonical_name in _ACTIVE_LAUNCHES:
                raise RuntimeError(
                    f"{STATE_LAUNCH_IN_PROGRESS}: Launch already in progress for '{self.canonical_name}'"
                )
            _ACTIVE_LAUNCHES.add(self.canonical_name)
            self.acquired = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            with _LAUNCH_LOCK:
                _ACTIVE_LAUNCHES.discard(self.canonical_name)


def is_launch_in_progress(app_name: str) -> bool:
    canonical, _, _, _ = normalize_app(app_name)
    with _LAUNCH_LOCK:
        return canonical.lower().strip() in _ACTIVE_LAUNCHES


# ── Post-Launch Verification ─────────────────────────────────────────────────

def verify_app_launched(
    app_name: str,
    timeout_seconds: float = 3.5,
    poll_interval: float = 0.3,
) -> tuple[bool, str]:
    """Poll for the application to appear in running processes or open windows.
    Returns (verified, details_str).
    """
    canonical, exes, window_keywords, is_shell = normalize_app(app_name)
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        # Check window
        w_found, w_title, _ = find_app_window(window_keywords)
        if w_found:
            return True, f"window '{w_title}' verified"

        # Check process (unless shell explorer)
        if not is_shell:
            p_found, pname, pid = get_running_process(exes)
            if p_found:
                return True, f"process '{pname}' verified (PID {pid})"
        else:
            if get_open_explorer_folders():
                return True, "File Explorer window verified"

        time.sleep(poll_interval)

    return False, f"process/window for '{canonical}' not detected within {timeout_seconds:.1f}s"
