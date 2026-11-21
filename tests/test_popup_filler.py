"""Test the Windows price dialog policy without a bidder or a Windows session."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from goszakup import popup_filler as popup


class FakeClock:
    now = 0.0
    stopped = False

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    wait = sleep

    def is_set(self):
        return self.stopped


class FakeWindows(popup._Win32):
    def __init__(self):
        self.windows = {}
        self.owner_images = {1: "TumarCSP.exe", 2: "Browser.exe", 3: "CryptoSocket.exe"}
        self.actions = []
        self.readback = None
        self.set_response = lambda value: value
        self.on_value_read = None
        self.user = SimpleNamespace(
            GetWindowLongW=lambda hwnd, _: self.windows[hwnd].style,
            IsWindowEnabled=lambda hwnd: self.windows[hwnd].enabled,
            IsWindowVisible=lambda hwnd: hwnd in self.windows,
            IsChild=lambda hwnd, child: hwnd in self.windows and child in self.visible_windows(hwnd),
            GetParent=lambda hwnd: self.windows[hwnd].parent,
            GetDlgCtrlID=lambda hwnd: self.windows[hwnd].control_id,
            PostMessageW=self.post,
        )

    def add_dialog(self, hwnd, *, pid=1, title="Заполнение ценового предложения", price_label=None):
        self.windows[hwnd] = SimpleNamespace(
            title=title, parent=None, pid=pid, enabled=True,
        )
        self.windows[hwnd + 1] = SimpleNamespace(
            parent=hwnd, pid=pid, enabled=True, style=0, kind="edit",
            text="", control_id=2,
        )
        self.add_button(hwnd + 2, hwnd, "&ОК")
        if price_label is not None:
            self.windows[hwnd + 3] = SimpleNamespace(
                parent=hwnd, pid=pid, kind="windowsforms10.static.app.0.123", text=price_label,
            )

    def add_button(self, hwnd, parent, text, *, style=1):
        self.windows[hwnd] = SimpleNamespace(
            parent=parent, pid=self.windows[parent].pid, enabled=True, style=style, kind="button",
            text=text, control_id=3,
        )

    def visible_windows(self, parent=None):
        return [hwnd for hwnd, window in self.windows.items() if window.parent == parent]

    def window_text(self, hwnd):
        return self.windows[hwnd].title

    def pid(self, hwnd):
        return self.windows[hwnd].pid if hwnd in self.windows else 0

    def is_provider(self, pid):
        return bool(popup._PROVIDER_IMAGES.fullmatch(self.owner_images[pid]))

    def _class_name(self, hwnd):
        return self.windows[hwnd].kind

    def _send(self, hwnd, message, arg, buffer, _deadline):
        window = self.windows[hwnd]
        if message == popup._WM_SETTEXT:
            self.actions.append(("write", hwnd, buffer.value))
            window.text = self.set_response(buffer.value)
            return 1
        if message == popup._EM_SETSEL:
            assert (arg, buffer) == (0, -1)
            self.actions.append(("select", hwnd))
            window.selected = True
            return 0
        if message == popup._WM_CHAR:
            character = chr(arg)
            self.actions.append(("char", hwnd, character))
            previous = "" if window.selected else window.text
            window.text = previous + character
            window.selected = False
            return 1
        assert message == popup._WM_GETTEXT
        buffer.value = self.readback if window.kind == "edit" and self.readback is not None else window.text
        if window.kind == "edit" and self.on_value_read is not None:
            self.on_value_read()
        return len(buffer.value)

    def post(self, hwnd, message, control_id, button):
        self.actions.append(("confirm", hwnd, message, button))
        if message == popup._WM_COMMAND:
            assert control_id == self.windows[button].control_id
        else:
            assert (message, control_id, button) == (popup._BM_CLICK, 0, 0)
        return True


@pytest.fixture
def desktop(monkeypatch):
    api = FakeWindows()
    clock = FakeClock()
    monkeypatch.setattr(popup, "is_supported", lambda: True)
    monkeypatch.setattr(popup, "_Win32", lambda: api)
    monkeypatch.setattr(popup, "time", clock)
    return api, clock


@pytest.mark.parametrize("readback", ["80.00", "80,0", "80"])
def test_fills_only_the_new_verified_provider_window(desktop, readback):
    api, clock = desktop
    api.add_dialog(10)  # Popup from an earlier operation is already visible.
    api.readback = readback

    def open_new_windows():
        api.add_dialog(20, pid=2)  # Browser with a matching title and controls.
        api.add_dialog(30, title="TumarCSP settings")
        api.add_dialog(40)

    assert popup.fill_price_popup(80.0, stop=clock, ready=open_new_windows)
    assert api.actions == [
        ("write", 41, "80.00"),
        ("confirm", 40, popup._WM_COMMAND, 42),
    ]


@pytest.mark.parametrize(("title", "label"), [
    ("", "Заполнение ценового предложения."),
    ("CryptoSocket", "Укажите цену\r\nза единицу:"),
])
def test_fills_a_new_borderless_provider_dialog_by_its_visible_label(desktop, title, label):
    api, clock = desktop
    api.add_dialog(10, title=title, pid=3, price_label=label)

    def open_new_windows():
        api.add_dialog(20, title=title, pid=2, price_label=label)
        api.add_dialog(30, title=title, pid=3, price_label="Настройки цены")
        api.add_dialog(40, title=title, pid=3, price_label=label)

    assert popup.fill_price_popup(80.0, stop=clock, ready=open_new_windows)
    assert api.actions == [
        ("write", 41, "80.00"),
        ("confirm", 40, popup._WM_COMMAND, 42),
    ]


def test_borderless_dialog_never_confirms_if_its_label_changes(desktop):
    api, clock = desktop
    api.on_value_read = lambda: setattr(api.windows[13], "text", "Сохранение настроек.")

    assert not popup.fill_price_popup(
        80.0, stop=clock, ready=lambda: api.add_dialog(10, title="", price_label="Укажите цену за единицу"),
    )
    assert api.actions == [("write", 11, "80.00")]


@pytest.mark.parametrize(("labels", "matched"), [
    (("Сохранить в JPEG", "Отмена"), True),
    (("Сохранить в JPEG",), False),
    (("Отмена",), False),
    (("Сохранить в JPEG", "Отмена", "Отмена"), False),
])
def test_borderless_dialog_requires_the_exact_pair_of_additional_buttons(desktop, labels, matched):
    api, clock = desktop

    def open_dialog():
        api.add_dialog(10, title="CryptoSocket", pid=3)
        for hwnd, label in enumerate(labels, 13):
            api.add_button(hwnd, 10, label, style=0x0B)

    assert popup.fill_price_popup(80.0, timeout_s=0.2, stop=clock, ready=open_dialog) is matched
    assert api.actions == ([
        ("write", 11, "80.00"),
        ("confirm", 10, popup._WM_COMMAND, 12),
    ] if matched else [])


def test_borderless_dialog_never_confirms_if_an_identifying_button_changes(desktop):
    api, clock = desktop
    api.on_value_read = lambda: setattr(api.windows[13], "text", "Сохранить")

    def open_dialog():
        api.add_dialog(10, title="", pid=3)
        api.add_button(13, 10, "Сохранить в JPEG")
        api.add_button(14, 10, "Отмена")

    assert not popup.fill_price_popup(80.0, stop=clock, ready=open_dialog)
    assert api.actions == [("write", 11, "80.00")]


@pytest.mark.parametrize("readback", ["180.00", "80.01", "80.00 KZT", ""])
def test_wrong_or_incomplete_readback_never_confirms(desktop, readback):
    api, clock = desktop
    api.readback = readback
    assert not popup.fill_price_popup(
        80.0, timeout_s=0.2, stop=clock, ready=lambda: api.add_dialog(10),
    )
    assert api.actions[:2] == [("write", 11, "80.00"), ("write", 11, "80,00")]
    assert {action[1] for action in api.actions} == {11}
    assert all(action[0] != "confirm" for action in api.actions)


def test_localized_settext_when_dot_readback_mismatches(desktop):
    api, clock = desktop
    api.set_response = lambda value: value if "," in value else ""
    assert popup.fill_price_popup(80.0, stop=clock, ready=lambda: api.add_dialog(10))
    assert api.actions == [
        ("write", 11, "80.00"), ("write", 11, "80,00"),
        ("confirm", 10, popup._WM_COMMAND, 12),
    ]


def test_targeted_character_entry_when_settext_readback_mismatches(desktop):
    api, clock = desktop
    api.set_response = lambda _: "180.00"
    assert popup.fill_price_popup(80.0, stop=clock, ready=lambda: api.add_dialog(10))
    assert api.actions == [
        ("write", 11, "80.00"), ("write", 11, "80,00"), ("select", 11),
        *(("char", 11, character) for character in "80.00"),
        ("confirm", 10, popup._WM_COMMAND, 12),
    ]


def test_character_entry_can_enable_button_after_an_exact_settext(desktop, monkeypatch):
    api, clock = desktop
    monkeypatch.setattr(api, "ready_to_confirm", lambda _: ("char", 11, "0") in api.actions)
    assert popup.fill_price_popup(80.0, stop=clock, ready=lambda: api.add_dialog(10))
    assert api.actions[:3] == [("write", 11, "80.00"), ("write", 11, "80,00"), ("select", 11)]
    assert api.actions[-1] == ("confirm", 10, popup._WM_COMMAND, 12)


def test_oversized_control_id_uses_one_bm_click_on_the_verified_button(desktop):
    api, clock = desktop

    def open_dialog():
        api.add_dialog(10)
        api.windows[12].control_id = 0x10001

    assert popup.fill_price_popup(80.0, stop=clock, ready=open_dialog)
    assert api.actions == [("write", 11, "80.00"), ("confirm", 12, popup._BM_CLICK, 0)]


def test_second_read_detects_an_edit_race_before_confirmation(desktop):
    api, clock = desktop

    def update_value_after_first_read():
        api.windows[11].text = "81.00"

    api.on_value_read = update_value_after_first_read
    assert not popup.fill_price_popup(80.0, stop=clock, ready=lambda: api.add_dialog(10))
    assert api.actions == [("write", 11, "80.00")]


def test_stop_during_readback_prevents_confirmation(desktop):
    api, clock = desktop
    api.on_value_read = lambda: setattr(clock, "stopped", True)
    assert not popup.fill_price_popup(80.0, stop=clock, ready=lambda: api.add_dialog(10))
    assert api.actions == [("write", 11, "80.00")]


def test_already_stopped_does_not_signal_readiness(desktop):
    api, clock = desktop
    clock.stopped = True
    ready = Mock()
    assert not popup.fill_price_popup(80.0, stop=clock, ready=ready)
    ready.assert_not_called()
    assert api.actions == []


def test_two_candidate_dialogs_do_not_receive_the_price(desktop):
    api, clock = desktop

    def open_both():
        api.add_dialog(10)
        api.add_dialog(20)

    assert not popup.fill_price_popup(80.0, stop=clock, ready=open_both)
    assert api.actions == []


def test_legacy_config_coordinates_cannot_click_an_unverified_window(desktop):
    api, clock = desktop
    filler = popup.make_autofiller(timeout_s=0.2, field_xy=[1, 2], ok_xy=[3, 4])
    assert filler.supports_stop and filler.requires_confirmation
    assert not filler(80.0, clock, ready=lambda: api.add_dialog(10, pid=2))
    assert api.actions == []
    assert clock.now == pytest.approx(0.2)
