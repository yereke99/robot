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

import re
from typing import Mapping, Optional, Sequence
from urllib.parse import urlencode

import httpx
import structlog

log = structlog.get_logger("client")

DEFAULT_BASE_URL = "https://v3bl.goszakup.gov.kz"


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
            cookies["ci_session"] = ci_session
        if agreement_cookie:
            cookies["agreement_user_2024"] = agreement_cookie

        headers = {
            "User-Agent": user_agent or (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru,en;q=0.8",
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
        transport = httpx.AsyncHTTPTransport(
            retries=3,
            http2=(proxy is None),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            proxy=proxy,
        )
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            cookies=cookies,
            headers=headers,
            # connect fails fast (10s) so a refused connection retries quickly;
            # reads get the full timeout_s for goszakup's slow endpoints.
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GoszakupClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---------- isolated sibling sessions ----------

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
        agreement = self._http.cookies.get("agreement_user_2024")
        return GoszakupClient(
            ci_session=None,
            base_url=self.base_url,
            agreement_cookie=agreement,
            timeout_s=self._timeout_s,
            user_agent=self._user_agent,
            proxy=self._proxy,
        )

    def adopt_ci_session(self, ci_session: str) -> None:
        """Replace this client's ci_session with a freshly-minted one (from an
        isolated login). Clears any stale ci_session first so there's exactly one.
        """
        jar = self._http.cookies.jar
        for cookie in list(jar):
            if cookie.name == "ci_session":
                jar.clear(cookie.domain, cookie.path, cookie.name)
        host = httpx.URL(self.base_url).host
        self._http.cookies.set("ci_session", ci_session, domain=host, path="/")

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

    async def _post_form(
        self,
        path: str,
        data: Mapping[str, str | int],
        *,
        referer_path: Optional[str] = None,
    ) -> httpx.Response:
        referer = f"{self.base_url}{referer_path or path}"
        encoded = urlencode(data)
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

    async def _get_page(self, path: str) -> str:
        """GET a goszakup HTML page. Follows 30x redirects -- once an
        application moves to the next stage, the previous URL 302s to the
        current one (e.g., /application/create/{ann} → /application/docs/{ann}/{app_id}).
        """
        log.info("http.get", path=path)
        r = await self._http.get(path, follow_redirects=True)
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
        return r.text

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
    ) -> Optional[str]:
        """POST ajax_create_application. Response is either JSON, or a 302
        whose Location points at /application/lots/{ann}/{app_id}.

        atagos account adds ``tax_payer_type`` and ``consortium`` (observed in
        HAR: subject_address=512489&iik=4497960&contact_phone=&tax_payer_type=UL
        &consortium=0).
        """
        r = await self._post_form(
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
        log.info(
            "create_application.response",
            status=r.status_code,
            location=r.headers.get("location"),
            ctype=r.headers.get("content-type"),
            body=r.text[:500],
        )

        # Surface server-side errors (e.g., "wrong iik for this tender type")
        try:
            j = r.json()
            if isinstance(j, dict) and j.get("status") in ("error", "fail"):
                msg = j.get("message") or j.get("error") or str(j)
                raise RuntimeError(f"ajax_create_application server error: {msg}")
        except RuntimeError:
            raise
        except Exception:
            pass

        app_id = self._extract_app_id_from_response(r, announce_id)
        if app_id:
            return app_id

        # Fallback: GET lots page, follow redirects, parse final URL.
        return await self._discover_application_id(announce_id)

    def _extract_app_id_from_response(
        self, r: httpx.Response, announce_id: str
    ) -> Optional[str]:
        # 1) JSON body
        try:
            j = r.json()
            for k in ("application_id", "app_id", "id"):
                if k in j and j[k]:
                    return str(j[k])
        except Exception:
            pass
        # 2) Location header on a 30x redirect
        loc = r.headers.get("location", "")
        for pat in (
            rf"/application/lots/{announce_id}/(\d+)",
            rf"/application/(?:index|preview|create)/{announce_id}/(\d+)",
            r"/application/[a-z_]+/\d+/(\d+)",
        ):
            m = re.search(pat, loc)
            if m:
                return m.group(1)
        # 3) Body content (sometimes the redirect target is embedded)
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
            r = await self._http.get(candidate_path, follow_redirects=True)
            log.info(
                "discover_app_id.try",
                path=candidate_path,
                final=str(r.url),
                status=r.status_code,
                body_len=len(r.content),
            )
            # 1) Final URL has app_id?
            m = re.search(rf"/application/[a-z_]+/{announce_id}/(\d+)", str(r.url))
            if m:
                return m.group(1)
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
                m = re.search(rf"/application/[a-z_]+/{announce_id}/(\d+)", loc)
                if m:
                    return m.group(1)
            # Save body for off-line inspection
            from .parser import _dump
            dump = _dump(r.text, f"app_id_discovery_{candidate_path.replace('/', '_')}")
            body_dumps.append(str(dump))

        log.error("discover_app_id.exhausted", dumps=body_dumps)
        return None

    async def add_lots(self, announce_id: str, app_id: str, lot_ids: Sequence[int]) -> httpx.Response:
        body = "&".join(f"selectLots%5B%5D={lid}" for lid in lot_ids)
        path = f"/ru/application/ajax_add_lots/{announce_id}/{app_id}"
        referer = f"/ru/application/lots/{announce_id}/{app_id}"
        r = await self._http.post(
            path, content=body,
            headers=_ajax_headers(f"{self.base_url}{referer}"),
        )
        log.info("add_lots", lots=list(lot_ids), status=r.status_code)
        return r

    async def lots_next(self, announce_id: str, app_id: str, *, confirmed: int = 0) -> httpx.Response:
        return await self._post_form(
            f"/ru/application/ajax_lots_next/{announce_id}/{app_id}",
            {"next": 1, "confirmed": confirmed},
            referer_path=f"/ru/application/lots/{announce_id}/{app_id}",
        )

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
        body = "&".join(parts)
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
        userfile[{doc}]={fid}&save_form=&signature[{fid}]=<CMS>.
        """
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
            {
                f"userfile[{doc_id}]": file_id,
                "save_form": "",
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
        return await self._post_form(
            f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}/{lot_id}/{num}",
            data,
            referer_path=f"/ru/application/show_doc/{announce_id}/{app_id}/{doc_id}",
        )

    # ---------- beneficiary (Приложение 1: attach + describe the ID doc) ----------

    async def check_file_status(self, file_ids: Sequence[int]) -> httpx.Response:
        """POST /files/check_status/ — confirm uploaded files passed AV/format
        checks before referencing them. Body: file_id_ar[0]=N&file_id_ar[1]=...
        """
        body = "&".join(f"file_id_ar%5B{i}%5D={fid}" for i, fid in enumerate(file_ids))
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
        body = urlencode(data)
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
        r = await self._post_form(
            f"/ru/application/ajax_get_encr_info/{announce_id}/{app_id}",
            {"lpId": lp_id, "version": version},
            referer_path=f"/ru/application/priceoffers/{announce_id}/{app_id}",
        )
        try:
            return r.json()
        except Exception:
            return {"status": "unknown", "raw": r.text[:1000]}

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
        body = "&".join([
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
        log.info("add_encrypt", item_id=item_id, status=r.status_code)
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
            content="&".join(parts),
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("save_gamma_signs", lps=list(signs_by_lp), status=r.status_code)
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
            content="&".join(parts),
            headers=_ajax_headers(
                f"{self.base_url}/ru/application/priceoffers/{announce_id}/{app_id}"
            ),
        )
        log.info("priceoffers_next_gamma", n=len(prices), status=r.status_code)
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
        body = "&".join(parts)
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
        body = "&".join(parts)
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
        body = "&".join(parts)
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

        # Step 1: get the nonce. Response in HAR was 32 bytes; the actual format
        # may be raw hex or a tiny JSON. Handle both.
        r1 = await self._http.post(
            "/ru/user/sendkey/kz",
            content=b"",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/ru/user/login",
            },
        )
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

        # Step 3: POST the signed XML
        r3 = await self._http.post(
            "/user/sendsign/kz",
            data={"sign": signed_xml},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/ru/user/login",
            },
        )
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

        # Step 5: submit the account password
        r5 = await self._http.post(
            "/ru/user/auth_confirm",
            data={"password": account_password, "agreed_check": "on"},
            headers={"Referer": f"{self.base_url}/ru/user/auth_confirm"},
        )
        loc = r5.headers.get("location", "")
        if r5.status_code != 302 or "/user/login" in loc or "error" in loc:
            raise RuntimeError(
                f"login failed at password step: status={r5.status_code} loc={loc!r} "
                f"body={r5.text[:300]!r}"
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

        ci = self._http.cookies.get("ci_session")
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
        # Server bounced us straight to a draft (lots/docs/preview/create/{ann}/{id})
        # -> tender open and a draft already exists. Counts as ready.
        if r.status_code == 200 and re.search(
            rf"/application/(?:lots|docs|preview|create)/{announce_id}/\d+", final_url
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

        Each probe of /ru/application/create/{id} round-trips in ~1-2s (it follows
        the pre-T0 404 -> /error_report redirect), so a strictly-sequential poll
        only checks every ~2s and can miss the open by that much. Pipelining the
        probes makes detection latency ~`interval_s`, not the request latency: the
        first probe that sees the 200+form (or an existing draft) wins and the
        rest are cancelled. An access_denied redirect fails fast; a not-open
        tender keeps the cadence going until it flips open or the deadline hits.

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
                r = await self._http.get(path, follow_redirects=True)
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
                outcome["ready"] = True
                done.set()
            elif verdict == "no_form" and n == 1:
                from .parser import _dump
                dump = _dump(r.text, f"poll_no_form_{announce_id}")
                btns = re.findall(r"<(?:button|a)[^>]*?>([^<]{3,60})</(?:button|a)>", r.text)
                btns = [re.sub(r"\s+", " ", t).strip() for t in btns]
                btns = [t for t in btns if any(c.isalpha() for c in t)][:25]
                log.warning("poll.no_form_in_200", dump=str(dump), sample_labels=btns)

        while not done.is_set() and loop.time() < deadline:
            if len(inflight) < max_inflight:
                seq += 1
                t = asyncio.ensure_future(_probe(seq))
                inflight.add(t)
                t.add_done_callback(inflight.discard)
            try:
                await asyncio.wait_for(done.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass

        for t in list(inflight):
            t.cancel()
        if "error" in outcome:
            raise outcome["error"]
        if not outcome.get("ready"):
            raise TimeoutError(
                f"announce {announce_id}: приём заявок не открылся за "
                f"{max_wait_s:.0f}s (тендер уже закрыт или ID неверный?)")
