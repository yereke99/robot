"""Verify our SIGN_XML output matches what NURSign produced in login HAR.

Signs <root><key>b2e5442b33b20cace135b12f215be621</key></root> (same nonce as
HAR), prints the result, compares structure against HAR reference.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import subprocess

from goszakup.local_signer import KalkanSigner


def kc(service, account):
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        return None


async def main() -> int:
    p12 = os.environ.get("P12_PATH") or kc("goszakup-ecp", "p12_path")
    if not p12:
        print("set P12_PATH env or store p12_path in Keychain (service=goszakup-ecp, account=p12_path)", file=sys.stderr)
        return 2
    pwd = kc("goszakup-ecp", "default") or os.environ.get("ECP_PASSWORD")
    if not pwd:
        import getpass
        pwd = getpass.getpass("ECP password: ")

    challenge = '<?xml version="1.0" encoding="UTF-8"?><root><key>b2e5442b33b20cace135b12f215be621</key></root>'
    print(f"Challenge XML:\n{challenge}\n")

    async with KalkanSigner(p12_path=p12, password=pwd) as k:
        import time
        t0 = time.perf_counter()
        signed = await k.sign_xml(challenge)
        t1 = time.perf_counter()

    print(f"Signed in {(t1-t0)*1000:.1f}ms\n")
    print("=== SIGNED XML (our Kalkan output) ===")
    print(signed)
    print()

    # Quick structure check
    checks = [
        ('contains <ds:Signature>', '<ds:Signature' in signed),
        ('uses c14n algorithm', 'xml-c14n' in signed),
        ('uses KZ GOST 2015-512 sig alg', 'gostr34102015-gostr34112015-512' in signed),
        ('contains <ds:DigestValue>', '<ds:DigestValue>' in signed),
        ('contains <ds:SignatureValue>', '<ds:SignatureValue>' in signed),
        ('contains <ds:X509Certificate>', '<ds:X509Certificate>' in signed),
        ('enveloped-signature transform', 'enveloped-signature' in signed),
        ('preserves <key> element', 'b2e5442b33b20cace135b12f215be621' in signed),
    ]
    print("=== Structural checks vs NURSign HAR reference ===")
    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")

    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
