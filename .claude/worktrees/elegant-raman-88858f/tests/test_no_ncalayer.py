"""Empirical proof: goszakup server doesn't care about NCALayer.

Run twice:
  1. With NCALayer running normally:  python tests/test_no_ncalayer.py
  2. With NCALayer QUIT:               python tests/test_no_ncalayer.py

Both should produce identical results -- because the goszakup server has
no way to know whether NCALayer is running on your machine (127.0.0.1
loopback is not visible to anyone but you).

If both runs pass: confirmed, Phase 2 (no NCALayer) is feasible.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import httpx


def _keychain_get(service: str, account: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or None
    except subprocess.CalledProcessError:
        return None


def get_ci_session() -> str:
    v = _keychain_get("goszakup-cookie", "ci_session") or os.environ.get("CI_SESSION")
    if not v:
        sys.exit("ci_session not found in Keychain or $CI_SESSION")
    return v


def get_agreement() -> str | None:
    return _keychain_get("goszakup-cookie", "agreement_user_2024") or os.environ.get("AGREEMENT_USER_2024")


def is_ncalayer_running() -> bool:
    """Try to TCP-connect to 127.0.0.1:13579."""
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 13579))
        s.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


async def main() -> int:
    ci_session = get_ci_session()
    ncalayer_up = is_ncalayer_running()

    print(f"NCALayer running on 127.0.0.1:13579 : {ncalayer_up}")
    print(f"ci_session cookie length            : {len(ci_session)} chars")
    print()

    cookies = {"ci_session": ci_session}
    agreement = get_agreement()
    if agreement:
        cookies["agreement_user_2024"] = agreement
        print(f"agreement_user_2024 cookie          : present ({len(agreement)}b)")
    else:
        print(f"agreement_user_2024 cookie          : MISSING (may matter)")
    print()

    async with httpx.AsyncClient(
        base_url="https://v3bl.goszakup.gov.kz",
        cookies=cookies,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
        },
        timeout=10.0,
        follow_redirects=False,
        http2=True,
    ) as client:
        checks = [
            ("homepage (public)",          "/ru/"),
            ("my applications (auth)",     "/ru/application/index"),
            ("user profile (auth)",        "/ru/user/profile"),
            ("any application page (auth)","/ru/application/lots/16941324"),
        ]
        all_ok = True
        for label, path in checks:
            try:
                r = await client.get(path)
            except Exception as e:
                print(f"  ❌ {label:30} EXCEPTION: {e}")
                all_ok = False
                continue
            # auth-required pages return 200 if cookie valid, 302→/login if not
            is_login_redirect = (
                r.status_code in (301, 302)
                and "/user/login" in r.headers.get("location", "")
            )
            status = (
                "✅ OK"          if r.status_code == 200
                else "🔒 LOGGED OUT" if is_login_redirect
                else f"⚠ {r.status_code}"
            )
            loc = r.headers.get("location", "")
            print(f"  {status:15} {label:30} HTTP {r.status_code}  ({len(r.content)}B)  -> {loc[:60]}")
            if is_login_redirect:
                all_ok = False

        print()
        if all_ok:
            verdict = (
                "✅ Server treats us as authenticated REGARDLESS of NCALayer state."
                if not ncalayer_up
                else "✅ All authenticated endpoints accessible (run again with NCALayer OFF)."
            )
            print(verdict)
            return 0
        else:
            print("⚠ Some checks failed -- ci_session may be expired or invalid.")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
