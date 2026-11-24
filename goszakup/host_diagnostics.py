"""Process-wide host details safe for bid reports."""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache

_CHROME_CLIENT_KEY = (
    r"Software\Google\Update\Clients\{8A69D345-D564-463C-AFF1-A69D9E530F96}"
)
_CHROME_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome"
_CHROME_VERSION = re.compile(r"[1-9][0-9]{0,5}(?:\.[0-9]{1,6}){3}")
_WINDOWS_ARCHITECTURES = {"AMD64": "x64", "X86": "x86", "ARM64": "ARM64", "ARM": "ARM"}


def _installed_chrome_version() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    # Chrome's version registered with Google Updater uses the 32-bit view,
    # even for the 64-bit browser. The uninstaller can use either view.
    registrations = (
        (_CHROME_CLIENT_KEY, "pv", winreg.KEY_WOW64_32KEY),
        (_CHROME_UNINSTALL_KEY, "DisplayVersion", winreg.KEY_WOW64_64KEY),
        (_CHROME_UNINSTALL_KEY, "DisplayVersion", winreg.KEY_WOW64_32KEY),
    )
    for path, value_name, view in registrations:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_QUERY_VALUE | view) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
            except OSError:
                continue
            if isinstance(value, str) and _CHROME_VERSION.fullmatch(value):
                return value
    return None


@lru_cache(maxsize=1)
def runtime_summary() -> str:
    """Describe software versions and CPU capacity without host identity."""
    if sys.platform == "win32":
        build = sys.getwindowsversion().platform_version[2]
        system = f"Windows, сборка {build}"
        machine = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")
        architecture = _WINDOWS_ARCHITECTURES.get(machine.upper())
    else:
        system = {"darwin": "macOS", "linux": "Linux"}.get(sys.platform, "ОС не определена")
        architecture = None

    python_version = ".".join(str(part) for part in sys.version_info[:3])
    logical_cpus = os.cpu_count()
    chrome_version = _installed_chrome_version()
    parts = [system]
    if architecture:
        parts.append(architecture)
    parts.extend((
        f"Python {python_version}",
        f"Логических процессоров {logical_cpus}" if logical_cpus else "Число процессоров недоступно",
        f"Chrome {chrome_version}" if chrome_version else "Версия Chrome недоступна",
    ))
    return " · ".join(parts)
