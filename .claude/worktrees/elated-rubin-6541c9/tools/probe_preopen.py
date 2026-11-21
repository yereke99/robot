"""Find not-yet-open tenders (status 220) and probe their create endpoint to
see what goszakup returns BEFORE the приём-заявок window opens.

Run: python -m tools.probe_preopen
Read-only. Reuses cached cookie.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from goszakup import secrets as gz_secrets
from goszakup.bot import config as bot_config
from goszakup.bot.search import find_tenders, probe_cookie, STATUS_PUBLISHED
from goszakup.client import GoszakupClient

FORM_MARKERS = ('name="subject_address"', 'name="contact_phone"',
                "ajax_create_application", "Создание заявки")


async def main() -> int:
    cfg = bot_config.load(Path("config.yaml"))
    ci = gz_secrets.get(cfg.ci_session_service, "ci_session")
    gz = GoszakupClient(ci_session=ci, base_url=cfg.base_url)
    try:
        print(f"[cookie] alive={await probe_cookie(gz)}")
        rows = await find_tenders(
            gz,
            statuses=tuple(cfg.raw["search"]["statuses"]),
            methods=tuple(cfg.raw["search"]["methods"]),
            limit=40,
        )
        pre = [r for r in rows if r["status_code"] == STATUS_PUBLISHED]
        openn = [r for r in rows if r["status_code"] != STATUS_PUBLISHED]
        print(f"[search] {len(rows)} rows: {len(pre)} not-yet-open(220), {len(openn)} open(210)")

        # Probe up to 3 not-yet-open tenders + 1 open one for contrast.
        sample = pre[:3] + openn[:1]
        if not sample:
            print("No tenders matched the configured filters right now.")
            return 0
        for r in sample:
            ann = r["announce_id"]
            path = f"/ru/application/create/{ann}"
            resp = await gz._http.get(path, follow_redirects=True)
            final = str(resp.url)
            present = [m for m in FORM_MARKERS if m in resp.text]
            print(f"\n--- {ann}  [{r['status_text']}]  end={r['end_date']}")
            print(f"    name={r['name'][:60]!r}")
            print(f"    create -> status={resp.status_code} final={final}")
            print(f"    form_markers={present} "
                  f"error_report={'error_report' in final} access_denied={'access_denied' in final}")
    finally:
        await gz.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
