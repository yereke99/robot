from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from goszakup.bot.context import BotContext
from goszakup.bot.handlers import common
from goszakup.bot.search import probe_portal_session
from goszakup.client import GoszakupClient


BASE = "https://v3bl.goszakup.gov.kz"
PROFILE = f"{BASE}/cabinet/profile"
OLD_SESSION = "old-example-session"
FRESH_SESSION = "fresh-example-session"


@pytest.mark.parametrize(("status_code", "expect"), [(401, "expired"), (503, "unavailable")])
async def test_profile_distinguishes_expiry_from_portal_failure(status_code, expect):
    async with respx.mock as router, GoszakupClient(ci_session=OLD_SESSION) as gz:
        router.get(PROFILE).respond(status_code)
        assert await probe_portal_session(gz) == expect


async def test_profile_rejects_inline_login_form_with_success_status():
    async with respx.mock as router, GoszakupClient(ci_session=OLD_SESSION) as gz:
        router.get(PROFILE).respond(200, html="<form action='/ru/user/login'><input name='username'></form>")
        assert await probe_portal_session(gz) == "expired"


async def test_profile_checks_safe_locale_redirect_without_rotating_cookie():
    async with respx.mock as router, GoszakupClient(ci_session=OLD_SESSION) as gz:
        router.get(PROFILE).respond(302, headers={"Location": "/ru/cabinet/profile"})
        locale = router.get(f"{BASE}/ru/cabinet/profile").respond(
            200, html="Личный кабинет", headers={"Set-Cookie": "ci_session=unverified; Path=/"},
        )
        assert await probe_portal_session(gz) == "active"
        assert locale.calls.last.request.headers["Cookie"] == f"ci_session={OLD_SESSION}"
        assert gz.get_cookie("ci_session") == OLD_SESSION


@pytest.mark.parametrize(("location", "expect"), [
    ("/ru/user/login", "expired"),
    ("https://zakup.gov.kz/authorize", "sso"),
    ("https://unrelated.example/cabinet/profile", "unavailable"),
])
async def test_profile_never_follows_login_or_cross_origin_redirect(location, expect):
    async with respx.mock as router, GoszakupClient(ci_session=OLD_SESSION) as gz:
        request = router.get(PROFILE).respond(302, headers={"Location": location})
        assert await probe_portal_session(gz) == expect
        assert len(router.calls) == request.call_count == 1


def _cookie_command(gz, *, chat_type="private", user_id=1):
    cfg = SimpleNamespace(ci_session_service="test", telegram=SimpleNamespace(allowed_user_ids={1}))
    ctx = BotContext(cfg, None, None, gz_client=gz, _ci_session=f"ci_session={OLD_SESSION}")
    send = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                             effective_chat=SimpleNamespace(type=chat_type, send_message=send))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"ctx": ctx}))
    return ctx, update, context, send


@pytest.mark.parametrize("raw", [
    FRESH_SESSION,
    f"ci_session={FRESH_SESSION}",
    f'curl "{PROFILE}" --cookie "ci_session={FRESH_SESSION}; other=1"',
])
async def test_cookie_activates_verified_session_and_notifies_waiter(monkeypatch, raw):
    secrets = {("test", "cookie_header"): f"ci_session={OLD_SESSION}; other=stale"}
    monkeypatch.setattr(common.gz_secrets, "set",
                        lambda service, key, value: secrets.__setitem__((service, key), value))
    monkeypatch.setattr(common.gz_secrets, "get", lambda service, key: secrets.get((service, key)))

    def profile(request: httpx.Request) -> httpx.Response:
        assert f"ci_session={FRESH_SESSION}" in request.headers["Cookie"]
        assert OLD_SESSION not in request.headers["Cookie"]
        return httpx.Response(200)

    async with respx.mock as router, GoszakupClient(ci_session=OLD_SESSION) as gz:
        router.get(PROFILE).mock(side_effect=profile)
        ctx, update, command, send = _cookie_command(gz)
        before = ctx.portal_session_changed
        await common.accept_portal_cookie(update, command, raw)
        assert before.is_set()
        assert gz.get_cookie("ci_session") == FRESH_SESSION
    assert BotContext(ctx.config, None, None).ci_session() == ctx.ci_session()
    assert secrets[("test", "ci_session")] == FRESH_SESSION
    assert "Сессия проверена" in send.call_args.args[0]


async def test_expired_replacement_cannot_drop_previous_session(monkeypatch):
    def unexpected_save(*_):
        pytest.fail("An expired cookie must not be persisted")

    monkeypatch.setattr(common.gz_secrets, "set", unexpected_save)
    async with respx.mock as router, GoszakupClient(ci_session=OLD_SESSION) as gz:
        router.get(PROFILE).respond(302, headers={"Location": "https://zakup.gov.kz/"})
        ctx, update, command, send = _cookie_command(gz)
        await common.accept_portal_cookie(update, command, FRESH_SESSION)
        assert not ctx.portal_session_changed.is_set()
        assert gz.get_cookie("ci_session") == OLD_SESSION
    assert "сессия истекла" in send.call_args.args[0]


async def test_cookie_check_cannot_replace_a_session_installed_during_its_probe(monkeypatch):
    other_session = "recently-installed-session"
    async with GoszakupClient(ci_session=OLD_SESSION) as gz:
        ctx, update, command, send = _cookie_command(gz)

        async def validate(_):
            ctx.set_ci_session(f"ci_session={other_session}")
            return "active"

        monkeypatch.setattr(common, "probe_portal_session", validate)
        await common.accept_portal_cookie(update, command, FRESH_SESSION)
        assert gz.get_cookie("ci_session") == other_session
    assert "уже обновилась" in send.call_args.args[0]


@pytest.mark.parametrize(("chat_type", "user_id"), [("group", 1), ("private", 2)])
async def test_cookie_replacement_requires_an_authorized_private_chat(chat_type, user_id):
    ctx, update, command, send = _cookie_command(None, chat_type=chat_type, user_id=user_id)
    await common.accept_portal_cookie(update, command, FRESH_SESSION)
    assert send.await_count == 1
    assert not ctx.portal_session_changed.is_set()
