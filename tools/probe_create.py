"""One-off diagnostic: what does goszakup return for a CLOSED / not-yet-open
tender at the two endpoints the bid flow touches?

  1. /ru/announce/index/{id}?tab=lots   <- discover_announce_lots (bid.py step 2)
  2. /ru/application/create/{id}         <- poll_announce_ready (pipeline pre-T0)

Run:  .venv/bin/python -m tools.probe_create <announce_id_or_url>

Reuses the same cached ci_session the bot uses; re-logs in via ECP if dead.
Prints status, final URL, and which poll branch would fire. Read-only — never
POSTs, never creates an application.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from goszakup import secrets as gz_secrets
from goszakup.bot import config as bot_config
from goszakup.bot.search import probe_cookie
from goszakup.client import GoszakupClient, SessionExpired
from goszakup.local_signer import KalkanSigner
from goszakup.parser import extract_announce_lots

_ANN_RE = re.compile(r"/announce/index/(\d+)|(\d{6,12})")

# Same markers poll_announce_ready checks for.
FORM_MARKERS = (
    'name="subject_address"',
    'name="contact_phone"',
    "ajax_create_application",
    "Создание заявки",
)


def _announce_id(raw: str) -> str:
    m = _ANN_RE.search(raw)
    if not m:
        sys.exit(f"could not parse announce_id from {raw!r}")
    return m.group(1) or m.group(2)


def _poll_verdict(status: int, final_url: str, body: str) -> str:
    """Replicate poll_announce_ready's branch logic on one response."""
    if "/user/login" in final_url:
        return "SessionExpired (cookie dead) — would raise, NOT a tender-state signal"
    if "access_denied" in final_url or "error_report" in final_url:
        return ("RAISE RuntimeError immediately (client.py:814-819) — "
                "'account isn't eligible'. THIS is the fast-fail pre-T0 bug.")
    if status == 200 and re.search(r"/application/(?:lots|docs|preview|create)/\d+/\d+", final_url):
        return "READY (existing draft redirect)"
    if status == 200 and any(m in body for m in FORM_MARKERS):
        matched = next(m for m in FORM_MARKERS if m in body)
        return f"READY (form rendered, matched {matched!r})"
    if status == 200:
        return ("200 but NO form markers -> poll logs a warning and KEEPS POLLING "
                "until 600s timeout. Bot DOES wait in this case.")
    return (f"status {status}, no redirect to a known endpoint -> falls through to "
            "sleep+retry. Bot keeps polling.")


async def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("usage: python -m tools.probe_create <announce_id_or_url>")
    ann = _announce_id(sys.argv[1])

    cfg = bot_config.load(Path("config.yaml"))
    ci = gz_secrets.get(cfg.ci_session_service, "ci_session")

    gz = GoszakupClient(ci_session=ci, base_url=cfg.base_url)
    try:
        alive = await probe_cookie(gz)
        print(f"[cookie] cached ci_session alive={alive}")
        if not alive:
            print("[cookie] dead -> logging in via ECP (this takes a few seconds)…")
            ecp_pw = gz_secrets.get(cfg.ecp_password_service, cfg.ecp_password_account)
            acc_pw = gz_secrets.get(cfg.account_password_service, cfg.account_password_account)
            signer = KalkanSigner(p12_path=cfg.p12_path, password=ecp_pw)
            await signer.start()
            try:
                new_ci = await gz.login(account_password=acc_pw, kalkan_signer=signer)
                gz_secrets.set(cfg.ci_session_service, "ci_session", new_ci)
                print("[cookie] re-login OK, cached the fresh ci_session")
            finally:
                await signer.close()

        print(f"\n=== announce {ann} ===")

        # 1) discovery endpoint
        for path in (f"/ru/announce/index/{ann}?tab=lots", f"/ru/announce/index/{ann}"):
            r = await gz._http.get(path, follow_redirects=True)
            lots = extract_announce_lots(r.text)
            print(f"[discover] GET {path}")
            print(f"           status={r.status_code} final={r.url} lots_found={len(lots)}")
            if lots:
                break

        # 2) create / poll endpoint — exactly what poll_announce_ready GETs
        path = f"/ru/application/create/{ann}"
        r = await gz._http.get(path, follow_redirects=True)
        final = str(r.url)
        body = r.text
        present = [m for m in FORM_MARKERS if m in body]
        print(f"\n[poll]  GET {path}")
        print(f"        status={r.status_code} final={final}")
        print(f"        body_len={len(body)} form_markers_present={present}")
        print(f"        access_denied_in_url={'access_denied' in final} "
              f"error_report_in_url={'error_report' in final}")
        print(f"\n>>> poll_announce_ready would: {_poll_verdict(r.status_code, final, body)}")
    except SessionExpired as e:
        print(f"[error] SessionExpired: {e}")
        return 2
    finally:
        await gz.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
