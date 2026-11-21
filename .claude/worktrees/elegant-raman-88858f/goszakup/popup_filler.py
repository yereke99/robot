"""Auto-fill the TumarCSP price popup (Windows only).

Gamma tenders seal the price inside TumarCSP: calling EncryptOfferPrice opens a
native Windows dialog ("Заполнение ценового предложения") with a price field and
an OK button. There's no API to pass the price — it must be entered in that
window. So for hands-free bidding we drive the window: wait for it, type the
minimum, click OK. TumarCSP then returns the sealed price over its WebSocket
exactly as if a human had typed it.

This is legitimate: it's the operator's own machine entering the operator's own
minimum price — identical input to typing by hand.

Two strategies, in order:

  1. win32 control automation (preferred). The Tumar popup is a classic native
     Win32 dialog. pywinauto's *win32* backend enumerates its Edit + Button
     cleanly (unlike the uia backend, which fails on some pywinauto builds), and
     lets us READ BACK the field before clicking OK.

  2. absolute-coordinate fallback (last resort). If the window can't be found by
     enumeration at all, fall back to screen coordinates measured for THIS
     machine (config: tumarcsp.popup.*): wait for the popup's background pixel,
     click the field, type, then verify via the clipboard, and only then Enter.

CRITICAL invariant for both: OK/Enter is pressed ONLY after the typed value is
confirmed to be in the field (window_text in win32 mode, clipboard copy in
coordinate mode). A wrong/empty price loses the tender, so we never submit blind.

Windows-only. On any other OS, or if it can't confirm the value, returns False
and the caller falls back to manual entry (the long EncryptOfferPrice timeout
still applies, so a human can type it).
"""

from __future__ import annotations

import ctypes
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

# Coordinates measured for the operator's machine, where TumarCSP renders its
# price popup at a fixed position. Override via config tumarcsp.popup.*.
# detect_xy/detect_rgb GATE everything: pyautogui is not even imported until this
# pixel shows the popup's background colour, so it can never disturb anything
# (incl. manual document signing) while the popup is absent.
DEFAULT_FIELD_XY = (1914, 1011)
DEFAULT_DETECT_XY = (1751, 952)
DEFAULT_DETECT_RGB = (240, 240, 240)

# Settle pause between "popup pixel detected" and the first pyautogui input — the
# window can paint a hair before it accepts keyboard focus, which is the flaky
# "typed nothing" case. Tunable via env (operator asked for 0.05s).
try:
    _PRE_TYPE_SETTLE_S = float(os.environ.get("ATAGOS_POPUP_SETTLE_S", "0.05"))
except ValueError:
    _PRE_TYPE_SETTLE_S = 0.05


def is_supported() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pywinauto  # noqa: F401
        return True
    except Exception:
        return False


def is_elevated() -> bool:
    """True if this process has admin rights (runs at High integrity).

    TumarCSP runs elevated; without parity, Windows UIPI blocks BOTH win32
    control automation (ReadProcessMemory) AND SendInput coordinate injection —
    so the popup cannot be filled. This is the root cause of every prior
    auto-fill failure: the bot ran at Medium integrity, Tumar at High."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _fmt_price(price: float) -> str:
    """Money string the field expects: 2 decimals, dot separator (the popup's
    own range is shown as e.g. 6030145.60)."""
    return f"{price:.2f}"


# --------------------------------------------------------------------------- #
# low-level helpers (Windows)                                                 #
# --------------------------------------------------------------------------- #
def _clipboard_text() -> str:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return str(data or "")
    except Exception:
        return ""


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


def _looks_like_price_dialog(w) -> bool:
    """True if ``w`` is a non-noise dialog that has BOTH a text field and an
    OK-style button. This is a title-INDEPENDENT signal: under UIPI the popup's
    title often reads back empty, and we don't know its exact caption anyway, so
    matching on structure (Edit + OK) is far more robust than matching on text.
    The readback/clipboard invariant in the fillers protects us from ever
    submitting into a wrongly-matched window."""
    try:
        has_edit = has_ok = False
        for c in w.descendants():
            try:
                cls = (c.class_name() or "").lower()
                if "edit" in cls:
                    has_edit = True
                elif "button" in cls and (c.window_text() or "").strip() in OK_LABELS:
                    has_ok = True
                if has_edit and has_ok:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _find_popup_window(title_needles: Sequence[str]):
    """Return the TumarCSP price popup as a top-level window, or None.

    Two-pass, both via win32 enumeration (backend-agnostic): (1) a window whose
    title contains a needle — strongest signal, preferred; (2) failing that, a
    non-noise dialog that structurally looks like the popup (Edit + OK button) —
    catches the UIPI case where the title is unreadable/empty."""
    try:
        from pywinauto import Desktop
    except Exception:
        return None
    try:
        wins = Desktop(backend="win32").windows(top_level_only=True, visible_only=True)
    except Exception:
        return None
    structural = None
    for w in wins:
        try:
            if w.class_name() in NOISE_CLASS:
                continue
            low = (w.window_text() or "").lower()
            if any(n in low for n in title_needles):
                return w
            if structural is None and _looks_like_price_dialog(w):
                structural = w
        except Exception:
            continue
    if structural is not None:
        try:
            log.info("popup.found_by_structure", title=structural.window_text(),
                     cls=structural.class_name())
        except Exception:
            pass
    return structural


# --------------------------------------------------------------------------- #
# strategy 1 — win32 control automation                                       #
# --------------------------------------------------------------------------- #
def _fill_via_win32(hwnd: int, value: str) -> bool:
    """Type + verify + click OK using the win32 backend. True only if the value
    is confirmed in the field. Returns False (no submit) on any doubt."""
    try:
        from pywinauto import Application
        app = Application(backend="win32").connect(handle=hwnd, timeout=2)
        dlg = app.window(handle=hwnd)
        dlg.set_focus()
    except Exception as e:  # noqa: BLE001
        log.warning("popup.win32.connect_failed", err=str(e))
        return False

    # locate the price field (first Edit control)
    edit = None
    try:
        for c in dlg.descendants():
            try:
                if "edit" in (c.class_name() or "").lower():
                    edit = c
                    break
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        log.warning("popup.win32.descendants_failed", err=str(e))
        return False
    if edit is None:
        log.warning("popup.win32.no_edit")
        return False

    def _readback() -> str:
        for getter in ("window_text", "get_value"):
            try:
                v = getattr(edit, getter)()
                if v:
                    return str(v)
            except Exception:
                pass
        return ""

    try:
        edit.set_focus()
    except Exception:
        pass
    # clear then set
    try:
        edit.set_edit_text("")
    except Exception:
        pass
    try:
        edit.set_edit_text(value)
    except Exception:
        pass
    if value not in _readback():
        try:
            edit.type_keys("^a{BACKSPACE}", set_foreground=True, pause=0.02)
            edit.type_keys(value, set_foreground=True, pause=0.02)
        except Exception:
            pass

    got = _readback()
    log.info("popup.win32.readback", expected=value, got=got)
    if value not in (got or ""):
        log.warning("popup.win32.value_not_set", expected=value, got=got)
        return False

    # click OK (only now that the value is confirmed)
    clicked = False
    try:
        for c in dlg.descendants():
            try:
                if "button" not in (c.class_name() or "").lower():
                    continue
                if (c.window_text() or "").strip() in OK_LABELS:
                    c.click()
                    clicked = True
                    break
            except Exception:
                continue
    except Exception:
        pass
    if not clicked:
        try:
            dlg.type_keys("{ENTER}", set_foreground=True)
        except Exception:
            pass
    log.info("popup.win32.filled", price=value, clicked_ok=clicked)
    return True


# --------------------------------------------------------------------------- #
# strategy 2 — absolute coordinates + clipboard verification                  #
# --------------------------------------------------------------------------- #
def _click(x: int, y: int) -> None:
    """Click absolute screen coords via the lowest-level injector available.
    pyautogui (SendInput) reaches windows the win32 backend can't touch."""
    try:
        import pyautogui
        pyautogui.click(x, y)
        return
    except Exception:
        pass
    from pywinauto import mouse
    mouse.click(coords=(x, y))


def _type(keys: str, *, literal: bool) -> None:
    try:
        import pyautogui
        if literal:
            pyautogui.typewrite(keys, interval=0.02)
        else:
            pyautogui.hotkey(*keys)  # keys is a tuple of key names
        return
    except Exception:
        pass
    from pywinauto import keyboard
    if literal:
        keyboard.send_keys(keys, pause=0.02)
    else:
        keyboard.send_keys("".join("^" if k == "ctrl" else k for k in keys))


def _fill_via_coords(
    value: str,
    *,
    field_xy: Sequence[int],
    ok_xy: Optional[Sequence[int]],
    detect_xy: Optional[Sequence[int]],
    detect_rgb: Optional[Sequence[int]],
    deadline: float,
) -> bool:
    """Wait for the popup pixel, THEN import + use pyautogui to click the field,
    type the price with NO inter-key delay, and press Enter. pyautogui is NOT
    imported until the popup is confirmed on screen, so it can't disturb anything
    earlier (e.g. document signing). No clipboard verify: TumarCSP's field doesn't
    answer Ctrl+A/Ctrl+C, so that check was a false-negative that blocked Enter."""
    # wait for the popup to actually be on screen (background pixel match).
    # Pure ctypes GetPixel — NO pyautogui import yet.
    if detect_xy and detect_rgb:
        want = tuple(int(v) for v in detect_rgb)
        while time.time() < deadline:
            px = _screen_pixel(int(detect_xy[0]), int(detect_xy[1]))
            if px and all(abs(px[i] - want[i]) <= 8 for i in range(3)):
                break
            time.sleep(0.1)
        else:
            log.warning("popup.coords.detect_timeout", xy=list(detect_xy), want=list(want))
            return False
    else:
        time.sleep(0.4)  # no detector configured — give the window a moment

    # The pixel matches the instant the window PAINTS, which can be a hair before
    # it's ready to take keyboard focus — typing then lands nowhere (the flaky
    # "sometimes it fills, sometimes not"). A short settle before any pyautogui
    # input fixes it. Tunable via ATAGOS_POPUP_SETTLE_S.
    settle = _PRE_TYPE_SETTLE_S
    if settle > 0:
        time.sleep(settle)

    # popup is up — ONLY NOW import pyautogui. PAUSE=0 so clicks/keys fire with no
    # artificial gaps (operator wants it fast); FAILSAFE off so a stray cursor
    # can't abort an automated run.
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.0
    except Exception as e:  # noqa: BLE001
        log.warning("popup.no_pyautogui", err=str(e))
        return False

    fx, fy = int(field_xy[0]), int(field_xy[1])
    try:
        pyautogui.click(fx, fy)            # focus the price field
        pyautogui.click(fx, fy)            # second click — ensure caret is in it
        time.sleep(0.05)
        pyautogui.hotkey("ctrl", "a")      # select any existing content to replace
        pyautogui.typewrite(value, interval=0.0)   # type the price, no gaps
        if ok_xy:
            pyautogui.click(int(ok_xy[0]), int(ok_xy[1]))
        pyautogui.press("enter")           # ALWAYS confirm (no verify gate)
    except Exception as e:  # noqa: BLE001
        log.warning("popup.coords.type_failed", err=str(e))
        return False
    log.info("popup.coords.filled", price=value, field_xy=[fx, fy],
             ok_xy=list(ok_xy) if ok_xy else None,
             note="typed + Enter, no clipboard verify (per operator)")
    return True


# --------------------------------------------------------------------------- #
# public entry point                                                          #
# --------------------------------------------------------------------------- #
def _window_rect(w):
    try:
        r = w.rectangle()  # GetWindowRect — works across privilege levels
        return int(r.left), int(r.top)
    except Exception:
        return None


def fill_price_popup(
    price: float,
    *,
    timeout_s: float = 180.0,
    field_xy: Optional[Sequence[int]] = None,
    ok_xy: Optional[Sequence[int]] = None,
    detect_xy: Optional[Sequence[int]] = None,
    detect_rgb: Optional[Sequence[int]] = None,
    **_ignored,  # tolerate legacy kwargs (poll_s, field_offset, ok_offset)
) -> bool:
    """Fill the TumarCSP price popup with **pyautogui only** (per operator request).

    No win32 / pywinauto control automation, no window enumeration. We wait for
    the popup's background pixel (``detect_xy``/``detect_rgb``), then click the
    field, type the price, verify it via the clipboard, and press Enter (or click
    ``ok_xy``). pyautogui is imported only AFTER the pixel matches, so nothing on
    screen is touched while documents are still being signed. Coordinates default
    to the operator's machine; override via config ``tumarcsp.popup.*``. Returns
    False (→ manual entry) on any doubt; never submits an unconfirmed value.
    """
    if sys.platform != "win32":
        log.warning("popup.unsupported_os", platform=sys.platform)
        return False
    if not is_elevated():
        log.warning("popup.not_elevated",
                    hint="bot is NOT running as Administrator; TumarCSP runs at High "
                         "integrity, so UIPI may block coordinate injection. Relaunch elevated.")

    value = _fmt_price(price)
    field_xy = field_xy or DEFAULT_FIELD_XY
    detect_xy = detect_xy or DEFAULT_DETECT_XY
    detect_rgb = detect_rgb or DEFAULT_DETECT_RGB
    deadline = time.time() + timeout_s
    log.info("popup.pyautogui.start", price=value, field_xy=list(field_xy),
             ok_xy=list(ok_xy) if ok_xy else None, detect_xy=list(detect_xy),
             detect_rgb=list(detect_rgb), timeout_s=timeout_s)
    return _fill_via_coords(value, field_xy=field_xy, ok_xy=ok_xy,
                            detect_xy=detect_xy, detect_rgb=detect_rgb, deadline=deadline)


def make_autofiller(
    *,
    timeout_s: float = 180.0,
    field_xy: Optional[Sequence[int]] = None,
    ok_xy: Optional[Sequence[int]] = None,
    field_offset: Optional[Sequence[int]] = None,
    ok_offset: Optional[Sequence[int]] = None,
    detect_xy: Optional[Sequence[int]] = None,
    detect_rgb: Optional[Sequence[int]] = None,
):
    """Return a callable(price) -> bool for TumarCSP.autofill."""
    def _fill(price: float) -> bool:
        return fill_price_popup(
            price, timeout_s=timeout_s, field_xy=field_xy, ok_xy=ok_xy,
            field_offset=field_offset, ok_offset=ok_offset,
            detect_xy=detect_xy, detect_rgb=detect_rgb,
        )
    return _fill
