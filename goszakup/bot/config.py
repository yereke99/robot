"""Bot config: load telegram + search sections from config.yaml.

Reuses the existing project config.yaml so the bot and CLI share BIN,
ECP paths, base URL, etc.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    allowed_user_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    report_chat_id: int | None = None


@dataclass(frozen=True)
class SearchConfig:
    # Server-side filters that match the goszakup web UI URL params.
    statuses: tuple[int, ...]       # filter[status][]: 210 (приём), 220 (опубликовано)
    methods: tuple[int, ...]        # filter[method][]: 120 = питание воспитанников
    default_max_sum: int
    keywords: tuple[str, ...]       # optional soft client-side narrower


@dataclass(frozen=True)
class BotConfig:
    raw: dict
    telegram: TelegramConfig
    search: SearchConfig
    base_url: str
    bin: str
    state_dir: Path
    log_dir: Path
    history_db_path: Path
    p12_path: str
    ecp_password_service: str
    ecp_password_account: str
    ci_session_service: str
    account_password_service: str
    account_password_account: str
    proxy_url: Optional[str]    # for residential proxy on cloud-hosted bots


def _resolve_token(raw_telegram: dict) -> str:
    env_name = raw_telegram.get("bot_token_env", "TG_BOT_TOKEN")
    token = os.environ.get(env_name)
    if not token:
        # Allow inline token only for local dev.
        token = raw_telegram.get("bot_token")
    if not token:
        raise RuntimeError(
            f"Telegram bot token missing. Set env {env_name} or "
            f"telegram.bot_token in config.yaml."
        )
    return token


def _report_chat_id(raw_telegram: dict) -> int | None:
    override = os.environ.get("GOSZAKUP_REPORT_CHAT_ID", "").strip()
    if override:
        if not re.fullmatch(r"[1-9][0-9]{0,15}", override):
            raise ValueError("GOSZAKUP_REPORT_CHAT_ID must be a personal Telegram chat ID")
        return int(override)
    configured = raw_telegram.get("report_chat_ids") or []
    if isinstance(configured, list) and len(configured) == 1 and re.fullmatch(
        r"[1-9][0-9]{0,15}", str(configured[0])
    ):
        return int(configured[0])
    return None


def load(config_path: Path = Path("config.yaml")) -> BotConfig:
    if not config_path.exists():
        raise RuntimeError(
            f"Не найден файл настроек {config_path.resolve()}. "
            "Перенеси рабочий config.yaml в папку проекта или задай BOT_CONFIG_PATH."
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    tg = raw.get("telegram") or {}
    allowed = frozenset(int(x) for x in (tg.get("allowed_user_ids") or []))
    admins = frozenset(int(x) for x in (tg.get("admin_user_ids") or [])) or allowed
    if not allowed:
        raise RuntimeError("telegram.allowed_user_ids is empty — bot would let nobody in")

    search = raw.get("search") or {}
    statuses = tuple(int(x) for x in (search.get("statuses") or [210, 220]))
    methods = tuple(int(x) for x in (search.get("methods") or []))
    keywords = tuple(str(k).lower() for k in (search.get("keywords") or []))
    default_max_sum = int(search.get("default_max_sum") or 0)

    state_dir = Path(os.environ.get("GOSZAKUP_STATE_DIR")
                     or raw.get("defaults", {}).get("state_dir")
                     or "~/.goszakup/state").expanduser()
    log_dir = Path(os.environ.get("GOSZAKUP_LOG_DIR")
                   or raw.get("defaults", {}).get("log_dir")
                   or "~/.goszakup/logs").expanduser()
    history_db_path = Path(os.environ.get("GOSZAKUP_HISTORY_DB")
                           or (state_dir.parent / "bot_history.db"))

    # Proxy URL precedence: env > config > none.
    proxy_url = (
        os.environ.get("GOSZAKUP_PROXY_URL")
        or raw.get("goszakup", {}).get("proxy_url")
        or None
    )

    return BotConfig(
        raw=raw,
        telegram=TelegramConfig(
            bot_token=_resolve_token(tg),
            allowed_user_ids=allowed,
            admin_user_ids=admins,
            report_chat_id=_report_chat_id(tg),
        ),
        search=SearchConfig(
            statuses=statuses,
            methods=methods,
            keywords=keywords,
            default_max_sum=default_max_sum,
        ),
        base_url=raw["goszakup"]["base_url"],
        bin=raw["company"].get("bin") or raw["company"]["iin"],
        state_dir=state_dir,
        log_dir=log_dir,
        history_db_path=history_db_path,
        p12_path=str(Path(raw["ecp"]["p12_path"]).expanduser()),
        ecp_password_service=raw["ecp"].get("password_keyring_service", "goszakup-ecp"),
        ecp_password_account=raw["ecp"].get("password_keyring_user", "default"),
        ci_session_service=raw["goszakup"].get("ci_session_service", "goszakup-cookie"),
        account_password_service=raw["account"].get("password_keyring_service", "goszakup-acc"),
        account_password_account=raw["account"].get("password_keyring_user", "default"),
        proxy_url=proxy_url,
    )
