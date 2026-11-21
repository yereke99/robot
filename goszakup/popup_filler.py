"""Auto-fill the TumarCSP price popup (Windows only).

Gamma tenders seal the price inside TumarCSP: calling EncryptOfferPrice opens a
native Windows dialog ("Заполнение ценового предложения") with a price field and
an OK button. There's no API to pass the price — it must be entered in that
window. So for hands-free bidding we drive the window: wait for it, type the
minimum (-20% / dumping floor), click OK. TumarCSP then returns the sealed price
over its WebSocket exactly as if a human had typed it.

Robust multi-stage architecture:
  1. Win32 control automation (pure ctypes, no pywinauto dependency).
     Finds the window by title ("Заполнение ценового предложения" etc.),
     by structure (Edit + OK button), or by process (CryptoSocket.exe/TumarCSP.exe).
     Injects the price via WM_SETTEXT, verifies with WM_GETTEXT, and clicks OK.

  2. Direct control UI automation (fallback for custom renderers / UIPI).
     Restores/focuses the window, clicks into the center of the Edit control,
     selects all (Ctrl+A), pastes/types the price, and clicks OK / presses Enter.

  3. Screen coordinate fallback (from config tumarcsp.popup.*).
     Clicks field_xy, types price, clicks ok_xy.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import time
from typing import Optional, Sequence

import structlog

log = structlog.get_logger("popup")

# Matches "Заполнение ценового предложения" (+ kz/en/case variants) loosely.
WINDOW_TITLE_NEEDLES = ("ценов", "предлож", "заполн", "price", "баға", "offer")
# Windows we must never mistake for the popup.
NOISE_CLASS = {
    "XamlExplorerHostIslandWindow", "Windows.UI.Core.CoreWindow",
    "Windows.UI.Input.InputSite.WindowClass", "tooltips_class32",
    "IME", "MSCTFIME UI", "Shell_TrayWnd",
}
OK_LABELS = ("OK", "ОК", "Ok", "ok", "Да", "Подтвердить", "Применить", "Принять")

# Coordinates measured for the operator's machine
DEFAULT_FIELD_XY = (1016, 492)
DEFAULT_OK_XY = (1012, 622)
DEFAULT_DETECT_XY = (800, 415)
DEFAULT_DETECT_RGB = (240, 240, 240)

try:
    _PRE_TYPE_SETTLE_S = float(os.environ.get("ATAGOS_POPUP_SETTLE_S", "0.05"))
except ValueError:
    _PRE_TYPE_SETTLE_S = 0.05


def is_supported() -> bool:
    """True on Windows — uses ctypes, win32gui, and pyautogui (no MFC/pywinauto required)."""
    return sys.platform == "win32"


def is_elevated() -> bool:
    """True if this process has admin rights (runs at High integrity)."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _stopped(stop) -> bool:
    """True once the caller signalled that the popup no longer needs filling."""
    return stop is not None and stop.is_set()


def _fmt_price(price: float) -> str:
    """Money string the field expects: 2 decimals, dot separator (e.g. 1777045.72)."""
    return f"{price:.2f}"


def _norm_digits(s: str) -> str:
    return str(s or "").replace(" ", "").replace("\xa0", "").replace(",", ".")


# --------------------------------------------------------------------------- #
# low-level helpers (Windows)                                                 #
# --------------------------------------------------------------------------- #
def _clipboard_text() -> str:
    try:
        import win32clipboard
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                break
            except Exception:
                time.sleep(0.02)
        else:
            return ""
        try:
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return str(data or "")
    except Exception:
        return ""


def _set_clipboard(text: str) -> bool:
    """Put unicode text on clipboard for Ctrl+V (more reliable than typewrite)."""
    try:
        import win32clipboard
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                break
            except Exception:
                time.sleep(0.02)
        else:
            return False
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        log.warning("popup.clipboard.set_failed", err=str(e)[:80])
        return False


def _cryptosocket_pids() -> set[int]:
    """PIDs of CryptoSocket / TumarCSP processes (owners of the price dialog)."""
    pids: set[int] = set()
    try:
        import win32process, win32api, win32con
        for pid in win32process.EnumProcesses():
            if pid <= 4:
                continue
            try:
                h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                try:
                    name = win32process.GetProcessImageFileName(h).lower()
                    if "cryptosocket" in name or "tumarcsp" in name:
                        pids.add(pid)
                finally:
                    win32api.CloseHandle(h)
            except Exception:
                continue
    except Exception as e:
        log.debug("popup.pid_scan_failed", err=str(e)[:80])
    return pids


def _find_popup_hwnd(needles: Sequence[str]) -> Optional[int]:
    """Find the Tumar price dialog hwnd using ctypes.

    Search order:
      1. Visible window whose title matches any needle (e.g. "ценов", "предлож", "заполн").
      2. Visible window structurally having both an Edit child and an OK Button child.
      3. Visible dialog-sized window owned by CryptoSocket.exe / TumarCSP.exe.
    """
    user32 = ctypes.windll.user32
    cs_pids = _cryptosocket_pids()
    by_title: list[int] = []
    by_structure: list[int] = []
    by_process: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cname = cls_buf.value or ""
        if cname in NOISE_CLASS:
            return True

        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value or ""
        low = title.lower()

        # 1. Title match
        if any(n in low for n in needles):
            by_title.append(int(hwnd))
            return False  # found immediately

        # 2. Structural scan (Edit child + OK button child)
        has_edit = [False]
        has_ok = [False]

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_child(ch, _lp):
            ch_cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ch, ch_cls, 256)
            ch_cname = (ch_cls.value or "").lower()
            if "edit" in ch_cname:
                has_edit[0] = True
            if "button" in ch_cname:
                tbuf = ctypes.create_unicode_buffer(64)
                user32.GetWindowTextW(ch, tbuf, 64)
                if (tbuf.value or "").strip() in OK_LABELS:
                    has_ok[0] = True
            return True

        user32.EnumChildWindows(hwnd, _enum_child, 0)
        if has_edit[0] and has_ok[0]:
            by_structure.append(int(hwnd))

        # 3. Process match (CryptoSocket / Tumar dialog)
        if cs_pids and not by_process:
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in cs_pids:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if 250 <= w <= 950 and 150 <= h <= 750:
                        by_process.append(int(hwnd))

        return True

    user32.EnumWindows(_enum, 0)
    if by_title:
        return by_title[0]
    if by_structure:
        return by_structure[0]
    if by_process:
        return by_process[0]
    return None


def _fill_hwnd(hwnd: int, value: str, ok_xy: Optional[Sequence[int]] = None) -> bool:
    """Fill price into target window.

    1. SendMessage WM_SETTEXT + verify WM_GETTEXT + SendMessage BM_CLICK.
    2. Fallback to foreground UI click & paste/type.
    """
    user32 = ctypes.windll.user32
    WM_SETTEXT = 0x000C
    WM_GETTEXT = 0x000D
    BM_CLICK = 0x00F5
    VK_RETURN = 0x0D
    WM_KEYDOWN = 0x0100

    edit_hwnds: list[int] = []
    ok_hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum_child(ch, _lp):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ch, cls, 256)
        cname = (cls.value or "").lower()
        if "edit" in cname and not edit_hwnds:
            edit_hwnds.append(int(ch))
        if "button" in cname:
            tbuf = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(ch, tbuf, 64)
            if (tbuf.value or "").strip() in OK_LABELS and not ok_hwnds:
                ok_hwnds.append(int(ch))
        return True

    user32.EnumChildWindows(hwnd, _enum_child, 0)

    # Strategy 1: Win32 WM_SETTEXT
    if edit_hwnds:
        target_edit = edit_hwnds[0]
        user32.SendMessageW(target_edit, WM_SETTEXT, 0, value)
        got = ctypes.create_unicode_buffer(128)
        user32.SendMessageW(target_edit, WM_GETTEXT, 128, got)
        got_val = got.value or ""
        if _norm_digits(value) in _norm_digits(got_val):
            if ok_hwnds:
                user32.SendMessageW(ok_hwnds[0], BM_CLICK, 0, 0)
            else:
                user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0)
            log.info("popup.win32.filled", price=value, hwnd=hwnd, readback=got_val)
            return True
        log.warning("popup.win32.set_mismatch", expected=value, got=got_val)

    # Strategy 2: Foreground UI click and paste / type
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.04)

    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.0
    except Exception as e:
        log.warning("popup.no_pyautogui", err=str(e)[:80])
        return False

    cx, cy = None, None
    if edit_hwnds:
        r = wintypes.RECT()
        if user32.GetWindowRect(edit_hwnds[0], ctypes.byref(r)):
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
    if cx is None or cy is None:
        r = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            w = r.right - r.left
            h = r.bottom - r.top
            cx = r.left + w // 2
            cy = r.top + int(h * 0.42)

    if cx is not None and cy is not None:
        pyautogui.click(cx, cy)
        time.sleep(0.02)
        pyautogui.click(cx, cy)
        time.sleep(0.02)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.02)
        if _set_clipboard(value):
            pyautogui.hotkey("ctrl", "v")
        else:
            pyautogui.typewrite(value, interval=0.0)
        time.sleep(0.05)

        if ok_hwnds:
            r_ok = wintypes.RECT()
            if user32.GetWindowRect(ok_hwnds[0], ctypes.byref(r_ok)):
                pyautogui.click((r_ok.left + r_ok.right) // 2, (r_ok.top + r_ok.bottom) // 2)
                time.sleep(0.02)
        elif ok_xy:
            pyautogui.click(int(ok_xy[0]), int(ok_xy[1]))
            time.sleep(0.02)
        pyautogui.press("enter")
        log.info("popup.ui.filled", price=value, hwnd=hwnd, click_xy=[cx, cy])
        return True

    return False


def _screen_pixel(x: int, y: int) -> Optional[tuple[int, int, int]]:
    try:
        u32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        hdc = u32.GetDC(0)
        try:
            col = gdi.GetPixel(hdc, x, y)
        finally:
            u32.ReleaseDC(0, hdc)
        if col < 0:
            return None
        return (col & 0xFF, (col >> 8) & 0xFF, (col >> 16) & 0xFF)
    except Exception:
        return None


def _matches_pixel(pixel: Optional[tuple[int, int, int]], rgb: Sequence[int]) -> bool:
    return pixel is not None and all(abs(pixel[i] - int(rgb[i])) <= 8 for i in range(3))


def _fill_via_coords(
    value: str,
    *,
    field_xy: Sequence[int],
    ok_xy: Optional[Sequence[int]],
    detect_xy: Optional[Sequence[int]],
    detect_rgb: Optional[Sequence[int]],
    deadline: float,
    stop=None,
    require_visible: bool = False,
) -> bool:
    """Fallback: Click field_xy, type the price, click ok_xy / press Enter."""
    if not require_visible:
        if detect_xy and detect_rgb:
            while time.time() < deadline:
                if _stopped(stop):
                    return False
                px = _screen_pixel(int(detect_xy[0]), int(detect_xy[1]))
                if _matches_pixel(px, detect_rgb):
                    break
                time.sleep(0.1)
        else:
            time.sleep(0.3)
    if _stopped(stop):
        # The popup is already gone (price entered) — typing now would land the
        # price + Enter in whatever window happens to be in front.
        return False

    settle = _PRE_TYPE_SETTLE_S
    if settle > 0:
        time.sleep(settle)

    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.0
    except Exception as e:
        log.warning("popup.no_pyautogui", err=str(e))
        return False

    if _stopped(stop):
        return False
    if require_visible and (
        not detect_xy or not detect_rgb
        or not _matches_pixel(_screen_pixel(int(detect_xy[0]), int(detect_xy[1])), detect_rgb)
    ):
        return False

    fx, fy = int(field_xy[0]), int(field_xy[1])
    try:
        pyautogui.click(fx, fy)
        time.sleep(0.02)
        pyautogui.click(fx, fy)
        time.sleep(0.02)
        if _stopped(stop):
            return False
        pyautogui.hotkey("ctrl", "a")
        if _set_clipboard(value):
            pyautogui.hotkey("ctrl", "v")
        else:
            pyautogui.typewrite(value, interval=0.0)
        time.sleep(0.05)
        if _stopped(stop):
            return False
        if ok_xy:
            pyautogui.click(int(ok_xy[0]), int(ok_xy[1]))
            time.sleep(0.02)
        if not _stopped(stop):
            pyautogui.press("enter")
    except Exception as e:
        log.warning("popup.coords.type_failed", err=str(e))
        return False
    log.info("popup.coords.filled", price=value, field_xy=[fx, fy],
             ok_xy=list(ok_xy) if ok_xy else None)
    return True


# --------------------------------------------------------------------------- #
# public entry point                                                          #
# --------------------------------------------------------------------------- #
def fill_price_popup(
    price: float,
    *,
    timeout_s: float = 60.0,
    field_xy: Optional[Sequence[int]] = None,
    ok_xy: Optional[Sequence[int]] = None,
    detect_xy: Optional[Sequence[int]] = None,
    detect_rgb: Optional[Sequence[int]] = None,
    stop=None,
    **_ignored,
) -> bool:
    """Fill the TumarCSP price popup.

    ``stop``: optional ``threading.Event``. TumarCSP sets it the moment
    EncryptOfferPrice answers (the popup is done — filled by us or by hand). The
    search loop then exits at once instead of running on for up to ``timeout_s``
    and finally typing the price blind at the fallback coordinates.

    Strategy 1 (fast, reliable): win32 control automation (WM_SETTEXT) or
    direct UI click & paste/type into the control.
    Strategy 2 (fallback): pyautogui coordinate injection from config.yaml.
    """
    if sys.platform != "win32":
        log.warning("popup.unsupported_os", platform=sys.platform)
        return False

    if _stopped(stop):
        return False
    value = _fmt_price(price)
    deadline = time.time() + timeout_s
    detect_point = None
    if field_xy and detect_xy and detect_rgb:
        point = (int(detect_xy[0]), int(detect_xy[1]))
        baseline = _screen_pixel(*point)
        # A color already on-screen before the popup opens is ambiguous.
        if baseline is not None and not _matches_pixel(baseline, detect_rgb):
            detect_point = point

    if not is_elevated():
        log.warning("popup.not_elevated",
                    hint="bot is NOT Administrator — start-bot.bat must elevate (UAC Yes)")

    log.info("popup.search_start", price=value, timeout_s=timeout_s, elevated=is_elevated())
    t0 = time.time()
    attempts = 0

    while time.time() < deadline:
        if _stopped(stop):
            log.info("popup.search.stopped", price=value, attempts=attempts)
            return False
        attempts += 1
        hwnd = _find_popup_hwnd(WINDOW_TITLE_NEEDLES)
        if hwnd:
            try:
                if _fill_hwnd(hwnd, value, ok_xy=ok_xy):
                    return True
            except Exception as e:
                log.warning("popup.fill_error", err=str(e)[:100], attempt=attempts)

        elapsed = time.time() - t0
        if elapsed < 2.5 and detect_point and detect_rgb:
            if _matches_pixel(_screen_pixel(*detect_point), detect_rgb):
                detect_point = None
                log.info("popup.coords.detected", price=value, elapsed_s=round(elapsed, 3))
                if _fill_via_coords(value, field_xy=field_xy, ok_xy=ok_xy,
                                    detect_xy=detect_xy, detect_rgb=detect_rgb,
                                    deadline=deadline, stop=stop, require_visible=True):
                    return True

        if elapsed >= 2.5 and field_xy:
            log.info("popup.coords.fallback_try", price=value, field_xy=list(field_xy))
            if _fill_via_coords(value, field_xy=field_xy, ok_xy=ok_xy,
                                detect_xy=detect_xy, detect_rgb=detect_rgb,
                                deadline=time.time() + 2.0, stop=stop):
                return True

        time.sleep(0.05)

    if _stopped(stop):
        return False
    if field_xy:
        log.info("popup.coords.last_resort", price=value, field_xy=list(field_xy))
        return _fill_via_coords(value, field_xy=field_xy, ok_xy=ok_xy,
                                detect_xy=detect_xy, detect_rgb=detect_rgb,
                                deadline=time.time() + 5.0, stop=stop)

    log.warning("popup.search.timeout", price=value, attempts=attempts)
    return False


def make_autofiller(
    *,
    timeout_s: float = 60.0,
    field_xy: Optional[Sequence[int]] = None,
    ok_xy: Optional[Sequence[int]] = None,
    field_offset: Optional[Sequence[int]] = None,
    ok_offset: Optional[Sequence[int]] = None,
    detect_xy: Optional[Sequence[int]] = None,
    detect_rgb: Optional[Sequence[int]] = None,
):
    """Return a callable(price, stop=None) -> bool for TumarCSP.autofill.

    ``supports_stop`` tells TumarCSP it may pass a ``threading.Event`` to end the
    search early (see fill_price_popup).
    """
    def _fill(price: float, stop=None) -> bool:
        return fill_price_popup(
            price, timeout_s=timeout_s, field_xy=field_xy, ok_xy=ok_xy,
            field_offset=field_offset, ok_offset=ok_offset,
            detect_xy=detect_xy, detect_rgb=detect_rgb, stop=stop,
        )
    _fill.supports_stop = True
    return _fill
