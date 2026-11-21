"""Python wrapper for KalkanSigner.java -- the local GOST signer that
replaces NCALayer/NURSign in the hot path of submission.

Architecture: a single long-lived Java subprocess holds the SigningEntity
in memory after one password entry. We talk to it over stdin/stdout with
a tiny line-based protocol (see KalkanSigner.java).

Latency: ~5-50ms per signature once the helper is warm. JVM cold-start is
~500ms-1s. The pipeline launches one helper per submission, so cold-start
is amortized over ~9 signatures.

Why a subprocess instead of an embedded JNI:
    1. Isolates Java memory from Python -- failures don't crash the script.
    2. We can keep using cpython without a JPype/Py4J dep.
    3. The Java side uses the exact same Kalkan library NCALayer/NURSign use,
       so output is byte-compatible.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import subprocess
import time
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx
import structlog

from .timing import trace_event

log = structlog.get_logger("local_signer")

PROJECT_ROOT = Path(__file__).parent.parent
JARS_DIR = PROJECT_ROOT / "goszakup" / "jars"
_SPEC_RANGE_MIN_BYTES = 4 * 1024 * 1024
_SPEC_RANGE_MAX_SUFFIX_BYTES = 8 * 1024 * 1024
_STRONG_ETAG = re.compile(r'"[!\x23-\x7e]*"')


def _range_eligibility(response: httpx.Response) -> tuple[int, str, str]:
    headers = response.headers
    length = headers.get("content-length", "").strip()
    size = int(length) if re.fullmatch(r"[0-9]{1,19}", length) else 0
    if response.status_code != 200 or response.history:
        return size, "", "status"
    if size < _SPEC_RANGE_MIN_BYTES:
        return size, "", "small_or_unknown_length"
    if headers.get("content-encoding", "identity").strip().lower() != "identity":
        return size, "", "encoded"
    if headers.get("accept-ranges", "").strip().lower() != "bytes":
        return size, "", "no_byte_ranges"
    etag = headers.get("etag", "").strip()
    if not _STRONG_ETAG.fullmatch(etag):
        return size, "", "no_strong_etag"
    return size, etag, "eligible"


def _java_executable() -> str:
    override = os.environ.get("JAVA_BIN")
    if override and Path(override).is_file():
        return override
    # Windows: scan known install locations for Adoptium/OpenJDK.
    if os.name == "nt":
        for base in (
            Path(r"C:\Program Files\Eclipse Adoptium"),
            Path(r"C:\Program Files\OpenJDK"),
            Path(r"C:\Program Files\Java"),
            Path(r"C:\Program Files (x86)\Eclipse Adoptium"),
        ):
            if not base.is_dir():
                continue
            # Pick the highest version directory available.
            subs = [s for s in base.iterdir() if s.is_dir()]
            for sub in sorted(subs, key=lambda p: p.name, reverse=True):
                java_exe = sub / "bin" / "java.exe"
                if java_exe.is_file():
                    return str(java_exe)
        return "java"  # PATH fallback (winget installer adds to PATH)
    # macOS (brew openjdk@21) and Linux (Debian openjdk-21).
    for candidate in (
        "/opt/homebrew/opt/openjdk@21/bin/java",
        "/opt/homebrew/opt/openjdk/bin/java",
        "/usr/lib/jvm/java-21-openjdk-amd64/bin/java",
        "/usr/lib/jvm/java-21-openjdk-arm64/bin/java",
        "/usr/lib/jvm/default-java/bin/java",
        "/usr/bin/java",
    ):
        if Path(candidate).is_file():
            return candidate
    return "java"


class KalkanSignerError(RuntimeError):
    pass


@dataclass
class KalkanResponse:
    ok: bool
    payload: str = ""
    error: str = ""


class KalkanSigner:
    """Long-lived Java helper for KZ GOST CMS signing."""

    def __init__(self, *, p12_path: str, password: str, jars_dir: Path = JARS_DIR):
        self.p12_path = str(Path(p12_path).expanduser())
        self._password = password
        self.jars_dir = jars_dir
        self._proc: Optional[subprocess.Popen] = None
        self._key_id: Optional[str] = None
        # Java helper has one stdin/stdout pair, so concurrent _cmd calls would
        # interleave writes and read each other's responses. Lock serializes
        # signing while leaving HTTP fetches in the caller fully concurrent.
        self._cmd_lock = asyncio.Lock()
        self._exchanges: set[asyncio.Task] = set()

    @property
    def key_id(self) -> str:
        """Short fingerprint of the .p12 this signer uses.

        Cached signatures are stored under it, so after the ECP is renewed (a new
        .p12) the old signatures — made with the old certificate — are simply
        never found again, instead of being re-submitted and rejected at T0.
        """
        if self._key_id is None:
            import hashlib
            try:
                digest = hashlib.sha256(Path(self.p12_path).read_bytes()).hexdigest()
                self._key_id = digest[:12]
            except OSError:
                self._key_id = "nokey"
        return self._key_id

    async def __aenter__(self) -> "KalkanSigner":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        # Classpath separator differs by platform: ':' on Unix, ';' on Windows.
        cp = f"{self.jars_dir}{os.sep}*{os.pathsep}{self.jars_dir}"
        logback_cfg = self.jars_dir / "logback.xml"
        java_args = [
            _java_executable(),
            f"-Dlogback.configurationFile={logback_cfg}",
            "-cp", cp,
            "KalkanSigner",
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *java_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        log.info("kalkan.started", pid=self._proc.pid)
        # Load the keystore (server has just printed "KalkanSigner ready" on stderr
        # but we don't read stderr to keep things simple -- the first cmd reply
        # confirms the process is alive).
        pw_b64 = base64.b64encode(self._password.encode("utf-8")).decode("ascii")
        resp = await self._cmd(f"LOAD {self.p12_path} {pw_b64}")
        if not resp.ok:
            raise KalkanSignerError(f"LOAD failed: {resp.error}")
        log.info("kalkan.loaded", subject=resp.payload)

    async def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.stdin.write(b"QUIT\n")
            await self._proc.stdin.drain()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._proc.kill()
        log.info("kalkan.closed")
        self._proc = None

    async def _cmd(self, line: str) -> KalkanResponse:
        exchange = asyncio.create_task(self._cmd_locked(line))
        self._exchanges.add(exchange)
        exchange.add_done_callback(self._observe_exchange)
        # If a caller is cancelled, the exchange still consumes its reply
        # under the lock before another command can read from stdout.
        return await asyncio.shield(exchange)

    def _observe_exchange(self, exchange: asyncio.Task) -> None:
        self._exchanges.discard(exchange)
        if not exchange.cancelled():
            exchange.exception()

    async def _cmd_locked(self, line: str) -> KalkanResponse:
        assert self._proc is not None
        queued_at = time.monotonic()
        async with self._cmd_lock:
            started = time.monotonic()
            if started - queued_at >= 0.001:
                trace_event("wait", "Очередь Kalkan", queued_at)
            try:
                self._proc.stdin.write((line + "\n").encode("utf-8"))
                await self._proc.stdin.drain()
                # Other lines may be logs from the Java library.
                for _ in range(100):
                    raw = await self._proc.stdout.readline()
                    if not raw:
                        return KalkanResponse(ok=False, error="helper_died")
                    text = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                    if text.startswith("OK"):
                        return KalkanResponse(ok=True, payload=text[3:] if len(text) > 2 else "")
                    if text.startswith("ERR"):
                        return KalkanResponse(ok=False, error=text[4:] if len(text) > 3 else "unknown")
                    if text == "PONG":
                        return KalkanResponse(ok=True, payload="PONG")
                    log.debug("kalkan.stray_stdout", line=text[:200])
                return KalkanResponse(ok=False, error="too_many_stray_lines")
            finally:
                trace_event("kalkan", line.split(" ", 1)[0], started)

    async def ping(self) -> bool:
        response = await self._cmd("PING")
        return response.ok and response.payload == "PONG"

    async def sign_file(self, path: str) -> str:
        """Sign a local file's bytes. Returns base64 CMS PKCS#7 detached."""
        # SIGN_FILE protocol takes path as second token; shell-quote to handle spaces
        # in the path. Java side just uses Files.readAllBytes() on the path verbatim.
        resp = await self._cmd(f"SIGN_FILE {path}")
        if not resp.ok:
            raise KalkanSignerError(f"sign_file failed: {resp.error}")
        return resp.payload

    async def sign_bytes(self, data: bytes) -> str:
        """Sign raw bytes. Returns base64 CMS PKCS#7 detached (no TSA)."""
        b64 = base64.b64encode(data).decode("ascii")
        resp = await self._cmd(f"SIGN_BYTES {b64}")
        if not resp.ok:
            raise KalkanSignerError(f"sign_bytes failed: {resp.error}")
        return resp.payload

    async def sign_text_with_tsa(self, text: str) -> str:
        """Sign UTF-8 text with embedded RFC 3161 TSA timestamp.

        Used for price-offer XML. The TSA hit (~50-300ms RTT to tsp.pki.gov.kz)
        is included in this call -- Kalkan does the network request internally.
        """
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        resp = await self._cmd(f"SIGN_TEXT_TSA {b64}")
        if not resp.ok:
            raise KalkanSignerError(f"sign_text_with_tsa failed: {resp.error}")
        return resp.payload

    async def sign_xml(self, xml: str) -> str:
        """Wrap an XML document with an XML-DSig signature (XMLUtil.createXmlSignature).

        Used for goszakup login -- the auth challenge is XML, the server expects
        an XML envelope back with a <ds:Signature> block embedded.
        """
        b64 = base64.b64encode(xml.encode("utf-8")).decode("ascii")
        resp = await self._cmd(f"SIGN_XML {b64}")
        if not resp.ok:
            raise KalkanSignerError(f"sign_xml failed: {resp.error}")
        return base64.b64decode(resp.payload).decode("utf-8")


# ---------------------------------------------------------------------------
# Pipeline-facing wrapper: same interface as NursignClient, no popup, no UI.
# ---------------------------------------------------------------------------

class LocalSigner:
    """Drop-in replacement for ``goszakup.nursign.NursignClient``.

    Internally uses KalkanSigner (Java subprocess + Kalkan provider) to do
    KZ GOST signing without NCALayer's popup. Files are downloaded via the
    same httpx client the pipeline uses (so authenticated cookies travel),
    then handed to Kalkan as raw bytes.

    Why we don't ask Kalkan to download itself: Kalkan would do an
    unauthenticated GET and goszakup would 403. We have to fetch with the
    ci_session cookie attached.
    """

    def __init__(
        self,
        *,
        http_client,  # httpx.AsyncClient -- typed loose to avoid import cycle
        p12_path: Optional[str] = None,
        password: Optional[str] = None,
        kalkan_signer: Optional["KalkanSigner"] = None,
    ):
        if kalkan_signer is not None:
            # External signer (e.g. the bot's long-lived one); don't own lifecycle.
            self._kalkan = kalkan_signer
            self._owns_kalkan = False
        else:
            if not p12_path or password is None:
                raise ValueError("LocalSigner needs either kalkan_signer or (p12_path, password)")
            self._kalkan = KalkanSigner(p12_path=p12_path, password=password)
            self._owns_kalkan = True
        self._http = http_client
        self._range_tasks: set[asyncio.Task] = set()

    async def __aenter__(self) -> "LocalSigner":
        if self._owns_kalkan:
            await self._kalkan.start()
        log.info("local_signer.ready", owns_kalkan=self._owns_kalkan)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_kalkan:
            await self._kalkan.close()

    def _observe_range_task(self, task: asyncio.Task) -> None:
        self._range_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _get_file_suffix(
        self, url: str, headers: dict[str, str], offset: int, size: int, etag: str,
    ) -> bytes | None:
        last = size - 1
        length = size - offset
        request_headers = {
            **headers,
            "Accept-Encoding": "identity",
            "Range": f"bytes={offset}-{last}",
            "If-Range": etag,
        }
        try:
            request = self._http.build_request("GET", url, headers=request_headers)
            # Use the same HTTP pool and outbound cookies. A speculative Range
            # response must never rotate the cookie jar used for draft writes.
            transport = self._http._transport_for_url(request.url)
            response = await transport.handle_async_request(request)
            response.request = request
            async with aclosing(response):
                if (
                    response.status_code != 206
                    or response.headers.get("content-range", "").strip().lower()
                    != f"bytes {offset}-{last}/{size}"
                    or response.headers.get("etag", "").strip() != etag
                    or response.headers.get("content-encoding", "identity").strip().lower() != "identity"
                    or response.headers.get("content-length", "").strip() != str(length)
                    or response.headers.get("content-type", "").lower().startswith("multipart/byteranges")
                ):
                    return None
                if response.is_stream_consumed:
                    return response.content if len(response.content) == length else None
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > length:
                        return None
                    data.extend(chunk)
                return bytes(data) if len(data) == length else None
        except Exception as exc:
            # The full GET remains in flight when this optional request fails.
            log.debug("local_signer.range.fallback", error_type=type(exc).__name__)
            return None

    async def _race_file_suffix(
        self, response: httpx.Response, url: str, headers: dict[str, str], size: int, etag: str,
    ) -> bytes:
        offset = max(size // 2, size - _SPEC_RANGE_MAX_SUFFIX_BYTES)
        suffix = asyncio.create_task(self._get_file_suffix(url, headers, offset, size, etag))
        self._range_tasks.add(suffix)
        suffix.add_done_callback(self._observe_range_task)
        tail = None
        original = bytearray()
        chunks = response.aiter_raw()

        async def read_original_tail() -> bytes:
            async for chunk in chunks:
                original.extend(chunk)
            return bytes(original)

        try:
            async for chunk in chunks:
                original.extend(chunk)
                if len(original) >= offset:
                    break
            else:
                return bytes(original)

            if len(original) >= size or (suffix.done() and suffix.result() is None):
                return await read_original_tail()

            tail = asyncio.create_task(read_original_tail())
            done, _ = await asyncio.wait((tail, suffix), return_when=asyncio.FIRST_COMPLETED)
            if tail in done:
                return tail.result()
            suffix_bytes = suffix.result()
            if suffix_bytes is None:
                return await tail
            log.info("local_signer.range.used", bytes=size, suffix_bytes=len(suffix_bytes))
            return bytes(original[:offset]) + suffix_bytes
        finally:
            # Keep a losing suffix tracked until its response closes, but let
            # signing start without waiting on auxiliary transport cleanup.
            if not suffix.done():
                suffix.cancel()
            if tail is not None:
                if not tail.done():
                    tail.cancel()
                # The original response still belongs to this call. Stop its
                # reader before the enclosing response context closes.
                await asyncio.gather(tail, return_exceptions=True)

    async def _download_file(self, url: str, headers: dict[str, str], immutable: bool) -> bytes:
        if (
            immutable and os.environ.get("ATAGOS_SPEC_RANGE_DOWNLOAD") == "1"
            and isinstance(self._http, httpx.AsyncClient)
        ):
            async with self._http.stream("GET", url, headers=headers) as response:
                size, etag, reason = _range_eligibility(response)
                log.info("local_signer.range.eligibility", source="enabled", ok=bool(etag),
                         verdict=reason, bytes=size)
                if etag:
                    if response.is_stream_consumed:
                        data = await response.aread()
                    else:
                        data = await self._race_file_suffix(response, url, headers, size, etag)
                    if len(data) != size:
                        raise httpx.ReadError("unexpected document length", request=response.request)
                    return data
                await response.aread()
        else:
            response = await self._http.get(url, headers=headers)
            if immutable:
                size, etag, reason = _range_eligibility(response)
                log.info("local_signer.range.eligibility", source="disabled", ok=bool(etag),
                         verdict=reason, bytes=size)
        if response.status_code >= 400:
            log.warning("local_signer.fetch.error", status=response.status_code)
        response.raise_for_status()
        return response.content

    async def sign_file(self, download_url: str, *, retries: int = 1,
                        sig_cache: Optional[dict] = None,
                        referer: Optional[str] = None,
                        cache_key: Optional[str] = None) -> str:
        """Mirror of ``NursignClient.sign_file``.

        Downloads the file (with our session cookies) and signs the bytes
        with KZ GOST. Returns base64 CMS PKCS#7 detached (no TSA), exactly
        the shape goszakup's ``signature[file_id]=...`` POST field expects.

        sig_cache: {key -> signature_b64}, keys namespaced by the signing key
        (see KalkanSigner.key_id). Two lookups:
          * ``cache_key`` (e.g. "fid:123") — checked BEFORE the download. A hit
            skips the download entirely; this is what makes the cache worth
            having, because fetching a multi-MB тех.спец file is the slow part
            (signing it is ~50ms). Pass it only for files that never change
            under the same id (the customer's tender documents).
          * sha256 of the content — checked after the download; a hit skips
            only the Kalkan call.

        referer: value for the Referer header. goszakup's /files/download_file/
        checks the referring page for a freshly-generated document; without it
        the endpoint returns 403 "Доступ к документу запрещен". NCALayer's
        remote-download path set this implicitly (browser context); our httpx
        download must set it explicitly.
        """
        import hashlib
        del retries
        ns_prefix = f"{getattr(self._kalkan, 'key_id', '') or 'key'}:"
        fast_key = f"{ns_prefix}{cache_key}" if cache_key else None
        if sig_cache is not None and fast_key and fast_key in sig_cache:
            log.info("local_signer.sig_cache.hit_no_download", key=cache_key)
            return sig_cache[fast_key]
        url = download_url
        if hasattr(self._http, "base_url") and str(self._http.base_url):
            base = str(self._http.base_url).rstrip("/")
            if url.startswith(base):
                url = url[len(base):]
        headers = {"Referer": referer} if referer else {}
        source = urlsplit(url)
        file_path = re.fullmatch(r"/files/download_file/(\d+)(?:/\d+)?/", source.path)
        immutable = bool(not (source.scheme or source.netloc or source.query) and file_path
                         and cache_key == f"fid:{file_path[1]}")
        log.info("local_signer.fetch", url=url)
        data = await self._download_file(url, headers, immutable)
        log.info("local_signer.fetch.done", bytes=len(data))
        content_hash = hashlib.sha256(data).hexdigest()
        content_key = f"{ns_prefix}{content_hash}"
        if sig_cache is not None and content_key in sig_cache:
            log.info("local_signer.sig_cache.hit", url=url, sha256=content_hash[:16])
            sig = sig_cache[content_key]
        else:
            sig = await self._kalkan.sign_bytes(data)
            if sig_cache is not None:
                sig_cache[content_key] = sig
                log.info("local_signer.sig_cache.stored", url=url, sha256=content_hash[:16])
        if sig_cache is not None and fast_key:
            sig_cache[fast_key] = sig
        return sig

    async def sign_xml_batch(self, items, *, retries: int = 1) -> dict[str, str]:
        """Mirror of ``NursignClient.sign_xml_batch``.

        Signs each XML with TSA timestamp embedded (CAdES-T). Serial to keep
        the helper protocol simple; Kalkan/TSA RTT is ~150-200ms per item
        which is fine for the 1-3 lot points a typical bid has.
        """
        del retries
        out: dict[str, str] = {}
        for key, xml in items.items():
            out[key] = await self._kalkan.sign_text_with_tsa(xml)
        return out

    async def sign_xml(self, xml: str) -> str:
        """Passthrough to the underlying Kalkan helper.

        Used by GoszakupClient.login() for the XML-DSig challenge.
        """
        return await self._kalkan.sign_xml(xml)
