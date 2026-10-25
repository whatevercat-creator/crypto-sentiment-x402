"""The paid tools refuse to sign when the 402 asks for more than the tool's
price, or for a different payTo/asset. Runs the real x402 client and signer
(a throwaway local key in place of the CDP wallet); no network: the SDK's
transport goes through httpx.MockTransport."""

import asyncio
import base64
import json

import httpx
import pytest
from eth_account import Account

import crypto_sentiment_x402_mcp as mcp_pkg

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _challenge(amount, pay_to=mcp_pkg.PAY_TO_ADDRESS, asset=USDC, network="eip155:8453"):
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": "https://example.test/paid"},
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "asset": asset,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
    }


def _signed(request):
    return any(
        name.lower() in ("payment-signature", "x-payment") for name in request.headers
    )


@pytest.fixture
def paid_api(monkeypatch):
    """Real _get_payment_client with a local key; the API answers each unpaid
    request with state["challenge"] and a signed retry with 200."""
    monkeypatch.setenv("CDP_API_KEY_ID", "test-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-secret")

    class FakeCdp:
        def __init__(self):
            self.evm = self

        async def __aenter__(self):
            return self

        async def get_or_create_account(self, name):
            return Account.create()

    monkeypatch.setattr(mcp_pkg, "CdpClient", FakeCdp)
    monkeypatch.setattr(mcp_pkg, "EvmLocalAccount", lambda account: account)
    monkeypatch.setattr(mcp_pkg, "_payment_client", None)

    calls = []
    state = {"challenge": None}

    def handler(request):
        calls.append(request)
        if _signed(request):
            return httpx.Response(200, json={"symbol": "BTC", "paid": True})
        header = base64.b64encode(json.dumps(state["challenge"]).encode()).decode()
        return httpx.Response(
            402, json=state["challenge"], headers={"PAYMENT-REQUIRED": header}
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(handler))
    return state, calls


def test_matching_402_is_signed_and_paid(paid_api):
    # Control: proves this setup really signs, so the refusals below mean something.
    state, calls = paid_api
    state["challenge"] = _challenge("10000")

    out = asyncio.run(mcp_pkg.crypto_sentiment("BTC"))

    assert out == {"symbol": "BTC", "paid": True}
    assert [_signed(r) for r in calls] == [False, True]


def test_over_price_is_refused_before_signing(paid_api):
    state, calls = paid_api
    state["challenge"] = _challenge("20000")  # $0.02 for a $0.01 tool

    out = asyncio.run(mcp_pkg.crypto_sentiment("BTC"))

    assert out["error"] == "refused: price/payTo mismatch, nothing was paid"
    assert out["charged"] is False
    assert "above the expected 10000" in out["detail"][0]
    assert out["expected"]["amount_base_units"] == "10000"
    assert len(calls) == 1 and not _signed(calls[0])


def test_history_over_price_is_refused(paid_api):
    state, calls = paid_api
    state["challenge"] = _challenge("50001")

    out = asyncio.run(mcp_pkg.get_hourly_history("BTC"))

    assert out["error"] == "refused: price/payTo mismatch, nothing was paid"
    assert out["expected"]["price_usd"] == 0.05
    assert len(calls) == 1 and not _signed(calls[0])


def test_wrong_pay_to_is_refused(paid_api):
    state, calls = paid_api
    state["challenge"] = _challenge(  # price itself is fine
        "50000", pay_to="0x2222222222222222222222222222222222222222"
    )

    out = asyncio.run(mcp_pkg.get_hourly_history("BTC"))

    assert out["error"] == "refused: price/payTo mismatch, nothing was paid"
    assert out["detail"] == [
        "payTo '0x2222222222222222222222222222222222222222' is not the "
        f"expected {mcp_pkg.PAY_TO_ADDRESS}"
    ]
    assert len(calls) == 1 and not _signed(calls[0])


@pytest.mark.parametrize(
    "overrides",
    [
        {"asset": "0x3333333333333333333333333333333333333333"},
        {"network": "eip155:84532"},
    ],
)
def test_wrong_asset_or_network_is_refused(paid_api, overrides):
    # The SDK's own filters reject these before the hook; same answer.
    state, calls = paid_api
    state["challenge"] = _challenge("10000", **overrides)

    out = asyncio.run(mcp_pkg.crypto_sentiment("BTC"))

    assert out["error"] == "refused: price/payTo mismatch, nothing was paid"
    assert out["charged"] is False
    assert len(calls) == 1 and not _signed(calls[0])


def test_hook_fails_closed_without_expected_price():
    class Ctx:
        selected_requirements = None

    result = mcp_pkg._check_requirements(Ctx())
    assert result is not None and "no expected price" in result.reason
