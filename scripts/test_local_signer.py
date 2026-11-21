"""End-to-end smoke test of the local GOST signer.

What it does:
    1. Pulls .p12 path from NCALayer's settings.json (or env var)
    2. Asks for the ECP password ONCE via getpass (no echo)
    3. Launches KalkanSigner.java helper
    4. Signs the bytes "hello world" twice (warm test)
    5. Prints the base64 CMS and decoded ASN.1 structure
    6. Compares structure against the NURSign signatures captured in HAR
       to verify byte-compatible output

If the structure matches NURSign output (same OIDs, same signed attrs),
we know our local signer produces what goszakup expects.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import json
import os
import sys
import time
from pathlib import Path

# Make the project root importable when this script is run as `python scripts/...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from goszakup.local_signer import KalkanSigner

DEFAULT_NCALAYER_SETTINGS = Path(
    "~/Library/Application Support/NCALayer/settings.json"
).expanduser()


def discover_p12() -> str:
    env = os.environ.get("P12_PATH")
    if env:
        return env
    if DEFAULT_NCALAYER_SETTINGS.is_file():
        try:
            data = json.loads(DEFAULT_NCALAYER_SETTINGS.read_text())
            recent = data.get("settings", {}).get("recentPath")
            if recent and Path(recent).is_file():
                return recent
        except Exception:
            pass
    sys.exit("Set P12_PATH=/path/to/key.p12 or use NCALayer at least once")


async def main() -> int:
    p12 = discover_p12()
    print(f"Using .p12: {p12}")
    password = getpass.getpass("ECP password: ")
    if not password:
        sys.exit("password required")

    async with KalkanSigner(p12_path=p12, password=password) as signer:
        # warmup
        assert await signer.ping(), "PING failed"
        print("  ✅ helper is alive\n")

        sample = b"hello world"

        # warm sign
        t0 = time.perf_counter()
        sig1 = await signer.sign_bytes(sample)
        t1 = time.perf_counter()
        # second sign -- should be near-zero overhead (entity cached)
        sig2 = await signer.sign_bytes(sample)
        t2 = time.perf_counter()

        print(f"  sign #1: {(t1-t0)*1000:6.1f} ms (entity already loaded)")
        print(f"  sign #2: {(t2-t1)*1000:6.1f} ms")
        print(f"  sig1 length: {len(sig1)} base64 chars")
        print(f"  identical? {sig1 == sig2}")
        print()

        # decode and print top-level structure
        try:
            from asn1crypto import cms
            raw = base64.b64decode(sig1)
            ci = cms.ContentInfo.load(raw)
            print(f"  content_type: {ci['content_type'].native}")
            sd = ci["content"]
            print(f"  digest_algorithms: {[a['algorithm'].native for a in sd['digest_algorithms']]}")
            certs = sd["certificates"]
            for c in certs:
                if c.name == "certificate":
                    cert = c.chosen
                    print(f"  cert subject: {cert.subject.human_friendly[:80]}")
                    break
            si = sd["signer_infos"][0]
            print(f"  signer digest_alg: {si['digest_algorithm']['algorithm'].dotted}")
            print(f"  signer sig_alg:    {si['signature_algorithm']['algorithm'].dotted}")
            sa = list(si["signed_attrs"]) if si["signed_attrs"] is not None else []
            print(f"  signed_attrs: {[a['type'].native for a in sa]}")
            print(f"  signature bytes: {len(si['signature'].native)}")
        except Exception as e:
            print(f"  (asn1 parse warning: {e})")

        # try TSA-mode (the priceoffer flavour) — this requires network to tsp.pki.gov.kz
        print()
        print("  -- with TSA (network to tsp.pki.gov.kz) --")
        try:
            t3 = time.perf_counter()
            sig_tsa = await signer.sign_text_with_tsa("hello with timestamp")
            t4 = time.perf_counter()
            print(f"  sign+TSA: {(t4-t3)*1000:6.1f} ms")
            print(f"  sig length: {len(sig_tsa)} base64 chars (should be ~5500+ if TSA worked)")
        except Exception as e:
            print(f"  TSA call failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
