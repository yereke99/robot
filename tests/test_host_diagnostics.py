from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from goszakup import host_diagnostics


def _mock_registry(monkeypatch, values):
    def open_key(hive, path, reserved, access):
        key = (hive, path, access)
        if key not in values:
            raise FileNotFoundError()
        return nullcontext(key)

    registry = SimpleNamespace(
        HKEY_CURRENT_USER="user",
        HKEY_LOCAL_MACHINE="machine",
        KEY_WOW64_32KEY=0x0200,
        KEY_WOW64_64KEY=0x0100,
        KEY_QUERY_VALUE=0x0001,
        OpenKey=Mock(side_effect=open_key),
        QueryValueEx=Mock(side_effect=lambda key, _name: (values[key], 1)),
    )
    monkeypatch.setattr(host_diagnostics.sys, "platform", "win32")
    monkeypatch.setitem(host_diagnostics.sys.modules, "winreg", registry)
    return registry


def test_chrome_registry_reads_64_bit_install_from_updater_32_bit_view(monkeypatch):
    registry = _mock_registry(monkeypatch, {
        ("machine", host_diagnostics._CHROME_CLIENT_KEY, 0x0201): "144.0.7559.96",
    })

    assert host_diagnostics._installed_chrome_version() == "144.0.7559.96"
    assert registry.OpenKey.call_count == 2
    registry.QueryValueEx.assert_called_once_with(
        ("machine", host_diagnostics._CHROME_CLIENT_KEY, 0x0201), "pv"
    )


def test_chrome_registry_rejects_arbitrary_text_and_uses_uninstall_version(monkeypatch):
    registry = _mock_registry(monkeypatch, {
        ("user", host_diagnostics._CHROME_CLIENT_KEY, 0x0201): "144.0.7559.96\nprivate-host",
        ("machine", host_diagnostics._CHROME_UNINSTALL_KEY, 0x0101): "143.0.7499.192",
    })

    assert host_diagnostics._installed_chrome_version() == "143.0.7499.192"
    registry.QueryValueEx.assert_called_with(
        ("machine", host_diagnostics._CHROME_UNINSTALL_KEY, 0x0101), "DisplayVersion"
    )


def test_missing_chrome_registry_does_not_claim_browser_version(monkeypatch):
    _mock_registry(monkeypatch, {})
    assert host_diagnostics._installed_chrome_version() is None


def test_runtime_summary_reports_windows_build_and_caches_chrome_lookup(monkeypatch):
    monkeypatch.setattr(host_diagnostics.sys, "platform", "win32")
    windows_version = SimpleNamespace(platform_version=(10, 0, 26100))
    monkeypatch.setattr(host_diagnostics.sys, "getwindowsversion", lambda: windows_version, raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "x86")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "AMD64")
    monkeypatch.setattr(host_diagnostics.os, "cpu_count", lambda: 12)
    chrome = Mock(return_value="144.0.7559.96")
    monkeypatch.setattr(host_diagnostics, "_installed_chrome_version", chrome)

    host_diagnostics.runtime_summary.cache_clear()
    try:
        summary = host_diagnostics.runtime_summary()
        assert summary == host_diagnostics.runtime_summary()
        assert summary.startswith("Windows, сборка 26100 · x64 · Python ")
        assert summary.endswith("Логических процессоров 12 · Chrome 144.0.7559.96")
        chrome.assert_called_once_with()
    finally:
        host_diagnostics.runtime_summary.cache_clear()
