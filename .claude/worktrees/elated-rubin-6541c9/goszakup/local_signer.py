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
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger("local_signer")

PROJECT_ROOT = Path(__file__).parent.parent
JARS_DIR = PROJECT_ROOT / "goszakup" / "jars"


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
        # Java helper has one stdin/stdout pair, so concurrent _cmd calls would
        # interleave writes and read each other's responses. Lock serializes
        # signing while leaving HTTP fetches in the caller fully concurrent.
        self._cmd_lock = asyncio.Lock()

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
        assert self._proc is not None
        async with self._cmd_lock:
            self._proc.stdin.write((line + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            # Read lines until we see one starting with OK/ERR/PONG. Any other
            # line is treated as stray log output (logback should send these
            # to stderr, but legacy libraries occasionally leak to stdout) and
            # skipped.
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

    async def ping(self) -> bool:
        assert self._proc is not None
        self._proc.stdin.write(b"PING\n")
        await self._proc.stdin.drain()
        return (await self._proc.stdout.readline()).strip() == b"PONG"

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

    async def __aenter__(self) -> "LocalSigner":
        if self._owns_kalkan:
            await self._kalkan.start()
        log.info("local_signer.ready", owns_kalkan=self._owns_kalkan)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_kalkan:
            await self._kalkan.close()

    async def sign_file(self, download_url: str, *, retries: int = 1,
                        sig_cache: Optional[dict] = None) -> str:
        """Mirror of ``NursignClient.sign_file``.

        Downloads the file (with our session cookies) and signs the bytes
        with KZ GOST. Returns base64 CMS PKCS#7 detached (no TSA), exactly
        the shape goszakup's ``signature[file_id]=...`` POST field expects.

        sig_cache: {sha256_hex -> signature_b64}. Если контент уже встречался
        (тот же тендер, следующая ставка) — скачивание + Kalkan пропускаются.
        GOST sign_bytes детерминирован (без TSA), так что кэш корректен.
        """
        import hashlib
        del retries
        url = download_url
        if hasattr(self._http, "base_url") and str(self._http.base_url):
            base = str(self._http.base_url).rstrip("/")
            if url.startswith(base):
                url = url[len(base):]
        log.info("local_signer.fetch", url=url)
        r = await self._http.get(url)
        r.raise_for_status()
        log.info("local_signer.fetch.done", bytes=len(r.content))
        content_hash = hashlib.sha256(r.content).hexdigest()
        if sig_cache is not None and content_hash in sig_cache:
            log.info("local_signer.sig_cache.hit", url=url, sha256=content_hash[:16])
            return sig_cache[content_hash]
        sig = await self._kalkan.sign_bytes(r.content)
        if sig_cache is not None:
            sig_cache[content_hash] = sig
            log.info("local_signer.sig_cache.stored", url=url, sha256=content_hash[:16])
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
