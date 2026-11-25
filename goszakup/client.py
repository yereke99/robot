"""Async HTTP client for goszakup.gov.kz AJAX endpoints.

One async httpx client per submission. HTTP/2 + keep-alive so the same
TCP connection serves all ~22 POSTs in the hot path. No browser, no Playwright.

Endpoints map (observed in HAR, in order of flow):

   01  POST  ajax_create_application/{ann}                   -> {status, app_id?}
   02  POST  ajax_add_lots/{ann}/{app}                       selectLots[]=N
   03  POST  ajax_lots_next/{ann}/{app}                      next=1&confirmed=0

   --  GET   show_doc/{ann}/{app}/{doc}                      (parse file_id)
   04  POST  show_doc/{ann}/{app}/{doc}                      sign=Y&signature[fid]=B64
   --  GET   sign_task/{ann}/{app}/{lot}/{doc}               (parse file_id list)
   05  POST  sign_task/{ann}/{app}/{lot}/{doc}               sign_task=Y&signature[fid]=B64

   06  POST  copy_data_docs/{ann}/{app}/{doc}                anno_number=... [3 steps]
   07  POST  show_doc/{ann}/{app}/3240                       bankruptcy=2&btn=docGen
   08  POST  show_doc/{ann}/{app}/3240                       btn=docSave&signature[fid]=B64
   09  POST  show_doc/{ann}/{app}/719/{lot}/{n}              typeDoc=3
   10  POST  show_doc/{ann}/{app}/719/{lot}/{n}/?show=3      typeDoc=3&save_electronic_data=...

   11  POST  ajax_docs_next/{ann}/{app}                      next=1
   12  POST  ajax_check_priceoffers/{ann}/{app}              offer[lot][pt][price]=N
   13  POST  ajax_add_priceoffers/{ann}/{app}                xmlData[pt]=...&signData[pt]=B64+TSA
   14  POST  ajax_priceoffers_next/{ann}/{app}               offer[lot][pt][price]=N
   15  POST  ajax_public_application/{ann}/{app}             public_app=Y&agree_*=false

Login is NOT here -- ci_session cookie is provided externally (via config
or `login` subcommand using Playwright once per session). Authentication
isn't on the hot path and reversing the ECP login challenge is Phase 2 work.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence
from urllib.parse import urlencode

import httpx
import structlog

from . import timing
from .hedge import HedgedRead, first_of
from .timing import HttpTiming, current_trace

log = structlog.get_logger("client")

DEFAULT_BASE_URL = "https://v3bl.goszakup.gov.kz"

# Most creates in the timing reports take over two seconds. One opt-in read
# near the end can pick up the draft if the portal commits before answering.
_EARLY_ID_PROBE_DELAY_S = 1.8

_SHAPE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,15}")
_SHAPE_WORD = re.compile(r"[A-Za-z_]{1,12}")


def reply_shape(response) -> str:
    """Text-free outline of a small AJAX reply for the bid report.

    ``status-ok``, ``status-0.message``, ``http302``, ``html``. Only key names
    and short codes are kept — never free text, ids or tokens — so the exact
    success/failure form of an endpoint can be read from a report without the
    body ever leaving the host.
    """
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code >= 300:
        return f"http{status_code}"
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        return "unknown"
    try:
        body = json.loads(text)
    except ValueError:  # HTML, empty or truncated body
        text = text.lstrip()
        return "html" if text.startswith("<") else ("text" if text else "empty")
    if isinstance(body, list):
        return f"list{min(len(body), 999)}"
    if not isinstance(body, dict):
        return "scalar"
    parts = []
    for key, value in list(body.items())[:6]:
        if not isinstance(key, str) or not _SHAPE_KEY.fullmatch(key):
            parts.append("key")
            continue
        if isinstance(value, bool):
            parts.append(f"{key}-{str(value).lower()}")
        elif isinstance(value, int) and -9 <= value <= 99:
            parts.append(f"{key}-{value}")
        elif isinstance(value, str) and _SHAPE_WORD.fullmatch(value):
            parts.append(f"{key}-{value}")
        elif isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,2}", value):
            parts.append(f"{key}-{value}")
        else:
            parts.append(key)
    return ".".join(parts)[:40] or "empty"


def _ajax_headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": DEFAULT_BASE_URL,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


class SessionExpired(RuntimeError):
    """Server redirected us to /user/login — the ci_session cookie is dead.

    Subclasses RuntimeError so legacy `except RuntimeError` blocks keep
    catching it. Bot layer catches this specifically to trigger one
    auto-relogin and a retry.
    """


class AlreadyPrepared(RuntimeError):
    """The draft for this tender is already at /preview/ stage.

    Means a prior session (CLI run, browser, or earlier bot interaction)
    already created an application AND walked it through the signing flow.
    The server redirects every editable URL (create/lots/docs/...) to
    /preview/, so the pipeline can't add anything — only /ajax_public_application
    is meaningful.

    Bot catches this specifically to offer "🔗 open in cabinet" instead of
    surfacing the cryptic "could not resolve app_lot" message.
    """

    def __init__(self, message: str, *, app_id: Optional[str] = None):
        super().__init__(message)
        self.app_id = app_id


@dataclass(frozen=True, repr=False)
class FetchedPricePage:
    html: str
    cookies: tuple[str | None, str | None]


class AlreadyAtPriceStage(RuntimeError):
    """The draft already advanced to the price-offer (priceoffers) stage.

    Documents are done and committed (docs_next passed on a prior run) but the
    encrypted price was not yet submitted — e.g. a previous run reached the
    gamma step and failed there. The server then redirects every earlier
    editable URL (create/lots/docs/show_doc) to /priceoffers/, so app_lot
    discovery finds nothing. A re-run should SKIP the whole doc phase and resume
    directly at the gamma price step (which reads its (app_lot, lp_id) pairs off
    the priceoffers page and doesn't need the tender_lot→app_lot mapping).
    """

    def __init__(
        self, message: str, *, app_id: Optional[str] = None, fetched_page: FetchedPricePage | None = None,
    ):
        super().__init__(message)
        self.app_id = app_id
        self.fetched_page = fetched_page


class LotDocumentSaveUnconfirmed(RuntimeError):
    def __init__(self, app_id: str, doc_id: int, lot_id: int, page: int, cause: httpx.TransportError):
        super().__init__(
            f"Портал не подтвердил сохранение документа {doc_id}, страница {page}, "
            f"лот заявки {lot_id} ({type(cause).__name__}). "
            f"Проверьте подпись в черновике {app_id}."
        )
        self.app_id = app_id


class _TimedBody(httpx.AsyncByteStream):
    """Record HTTP completion without pre-reading or buffering the body."""

    def __init__(self, stream: httpx.AsyncByteStream, status: int,
                 record: Callable[[int | str, int], None]):
        self._stream = stream
        self._status = status
        self._record = record
        self._size = 0
        self._finished = False

    def _finish(self, status: int | str) -> None:
        if not self._finished:
            self._finished = True
            self._record(status, self._size)

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                self._size += len(chunk)
                yield chunk
        except (asyncio.CancelledError, Exception) as exc:
            self._finish(f"{self._status}/{type(exc).__name__}")
            raise
        else:
            self._finish(self._status)

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            self._finish(f"{self._status}/closed")


class _TimedTransport(httpx.AsyncHTTPTransport):
    """Measure response headers and body using the existing connection pool."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        trace = current_trace.get()
        if trace is None and timing.runtime_event_sink is None:
            return await super().handle_async_request(request)

        started = time.monotonic()
        connection_started: dict[str, float] = {}
        connection_elapsed = {"connect_tcp": 0.0, "start_tls": 0.0}

        def emit(status, http=None):
            if trace is not None:
                trace.add(request.method, request.url.path, started, status, http=http)
            timing.emit_runtime_event(request.method, request.url.path, started, status, http=http)

        async def connection_event(name, _info):
            operation, _, phase = name.rpartition(".")
            operation = operation.rsplit(".", 1)[-1]
            if operation not in connection_elapsed:
                return
            now = time.monotonic()
            if phase == "started":
                connection_started[operation] = now
            elif operation in connection_started:
                connection_elapsed[operation] += now - connection_started.pop(operation)

        install_hook = "trace" not in request.extensions
        if install_hook:
            request.extensions["trace"] = connection_event
        try:
            response = await super().handle_async_request(request)
        except (asyncio.CancelledError, Exception) as exc:
            emit(type(exc).__name__)
            raise
        finally:
            if install_hook:
                request.extensions.pop("trace", None)

        headers_s = time.monotonic() - started
        version = response.http_version

        def record(status, size):
            emit(status, HttpTiming(headers_s, version, size, connection_elapsed["connect_tcp"],
                                    connection_elapsed["start_tls"]))

        response.stream = _TimedBody(response.stream, response.status_code, record)
        return response


class _EarlyApplicationId:
    """Learn the new draft's id while ajax_create_application is in flight.

    The baseline read must point back to the create form, since an older draft
    can have the same appearance as a new one. At most one later read can be
    reused, after the create POST confirms success on the same session.
    """

    def __init__(self, client: "GoszakupClient", announce_id: str):
        self._client = client
        self._announce_id = str(announce_id)
        self._session = client.get_cookie("ci_session")
        self._baseline: Optional[asyncio.Task] = None
        self._hedge: Optional[HedgedRead] = None
        self.ambiguous = False
        self._create_finished = False

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._baseline = loop.create_task(self._client._read_application_id(self._announce_id))
        self._hedge = HedgedRead(
            self._probe, first_delay_s=_EARLY_ID_PROBE_DELAY_S,
            max_inflight=1, max_total=1, label="app_id",
        ).start()

    async def _probe(self, attempt: int) -> Optional[str]:
        clean, seen_before = await asyncio.shield(self._baseline)
        if self._create_finished:
            return None
        if not clean or seen_before or self._client.get_cookie("ci_session") != self._session:
            self.ambiguous = True
            return None
        answered, app_id = await self._client._read_application_id(self._announce_id)
        return app_id if answered else None

    async def _confirmed_probe(self) -> Optional[str]:
        app_id = await self._hedge.first()
        if self._client.get_cookie("ci_session") != self._session:
            return None
        return app_id

    async def resolve(self, usual) -> Optional[str]:
        """The id for a create POST that answered without one."""
        self._create_finished = True
        hedge = self._hedge
        hedge.stop()
        if self._client.get_cookie("ci_session") != self._session:
            log.info("create_application.early_id", ok=False, verdict="session_changed")
            return await usual()
        early = hedge.hit
        if early is not None and not self.ambiguous:
            lead = asyncio.get_running_loop().time() - (hedge.hit_at or 0.0)
            log.info("create_application.early_id", ok=True, lead_s=round(lead, 3), n=hedge.launched)
            return early
        if self.ambiguous:
            log.info("create_application.early_id", ok=False, verdict="draft_seen_before_create")
            return await usual()
        # Start the usual read immediately if the single probe is still pending.
        app_id, from_probe = await first_of(self._confirmed_probe(), usual)
        log.info("create_application.early_id", ok=bool(from_probe), lead_s=0.0, n=hedge.launched)
        return app_id

    async def aclose(self) -> None:
        if self._hedge is not None:
            await self._hedge.aclose()
        baseline = self._baseline
        if baseline is not None and not baseline.done():
            baseline.cancel()
            await asyncio.gather(baseline, return_exceptions=True)
        elif baseline is not None and not baseline.cancelled():
            baseline.exception()


class GoszakupClient:
    """Async HTTPS client for one submission run."""

    def __init__(
        self,
        *,
        ci_session: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        agreement_cookie: Optional[str] = None,
        timeout_s: float = 20.0,
        user_agent: Optional[str] = None,
        proxy: Optional[str] = None,
        _ssl_context: Optional[ssl.SSLContext] = None,
    ):
        """`proxy` accepts http://, https://, socks5:// URLs (with creds).
        Used to route around goszakup's datacenter-IP block on v3bl: VPS
        bots set proxy to a KZ residential proxy provider.
        """
        self.base_url = base_url.rstrip("/")
        # Stashed so clone_unauthenticated() can spin up a sibling client with the
        # exact same transport config (relogin on an isolated jar; per-doc save
        # sessions). agreement_user_2024 is read back out of the live jar instead.
        self._proxy = proxy
        self._timeout_s = timeout_s
        self._user_agent = user_agent
        cookies: dict[str, str] = {}
        if ci_session:
            if ";" in ci_session or "=" in ci_session:
                for item in ci_session.split(";"):
                    if "=" in item:
                        k, v = item.strip().split("=", 1)
                        cookies[k.strip()] = v.strip().strip('"')
            else:
                cookies["ci_session"] = ci_session
        if agreement_cookie:
            cookies["agreement_user_2024"] = agreement_cookie

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/157.0.0.0 Safari/537.36"
            ),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

        # Keep-alive + HTTP/2: one TCP+TLS handshake for the whole submission.
        # NB: HTTP/2 is incompatible with most proxies — fall back to HTTP/1.1
        # if proxy is configured. Costs us a few ms of TLS handshakes per
        # submission, but residential proxies essentially never speak h2.
        #
        # retries=3: goszakup intermittently refuses/slows a NEW TCP connection
        # mid-bid (observed httpx.ConnectTimeout on a doc save). A connect failure
        # means the request never reached the server, so httpx safely retries
        # ESTABLISHING the connection — it never re-sends a request already on the
        # wire, so POSTs stay exactly-once (no duplicate create_application/save).
        # The same verified context serves sibling transports so their separate
        # connection pools don't each reload the CA bundle before polling.
        self._ssl_context = _ssl_context if _ssl_context is not None else httpx.create_ssl_context()
        transport = _TimedTransport(
            verify=self._ssl_context,
            retries=3,
            http2=(proxy is None),
            # The bot heartbeat runs every 30s. httpx's default 5s expiry
            # discarded that warmed connection before the next heartbeat/bid.
            # The portal speaks HTTP/1.1 only, so every overlapping request is
            # its own connection. Reads that overlap a stage POST (hedge.py)
            # peak at ~8 on this client; leave headroom so the request the bid
            # is actually waiting for never queues behind a probe.
            limits=httpx.Limits(
                max_keepalive_connections=4, max_connections=12,
                keepalive_expiry=60.0,
            ),
            proxy=proxy,
        )
        # Kept so warm_connection() can open a pooled connection straight through
        # the transport (no cookies sent, no Set-Cookie absorbed).
        self._transport = transport
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            # connect fails fast (10s) so a refused connection retries quickly;
            # reads get the full timeout_s for goszakup's slow endpoints.
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            follow_redirects=False,
            transport=transport,
            event_hooks={"response": [self._dedupe_cookies_hook]},
        )
        # Seed cookies bound to OUR host + path "/" — the same jar slot the
        # server's own (host-only) Set-Cookie lands in, so a rotated ci_session
        # REPLACES the seeded one. A domain-less seed sat in a different slot:
        # after the first rotation the jar held two ci_session cookies and sent
        # both, stale one first — PHP reads the first, i.e. the dead session.
        host = httpx.URL(self.base_url).host
        for name, value in cookies.items():
            self._http.cookies.set(name, value, domain=host, path="/")

        # /ru/application/create/{ann} HTML captured by the poll probe that saw
        # the tender open (announce_id -> html). create_application reads the
        # valid address/IIK options out of it instead of re-GETting the page —
        # one whole round-trip off the T0 critical path.
        self._create_form_html: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GoszakupClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---------- isolated sibling sessions ----------

    def get_cookie(self, name: str) -> Optional[str]:
        """Safely get cookie value without CookieConflict if multiple domains/paths exist."""
        matches = [c for c in self._http.cookies.jar if c.name == name]
        if not matches:
            return None
        host = httpx.URL(self.base_url).host
        for c in reversed(matches):
            if c.domain and (c.domain == host or host.endswith(c.domain.lstrip("."))):
                return c.value
        return matches[-1].value

    def clone_unauthenticated(self) -> "GoszakupClient":
        """A fresh client with this one's transport config (base_url, proxy,
        timeout, UA) and the agreement cookie, but NO ci_session.

        Used wherever a stateful flow must NOT share the live cookie jar:
          * relogin-in-poll — the multi-step ECP login builds up a pre-auth
            session; if a concurrent probe on the shared jar 302s to /user/login
            it Set-Cookies a fresh ci_session and clobbers the login mid-flight
            (server then 302s the password step to /error_report/access_denied).
            Logging in on a clone keeps the jar private, then we adopt the result.
          * parallel_sessions doc saves — each doc gets its own logged-in session.
        """
        agreement = self.get_cookie("agreement_user_2024")
        csrf = self.get_cookie("csrf_cookie")
        clone = GoszakupClient(
            ci_session=None,
            base_url=self.base_url,
            agreement_cookie=agreement,
            timeout_s=self._timeout_s,
            user_agent=self._user_agent,
            proxy=self._proxy,
            _ssl_context=self._ssl_context,
        )
        if csrf:
            host = httpx.URL(self.base_url).host
            clone._http.cookies.set("csrf_cookie", csrf, domain=host, path="/")
        return clone

    def adopt_ci_session(self, ci_session: str) -> None:
        """Replace this client's ci_session (or all cookies if header string) with fresh ones."""
        host = httpx.URL(self.base_url).host
        jar = self._http.cookies.jar
        if ";" in ci_session or ("=" in ci_session and not ci_session.startswith("ey")):
            for item in ci_session.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    k_clean = k.strip()
                    v_clean = v.strip().strip('"')
                    for c in list(jar):
                        if c.name == k_clean:
                            jar.clear(c.domain, c.path, c.name)
                    self._http.cookies.set(k_clean, v_clean, domain=host, path="/")
        else:
            for cookie in list(jar):
                if cookie.name == "ci_session":
                    jar.clear(cookie.domain, cookie.path, cookie.name)
            self._http.cookies.set("ci_session", ci_session, domain=host, path="/")

    async def _dedupe_cookies_hook(self, response: httpx.Response) -> None:
        """Keep ONE cookie per name the server just (re)issued.

        httpx stores cookies per (domain, path, name). If the server issues
        ``ci_session`` with a Domain attribute (or under any slot other than the
        one we seeded), the jar ends up with two cookies of that name and sends
        both — the stale one first, which is the one PHP reads. A browser never
        holds our hand-seeded copy, so drop every same-name cookie except the
        value this response just set.
        """
        try:
            fresh: dict[str, str] = {}
            for raw in response.headers.get_list("set-cookie"):
                pair = raw.split(";", 1)[0]
                if "=" not in pair:
                    continue
                name, value = pair.split("=", 1)
                fresh[name.strip()] = value.strip().strip('"')
            if not fresh:
                return
            jar = self._http.cookies.jar
            by_name: dict[str, list] = {}
            for c in jar:
                if c.name in fresh:
                    by_name.setdefault(c.name, []).append(c)
            for name, found in by_name.items():
                if len(found) < 2:
                    continue
                keep = next(
                    (c for c in reversed(found)
                     if (c.value or "").strip('"') == fresh[name]), None)
                if keep is None:
                    continue  # the new value isn't in the jar (expired/rejected) — leave as is
                if keep.path not in ("/", ""):
                    continue  # path-scoped cookie doesn't replace a site-wide one
                for c in found:
                    if c is not keep:
                        jar.clear(c.domain, c.path, c.name)
                log.info("cookies.deduped", name=name, dropped=len(found) - 1)
        except Exception as e:  # noqa: BLE001 — hygiene only, never fail a request
            log.debug("cookies.dedupe_failed", err=str(e)[:120])

    def sync_cookies_to(self, other: "GoszakupClient") -> None:
        """Copy EVERY cookie of this client into ``other`` (same-name ones replaced).

        A per-document sibling then sends exactly the Cookie header this client
        would — the session plus the WAF's companion cookies (BNES_*). Cloning
        only the bare ci_session left siblings a subset of what the portal sees
        from the main client.
        """
        import copy
        dst = other._http.cookies.jar
        names = {c.name for c in self._http.cookies.jar}
        for stale in [c for c in dst if c.name in names]:
            dst.clear(stale.domain, stale.path, stale.name)
        for c in list(self._http.cookies.jar):
            dst.set_cookie(copy.copy(c))

    async def warm_connection(self, *, timeout_s: float = 8.0) -> bool:
        """Open (or keep alive) one pooled connection WITHOUT touching the session.

        Sends a cookie-less HEAD straight through the transport: the TCP+TLS
        handshake is paid now instead of on the first document request, but no
        ci_session is presented (so PHP neither loads nor rotates our session)
        and no Set-Cookie can reach the jar. Returns True if the connection is up.
        """
        try:
            request = httpx.Request(
                "HEAD", f"{self.base_url}/ru/",
                headers={
                    "User-Agent": self._http.headers.get("User-Agent", "Mozilla/5.0"),
                    "Accept": "*/*",
                },
                extensions={"timeout": httpx.Timeout(timeout_s).as_dict()},
            )
            response = await self._transport.handle_async_request(request)
            try:
                await response.aread()
            finally:
                await response.aclose()
            return True
        except Exception as e:  # noqa: BLE001 — pure optimisation; a miss just re-handshakes later
            log.debug("warm_connection.failed", err=str(e)[:120])
            return False

    async def peek(self, path: str) -> httpx.Response:
        """A GET that can neither change nor replace the cabinet session.

        For reads that overlap a write on this client (hedge.py). Two rules:

          * It is flagged as an AJAX call. CodeIgniter never regenerates the
            session id on those, so a probe cannot be the request that rotates
            the session while other requests still carry the old id.
          * It goes straight through the transport: the jar's cookies are sent,
            but no Set-Cookie of the answer is stored. A probe that the portal
            treated as anonymous is answered with a fresh anonymous
            ``ci_session`` — stored, that would replace the live one.

        Redirects are not followed; the body is read in full.
        """
        request = self._http.build_request("GET", path, headers={
            "Accept": "text/html, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        response = await self._transport.handle_async_request(request)
        response.request = request
        try:
            await response.aread()
        finally:
            await response.aclose()
        return response

    async def relogin_isolated(self, *, account_password: str, kalkan_signer) -> str:
        """Re-authenticate on a throwaway client, then adopt the fresh ci_session.

        This is the race-free relogin: the 6-step login runs on a private jar that
        no concurrent poll probe can touch, so it can't be clobbered mid-flow. Only
        the resulting ci_session is copied back into this (shared) client.
        """
        sib = self.clone_unauthenticated()
        try:
            ci = await sib.login(
                account_password=account_password, kalkan_signer=kalkan_signer
            )
            self.adopt_ci_session(ci)
            log.info("relogin_isolated.adopted")
            return ci
        finally:
            await sib.aclose()

    # ---------- low-level helpers ----------

    def _csrf_data(self, extra: Mapping[str, str | int]) -> dict:
        """Return `extra` plus the CSRF token field required by every POST."""
        csrf_val = self.get_cookie("csrf_cookie")
        if csrf_val:
            return {"csrf": csrf_val, **dict(extra)}
        return dict(extra)

    def _csrf_prefix(self) -> str:
        """URL-encoded CSRF prefix to prepend to raw body strings: 'csrf=VAL&'."""
        val = self.get_cookie("csrf_cookie")
        return f"csrf={val}&" if val else ""

    async def _post_form(
        self,
        path: str,
        data: Mapping[str, str | int],
        *,
        referer_path: Optional[str] = None,
    ) -> httpx.Response:
        referer = f"{self.base_url}{referer_path or path}"
        encoded = urlencode(self._csrf_data(data))
        log.info("http.post", path=path, body_len=len(encoded))
        r = await self._http.post(path, content=encoded, headers=_ajax_headers(referer))
        log.info("http.post.done", path=path, status=r.status_code, len=len(r.content))

        # Session-expired detection. We don't follow redirects on POST
        # (follow_redirects=False in the client) so a dead cookie shows up
        # as a 30x with Location: /user/login... — without this check,
        # downstream parsers read login HTML as JSON and emit cryptic
        # "no file_id" / "no offer pairs" errors mid-bid.
        loc = (r.headers.get("location") or "").lower()
        if r.status_code in (301, 302, 303, 307, 308) and "/user/login" in loc:
            raise SessionExpired(f"POST {path} -> {loc}")
        # Rare: server returns 200 with the login HTML page (happens on some
        # AJAX endpoints when CSRF/session is dead). Only flag if the body
        # is HTML containing both /user/login and a login form marker.
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code == 200 and "html" in ctype and len(r.content) > 1024:
            sample = r.content[:2048].lower()
            if b"/user/login" in sample and b"name=\"username\"" in sample:
                raise SessionExpired(f"POST {path} returned login HTML (200)")
        return r

    async def _get_page(self, path: str, *, follow_redirects: bool = True) -> str:
        """GET a goszakup HTML page. Follows 30x redirects -- once an
        application moves to the next stage, the previous URL 302s to the
        current one (e.g., /application/create/{ann} → /application/docs/{ann}/{app_id}).
        """
        return (await self._get_page_response(path, follow_redirects=follow_redirects)).text

    async def _get_page_response(self, path: str, *, follow_redirects: bool = True) -> httpx.Response:
        """Fetch an HTML page and retain its final URL."""
        log.info("http.get", path=path)
        r = await self._http.get(path, follow_redirects=follow_redirects)
        final_url = str(r.url)
        log.info(
            "http.get.done",
            path=path,
            status=r.status_code,
            final=final_url,
            len=len(r.content),
        )
        if "/user/login" in final_url:
            raise SessionExpired(f"GET {path} -> {final_url}")
        r.raise_for_status()
        return r

    # ---------- step 1-3: create + lots ----------

    async def create_application(
        self,
        announce_id: str,
        *,
        subject_address: str,
        iik: str,
        contact_phone: str = "",
        tax_payer_type: str = "UL",
        consortium: int = 0,
        early_id: bool = False,
    ) -> Optional[str]:
        """POST ajax_create_application. Automatically validates and picks real
        subject_address and iik from the create form if config is outdated or mismatched.

        ``early_id``: read the draft's id while the POST is still in flight
        (see _EarlyApplicationId) instead of only after its answer.
        """
        async def _fetch_valid_options() -> tuple[list[str], list[str]]:
            try:
                # The poll probe that detected the opening already holds this
                # exact page — reuse it. Re-GETting it here cost a full extra
                # round-trip between "tender open" and "application created".
                html = self._create_form_html.pop(str(announce_id), None)
                if html is not None:
                    log.info("create_application.form_from_poll", len=len(html))
                else:
                    r_form = await self._http.get(
                        f"/ru/application/create/{announce_id}",
                        follow_redirects=True,
                        timeout=8.0,
                    )
                    html = r_form.text
                valid_addrs: list[str] = []
                valid_iiks: list[str] = []
                m_addr = re.search(r'name=["\']subject_address["\'][^>]*>(.*?)</select>', html, re.DOTALL)
                if m_addr:
                    valid_addrs = [v for v in re.findall(r'<option\s+value=["\'](\d+)["\']', m_addr.group(1)) if v not in ("0", "")]
                m_iik = re.search(r'name=["\']iik["\'][^>]*>(.*?)</select>', html, re.DOTALL)
                if m_iik:
                    valid_iiks = [v for v in re.findall(r'<option\s+value=["\'](\d+)["\']', m_iik.group(1)) if v not in ("0", "")]
                return valid_addrs, valid_iiks
            except Exception as e:
                log.warning("create_application.fetch_options_failed", err=str(e))
                return [], []

        cur_addr = str(subject_address).strip()
        cur_iik = str(iik).strip()

        # Probe create form to auto-detect real company options
        valid_addrs, valid_iiks = await _fetch_valid_options()
        if valid_addrs and cur_addr not in valid_addrs:
            log.info("create_application.auto_pick_addr", old=cur_addr, new=valid_addrs[0], all=valid_addrs)
            cur_addr = valid_addrs[0]
        if valid_iiks and cur_iik not in valid_iiks:
            log.info("create_application.auto_pick_iik", old=cur_iik, new=valid_iiks[0], all=valid_iiks)
            cur_iik = valid_iiks[0]

        watch: Optional[_EarlyApplicationId] = None
        if early_id and self.get_cookie("ci_session"):
            watch = _EarlyApplicationId(self, announce_id)
            watch.start()
        try:
            return await self._create_application(
                announce_id, cur_addr=cur_addr, cur_iik=cur_iik, valid_iiks=valid_iiks,
                contact_phone=contact_phone, tax_payer_type=tax_payer_type,
                consortium=consortium, watch=watch,
            )
        finally:
            if watch is not None:
                await watch.aclose()

    async def post_create_application(
        self, announce_id: str, *, subject_address: str, iik: str, contact_phone: str = "",
        tax_payer_type: str = "UL", consortium: int = 0,
    ) -> httpx.Response:
        """The bare ajax_create_application POST: exactly one request, the raw
        answer, no reading of the form before it and no discovery after it.
        (create_application() is built on it; tools/probe_preopen.py uses it to
        see what the portal answers before a tender opens.)"""
        return await self._post_form(
            f"/ru/application/ajax_create_application/{announce_id}",
            {
                "subject_address": subject_address,
                "iik": iik,
                "contact_phone": contact_phone,
                "tax_payer_type": tax_payer_type,
                "consortium": consortium,
            },
            referer_path=f"/ru/application/create/{announce_id}",
        )

    async def _create_application(
        self, announce_id: str, *, cur_addr: str, cur_iik: str, valid_iiks: list[str],
        contact_phone: str, tax_payer_type: str, consortium: int,
        watch: Optional[_EarlyApplicationId],
    ) -> Optional[str]:
        r = await self.post_create_application(
            announce_id, subject_address=cur_addr, iik=cur_iik, contact_phone=contact_phone,
            tax_payer_type=tax_payer_type, consortium=consortium)
        log.info(
            "create_application.response",
            status=r.status_code,
            location=r.headers.get("location"),
            ctype=r.headers.get("content-type"),
            body=r.text[:500],
            reply=reply_shape(r),
        )

        # Surface server-side errors
        j = None
        try:
            j = r.json()
            if isinstance(j, dict) and j.get("status") in ("error", "fail"):
                msg = j.get("message") or j.get("error") or str(j)
                # If error is about bank account or address, try alternate options from form
                if ("счет" in msg.lower() or "адрес" in msg.lower()) and valid_iiks:
                    for alt_iik in valid_iiks:
                        if alt_iik == cur_iik:
                            continue
                        log.info("create_application.retrying_alt_iik", alt_iik=alt_iik)
                        r2 = await self.post_create_application(
                            announce_id, subject_address=cur_addr, iik=alt_iik,
                            contact_phone=contact_phone, tax_payer_type=tax_payer_type,
                            consortium=consortium)
                        j2 = r2.json()
                        if isinstance(j2, dict) and j2.get("status") not in ("error", "fail"):
                            app_id = self._extract_app_id_from_response(r2, announce_id)
                            if app_id:
                                return app_id
                raise RuntimeError(f"ajax_create_application server error: {msg}")
        except RuntimeError:
            raise
        except Exception:
            pass

        app_id = self._extract_app_id_from_response(r, announce_id)
        if app_id:
            return app_id

        # Fallback: GET lots page, follow redirects, parse final URL.
        log.info("create_application.discover_id", status=r.status_code)
        if (watch is not None and r.is_success and isinstance(j, dict)
                and j.get("status") == "ok" and not (j.get("error") or j.get("errors"))):
            return await watch.resolve(lambda: self._discover_application_id(announce_id))
        return await self._discover_application_id(announce_id)

    async def _read_application_id(self, announce_id: str) -> tuple[bool, Optional[str]]:
        """One read of this tender's applicant "lots" page, redirects not followed.

        Returns ``(clean, app_id)``. ``clean`` is False when the answer cannot be
        read as "this draft" or "no draft" (network error, an error status, or a
        page naming several drafts). Never raises.
        """
        announce_id = str(announce_id)
        try:
            r = await self.peek(f"/ru/application/lots/{announce_id}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a probe; the usual read still runs
            log.debug("create_application.early_id_read_failed", error_type=type(exc).__name__)
            return False, None
        if r.is_redirect:
            location = r.headers.get("location") or ""
            app_id = self._app_id_from_target(location, announce_id)
            if app_id:
                return True, app_id
            base = httpx.URL(self.base_url)
            try:
                target = base.join(location)
            except (httpx.InvalidURL, ValueError):
                return False, None
            same_origin = (target.scheme, target.host, target.port) == (base.scheme, base.host, base.port)
            create_path = rf"/(?:(?:ru|en|kz)/)?application/create/{re.escape(announce_id)}/?"
            return same_origin and bool(re.fullmatch(create_path, target.path)), None
        if r.status_code != 200:
            return False, None
        ids = {
            app_id
            for target in re.findall(r"""\b(?:href|action)\s*=\s*["']([^"']+)["']""", r.text, re.IGNORECASE)
            if "/application/" in target and (app_id := self._app_id_from_target(target, announce_id))
        }
        return (True, ids.pop()) if len(ids) == 1 else (False, None)

    def _extract_app_id_from_response(
        self, r: httpx.Response, announce_id: str
    ) -> Optional[str]:
        # PHP JSON can escape path separators as \/, so inspect decoded values.
        try:
            j = r.json()
        except ValueError:
            j = None
        if isinstance(j, dict):
            for k in ("application_id", "app_id", "id"):
                value = j.get(k)
                if isinstance(value, (int, str)) and re.fullmatch(r"[1-9][0-9]*", str(value)):
                    return str(value)
            for k in ("redirect", "redirect_url", "url", "location"):
                target = j.get(k)
                if isinstance(target, str) and (app_id := self._app_id_from_target(target, announce_id)):
                    return app_id

        location = r.headers.get("location")
        if location and (app_id := self._app_id_from_target(location, announce_id)):
            return app_id
        # 3) Body content (sometimes the redirect target is embedded)
        if isinstance(j, dict):
            return None
        try:
            body = r.text
            for pat in (
                rf"/application/lots/{announce_id}/(\d+)",
                rf"application_id[\"']?\s*[:=]\s*[\"']?(\d+)",
            ):
                m = re.search(pat, body)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def _app_id_from_target(self, target: str, announce_id: str) -> Optional[str]:
        """Read a draft ID only from a portal URL for this announcement."""
        base = httpx.URL(self.base_url)
        try:
            url = base.join(target)
        except (httpx.InvalidURL, ValueError):
            return None
        if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
            return None
        match = re.search(rf"/application/[a-z_]+/{re.escape(announce_id)}/(\d+)/?$", url.path)
        return match.group(1) if match else None

    async def _discover_application_id(self, announce_id: str) -> Optional[str]:
        """Resolve application_id by GETing /application/lots/{ann} and following
        redirects to /application/lots/{ann}/{app_id}.

        If that doesn't work, also try /ru/application/create/{ann} (the entry
        point the browser uses after submitting the create form -- the server
        often redirects this one to the just-created application).
        """
        body_dumps: list[str] = []
        for candidate_path in (
            f"/ru/application/lots/{announce_id}",
            f"/ru/application/create/{announce_id}",
            f"/ru/application/index/{announce_id}",
        ):
            log.info("discover_app_id.get", path=candidate_path)
            r = await self._http.get(candidate_path, follow_redirects=False)
            if r.next_request is not None:
                location = r.headers.get("location", "")
                app_id = self._app_id_from_target(location, announce_id)
                if app_id:
                    log.info("discover_app_id.from_redirect")
                    return app_id
                r = await self._http.send(r.next_request, follow_redirects=True)
            log.info(
                "discover_app_id.try",
                path=candidate_path,
                final=str(r.url),
                status=r.status_code,
                body_len=len(r.content),
            )
            # 1) Final URL has app_id?
            app_id = self._app_id_from_target(str(r.url), announce_id)
            if app_id:
                return app_id
            # 2) Body contains a reference like /application/<verb>/{ann}/{app_id}?
            for pat in (
                rf"/application/lots/{announce_id}/(\d+)",
                rf"/application/(?:create|preview|docs|sign_task)/{announce_id}/(\d+)",
                rf"/application/[a-z_]+/{announce_id}/(\d+)",
                r'name=["\']application_id["\']\s+value=["\'](\d+)',
                r'data-application-id=["\'](\d+)',
                r'application_id\s*[:=]\s*["\']?(\d+)',
            ):
                m = re.search(pat, r.text)
                if m:
                    log.info("discover_app_id.matched", pattern=pat, app_id=m.group(1))
                    return m.group(1)
            # 3) Redirect history?
            for resp in r.history:
                loc = resp.headers.get("location", "")
                app_id = self._app_id_from_target(loc, announce_id)
                if app_id:
                    return app_id
            # Save body for off-line inspection
            from .parser import _dump
            dump = _dump(r.text, f"app_id_discovery_{candidate_path.replace('/', '_')}")
            body_dumps.append(str(dump))

        log.error("discover_app_id.exhausted", dumps=body_dumps)
        return None

    async def add_lots(self, announce_id: str, app_id: str, lot_ids: Sequence[int]) -> httpx.Response:
        body = self._csrf_prefix() + "&".join(f"selectLots%5B%5D={lid}" for lid in lot_ids)
        path = f"/ru/application/ajax_add_lots/{announce_id}/{app_id}"
        referer = f"/ru/application/lots/{announce_id}/{app_id}"
        r = await self._http.post(
            path, content=body,
            headers=_ajax_headers(f"{self.base_url}{referer}"),
        )
        # body logged on purpose: if it carries the app_lot ids, the separate
        # GET that discovers them (one round-trip at T0) can go.
        log.info("add_lots", lots=list(lot_ids), status=r.status_code, body=r.text[:400],
                 len=len(r.content), reply=reply_shape(r))
        return r

    async def lots_next(self, announce_id: str, app_id: str, *, confirmed: int = 0) -> httpx.Response:
        r = await self._post_form(
            f"/ru/application/ajax_lots_next/{announce_id}/{app_id}",
            {"next": 1, "confirmed": confirmed},
            referer_path=f"/ru/application/lots/{announce_id}/{app_id}",
        )
        log.info("lots_next.response", status=r.status_code,
                 location=r.headers.get("location"), body=r.text[:400], reply=reply_shape(r))
        return r

    # ---------- step 4-5: doc signing ----------

    async def get_show_doc(self, announce_id: str, app_id: str, doc_id: int, suffix: str = "") -> str:
        return await self._get_page(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}{suffix}"
        )

    async def post_show_doc_signature(
        self,
        announce_id: str,
        app_id: str,
        doc_id: int,
        *,
        file_id: int,
        signature_b64: str,
        sign_flag: bool = True,
        extra: Optional[Mapping[str, str]] = None,
    ) -> httpx.Response:
        data: dict[str, str | int] = {}
        if sign_flag:
            data["sign"] = "Y"
        data[f"signature[{file_id}]"] = signature_b64
        if extra:
            data.update(extra)
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
            data,
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    async def get_sign_task(self, announce_id: str, app_id: str, lot_id: int, doc_id: int) -> str:
        return await self._get_page(
            f"/ru/application/sign_task/{announce_id}/{app_id}/{lot_id}/{doc_id}"
        )

    async def post_sign_task_signature(
        self,
        announce_id: str,
        app_id: str,
        lot_id: int,
        doc_id: int,
        *,
        file_id: int,
        signature_b64: str,
    ) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/sign_task/{announce_id}/{app_id}/{lot_id}/{doc_id}",
            {
                "sign_task": "Y",
                f"signature[{file_id}]": signature_b64,
            },
            referer_path=f"/ru/application/sign_task/{announce_id}/{app_id}/{lot_id}/{doc_id}",
        )

    # ---------- step 6: copy qualification data from another tender ----------

    async def copy_data_docs_search(
        self, announce_id: str, app_id: str, doc_id: int, *, source_anno: str
    ) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/copy_data_docs/{announce_id}/{app_id}/{doc_id}",
            {"anno_number": source_anno, "search": "Найти"},
            referer_path=f"/ru/application/copy_data_docs/{announce_id}/{app_id}/{doc_id}",
        )

    async def copy_data_docs_apply(
        self,
        announce_id: str,
        app_id: str,
        doc_id: int,
        *,
        source_anno: str,
        from_lot: int,
        to_lots: Sequence[int],
    ) -> httpx.Response:
        parts = [f"from_lot={from_lot}"]
        parts += [f"to_lot%5B%5D={l}" for l in to_lots]
        parts += [f"anno_number={source_anno}", "submit=%D0%9F%D1%80%D0%B8%D0%BC%D0%B5%D0%BD%D0%B8%D1%82%D1%8C"]
        body = self._csrf_prefix() + "&".join(parts)
        path = f"/ru/application/copy_data_docs/{announce_id}/{app_id}/{doc_id}"
        r = await self._http.post(
            path, content=body,
            headers=_ajax_headers(f"{self.base_url}{path}"),
        )
        log.info("copy_data_docs.apply", from_lot=from_lot, to_lots=list(to_lots))
        return r

    async def copy_data_docs_finalize(
        self, announce_id: str, app_id: str, doc_id: int, *, source_anno: str
    ) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/copy_data_docs/{announce_id}/{app_id}/{doc_id}",
            {"anno_number": source_anno, "submit": "Применить"},
            referer_path=f"/ru/application/copy_data_docs/{announce_id}/{app_id}/{doc_id}",
        )

    # ---------- step 7-8: bankruptcy declaration ----------

    async def bankruptcy_gen(
        self, announce_id: str, app_id: str, *, doc_id: int = 3240, bankruptcy: int = 2
    ) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
            {"bankruptcy": bankruptcy, "btn": "docGen"},
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    async def bankruptcy_save(
        self,
        announce_id: str,
        app_id: str,
        *,
        doc_id: int = 3240,
        file_id: int,
        signature_b64: str,
    ) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
            {
                "btn": "docSave",
                f"signature[{file_id}]": signature_b64,
            },
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    # ---------- step 9-10: deposit / guarantee per lot ----------

    async def deposit_add(
        self,
        announce_id: str,
        app_id: str,
        *,
        doc_id: int = 719,
        lot_id: int,
        num: int = 1,
        type_doc: int = 3,
    ) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}/{lot_id}/{num}",
            {"typeDoc": type_doc},
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    async def deposit_save(
        self,
        announce_id: str,
        app_id: str,
        *,
        doc_id: int = 719,
        lot_id: int,
        num: int = 1,
        type_doc: int = 3,
    ) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}/{lot_id}/{num}/?show=3",
            {"typeDoc": type_doc, "save_electronic_data": "Сохранить"},
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    # ========== atagos doc flow (generate → sign two-step) ==========
    #
    # This account's tenders carry a different document set than temka's
    # (docs 1253, 3359/beneficiary, 3334/per-lot, 3327/agreement, 3335/bankruptcy,
    # 3328/deposit). Most docs follow a two-step pattern:
    #     1. POST show_doc/{doc}        generate=...        (server builds the PDF)
    #     2. POST show_doc/{doc}        userfile[{doc}]={fid}&save_form=&signature[{fid}]=<CMS>

    async def show_doc_generate(
        self,
        announce_id: str,
        app_id: str,
        doc_id: int,
        *,
        fields: Optional[Mapping[str, str | int]] = None,
    ) -> httpx.Response:
        """Step 1 of a generate→sign doc. ``fields`` is the exact form body, e.g.
        {"generate": "Сформировать документ"} for doc 1253,
        {"generate": "", "userfile[3359]": "", "save_form": ""} for doc 3359,
        {"generate": "Принять соглашение"} for doc 3327.
        """
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
            dict(fields or {"generate": ""}),
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    async def show_doc_save_signed(
        self,
        announce_id: str,
        app_id: str,
        doc_id: int,
        *,
        file_id: int,
        signature_b64: str,
    ) -> httpx.Response:
        """Step 2: attach the signed file. Body shape from HAR:
        userfile[{doc}]={fid}&save_form=&signature[{fid}]=<CMS>&sign=Y.
        """
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
            {
                f"userfile[{doc_id}]": file_id,
                "save_form": "",
                "sign": "Y",
                f"signature[{file_id}]": signature_b64,
            },
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    async def sign_lot_doc(
        self,
        announce_id: str,
        app_id: str,
        doc_id: int,
        lot_id: int,
        num: int,
        *,
        signatures: Mapping[int, str],
    ) -> httpx.Response:
        """Per-lot doc sign (doc 3334). Body: send=Сохранить&sign_files=&
        signature[{fid}]=<CMS>[&signature[{fid2}]=...].  ``signatures`` maps
        file_id -> CMS (a single lot-doc page can list several files).
        """
        data: dict[str, str | int] = {"send": "Сохранить", "sign_files": ""}
        for fid, sig in signatures.items():
            data[f"signature[{fid}]"] = sig
        try:
            return await self._post_form(
                f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}/{lot_id}/{num}",
                data,
                referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
            )
        except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError,
                httpx.WriteError, httpx.WriteTimeout) as exc:
            # The portal may already have committed the signature.
            raise LotDocumentSaveUnconfirmed(app_id, doc_id, lot_id, num, exc) from exc

    # ---------- beneficiary (Приложение 1: attach + describe the ID doc) ----------

    async def check_file_status(self, file_ids: Sequence[int]) -> httpx.Response:
        """POST /files/check_status/ — confirm uploaded files passed AV/format
        checks before referencing them. Body: file_id_ar[0]=N&file_id_ar[1]=...
        """
        body = self._csrf_prefix() + "&".join(f"file_id_ar%5B{i}%5D={fid}" for i, fid in enumerate(file_ids))
        r = await self._http.post(
            "/files/check_status/",
            content=body,
            headers=_ajax_headers(f"{self.base_url}/ru/application/docs"),
        )
        log.info("check_file_status", file_ids=list(file_ids), status=r.status_code)
        return r

    async def beneficiary_save_info(
        self,
        *,
        name: str,
        citizenship: str,
        res_country: str,
        doc_number: str,
        doc_date: str,
        options: Mapping[str, int],
        app_lot_id: int,
        file_ids: Sequence[int],
        beneficiary_id: str = "",
    ) -> httpx.Response:
        """POST /ru/beneficiary/ajax_save_info. Body (HAR):
        beneficiary_name=..&citizenship=398&res_country=398&beneficiary_doc_number=..
        &beneficiary_doc_date=..&option_1..4=..&app_lot_id=..&beneficiary_id=
        &userfile[1][]=<file_id>
        """
        data: dict[str, str | int] = {
            "beneficiary_name": name,
            "citizenship": citizenship,
            "res_country": res_country,
            "beneficiary_doc_number": doc_number,
            "beneficiary_doc_date": doc_date,
            "app_lot_id": app_lot_id,
            "beneficiary_id": beneficiary_id,
        }
        for k, v in options.items():
            data[k] = v
        # userfile[1][] is a repeated array; build separately to allow repeats.
        from urllib.parse import urlencode
        body = self._csrf_prefix() + urlencode(data)
        body += "".join(f"&userfile%5B1%5D%5B%5D={fid}" for fid in file_ids)
        r = await self._http.post(
            "/ru/beneficiary/ajax_save_info",
            content=body,
            headers=_ajax_headers(f"{self.base_url}/ru/application/show_doc"),
        )
        log.info("beneficiary_save_info", app_lot=app_lot_id, files=list(file_ids), status=r.status_code)
        return r

    async def beneficiary_show_info(self, beneficiary_id: str) -> httpx.Response:
        return await self._post_form(
            "/ru/beneficiary/ajax_show_info",
            {"beneficiary_id": beneficiary_id},
            referer_path="/ru/application/show_doc",
        )

    @staticmethod
    def _parse_upload_form(html: str) -> Optional[dict]:
        """Extract the file-upload <form> from a doc page: action, the file
        field name, every hidden field, and the data-file-token.

        Mirrors upload.js `generate_file`: it POSTs `new FormData(upload_form)`
        to `upload_form.action` and expects JSON {status, link, file_id}. So we
        must replay ALL the form's fields (token, file_name, file_size, …), not
        just the file — a bare file POST 302s.
        """
        token = None
        mtok = re.search(r'data-file-token="([^"]+)"', html)
        if mtok:
            token = mtok.group(1)
        for fm in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html, re.S | re.I):
            attrs, body = fm.group(1), fm.group(2)
            am = re.search(r'action="([^"]+)"', attrs)
            if not am:
                continue
            action = am.group(1)
            has_file = re.search(r'<input[^>]+type="file"', body, re.I)
            if not (has_file or "upload" in action.lower()):
                continue
            ff = (re.search(r'<input[^>]+type="file"[^>]+name="([^"]+)"', body, re.I)
                  or re.search(r'<input[^>]+name="([^"]+)"[^>]+type="file"', body, re.I))
            hidden: dict[str, str] = {}
            for hm in re.finditer(r"<input[^>]+type=\"hidden\"[^>]*>", body, re.I):
                tag = hm.group(0)
                nm = re.search(r'name="([^"]+)"', tag)
                vm = re.search(r'value="([^"]*)"', tag)
                if nm:
                    hidden[nm.group(1)] = vm.group(1) if vm else ""
            return {
                "action": action,
                "file_field": ff.group(1) if ff else "file",
                "hidden": hidden,
                "token": token,
            }
        return None

    async def upload_file(
        self,
        file_path: str,
        *,
        page_html: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> Optional[int]:
        """Upload a local file (the ID PDF) the way the page does and return its
        file_id. Reads the upload form out of ``page_html`` (action, token,
        hidden fields). Reply is JSON {status:1, link, file_id} (upload.js:610).
        """
        from pathlib import Path
        p = Path(file_path).expanduser()
        data = p.read_bytes()

        form = self._parse_upload_form(page_html or "")
        action = (form or {}).get("action") or "/ru/files/upload_file_new"
        file_field = (form or {}).get("file_field") or "file"
        fields: dict[str, str] = dict((form or {}).get("hidden") or {})
        if form and form.get("token"):
            fields.setdefault("token", form["token"])
        # upload.js populates these on the form right before submit, so OVERRIDE
        # the (empty) hidden placeholders — sending blank file_name/size is
        # rejected with a tiny {"status":0} body.
        fields["file_name"] = p.name
        fields["file_size"] = str(len(data))

        # action may be absolute, base-prefixed, or relative
        url = action
        if url.startswith(self.base_url):
            url = url[len(self.base_url):]
        elif not url.startswith(("http", "/")):
            url = "/" + url

        csrf_val = self.get_cookie("csrf_cookie")
        if csrf_val:
            fields["csrf"] = csrf_val
        r = await self._http.post(
            url,
            data=fields,
            files={file_field: (p.name, data, "application/pdf")},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer or f"{self.base_url}/ru/application/docs",
            },
        )
        log.info("upload_file.done", name=p.name, action=action,
                 status=r.status_code, len=len(r.content),
                 fields=list(fields), file_field=file_field,
                 body=r.text[:300])
        try:
            j = r.json()
            if isinstance(j, dict):
                for k in ("file_id", "id", "fileId"):
                    if j.get(k):
                        return int(j[k])
        except Exception:
            pass
        m = re.search(r"download_file/(\d+)", r.text)
        return int(m.group(1)) if m else None

    # ---------- gamma price offer (encrypted) ----------

    async def get_encr_info(self, announce_id: str, app_id: str, *, lp_id: int, version: str) -> dict:
        """POST ajax_get_encr_info -> JSON with the recipient EFC cert + range
        the page forwards to TumarCSP.EncryptOfferPrice. Body: lpId=N&version=X.
        """
        ver = version or "1.0.13.2285"
        r = await self._post_form(
            f"/ru/application/ajax_get_encr_info/{announce_id}/{app_id}",
            {"lpId": lp_id, "version": ver},
            referer_path=f"/ru/application/priceoffers/{announce_id}/{app_id}",
        )
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            if "/preview/" in loc:
                raise AlreadyPrepared(f"application {app_id} already at preview ({loc})")
            log.warning("get_encr_info.redirect", status=r.status_code, loc=loc)
        try:
            j = r.json()
            if isinstance(j, dict):
                return j
        except Exception:
            pass
        return {"status": "unknown", "raw": r.text[:1000], "status_code": r.status_code}

    async def add_encrypt(
        self,
        announce_id: str,
        app_id: str,
        *,
        item_id: int,
        encrypted_data: str,
        session_key: str,
        salt: str,
        info: str,
        sign: str,
    ) -> httpx.Response:
        """POST ajax_add_encrypt — store the sealed price. Field names mirror
        the HAR exactly (itemID, encryptedData, sessionKey, salt, info, sign)."""
        from urllib.parse import quote
        body = self._csrf_prefix() + "&".join([
            f"itemID={item_id}",
            f"encryptedData={quote(encrypted_data, safe='')}",
            f"sessionKey={quote(session_key, safe='')}",
            f"salt={quote(salt, safe='')}",
            f"info={quote(info, safe='')}",
            f"sign={quote(sign, safe='')}",
        ])
        r = await self._http.post(
            f"/ru/application/ajax_add_encrypt/{announce_id}/{app_id}",
            content=body,
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("add_encrypt", item_id=item_id, status=r.status_code, reply=reply_shape(r))
        return r

    async def save_gamma_signs(
        self,
        announce_id: str,
        app_id: str,
        *,
        signs_by_lp: Mapping[int, tuple[str, str]],
    ) -> httpx.Response:
        """POST ajax_save_gamma_signs. ``signs_by_lp`` maps lpId -> (encrypted_data,
        cms_signature). Body: xmlData[{lp}]=<encData>&signData[{lp}]=<CMS>.
        Note: xmlData is the *encrypted* blob (not plaintext XML) on gamma tenders.
        """
        from urllib.parse import quote
        parts = []
        for lp, (enc, sig) in signs_by_lp.items():
            parts.append(f"xmlData%5B{lp}%5D={quote(enc, safe='')}")
            parts.append(f"signData%5B{lp}%5D={quote(sig, safe='')}")
        r = await self._http.post(
            f"/ru/application/ajax_save_gamma_signs/{announce_id}/{app_id}",
            content=self._csrf_prefix() + "&".join(parts),
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("save_gamma_signs", lps=list(signs_by_lp), status=r.status_code,
                 reply=reply_shape(r))
        return r

    async def priceoffers_next_gamma(
        self,
        announce_id: str,
        app_id: str,
        *,
        prices: Mapping[tuple[int, int], str],
        is_construction_pilot: str = "",
    ) -> httpx.Response:
        """POST ajax_priceoffers_next where the price value is the encrypted blob.
        ``prices`` maps (app_lot_id, lp_id) -> encrypted_data. Body matches the
        page's .offer_gamma input serialize() — name="offer[{lot}][{lp}][price]"
        (NO trailing []): offer[{app_lot}][{lp}][price]=<encData>&is_construction_pilot=.
        """
        from urllib.parse import quote
        parts = [
            f"offer%5B{lot}%5D%5B{lp}%5D%5Bprice%5D={quote(enc, safe='')}"
            for (lot, lp), enc in prices.items()
        ]
        parts.append(f"is_construction_pilot={is_construction_pilot}")
        r = await self._http.post(
            f"/ru/application/ajax_priceoffers_next/{announce_id}/{app_id}",
            content=self._csrf_prefix() + "&".join(parts),
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("priceoffers_next_gamma", n=len(prices), status=r.status_code,
                 reply=reply_shape(r))
        return r

    # ---------- step 11-15: docs_next + price offer + public ----------

    async def docs_next(self, announce_id: str, app_id: str) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/ajax_docs_next/{announce_id}/{app_id}",
            {"next": 1},
            referer_path=f"/ru/application/docs/{announce_id}/{app_id}",
        )

    async def check_priceoffers(
        self,
        announce_id: str,
        app_id: str,
        *,
        prices: Mapping[tuple[int, int], int],
        is_construction_pilot: str = "",
    ) -> httpx.Response:
        # offer[<lot_id>][<point_id>][price]=N
        parts = [
            f"offer%5B{lot}%5D%5B{pt}%5D%5Bprice%5D={price}"
            for (lot, pt), price in prices.items()
        ]
        parts.append(f"is_construction_pilot={is_construction_pilot}")
        body = self._csrf_prefix() + "&".join(parts)
        path = f"/ru/application/ajax_check_priceoffers/{announce_id}/{app_id}"
        r = await self._http.post(
            path, content=body,
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("check_priceoffers", n=len(prices), status=r.status_code)
        return r

    async def add_priceoffers(
        self,
        announce_id: str,
        app_id: str,
        *,
        xml_by_point: Mapping[int, str],
        sig_by_point: Mapping[int, str],
        form_data: str,
    ) -> httpx.Response:
        from urllib.parse import quote
        parts = []
        for pt, xml in xml_by_point.items():
            parts.append(f"xmlData%5B{pt}%5D={quote(xml, safe='')}")
        for pt, sig in sig_by_point.items():
            parts.append(f"signData%5B{pt}%5D={quote(sig, safe='')}")
        parts.append(f"formData={quote(form_data, safe='')}")
        body = self._csrf_prefix() + "&".join(parts)
        path = f"/ru/application/ajax_add_priceoffers/{announce_id}/{app_id}"
        r = await self._http.post(
            path, content=body,
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("add_priceoffers", n=len(xml_by_point), status=r.status_code)
        return r

    async def priceoffers_next(
        self,
        announce_id: str,
        app_id: str,
        *,
        prices: Mapping[tuple[int, int], int],
        is_construction_pilot: str = "",
    ) -> httpx.Response:
        parts = [
            f"offer%5B{lot}%5D%5B{pt}%5D%5Bprice%5D={price}"
            for (lot, pt), price in prices.items()
        ]
        parts.append(f"is_construction_pilot={is_construction_pilot}")
        body = self._csrf_prefix() + "&".join(parts)
        path = f"/ru/application/ajax_priceoffers_next/{announce_id}/{app_id}"
        r = await self._http.post(
            path, content=body,
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("priceoffers_next", status=r.status_code)
        return r

    async def public_application(
        self,
        announce_id: str,
        app_id: str,
        *,
        agree_price: bool = False,
        agree_contract_project: bool = False,
        agree_covid19: bool = False,
    ) -> dict:
        """Final submit. Server may reject ("status":"error") if BIN data stale."""
        r = await self._post_form(
            f"/ru/application/ajax_public_application/{announce_id}/{app_id}",
            {
                "public_app": "Y",
                "agree_price": "true" if agree_price else "false",
                "agree_contract_project": "true" if agree_contract_project else "false",
                "agree_covid19": "true" if agree_covid19 else "false",
            },
            referer_path=f"/ru/application/preview/{announce_id}/{app_id}",
        )
        log.info("public_application.response", status=r.status_code, reply=reply_shape(r))
        try:
            return r.json()
        except Exception:
            return {"status": "unknown", "raw": r.text[:500]}

    # ---------- login (5-step ECP flow) ----------

    async def login(self, *, account_password: str, kalkan_signer) -> str:
        """Authenticate via ECP and refresh the ci_session cookie.

        Replays the 5-step flow we reverse-engineered from a captured login HAR:

            1. POST /ru/user/sendkey/kz        -> server returns 32-hex nonce
            2. SIGN_XML <root><key>NONCE</key></root>   (Kalkan, KZ GOST 2015-512)
            3. POST /user/sendsign/kz  sign=<signed_xml>   -> 200, sets temp state
            4. GET  /ru/user/auth_confirm                  -> form page (cosmetic)
            5. POST /ru/user/auth_confirm  password=...&agreed_check=on
                                                            -> 302 + ci_session

        Note the missing /ru/ prefix on step 3 -- it's per the real browser flow,
        not a typo.

        Returns the fresh ``ci_session`` cookie value (also already attached to
        the client's cookie jar for subsequent requests).
        """
        log.info("login.start")

        # Opt #4 regression fix: when called on a long-lived shared client
        # that already has a (possibly-stale) ci_session in its jar, the
        # 5-step flow gets confused — server can't reconcile the new auth
        # nonce with the prior session and 302s the password step back to
        # /user/login. Clear ci_session before starting so each login is
        # served from a clean state. agreement_user_2024 is preserved.
        jar = self._http.cookies.jar
        cleared = 0
        for cookie in list(jar):
            if cookie.name == "ci_session":
                jar.clear(cookie.domain, cookie.path, cookie.name)
                cleared += 1
        if cleared:
            log.info("login.cleared_stale_ci_session", n=cleared)

        # Step 0: GET the login page so the server issues csrf_cookie.
        # Also extract the hidden CSRF field name+value from the form — CI
        # checks that POST[csrf_field] == cookie[csrf_cookie].
        r0 = await self._http.get("/ru/user/login")
        if r0.status_code not in (200, 302):
            log.warning("login.login_page.unexpected_status", status=r0.status_code)
        # The site uses <meta name="csrf-token-name"> / <meta name="csrf-token-hash">
        # instead of a hidden input.  Extract both and echo them in every POST.
        _csrf_name_m = re.search(
            r'<meta[^>]+name=["\']csrf-token-name["\'][^>]+content=["\']([^"\']+)["\']',
            r0.text, re.IGNORECASE,
        )
        _csrf_hash_m = re.search(
            r'<meta[^>]+name=["\']csrf-token-hash["\'][^>]+content=["\']([^"\']+)["\']',
            r0.text, re.IGNORECASE,
        )
        _csrf_field: Optional[tuple[str, str]] = None
        if _csrf_name_m and _csrf_hash_m:
            _csrf_field = (_csrf_name_m.group(1), _csrf_hash_m.group(1))
        log.info("login.login_page.ok", status=r0.status_code,
                 csrf_cookie=bool(self.get_cookie("csrf_cookie")),
                 csrf_field=_csrf_field and _csrf_field[0],
                 csrf_hash=_csrf_field and _csrf_field[1][:8])

        # Step 1: get the nonce.
        # CodeIgniter CSRF check: POST body must include csrf_field=value.
        csrf_data = {_csrf_field[0]: _csrf_field[1]} if _csrf_field else {}
        r1 = await self._http.post(
            "/ru/user/sendkey/kz",
            data=csrf_data if csrf_data else None,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": self.base_url,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/ru/user/login",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        if r1.status_code >= 400:
            log.error("login.sendkey.error", status=r1.status_code,
                      body=r1.text[:2000])
        r1.raise_for_status()
        body = r1.text.strip()
        if not body:
            raise RuntimeError("sendkey/kz returned empty body")
        if body.startswith("{"):
            import json as _json
            j = _json.loads(body)
            nonce = j.get("key") or j.get("nonce") or j.get("token")
            if not nonce:
                raise RuntimeError(f"sendkey/kz JSON missing key: {body[:200]}")
        else:
            # raw hex
            nonce = body
        if len(nonce) < 16:
            raise RuntimeError(f"unexpected sendkey nonce: {nonce!r}")
        log.info("login.nonce", len=len(nonce))

        # Step 2: sign the challenge XML
        challenge = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f"<root><key>{nonce}</key></root>"
        )
        signed_xml = await kalkan_signer.sign_xml(challenge)
        log.info("login.signed", chars=len(signed_xml))

        # Helper: build a data dict that always includes the CSRF token.
        def _with_csrf(extra: dict) -> dict:
            if _csrf_field:
                return {_csrf_field[0]: _csrf_field[1], **extra}
            return extra

        # Step 3: POST the signed XML
        r3 = await self._http.post(
            "/user/sendsign/kz",
            data=_with_csrf({"sign": signed_xml}),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/ru/user/login",
            },
        )
        if r3.status_code >= 400:
            log.error("login.sendsign.error", status=r3.status_code, body=r3.text[:500])
        r3.raise_for_status()
        log.info("login.sendsign.ok", status=r3.status_code)

        # Step 4: load auth_confirm form (some PHP sessions key off this view)
        r4 = await self._http.get(
            "/ru/user/auth_confirm",
            headers={"Referer": f"{self.base_url}/ru/user/login"},
        )
        # follow_redirects is False by default; tolerate either 200 or a 302
        if r4.status_code not in (200, 302):
            raise RuntimeError(f"auth_confirm GET failed: {r4.status_code}")
        # Refresh CSRF token from auth_confirm page if it rotated.
        _ac_name_m = re.search(
            r'<meta[^>]+name=["\']csrf-token-name["\'][^>]+content=["\']([^"\']+)["\']',
            r4.text, re.IGNORECASE,
        )
        _ac_hash_m = re.search(
            r'<meta[^>]+name=["\']csrf-token-hash["\'][^>]+content=["\']([^"\']+)["\']',
            r4.text, re.IGNORECASE,
        )
        if _ac_name_m and _ac_hash_m:
            _csrf_field = (_ac_name_m.group(1), _ac_hash_m.group(1))

        # Step 5: submit the account password
        r5 = await self._http.post(
            "/ru/user/auth_confirm",
            data=_with_csrf({"password": account_password, "agreed_check": "on"}),
            headers={"Referer": f"{self.base_url}/ru/user/auth_confirm"},
        )
        loc = r5.headers.get("location", "")
        if r5.status_code != 302 or "/user/login" in loc or "error" in loc:
            err_detail = ""
            if loc:
                try:
                    r_err = await self._http.get(loc)
                    alerts = re.findall(r'<div[^>]*class="[^"]*(?:alert|error|message)[^"]*"[^>]*>(.*?)</div>', r_err.text, re.DOTALL | re.IGNORECASE)
                    if alerts:
                        from lxml import html as _html
                        err_detail = " ".join(_html.fromstring(a).text_content().strip() for a in alerts if a.strip())
                except Exception:
                    pass
            raise RuntimeError(
                f"login failed at password step: status={r5.status_code} loc={loc!r} "
                f"причина: {err_detail or 'неверный пароль от личного кабинета'}"
            )
        log.info("login.success.redirect", to=loc)

        # Step 6: follow the redirect to /cabinet/profile. PHP regenerates the
        # session ID on first authenticated GET, so without this hop we'd be
        # left with the pre-auth ci_session that the server won't honour for
        # /announce/* endpoints.
        cabinet_path = loc.replace(self.base_url, "") or "/ru/cabinet/profile"
        r6 = await self._http.get(cabinet_path, follow_redirects=True)
        log.info("login.cabinet", status=r6.status_code, final=str(r6.url))

        # Dump all cookies for visibility (values truncated).
        all_cookies = {k: (v[:30] + "...") if len(v) > 30 else v
                       for k, v in self._http.cookies.items()}
        log.info("login.cookies", cookies=all_cookies)

        ci = self.get_cookie("ci_session")
        if not ci:
            raise RuntimeError(
                f"login flow completed but ci_session cookie absent; "
                f"cookies present: {list(self._http.cookies.keys())}"
            )
        return ci

    # ---------- pre-T0 helpers ----------

    async def discover_announce_lots(self, announce_id: str) -> list[dict]:
        """Public announce page lists tender lots with data-lot-id attrs.

        The lots live behind a `?tab=lots` query param; the default tab is
        "Общие сведения" which doesn't have lot data.
        """
        from .parser import extract_announce_lots
        # Try the lots tab first; fall back to default page for older layouts.
        for path in (
            f"/ru/announce/index/{announce_id}?tab=lots",
            f"/ru/announce/index/{announce_id}",
        ):
            html_text = await self._get_page(path)
            lots = extract_announce_lots(html_text)
            if lots:
                return lots
        return []

    # form fields that prove the create page rendered for us
    _CREATE_FORM_MARKERS = (
        'name="subject_address"',
        'name="contact_phone"',
        "ajax_create_application",
        "Создание заявки",
    )

    def _classify_create_poll(self, r, final_url: str, announce_id: str):
        """One create-page probe -> (verdict, matched_marker).

        verdict: ready | existing_draft | not_open | login_expired | access_denied | no_form
        """
        if "/user/login" in final_url:
            return "login_expired", None
        if "access_denied" in final_url:
            # Genuine ineligibility (tariff/agreement) — never clears by waiting.
            return "access_denied", None
        # Server bounced us straight to a draft (any post-create stage for
        # {ann}/{id}) -> tender open and a draft already exists. Counts as ready.
        # Must list EVERY stage the draft can be parked at: a prior run that
        # advanced the draft to the price stage (docs_next done, price not yet
        # submitted) redirects /create/ -> /priceoffers/, and omitting that stage
        # made the probe fall through to "no_form" and poll forever.
        if r.status_code == 200 and re.search(
            rf"/application/(?:lots|docs|priceoffers|preview|create)/{announce_id}/\d+",
            final_url
        ):
            return "existing_draft", None
        if r.status_code == 200:
            for m in self._CREATE_FORM_MARKERS:
                if m in r.text:
                    return "ready", m
            return "no_form", None
        # A not-yet-open tender 404s -> /ru/error_report/not_found; the form only
        # materialises at T0. Keep polling (bounded by the deadline).
        return "not_open", None

    async def poll_announce_ready(
        self,
        announce_id: str,
        *,
        interval_s: float = 0.5,
        max_wait_s: float = 1800.0,
        max_inflight: int = 3,
        relogin=None,
    ) -> None:
        """Detect T0 fast by firing create-page probes on a fixed `interval_s`
        cadence, OVERLAPPING up to `max_inflight` at once — instead of strictly
        waiting for each ~1-2s round-trip before the next.

        A redirect to the portal's not-found page means there is no form yet.
        Skip loading that error page so its GET cannot occupy an opening-probe
        slot. Follow other redirects for login, eligibility and existing drafts.
        The first probe to see the form or a draft wins and the rest are cancelled.

        ``relogin``: optional async callback. The cabinet ci_session lives only
        ~5 min, so on a long pre-T0 wait the cookie can die mid-poll and every
        probe starts redirecting to /user/login. If a relogin callback is given
        we re-authenticate (re-sign the ECP nonce) in place and KEEP polling
        instead of raising — so the bot survives a session expiry while waiting
        for the tender to open. Guarded so exactly one relogin runs at a time
        and a fresh cookie gets a chance before we try again. Without a callback
        (manual ci_session, no stored creds) we raise SessionExpired as before.
        """
        import asyncio

        path = f"/ru/application/create/{announce_id}"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max_wait_s
        done = asyncio.Event()
        outcome: dict = {}                       # {"ready": True} | {"error": Exception}
        inflight: set = set()
        seq = 0
        relogin_lock = asyncio.Lock()
        relogin_at = 0.0                          # loop time of last successful relogin

        async def _relogin_once(n: int) -> None:
            nonlocal relogin_at
            if relogin_lock.locked():             # another probe is already re-logging in
                return
            async with relogin_lock:
                # Debounce: a relogin we just did refreshed the jar — let the
                # next few probes try the fresh cookie before re-logging again.
                if loop.time() - relogin_at < 15.0:
                    return
                log.warning("poll.session_expired.relogin", attempt=n)
                try:
                    await relogin()
                except Exception as e:            # noqa: BLE001 — couldn't recover; bail
                    log.error("poll.relogin.failed", attempt=n, err=str(e))
                    if "error" not in outcome:
                        outcome["error"] = SessionExpired(
                            f"poll relogin failed: {e}")
                        done.set()
                    return
                relogin_at = loop.time()
                log.info("poll.relogin.ok", attempt=n)

        async def _probe(n: int) -> None:
            try:
                r = await self._http.get(path, follow_redirects=False)
                if r.next_request is not None:
                    target = r.next_request.url
                    if target.host == httpx.URL(self.base_url).host and target.path.rstrip("/") in (
                        "/ru/error_report/not_found", "/kz/error_report/not_found", "/error_report/not_found",
                    ):
                        return
                    r = await self._http.send(r.next_request, follow_redirects=True)
            except Exception as e:                # one flaky probe -> let cadence retry
                log.debug("poll.probe.error", attempt=n, err=str(e))
                return
            final_url = str(r.url)
            verdict, matched = self._classify_create_poll(r, final_url, announce_id)
            if n == 1 or verdict not in ("not_open",):
                log.info("poll.tick", attempt=n, status=r.status_code,
                         final=final_url, body_len=len(r.content), verdict=verdict)
            if verdict == "login_expired":
                if relogin is not None:
                    await _relogin_once(n)        # refresh the session, keep polling
                elif "error" not in outcome:
                    outcome["error"] = SessionExpired(
                        "poll redirected to /user/login: ci_session expired. "
                        "Run `submit.py login` to refresh.")
                    done.set()
            elif verdict == "access_denied" and "error" not in outcome:
                outcome["error"] = RuntimeError(
                    f"poll → {final_url}: account isn't eligible to bid. "
                    "Check tariff status and agreement signing.")
                done.set()
            elif verdict in ("ready", "existing_draft") and not outcome.get("ready"):
                log.info("poll.ready", attempt=n, final=final_url, matched=matched)
                if verdict == "ready":
                    # Hand the open create form to create_application (saves
                    # it re-GETting the same page). Bounded: one page per tender.
                    while len(self._create_form_html) >= 8:
                        self._create_form_html.pop(next(iter(self._create_form_html)))
                    self._create_form_html[str(announce_id)] = r.text
                outcome["ready"] = True
                done.set()
            elif verdict == "no_form" and n == 1:
                from .parser import _dump
                dump = _dump(r.text, f"poll_no_form_{announce_id}")
                btns = re.findall(r"<(?:button|a)[^>]*?>([^<]{3,60})</(?:button|a)>", r.text)
                btns = [re.sub(r"\s+", " ", t).strip() for t in btns]
                btns = [t for t in btns if any(c.isalpha() for c in t)][:25]
                log.warning("poll.no_form_in_200", dump=str(dump), sample_labels=btns)

        try:
            while not done.is_set() and loop.time() < deadline:
                if len(inflight) < max_inflight:
                    seq += 1
                    task = asyncio.create_task(_probe(seq))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                try:
                    await asyncio.wait_for(done.wait(), timeout=interval_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            pending = list(inflight)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if "error" in outcome:
            raise outcome["error"]
        if not outcome.get("ready"):
            raise TimeoutError(
                f"announce {announce_id}: приём заявок не открылся за "
                f"{max_wait_s:.0f}s (тендер уже закрыт или ID неверный?)")
