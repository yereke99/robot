"""Bot entry point: `python -m goszakup.bot.main`.

Boots the long-lived KalkanSigner, opens the history DB, registers handlers,
sets the Russian command menu, and starts long-polling.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
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
from telegram.error import Conflict, NetworkError, TelegramError
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
from .handlers import settings as settings_handler
from .history_db import HistoryDB
from .instance_lock import BotAlreadyRunning, bot_instance
from .run_reports import RunReports
from .texts import BOT_COMMANDS_RU

log = structlog.get_logger("bot")


def _setup_logging(log_dir: Path, *, capture_reports: bool = False) -> log_capture.RuntimeLog:
    runtime_log = log_capture.RuntimeLog(log_dir, capture_reports=capture_reports)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    # Report polling retries once through our error handler. HTTP library
    # stack traces can include Telegram URLs with the bot token.
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
            runtime_log.process,
            log_capture.progress_processor,
            log_capture.trace_processor,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return runtime_log


def _telegram_error_type(error: BaseException) -> str:
    if isinstance(error, Conflict):
        return "other_poller" if "other getupdates" in str(error).lower() else "conflict"
    if isinstance(error, NetworkError):
        cause: BaseException | None = error
        for _ in range(8):
            if cause is None:
                break
            if isinstance(cause, socket.gaierror) or "getaddrinfo failed" in str(cause).lower():
                return "dns"
            cause = cause.__cause__ or cause.__context__
    return type(error).__name__


async def _telegram_error(update, context) -> None:
    error = context.error
    error_type = _telegram_error_type(error)
    if (update is not None or context.job is not None or context.coroutine is not None
            or not isinstance(error, (NetworkError, Conflict))):
        log.error("telegram.update_failed", error_type=error_type)
        return

    issues = context.application.bot_data.setdefault("polling_errors", {})
    count, last_logged = issues.get(error_type, (0, float("-inf")))
    now = time.monotonic()
    if now - last_logged >= 30.0:
        log.warning("telegram.polling_error", error_type=error_type, count=count + 1)
        last_logged = now
    issues[error_type] = (count + 1, last_logged)


async def _post_init(application: Application) -> None:
    ctx: BotContext = application.bot_data["ctx"]

    # Start the long-lived Java signer here so it lives inside PTB's loop.
    # Failing fast on a bad ECP password is intentional — the user will see
    # the stacktrace in `docker compose logs` instead of at first /bid.
    started = time.monotonic()
    await ctx.signer.start()
    log.info("bot.signer_ready", elapsed_ms=round((time.monotonic() - started) * 1000))

    # Opt #4: Long-lived GoszakupClient. Pre-warm TLS by hitting a cheap
    # public endpoint so the first /bid doesn't pay handshake cost.
    ctx.gz_client = GoszakupClient(
        ci_session=ctx.ci_session(),
        agreement_cookie=ctx.agreement_cookie(),
        base_url=ctx.config.base_url,
        proxy=ctx.config.proxy_url,
    )
    try:
        t0 = time.monotonic()
        await ctx.gz_client._http.head("/ru/", timeout=8.0)
        log.info("bot.portal_prewarm", elapsed_ms=round((time.monotonic() - t0) * 1000))
    except Exception as e:
        log.warning("bot.portal_prewarm_failed", error_type=type(e).__name__)

    # Heartbeat: keep the TCP+TLS connection alive against nginx idle
    # timeout (~75s). Skipped while a bid holds the lock so we don't add
    # noise to the hot path. Failures here are pure-optimization losses
    # (the next real request just re-handshakes), so we log at DEBUG.
    import asyncio
    async def _heartbeat():
        ticks = 0
        while True:
            try:
                await asyncio.sleep(30)
                if ctx.current_bid is not None or ctx.bid_lock.locked():
                    continue
                if ctx.gz_client is None:
                    continue
                ticks += 1
                try:
                    await ctx.gz_client._http.head("/ru/", timeout=5.0)
                except Exception as e:
                    logging.debug("opt4.gz_client.heartbeat failed: %r", e)

                # Каждые 3-4 минуты отправляем запрос в кабинет для продления жизни ci_session
                if ticks % 6 == 0 and ctx.ci_session():
                    try:
                        r = await ctx.gz_client._http.get("/cabinet/profile", timeout=8.0, follow_redirects=False)
                        if r.status_code == 200:
                            logging.debug("session keepalive: ci_session active")
                        elif r.status_code in (301, 302) and "/user/login" in (r.headers.get("location") or ""):
                            logging.warning("session keepalive: cookie expired on portal")
                    except Exception as e:
                        logging.debug("session keepalive error: %r", e)
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
    try:
        await application.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS_RU]
        )
    except NetworkError as e:
        log.warning("telegram.command_menu_failed", error_type=_telegram_error_type(e))
    # Загружаем персистентные кэши (lp_id, tz_sig) с диска
    ctx.load_caches()
    defaults = ctx.config.raw.get("defaults", {})
    if (defaults.get("pre_t0_warmup", True)
            and defaults.get("pipeline_type", "catering") == "repairs"):
        # Reuse the existing connection path before the first /bid, so its
        # SetAPIKey/GetVersion exchange is already complete when bidding starts.
        try:
            await asyncio.wait_for(
                bid_handler._ensure_tumar(ctx, ctx.config.raw.get("tumarcsp", {})),
                timeout=15.0,
            )
        except Exception as e:
            logging.warning("startup TumarCSP warmup failed: %r", e)
    try:
        from ..popup_filler import is_elevated
        if is_elevated():
            logging.info("popup.autofill: running as Administrator — OK")
        else:
            logging.warning(
                "popup.autofill: NOT elevated — Tumar price popup will NOT "
                "auto-fill (UIPI). Relaunch start-bot.bat as Administrator."
            )
    except Exception:
        pass
    log.info("bot.ready", count=len(ctx.config.telegram.allowed_user_ids))
    reports = getattr(ctx, "run_reports", None)
    if reports is not None:
        reports.start(application)


async def _post_stop(application: Application) -> None:
    reports = application.bot_data["ctx"].run_reports
    if reports is not None:
        await reports.stop()


async def _post_shutdown(application: Application) -> None:
    ctx: BotContext = application.bot_data.get("ctx")
    if ctx is None:
        return
    if ctx.run_reports is not None:
        await ctx.run_reports.cancel()
    hb = application.bot_data.get("heartbeat_task")
    if hb is not None:
        hb.cancel()
    try:
        if ctx.gz_client is not None:
            await ctx.gz_client.aclose()
    except Exception:
        pass
    try:
        if getattr(ctx, "tumar", None) is not None:
            await ctx.tumar.close()
            ctx.tumar = None
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
    try:
        with bot_instance(cfg.telegram.bot_token):
            managed = os.environ.get("GOSZAKUP_MANAGED_START") == "1"
            chat_id = cfg.telegram.report_chat_id
            runtime_log = _setup_logging(cfg.log_dir, capture_reports=managed)
            try:
                reports = None
                if managed:
                    reports = RunReports(
                        runtime_log, cfg.log_dir, chat_id,
                        repo=Path(__file__).resolve().parents[2],
                        revision=os.environ.get("GOSZAKUP_RUN_REVISION", ""),
                    )
                    if chat_id is None:
                        log.warning("bot.run_report_chat_missing")
                log.info("bot.starting", pid=os.getpid(), platform=sys.platform,
                         python=sys.version.split()[0],
                         pipeline_type=cfg.raw.get("defaults", {}).get("pipeline_type", "catering"))
                return _run_bot(cfg, reports)
            except TelegramError as e:
                log.error("telegram.startup_failed", error_type=_telegram_error_type(e))
                return 1
            finally:
                runtime_log.close()
    except BotAlreadyRunning as e:
        print(e, file=sys.stderr)
        return 1


def _run_bot(cfg: bot_config.BotConfig, reports: RunReports | None = None) -> int:

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
        account_password=acc_pw, run_reports=reports,
    )
    # Seed runtime bid settings (guarantee type, confirm-before-submit, price
    # floor, dry-run) from config.yaml defaults; editable live via /config.
    ctx.bid_settings = settings_handler.seed_settings(cfg.raw)

    app = (
        Application.builder()
        .token(cfg.telegram.bot_token)
        .defaults(Defaults(parse_mode=None, block=False))
        .post_init(_post_init)
        .post_stop(_post_stop)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["ctx"] = ctx
    app.add_error_handler(_telegram_error)

    # Minimal command set: bot is single-purpose — paste a tender URL, it bids.
    app.add_handler(CommandHandler("start", common.start_cmd))
    app.add_handler(CommandHandler("help", common.help_cmd))
    app.add_handler(CommandHandler("cancel", common.cancel_cmd))
    app.add_handler(CommandHandler("login", common.login_cmd))
    app.add_handler(CommandHandler("cookie", common.cookie_cmd))
    app.add_handler(CommandHandler("codes", common.codes_cmd))
    app.add_handler(CommandHandler("config", settings_handler.config_cmd))
    app.add_handler(CommandHandler("timing", bid_handler.timing_cmd))
    app.add_handler(CallbackQueryHandler(
        settings_handler.confirm_cb, pattern=r"^submit(ok|no):"))
    app.add_handler(scheduled_handler.build_schedule_handler())

    # Bid conversation owns /bid + URL/number text + lot_pick callbacks.
    app.add_handler(bid_handler.build_conversation_handler())

    app.run_polling(close_loop=False, drop_pending_updates=True, bootstrap_retries=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
