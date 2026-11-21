"""Async client for NCALayer's NURSign plugin (WebSocket).

NCALayer listens on wss://127.0.0.1:13579/ with a self-signed cert. The
NURSign module supports two signing modes (both observed in real traffic):

    binary    -- NCALayer downloads a file from upload_url, signs the bytes,
                 returns base64 CMS PKCS#7 detached signature (no TSA timestamp).
                 Used for document signatures (Заявка, Перечень категорий, ТЗ,
                 Декларация).

    multitext -- Client sends {id: xml_string, ...}, NCALayer signs each XML,
                 returns CMS signatures WITH TSA timestamp embedded.
                 Used for price offers (ajax_add_priceoffers).

Protocol (one full sign cycle, observed in HAR):

    open wss://127.0.0.1:13579/
    <- {"result":{"version":"1.4"}}                       (server greeting)
    -> {"module":"NURSign","type":"version"}
    <- {"result":{"version":"1.0.0"},"errorCode":"NONE"}  (plugin loaded)
    -> {"module":"NURSign","type":"binary","upload_url":"...","source":"remote"}
    <- {"result":"<base64 CMS>"}                          (signed)
       or {"errorMessage":"Неверный пароль","errorCode":"500"}

We keep one WS connection open for the whole submission flow on the
assumption that NCALayer caches the password within a session. This is
not yet verified -- see TODO task #7. If wrong, behavior degrades to
a popup per signature (still no worse than the legacy script).
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Mapping, Optional

import structlog
import websockets

log = structlog.get_logger("nursign")

NCALAYER_URL = "wss://127.0.0.1:13579/"


class NursignError(Exception):
    """NURSign returned an error response."""


def _ssl_ctx_insecure() -> ssl.SSLContext:
    # NCALayer ships a self-signed cert bound to 127.0.0.1.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class NursignClient:
    """Persistent WebSocket connection to NCALayer/NURSign.

    Usage:
        async with NursignClient() as ns:
            sig1 = await ns.sign_file("https://.../files/download_file/123/")
            batch = await ns.sign_xml_batch({"p1": "<xml/>", "p2": "<xml/>"})
    """

    def __init__(self, url: str = NCALAYER_URL, recv_timeout_s: float = 300.0):
        self.url = url
        self.recv_timeout_s = recv_timeout_s
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

    async def connect(self, *, origin: str = "https://v3bl.goszakup.gov.kz") -> None:
        log.info("ncalayer.connect", url=self.url, origin=origin)
        # NCALayer validates Origin header on WS handshake. The browser sends
        # Origin: https://v3bl.goszakup.gov.kz automatically; we have to set it
        # explicitly or NCALayer drops the connection right after the greeting.
        self._ws = await websockets.connect(
            self.url,
            ssl=_ssl_ctx_insecure(),
            origin=origin,
            additional_headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
                ),
            },
            max_size=10 * 1024 * 1024,  # CMS+TSA payload can be ~6KB but allow headroom
        )

        greeting = await self._recv_json()
        version = greeting.get("result", {}).get("version")
        if not version:
            raise NursignError(f"unexpected NCALayer greeting: {greeting!r}")
        log.info("ncalayer.greeted", version=version)

        await self._send({"module": "NURSign", "type": "version"})
        resp = await self._recv_json()
        if resp.get("errorCode") != "NONE":
            raise NursignError(f"NURSign plugin not available: {resp!r}")
        log.info("nursign.ready", plugin_version=resp.get("result", {}).get("version"))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
            log.info("ncalayer.closed")

    async def __aenter__(self) -> "NursignClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _send(self, payload: dict) -> None:
        assert self._ws is not None, "call connect() first"
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _recv_json(self) -> dict:
        assert self._ws is not None, "call connect() first"
        raw = await asyncio.wait_for(self._ws.recv(), timeout=self.recv_timeout_s)
        return json.loads(raw)

    async def sign_file(self, download_url: str, *, retries: int = 5) -> str:
        """Ask NCALayer to download a file and sign it (binary/remote mode).

        Returns base64 CMS PKCS#7 detached signature WITHOUT TSA timestamp.
        This is what `signature[file_id]=...` POST bodies expect.

        Retries on "Неверный пароль" -- the user may mistype the popup. Each
        retry re-sends the same request; NCALayer will re-prompt.
        """
        request = {
            "module": "NURSign",
            "type": "binary",
            "upload_url": download_url,
            "source": "remote",
        }

        for attempt in range(1, retries + 1):
            log.info("nursign.sign_file.attempt", url=download_url, attempt=attempt)
            await self._send(request)
            resp = await self._recv_json()

            if isinstance(resp.get("result"), str):
                sig = resp["result"]
                log.info("nursign.sign_file.ok", url=download_url, sig_len=len(sig))
                return sig

            err = resp.get("errorMessage") or resp.get("errorCode") or str(resp)
            if "пароль" in str(err).lower() and attempt < retries:
                log.warning("nursign.password_retry", attempt=attempt, err=err)
                continue
            raise NursignError(f"sign_file failed: {err}")

        raise NursignError(f"sign_file exhausted {retries} retries")

    async def sign_xml_batch(
        self, items: Mapping[str, str], *, retries: int = 5
    ) -> dict[str, str]:
        """Sign multiple XML payloads in one call (multitext mode + TSA).

        items: {point_id: xml_string}
        Returns: {point_id: base64_cms_pkcs7_with_tsa_timestamp}

        Used for ajax_add_priceoffers, where each lot point requires:
            xmlData[point_id] = <xml string>
            signData[point_id] = <this signature>
        """
        request = {
            "module": "NURSign",
            "type": "multitext",
            "data": dict(items),
        }

        for attempt in range(1, retries + 1):
            log.info("nursign.sign_xml_batch.attempt", n=len(items), attempt=attempt)
            await self._send(request)
            resp = await self._recv_json()

            result = resp.get("result")
            if isinstance(result, dict) and "items" in result:
                sigs = result["items"]
                log.info("nursign.sign_xml_batch.ok", n=len(sigs))
                return sigs

            err = resp.get("errorMessage") or resp.get("errorCode") or str(resp)
            if "пароль" in str(err).lower() and attempt < retries:
                log.warning("nursign.password_retry", attempt=attempt, err=err)
                continue
            raise NursignError(f"sign_xml_batch failed: {err}")

        raise NursignError(f"sign_xml_batch exhausted {retries} retries")
