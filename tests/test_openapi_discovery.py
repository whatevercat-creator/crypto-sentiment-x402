"""
How the API describes itself to x402 directories (x402scan's
@agentcash/discovery audit): the paid route carries x-payment-info, API-key
routes reference the X-API-Key scheme, free routes say "security": [], and
/favicon.ico is served.
"""

import pytest

from tests.test_home import client  # noqa: F401  (shared fixture)
from app import main

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
    assert schemes["RapidAPIProxySecret"]["name"] == "X-RapidAPI-Proxy-Secret"
    assert spec["paths"]["/rapidapi/sentiment/{symbol}"]["get"]["security"] == [
        {"RapidAPIProxySecret": []}
    ]


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
