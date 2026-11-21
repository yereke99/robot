#!/usr/bin/env python3
"""CLI entry point for fast goszakup submission.

Usage:
    submit.py bid --announce 16941324 --lots 41670316 --price 1000000
    submit.py bid ... --dry-run         # everything except final submit
    submit.py bid ... --resume          # pick up from last saved step

Cookies:
    --ci-session $CI_SESSION   or  ci_session in env

Config:
    --config ~/.goszakup/config.yaml   (defaults to ./config.yaml)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path

import structlog
import yaml

from goszakup import secrets as gz_secrets
from goszakup.pipeline import BidParams, submit_bid
from goszakup.state import StateStore


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"config not found: {path}; copy config.example.yaml")
    return yaml.safe_load(path.read_text())


def resolve_ci_session(args, config: dict, *, required: bool = True) -> str | None:
    arg_val = getattr(args, "ci_session", None)
    if arg_val:
        return arg_val
    env = os.environ.get("CI_SESSION")
    if env:
        return env
    svc = config.get("goszakup", {}).get("ci_session_service", "goszakup-cookie")
    v = gz_secrets.get(svc, "ci_session")
    if v:
        return v
    if not required:
        return None
    sys.exit(
        "ci_session cookie required. Provide via --ci-session, $CI_SESSION,\n"
        "or store in keychain/secrets file (goszakup-cookie/ci_session). To\n"
        "capture: log into v3bl.goszakup.gov.kz in Chrome, open DevTools →\n"
        "Application → Cookies → copy the value of ci_session."
    )


def resolve_agreement(config: dict) -> str | None:
    svc = config.get("goszakup", {}).get("ci_session_service", "goszakup-cookie")
    return gz_secrets.get(svc, "agreement_user_2024") or os.environ.get("AGREEMENT_USER_2024")


def resolve_ecp_password(args, config: dict) -> str:
    """Find the .p12 password. Priority: --ecp-password > env > keychain/file > prompt.

    Prompted passwords get stored via gz_secrets.set so subsequent runs are
    fully unattended.
    """
    if getattr(args, "ecp_password", None):
        return args.ecp_password
    env = os.environ.get("ECP_PASSWORD")
    if env:
        return env
    svc = config.get("ecp", {}).get("password_keyring_service", "goszakup-ecp")
    user = config.get("ecp", {}).get("password_keyring_user", "default")
    v = gz_secrets.get(svc, user)
    if v:
        return v
    pw = getpass.getpass("ECP password (will be stored in Keychain or secrets file): ")
    if not pw:
        sys.exit("password required")
    try:
        backend = gz_secrets.set(svc, user, pw)
        print(f"  saved to {backend} ({svc}/{user})", file=sys.stderr)
    except Exception as e:
        print(f"  warning: could not persist secret: {e}", file=sys.stderr)
    return pw


def resolve_p12_path(args, config: dict) -> str:
    if getattr(args, "p12", None):
        return args.p12
    env = os.environ.get("P12_PATH")
    if env:
        return env
    cfg_path = config.get("ecp", {}).get("p12_path")
    if cfg_path:
        p = Path(cfg_path).expanduser()
        if p.is_file():
            return str(p)
    # NCALayer recentPath fallback
    ncalayer_settings = Path(
        "~/Library/Application Support/NCALayer/settings.json"
    ).expanduser()
    if ncalayer_settings.is_file():
        try:
            data = json.loads(ncalayer_settings.read_text())
            recent = data.get("settings", {}).get("recentPath")
            if recent and Path(recent).is_file():
                return recent
        except Exception:
            pass
    sys.exit("p12 path required: provide --p12 PATH, $P12_PATH, config.ecp.p12_path, or use NCALayer once")


async def cmd_find_lots(args, config: dict) -> int:
    """Print lots for a given announce_id."""
    from goszakup.client import GoszakupClient
    ci_session = None if args.no_auth else resolve_ci_session(args, config, required=False)
    async with GoszakupClient(
        ci_session=ci_session,
        agreement_cookie=resolve_agreement(config),
        base_url=config["goszakup"]["base_url"],
    ) as gz:
        lots = await gz.discover_announce_lots(str(args.announce))

    if not lots:
        print(f"❌ No lots found for announce {args.announce}", file=sys.stderr)
        return 1

    print(f"Lots in announce {args.announce}:\n")
    print(f"{'lot_id':>10}  {'lot number':<22}  {'sum':>14}  name")
    print("-" * 100)
    for lot in lots:
        print(f"{lot['lot_id']:>10}  {lot['lot_number']:<22}  {lot['sum']:>14}  {lot['name']}")
    print(f"\nFor a single-lot run:")
    if len(lots) == 1:
        print(f"  submit.py bid --announce {args.announce} --lots {lots[0]['lot_id']} --price <KZT> --dry-run")
    else:
        ids = ",".join(str(l["lot_id"]) for l in lots)
        print(f"  submit.py bid --announce {args.announce} --lots <one_or_more_of:{ids}> --price <KZT> --dry-run")
    return 0


async def cmd_login(args, config: dict) -> int:
    """ECP login: 5-step flow via Kalkan, saves fresh ci_session to Keychain."""
    from goszakup.client import GoszakupClient
    from goszakup.local_signer import KalkanSigner

    p12_path = resolve_p12_path(args, config)
    ecp_password = resolve_ecp_password(args, config)

    # account password
    acc_pw = args.account_password or os.environ.get("ACCOUNT_PASSWORD")
    if not acc_pw:
        svc = config.get("account", {}).get("password_keyring_service", "goszakup-acc")
        user = config.get("account", {}).get("password_keyring_user", "default")
        acc_pw = gz_secrets.get(svc, user)
    if not acc_pw:
        acc_pw = getpass.getpass("Goszakup account password: ")
        if not acc_pw:
            sys.exit("account password required")
        try:
            svc = config.get("account", {}).get("password_keyring_service", "goszakup-acc")
            user = config.get("account", {}).get("password_keyring_user", "default")
            backend = gz_secrets.set(svc, user, acc_pw)
            print(f"  saved to {backend} ({svc}/{user})", file=sys.stderr)
        except Exception:
            pass

    print(f"  using .p12: {p12_path}", file=sys.stderr)
    print("  step 1/5: sendkey/kz", file=sys.stderr)

    async with KalkanSigner(p12_path=p12_path, password=ecp_password) as kalkan:
        async with GoszakupClient(base_url=config["goszakup"]["base_url"]) as client:
            try:
                ci_session = await client.login(
                    account_password=acc_pw,
                    kalkan_signer=kalkan,
                )
            except Exception as e:
                print(f"\n❌ LOGIN FAILED: {e}", file=sys.stderr)
                return 1

    # Persist so future `submit.py bid ...` runs pick it up automatically.
    svc = config.get("goszakup", {}).get("ci_session_service", "goszakup-cookie")
    try:
        backend = gz_secrets.set(svc, "ci_session", ci_session)
        print(f"\n✅ login OK; ci_session saved to {backend} ({svc}/ci_session)", file=sys.stderr)
    except Exception as e:
        print(f"\n⚠ login OK but failed to persist cookie: {e}", file=sys.stderr)
        print(f"   raw value: {ci_session[:30]}...", file=sys.stderr)

    return 0


async def cmd_bid(args, config: dict) -> int:
    p12_path = None
    ecp_password = None
    if args.backend == "local":
        p12_path = resolve_p12_path(args, config)
        ecp_password = resolve_ecp_password(args, config)
        print(f"  using .p12: {p12_path}", file=sys.stderr)

    # Optional auto-login: do a fresh ECP login inside the same http session
    # so we don't race with concurrent Chrome activity for the user's slot.
    auto_login_acc_pw = None
    if args.auto_login or args.account_password:
        acc_pw = args.account_password or os.environ.get("ACCOUNT_PASSWORD")
        if not acc_pw:
            svc = config.get("account", {}).get("password_keyring_service", "goszakup-acc")
            user = config.get("account", {}).get("password_keyring_user", "default")
            acc_pw = gz_secrets.get(svc, user)
        if not acc_pw:
            acc_pw = getpass.getpass("Goszakup account password: ")
        if not acc_pw:
            sys.exit("--auto-login requires goszakup account password")
        auto_login_acc_pw = acc_pw

    # Auto-resolve lot_ids if user passed --lots auto or omitted them.
    if not args.lots or args.lots.lower() == "auto":
        from goszakup.client import GoszakupClient as _GC
        async with _GC(
            ci_session=resolve_ci_session(args, config),
            agreement_cookie=resolve_agreement(config),
            base_url=config["goszakup"]["base_url"],
        ) as _gz_probe:
            lots = await _gz_probe.discover_announce_lots(str(args.announce))
        if not lots:
            sys.exit(f"no lots found for announce {args.announce}")
        if len(lots) > 1:
            print("multiple lots in this announce; pick one with --lots <id,id,…>:", file=sys.stderr)
            for l in lots:
                print(f"  {l['lot_id']:>10}  {l['lot_number']:<22}  {l['sum']:>14}  {l['name']}", file=sys.stderr)
            sys.exit(2)
        tender_lot_ids = [lots[0]["lot_id"]]
        print(f"  auto-resolved single lot: {tender_lot_ids[0]} ({lots[0]['lot_number']})", file=sys.stderr)
    else:
        tender_lot_ids = [int(x) for x in args.lots.split(",")]

    params = BidParams(
        announce_id=str(args.announce),
        tender_lot_ids=tender_lot_ids,
        price=int(args.price),
        qualification_source_anno=args.qualification_source,
        dry_run=args.dry_run,
        backend=args.backend,
        p12_path=p12_path,
        ecp_password=ecp_password,
        auto_login_account_password=auto_login_acc_pw,
    )

    state_dir = Path(config["defaults"]["state_dir"]).expanduser()
    store = StateStore(state_dir)
    if args.fresh:
        store.clear(params.announce_id)

    company = config["company"]
    ci_session = resolve_ci_session(args, config)
    agreement = resolve_agreement(config)

    state = await submit_bid(
        params=params,
        ci_session=ci_session,
        company=company,
        state_store=store,
        base_url=config["goszakup"]["base_url"],
        agreement_cookie=agreement,
        pre_t0_poll=config["defaults"].get("pre_t0_warmup", True),
        poll_interval_s=config["defaults"].get("poll_interval_ms", 100) / 1000.0,
    )

    ts = state.timestamps
    if "start" in ts and "signed" in ts:
        print(f"\n=== TIMINGS ===")
        if "t0" in ts:
            print(f"  pre-T0 wait:   {ts['t0'] - ts['start']:.2f}s")
        if "created" in ts:
            print(f"  T0 -> created: {ts['created'] - ts.get('t0', ts['start']):.2f}s")
        if "docs_signed" in ts:
            print(f"  docs signing:  {ts['docs_signed'] - ts['created']:.2f}s")
        print(f"  pricing+sign:  {ts['signed'] - ts['docs_signed']:.2f}s")
        if "public" in ts:
            print(f"  final submit:  {ts['public'] - ts['signed']:.2f}s")
        print(f"  total (T0->):  {ts.get('public', ts['signed']) - ts.get('t0', ts['start']):.2f}s")

    if state.step == "public_ok":
        print(f"\n✅ SUBMITTED: application_id={state.application_id}")
        return 0
    if state.step == "dry_run_complete":
        print(f"\n✅ DRY-RUN COMPLETE: application_id={state.application_id}")
        print(f"   Verify in goszakup UI; status should be 'Проект'.")
        return 0
    print(f"\n❌ FAILED: step={state.step} error={state.error}")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(prog="submit", description="goszakup fast bidder")
    p.add_argument("--config", default="config.yaml", help="path to YAML config")
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="ECP login → refresh ci_session in Keychain")
    login.add_argument("--p12", default=None, help="path to .p12 ECP file")
    login.add_argument("--ecp-password", default=None, help="ECP password")
    login.add_argument(
        "--account-password", default=None,
        help="goszakup account password (prompted if absent; cached in Keychain)",
    )

    fl = sub.add_parser("find-lots", help="list lots for an announce_id")
    fl.add_argument("--announce", required=True, help="announce_id")
    fl.add_argument("--no-auth", action="store_true",
                    help="skip ci_session (announce pages are usually public)")

    bid = sub.add_parser("bid", help="submit a bid on an open tender")
    bid.add_argument("--announce", required=True, help="announce_id (tender number)")
    bid.add_argument(
        "--lots", default="auto",
        help="comma-separated lot IDs; 'auto' (default) picks the only lot if single",
    )
    bid.add_argument("--price", required=True, help="bid price in KZT")
    bid.add_argument(
        "--qualification-source", default=None,
        help="prior tender anno_number to copy worker quals from",
    )
    bid.add_argument("--ci-session", default=None, help="ci_session cookie value")
    bid.add_argument(
        "--backend", choices=("local", "ncalayer"), default="local",
        help="crypto backend: local (Kalkan, fast, no popup) or ncalayer (legacy)",
    )
    bid.add_argument("--p12", default=None, help="path to .p12 ECP file (backend=local)")
    bid.add_argument("--ecp-password", default=None, help="ECP password (backend=local; use Keychain instead)")
    bid.add_argument(
        "--auto-login", action="store_true",
        help="run fresh ECP login in the same http session before bidding (avoids "
             "Chrome session-stealing). +3.5s overhead.",
    )
    bid.add_argument(
        "--account-password", default=None,
        help="goszakup account password (only used with --auto-login)",
    )
    bid.add_argument("--dry-run", action="store_true", help="do not submit final")
    bid.add_argument("--fresh", action="store_true", help="ignore saved state")

    args = p.parse_args()
    config = load_config(Path(args.config).expanduser())
    setup_logging(Path(config["defaults"]["log_dir"]).expanduser())

    if args.cmd == "bid":
        return asyncio.run(cmd_bid(args, config))
    if args.cmd == "login":
        return asyncio.run(cmd_login(args, config))
    if args.cmd == "find-lots":
        return asyncio.run(cmd_find_lots(args, config))
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
