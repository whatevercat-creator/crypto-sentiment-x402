"""
One "sentiment_paid_call" JSON log line per successful paid /sentiment call,
with the payer read from PAYMENT-SIGNATURE and the tx hash from the
settlement, and never the signature itself.
"""

import base64
import json
import logging

import pytest
from x402.schemas import SettleResponse, VerifyResponse

from tests.test_home import client  # noqa: F401  (shared fixture)
from app import main

PAYER = "0x1111111111111111111111111111111111111111"
TX = "0x" + "ab" * 32
SIGNATURE = "0x" + "cd" * 65


@pytest.fixture
def paid(client, monkeypatch):  # noqa: F811
    async def fake_payload(symbol):
        return {"symbol": symbol.upper(), "label": "neutral", "score": 0.0}

    settled = {"success": True}

    async def verify(payload, requirements):
        return VerifyResponse(is_valid=True, payer=PAYER)

    async def settle(payload, requirements):
        if not settled["success"]:
            return SettleResponse(
                success=False, error_reason="insufficient_funds", transaction="",
                network=main.CAIP2_NETWORK, payer=PAYER,
            )
        return SettleResponse(success=True, transaction=TX, network=main.CAIP2_NETWORK, payer=PAYER)

    monkeypatch.setattr(main, "compute_sentiment_payload", fake_payload)
    monkeypatch.setattr(main.facilitator, "verify", verify)
    monkeypatch.setattr(main.facilitator, "settle", settle)
    return client, settled


def _payment_header(client, symbol):  # noqa: F811
    challenge = json.loads(
        base64.b64decode(client.get(f"/sentiment/{symbol}").headers["payment-required"])
    )
    accepted = challenge["accepts"][0]
    payload = {
        "x402Version": 2,
        "resource": challenge["resource"],
        "accepted": accepted,
        "payload": {
            "signature": SIGNATURE,
            "authorization": {
                "from": PAYER,
                "to": accepted["payTo"],
                "value": accepted["amount"],
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "00" * 32,
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _paid_call_lines(caplog):
    lines = []
    for record in caplog.records:
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "sentiment_paid_call":
            lines.append(data)
    return lines


def test_successful_paid_call_logs_one_line(paid, caplog):
    client, _ = paid
    header = _payment_header(client, "ETH")
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = client.get("/sentiment/ETH", headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 200
    [line] = _paid_call_lines(caplog)
    assert set(line) == {"event", "ts", "symbol", "price", "payer", "transaction"}
    assert line["symbol"] == "ETH"
    assert line["price"] == main.PRICE_USD
    assert line["payer"] == PAYER
    assert line["transaction"] == TX
    # Neither the signature nor the raw header ever reaches the logs.
    assert SIGNATURE not in caplog.text
    assert header not in caplog.text


def test_unpaid_and_failed_settlement_are_not_logged(paid, caplog):
    client, settled = paid
    header = _payment_header(client, "BTC")
    settled["success"] = False
    with caplog.at_level(logging.INFO, logger="app.main"):
        assert client.get("/sentiment/BTC").status_code == 402
        assert client.get("/sentiment/BTC", headers={"PAYMENT-SIGNATURE": header}).status_code == 402
    assert _paid_call_lines(caplog) == []


@pytest.mark.parametrize("header", [None, "", "not base64!", base64.b64encode(b"[1]").decode()])
def test_unreadable_signature_gives_null_payer(header):
    assert main._payer_from_signature(header) is None
    assert main._settlement_tx(header) is None
