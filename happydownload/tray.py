"""Lightweight pure-Win32 system tray icon for Happydownload (no external dependencies)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import threading
import webbrowser
from typing import Callable, Optional

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IDI_APPLICATION = 32512
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
TPM_RETURNCMD = 0x0100
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800

CMD_OPEN = 1001
CMD_PAUSE_ALL = 1002
CMD_RESUME_ALL = 1003
CMD_EXIT = 1004


def start_tray_icon(
    ui_url: str,
    on_pause_all: Callable[[], int],
    on_resume_all: Callable[[], int],
    on_exit: Callable[[], None],
    on_open_ui: Optional[Callable[[], None]] = None,
) -> Optional[threading.Thread]:
    """Start a background Windows system tray icon thread if running on Windows."""
    if sys.platform != "win32":
        return None

    t = threading.Thread(
        target=_run_win32_tray,
        args=(ui_url, on_pause_all, on_resume_all, on_exit, on_open_ui),
        name="happydl-tray",
        daemon=True,
    )
    t.start()
    return t


def _run_win32_tray(
    ui_url: str,
    on_pause_all: Callable[[], int],
    on_resume_all: Callable[[], int],
    on_exit: Callable[[], None],
    on_open_ui: Optional[Callable[[], None]] = None,
) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        ]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT
    ]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p
    ]
    user32.TrackPopupMenu.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    def show_menu(hwnd: wintypes.HWND) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "Open Happydownload UI")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_PAUSE_ALL, "Pause All Downloads")
        user32.AppendMenuW(menu, MF_STRING, CMD_RESUME_ALL, "Resume All Downloads")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "Exit Happydownload")

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(
            menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None
        )
        user32.DestroyMenu(menu)
        if cmd == CMD_OPEN:
            if on_open_ui:
                on_open_ui()
            else:
                webbrowser.open(ui_url)
        elif cmd == CMD_PAUSE_ALL:
            on_pause_all()
        elif cmd == CMD_RESUME_ALL:
            on_resume_all()
        elif cmd == CMD_EXIT:
            user32.PostQuitMessage(0)
            on_exit()

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam == WM_LBUTTONDBLCLK:
                if on_open_ui:
                    on_open_ui()
                else:
                    webbrowser.open(ui_url)
            elif lparam == WM_RBUTTONUP:
                show_menu(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wnd_proc)
    hinst = kernel32.GetModuleHandleW(None)
    cls_name = "HappydownloadTrayWindow"

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = cls_name
    user32.RegisterClassExW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(0, cls_name, "Happydownload", 0, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        return

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAYICON
    ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "favicon.ico")
    custom_icon = None
    if os.path.isfile(ico_path):
        # IMAGE_ICON = 1, LR_LOADFROMFILE = 0x0010
        custom_icon = user32.LoadImageW(None, ico_path, 1, 0, 0, 0x0010)
    nid.hIcon = custom_icon or user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
    nid.szTip = "Happydownload v2.0.0 — Adaptive Downloader (by Aswin Lal S Y)"
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
