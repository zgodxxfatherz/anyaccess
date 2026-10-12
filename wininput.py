"""
wininput.py — low-level Windows input injection via ctypes SendInput.

Used by host.py to replay the viewer's mouse/keyboard events on the shared
machine. No pywin32 dependency: pure ctypes against user32/kernel32.

All coordinates are in *virtual desktop* pixels (spans every monitor) and
are normalized to the 0..65535 range with MOUSEEVENTF_VIRTUALDESK, so mouse
movement is correct on any monitor layout and DPI setting.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ------------------------------------------------------------------ constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

CF_UNICODE = 13
GMEM_MOVEABLE = 0x0002


# ------------------------------------------------------------------ structures
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = ctypes.c_uint
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetCursorPos.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.CloseClipboard.argtypes = ()
user32.EmptyClipboard.argtypes = ()
user32.SetClipboardData.argtypes = (ctypes.c_uint, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = (ctypes.c_uint,)
user32.GetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL

BUTTON_FLAGS = {
    ("left", True): MOUSEEVENTF_LEFTDOWN,
    ("left", False): MOUSEEVENTF_LEFTUP,
    ("right", True): MOUSEEVENTF_RIGHTDOWN,
    ("right", False): MOUSEEVENTF_RIGHTUP,
    ("middle", True): MOUSEEVENTF_MIDDLEDOWN,
    ("middle", False): MOUSEEVENTF_MIDDLEUP,
}


# ------------------------------------------------------------------ functions
def set_dpi_aware() -> None:
    """Call BEFORE creating any window so pixel coords match the screen."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def desktop_rect():
    """(x, y, w, h) of the virtual desktop in physical pixels."""
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, max(w, 1), max(h, 1)


def _send(*inputs: INPUT) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error())


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    mi = MOUSEINPUT(dx, dy, data, flags, 0, None)
    _send(INPUT(INPUT_MOUSE, _INPUTUNION(mi=mi)))


def move_mouse(x: int, y: int) -> None:
    """Absolute move in virtual-desktop pixels."""
    vx, vy, vw, vh = desktop_rect()
    nx = int((x - vx) * 65535 / (vw - 1)) if vw > 1 else 0
    ny = int((y - vy) * 65535 / (vh - 1)) if vh > 1 else 0
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)


def mouse_button(button: str, down: bool) -> None:
    flag = BUTTON_FLAGS.get((button, down))
    if flag is not None:
        _mouse(flag)


def wheel(delta: int) -> None:
    _mouse(MOUSEEVENTF_WHEEL, data=delta)


def _keyboard(wvk: int = 0, wscan: int = 0, flags: int = 0) -> None:
    ki = KEYBDINPUT(wvk, wscan, flags, 0, None)
    _send(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki)))


def key_vk(vk: int, up: bool = False) -> None:
    """Send a virtual-key event (layout-mapped, composes with modifiers)."""
    _keyboard(wvk=vk, flags=KEYEVENTF_KEYUP if up else 0)


def key_unicode(ch: str, up: bool = False) -> None:
    """Send a literal character (works for any layout, e.g. Persian text)."""
    _keyboard(wscan=ord(ch), flags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0))


def get_cursor_pos():
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return pt.x, pt.y


def get_clipboard_text():
    """Return unicode clipboard text, or None if unavailable/not text."""
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODE)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """Replace the clipboard with unicode text. True on success."""
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(5):  # OpenClipboard can fail while another app holds it
        if user32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODE, handle))
    finally:
        user32.CloseClipboard()
