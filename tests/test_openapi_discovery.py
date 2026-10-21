"""
How the API describes itself to x402 directories (x402scan's
@agentcash/discovery audit): the paid route carries x-payment-info, API-key
routes reference the X-API-Key scheme, free routes say "security": [], and
/favicon.ico is served.
"""

import pytest

from tests.test_home import client  # noqa: F401  (shared fixture)
from app import main
from app.coins import COIN_NAMES

API_KEY_ROUTES = [
    ("/v1/sentiment/{symbol}", "get"),
    ("/alerts/watch", "get"),
    ("/alerts/watch", "post"),
    ("/alerts/watch/{watch_id}", "delete"),
    ("/alerts/history", "get"),
    ("/dataset/export", "get"),
]


@pytest.fixture
def spec(client):  # noqa: F811
    return client.get("/openapi.json").json()


def test_paid_route_declares_x402_price(spec):
    op = spec["paths"]["/sentiment/{symbol}"]["get"]
    assert op["x-payment-info"] == {
        "price": {"mode": "fixed", "currency": "USD", "amount": main.PRICE_USD.lstrip("$")},
        "protocols": [{"x402": {}}],
    }
    assert "402" in op["responses"]
    assert "security" not in op


@pytest.mark.parametrize("path,method", API_KEY_ROUTES)
def test_api_key_routes_reference_the_scheme(spec, path, method):
    assert spec["paths"][path][method]["security"] == [{"ApiKeyAuth": []}]


def test_security_schemes_defined(spec):
    schemes = spec["components"]["securitySchemes"]
    assert schemes["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": schemes["ApiKeyAuth"]["description"],
    }
    assert "RapidAPIProxySecret" not in schemes


def test_every_operation_declares_an_auth_mode(spec):
    # The audit flags L2_AUTH_MODE_MISSING for any operation with neither.
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            assert "x-payment-info" in op or "security" in op, f"{method.upper()} {path}"
    free = [
        f"{m.upper()} {p}"
        for p, ops in spec["paths"].items()
        for m, op in ops.items()
        if op.get("security") == []
    ]
    assert "GET /billing/pricing" in free and "GET /health" in free
    assert not any("/alerts" in f or "/v1/" in f or "/dataset/export" in f for f in free)


def test_guidance_explains_the_paid_route(spec):
    guidance = spec["info"]["x-guidance"]
    assert "GET /sentiment/{symbol}" in guidance
    assert main.PRICE_USD in guidance


def test_favicon_served_free(client):  # noqa: F811
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO header
    assert "payment-required" not in {k.lower() for k in r.headers}
    assert "/favicon.ico" not in client.get("/openapi.json").json()["paths"]


HIDDEN_ROUTES = [
    ("/billing/webhook", "post"),
    ("/billing/success", "get"),
    ("/billing/cancel", "get"),
    ("/integrations/tradingview/{api_key}", "post"),
    ("/rapidapi/sentiment/{symbol}", "get"),
]


@pytest.mark.parametrize("path,method", HIDDEN_ROUTES)
def test_internal_routes_hidden_from_spec(spec, path, method):
    assert method not in spec["paths"].get(path, {})


def test_info_description_present(spec):
    description = spec["info"]["description"]
    assert f"{len(COIN_NAMES)} coins" in description
    assert main.PRICE_USD in description and "not charged" in description
    for phrase in ("always", "you must", "call this first"):
        assert phrase not in description.lower()


def test_sentiment_symbol_enum_lists_supported_coins(spec):
    (symbol,) = spec["paths"]["/sentiment/{symbol}"]["get"]["parameters"]
    assert symbol["schema"]["enum"] == list(COIN_NAMES)


def test_history_range_params_are_plain_strings(spec):
    # No format: date-time -- the route also takes timestamps without an offset.
    params = {p["name"]: p for p in spec["paths"]["/history/{symbol}"]["get"]["parameters"]}
    for name in ("start", "end"):
        assert params[name]["schema"]["type"] == "string"
        assert "format" not in params[name]["schema"]
        assert params[name]["required"] is False


def test_enum_is_spec_only(client):  # noqa: F811
    # An unsupported symbol still reaches the paywall like before, not a 422
    # from request validation.
    assert client.get("/sentiment/AAPL").status_code == 402
