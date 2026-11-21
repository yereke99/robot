"""Bot-level shared state.

One instance lives in `application.bot_data["ctx"]` for the whole process
lifetime. Holds the long-lived KalkanSigner, the in-flight bid lock, the
history DB connection, and search cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_cache_log = logging.getLogger("ctx.cache")

from ..client import GoszakupClient
from ..local_signer import KalkanSigner
from .config import BotConfig
from .history_db import HistoryDB


@dataclass
class CachedSearch:
    tenders: list[dict]
    fetched_at: float
    keywords: tuple[str, ...]
    max_sum: int


@dataclass
class BidQueueItem:
    user_id: int
    chat_id: int
    announce_id: str
    started_at: float


@dataclass
class BotContext:
    config: BotConfig
    signer: KalkanSigner               # started in main(); shared by all bids
    history: HistoryDB
    # Static secrets read once at boot — they don't change at runtime.
    # macOS Keychain `security` subprocess calls are ~300-700ms each;
    # reading them on every bid added ~2s of wall-clock latency.
    account_password: Optional[str] = None
    # Opt #4: single long-lived GoszakupClient for the entire bot lifetime.
    # Pre-warmed at startup so the bid hot-path skips TLS+HTTP/2 handshake.
    # Single-tenant invariant guarantees the cookie jar isn't shared between
    # different users. Heartbeat task keeps the TCP alive against nginx idle
    # timeout. Borrowed by all handlers + submit_bid (never aclosed mid-life).
    gz_client: Optional[GoszakupClient] = None
    bid_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_bid: Optional[BidQueueItem] = None
    queue_waiters: int = 0             # count of users waiting on bid_lock
    started_at: float = field(default_factory=time.time)
    last_login_at: Optional[float] = None
    last_cookie_probe: Optional[tuple[float, bool]] = None  # (timestamp, ok?)
    search_cache: dict[int, CachedSearch] = field(default_factory=dict)  # user_id -> result
    # Mutable cookie cache: refreshed by /login and by the auto-login inside
    # submit_bid. ci_session() prefers this over re-reading from disk.
    _ci_session: Optional[str] = None
    _agreement_cookie: Optional[str] = None

    # ТЗ контент-кэш (hash → sig): если тот же контент — пропускаем Kalkan signing.
    # Ключ: sha256_hex, значение: gost_sig_b64
    tz_sig_cache: dict = field(default_factory=dict)

    # lp_id кэш: announce_id -> [lp_id, ...] (из прошлых ставок на тот же тендер)
    # lp_id — это ID лотовой точки (tender-specific, не меняется между ставками).
    # Зная его заранее, можно запустить encr_info во время docs phase → -2.5с.
    lp_id_cache: dict[str, list[int]] = field(default_factory=dict)

    # doc_pool кэш: переиспользуем залогиненные сессии между ставками.
    # Сессии живут ~5мин; обновляем если старше SESSION_POOL_TTL_S.
    _doc_pool: list = field(default_factory=list)          # [(GoszakupClient, LocalSigner)]
    _doc_pool_created_at: float = 0.0
    _doc_pool_announce: str = ""                           # для какого announce_id пул создан

    SESSION_POOL_TTL_S: float = 240.0  # 4 минуты (сессии живут ~5мин)

    def get_doc_pool_if_fresh(self, announce_id: str) -> list:
        """Вернуть пул если свежий и для того же тендера."""
        age = time.time() - self._doc_pool_created_at
        if (self._doc_pool
                and age < self.SESSION_POOL_TTL_S
                and self._doc_pool_announce == announce_id):
            return list(self._doc_pool)
        return []

    def save_doc_pool(self, pool: list, announce_id: str) -> None:
        """Сохранить пул после успешной ставки для повторного использования."""
        self._doc_pool = list(pool)
        self._doc_pool_created_at = time.time()
        self._doc_pool_announce = announce_id

    def invalidate_doc_pool(self) -> None:
        """Сбросить пул (при ошибке или смене тендера)."""
        self._doc_pool = []
        self._doc_pool_created_at = 0.0
        self._doc_pool_announce = ""

    # ─── Персистентные кэши ───────────────────────────────────────────────────

    def _cache_file(self) -> Path:
        p = self.config.state_dir / "bot_cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def save_caches(self) -> None:
        """Сохранить кэши на диск."""
        try:
            data = {
                "lp_id_cache": self.lp_id_cache,
                "tz_sig_cache": self.tz_sig_cache,
            }
            p = self._cache_file()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(p)
            _cache_log.info("cache saved: lp_ids=%d tz_sigs=%d",
                            len(self.lp_id_cache), len(self.tz_sig_cache))
        except Exception as e:
            _cache_log.warning("cache save failed: %s", e)

    def load_caches(self) -> None:
        """Загрузить кэши с диска при старте бота."""
        try:
            p = self._cache_file()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            self.lp_id_cache.update(data.get("lp_id_cache") or {})
            self.tz_sig_cache.update(data.get("tz_sig_cache") or {})
            _cache_log.info("cache loaded: lp_ids=%d tz_sigs=%d",
                            len(self.lp_id_cache), len(self.tz_sig_cache))
        except Exception as e:
            _cache_log.warning("cache load failed: %s", e)

    def ci_session(self) -> Optional[str]:
        if self._ci_session:
            return self._ci_session
        from .. import secrets
        self._ci_session = secrets.get(self.config.ci_session_service, "ci_session")
        return self._ci_session

    def agreement_cookie(self) -> Optional[str]:
        if self._agreement_cookie:
            return self._agreement_cookie
        from .. import secrets
        self._agreement_cookie = secrets.get(
            self.config.ci_session_service, "agreement_user_2024",
        )
        return self._agreement_cookie

    def set_ci_session(self, value: str) -> None:
        """Called after a successful login to update the in-memory cookie."""
        self._ci_session = value
