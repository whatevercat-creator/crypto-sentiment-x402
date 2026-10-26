"""
A malformed Host header must not get /sentiment/BTC past the x402 paywall.

GHSA-86qp-5c8j-p5mr: older Starlette built request.url from the Host header,
so a Host carrying "/", "?" or "#" moved the real path out of
request.url.path, which is what the x402 middleware matches routes on.
"""

import pytest

from tests.test_home import client  # noqa: F401  (shared fixture)

MALFORMED_HOSTS = [
    "evil.example/",
    "evil.example/free?",
    "evil.example?",
    "evil.example#",
    "evil.example/docs#",
    "evil.example:99999",
    "[::1",
    "a b",
    "evil.example@other.example",
]


@pytest.mark.parametrize("host", MALFORMED_HOSTS)
def test_malformed_host_still_gets_402(client, host):  # noqa: F811
    r = client.get("/sentiment/BTC", headers={"Host": host})
    assert r.status_code == 402, (host, r.status_code, r.text[:200])
    assert r.headers.get("payment-required")
    assert r.json()["accepts"], "402 body must carry accepts[]"
