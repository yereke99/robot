"""Live test against a running NCALayer + NURSign plugin.

Gated by RUN_LIVE_TESTS=1 because it pops up the ECP password dialog
and requires manual interaction. Run it once after NCALayer is up:

    RUN_LIVE_TESTS=1 pytest tests/test_nursign_live.py -s

What it proves:
    1. NCALayer is reachable on wss://127.0.0.1:13579/
    2. The NURSign plugin is loaded (we get the version greeting)
    3. We can ask NCALayer to download+sign a file by URL
    4. The returned base64 is a plausible CMS PKCS#7 (starts with MII...)
    5. (BONUS) signing twice in the same WS session: does the password popup
       appear twice or once? Result is logged so we can resolve TODO task #7.

The fixture URL is a public PDF on goszakup itself so we don't need a
real bid open. Replace if it 404s.
"""

from __future__ import annotations

import base64
import os
import time

import pytest

from goszakup.nursign import NursignClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="set RUN_LIVE_TESTS=1 to run NCALayer integration tests",
    ),
]

# A small static file on goszakup we can ask NCALayer to download+sign.
# If this 404s, pick any small file URL accessible without auth.
FIXTURE_URL = os.environ.get(
    "NURSIGN_FIXTURE_URL",
    "https://v3bl.goszakup.gov.kz/files/download_file/305885076/",
)


async def test_ncalayer_handshake() -> None:
    async with NursignClient() as ns:
        # If we got here without raising, plugin version was 'NONE' error code.
        assert ns._ws is not None


async def test_sign_file_once() -> None:
    async with NursignClient() as ns:
        sig = await ns.sign_file(FIXTURE_URL)
        assert sig.startswith("MII"), f"not CMS-looking: {sig[:30]}"
        # base64 decodable
        raw = base64.b64decode(sig)
        assert raw[:1] == b"\x30", "CMS DER must start with SEQUENCE tag 0x30"


async def test_password_caches_within_session() -> None:
    """Sign twice. If popup appears once, caching works."""
    async with NursignClient() as ns:
        t0 = time.perf_counter()
        sig1 = await ns.sign_file(FIXTURE_URL)
        t1 = time.perf_counter()
        sig2 = await ns.sign_file(FIXTURE_URL)
        t2 = time.perf_counter()

        # signature 2 should be FAST (no popup) if password is cached
        first_dur = t1 - t0
        second_dur = t2 - t1
        print(
            f"\n  sign_file #1: {first_dur:.2f}s (popup expected first time)"
            f"\n  sign_file #2: {second_dur:.2f}s "
            f"({'CACHED' if second_dur < first_dur / 2 else 'POPUP AGAIN'})"
        )
        # Both produced valid sigs
        assert sig1.startswith("MII")
        assert sig2.startswith("MII")
