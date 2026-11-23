"""Windows power management, clipboard link detection, notifications, and schedule helpers."""

from __future__ import annotations

import ctypes
from datetime import datetime
import re
import subprocess
import sys
import threading
from typing import Optional
from urllib.parse import urlparse

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

CF_UNICODETEXT = 13

_MEDIA_OR_ARCHIVE_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".iso",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bin", ".exe", ".msi", ".dmg",
}
_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)

_sleep_lock = threading.Lock()
_sleep_prevented = False


def set_prevent_sleep(active: bool) -> bool:
    """Prevent or allow Windows idle sleep while massive downloads are active."""
    global _sleep_prevented
    if sys.platform != "win32":
        return False
    with _sleep_lock:
        if active == _sleep_prevented:
            return True
        try:
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if active else ES_CONTINUOUS
            res = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
            if res:
                _sleep_prevented = active
                return True
        except Exception:
            pass
        return False


def execute_power_action(action: str) -> bool:
    """Trigger post-completion power action ('sleep' or 'shutdown') on Windows."""
    if sys.platform != "win32":
        return False
    act = (action or "none").lower().strip()
    try:
        if act == "sleep":
            subprocess.Popen(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        if act == "shutdown":
            # 30-second grace window so user can run `shutdown /a` if needed
            subprocess.Popen(
                ["shutdown", "/s", "/t", "30", "/c", "Happydownload completed all downloads."],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
    except Exception:
        return False
    return False


def read_clipboard_text() -> Optional[str]:
    """Read current Unicode text from the Windows clipboard without spawning subprocesses."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_int
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int

    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception:
        return None
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


def extract_download_url_candidate(text: Optional[str]) -> Optional[str]:
    """Return URL if clipboard text looks like a public HTTP/HTTPS video page or file link."""
    if not text:
        return None
    candidate = text.strip()
    if len(candidate) > 2048 or "\n" in candidate or not _URL_RE.match(candidate):
        return None
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if not host or host in ("127.0.0.1", "localhost", "::1"):
        return None
    return candidate


def show_desktop_notification(title: str, message: str) -> None:
    """Display a non-blocking Windows balloon notification when a download completes."""
    if sys.platform != "win32":
        return
    safe_title = title.replace("'", "''")[:80]
    safe_msg = message.replace("'", "''")[:200]
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(5000, '{safe_title}', '{safe_msg}', [System.Windows.Forms.ToolTipIcon]::Info); "
        "Start-Sleep -Seconds 6; $n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _parse_hhmm(val: str) -> int:
    try:
        parts = val.strip().split(":")
        h = max(0, min(23, int(parts[0])))
        m = max(0, min(59, int(parts[1]))) if len(parts) > 1 else 0
        return h * 60 + m
    except Exception:
        return 0


def is_in_full_speed_window(
    start_hhmm: str,
    end_hhmm: str,
    now_dt: Optional[datetime] = None,
) -> bool:
    """Check whether current local time falls inside `[start, end)` (supports overnight windows)."""
    dt = now_dt or datetime.now()
    cur = dt.hour * 60 + dt.minute
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    # Overnight window e.g. 23:00 -> 07:00
    return cur >= start or cur < end

