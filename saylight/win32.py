"""Win32 helpers for Saylight: ctypes wrappers with exact signatures (64-bit safe). No Qt here.

Every function may be called from any thread unless its docstring says otherwise. The registry readers
are read-only; nothing in this module writes to the registry. This module uses its own WinDLL instances,
so its argtypes never clash with other modules.
"""

import ctypes
import logging
import math
import os
import re
import time
import winreg
from collections.abc import Iterable, Sequence
from ctypes import wintypes

log = logging.getLogger("saylight.win32")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shell32 = ctypes.WinDLL("shell32")

HRESULT = ctypes.c_long
ULONG_PTR = ctypes.c_size_t

# --- Constants -------------------------------------------------------------

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
VK_C = 0x43
VK_F1, VK_F24 = 0x70, 0x87
VK_OEM_3 = 0xC0  # the ` key on US/Canadian English keyboards
VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB2, 0xB3

WM_HOTKEY = 0x0312
WM_APP = 0x8000
PM_NOREMOVE, PM_REMOVE = 0x0000, 0x0001
QS_SENDMESSAGE, QS_ALLINPUT = 0x0040, 0x04FF
PM_QS_SENDMESSAGE = QS_SENDMESSAGE << 16
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED = 0x00000000, 0x00000102, 0xFFFFFFFF
MWMO_INPUTAVAILABLE = 0x0004  # wake for queued input even if a PeekMessage call has already seen it

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0

SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
HWND_TOPMOST = -1

DWMWA_TRANSITIONS_FORCEDISABLED = 3
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND, DWMWCP_ROUNDSMALL = 2, 3
DWMWA_COLOR_NONE, DWMWA_COLOR_DEFAULT = 0xFFFFFFFE, 0xFFFFFFFF
BORDER_NONE: tuple[()] = ()  # pass to dwm_set_border_color() for no border at all

SPI_GETHIGHCONTRAST = 0x0042
SPI_GETCLIENTAREAANIMATION = 0x1042
SPI_GETCARETWIDTH = 0x2006
HCF_HIGHCONTRASTON = 0x00000001
SM_CXSMICON = 49

COLOR_WINDOW, COLOR_WINDOWTEXT, COLOR_HIGHLIGHT, COLOR_HIGHLIGHTTEXT, COLOR_GRAYTEXT = 5, 8, 13, 14, 17

MB_OK, MB_ICONHAND = 0x00000000, 0x00000010

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002

# The taskbar, tray overflow, desktop and Start/Search, plus three transient explorer.exe surfaces
# (task switching, Alt+Tab / Task View).
SHELL_CLASSES = frozenset({
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
    "Progman", "WorkerW", "Windows.UI.Core.CoreWindow",
    "ForegroundStaging", "XamlExplorerHostIslandWindow", "MultitaskingViewFrame",
})
TERMINAL_CLASSES = frozenset({"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "mintty"})

_OWN_PID = os.getpid()


# --- Structures and prototypes ----------------------------------------------

class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD))


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class HIGHCONTRASTW(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD), ("lpszDefaultScheme", wintypes.LPWSTR))


WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                  wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


def _sig(func, restype, *argtypes):
    func.restype = restype
    func.argtypes = argtypes


_sig(user32.GetForegroundWindow, wintypes.HWND)
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.IsWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.GetClassNameW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, wintypes.LPDWORD)
_sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, wintypes.UINT)
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_sig(user32.SystemParametersInfoW, wintypes.BOOL, wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT)
_sig(user32.GetSysColor, wintypes.DWORD, ctypes.c_int)
_sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_sig(user32.MessageBeep, wintypes.BOOL, wintypes.UINT)
_sig(user32.VkKeyScanW, ctypes.c_short, wintypes.WCHAR)
_sig(user32.MapVirtualKeyW, wintypes.UINT, wintypes.UINT, wintypes.UINT)
_sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_sig(user32.SendInput, wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_sig(user32.RegisterHotKey, wintypes.BOOL, wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_sig(user32.UnregisterHotKey, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_sig(user32.PostThreadMessageW, wintypes.BOOL, wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.MsgWaitForMultipleObjects, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
     wintypes.BOOL, wintypes.DWORD, wintypes.DWORD)
_sig(user32.MsgWaitForMultipleObjectsEx, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
     wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
_sig(user32.PeekMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
     wintypes.UINT)
_sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(user32.DispatchMessageW, ctypes.c_ssize_t, ctypes.POINTER(wintypes.MSG))
_sig(user32.SetWinEventHook, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
     wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
_sig(user32.UnhookWinEvent, wintypes.BOOL, wintypes.HANDLE)
_sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE,
     wintypes.LPVOID)
_sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.OpenClipboard, wintypes.BOOL, wintypes.HWND)
_sig(user32.CloseClipboard, wintypes.BOOL)
_sig(user32.EmptyClipboard, wintypes.BOOL)
_sig(user32.IsClipboardFormatAvailable, wintypes.BOOL, wintypes.UINT)
_sig(user32.GetClipboardData, wintypes.HANDLE, wintypes.UINT)
_sig(user32.SetClipboardData, wintypes.HANDLE, wintypes.UINT, wintypes.HANDLE)
_sig(user32.GetClipboardSequenceNumber, wintypes.DWORD)
_sig(user32.CountClipboardFormats, ctypes.c_int)
_sig(user32.RegisterClipboardFormatW, wintypes.UINT, wintypes.LPCWSTR)
_sig(kernel32.GetCurrentThreadId, wintypes.DWORD)
_sig(kernel32.GlobalAlloc, wintypes.HGLOBAL, wintypes.UINT, ctypes.c_size_t)
_sig(kernel32.GlobalLock, wintypes.LPVOID, wintypes.HGLOBAL)
_sig(kernel32.GlobalUnlock, wintypes.BOOL, wintypes.HGLOBAL)
_sig(kernel32.GlobalFree, wintypes.HGLOBAL, wintypes.HGLOBAL)
_sig(kernel32.GlobalSize, ctypes.c_size_t, wintypes.HGLOBAL)
_sig(kernel32.CreateEventW, wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_sig(kernel32.SetEvent, wintypes.BOOL, wintypes.HANDLE)
_sig(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_sig(dwmapi.DwmSetWindowAttribute, HRESULT, wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD)
_sig(shell32.SHQueryUserNotificationState, HRESULT, ctypes.POINTER(ctypes.c_int))


def last_error() -> int:
    """The calling thread's last Win32 error from a call made through this module."""
    return ctypes.get_last_error()


# --- Windows -----------------------------------------------------------------

def get_foreground_window() -> int:
    """The foreground window, or 0 (for example while activation changes or on the secure desktop)."""
    return user32.GetForegroundWindow() or 0


def get_class_name(hwnd: int) -> str:
    """The window's class name, or "" if hwnd is not a window."""
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(257)  # class names are at most 256 characters
    n = user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value if n > 0 else ""


def get_window_pid(hwnd: int) -> int:
    """The id of the process that owns the window, or 0."""
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def set_foreground_window(hwnd: int) -> bool:
    """Ask Windows to bring hwnd to the foreground; Windows may refuse (False)."""
    return bool(hwnd) and bool(user32.SetForegroundWindow(hwnd))


def is_shell_window(hwnd: int) -> bool:
    """True for the taskbar, tray overflow, desktop, Start/Search and task-switching surfaces."""
    return get_class_name(hwnd) in SHELL_CLASSES


def is_terminal_window(hwnd: int) -> bool:
    """True for Windows Terminal, the classic console and mintty, where Ctrl+C means "interrupt"."""
    return get_class_name(hwnd) in TERMINAL_CLASSES


def is_app_window(hwnd: int) -> bool:
    """True for a live window of another process that is not part of the shell."""
    return is_window(hwnd) and not is_shell_window(hwnd) and get_window_pid(hwnd) != _OWN_PID


def is_own_window(hwnd: int) -> bool:
    """True for a live window of this process: the panel, the Type or paste box, a menu or a tooltip."""
    return is_window(hwnd) and get_window_pid(hwnd) == _OWN_PID


# --- DWM and window placement -------------------------------------------------

def _dwm_set(hwnd: int, attribute: int, value: int, ctype=wintypes.DWORD) -> bool:
    v = ctype(value)
    hr = dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))
    if hr != 0:
        log.debug("DwmSetWindowAttribute(%d) failed: 0x%08X", attribute, hr & 0xFFFFFFFF)
    return hr == 0


def dwm_set_corner(hwnd: int, small: bool = False) -> bool:
    """Rounded corners (8 px, or the small radius), which also gives the native border and shadow."""
    return _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUNDSMALL if small else DWMWCP_ROUND, ctypes.c_int)


def dwm_set_dark(hwnd: int, dark: bool) -> bool:
    return _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0, wintypes.BOOL)


def dwm_disable_transitions(hwnd: int) -> bool:
    return _dwm_set(hwnd, DWMWA_TRANSITIONS_FORCEDISABLED, 1, wintypes.BOOL)


def dwm_set_border_color(hwnd: int, rgb: tuple | None) -> bool:
    """Border colour as (r, g, b); None is the system default; BORDER_NONE removes the border."""
    if rgb is None:
        colorref = DWMWA_COLOR_DEFAULT
    elif rgb == BORDER_NONE:
        colorref = DWMWA_COLOR_NONE
    else:
        r, g, b = (int(c) & 0xFF for c in rgb)
        colorref = r | (g << 8) | (b << 16)
    return _dwm_set(hwnd, DWMWA_BORDER_COLOR, colorref)


def set_topmost(hwnd: int) -> None:
    """Put the window in the topmost band without activating, moving or resizing it."""
    if not user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE):
        log.debug("SetWindowPos(TOPMOST) failed (error %d)", last_error())


def move_window(hwnd: int, x: int, y: int) -> None:
    """Move the window's top-left corner to (x, y) in physical pixels, without activating it."""
    if not user32.SetWindowPos(hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE):
        log.debug("SetWindowPos(move) failed (error %d)", last_error())


def get_cursor_pos() -> tuple[int, int]:
    """The cursor position in physical pixels. Raises OSError on failure (e.g. on the secure desktop)."""
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(last_error())
    return pt.x, pt.y


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) in physical pixels. Raises OSError if hwnd is not a window."""
    rc = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        raise ctypes.WinError(last_error())
    return rc.left, rc.top, rc.right, rc.bottom


# --- System state ----------------------------------------------------------

def query_user_notification_state() -> int:
    """SHQueryUserNotificationState: 2 busy, 3 D3D full screen, 4 presentation, 5 accepts notifications; 0 if it
    fails."""
    state = ctypes.c_int(0)
    return state.value if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 else 0


def animations_enabled() -> bool:
    """SPI_GETCLIENTAREAANIMATION ("Animation effects"); True if it can't be read."""
    value = wintypes.BOOL(1)
    if not user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0):
        return True
    return bool(value.value)


def caret_width_px() -> int:
    """The text cursor thickness from Settings > Accessibility > Text cursor (SPI_GETCARETWIDTH), in pixels;
    1 if it can't be read."""
    value = wintypes.DWORD(1)
    if not user32.SystemParametersInfoW(SPI_GETCARETWIDTH, 0, ctypes.byref(value), 0):
        return 1
    return max(1, int(value.value))


def high_contrast() -> bool:
    hc = HIGHCONTRASTW(cbSize=ctypes.sizeof(HIGHCONTRASTW))
    if not user32.SystemParametersInfoW(SPI_GETHIGHCONTRAST, ctypes.sizeof(hc), ctypes.byref(hc), 0):
        return False
    return bool(hc.dwFlags & HCF_HIGHCONTRASTON)


def sys_color_rgb(index: int) -> tuple[int, int, int]:
    c = user32.GetSysColor(index)
    return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF


def small_icon_size() -> int:
    """SM_CXSMICON for this process's DPI awareness (20 here once Qt has made the process PerMonitorV2)."""
    return user32.GetSystemMetrics(SM_CXSMICON)


def message_beep(kind: str) -> None:
    """The system "ok" (MB_OK) or "error" (MB_ICONHAND) sound."""
    beeps = {"ok": MB_OK, "error": MB_ICONHAND}
    if kind not in beeps:
        raise ValueError(f"unknown beep kind {kind!r}")
    user32.MessageBeep(beeps[kind])


# --- Registry readers (read-only) -----------------------------------------

_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_ACCENT = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"
_ACCESSIBILITY = r"Software\Microsoft\Accessibility"


def _reg_value(path: str, name: str):
    """A value under HKCU, or None. Opens the key with KEY_READ only."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def smart_app_control_on() -> bool:
    """Smart App Control is enforcing (not just evaluating), so it may block an unsigned program it hasn't seen
    before, with no way for the person to allow it."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy", 0,
                            winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")[0] == 1
    except OSError:
        return False


def _reg_flag(path: str, name: str, default: bool) -> bool:
    value = _reg_value(path, name)
    return bool(value) if isinstance(value, int) else default


def text_scale_factor() -> float:
    """Settings > Accessibility > Text size, as 1.0–2.25; 1.0 if unset or unreadable."""
    value = _reg_value(_ACCESSIBILITY, "TextScaleFactor")
    if not isinstance(value, int):
        return 1.0
    return min(2.25, max(1.0, value / 100))


def apps_use_light_theme() -> bool:
    return _reg_flag(_PERSONALIZE, "AppsUseLightTheme", True)


def system_uses_light_theme() -> bool:
    return _reg_flag(_PERSONALIZE, "SystemUsesLightTheme", True)


def color_prevalence() -> bool:
    """True when the taskbar and Start are accent-coloured."""
    return _reg_flag(_PERSONALIZE, "ColorPrevalence", False)


def accent_palette() -> list[tuple[int, int, int]] | None:
    """The 8 AccentPalette entries as (r, g, b): Light3, Light2, Light1, Base, Dark1, Dark2, Dark3, unused."""
    value = _reg_value(_ACCENT, "AccentPalette")
    if not isinstance(value, bytes) or len(value) < 32:
        return None
    return [(value[i], value[i + 1], value[i + 2]) for i in range(0, 32, 4)]


# --- Hotkeys and keyboard -------------------------------------------------------

_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
              "win": MOD_WIN, "windows": MOD_WIN}
_NAMED_KEYS = {"space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "insert": 0x2D,
               "delete": 0x2E, "pause": 0x13, "scrolllock": 0x91}
_BARE_NAMED_KEYS = frozenset({"pause", "scrolllock"})  # safe to use without modifiers, like F-keys
_FKEY = re.compile(r"f([1-9]|1[0-9]|2[0-4])")


def _resolve_key(key: str) -> tuple[int, int, bool] | None:
    """(vk, implied modifiers, usable without modifiers) for one key name, or None."""
    low = key.lower()
    m = _FKEY.fullmatch(low)
    if m:
        return VK_F1 + int(m.group(1)) - 1, 0, True
    if low in _NAMED_KEYS:
        return _NAMED_KEYS[low], 0, low in _BARE_NAMED_KEYS
    if len(key) != 1 or ord(key) > 0xFFFF:
        return None
    result = user32.VkKeyScanW(key.lower() if key.isalpha() else key)  # Ctrl+A means the A key, not Shift+A
    shift_state = (result >> 8) & 0xFF
    if key == "`" and (result == -1 or shift_state):  # missing or shifted on this layout: use VK_OEM_3
        return VK_OEM_3, 0, False
    if result == -1 or shift_state & ~1:  # not on this layout, or needs Ctrl/AltGr
        return None
    return result & 0xFF, MOD_SHIFT if shift_state & 1 else 0, False


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """Parse "Ctrl+Alt+`"-style specs into (MOD_* flags, vk); MOD_NOREPEAT is not included.

    Modifiers: Ctrl, Alt, Shift, Win (any case). Key: one character (resolved with VkKeyScanW on this
    thread's layout), F1–F24, or Space/PageUp/PageDown/End/Home/Insert/Delete/Pause/ScrollLock.
    A character key needs at least one modifier. "" or anything unparseable returns None.
    """
    if not isinstance(spec, str) or not spec.strip():
        return None
    head, _, key = spec.strip().rpartition("+")
    if not key and head.endswith("+"):  # "Ctrl++": the key is "+"
        head, key = head[:-1], "+"
    key = key.strip()
    if not key:
        return None
    mods = 0
    for part in head.split("+") if head.strip() else ():
        bit = _MODIFIERS.get(part.strip().lower())
        if bit is None:
            return None
        mods |= bit
    resolved = _resolve_key(key)
    if resolved is None:
        return None
    vk, implied, bare_ok = resolved
    if not mods and not bare_ok:
        return None
    return mods | implied, vk


def register_hotkey(hk_id: int, modifiers: int, vk: int) -> bool:
    """RegisterHotKey(NULL, …): WM_HOTKEY goes to the calling thread's queue; unregister on the same thread."""
    return bool(user32.RegisterHotKey(None, hk_id, modifiers, vk))


def unregister_hotkey(hk_id: int) -> bool:
    return bool(user32.UnregisterHotKey(None, hk_id))


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _send_batch(events: Sequence[tuple[int, bool]]) -> int:
    batch = (INPUT * len(events))()
    for item, (vk, down) in zip(batch, events):
        item.type = INPUT_KEYBOARD
        item.u.ki.wVk = vk
        item.u.ki.wScan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) & 0xFFFF
        item.u.ki.dwFlags = 0 if down else KEYEVENTF_KEYUP
    return user32.SendInput(len(batch), batch, ctypes.sizeof(INPUT))


def send_key_events(events: Iterable[tuple[int, bool]]) -> bool:
    """Inject (vk, is_down) key events as one SendInput batch that other input can't interleave with.

    If Windows inserts only part of the batch, the keys it pressed are released again, so a modifier
    such as Ctrl can never be left stuck down.
    """
    events = list(events)
    sent = _send_batch(events)
    if sent != len(events):
        log.warning("SendInput inserted %d of %d key events (error %d)", sent, len(events), last_error())
        state: dict[int, bool] = {}
        for vk, down in events[:sent]:
            state[vk] = down
        stuck = [vk for vk, down in reversed(state.items()) if down]
        if stuck:
            _send_batch([(vk, False) for vk in stuck])
    return sent == len(events)


def send_keys(vks: Sequence[int]) -> bool:
    """Press vks in order, then release them in reverse order (e.g. Ctrl, C)."""
    return send_key_events([(vk, True) for vk in vks] + [(vk, False) for vk in reversed(vks)])


# --- Threads, messages and events ---------------------------------------------

def get_current_thread_id() -> int:
    return kernel32.GetCurrentThreadId()


def post_thread_message(thread_id: int, message: int, wparam: int = 0, lparam: int = 0) -> bool:
    return bool(thread_id) and bool(user32.PostThreadMessageW(thread_id, message, wparam, lparam))


def wait_pumping_sent(seconds: float) -> None:
    """Sleep up to `seconds` while dispatching messages *sent* to this thread's windows.

    A clipboard owner must keep answering sent messages such as WM_DESTROYCLIPBOARD: while its thread
    doesn't, other apps' EmptyClipboard (their Ctrl+C) blocks for up to 5 s. Posted messages (hotkeys,
    commands) stay queued for the caller's own loop.
    """
    msg = wintypes.MSG()
    end = time.monotonic() + max(0.0, seconds)
    while True:
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE | PM_QS_SENDMESSAGE)
        left = end - time.monotonic()
        if left <= 0:
            return
        user32.MsgWaitForMultipleObjectsEx(0, None, max(1, math.ceil(left * 1000)), QS_SENDMESSAGE, 0)


def set_win_event_hook(event_min: int, event_max: int, proc, flags: int) -> int:
    """SetWinEventHook for all processes and threads; keep a reference to `proc` until unhooked."""
    return user32.SetWinEventHook(event_min, event_max, None, proc, 0, 0, flags) or 0


def unhook_win_event(hook: int) -> bool:
    return bool(hook) and bool(user32.UnhookWinEvent(hook))


def create_event(*, manual_reset: bool = True, name: str | None = None) -> int:
    """CreateEventW (initially unsignalled); 0 on failure."""
    return kernel32.CreateEventW(None, manual_reset, False, name) or 0


def set_event(handle: int) -> bool:
    return bool(kernel32.SetEvent(handle))


def close_handle(handle: int) -> bool:
    return bool(handle) and bool(kernel32.CloseHandle(handle))
