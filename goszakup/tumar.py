"""TumarCSP client — drives the local Гамма Технологии crypto provider that
seals goszakup price offers (the "gamma" price-encryption scheme).

Why this exists
---------------
On goszakup, signing is done by NCALayer/NURSign (which we replace with the
Kalkan Java helper — see local_signer.py). But the *price offer* on gamma
tenders is **encrypted**, not just signed, and that encryption is done by a
*different* desktop provider: **TumarCSP** (ТОО «Гамма Технологии»), exposed
over a local WebSocket at ``wss://127.0.0.1:6127/tumarcsp/``.

The browser flow we replicate (captured verbatim from a real submission HAR):

    1. SetAPIKey   — register the EFC licence key (no popup)
    2. GetVersion  — returns e.g. "1.0.13.2285" (echoed to ajax_get_encr_info)
    3. EncryptOfferPrice(pl_sum, d_sum, public_key, sign, salt, id_priceoffer,
                         messages) -> {encryptData, encryptKey, sn, sign}

``EncryptOfferPrice`` is a proprietary, vendor-specific function: the price is
sealed with the per-tender EFC recipient certificate so only the tender
commission can decrypt it at opening time. We do **not** reimplement it — we
drive the real provider, exactly like the browser. This means **TumarCSP must
be installed and running on whatever host runs the bot.**

The output maps to the ``ajax_add_encrypt`` POST like so:

    encryptedData = encryptData        (also the priceoffer "price" value and
                                        the xmlData signed for ajax_save_gamma_signs)
    sessionKey    = encryptKey
    salt          = salt               (the *input* salt, echoed back)
    info          = sn                 (recipient cert serial number)
    sign          = sign               (output integrity tag)
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Callable, Optional

import structlog
import websockets
from websockets.protocol import State

from .timing import trace_event

log = structlog.get_logger("tumar")

DEFAULT_WS_URL = "wss://127.0.0.1:6127/tumarcsp/"

# --- process-global price-popup gate -----------------------------------------
# The local provider has one price dialog per active request. Serialize requests
# process-wide so each fill belongs to its offer, with a small gap after each.
_POPUP_LOCK = asyncio.Lock()
_POPUP_MIN_SPACING_S = 1.5
_AUTO_POPUP_TIMEOUT_S = 6.0
_popup_last_release = 0.0


@asynccontextmanager
async def _popup_gate(label: str = ""):
    """Hold the global popup gate: exclusive + spaced. `label` (id_priceoffer)
    ties each acquisition to the exact price/offer it is filling, for the log."""
    global _popup_last_release
    queued_at = time.monotonic()
    async with _POPUP_LOCK:
        loop = asyncio.get_event_loop()
        wait = _POPUP_MIN_SPACING_S - (loop.time() - _popup_last_release)
        if wait > 0:
            log.info("tumar.popup_gate.wait", spacing_s=round(wait, 2), label=label)
            await asyncio.sleep(wait)
        if time.monotonic() - queued_at >= 0.001:
            trace_event("wait", "Очередь окна TumarCSP", queued_at)
        log.info("tumar.popup_gate.enter", label=label)
        try:
            yield
        finally:
            _popup_last_release = asyncio.get_event_loop().time()
            log.info("tumar.popup_gate.exit", label=label)


class TumarError(RuntimeError):
    """TumarCSP returned an error or is unreachable."""


def _encrypted_reply(raw: str) -> dict:
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TumarError("TumarCSP вернул некорректный ответ на шифрование цены.") from exc
    if (not isinstance(result, dict) or str(result.get("result")).lower() != "true"
            or not result.get("encryptData") or not result.get("encryptKey")):
        raise TumarError(f"TumarCSP отклонил шифрование цены: {result}")
    return result


@dataclass
class EncryptResult:
    """Result of EncryptOfferPrice, named to match the ajax_add_encrypt fields."""

    encrypted_data: str   # -> encryptedData / price value / xmlData
    session_key: str      # -> sessionKey
    info: str             # -> info  (the "sn" field; recipient cert serial)
    sign: str             # -> sign
    code: str = "0"


@dataclass
class EncrInfo:
    """The pieces ajax_get_encr_info hands the client to feed EncryptOfferPrice.

    Field names mirror the EncryptOfferPrice ``Param`` keys so the page→provider
    hand-off stays one-to-one. ``ajax_get_encr_info`` returns these; we forward
    them. ``pl_sum`` is the ceiling, ``d_sum`` is the dumping floor we bid.
    """

    sign: str
    salt: str
    pl_sum: float
    d_sum: float
    id_priceoffer: str
    # Newer ajax_get_encr_info returns a composite ``info`` ("<id>;<pl>;<min>;")
    # and NO public_key; older format returned a per-tender public_key cert.
    # Both are forwarded if present.
    info: str = ""
    public_key: Optional[str] = None
    message_up: str = ""
    message_down: str = ""


def _ssl_context() -> ssl.SSLContext:
    # TumarCSP serves a self-signed cert on localhost — the browser is told to
    # trust it out of band. We connect to 127.0.0.1 only, so skip verification.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TumarCSP:
    """Async client for the local TumarCSP WebSocket.

    Short-lived: connect, SetAPIKey, GetVersion, EncryptOfferPrice, close.
    A fresh connection per price-encryption keeps the protocol stateless and
    matches what the browser does (it opens a new socket each time).
    """

    # TumarCSP only processes frames from a goszakup page — it checks the
    # WebSocket handshake Origin. Without it the socket upgrades (101) but our
    # SetAPIKey is silently ignored and recv() hangs. Match the browser exactly.
    DEFAULT_ORIGIN = "https://v3bl.goszakup.gov.kz"
    BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        api_key: str,
        ws_url: str = DEFAULT_WS_URL,
        origin: str = DEFAULT_ORIGIN,
        timeout_s: float = 15.0,
        encrypt_timeout_s: float = 300.0,
        autofill: Optional[Callable[..., bool]] = None,
    ):
        self._api_key = api_key
        self._ws_url = ws_url
        self._origin = origin
        self._timeout_s = timeout_s
        # Optional callback(price)->bool that fills the TumarCSP price popup
        # (see popup_filler.py). Runs in a worker thread while we await the
        # WS reply. If None or it fails, the operator types the price by hand.
        self._autofill = autofill
        # The supported interactive path allows time for manual confirmation.
        # The built-in Windows bot uses its own shorter deadline.
        self._encrypt_timeout_s = encrypt_timeout_s
        self._connect_lock = asyncio.Lock()
        self._ws = None
        self.version: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state == State.OPEN

    async def ensure_connected(self) -> None:
        """Reconnect if the provider closed the socket while waiting for the portal."""
        async with self._connect_lock:
            if not self.connected:
                await self.close()
                await self.connect()

    async def __aenter__(self) -> "TumarCSP":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        started = time.monotonic()
        try:
            self._ws = await websockets.connect(
                self._ws_url,
                ssl=_ssl_context(),
                origin=self._origin,            # <-- TumarCSP gates on this
                user_agent_header=self.BROWSER_UA,
                open_timeout=self._timeout_s,
                ping_interval=None,
            )
        except Exception as e:  # noqa: BLE001 — surface a precise, actionable error
            raise TumarError(
                f"cannot reach TumarCSP at {self._ws_url}: {e}. "
                "Is the Гамма Технологии provider (TumarCSP) installed and running "
                "on this host? It is required to encrypt gamma price offers."
            ) from e
        finally:
            trace_event("tumar", "Подключение WebSocket TumarCSP", started)
        try:
            await self._set_api_key()
            self.version = await self._get_version()
        except BaseException:
            # A failed or cancelled __aenter__ never reaches __aexit__.
            with suppress(Exception):
                await self.close()
            raise
        log.info("tumar.ready", version=self.version)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    async def _rpc(self, payload: dict, *, timeout: Optional[float] = None) -> dict:
        assert self._ws is not None, "TumarCSP not connected"
        started = time.monotonic()
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout or self._timeout_s)
        finally:
            trace_event("tumar", f"TumarCSP {payload['Function']}", started)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise TumarError(f"non-JSON reply from TumarCSP: {raw[:200]!r}") from e

    async def _set_api_key(self) -> None:
        resp = await self._rpc({
            "TumarCSP": "SYSAPI",
            "Function": "SetAPIKey",
            "Param": {"apiKey": self._api_key},
        })
        if str(resp.get("result")).lower() != "true":
            raise TumarError(f"SetAPIKey failed: {resp}")

    async def _get_version(self, kind: int = 3) -> str:
        resp = await self._rpc({
            "TumarCSP": "BaseAPI",
            "Function": "GetVersion",
            "Param": {"type": kind},
        })
        if str(resp.get("result")).lower() != "true":
            raise TumarError(f"GetVersion failed: {resp}")
        return str(resp.get("response", ""))

    async def _finish_popup(self, started, stop, fill_task, *, cleanup_s: float = 5.0) -> None:
        stop.set()
        trace_event("tumar", "EncryptOfferPrice (окно цены)", started)
        log.info("tumar.encrypt.popup_done", dt_s=round(time.monotonic() - started, 3))
        if fill_task is not None:
            cleanup_started = time.monotonic()
            try:
                ok = await asyncio.wait_for(asyncio.shield(fill_task), timeout=cleanup_s)
                log.info("tumar.encrypt.autofill.result", ok=ok)
            except Exception:
                fill_task.cancel()
            finally:
                trace_event("tumar", "Завершение автозаполнения", cleanup_started)

    async def _request_price_automatically(self, ws, payload: dict, price: float) -> dict:
        started = time.monotonic()
        stop = threading.Event()
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        fill = asyncio.create_task(asyncio.to_thread(
            self._autofill, price, stop, ready=lambda: loop.call_soon_threadsafe(ready.set),
        ))
        baseline = asyncio.create_task(ready.wait())
        reply = None
        complete = False

        async def receive() -> dict:
            raw = await ws.recv()
            log.info("tumar.encrypt.provider_reply", elapsed_s=round(time.monotonic() - started, 3))
            return _encrypted_reply(raw)

        try:
            async with asyncio.timeout(_AUTO_POPUP_TIMEOUT_S):
                await asyncio.wait((baseline, fill), return_when=asyncio.FIRST_COMPLETED)
                if fill.done():
                    raise TumarError("Не удалось подготовить поиск окна цены TumarCSP.")
                log.info("tumar.encrypt.autofill.start")
                await ws.send(json.dumps(payload, ensure_ascii=False))
                reply = asyncio.create_task(receive())
                done, _ = await asyncio.wait((reply, fill), return_when=asyncio.FIRST_COMPLETED)
                encrypted = reply.result() if reply in done else None
                try:
                    confirmed = await asyncio.shield(fill)
                except Exception as exc:
                    raise TumarError("Ошибка при автоматическом вводе цены TumarCSP.") from exc
                if not confirmed:
                    raise TumarError("Бот не смог подтвердить точную цену в окне TumarCSP.")
                log.info("tumar.encrypt.autofill.confirmed", elapsed_s=round(time.monotonic() - started, 3))
                if encrypted is None:
                    encrypted = await reply
                complete = True
                return encrypted
        except TimeoutError as exc:
            raise TumarError("Истекло время автоматического подтверждения цены TumarCSP.") from exc
        finally:
            stop.set()
            if not complete and self._ws is ws:
                self._ws = None
            try:
                baseline.cancel()
                await asyncio.gather(baseline, return_exceptions=True)
                if reply is not None:
                    reply.cancel()
                    await asyncio.gather(reply, return_exceptions=True)
                await self._finish_popup(started, stop, fill, cleanup_s=1.0)
            finally:
                if not complete:
                    with suppress(Exception):
                        await asyncio.wait_for(ws.close(), timeout=1.0)

    async def encrypt_offer_price(self, info: EncrInfo) -> EncryptResult:
        """Seal one lot-point price. ``info`` comes straight from ajax_get_encr_info.

        We bid ``info.d_sum`` — the dumping floor, i.e. the minimum allowed
        price. (Anything above it loses the tender, per the operator's hard
        rule; see EncryptOfferPrice's d_messageDown / d_messageUp guards.)
        """
        param: dict = {
            "pl_sum": info.pl_sum,
            "d_sum": info.d_sum,
            "d_messageUp": info.message_up,
            "d_messageDown": info.message_down,
            "id_priceoffer": info.id_priceoffer,
            "sign": info.sign,
            "salt": info.salt,
        }
        # public_key is REQUIRED by EncryptOfferPrice (it errors "No such node
        # (public_key)" without it). The page keeps it in a hidden input, not in
        # ajax_get_encr_info. The inline script sends exactly these keys — no
        # `info` field — so we match it 1:1.
        if info.public_key:
            param["public_key"] = info.public_key
        log.info("tumar.encrypt.param_keys", keys=sorted(param),
                 has_public_key=bool(info.public_key))

        # EncryptOfferPrice opens the provider's dialog. The built-in Windows
        # filler verifies the field; explicit interactive callers can enter it.
        payload = {"TumarCSP": "EFCAPI", "Function": "EncryptOfferPrice", "Param": param}

        # Hold the global popup gate across send -> fill -> recv: the popup is on
        # screen for this whole window, so no other tender may drive its own
        # popup meanwhile (and the next one waits out the spacing). Each gate
        # acquisition types exactly THIS offer's d_sum.
        async with _popup_gate(label=info.id_priceoffer):
            ws = self._ws
            if ws is None:
                raise TumarError("Соединение с TumarCSP закрыто. Запусти новую попытку.")
            if getattr(self._autofill, "requires_confirmation", False):
                resp = await self._request_price_automatically(ws, payload, info.d_sum)
            else:
                resp = await self._request_price_interactively(ws, payload, info)
        log.info(
            "tumar.encrypted",
            id_priceoffer=info.id_priceoffer,
            d_sum=info.d_sum,
            pl_sum=info.pl_sum,
            code=resp.get("code"),
        )
        return EncryptResult(
            encrypted_data=resp["encryptData"],
            session_key=resp["encryptKey"],
            info=resp.get("sn", ""),
            sign=resp.get("sign", ""),
            code=str(resp.get("code", "0")),
        )

    async def _request_price_interactively(self, ws, payload: dict, info: EncrInfo) -> dict:
        # Start the filler BEFORE EncryptOfferPrice so it is already
        # polling when the dialog appears — otherwise the first paint
        # is often missed and the operator has to type by hand.
        fill_task = None
        # Lets us tell the filler thread "the popup is done, stop looking".
        fill_stop = threading.Event()
        t_popup_mono = time.monotonic()
        try:
            if self._autofill is not None:
                log.info("tumar.encrypt.autofill.start", price=info.d_sum,
                         id_priceoffer=info.id_priceoffer)
                if getattr(self._autofill, "supports_stop", False):
                    fill_call = asyncio.to_thread(self._autofill, info.d_sum, fill_stop)
                else:
                    fill_call = asyncio.to_thread(self._autofill, info.d_sum)
                fill_task = asyncio.ensure_future(fill_call)
                await asyncio.sleep(0.05)
            else:
                log.info("tumar.encrypt.waiting_for_popup",
                         hint="type the price and click OK in the TumarCSP popup")

            await ws.send(json.dumps(payload, ensure_ascii=False))
            raw = await asyncio.wait_for(ws.recv(), timeout=self._encrypt_timeout_s)
        finally:
            await self._finish_popup(t_popup_mono, fill_stop, fill_task)
        return _encrypted_reply(raw)
