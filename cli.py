"""atagos CLI — drive the goszakup gamma bidder from the terminal.

    python cli.py login                 # refresh ci_session via ECP
    python cli.py tumar-check           # verify TumarCSP is reachable + version
    python cli.py bid --announce <ANN> --lots <L1,L2> [--price N] \
                      [--dry-run] [--auto-login]

`bid` without --price bids the dumping floor (d_sum) the server returns — the
exact minimum, which is what we always want. --dry-run stops before the final
ajax_public_application (draft stays at "Проект", easily revoked).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import structlog
import yaml

from goszakup import secrets as gz_secrets
from goszakup.client import GoszakupClient
from goszakup.flow import AtagosBidParams, BeneficiaryInfo, submit_bid
from goszakup.local_signer import KalkanSigner
from goszakup.state import StateStore
from goszakup.tumar import TumarCSP
from goszakup.timing import poll_interval_s

log = structlog.get_logger("cli")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ecp_password(cfg: dict) -> str:
    svc = cfg["ecp"]["password_keyring_service"]
    acc = cfg["ecp"]["password_keyring_user"]
    pw = gz_secrets.get(svc, acc)
    if not pw:
        pw = input(f"ECP (.p12) password [{svc}/{acc}]: ").strip()
        backend = gz_secrets.set(svc, acc, pw)
        print(f"  saved to {backend}")
    return pw


def _account_password(cfg: dict) -> str | None:
    svc = cfg["account"]["password_keyring_service"]
    acc = cfg["account"]["password_keyring_user"]
    return gz_secrets.get(svc, acc)


async def cmd_login(cfg: dict) -> int:
    ecp_pw = _ecp_password(cfg)
    acc_pw = _account_password(cfg)
    if not acc_pw:
        acc_pw = input("goszakup cabinet password: ").strip()
        gz_secrets.set(cfg["account"]["password_keyring_service"],
                       cfg["account"]["password_keyring_user"], acc_pw)
    async with GoszakupClient(base_url=cfg["goszakup"]["base_url"]) as gz:
        signer = KalkanSigner(p12_path=cfg["ecp"]["p12_path"], password=ecp_pw)
        await signer.start()
        try:
            ci = await gz.login(account_password=acc_pw, kalkan_signer=signer)
        finally:
            await signer.close()
    backend = gz_secrets.set(cfg["goszakup"]["ci_session_service"], "ci_session", ci)
    print(f"✅ ci_session refreshed (saved to {backend})")
    return 0


async def cmd_tumar_check(cfg: dict) -> int:
    tc = cfg["tumarcsp"]
    try:
        async with TumarCSP(api_key=tc["api_key"], ws_url=tc["ws_url"]) as tumar:
            print(f"✅ TumarCSP reachable — version {tumar.version}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"❌ TumarCSP check failed: {e}")
        return 2


async def cmd_bid(cfg: dict, args) -> int:
    ci = gz_secrets.get(cfg["goszakup"]["ci_session_service"], "ci_session")
    if not ci and not args.auto_login:
        print("no ci_session — run `python cli.py login` first, or pass --auto-login")
        return 2

    ecp_pw = _ecp_password(cfg)
    acc_pw = _account_password(cfg) if args.auto_login else None
    bcfg = cfg["beneficiary"]
    beneficiary = BeneficiaryInfo(
        name=bcfg["name"], citizenship=str(bcfg["citizenship"]),
        res_country=str(bcfg["res_country"]), doc_number=str(bcfg["doc_number"]),
        doc_date=bcfg["doc_date"], options=bcfg["options"], id_pdf_path=bcfg["id_pdf_path"],
        id_file_id=bcfg.get("id_file_id") or None,
    )
    force_dry = os.getenv("ATAGOS_FORCE_DRY_RUN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    dry = bool(args.dry_run or cfg.get("defaults", {}).get("dry_run") or force_dry)
    if dry and not args.dry_run:
        print("⚠ dry-run ON (config.yaml dry_run / ATAGOS_FORCE_DRY_RUN) — submit blocked")
    params = AtagosBidParams(
        announce_id=str(args.announce),
        tender_lot_ids=[int(x) for x in str(args.lots).split(",") if x.strip()],
        price=args.price,
        dumping_floor_ratio=float(cfg["defaults"].get("dumping_floor_ratio", 0.80)),
        dry_run=dry,
        p12_path=cfg["ecp"]["p12_path"],
        ecp_password=ecp_pw,
        tumar_api_key=cfg["tumarcsp"]["api_key"],
        tumar_ws_url=cfg["tumarcsp"]["ws_url"],
        autofill_price_popup=bool(cfg["tumarcsp"].get("autofill_popup", False)),
        popup_coords=cfg["tumarcsp"].get("popup") or None,
        auto_login_account_password=acc_pw,
        lot_barrier=str(cfg.get("defaults", {}).get("lot_barrier", "each")),
    )
    store = StateStore(Path(cfg["defaults"]["state_dir"]).expanduser())
    state = await submit_bid(
        params=params,
        ci_session=ci or "",
        company=cfg["company"],
        beneficiary=beneficiary,
        state_store=store,
        base_url=cfg["goszakup"]["base_url"],
        pre_t0_poll=not args.no_poll,
        poll_interval_s=poll_interval_s(cfg),
    )
    print(f"\n=== {'DRY-RUN ' if args.dry_run else ''}done: step={state.step} app_id={state.application_id} ===")
    if state.error:
        print("error:", state.error)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="atagos")
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("tumar-check")
    b = sub.add_parser("bid")
    b.add_argument("--announce", required=True)
    b.add_argument("--lots", required=True, help="comma-separated tender lot ids")
    b.add_argument("--price", type=float, default=None, help="default: dumping floor (min)")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--auto-login", action="store_true")
    b.add_argument("--no-poll", action="store_true", help="skip pre-T0 wait (tender already open)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.cmd == "login":
        return asyncio.run(cmd_login(cfg))
    if args.cmd == "tumar-check":
        return asyncio.run(cmd_tumar_check(cfg))
    if args.cmd == "bid":
        return asyncio.run(cmd_bid(cfg, args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
