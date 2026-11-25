"""Open-tender search: library version of scripts/find_open_tenders.py.

Reuses GoszakupClient so the bot doesn't shell out. Sends the same
filter[] query params goszakup's web UI uses, so server-side narrowing
returns exactly the matching tenders (no client-side keyword guessing).

Status codes (goszakup taxonomy):
    210 — Опубликовано (приём заявок)   — ready to bid right now
    220 — Опубликовано                  — published but not yet accepting

Method codes (procurement method), as filter[method][]:
    2 — observed in this account's filled filter URL (filter[method][]=2)
    (temka's account used 120; codes are per procurement method, not universal)

When a tender is status 220, submit_bid must poll until the form opens
(see pre_t0_poll in pipeline.py).
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal, Optional

import httpx

from lxml import html as lxml_html

from ..client import GoszakupClient, SessionExpired, portal_auth_redirect

STATUS_OPEN_FOR_BIDS = 210      # Опубликовано (приём заявок)
STATUS_PUBLISHED = 220           # Опубликовано (приём ещё не открыт)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_sum(s: str) -> Optional[float]:
    try:
        return float(s.replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _infer_status(text: str) -> int:
    """Map status text to goszakup numeric code."""
    t = text.lower()
    if "приём" in t or "прием" in t:
        return STATUS_OPEN_FOR_BIDS
    return STATUS_PUBLISHED


async def find_tenders(
    gz: GoszakupClient,
    *,
    statuses: tuple[int, ...] = (210, 220),
    methods: tuple[int, ...] = (),
    max_sum: Optional[int] = None,
    name_query: Optional[str] = None,
    keywords: tuple[str, ...] = (),
    trade_type: str = "r",
    limit: int = 50,
) -> list[dict]:
    """Hit /ru/search/announce with server-side filters; return parsed rows.

    Returned dict shape:
        {announce_id, name, end_date, sum_str, sum_val, status_code, status_text}

    ``keywords`` is kept as a soft client-side narrower for users who want to
    further filter results after the server-side method[]/status[] filter.
    """
    # Param names mirror goszakup's filled search URL exactly:
    # filter[status][]=210&filter[status][]=220&filter[method][]=2
    # &filter[name]=...&filter[trade_type]=r&filter[year]=0
    params: list[tuple[str, str]] = [("filter[year]", "0")]
    for s in statuses:
        params.append(("filter[status][]", str(s)))
    for m in methods:
        params.append(("filter[method][]", str(m)))
    if name_query:
        params.append(("filter[name]", name_query))
    if trade_type:
        params.append(("filter[trade_type]", trade_type))

    r = await gz._http.get("/ru/search/announce", params=params, follow_redirects=True)
    if "/user/login" in str(r.url):
        raise SessionExpired(f"search redirected to {r.url}")
    r.raise_for_status()
    tree = lxml_html.fromstring(r.text)
    rows = tree.xpath("//table[contains(@class,'table')]//tbody/tr")
    lower_kw = tuple(k.lower() for k in keywords)

    out: list[dict] = []
    for row in rows:
        cells = row.xpath(".//td")
        if len(cells) < 6:
            continue
        href = row.xpath(".//a[contains(@href,'/announce/index/')]/@href")
        if not href:
            continue
        m = re.search(r"/announce/index/(\d+)", href[0])
        if not m:
            continue
        announce_id = m.group(1)
        name = _clean(cells[1].text_content())
        end_dt = _clean(cells[4].text_content())
        sum_str = _clean(cells[5].text_content())
        sum_val = _parse_sum(sum_str)

        # Status column varies between goszakup table layouts; scan all cells
        # for known status text instead of hardcoding an index.
        full_row_text = _clean(row.text_content()).lower()
        if "приём заявок" in full_row_text or "прием заявок" in full_row_text:
            status_code, status_text = STATUS_OPEN_FOR_BIDS, "Приём заявок"
        else:
            status_code, status_text = STATUS_PUBLISHED, "Ждём открытия"

        if max_sum is not None and (sum_val is None or sum_val > max_sum):
            continue
        if lower_kw and not any(k in name.lower() for k in lower_kw):
            continue

        out.append({
            "announce_id": announce_id,
            "name": name,
            "end_date": end_dt,
            "sum_str": sum_str,
            "sum_val": sum_val,
            "status_code": status_code,
            "status_text": status_text,
        })
        if len(out) >= limit:
            break
    return out


_PROFILE_PATH = re.compile(r"/(?:(?:ru|kz|en)/)?cabinet/profile/?")
_LOGIN_USERNAME = re.compile(rb"<input\b[^>]*\bname\s*=\s*[\"']username[\"']", re.I)


async def probe_portal_session(
    gz: GoszakupClient, *, timeout_s: float = 5.0,
) -> Literal["active", "expired", "sso", "unavailable"]:
    """Check cabinet access without following the SSO redirect or replacing cookies."""
    base = httpx.URL(gz.base_url)
    path = "/cabinet/profile"
    try:
        async with asyncio.timeout(timeout_s):
            for _ in range(3):
                response = await gz.peek(path)
                auth_redirect = portal_auth_redirect(response, base_url=gz.base_url)
                if auth_redirect == "sso":
                    return "sso"
                if auth_redirect == "legacy" or response.status_code == 401:
                    return "expired"
                if response.status_code == 200:
                    if b"/user/login" in response.content.lower() and _LOGIN_USERNAME.search(response.content):
                        return "expired"
                    return "active"
                if not response.has_redirect_location:
                    break
                target = response.url.join(response.headers["location"])
                if (target.userinfo
                        or (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port)
                        or not _PROFILE_PATH.fullmatch(target.path)):
                    break
                path = target.raw_path.decode("ascii")
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL):
        pass
    return "unavailable"


async def probe_cookie(gz: GoszakupClient) -> bool:
    return await probe_portal_session(gz) == "active"
