"""Input parsing for the Telegram bot entry points.

These guard the mobile shortcuts: a user pastes a number, a "<id>-1", or a full
goszakup URL, and the power-path `/bid <id> <lots> [price]`.
"""

from __future__ import annotations

import pytest

from goszakup.bot.atagos_bot import (
    _extract_announce_id,
    _fmt_lots,
    _lot_sum_disp,
    _parse_bid_args,
)


@pytest.mark.parametrize("raw,expected", [
    ("17046469", "17046469"),
    ("  17046469  ", "17046469"),
    ("17046469-1", "17046469"),
    ("https://v3bl.goszakup.gov.kz/ru/announce/index/17046469", "17046469"),
    ("v3bl.goszakup.gov.kz/kz/announce/index/17046469?tab=lots", "17046469"),
    ("не число", None),
    ("", None),
    ("123", None),            # too short to be an announce id
])
def test_extract_announce_id(raw, expected):
    assert _extract_announce_id(raw) == expected


def test_parse_bid_args_lots_and_price():
    assert _parse_bid_args(["17046469", "41931596,41931597", "6030145.6"]) == (
        "17046469", [41931596, 41931597], 6030145.6)


def test_parse_bid_args_no_price():
    ann, lots, price = _parse_bid_args(["17046469", "41931596"])
    assert (ann, lots, price) == ("17046469", [41931596], None)


def test_parse_bid_args_accepts_url_first_arg():
    ann, lots, _ = _parse_bid_args(
        ["https://v3bl.goszakup.gov.kz/ru/announce/index/17046469", "41931596"])
    assert ann == "17046469" and lots == [41931596]


def test_parse_bid_args_requires_lots():
    with pytest.raises(ValueError):
        _parse_bid_args(["17046469"])


def test_fmt_lots():
    assert _fmt_lots([41931596, 41931597]) == "41931596, 41931597"


def test_lot_sum_disp_millions():
    assert _lot_sum_disp("7 537 682") == "7.54M ₸"
    assert _lot_sum_disp("плохое") == "плохое"
