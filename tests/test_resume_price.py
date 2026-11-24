from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from goszakup import flow, tumar
from goszakup.client import GoszakupClient
from goszakup.state import StateStore


BASE = "https://portal.test"
FOREIGN = "https://other.test"
ANN = "100"
APP = "200"
PRICE = f"/ru/application/priceoffers/{ANN}/{APP}"
DOCS = f"/ru/application/docs/{ANN}/{APP}"
TECH = f"/ru/application/show_doc/{ANN}/{APP}/3334"


def _price_page(public_key: str = "CURRENT", *, lp: int = 400) -> str:
    return ('<script>current_method = "priceoffers"</script>'
            f'<input type="hidden" id="public_key" value="{public_key}">'
            f'<input class="offer_gamma" app_lot_id="300" buy_lot_point_id="{lp}">')


def _configure_gamma(client):
    provider = SimpleNamespace(
        _autofill=lambda _: True,
        version="2.0.0.0",
        encrypt_offer_price=AsyncMock(return_value=tumar.EncryptResult("ENC", "KEY", "INFO", "SIGN")),
    )
    for method in ("add_encrypt", "save_gamma_signs", "priceoffers_next_gamma"):
        setattr(client, method, AsyncMock(return_value=httpx.Response(200, json={"status": "ok"})))
    client.public_application = AsyncMock()
    return provider


def _register_encryption_info(router):
    return router.post(f"{BASE}/ru/application/ajax_get_encr_info/{ANN}/{APP}").respond(
        200, json={"info": "5_400;100;70;", "salt": "CURRENT-SALT", "sign": "SIGN"},
    )


@pytest.mark.parametrize("proof", ["same", "missing_key", "wrong_app", "foreign_origin"])
async def test_resuming_from_discovery_reuses_only_an_authenticated_price_page(proof, tmp_path):
    async with respx.mock(assert_all_called=False) as router:
        wrong_app = f"/ru/application/priceoffers/{ANN}/201"
        target = {"wrong_app": wrong_app, "foreign_origin": FOREIGN + PRICE}.get(proof, PRICE)
        router.get(BASE + TECH).respond(302, headers={"location": target})
        router.get(BASE + DOCS).respond(200, html="<html></html>")
        current_price = router.get(BASE + PRICE)
        if proof == "missing_key":
            current_price.mock(side_effect=[
                httpx.Response(200, html=_price_page("")),
                httpx.Response(200, html=_price_page()),
            ])
        else:
            current_price.respond(200, html=_price_page())
        if proof == "wrong_app":
            router.get(BASE + wrong_app).respond(200, html=_price_page("WRONG", lp=900))
        if proof == "foreign_origin":
            router.get(FOREIGN + PRICE).respond(200, html=_price_page("WRONG", lp=900))
        encr_info = _register_encryption_info(router)

        async with GoszakupClient(base_url=BASE, ci_session="current-session") as client:
            provider = _configure_gamma(client)
            client.create_application = AsyncMock(return_value=APP)
            client.add_lots = AsyncMock()
            client.lots_next = AsyncMock()
            params = flow.AtagosBidParams(
                announce_id=ANN, tender_lot_ids=[1], dry_run=True, early_app_lots=False,
                doc_plan=[], tumar=provider, planned_price_ratio=0.80,
                signer=SimpleNamespace(sign_text_with_tsa=AsyncMock(return_value="CMS")),
            )
            state = await flow.submit_bid(
                params=params, ci_session="current-session", gz_client=client, pre_t0_poll=False,
                company={"subject_address": "address", "iik": "account"},
                beneficiary=None, state_store=StateStore(tmp_path),
            )

            assert state.step == "dry_run_complete"
            assert current_price.call_count == (2 if proof == "missing_key" else 1)
            assert encr_info.call_count == 1
            info = provider.encrypt_offer_price.await_args.args[0]
            assert (info.public_key, info.salt, info.bidding_price) == ("CURRENT", "CURRENT-SALT", 80.0)
            client.public_application.assert_not_awaited()


async def test_resume_price_page_is_refetched_if_session_cookies_change():
    async with respx.mock(assert_all_called=False) as router:
        router.get(BASE + TECH).respond(302, headers={"location": PRICE})
        current_price = router.get(BASE + PRICE).mock(side_effect=[
            httpx.Response(200, html=_price_page("PREVIOUS")),
            httpx.Response(200, html=_price_page()),
        ])
        _register_encryption_info(router)
        async with GoszakupClient(base_url=BASE, ci_session="previous-session") as client:
            provider = _configure_gamma(client)
            params = flow.AtagosBidParams(announce_id=ANN, tender_lot_ids=[1], tumar=provider, doc_plan=[])
            with pytest.raises(flow.AlreadyAtPriceStage) as detected:
                await flow._discover_app_lots(client, params, APP)
            assert detected.value.fetched_page is not None

            client.adopt_ci_session("current-session")
            signer = SimpleNamespace(sign_xml_batch=AsyncMock(return_value={400: "CMS"}))
            async with AsyncExitStack() as stack:
                await flow._gamma_price(
                    client, signer, params, APP, stack, fetched_price_page=detected.value.fetched_page,
                )

            assert current_price.call_count == 2
            assert provider.encrypt_offer_price.await_args.args[0].public_key == "CURRENT"
