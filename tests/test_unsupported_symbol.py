"""
A symbol this API doesn't score (a stock ticker, say) is turned away with a
404 before anything is charged: no x402 settlement, no subscription quota,
and one "unsupported_symbol" log line. Supported coins are unaffected.
"""

import json
import logging

import pytest
from fastapi import HTTPException

from tests.test_home import client  # noqa: F401  (shared fixture)
from tests.test_paid_call_log import _paid_call_lines, _payment_header, paid  # noqa: F401
from app import main, sentiment_service
from app.coins import COIN_NAMES, UnsupportedSymbol, supported_symbol


def _unsupported_lines(caplog):
    lines = []
    for record in caplog.records:
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "unsupported_symbol":
            lines.append(data)
    return lines


@pytest.mark.parametrize("symbol", ["DOGE", "xrp", " btc "])
def test_supported_coins_pass(symbol):
    assert supported_symbol(symbol) == symbol.strip().upper()


@pytest.mark.parametrize("symbol", ["AAPL", "TSLA", "COIN", "ZZZ"])
def test_unknown_tickers_are_unsupported(symbol):
    with pytest.raises(UnsupportedSymbol) as e:
        supported_symbol(symbol)
    assert "Not charged" in str(e.value) and "BTC" in str(e.value)


def test_malformed_symbol_is_400_not_404():
    with pytest.raises(HTTPException) as e:
        sentiment_service.require_supported_symbol("{symbol}", "x402")
    assert e.value.status_code == 400


def test_paid_call_for_stock_is_404_and_never_settled(paid, caplog, monkeypatch):  # noqa: F811
    client, _ = paid
    settles = []
    real_settle = main.facilitator.settle

    async def counting_settle(payload, requirements):
        settles.append(1)
        return await real_settle(payload, requirements)

    monkeypatch.setattr(main.facilitator, "settle", counting_settle)
    header = _payment_header(client, "AAPL")
    with caplog.at_level(logging.INFO):
        r = client.get("/sentiment/AAPL", headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 404
    assert "AAPL is not a supported symbol" in r.json()["detail"]
    assert settles == []
    assert _paid_call_lines(caplog) == []
    [line] = _unsupported_lines(caplog)
    assert line["symbol"] == "AAPL" and line["lane"] == "x402"

    # A supported coin still settles and logs as before.
    header = _payment_header(client, "DOGE")
    r = client.get("/sentiment/DOGE", headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 200 and settles == [1]


def test_unpaid_request_still_gets_the_402_challenge(client):  # noqa: F811
    # Directories probe with placeholder and arbitrary paths and expect a 402.
    assert client.get("/sentiment/AAPL").status_code == 402
    assert client.get("/sentiment/%7Bsymbol%7D").status_code == 402
    assert client.get("/sentiment/%3Asymbol").status_code == 402
    assert client.get("/sentiment/BTC").status_code == 402


def test_subscription_call_for_stock_is_not_counted(client, monkeypatch):  # noqa: F811
    charged = []
    monkeypatch.setattr(main, "verify_and_charge_api_key", lambda key: charged.append(key))
    r = client.get("/v1/sentiment/AAPL", headers={"X-API-Key": "k"})
    assert r.status_code == 404 and charged == []


def test_llms_txt_lists_supported_symbols(client):  # noqa: F811
    text = client.get("/llms.txt").text
    assert ", ".join(COIN_NAMES) in text
