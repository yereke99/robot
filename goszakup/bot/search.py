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

import re
from typing import Optional

from lxml import html as lxml_html

from ..client import GoszakupClient, SessionExpired

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


async def probe_cookie(gz: GoszakupClient) -> bool:
    """Return True iff cookie is alive (cabinet page actually accessible).

    Eng review M3: a naive `status==200` check can lie — goszakup
    occasionally returns 200 for an unauth user with a login form rendered
    inline. We follow redirects and verify (a) we didn't land at /login
    and (b) the body doesn't carry login-form markers.
    """
    try:
        r = await gz._http.get("/cabinet/profile", follow_redirects=True)
    except Exception:
        return False
    final = str(r.url).lower()
    if "/user/login" in final or "/login" in final:
        return False
    if r.status_code != 200:
        return False
    body = r.text[:8192].lower()
    if "/user/login" in body and 'name="username"' in body:
        return False
    return True
