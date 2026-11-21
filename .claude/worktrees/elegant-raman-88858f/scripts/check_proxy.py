#!/usr/bin/env python3
"""Pre-flight: does this proxy let us reach v3bl.goszakup.gov.kz unblocked?

Usage:
    .venv/bin/python scripts/check_proxy.py http://user:pass@host:port
    .venv/bin/python scripts/check_proxy.py socks5://user:pass@host:port
    .venv/bin/python scripts/check_proxy.py    # no proxy — baseline

Spends 3 requests:
    1. /                       — goszakup main domain, 200 = sane
    2. /ru/                    — v3bl bidding domain, 302 = unblocked
    3. /ru/announce/index/...  — announce page, 200 = unblocked

Reports the IP we exit through (visible to goszakup) so you can see what
ASN the proxy is on.
"""

from __future__ import annotations

import asyncio
import sys

import httpx


URLS = [
    ("https://goszakup.gov.kz/",                       (200, 302)),
    ("https://v3bl.goszakup.gov.kz/ru/",               (200, 302)),
    ("https://v3bl.goszakup.gov.kz/ru/announce/index/16941325", (200, 302)),
]


async def main() -> int:
    proxy = sys.argv[1] if len(sys.argv) > 1 else None
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36"),
    }

    print(f"\nproxy: {proxy or '(direct)'}")

    async with httpx.AsyncClient(
        proxy=proxy, headers=headers, timeout=20.0, follow_redirects=False,
        http2=(proxy is None),
    ) as c:
        try:
            ip_r = await c.get("https://ifconfig.me", timeout=15.0)
            print(f"outbound IP visible to goszakup: {ip_r.text.strip()}")
        except Exception as e:
            print(f"outbound IP check failed: {e}")

        ok = True
        for url, expected in URLS:
            try:
                r = await c.get(url, timeout=15.0)
                marker = "✅" if r.status_code in expected else "❌"
                print(f"  {marker} {r.status_code:>3}  {url}")
                if r.status_code not in expected:
                    ok = False
            except Exception as e:
                print(f"  ❌ ERR  {url}  ({type(e).__name__}: {e})")
                ok = False

    print()
    if ok:
        print("🟢 PROXY OK — goszakup пропускает, можно покупать тариф этого провайдера.")
        return 0
    else:
        print("🔴 PROXY BLOCKED — этот провайдер тоже забанен на v3bl. Пробуй другого.")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
