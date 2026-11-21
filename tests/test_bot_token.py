"""_resolve_bot_token must tolerate the common Windows config mix-ups:
token written into config, token pasted into bot_token_env (instead of a name),
or the proper env-var-name indirection.
"""

from __future__ import annotations

import pytest

from goszakup.bot.atagos_bot import _resolve_bot_token

_TOKEN = "8911361869:AAEgFXqAedG3-gS0lzXThiBLhg_XZG3doBM"


def test_literal_bot_token_in_config():
    assert _resolve_bot_token({"telegram": {"bot_token": _TOKEN}}) == _TOKEN


def test_token_pasted_into_bot_token_env_is_used_directly():
    # the exact mistake on the Windows box: token in the *name* field
    cfg = {"telegram": {"bot_token_env": _TOKEN}}
    assert _resolve_bot_token(cfg) == _TOKEN


def test_env_var_name_indirection(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", _TOKEN)
    assert _resolve_bot_token({"telegram": {"bot_token_env": "TG_BOT_TOKEN"}}) == _TOKEN


def test_missing_returns_none(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    assert _resolve_bot_token({"telegram": {"bot_token_env": "TG_BOT_TOKEN"}}) is None


def test_literal_wins_over_env(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "9999:OTHER-token-aaaaaaaaaaaaaaaaaaaa")
    cfg = {"telegram": {"bot_token": _TOKEN, "bot_token_env": "TG_BOT_TOKEN"}}
    assert _resolve_bot_token(cfg) == _TOKEN
