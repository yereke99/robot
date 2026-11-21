"""Bot-level shared state.

One instance lives in `application.bot_data["ctx"]` for the whole process
lifetime. Holds the long-lived KalkanSigner, the in-flight bid lock, the
history DB connection, and search cache.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

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
