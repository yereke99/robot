"""Bot entry point: `python -m goszakup.bot.main`.

Boots the long-lived KalkanSigner, opens the history DB, registers handlers,
sets the Russian command menu, and starts long-polling.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Cross-platform .env loader. macOS dev box uses Keychain; Linux/Windows
# deployments use a .env file alongside the repo. No-op if not installed
# or no file present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import structlog
from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
)

from .. import secrets as gz_secrets
from ..client import GoszakupClient
from ..local_signer import KalkanSigner
from . import config as bot_config
from . import log_capture
from .context import BotContext
from .handlers import bid as bid_handler
from .handlers import scheduled as scheduled_handler
from .handlers import common
from .history_db import HistoryDB
from .texts import BOT_COMMANDS_RU


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    # Mute PTB's verbose network-retry stack traces — they fire on every
    # transient DNS/connect blip and drown out our own pipeline logs.
    # The Application/Updater logger still reports actual fatal errors.
    for noisy in (
        "telegram.ext._utils.networkloop",
        "telegram.ext._updater",
        "telegram._bot",
        "telegram.request",
        "httpx",
        "httpcore",
    ):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.CRITICAL)
        lg.propagate = False     # don't bubble to root either
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            log_capture.progress_processor,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


async def _post_init(application: Application) -> None:
    ctx: BotContext = application.bot_data["ctx"]

    # Start the long-lived Java signer here so it lives inside PTB's loop.
    # Failing fast on a bad ECP password is intentional — the user will see
    # the stacktrace in `docker compose logs` instead of at first /bid.
    await ctx.signer.start()
    logging.info("kalkan signer started")

    # Opt #4: Long-lived GoszakupClient. Pre-warm TLS by hitting a cheap
    # public endpoint so the first /bid doesn't pay handshake cost.
    ctx.gz_client = GoszakupClient(
        ci_session=ctx.ci_session(),
        agreement_cookie=ctx.agreement_cookie(),
        base_url=ctx.config.base_url,
        proxy=ctx.config.proxy_url,
    )
    try:
        import time as _t
        t0 = _t.monotonic()
        await ctx.gz_client._http.head("/ru/", timeout=8.0)
        logging.info("opt4.gz_client.prewarm ms=%d", int((_t.monotonic() - t0) * 1000))
    except Exception as e:
        logging.warning("opt4.gz_client.prewarm failed: %s", e)

    # Heartbeat: keep the TCP+TLS connection alive against nginx idle
    # timeout (~75s). Skipped while a bid holds the lock so we don't add
    # noise to the hot path. Failures here are pure-optimization losses
    # (the next real request just re-handshakes), so we log at DEBUG.
    import asyncio
    async def _heartbeat():
        while True:
            try:
                await asyncio.sleep(30)
                if ctx.current_bid is not None or ctx.bid_lock.locked():
                    continue
                if ctx.gz_client is None:
                    continue
                try:
                    await ctx.gz_client._http.head("/ru/", timeout=5.0)
                except Exception as e:
                    logging.debug("opt4.gz_client.heartbeat failed: %r", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logging.exception("heartbeat loop error: %s", e)
    application.bot_data["heartbeat_task"] = asyncio.create_task(_heartbeat())

    # Mark stale pending rows from a prior crash.
    n = ctx.history.mark_interrupted_pending()
    if n:
        logging.info("marked %s stale pending bids as interrupted", n)

    # Register Russian command menu for the blue Telegram button.
    await application.bot.set_my_commands(
        [BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS_RU]
    )
    # Загружаем персистентные кэши (lp_id, tz_sig) с диска
    ctx.load_caches()
    logging.info("bot ready: %s whitelist users", len(ctx.config.telegram.allowed_user_ids))


async def _post_shutdown(application: Application) -> None:
    ctx: BotContext = application.bot_data.get("ctx")
    if ctx is None:
        return
    hb = application.bot_data.get("heartbeat_task")
    if hb is not None:
        hb.cancel()
    try:
        if ctx.gz_client is not None:
            await ctx.gz_client.aclose()
    except Exception:
        pass
    try:
        await ctx.signer.close()
    except Exception:
        pass
    try:
        ctx.history.close()
    except Exception:
        pass


def main() -> int:
    config_path = Path(os.environ.get("BOT_CONFIG_PATH", "config.yaml"))
    cfg = bot_config.load(config_path)
    _setup_logging(cfg.log_dir)

    # Resolve ECP password and start the Java signer once for the whole process.
    ecp_pw = gz_secrets.get(cfg.ecp_password_service, cfg.ecp_password_account)
    if not ecp_pw:
        env_hint = (
            "GOSZAKUP_" + cfg.ecp_password_service.upper().replace("-", "_") +
            "_" + cfg.ecp_password_account.upper()
        )
        sys.exit(
            f"ECP password missing. Set env {env_hint} or put it in the secrets "
            f"file at {Path(os.environ.get('GOSZAKUP_SECRETS_FILE', '~/.goszakup/secrets.json')).expanduser()}"
        )
    if not Path(cfg.p12_path).is_file():
        sys.exit(f"ECP .p12 not found at {cfg.p12_path}; mount it or fix ecp.p12_path in config.yaml")

    # NB: signer.start() is awaited inside _post_init, not here — PTB owns the loop.
    signer = KalkanSigner(p12_path=cfg.p12_path, password=ecp_pw)
    history_db = HistoryDB(cfg.history_db_path)

    # Read once at boot — avoids 3-4 subprocess Keychain calls per bid.
    acc_pw = gz_secrets.get(cfg.account_password_service, cfg.account_password_account)
    if not acc_pw:
        logging.warning(
            "goszakup account password not found at boot — /login and "
            "auto-login on bids will fail until it's set",
        )
    ctx = BotContext(
        config=cfg, signer=signer, history=history_db,
        account_password=acc_pw,
    )

    app = (
        Application.builder()
        .token(cfg.telegram.bot_token)
        .defaults(Defaults(parse_mode=None, block=False))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["ctx"] = ctx

    # Minimal command set: bot is single-purpose — paste a tender URL, it bids.
    app.add_handler(CommandHandler("start", common.start_cmd))
    app.add_handler(CommandHandler("help", common.help_cmd))
    app.add_handler(CommandHandler("cancel", common.cancel_cmd))
    app.add_handler(CommandHandler("login", common.login_cmd))
    app.add_handler(scheduled_handler.build_schedule_handler())

    # Bid conversation owns /bid + URL/number text + lot_pick callbacks.
    app.add_handler(bid_handler.build_conversation_handler())

    app.run_polling(close_loop=False, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
