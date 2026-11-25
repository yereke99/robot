"""Sign the tender's own lot documents before the application exists.

Приложение 15 (the тех.спец and its additional files) is not written by the
bidder: the application receives COPIES of the files the customer attached to
the tender, and the bidder signs those copies. Checked on tender 17983518 — the
copies get new file ids, but their sizes equal the originals byte for byte, and
the originals are listed (and downloadable) on the announcement's
"Документация" tab long before the tender opens.

So the slow part — downloading the files — can happen while the bot waits for
the opening:

  * ``run`` reads that list, downloads every file of the selected lots and
    signs it. Signatures go into the usual content cache (sha256 -> CMS).
  * After the application is created, a page of doc 3334 links the copies by
    file name. A copy whose name matches exactly one pre-signed file is
    committed with that signature at once, while its bytes are downloaded in
    the background and compared by sha256 (flow._verify_presigned). A mismatch
    is re-signed from the real bytes and re-saved before docs_next is called,
    so nothing unverified ever reaches the next stage.

Nothing here presents the cabinet session. The files and the public list are
served without a login, so every request goes out on a client that carries no
``ci_session`` — while the bot waits for the opening, a request that made the
portal rotate the session on a side connection would leave the main client
holding a dead cookie. (Only if the public list cannot be reached is the list
read once on the MAIN client, whose own jar then absorbs any rotation.)

Everything here is an optimisation: any failure leaves the bid on the usual
download -> sign -> save path.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

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


def name_key(name: str) -> str:
    """How two spellings of one file name are compared."""
    return _text(name).casefold()


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


def link_names(page_html: str) -> dict[int, list[str]]:
    """file_id -> the texts of the links that download it, in page order."""
    names: dict[int, list[str]] = {}
    for fid, _token, _host, text in _download_links(page_html):
        bucket = names.setdefault(fid, [])
        if text and text not in bucket:
            bucket.append(text)
    return names


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


@dataclass(frozen=True)
class PresignedFile:
    name: str
    key: str
    lot: Optional[int]
    size: int
    sha256: str
    signature: str
    source_fid: int


class TenderFiles:
    """Pre-signed tender files, filled in as each one is ready."""

    def __init__(self) -> None:
        self._files: list[PresignedFile] = []
        self.listed = 0
        self.source = "none"
        self.finished = False

    def __len__(self) -> int:
        return len(self._files)

    def add(self, item: PresignedFile) -> None:
        self._files.append(item)

    def match(self, lot: Optional[int], names: Iterable[str]) -> Optional[PresignedFile]:
        """The single pre-signed file these link texts name, else None.

        Two listed files with the same name and different content (the same
        template attached to two lots, say) are never guessed between.
        """
        keys = {name_key(name) for name in names}
        keys.discard("")
        if not keys:
            return None
        found = [item for item in self._files
                 if item.key in keys and (item.lot is None or lot is None or item.lot == lot)]
        if len({item.sha256 for item in found}) != 1:
            return None
        return found[0]


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
              sig_cache: Optional[dict], files: TenderFiles, main_http=None) -> None:
    """Download and sign the tender's files of ``doc_id`` for the chosen lots.

    ``anonymous`` is a client WITHOUT the cabinet session (GoszakupClient.
    clone_unauthenticated()); ``signer`` a LocalSigner on it. ``main_http`` is
    the main client's httpx client, used for the list only if the public one
    is unreachable. Results appear in ``files`` one by one, so a file that is
    ready helps even while a larger one is still loading.
    """
    started = time.monotonic()
    listing, source = await _listing(anonymous, main_http, str(announce_id), int(doc_id))
    entries = parse_tender_files(listing, httpx.URL(anonymous.base_url).host)
    files.listed, files.source = len(entries), source
    chosen = {int(lot) for lot in lot_ids}
    wanted = [entry for entry in entries if entry.lot is None or entry.lot in chosen]
    if not wanted:
        wanted = entries            # the lot column did not carry the lot id
    wanted = wanted[:_MAX_FILES]
    gate = asyncio.Semaphore(_CONCURRENCY)
    total = 0

    async def one(entry: ListedFile) -> None:
        nonlocal total
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
            digest, signature = await signer.sign_content(data, sig_cache)
            files.add(PresignedFile(
                name=entry.name, key=name_key(entry.name), lot=entry.lot, size=len(data),
                sha256=digest, signature=signature, source_fid=entry.fid))

    await asyncio.gather(*(one(entry) for entry in wanted))
    log.info("flow.presign.done", source=source, listed=len(entries), n=len(files),
             bytes=total, elapsed_s=round(time.monotonic() - started, 2))
