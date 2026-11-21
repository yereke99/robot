"""Find open tenders accepting bids.

Logs into goszakup (refreshing ci_session in Keychain along the way),
fetches the announce search page, and prints the first N tenders that
match common bid-able criteria.

Usage:
    .venv/bin/python scripts/find_open_tenders.py [--limit 20] [--max-sum 5000000]

Filters down to:
    status = "Опубликовано (прием заявок)"  (status code 210 in goszakup taxonomy)

For each tender prints: announce_id, end_date, sum, name. To get the
lot_id, open https://v3bl.goszakup.gov.kz/ru/announce/index/{announce_id}
in Chrome, switch to "Лоты" tab, inspect the row (`data-lot-id` attr).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
from pathlib import Path

# Allow `python scripts/...` from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import html as lxml_html

from goszakup import secrets as gz_secrets
from goszakup.client import GoszakupClient
from goszakup.local_signer import KalkanSigner


def get_credentials() -> tuple[str, str, str]:
    p12 = os.environ.get("P12_PATH") or gz_secrets.get("goszakup-ecp", "p12_path")
    if not p12:
        p12 = input("Path to .p12 (GOST-2015 ECP): ").strip()
    ecp_pw = (gz_secrets.get("goszakup-ecp", "default")
              or os.environ.get("ECP_PASSWORD")
              or getpass.getpass("ECP password: "))
    acc_pw = (gz_secrets.get("goszakup-acc", "default")
              or os.environ.get("ACCOUNT_PASSWORD")
              or getpass.getpass("Goszakup account password: "))
    return p12, ecp_pw, acc_pw


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--max-sum", type=float, default=None,
                    help="filter to tenders with planned sum below this (KZT)")
    args = ap.parse_args()

    # Use saved ci_session if present; only re-login if missing.
    saved_ci = gz_secrets.get("goszakup-cookie", "ci_session")
    saved_agreement = gz_secrets.get("goszakup-cookie", "agreement_user_2024")

    if saved_ci:
        print("Using saved ci_session from Keychain…", file=sys.stderr)
        gz = GoszakupClient(
            ci_session=saved_ci,
            agreement_cookie=saved_agreement,
            base_url="https://v3bl.goszakup.gov.kz",
        )
        await gz.__aenter__()
        kalkan = None
    else:
        p12, ecp_pw, acc_pw = get_credentials()
        kalkan = KalkanSigner(p12_path=p12, password=ecp_pw)
        await kalkan.start()
        gz = GoszakupClient(base_url="https://v3bl.goszakup.gov.kz")
        await gz.__aenter__()
        print("Logging in…", file=sys.stderr)
        ci = await gz.login(account_password=acc_pw, kalkan_signer=kalkan)
        try:
            gz_secrets.set("goszakup-cookie", "ci_session", ci)
        except Exception as e:
            print(f"  warning: secret persistence failed: {e}", file=sys.stderr)

    try:
        print("Fetching tenders…", file=sys.stderr)
        r = await gz._http.get("/ru/search/announce", follow_redirects=True)
        r.raise_for_status()

        tree = lxml_html.fromstring(r.text)
        rows = tree.xpath("//table[contains(@class,'table')]//tbody/tr")
        print(f"  raw rows: {len(rows)}", file=sys.stderr)

        print()
        print(f"{'announce_id':>11}  {'sum, тг':>16}  {'end_date':>19}  name")
        print("-" * 110)

        shown = 0
        for row in rows:
            cells = row.xpath(".//td")
            if len(cells) < 6:
                continue
            href = row.xpath(".//a[contains(@href,'/announce/index/')]/@href")
            if not href:
                continue
            m = re.search(r"/announce/index/(\d+)", href[0])
            if not m:
                continue
            announce_id = m.group(1)
            name = _clean(cells[1].text_content())[:70]
            end_dt = _clean(cells[4].text_content())
            sum_str = _clean(cells[5].text_content())
            try:
                sum_val = float(sum_str.replace("\xa0", "").replace(" ", "").replace(",", "."))
            except ValueError:
                sum_val = None
            if args.max_sum is not None and (sum_val is None or sum_val > args.max_sum):
                continue
            print(f"{announce_id:>11}  {sum_str:>16}  {end_dt:>19}  {name}")
            shown += 1
            if shown >= args.limit:
                break

        print()
        print(f"Showing {shown}/{len(rows)} tenders.")
        print(f"Next: .venv/bin/python submit.py find-lots --announce <id>")
    finally:
        await gz.aclose()
        if kalkan is not None:
            await kalkan.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
