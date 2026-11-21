"""HTML parsing for goszakup pages.

We extract:
  - file_id from show_doc / sign_task pages (so we can ask NCALayer to sign
    https://.../files/download_file/{file_id}/).
  - List of (lot_id, point_id) from the priceoffers page (for xmlData payload).
  - copy_data_docs from-lot/to-lot options.

Without a captured HTML sample we rely on multiple patterns; the matched
pattern is logged. If parsing fails we dump the HTML under ~/.goszakup/logs
so it's debuggable next iteration.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import structlog
from lxml import html

log = structlog.get_logger("parser")

DEBUG_DUMP_DIR = Path("~/.goszakup/logs/html_dumps").expanduser()


def _dump(html_text: str, hint: str) -> Path:
    DEBUG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", hint)[:80]
    p = DEBUG_DUMP_DIR / f"{ts}_{safe}.html"
    p.write_text(html_text, encoding="utf-8")
    return p


def extract_file_id(html_text: str, *, hint: str = "unknown") -> int:
    """Find the file_id to sign on a show_doc / sign_task page.

    Strategy in priority order:
      1. download_file/{N}/ URL in href/src/data-* attribute
      2. signature[N] form field in any form
      3. data-file-id="N" attribute
      4. Inline JS hint: file_id = N or files/download_file/N

    On miss, dumps the HTML and raises.
    """
    file_ids = list(_iter_file_ids(html_text))
    if not file_ids:
        dump = _dump(html_text, f"no_file_id_{hint}")
        log.error("parser.no_file_id", hint=hint, dump=str(dump))
        raise ValueError(f"no file_id found on page ({hint}); HTML dumped to {dump}")
    if len(set(file_ids)) > 1:
        log.warning("parser.multiple_file_ids", hint=hint, found=file_ids[:5])
    return file_ids[0]


def extract_all_file_ids(html_text: str, *, hint: str = "unknown", quiet: bool = False) -> list[int]:
    """Find every file_id on a sign_task page (ТЗ has multiple docs per lot).

    ``quiet=True``: a fileless page is an EXPECTED outcome (the caller is probing
    for the end of a contiguous sub-doc list), so don't log it as an error or dump
    HTML — just raise so the caller can branch. Used by the тех.спец page walker:
    page N+1 being fileless is the normal list terminator, not a failure.
    """
    ids = list(dict.fromkeys(_iter_file_ids(html_text)))  # preserve order, dedupe
    if not ids:
        if not quiet:
            dump = _dump(html_text, f"no_file_ids_{hint}")
            log.error("parser.no_file_ids", hint=hint, dump=str(dump))
            raise ValueError(f"no file_ids found on page ({hint}); HTML dumped to {dump}")
        raise ValueError(f"no file_ids found on page ({hint})")
    log.info("parser.file_ids", hint=hint, count=len(ids))
    return ids


_PATTERNS = [
    re.compile(r"download_file/(\d+)/?"),          # relaxed: matches /ru/files/... too
    re.compile(r"signature\[(\d+)\]"),
    re.compile(r'data-file[-_]?id="(\d+)"'),
    re.compile(r'userfile\[\d+\][^>]*?value="(\d+)"'),
    re.compile(r"file_id\s*[=:]\s*['\"]?(\d+)"),
]


def _iter_file_ids(html_text: str) -> Iterable[int]:
    for pat in _PATTERNS:
        for m in pat.finditer(html_text):
            yield int(m.group(1))


def extract_priceoffer_structure(html_text: str) -> list[tuple[int, int]]:
    """Find (lot_id, point_id) pairs from the priceoffers page.

    The form has fields like offer[<lot_id>][<point_id>][price].
    """
    pat = re.compile(r"offer\[(\d+)\]\[(\d+)\]\[price\]")
    pairs = list(dict.fromkeys((int(a), int(b)) for a, b in pat.findall(html_text)))
    if not pairs:
        dump = _dump(html_text, "no_priceoffers")
        log.error("parser.no_priceoffers", dump=str(dump))
        raise ValueError(f"no (lot_id, point_id) pairs found; HTML dumped to {dump}")
    return pairs


def extract_copy_data_from_lots(html_text: str) -> list[int]:
    """List of `from_lot` IDs available when copying qualification data.

    Used after the first copy_data_docs search step to pick a source lot.
    """
    tree = html.fromstring(html_text)
    radios = tree.xpath("//input[@type='radio' and @name='from_lot']/@value")
    return [int(v) for v in radios if v.isdigit()]


def extract_copy_data_to_lots(html_text: str) -> list[int]:
    """List of `to_lot[]` IDs available (our lots that need the data)."""
    tree = html.fromstring(html_text)
    cbs = tree.xpath("//input[@type='checkbox' and @name='to_lot[]']/@value")
    return [int(v) for v in cbs if v.isdigit()]


def extract_announce_lots(html_text: str) -> list[dict]:
    """Parse /ru/announce/index/{announce_id} → list of lots in the tender.

    Tries several patterns since goszakup pages vary by tender method:
      1. `<a class="btn-select-history" data-lot-id="...">`  (classic)
      2. `<a class="btn-select-lot" data-lot-id="...">`      (alternative)
      3. Any anchor in a lots table with `data-lot-id`
      4. Hidden inputs `name="lot_id" value="..."` inside lots forms
    """
    tree = html.fromstring(html_text)
    lots: list[dict] = []
    seen: set[int] = set()

    selectors = [
        "//a[contains(@class,'btn-select-history')][@data-lot-id]",
        "//a[contains(@class,'btn-select-lot')][@data-lot-id]",
        "//a[@data-lot-id]",
        "//*[@data-trb-buy][@data-lot-id]",
    ]
    candidates = []
    for sel in selectors:
        candidates = tree.xpath(sel)
        if candidates:
            log.info("parser.announce_lots.matched", selector=sel, count=len(candidates))
            break

    for a in candidates:
        lot_id_raw = a.get("data-lot-id") or ""
        if not lot_id_raw.isdigit():
            continue
        lot_id = int(lot_id_raw)
        if lot_id in seen:
            continue
        seen.add(lot_id)
        lot_number = _clean(a.text_content()) or f"lot_{lot_id}"
        row = a.xpath("ancestor::tr[1]")
        name = sum_str = ""
        if row:
            cells = row[0].xpath("./td")
            if len(cells) > 3:
                name = _clean(cells[3].text_content())[:80]
            if len(cells) > 8:
                sum_str = _clean(cells[8].text_content())
        lots.append({
            "lot_id": lot_id,
            "lot_number": lot_number,
            "name": name,
            "sum": sum_str,
        })

    if not lots:
        dump = _dump(html_text, "announce_no_lots")
        log.warning("parser.announce_lots.empty", dump=str(dump))
    return lots


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()
