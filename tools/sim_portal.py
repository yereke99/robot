"""End-to-end simulation of one goszakup bid against a stateful fake portal.

Runs the REAL GoszakupClient + flow.submit_bid (from whichever package root is
first on sys.path) over a respx-mocked HTTP layer, on a VIRTUAL clock: every
portal request "takes" a configured latency, but nothing actually sleeps, so a
45-second submission is measured in milliseconds and is fully deterministic.

The fake portal enforces the ordering rules the bot depends on and records any
violation, so this doubles as a correctness check of the orchestration.

    python sim_portal.py <scenario-json>      -> prints one JSON result line
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl

import httpx
import respx

BASE = "https://v3bl.goszakup.gov.kz"
ANN = "17905992"
APP = "73859197"
PAD = "<div>" + "x" * 6000 + "</div>"


class VirtualLoop(asyncio.SelectorEventLoop):
    """asyncio loop whose clock jumps straight to the next timer (no real waiting)."""

    def __init__(self):
        super().__init__()
        self._vtime = 0.0
        self._idle_spins = 0
        real_select = self._selector.select

        def select(timeout=None):
            if timeout is None:
                self._idle_spins += 1
                if self._idle_spins > 2000:
                    raise RuntimeError("simulation deadlock: nothing scheduled")
            else:
                self._idle_spins = 0
                if timeout > 0:
                    self._vtime += timeout
            return real_select(0)

        self._selector.select = select

    def time(self):
        return self._vtime


class Portal:
    def __init__(self, loop, cfg):
        self.loop = loop
        self.G = cfg.get("G", 1.6)                 # page GET latency
        self.P = cfg.get("P", 0.7)                 # ajax POST latency
        self.public_delay = cfg.get("public_delay", self.P)
        self.docs_get = cfg.get("docs_get", self.G)
        self.tumar_version = cfg.get("tumar_version", "1.0.13.2285")
        self.open_at = cfg.get("open_at", 30.0)    # tender opens (virtual s)
        self.lag = cfg.get("lag", 3.0)             # тех.спец validation lag after the last page save
        self.dl_base = cfg.get("dl_base", 0.3)
        self.dl_rate = cfg.get("dl_rate", 1.5e6)   # bytes/s for file downloads
        self.n_ts_pages = cfg.get("ts_pages", 2)
        self.lots = cfg.get("lots", 1)
        self.drop_first_save = set(cfg.get("drop_first_save", []))   # doc ids whose 1st save is "clobbered"
        self.drop_ts_first_save = set(cfg.get("drop_ts_first_save", []))  # тех.спец page nums lost once
        self.ts_attempts: dict[tuple[int, int], int] = {}
        self.bankruptcy_post_has_link = cfg.get("bankruptcy_post_has_link", False)
        self.lock = asyncio.Lock() if cfg.get("session_lock") else None

        self.lot_ids = [41000001 + i for i in range(self.lots)]
        self.app_lots = {lid: 85000001 + i for i, lid in enumerate(self.lot_ids)}
        self.lp_ids = {al: 41391061 + i for i, al in enumerate(self.app_lots.values())}

        self.log: list[dict] = []
        self.violations: list[str] = []
        self.app_created = False
        self.lots_added = False
        self.stage = None                          # lots | docs | priceoffers | preview | public
        self.next_fid = 900000001
        self.generated: dict[int, int] = {}        # doc -> fid
        self.signed: set[int] = set()
        self.save_attempts: dict[int, int] = {}
        self.beneficiary_saved = False
        self.deposit_added: set[int] = set()
        self.deposit_saved: set[int] = set()
        self.ts_saved: dict[tuple[int, int], float] = {}   # (app_lot, num) -> commit time
        self.ts_inflight = 0
        self.parent_get_since_last_ts_save = True
        self.last_ts_save_at = None
        self.salts: dict[int, str] = {}
        self.encr_calls = 0
        self.add_encrypt_done: set[int] = set()
        self.signs_saved = False
        self.t_public_request = None
        self.t_public = None
        self.t_create_post = None
        self.sizes: dict[int, int] = {}            # fid -> bytes

    # ------------------------------------------------------------------ helpers
    def now(self) -> float:
        return self.loop.time()

    def bad(self, msg: str) -> None:
        self.violations.append(f"t={self.now():.2f} {msg}")

    def ts_fids(self, app_lot: int, num: int) -> list[int]:
        base = 700000000 + (app_lot % 1000) * 100 + num * 10
        if num == 1:
            fids = [base + 1]
            sizes = [180_000]
        else:
            fids = [base + 1, base + 2]
            sizes = [1_700_000, 2_200_000]
        for f, s in zip(fids, sizes):
            self.sizes[f] = s
        return fids

    async def serve(self, request, latency, check_and_build):
        """Session lock (optional) -> evaluate at arrival -> latency -> commit."""
        t0 = self.now()
        path = request.url.path + (("?" + request.url.query.decode()) if request.url.query else "")
        entry = {"t0": round(t0, 3), "m": request.method, "path": path}
        self.log.append(entry)

        async def run():
            commit = check_and_build()            # returns (response, commit_fn|None)
            resp, commit_fn = commit if isinstance(commit, tuple) else (commit, None)
            await asyncio.sleep(latency)
            if commit_fn:
                commit_fn()
            return resp

        if self.lock:
            async with self.lock:
                resp = await run()
        else:
            resp = await run()
        entry["t1"] = round(self.now(), 3)
        entry["status"] = resp.status_code
        return resp

    def redirect_to_stage(self):
        if self.stage in ("priceoffers",):
            return httpx.Response(302, headers={"location": f"/ru/application/priceoffers/{ANN}/{APP}"})
        if self.stage in ("preview", "public"):
            return httpx.Response(302, headers={"location": f"/ru/application/preview/{ANN}/{APP}"})
        if self.stage == "lots":
            return httpx.Response(302, headers={"location": f"/ru/application/lots/{ANN}/{APP}"})
        return httpx.Response(302, headers={"location": f"/ru/application/docs/{ANN}/{APP}"})

    # ------------------------------------------------------------------- pages
    def docs_page(self) -> str:
        rows = [
            (1253, "Приложение 4 (Заявка на участие в конкурсе)"),
            (3359, "Приложение 1 (Сведения о бенефициарном собственнике)"),
            (3334, "Приложение 15 (Техническая спецификация)"),
            (3715, "Приложение 2 (Соглашение об участии в конкурсе)"),
            (3335, "Приложение 5 (Сведения об отсутствии банкротства)"),
            (3328, "Приложение 19 (Обеспечение заявки)"),
        ]
        body = "".join(
            f'<tr><td>{title}</td><td><a href="/ru/application/show_doc/{ANN}/{APP}/{d}">Перейти</a></td></tr>'
            for d, title in rows)
        return f'<script>current_method = "docs"</script><table>{body}</table>' + PAD

    def doc_page(self, doc: int) -> str:
        if doc == 3334:
            rows = "".join(
                f'<tr><td>{lid}</td><td>'
                f'<a href="/ru/application/show_doc/{ANN}/{APP}/3334/{al}/1">Перейти</a></td></tr>'
                for lid, al in self.app_lots.items())
            return f"<table>{rows}</table>" + PAD
        if doc in self.generated:
            fid = self.generated[doc]
            return f'<a href="/files/download_file/{fid}/{fid + 7}/">документ</a>' + PAD
        return '<input type="submit" name="generate" value="Сформировать документ">' + PAD

    def ts_page(self, app_lot: int, num: int) -> str:
        if num <= self.n_ts_pages:
            links = "".join(
                f'<a href="/files/download_file/{f}/">файл</a>' for f in self.ts_fids(app_lot, num))
            return "<h3>Техническая спецификация</h3>" + links + PAD
        if num == self.n_ts_pages + 1:
            return ('<h3>Техническая спецификация</h3><form id="form_uploading">'
                    '<input type="file" name="userfile[0]"><input type="submit" value="Сохранить">'
                    "</form>" + PAD)
        return "<html><body></body></html>"

    def price_page(self) -> str:
        inputs = "".join(
            f'<tr><td><input class="offer_gamma" app_lot_id="{al}" buy_lot_point_id="{lp}" '
            f'data-planed-price="2221307.14"></td></tr>'
            for al, lp in self.lp_ids.items())
        return (f'<script>current_method = "priceoffers"</script>'
                f'<input type="hidden" id="public_key" value="PUBKEY"><table>{inputs}</table>' + PAD)

    # ------------------------------------------------------------ docs_next rule
    def docs_missing(self):
        lotname = "85996082-ОК2"
        if 3359 not in self.signed:
            return f"Подпишите прикрепленный документ в категории Приложение 1 у лота № {lotname}"
        if 3715 not in self.signed:
            return "Подпишите прикрепленный документ в категории Приложение 2"
        if 1253 not in self.signed:
            return "Подпишите прикрепленный документ в категории Приложение 4"
        if 3335 not in self.signed:
            return "Подпишите прикрепленный документ в категории Приложение 5"
        for al in self.app_lots.values():
            for num in range(1, self.n_ts_pages + 1):
                if (al, num) not in self.ts_saved:
                    return ("Подпишите прикрепленный документ в категории Приложение 15 "
                            f"(Техническая спецификация) у лота № {lotname}")
        if self.last_ts_save_at is not None and self.now() < self.last_ts_save_at + self.lag:
            return ("Подпишите прикрепленный документ в категории Приложение 15 "
                    f"(Техническая спецификация) у лота № {lotname}")
        for al in self.app_lots.values():
            if al not in self.deposit_saved:
                return "Прикрепите документ в категории Приложение 19 (Обеспечение заявки)"
        return None

    # ------------------------------------------------------------------ routing
    def install(self, r: respx.MockRouter) -> None:
        P, G = self.P, self.G

        @r.route(method="HEAD", path="/ru/")
        async def head(request):
            return await self.serve(request, 0.15, lambda: httpx.Response(200))

        @r.get(url__regex=rf".*/ru/application/create/{ANN}$")
        async def create_form(request):
            def build():
                if self.now() < self.open_at:
                    return httpx.Response(302, headers={"location": "/ru/error_report/not_found"})
                if self.app_created:
                    return self.redirect_to_stage()
                form = ('<form action="ajax_create_application">'
                        '<select name="subject_address"><option value="165932">a</option></select>'
                        '<select name="iik"><option value="2589827">i</option></select>'
                        '<input name="contact_phone"></form>')
                return httpx.Response(200, html=form + PAD)
            return await self.serve(request, G, build)

        @r.get(url__regex=r".*/ru/error_report/not_found$")
        async def not_found(request):
            return await self.serve(request, G * 0.4, lambda: httpx.Response(404, html="<h1>404</h1>"))

        @r.post(url__regex=rf".*/ajax_create_application/{ANN}$")
        async def create(request):
            def build():
                if self.now() < self.open_at:
                    self.bad("create before open")
                    return httpx.Response(200, json={"status": "error", "message": "не открыт"})
                if self.app_created:
                    self.bad("DUPLICATE create_application")
                if self.t_create_post is None:
                    self.t_create_post = self.now()

                def commit():
                    self.app_created = True
                    self.stage = "lots"
                return httpx.Response(200, json={"status": "ok", "application_id": APP}), commit
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ajax_add_lots/{ANN}/{APP}$")
        async def add_lots(request):
            def build():
                if self.stage != "lots":
                    self.bad(f"add_lots at stage {self.stage}")

                def commit():
                    self.lots_added = True
                return httpx.Response(200, json={"status": "ok"}), commit
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ajax_lots_next/{ANN}/{APP}$")
        async def lots_next(request):
            def build():
                if not self.lots_added:
                    self.bad("lots_next before add_lots committed")

                def commit():
                    self.stage = "docs"
                return httpx.Response(200, json={"status": "ok"}), commit
            return await self.serve(request, P, build)

        @r.get(url__regex=rf".*/ru/application/docs/{ANN}/{APP}$")
        async def docs(request):
            def build():
                if self.stage != "docs":
                    return self.redirect_to_stage() if self.stage != "lots" else httpx.Response(200, html="lots")
                return httpx.Response(200, html=self.docs_page())
            return await self.serve(request, self.docs_get, build)

        @r.get(url__regex=rf".*/ru/application/lots/{ANN}/{APP}$")
        async def lots_page(request):
            return await self.serve(request, G, lambda: httpx.Response(200, html="<html>lots</html>" + PAD))

        @r.get(url__regex=rf".*/ru/application/show_doc/{ANN}/{APP}/(\d+)$")
        async def show_doc(request):
            doc = int(request.url.path.rsplit("/", 1)[-1])

            def build():
                if self.stage != "docs":
                    if self.stage == "lots":
                        self.bad(f"show_doc/{doc} GET before lots_next committed")
                    return self.redirect_to_stage()
                if doc == 3334:
                    self.parent_get_since_last_ts_save = True
                return httpx.Response(200, html=self.doc_page(doc))
            return await self.serve(request, G, build)

        @r.get(url__regex=rf".*/ru/application/show_doc/{ANN}/{APP}/3334/(\d+)/(\d+)$")
        async def ts_page(request):
            parts = request.url.path.split("/")
            app_lot, num = int(parts[-2]), int(parts[-1])

            def build():
                if self.stage != "docs":
                    return self.redirect_to_stage()
                if app_lot not in self.lp_ids:
                    self.bad(f"тех.спец page for unknown app_lot {app_lot}")
                return httpx.Response(200, html=self.ts_page(app_lot, num))
            return await self.serve(request, G, build)

        @r.get(url__regex=r".*/files/download_file/(\d+)/(?:(\d+)/)?$")
        async def download(request):
            fid = int(re.search(r"download_file/(\d+)", request.url.path).group(1))
            size = self.sizes.get(fid, 60_000)
            latency = self.dl_base + size / self.dl_rate
            return await self.serve(
                request, latency, lambda: httpx.Response(200, content=f"file-{fid}".encode()))

        @r.post(url__regex=rf".*/ru/application/show_doc/{ANN}/{APP}/(\d+)$")
        async def doc_post(request):
            doc = int(request.url.path.rsplit("/", 1)[-1])
            form = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
            sig_keys = [k for k in form if k.startswith("signature[")]

            def build():
                if self.stage != "docs":
                    self.bad(f"POST show_doc/{doc} at stage {self.stage}")
                    return self.redirect_to_stage()
                loc = {"location": f"/ru/application/show_doc/{ANN}/{APP}/{doc}"}
                if sig_keys:                                     # signature commit
                    fid = int(sig_keys[0][len("signature["):-1])
                    if self.generated.get(doc) != fid:
                        self.bad(f"save doc {doc} with fid {fid}, generated {self.generated.get(doc)}")
                    if doc == 3335 and form.get("btn") != "docSave":
                        self.bad("bankruptcy save without btn=docSave")
                    n = self.save_attempts.get(doc, 0) + 1
                    self.save_attempts[doc] = n

                    def commit():
                        if doc in self.drop_first_save and n == 1:
                            return                               # the "clobbered" save
                        self.signed.add(doc)
                    return httpx.Response(302, headers=loc), commit
                if form.get("btn") == "docGen":                  # bankruptcy generate
                    fid = self.next_fid
                    self.next_fid += 1

                    def commit():
                        self.generated[doc] = fid
                    if self.bankruptcy_post_has_link:
                        page = f'<a href="/files/download_file/{fid}/{fid + 7}/">документ</a>' + PAD
                        return httpx.Response(200, html=page), commit
                    return httpx.Response(302, headers=loc), commit
                if "generate" in form:                           # generate / agree
                    if doc == 3359 and not self.beneficiary_saved:
                        self.bad("beneficiary generate before ajax_save_info")
                    fid = self.next_fid
                    self.next_fid += 1

                    def commit():
                        self.generated[doc] = fid
                    page = f'<a href="/files/download_file/{fid}/{fid + 7}/">документ</a>' + PAD
                    return httpx.Response(200, html=page), commit
                self.bad(f"unrecognised POST show_doc/{doc}: {sorted(form)}")
                return httpx.Response(200, html="?")
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ru/application/show_doc/{ANN}/{APP}/(\d+)/(\d+)/(\d+)/?(\?.*)?$")
        async def lot_post(request):
            parts = [p for p in request.url.path.split("/") if p]
            doc, app_lot, num = int(parts[-3]), int(parts[-2]), int(parts[-1])
            form = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
            show3 = request.url.query == b"show=3"

            def build():
                if self.stage != "docs":
                    self.bad(f"lot POST doc {doc} at stage {self.stage}")
                    return self.redirect_to_stage()
                loc = {"location": f"/ru/application/show_doc/{ANN}/{APP}/{doc}"}
                if doc == 3328:
                    if show3 and "save_electronic_data" in form:
                        if app_lot not in self.deposit_added:
                            self.bad("deposit_save before deposit_add")
                        return httpx.Response(302, headers=loc), (lambda: self.deposit_saved.add(app_lot))
                    return httpx.Response(200, html="ok"), (lambda: self.deposit_added.add(app_lot))
                # тех.спец page save
                sigs = sorted(int(k[len("signature["):-1]) for k in form if k.startswith("signature["))
                if sigs != sorted(self.ts_fids(app_lot, num)):
                    self.bad(f"тех.спец save {app_lot}/{num}: signatures for {sigs}")
                if self.ts_inflight:
                    self.bad("two тех.спец page saves OVERLAP")
                if not self.parent_get_since_last_ts_save:
                    self.bad("тех.спец page saved without the GET barrier after the previous page")
                self.ts_inflight += 1

                n_try = self.ts_attempts.get((app_lot, num), 0) + 1
                self.ts_attempts[(app_lot, num)] = n_try

                def commit():
                    self.ts_inflight -= 1
                    self.parent_get_since_last_ts_save = False
                    if num in self.drop_ts_first_save and n_try == 1:
                        return                                   # the lost save
                    self.ts_saved[(app_lot, num)] = self.now()
                    self.last_ts_save_at = self.now()
                return httpx.Response(302, headers=loc), commit
            return await self.serve(request, P * 1.3, build)

        @r.post(url__regex=r".*/files/check_status/$")
        async def check_status(request):
            return await self.serve(request, P, lambda: httpx.Response(200, json={"status": 1}))

        @r.post(url__regex=r".*/ru/beneficiary/ajax_save_info$")
        async def ben_save(request):
            def build():
                return httpx.Response(200, json={"status": 1}), (lambda: setattr(self, "beneficiary_saved", True))
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ajax_docs_next/{ANN}/{APP}$")
        async def docs_next(request):
            def build():
                if self.stage != "docs":
                    self.bad(f"docs_next at stage {self.stage}")
                    return httpx.Response(200, json={"status": "ok"})
                missing = self.docs_missing()
                if missing:
                    return httpx.Response(200, json={"status": "error", "message": missing})
                return httpx.Response(200, json={"status": "ok"}), (lambda: setattr(self, "stage", "priceoffers"))
            return await self.serve(request, P, build)

        @r.get(url__regex=rf".*/ru/application/priceoffers/{ANN}/{APP}$")
        async def priceoffers(request):
            def build():
                if self.stage != "priceoffers":
                    return self.redirect_to_stage()
                return httpx.Response(200, html=self.price_page())
            return await self.serve(request, G, build)

        @r.post(url__regex=rf".*/ajax_get_encr_info/{ANN}/{APP}$")
        async def encr_info(request):
            form = dict(parse_qsl(request.content.decode()))

            def build():
                if self.stage != "priceoffers":
                    self.bad("get_encr_info outside the price stage")
                    return httpx.Response(200, json={"status": "error", "message": "not at price stage"})
                if form.get("version") != self.tumar_version:
                    self.bad("get_encr_info did not use the active provider version")
                lp = int(form["lpId"])
                self.encr_calls += 1
                salt = f"SALT-{lp}-{self.encr_calls}"
                self.salts[lp] = salt                             # the LATEST salt is the valid one
                return httpx.Response(200, json={
                    "plnSum": "2221307.14", "minPrice": "1777045.72", "salt": salt,
                    "sign": "SRVSIGN", "info": f"566386_{lp};2221307.14;1777045.72;"})
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ajax_add_encrypt/{ANN}/{APP}$")
        async def add_encrypt(request):
            form = dict(parse_qsl(request.content.decode()))

            def build():
                lp = int(form["itemID"])
                if self.stage != "priceoffers":
                    self.bad("add_encrypt outside the price stage")
                if form.get("salt") != self.salts.get(lp):
                    self.bad(f"add_encrypt with a STALE salt {form.get('salt')} != {self.salts.get(lp)}")
                return httpx.Response(200, json={"status": "ok"}), (lambda: self.add_encrypt_done.add(lp))
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ajax_save_gamma_signs/{ANN}/{APP}$")
        async def save_signs(request):
            def build():
                if self.add_encrypt_done != set(self.lp_ids.values()):
                    self.bad("save_gamma_signs before every add_encrypt committed")
                return httpx.Response(200, json={"status": "ok"}), (lambda: setattr(self, "signs_saved", True))
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ajax_priceoffers_next/{ANN}/{APP}$")
        async def price_next(request):
            def build():
                if not self.signs_saved:
                    self.bad("priceoffers_next before save_gamma_signs committed")
                return httpx.Response(200, json={"status": "ok"}), (lambda: setattr(self, "stage", "preview"))
            return await self.serve(request, P, build)

        @r.post(url__regex=rf".*/ajax_public_application/{ANN}/{APP}$")
        async def public(request):
            if self.t_public_request is not None:
                self.bad("DUPLICATE public_application")
            self.t_public_request = self.now()

            def build():
                if self.stage != "preview":
                    self.bad(f"public_application at stage {self.stage}")
                    return httpx.Response(200, json={"status": "error", "message": "not ready"})

                def commit():
                    self.stage = "public"
                    self.t_public = self.now()
                return httpx.Response(200, json={"status": "ok"}), commit
            return await self.serve(request, self.public_delay, build)

        @r.route()
        async def anything_else(request):
            self.bad(f"UNEXPECTED request {request.method} {request.url.path}")
            return httpx.Response(404, html="unexpected")


async def run_scenario(cfg: dict) -> dict:
    from goszakup import flow, tumar
    from goszakup.client import GoszakupClient
    from goszakup.state import StateStore

    loop = asyncio.get_event_loop()
    portal = Portal(loop, cfg)

    class FakeKalkan:
        key_id = "sim"

        async def sign_bytes(self, data: bytes) -> str:
            await asyncio.sleep(0.05)
            return "SIG-" + data.decode()[:20]

        async def sign_text_with_tsa(self, text: str) -> str:
            await asyncio.sleep(cfg.get("tsa", 0.4))
            return "CMS-TSA"

        async def sign_xml(self, xml: str) -> str:
            return "<signed/>"

    class FakeTumar:
        version = portal.tumar_version
        _autofill = object()

        async def encrypt_offer_price(self, info):
            await asyncio.sleep(cfg.get("popup", 0.8))
            return tumar.EncryptResult(
                encrypted_data=f"ENC-{info.id_priceoffer}", session_key="KEY",
                info="SN", sign="OUTSIGN")

    async def open_tumar(params, stack):
        await asyncio.sleep(cfg.get("tumar_warmup", 0.0))
        return FakeTumar()

    tz_cache = dict(cfg.get("tz_cache", {}))
    lp_cache = {ANN: [41391061]} if cfg.get("lp_cache") else {}
    with tempfile.TemporaryDirectory() as tmp, patch.object(flow, "_open_tumar", open_tumar):
        async with respx.mock(base_url=BASE, assert_all_called=False) as router:
            portal.install(router)
            async with GoszakupClient(ci_session="dummy") as gz:
                params = flow.AtagosBidParams(
                    announce_id=ANN, tender_lot_ids=list(portal.lot_ids), price=None,
                    dry_run=cfg.get("dry_run", False), signer=FakeKalkan(),
                    tumar=None if "tumar_warmup" in cfg else FakeTumar(),
                    stop_after_docs=cfg.get("two_phase", False),
                    skip_initial_login=True,
                    auto_login_account_password=(None if cfg.get("no_password") else "pw"),
                    doc_save_mode=cfg.get("mode", "parallel_sessions"), tumar_api_key="k",
                    max_poll_wait_s=600.0,
                )
                error = None
                state = None
                try:
                    bid_args = dict(
                        ci_session="dummy",
                        company={"subject_address": "165932", "iik": "2589827",
                                 "contact_phone": "87000000000"},
                        beneficiary=flow.BeneficiaryInfo(
                            name="X", citizenship="398", res_country="398",
                            doc_number="1", doc_date="2007-12-21",
                            options={"option_1": 0, "option_4": 1},
                            id_pdf_path="", id_file_id=307218850),
                        state_store=StateStore(Path(tmp)),
                        gz_client=gz, lp_id_cache=lp_cache, tz_sig_cache=tz_cache,
                        poll_interval_s=0.2,
                    )
                    state = await flow.submit_bid(params=params, **bid_args)
                    if cfg.get("two_phase") and state.step == "docs_ready":
                        if portal.encr_calls or portal.stage != "priceoffers":
                            portal.bad("prepare did not stop at the start of the price stage")
                        resume = replace(params, stop_after_docs=False, resume_app_id=state.application_id)
                        state = await flow.submit_bid(params=resume, pre_t0_poll=False, **bid_args)
                except Exception as e:  # noqa: BLE001
                    error = f"{type(e).__name__}: {e}"

    opened = portal.open_at
    after_open = [e for e in portal.log if e["t0"] >= opened]
    return {
        "step": getattr(state, "step", None),
        "error": error or getattr(state, "error", None),
        "violations": portal.violations,
        "t_open": opened,
        "t_create_post": portal.t_create_post,
        "t_public": portal.t_public,
        "to_public_request_s": (round(portal.t_public_request - opened, 2)
                                if portal.t_public_request is not None else None),
        "from_open_s": round(portal.t_public - opened, 2) if portal.t_public else None,
        "requests_after_open": len(after_open),
        "encr_info_calls": portal.encr_calls,
        "save_attempts": portal.save_attempts,
        "log": after_open if cfg.get("want_log") else None,
    }


def main() -> None:
    cfg = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    import logging
    import structlog
    level = logging.INFO if cfg.get("verbose") else logging.CRITICAL
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level))
    loop = VirtualLoop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(run_scenario(cfg))
    finally:
        loop.close()
    print("RESULT " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
