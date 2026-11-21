"""Keep one polling bot per token on the same Windows host or Unix account."""

from __future__ import annotations

import hashlib
import os
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path


class BotAlreadyRunning(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Бот с этим токеном уже запущен на этом компьютере.")


@contextmanager
def _windows_mutex(name: str):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = create(None, False, f"Global\\GoszakupBot-{name}")
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:  # ERROR_ALREADY_EXISTS
            raise BotAlreadyRunning()
        yield
    finally:
        close(handle)


@contextmanager
def _unix_lock(name: str):
    import fcntl

    directory = Path.home() / ".goszakup" / "locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep the inode for the next process. The OS releases the lock on close.
    with (directory / f"{name}.lock").open("ab") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BotAlreadyRunning() from None
        yield


def bot_instance(token: str) -> AbstractContextManager[None]:
    name = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return _windows_mutex(name) if os.name == "nt" else _unix_lock(name)
