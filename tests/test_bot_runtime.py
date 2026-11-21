"""Test startup and Telegram polling diagnostics without external services."""

import ctypes
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from telegram.error import Conflict, NetworkError

from goszakup.bot import main
from goszakup.bot.instance_lock import BotAlreadyRunning, _windows_mutex, bot_instance


def test_single_instance_releases_lock_and_allows_another_token(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    token = uuid4().hex
    with bot_instance(token):
        with bot_instance(uuid4().hex):
            with pytest.raises(BotAlreadyRunning), bot_instance(token):
                pytest.fail("duplicate bot entered the guarded code")
    with bot_instance(token):
        pass


def test_windows_duplicate_mutex_closes_the_contenders_handle(monkeypatch):
    kernel = SimpleNamespace(CreateMutexW=Mock(return_value=123), CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", Mock(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", Mock(return_value=183), raising=False)
    with pytest.raises(BotAlreadyRunning), _windows_mutex("test"):
        pytest.fail("duplicate bot entered the guarded code")
    kernel.CreateMutexW.assert_called_once_with(None, False, "Global\\GoszakupBot-test")
    kernel.CloseHandle.assert_called_once_with(123)


def test_duplicate_start_never_initializes_bot(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    token = uuid4().hex
    cfg = SimpleNamespace(telegram=SimpleNamespace(bot_token=token))
    monkeypatch.setattr(main.bot_config, "load", Mock(return_value=cfg))
    setup = Mock()
    monkeypatch.setattr(main, "_setup_logging", setup)
    with bot_instance(token):
        assert main.main() == 1
    setup.assert_not_called()
    assert "уже запущен" in capsys.readouterr().err


async def test_polling_errors_are_classified_and_rate_limited(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(main, "log", logger)
    clock = SimpleNamespace(monotonic=Mock(side_effect=[1, 2, 3, 32]))
    monkeypatch.setattr(main, "time", clock)
    error = NetworkError("raw URL must not appear in logs")
    error.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    ctx = SimpleNamespace(error=error, job=None, coroutine=None,
                          application=SimpleNamespace(bot_data={}))

    await main._telegram_error(None, ctx)
    await main._telegram_error(None, ctx)
    ctx.error = Conflict("terminated by other getUpdates request")
    await main._telegram_error(None, ctx)
    ctx.error = error
    await main._telegram_error(None, ctx)

    assert [(call.args, call.kwargs) for call in logger.warning.call_args_list] == [
        (("telegram.polling_error",), {"error_type": "dns", "count": 1}),
        (("telegram.polling_error",), {"error_type": "other_poller", "count": 1}),
        (("telegram.polling_error",), {"error_type": "dns", "count": 3}),
    ]
