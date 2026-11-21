"""Fill and confirm TumarCSP's native Windows price dialog."""

from __future__ import annotations

import ctypes
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PureWindowsPath
from typing import Callable, Sequence

import structlog

log = structlog.get_logger("popup")

_PRICE_TITLES = (
    ("ценов", "предлож"), ("заполн", "цен"), ("ввод", "цен"), ("введ", "цен"),
    ("price", "offer"), ("price", "proposal"), ("price", "bid"), ("price", "enter"),
    ("баға", "ұсын"), ("баға", "енгіз"),
)
_PRICE_LABELS = {"заполнение ценового предложения", "укажите цену за единицу"}
_PRICE_ACTION_LABELS = {"сохранить в jpeg", "отмена"}
_PROVIDER_IMAGES = re.compile(r"(?:cryptosocket|tumarcsp)(?:[ _-]?(?:32|64))?\.exe", re.IGNORECASE)
_OK_LABELS = {"ok", "ок", "да", "подтвердить", "принять", "растау"}
_POLL_INTERVAL_S = 0.05
_DEFAULT_TIMEOUT_S = 4.0
_CONTROL_READY_GRACE_S = 0.15

_WM_SETTEXT = 0x000C
_WM_GETTEXT = 0x000D
_WM_CHAR = 0x0102
_WM_COMMAND = 0x0111
_EM_SETSEL = 0x00B1
_BM_CLICK = 0x00F5
_GWL_STYLE = -16
_ES_READONLY = 0x0800
_SMTO_ABORTIFHUNG = 0x0002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def is_supported() -> bool:
    return sys.platform == "win32"


def is_elevated() -> bool:
    if not is_supported():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _price_matches(expected: str, actual: str) -> bool:
    normalized = "".join(actual.split()).replace(",", ".")
    return bool(
        re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", normalized)
        and Decimal(expected) == Decimal(normalized)
    )


def _price_title(title: str) -> bool:
    text = title.casefold()
    return any(all(word in text for word in group) for group in _PRICE_TITLES)


def _can_act(deadline: float, stop) -> bool:
    return (stop is None or not stop.is_set()) and time.monotonic() < deadline


def _pause(deadline: float, stop) -> None:
    delay = max(0.0, min(_POLL_INTERVAL_S, deadline - time.monotonic()))
    if stop is None:
        time.sleep(delay)
    else:
        stop.wait(delay)


@dataclass(frozen=True)
class _Dialog:
    hwnd: int
    pid: int
    field: int
    button: int
    identifying_controls: tuple[tuple[int, str], ...] = ()


class _Win32:
    """Native controls targeted by HWND, independent of desktop focus."""

    def __init__(self) -> None:
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        hwnd, uint, dword = wintypes.HWND, wintypes.UINT, wintypes.DWORD
        flag, handle = wintypes.BOOL, wintypes.HANDLE
        out_dword = ctypes.POINTER(dword)
        self._bind(self.user.EnumWindows, flag, self._enum_proc, wintypes.LPARAM)
        self._bind(self.user.EnumChildWindows, flag, hwnd, self._enum_proc, wintypes.LPARAM)
        self._bind(self.user.GetWindowThreadProcessId, dword, hwnd, out_dword)
        self._bind(self.user.GetWindowTextW, ctypes.c_int, hwnd, wintypes.LPWSTR, ctypes.c_int)
        self._bind(self.user.GetClassNameW, ctypes.c_int, hwnd, wintypes.LPWSTR, ctypes.c_int)
        self._bind(self.user.GetWindowLongW, wintypes.LONG, hwnd, ctypes.c_int)
        self._bind(self.user.IsWindowVisible, flag, hwnd)
        self._bind(self.user.IsWindowEnabled, flag, hwnd)
        self._bind(self.user.IsChild, flag, hwnd, hwnd)
        self._bind(self.user.GetParent, hwnd, hwnd)
        self._bind(self.user.GetDlgCtrlID, ctypes.c_int, hwnd)
        self._bind(self.user.PostMessageW, flag, hwnd, uint, wintypes.WPARAM, wintypes.LPARAM)
        self._bind(
            self.user.SendMessageTimeoutW, wintypes.LPARAM,
            hwnd, uint, wintypes.WPARAM, wintypes.LPARAM, uint, uint, ctypes.POINTER(ctypes.c_size_t),
        )
        self._bind(self.kernel.OpenProcess, handle, dword, flag, dword)
        self._bind(self.kernel.QueryFullProcessImageNameW, flag, handle, dword, wintypes.LPWSTR, out_dword)
        self._bind(self.kernel.CloseHandle, flag, handle)

    @staticmethod
    def _bind(function, result, *args) -> None:
        function.restype = result
        function.argtypes = args

    def visible_windows(self, parent: int | None = None) -> list[int]:
        windows: list[int] = []

        @self._enum_proc
        def collect(hwnd, _):
            if self.user.IsWindowVisible(hwnd):
                windows.append(hwnd)
            return True

        if parent is None:
            self.user.EnumWindows(collect, 0)
        else:
            self.user.EnumChildWindows(parent, collect, 0)
        return windows

    def pid(self, hwnd: int) -> int:
        process = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
        return process.value

    def is_provider(self, pid: int) -> bool:
        process = self.kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return False
        try:
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if not self.kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                return False
            return _PROVIDER_IMAGES.fullmatch(PureWindowsPath(name.value).name) is not None
        finally:
            self.kernel.CloseHandle(process)

    def window_text(self, hwnd: int) -> str:
        text = ctypes.create_unicode_buffer(512)
        self.user.GetWindowTextW(hwnd, text, len(text))
        return text.value

    def _class_name(self, hwnd: int) -> str:
        name = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, name, len(name))
        return name.value.casefold()

    def _send(self, hwnd, message, arg, lparam, deadline) -> int | None:
        timeout_ms = int((deadline - time.monotonic()) * 1000)
        if timeout_ms <= 0:
            return None
        result = ctypes.c_size_t()
        if self.user.SendMessageTimeoutW(
            hwnd, message, arg, lparam if isinstance(lparam, int) else ctypes.addressof(lparam),
            _SMTO_ABORTIFHUNG, min(200, timeout_ms), ctypes.byref(result),
        ):
            return result.value
        return None

    def _control_text(self, hwnd: int, deadline: float) -> str:
        buffer = ctypes.create_unicode_buffer(128)
        result = self._send(hwnd, _WM_GETTEXT, len(buffer), buffer, deadline)
        return buffer.value if result and result < len(buffer) - 1 else ""

    def _control_label(self, hwnd: int, deadline: float) -> str:
        caption = self._control_text(hwnd, deadline).replace("&", "").casefold()
        return " ".join(caption.split()).rstrip(" .:")

    def select_dialog(self, hwnd: int, deadline: float) -> _Dialog | None:
        pid = self.pid(hwnd)
        if not pid or not self.is_provider(pid):
            return None
        has_price_title = _price_title(self.window_text(hwnd))
        price_label = None
        action_buttons = []
        fields = []
        buttons = []
        for child in self.visible_windows(hwnd):
            if time.monotonic() >= deadline:
                return None
            if self.pid(child) != pid:
                continue
            kind = self._class_name(child)
            # Some provider versions put the heading inside a borderless form.
            if "static" in kind or "label" in kind:
                if not has_price_title and price_label is None:
                    label = self._control_label(child, deadline)
                    if label in _PRICE_LABELS:
                        price_label = (child, label)
                continue
            style = self.user.GetWindowLongW(child, _GWL_STYLE)
            if "edit" in kind and not style & _ES_READONLY and self.user.IsWindowEnabled(child):
                fields.append(child)
            if "button" in kind and style & 0x0F in (0, 1, 0x0B):
                label = self._control_label(child, deadline)
                if label in _OK_LABELS:
                    buttons.append(child)
                elif not has_price_title and label in _PRICE_ACTION_LABELS:
                    action_buttons.append((child, label))
        if len(fields) != 1 or len(buttons) != 1:
            return None
        if has_price_title:
            identifying_controls = ()
        elif price_label is not None:
            identifying_controls = (price_label,)
        elif len(action_buttons) == 2 and {label for _, label in action_buttons} == _PRICE_ACTION_LABELS:
            # An owner-drawn heading may have no separate native text control.
            identifying_controls = tuple(action_buttons)
        else:
            return None
        return _Dialog(hwnd, pid, fields[0], buttons[0], identifying_controls)

    def valid(self, dialog: _Dialog) -> bool:
        children = (dialog.field, dialog.button, *(hwnd for hwnd, _ in dialog.identifying_controls))
        return (
            self.pid(dialog.hwnd) == dialog.pid
            and self.is_provider(dialog.pid)
            and (dialog.identifying_controls or _price_title(self.window_text(dialog.hwnd)))
            and self.user.IsWindowVisible(dialog.hwnd)
            and self.user.IsWindowEnabled(dialog.field)
            and all(
                self.user.IsChild(dialog.hwnd, child)
                and self.pid(child) == dialog.pid
                and self.user.IsWindowVisible(child)
                for child in children
            )
        )

    def _matches_identifying_controls(self, dialog: _Dialog, deadline: float) -> bool:
        return all(self._control_label(hwnd, deadline) == label for hwnd, label in dialog.identifying_controls)

    def set_value(self, dialog: _Dialog, value: str, deadline: float, stop) -> bool:
        if not self._matches_identifying_controls(dialog, deadline) or not _can_act(deadline, stop):
            return False
        return bool(self._send(dialog.field, _WM_SETTEXT, 0, ctypes.create_unicode_buffer(value), deadline))

    def type_value(self, dialog: _Dialog, value: str, deadline: float, stop) -> bool:
        if (
            not self.valid(dialog)
            or not self._matches_identifying_controls(dialog, deadline)
            or not _can_act(deadline, stop)
        ):
            return False
        if self._send(dialog.field, _EM_SETSEL, 0, -1, deadline) is None:
            return False
        for character in value:
            if not self.valid(dialog) or not _can_act(deadline, stop):
                return False
            if self._send(dialog.field, _WM_CHAR, ord(character), 1, deadline) is None:
                return False
        return True

    def read_value(self, dialog: _Dialog, deadline: float) -> str:
        return self._control_text(dialog.field, deadline)

    def ready_to_confirm(self, dialog: _Dialog) -> bool:
        return bool(self.user.IsWindowEnabled(dialog.button))

    def confirm(self, dialog: _Dialog, value: str, deadline: float, stop) -> bool:
        parent = self.user.GetParent(dialog.button)
        control_id = self.user.GetDlgCtrlID(dialog.button)
        if parent != dialog.hwnd and not self.user.IsChild(dialog.hwnd, parent):
            return False
        if (
            self.pid(parent) != dialog.pid
            or not self.valid(dialog)
            or not self.ready_to_confirm(dialog)
            or self._control_label(dialog.button, deadline) not in _OK_LABELS
            or not _price_matches(value, self.read_value(dialog, deadline))
            or not self._matches_identifying_controls(dialog, deadline)
            or not _can_act(deadline, stop)
        ):
            return False
        if not 0 < control_id <= 0xFFFF:
            # WinForms can use a control identifier larger than a WORD.
            return bool(self.user.PostMessageW(dialog.button, _BM_CLICK, 0, 0))
        # BN_CLICKED == 0, so the low word is the control ID. Send the normal
        # button notification to its own parent, with no desktop input.
        return bool(self.user.PostMessageW(parent, _WM_COMMAND, control_id, dialog.button))


def _fill_dialog(api: _Win32, dialog: _Dialog, value: str, deadline: float, stop) -> bool:
    values = (value, value.replace(".", ","))
    for targeted_typing in (False, True):
        for candidate in values:
            if not api.valid(dialog) or not _can_act(deadline, stop):
                return False
            if targeted_typing:
                wrote = api.type_value(dialog, candidate, deadline, stop)
            else:
                wrote = api.set_value(dialog, candidate, deadline, stop)
            if wrote and _price_matches(value, api.read_value(dialog, deadline)):
                # A setter can display the right value without triggering the
                # provider's keyboard-driven validation. Give it a moment to
                # enable the button, then try real character messages if needed.
                ready_deadline = min(deadline, time.monotonic() + _CONTROL_READY_GRACE_S)
                while _can_act(ready_deadline, stop) and api.valid(dialog):
                    if api.ready_to_confirm(dialog):
                        confirmed = api.confirm(dialog, value, deadline, stop)
                        if not confirmed:
                            log.warning("popup.win32.confirm_failed")
                        return confirmed
                    _pause(ready_deadline, stop)

    log.warning("popup.win32.readback_unconfirmed")
    return False


def fill_price_popup(
    price: float,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    stop=None,
    ready: Callable[[], None] | None = None,
    **_legacy_coordinates,
) -> bool:
    """Write the requested price into one newly visible, verified Tumar dialog.

    Coordinates from older profiles are accepted for compatibility. Only native
    controls in the verified window receive input. The caller verifies acceptance
    by receiving the encrypted price over its TumarCSP WebSocket.
    """
    if not is_supported() or (stop is not None and stop.is_set()):
        return False
    value = f"{price:.2f}"
    if not _price_matches(value, value):
        log.warning("popup.invalid_price")
        return False
    deadline = time.monotonic() + timeout_s
    api = _Win32()
    previous_windows = set(api.visible_windows())
    if not _can_act(deadline, stop):
        return False
    if ready is not None:
        ready()

    log.info("popup.search_start", timeout_s=timeout_s)
    while _can_act(deadline, stop):
        candidates = []
        for hwnd in api.visible_windows():
            if not _can_act(deadline, stop):
                return False
            if hwnd not in previous_windows:
                dialog = api.select_dialog(hwnd, deadline)
                if dialog is not None:
                    candidates.append(dialog)
        if len(candidates) == 1:
            dialog = candidates[0]
            if _fill_dialog(api, dialog, value, deadline, stop):
                matched_by = ("title", "label", "buttons")[len(dialog.identifying_controls)]
                log.info("popup.win32.filled", matched_by=matched_by)
                return True
            return False
        if len(candidates) > 1:
            log.warning("popup.search.ambiguous")
            return False
        _pause(deadline, stop)

    if stop is None or not stop.is_set():
        log.warning("popup.search.timeout")
    return False


def make_autofiller(
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    field_xy: Sequence[int] | None = None,
    ok_xy: Sequence[int] | None = None,
    field_offset: Sequence[int] | None = None,
    ok_offset: Sequence[int] | None = None,
    detect_xy: Sequence[int] | None = None,
    detect_rgb: Sequence[int] | None = None,
):
    """Create the managed Windows filler using the existing configuration API."""

    def _fill(price: float, stop=None, *, ready: Callable[[], None] | None = None) -> bool:
        return fill_price_popup(price, timeout_s=timeout_s, stop=stop, ready=ready)

    _fill.supports_stop = True
    _fill.requires_confirmation = True
    return _fill
