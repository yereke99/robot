"""Warm the signature cache from publicly listed technical specifications.

The application can use different file IDs for its copies. Only the verified
content hash lets the normal signing path reuse an existing signature.
Public downloads go through an anonymous client. If needed, the document
list can be fetched through the main authenticated client.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import httpx
import structlog

log = structlog.get_logger("presign")

# The public face of the portal serves the same file list without a login.
# Used only when the cabinet host does not (session gone, route moved).
PUBLIC_PORTAL = "https://goszakup.gov.kz"

_MAX_FILES = 40
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_CONCURRENCY = 3

_ROW = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF = re.compile(r"""href\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_DOWNLOAD_PATH = re.compile(r"/files/download_file/(\d+)(?:/(\d+))?/?")
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _download_links(fragment: str) -> list[tuple[int, Optional[str], str, str]]:
    """[(file_id, token, host, link text)] for every download link in the HTML."""
    out = []
    for attrs, inner in _ANCHOR.findall(fragment or ""):
        href = _HREF.search(attrs)
        if not href:
            continue
        target = html.unescape(href.group(2)).strip()
        try:
            url = httpx.URL(target)
        except (httpx.InvalidURL, ValueError):
            continue
        found = _DOWNLOAD_PATH.search(url.path)
        if not found:
            continue
        out.append((int(found.group(1)), found.group(2), url.host or "", _text(inner)))
    return out


@dataclass(frozen=True)
class ListedFile:
    lot: Optional[int]        # tender lot the row belongs to, when the row says
    fid: int
    path: str                 # /files/download_file/<id>/<token>/
    name: str


def parse_tender_files(listing_html: str, portal_host: str) -> list[ListedFile]:
    """Rows of the announcement's file list (actionAjaxModalShowFiles).

    Each row: lot number ("88452090-ОК1"), a download link whose text is the
    file name, author, organisation, date, signature link. Links to another
    host are skipped — only the portal's own files are fetched.
    """
    files: list[ListedFile] = []
    seen: set[int] = set()
    for row in _ROW.findall(listing_html or ""):
        cells = _CELL.findall(row)
        lot = None
        if cells:
            lot_match = re.match(r"\s*(\d{5,12})\b", _text(cells[0]))
            if lot_match:
                lot = int(lot_match.group(1))
        for fid, token, host, text in _download_links(row):
            if fid in seen or not text or (host and host != portal_host):
                continue
            if not token:
                continue        # a one-segment link cannot be downloaded
            seen.add(fid)
            files.append(ListedFile(lot, fid, f"/files/download_file/{fid}/{token}/", text))
    return files


_XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "text/html, */*; q=0.01"}


def _is_listing(reply: httpx.Response) -> bool:
    return reply.status_code == 200 and "download_file/" in reply.text


async def _listing(anonymous, main_http, announce_id: str, doc_id: int) -> tuple[str, str]:
    path = f"/ru/announce/actionAjaxModalShowFiles/{announce_id}/{doc_id}"
    # 1. The public portal: no cookies, nothing of the account.
    try:
        reply = await anonymous._http.get(f"{PUBLIC_PORTAL}{path}", follow_redirects=True,
                                          headers=_XHR)
        if _is_listing(reply):
            return reply.text, "public"
        log.info("flow.presign.listing_miss", source="public", status=reply.status_code)
    except (httpx.HTTPError, OSError) as exc:
        log.info("flow.presign.listing_miss", source="public", error_type=type(exc).__name__)
    # 2. The cabinet host, on the main client (its jar keeps any new session).
    if main_http is not None:
        try:
            reply = await main_http.get(path, follow_redirects=False, headers=_XHR)
            if _is_listing(reply):
                return reply.text, "session"
            log.info("flow.presign.listing_miss", source="session", status=reply.status_code)
        except (httpx.HTTPError, OSError) as exc:
            log.info("flow.presign.listing_miss", source="session", error_type=type(exc).__name__)
    return "", "none"


async def _download(anonymous, path: str) -> Optional[bytes]:
    async with anonymous._http.stream("GET", path) as reply:
        if reply.status_code != 200:
            return None
        length = reply.headers.get("content-length", "").strip()
        if length.isdigit() and int(length) > _MAX_FILE_BYTES:
            return None
        return await reply.aread()


async def run(anonymous, signer, *, announce_id: str, lot_ids: Sequence[int], doc_id: int,
              sig_cache: dict, main_http=None) -> None:
    """Download and sign the tender's files of ``doc_id`` for the chosen lots.

    ``anonymous`` is a client WITHOUT the cabinet session (GoszakupClient.
    clone_unauthenticated()); ``signer`` a LocalSigner on it. ``main_http`` is
    the main client's httpx client, used for the list if the public one
    is unreachable.
    """
    started = time.monotonic()
    listing, source = await _listing(anonymous, main_http, str(announce_id), int(doc_id))
    entries = parse_tender_files(listing, httpx.URL(anonymous.base_url).host)
    chosen = {int(lot) for lot in lot_ids}
    wanted = [entry for entry in entries if entry.lot is None or entry.lot in chosen]
    if not wanted:
        wanted = entries            # the lot column did not carry the lot id
    wanted = wanted[:_MAX_FILES]
    gate = asyncio.Semaphore(_CONCURRENCY)
    total = downloaded = cached = 0

    async def one(entry: ListedFile) -> None:
        nonlocal total, downloaded, cached
        key = f"public_fid:{entry.fid}"
        if signer.cached_signature(sig_cache, key):
            cached += 1
            return
        async with gate:
            if total > _MAX_TOTAL_BYTES:
                return
            try:
                data = await _download(anonymous, entry.path)
            except (httpx.HTTPError, OSError) as exc:
                log.info("flow.presign.file_miss", fid=entry.fid, error_type=type(exc).__name__)
                return
            if data is None:
                log.info("flow.presign.file_miss", fid=entry.fid, verdict="not_served")
                return
            total += len(data)
            _, signature = await signer.sign_content(data, sig_cache)
            signer.remember_signature(sig_cache, key, signature)
            downloaded += 1

    await asyncio.gather(*(one(entry) for entry in wanted))
    log.info("flow.presign.done", source=source, listed=len(entries), n=downloaded, cached=cached,
             bytes=total, elapsed_s=round(time.monotonic() - started, 2))
