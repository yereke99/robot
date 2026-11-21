"""Cross-platform secret store.

Replaces the macOS-only `security` calls scattered across submit.py and
scripts/find_open_tenders.py. Same priority order as before (Keychain wins
on macOS) so the existing CLI dev box keeps working unchanged.

Lookup order:
    1. macOS Keychain via `security` (if the binary is on PATH)
    2. Environment variable: GOSZAKUP_<SERVICE>_<ACCOUNT> upper-cased,
       non-alpha replaced with `_`
    3. JSON file at $GOSZAKUP_SECRETS_FILE (default ~/.goszakup/secrets.json)
       with mode 0600. Schema: `{"<service>/<account>": "<value>", ...}`

Write order is symmetric: try Keychain first, fall back to file. Env is
read-only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _has_security() -> bool:
    return shutil.which("security") is not None


def _env_key(service: str, account: str) -> str:
    raw = f"GOSZAKUP_{service}_{account}".upper()
    return "".join(c if c.isalnum() else "_" for c in raw)


def _secrets_file() -> Path:
    env = os.environ.get("GOSZAKUP_SECRETS_FILE")
    if env:
        return Path(env).expanduser()
    return Path("~/.goszakup/secrets.json").expanduser()


def _read_file_secret(service: str, account: str) -> Optional[str]:
    p = _secrets_file()
    if not p.is_file():
        return None
    # On POSIX (macOS/Linux) refuse to read group/world-readable secrets —
    # cheap defence against a careless chmod. On Windows the POSIX mode bits
    # don't map to NTFS ACLs (Path.stat().st_mode is always 0o666 regardless
    # of who can actually read), so the check would always fire — skip it and
    # leave file lockdown to NTFS Properties → Security.
    if os.name != "nt":
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            raise RuntimeError(
                f"refusing to read {p}: mode {oct(mode)} is too permissive; "
                f"run `chmod 600 {p}`"
            )
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{p} is not valid JSON: {e}") from e
    val = data.get(f"{service}/{account}")
    return str(val) if val else None


def _write_file_secret(service: str, account: str, value: str) -> None:
    p = _secrets_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        data = json.loads(p.read_text())
    else:
        data = {}
    data[f"{service}/{account}"] = value
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    if os.name != "nt":
        tmp.chmod(0o600)  # no-op on Windows; rely on NTFS ACL
    tmp.replace(p)


def _keychain_get(service: str, account: str) -> Optional[str]:
    if not _has_security():
        return None
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or None
    except subprocess.CalledProcessError:
        return None


def _keychain_set(service: str, account: str, value: str) -> bool:
    if not _has_security():
        return False
    try:
        subprocess.run(
            ["security", "add-generic-password", "-s", service, "-a", account,
             "-w", value, "-U"],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def get(service: str, account: str) -> Optional[str]:
    """Resolve a secret. Returns None if not found in any backend."""
    v = _keychain_get(service, account)
    if v:
        return v
    v = os.environ.get(_env_key(service, account))
    if v:
        return v
    return _read_file_secret(service, account)


def set(service: str, account: str, value: str) -> str:
    """Persist a secret. Returns the backend name used ("keychain" or "file")."""
    if _keychain_set(service, account, value):
        return "keychain"
    _write_file_secret(service, account, value)
    return "file"
