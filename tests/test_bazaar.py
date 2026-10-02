"""
The /sentiment Bazaar discovery extension must validate as declared (the
x402 middleware checks it at startup), and the 402 must still carry the
real request's method and symbol.
"""

import base64
import json

from x402.extensions.bazaar import (
    validate_discovery_extension,
    validate_discovery_extension_spec,
)

from tests.test_home import client  # noqa: F401  (shared fixture)
from app import main


def _assert_valid(ext):
    for check in (validate_discovery_extension_spec, validate_discovery_extension):
        result = check(ext)
        assert result.valid, result.errors


def test_declared_extension_validates():
    ext = main.routes["GET /sentiment/:symbol"].extensions["bazaar"]
    _assert_valid(ext)
    assert ext["info"]["input"]["method"] == "GET"
    assert "symbol" in ext["info"]["input"]["pathParams"]


def test_402_extension_carries_request_symbol(client):  # noqa: F811
    r = client.get("/sentiment/ETH")
    assert r.status_code == 402
    ext = json.loads(base64.b64decode(r.headers["payment-required"]))["extensions"]["bazaar"]
    _assert_valid(ext)
    assert ext["info"]["input"]["method"] == "GET"
    assert ext["info"]["input"]["pathParams"] == {"symbol": "ETH"}
    assert ext["routeTemplate"] == "/sentiment/:symbol"


def test_402_mirrors_challenge_without_www_authenticate(client):  # noqa: F811
    r = client.get("/sentiment/BTC")
    assert r.status_code == 402
    decoded = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert r.json() == decoded
    assert r.json()["accepts"], "body must carry accepts[]"
    # A bare "WWW-Authenticate: Payment" is an incomplete MPP challenge and
    # fails x402scan's audit, so 402s don't send one.
    assert "www-authenticate" not in r.headers
