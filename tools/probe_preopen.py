"""What does goszakup answer BEFORE a tender's приём-заявок window opens?

    python -m tools.probe_preopen                     # survey, read-only
    python -m tools.probe_preopen --post <announce>   # ONE ajax_create_application

The survey lists not-yet-open tenders (status 220) and shows what their create
page returns. It never POSTs.

``--post`` answers the question the survey cannot: what the portal says to an
``ajax_create_application`` sent to a tender that is NOT open yet. The bot
currently learns that a tender opened by polling the create FORM and only then
POSTs — about 1-1.5 s of real time after the opening second. If a POST before
the opening is simply refused (an error, a redirect) and leaves nothing behind,
the bot could instead send the create POST at the opening second itself
(docs/PERFORMANCE_AUDIT.md, «Следующие резервы»). That is only safe to build
once this answer is known, hence this probe:

  * exactly one POST, with the company fields of config.yaml;
  * refused unless the create page says the tender is not open yet (on an open
    tender the POST would create a real draft) — ``--allow-open`` overrides;
  * the draft list is read before and after, so a draft the POST left behind
    is reported;
  * every read is sent so that it cannot rotate the cabinet session
    (GoszakupClient.peek) — a running bot keeps its login.

Nothing secret is printed: paste the output as is.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import structlog

from goszakup import secrets as gz_secrets
from goszakup.bot import config as bot_config
from goszakup.bot.search import find_tenders, probe_cookie, STATUS_PUBLISHED
from goszakup.client import GoszakupClient, SessionExpired, reply_shape

FORM_MARKERS = ('name="subject_address"', 'name="contact_phone"',
                "ajax_create_application", "Создание заявки")
_ANN_RE = re.compile(r"/announce/index/(\d+)|(\d{6,12})")
_PORTAL_TZ = timezone(timedelta(hours=5))          # the portal shows Astana time


def _announce_field(page: str, label: str) -> str | None:
    """A read-only field of the announcement card: <label>…</label> … <input value="…">."""
    found = re.search(
        rf"<label[^>]*>\s*{re.escape(label)}\s*</label>\s*<div[^>]*>\s*<input[^>]*?\bvalue=([\"'])(.*?)\1",
        page, re.S)
    return found.group(2).strip() if found else None


def _create_page_state(reply, announce_id: str) -> str:
    """not_open | open | draft_exists | login | access_denied | redirect | page | http<code>"""
    location = (reply.headers.get("location") or "").lower()
    if reply.status_code in (301, 302, 303, 307, 308):
        if "/user/login" in location:
            return "login"
        if "access_denied" in location:
            return "access_denied"
        if location.rstrip("/").endswith("/error_report/not_found"):
            return "not_open"
        if re.search(rf"/application/(?:lots|docs|priceoffers|preview)/{announce_id}/\d+", location):
            return "draft_exists"
        return "redirect"
    if reply.status_code == 200:
        return "open" if any(marker in reply.text for marker in FORM_MARKERS) else "page"
    return f"http{reply.status_code}"


async def probe_create_post(gz: GoszakupClient, announce_id: str, company: dict, *,
                            allow_open: bool = False) -> dict:
    """Send at most ONE ajax_create_application to ``announce_id`` and describe
    the portal's answer. Returns a plain dict (see ``_print_post_result``)."""
    announce_id = str(announce_id)
    result: dict = {"announce_id": announce_id, "sent": False}

    card = await gz.peek(f"/ru/announce/index/{announce_id}")
    if card.status_code == 200:
        result["status_text"] = _announce_field(card.text, "Статус объявления")
        result["starts_at"] = _announce_field(card.text, "Срок начала приема заявок")
        result["ends_at"] = _announce_field(card.text, "Срок окончания приема заявок")
    result["card"] = card.status_code

    form = await gz.peek(f"/ru/application/create/{announce_id}")
    state = _create_page_state(form, announce_id)
    result["create_page"] = state
    result["create_page_location"] = form.headers.get("location")
    if state in ("login", "access_denied", "draft_exists"):
        result["refused"] = state
        return result
    if state != "not_open" and not allow_open:
        result["refused"] = "not_confirmed_closed"
        return result

    result["draft_before"] = await gz._read_application_id(announce_id)

    started = time.monotonic()
    try:
        reply = await gz.post_create_application(
            announce_id,
            subject_address=str(company["subject_address"]).strip(),
            iik=str(company["iik"]).strip(),
            contact_phone=company.get("contact_phone", ""),
            tax_payer_type=company.get("tax_payer_type", "UL"),
            consortium=company.get("consortium", 0),
        )
    except SessionExpired:
        result.update(sent=True, seconds=round(time.monotonic() - started, 3), answer="login")
        return result
    received = datetime.now(timezone.utc)
    result.update(
        sent=True,
        seconds=round(time.monotonic() - started, 3),
        http_status=reply.status_code,
        location=reply.headers.get("location"),
        content_type=reply.headers.get("content-type"),
        length=len(reply.content),
        shape=reply_shape(reply),
        body=re.sub(r"\s+", " ", reply.text[:300]).strip(),
    )
    try:
        server = parsedate_to_datetime(reply.headers.get("date", ""))
        result["server_time"] = server.astimezone(_PORTAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        # The header is cut to whole seconds: the true offset is 0..1 s larger.
        result["clock_offset_s"] = round((server - received).total_seconds(), 2)
    except (TypeError, ValueError):
        pass

    result["draft_after"] = await gz._read_application_id(announce_id)
    return result


def _draft(read) -> str:
    if read is None:
        return "не проверялось"
    clean, app_id = read
    if app_id:
        return f"ЕСТЬ, id {app_id}"
    return "нет" if clean else "не удалось определить"


def _print_post_result(result: dict) -> None:
    print(f"\n=== POST-проба: тендер {result['announce_id']} ===")
    print(f"карточка: http={result.get('card')} статус={result.get('status_text')!r}")
    print(f"приём заявок: с {result.get('starts_at')} по {result.get('ends_at')} (время портала)")
    print(f"страница создания до пробы: {result.get('create_page')} "
          f"location={result.get('create_page_location')}")
    if not result["sent"]:
        reason = {
            "login": "сессия не активна — войдите (python cli.py login) и повторите",
            "access_denied": "портал отказывает этому аккаунту в участии (access_denied)",
            "draft_exists": "по этому тендеру уже есть черновик",
            "not_confirmed_closed": ("портал не подтвердил, что приём ещё не начат; на открытом "
                                     "тендере POST создаст черновик. Нужен тендер до открытия "
                                     "(или --allow-open, если черновик вас устраивает)"),
        }.get(result.get("refused"), str(result.get("refused")))
        print(f"POST НЕ отправлен: {reason}")
        return
    print(f"черновик до пробы: {_draft(result.get('draft_before'))}")
    if result.get("answer") == "login":
        print(f"POST отправлен, ответ за {result['seconds']} с: перенаправление на вход "
              "(сессия не активна)")
        return
    print(f"POST отправлен, ответ за {result['seconds']} с")
    print(f"  http={result['http_status']} location={result['location']} "
          f"content-type={result['content_type']} длина={result['length']}")
    print(f"  форма ответа: {result['shape']}")
    print(f"  тело (до 300 знаков): {result['body']!r}")
    if "clock_offset_s" in result:
        print(f"  время портала в ответе: {result['server_time']}; часы портала минус часы "
              f"этого компьютера: {result['clock_offset_s']:+.2f} с (заголовок без долей секунды: "
              "истинное значение на 0…1 с больше)")
    after = result.get("draft_after")
    print(f"черновик после пробы: {_draft(after)}")
    if after and after[1]:
        print("  ! POST оставил черновик. Он не подан; удалить можно в кабинете "
              "(Мои заявки → черновик → удалить).")
    print("\nПришлите этот вывод целиком — по нему решается, можно ли создавать заявку "
          "POST-ом в секунду открытия.")


def _cached_cookie(cfg) -> str | None:
    # the same order the bot uses (BotContext.ci_session)
    return (gz_secrets.get(cfg.ci_session_service, "cookie_header")
            or gz_secrets.get(cfg.ci_session_service, "ci_session"))


async def post_probe(announce: str, *, allow_open: bool, assume_yes: bool) -> int:
    found = _ANN_RE.search(announce)
    if not found:
        print(f"не удалось разобрать номер тендера из {announce!r}")
        return 2
    announce_id = found.group(1) or found.group(2)
    cfg = bot_config.load(Path("config.yaml"))
    cookie = _cached_cookie(cfg)
    if not cookie:
        print("нет сохранённой сессии — выполните python cli.py login")
        return 2
    print(f"Тендер {announce_id}: будет отправлен ОДИН запрос ajax_create_application "
          "от вашего аккаунта.")
    print("Если портал уже принимает заявки, проба остановится до отправки"
          + (" — но задан --allow-open: тогда POST создаст черновик." if allow_open else "."))
    if not assume_yes and input("Отправить? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        print("отменено, ничего не отправлено")
        return 1
    async with GoszakupClient(ci_session=cookie, base_url=cfg.base_url,
                              proxy=cfg.proxy_url) as gz:
        result = await probe_create_post(gz, announce_id, cfg.raw["company"],
                                         allow_open=allow_open)
    _print_post_result(result)
    return 0 if result["sent"] else 2


async def survey() -> int:
    cfg = bot_config.load(Path("config.yaml"))
    ci = gz_secrets.get(cfg.ci_session_service, "ci_session")
    gz = GoszakupClient(ci_session=ci, base_url=cfg.base_url)
    try:
        print(f"[cookie] alive={await probe_cookie(gz)}")
        rows = await find_tenders(
            gz,
            statuses=tuple(cfg.raw["search"]["statuses"]),
            methods=tuple(cfg.raw["search"]["methods"]),
            limit=40,
        )
        pre = [r for r in rows if r["status_code"] == STATUS_PUBLISHED]
        openn = [r for r in rows if r["status_code"] != STATUS_PUBLISHED]
        print(f"[search] {len(rows)} rows: {len(pre)} not-yet-open(220), {len(openn)} open(210)")

        # Probe up to 3 not-yet-open tenders + 1 open one for contrast.
        sample = pre[:3] + openn[:1]
        if not sample:
            print("No tenders matched the configured filters right now.")
            return 0
        for r in sample:
            ann = r["announce_id"]
            path = f"/ru/application/create/{ann}"
            resp = await gz._http.get(path, follow_redirects=True)
            final = str(resp.url)
            present = [m for m in FORM_MARKERS if m in resp.text]
            print(f"\n--- {ann}  [{r['status_text']}]  end={r['end_date']}")
            print(f"    name={r['name'][:60]!r}")
            print(f"    create -> status={resp.status_code} final={final}")
            print(f"    form_markers={present} "
                  f"error_report={'error_report' in final} access_denied={'access_denied' in final}")
        if pre:
            print("\nЧтобы узнать ответ портала на POST до открытия: "
                  f"python -m tools.probe_preopen --post {pre[0]['announce_id']}")
    finally:
        await gz.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.probe_preopen",
        description="Что портал отвечает до начала приёма заявок.")
    parser.add_argument("--post", metavar="ТЕНДЕР",
                        help="отправить ОДИН ajax_create_application на тендер до открытия")
    parser.add_argument("--allow-open", action="store_true",
                        help="отправить POST, даже если тендер уже открыт (будет создан черновик)")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    args = parser.parse_args(argv)
    if args.post:
        # only the probe's own lines: the output is meant to be pasted as is
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
        return asyncio.run(post_probe(args.post, allow_open=args.allow_open, assume_yes=args.yes))
    return asyncio.run(survey())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
