"""Print the file-id-relevant bits of a dumped goszakup doc page.

When the parser can't find a file_id on a show_doc page it dumps the HTML.
This prints just the parts we need to teach the parser the right selector,
so you can paste a short diagnostic instead of a 30KB HTML file.

Usage:
    python tools/peek_doc.py            # newest dump
    python tools/peek_doc.py 1253       # newest dump whose name contains "1253"
"""
from __future__ import annotations

import glob
import os
import re
import sys

pat = sys.argv[1] if len(sys.argv) > 1 else ""
dirs = [
    os.path.expanduser("~/.atagos/logs/html_dumps"),
    os.path.expanduser("~/.goszakup/logs/html_dumps"),
]
files: list[str] = []
for d in dirs:
    files += glob.glob(os.path.join(d, f"*{pat}*.html"))
if not files:
    sys.exit(f"no dumps found (pattern={pat!r}) under: {dirs}")

f = max(files, key=os.path.getmtime)
h = open(f, encoding="utf-8", errors="replace").read()

print("FILE:", f)
print("LEN :", len(h))

# For a fileless lot sub-doc (e.g. Приложение 15) the question is "what action
# does this page want?" — generate, upload, or agree — not just "where's the
# file_id". Surface that first.
print("\n-- doc title / category --")
for t in re.findall(r"Приложение\s*\d+[^<]{0,140}|Техническ[а-я]+\s+специфик[а-я]+[^<]{0,80}", h)[:4]:
    print("  ", re.sub(r"\s+", " ", t).strip())

print("\n-- action buttons/inputs (generate / agree / save / submit) --")
for m in re.findall(r"<(?:button|input)\b[^>]*>", h):
    if re.search(r"сформир|принять|согла|сохран|подпис|submit|generate|type=\"submit\"", m, re.I):
        print("  ", re.sub(r"\s+", " ", m)[:220])

print("\n-- <form> action + method (what to POST and where) --")
for m in re.findall(r"<form\b[^>]*>", h):
    print("  ", re.sub(r"\s+", " ", m)[:220])

print("\n-- download_file refs --")
print(sorted(set(re.findall(r"[\w./-]*download_file/\d+/?", h)))[:15])

print("\n-- all 7-12 digit numbers --")
print(sorted(set(re.findall(r"\d{7,12}", h)))[:40])

print("\n-- <form> tags (action) + data-file-token --")
for m in re.findall(r"<form\b[^>]*>", h):
    print("  ", re.sub(r"\s+", " ", m)[:220])
for m in set(re.findall(r'data-file-token="[^"]+"', h)):
    print("  ", m)

print("\n-- file inputs --")
for m in re.findall(r"<input[^>]+type=\"file\"[^>]*>", h, re.I):
    print("  ", re.sub(r"\s+", " ", m)[:220])

print("\n-- <input> tags --")
for m in re.findall(r"<input[^>]+>", h)[:50]:
    print("  ", re.sub(r"\s+", " ", m)[:220])

print("\n-- <a>/<button> mentioning file/download/sign/digits --")
for m in re.findall(r"<(?:a|button)[^>]+>", h):
    if re.search(r"download|file|sign|подпис|\d{7,}", m, re.I):
        print("  ", re.sub(r"\s+", " ", m)[:220])

print("\n-- cert-like base64 blobs (MII...) --")
for b in sorted(set(re.findall(r"MII[A-Za-z0-9+/=]{80,}", h)), key=len, reverse=True)[:4]:
    print(f"  len={len(b)}: {b[:140]}...")

print("\n-- gamma / public_key / EncryptOfferPrice context --")
for kw in ("public_key", "publicKey", "pubKey", "EncryptOfferPrice", "get_encr_info",
           "ajax_add_encrypt", "encryptData", "id_priceoffer", "certificate", "cert"):
    for m in list(re.finditer(re.escape(kw), h))[:2]:
        i = m.start()
        print(f"  [{kw}]", repr(re.sub(r"\s+", " ", h[max(0, i - 130):i + 220])))

print("\n-- context around likely tokens --")
for kw in ("download_file", "userfile", "signature", "data-id", "data-file",
           "file_id", "fileId", "sign_file", "btnSign", "js-sign", "data-fileid",
           "Подписать", "sign_doc"):
    for m in list(re.finditer(re.escape(kw), h))[:2]:
        i = m.start()
        print(f"  [{kw}]", repr(re.sub(r"\s+", " ", h[max(0, i - 90):i + 90])))
