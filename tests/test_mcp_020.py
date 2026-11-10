"""
0.2.0: verb_noun tool names (clean renames, no aliases), every tool name a
description mentions exists, titles longer than names, two new tools
(get_market_sentiment free, get_movers paid behind the same 402 guard), the
session spend cap (MAX_SPEND_USD_PER_SESSION), server.json title/websiteUrl,
the home page's tool list, and version 0.2.0 everywhere. No network.
"""

import asyncio
import json
import pathlib
import re
import tomllib

import httpx
import pytest

import crypto_sentiment_x402_mcp as mcp_pkg
from app import market
from tests.test_mcp_free_tools import mock_http, mock_paid_http  # noqa: F401 (fixtures)
from tests.test_mcp_payment_guard import _challenge, _signed, paid_api  # noqa: F401
from tests.test_mcp_tool_metadata import FREE, PAID, _tools, _wire

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text())
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
VERSION = "0.2.0"

OLD_NAMES = ("crypto_sentiment", "get_hourly_history", "archive_coverage", "price_quote")
TOOL_NAMES = {
    "get_sentiment", "get_sentiment_history", "get_movers",
    "list_supported_coins", "get_archive_coverage", "get_call_price",
    "get_market_sentiment",
}
VERBS = ("get", "list")


def _texts():
    """Every description an agent sees: tool descriptions, titles and each
    input property's schema description."""
    for t in _tools().values():
        yield t.name, t.description
        yield t.name, t.title
        for prop in t.input_schema.get("properties", {}).values():
            yield t.name, prop.get("description", "")


@pytest.fixture(autouse=True)
def fresh_session(monkeypatch):
    """Each test starts a new 'session': nothing spent, default cap."""
    monkeypatch.setattr(mcp_pkg, "_spent_base_units", 0)
    monkeypatch.delenv("MAX_SPEND_USD_PER_SESSION", raising=False)


# --- names ---

def test_exactly_the_seven_tools():
    assert set(_tools()) == TOOL_NAMES
    assert set(FREE) | set(PAID) == TOOL_NAMES


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_names_are_verb_noun(name):
    assert re.fullmatch(r"[a-z]+(_[a-z]+)+", name), name
    assert name.split("_")[0] in VERBS, name


def test_old_names_are_gone_no_aliases():
    for old in OLD_NAMES:
        assert old not in _tools()
        assert not hasattr(mcp_pkg, old)


def test_every_tool_name_mentioned_in_a_description_exists():
    # A tool-like token: verb_noun with one of our verbs, or an old name.
    pattern = re.compile(r"\b(?:(?:get|list)_[a-z_]*[a-z]|" + "|".join(OLD_NAMES) + r")\b")
    for owner, text in _texts():
        for token in pattern.findall(text or ""):
            assert token in TOOL_NAMES, f"{owner} mentions {token!r}"


def test_no_description_names_an_old_tool():
    for owner, text in _texts():
        for old in OLD_NAMES:
            assert not re.search(rf"(?<!get_)\b{old}\b", text or ""), (owner, old)


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_title_is_longer_than_name(name):
    title = _tools()[name].title
    assert title and title != name and len(title) > len(name)
    assert title.endswith("(paid)" if name in mcp_pkg.TOOL_PRICES_USD else "(free)")


# --- paid tools' descriptions say how their inputs are used ---

@pytest.mark.parametrize(
    "name, sentence",
    [
        ("get_sentiment", "symbol picks the coin and goes into the request path"),
        ("get_sentiment_history", "symbol goes into the request path (/history/{symbol}) and start/end are sent as query parameters"),
        ("get_movers", "limit is sent as the ?limit= query parameter"),
    ],
)
def test_paid_descriptions_explain_inputs(name, sentence):
    assert sentence in " ".join(_tools()[name].description.split())


def test_get_sentiment_freshness_sentence():
    d = " ".join(_tools()["get_sentiment"].description.split())
    assert "Each call fetches and scores the feeds live (no cache), so repeat calls can differ as news arrives." in d


def test_existing_schema_descriptions_unchanged_except_tool_name():
    s = _tools()["get_sentiment"].input_schema["properties"]["symbol"]["description"]
    assert s == ("Uppercase crypto ticker, one of the 24 from list_supported_coins "
                 "(e.g. BTC). Stock tickers and other symbols are refused and not charged.")
    h = _tools()["get_sentiment_history"].input_schema["properties"]
    assert h["symbol"]["description"].endswith("get_archive_coverage lists the logged symbols.")
    assert h["start"]["description"].startswith("ISO 8601 timestamp, e.g. 2026-10-13T00:00:00Z")


# --- get_market_sentiment (free) ---

def test_market_sentiment_metadata():
    t = _tools()["get_market_sentiment"]
    paras = [" ".join(p.split()) for p in t.description.strip().split("\n\n")]
    assert paras[0].startswith("Crypto market sentiment index")
    assert "Free" in paras[0]
    assert paras[1].startswith("Use when ")
    assert "Refreshed at most hourly" in paras[1]
    assert t.input_schema.get("properties", {}) == {}
    assert "get_market_sentiment" in mcp_pkg.FREE_TOOLS


def test_market_sentiment_calls_free_endpoint_without_wallet(mock_http, monkeypatch):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.setattr(mcp_pkg, "_get_payment_client", lambda: (_ for _ in ()).throw(AssertionError("no wallet")))
    state, calls = mock_http
    body = {**market.MARKET_SENTIMENT_EXAMPLE}
    state["handler"] = lambda r: httpx.Response(200, json=body)

    out = asyncio.run(mcp_pkg.get_market_sentiment())

    assert out == body
    for key in ("cached", "computed_at", "next_refresh_at"):
        assert key in out
    assert [str(r.url) for r in calls] == [f"{mcp_pkg.API_URL}/market-sentiment"]
    assert calls[0].method == "GET"
    assert not any(n.lower() in ("payment-signature", "x-payment") for n in calls[0].headers)


def test_market_sentiment_error_passthrough(mock_http):
    state, _ = mock_http
    state["handler"] = lambda r: httpx.Response(503, text="down")
    out = asyncio.run(mcp_pkg.get_market_sentiment())
    assert out == {"error": "API returned HTTP 503", "detail": "down"}


# --- get_movers (paid) ---

def test_movers_metadata_and_price():
    t = _tools()["get_movers"]
    assert mcp_pkg.TOOL_PRICES_USD["get_movers"] == 0.02
    assert market.MOVERS_PRICE_USD == "$0.02"
    assert _wire(t)["annotations"] == {
        "readOnlyHint": False, "idempotentHint": False,
        "destructiveHint": False, "openWorldHint": True,
    }
    d = " ".join(t.description.split())
    assert d.startswith("Crypto sentiment movers")
    assert "Use when " in d
    assert "Coins without enough recent news" in d and "left out" in d
    assert "422" in d and "charged: false" in d


def test_movers_limit_schema_matches_api_bounds():
    prop = _tools()["get_movers"].input_schema["properties"]["limit"]
    assert _tools()["get_movers"].input_schema.get("required", []) == []
    flat = json.dumps(prop)
    assert f'"minimum": 1' in flat and f'"maximum": {market.MOVERS_MAX_LIMIT}' in flat
    assert str(market.MOVERS_DEFAULT_LIMIT) in prop["description"]


def test_movers_without_cdp_vars(monkeypatch):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    monkeypatch.setattr(mcp_pkg, "_get_payment_client", lambda: (_ for _ in ()).throw(AssertionError("no wallet")))
    out = asyncio.run(mcp_pkg.get_movers())
    assert out["error"] == "no wallet configured" and out["charged"] is False
    assert "get_market_sentiment" in out["free_tools"]


def test_movers_url_and_limit(mock_paid_http):
    mock_paid_http.state["response"] = lambda r: httpx.Response(200, json={"movers_up": [], "movers_down": []})
    asyncio.run(mcp_pkg.get_movers())
    asyncio.run(mcp_pkg.get_movers(limit=5))
    assert str(mock_paid_http[0].url) == f"{mcp_pkg.API_URL}/movers"
    assert mock_paid_http[0].url.query == b""
    assert dict(mock_paid_http[1].url.params) == {"limit": "5"}


def test_movers_422_is_not_charged(mock_paid_http):
    detail = {"detail": "No coin had enough recent news both now and ~24h ago to rank a sentiment shift"}
    mock_paid_http.state["response"] = lambda r: httpx.Response(422, json=detail)
    out = asyncio.run(mcp_pkg.get_movers())
    assert out["error"] == "API returned HTTP 422"
    assert out["detail"] == detail
    assert out["charged"] is False


def test_movers_matching_402_is_paid(paid_api):
    state, calls = paid_api
    state["challenge"] = _challenge("20000")
    out = asyncio.run(mcp_pkg.get_movers())
    assert out == {"symbol": "BTC", "paid": True}  # the fake API's 200 body
    assert [_signed(r) for r in calls] == [False, True]


@pytest.mark.parametrize(
    "challenge_kwargs, detail_part",
    [
        ({"amount": "20001"}, "above the expected 20000"),
        ({"amount": "20000", "pay_to": "0x2222222222222222222222222222222222222222"}, "payTo"),
    ],
)
def test_movers_refuses_before_signing(paid_api, challenge_kwargs, detail_part):
    state, calls = paid_api
    state["challenge"] = _challenge(**challenge_kwargs)
    out = asyncio.run(mcp_pkg.get_movers())
    assert out["error"] == "refused: price/payTo mismatch, nothing was paid"
    assert out["charged"] is False
    assert detail_part in out["detail"][0]
    assert out["expected"]["price_usd"] == 0.02
    assert len(calls) == 1 and not _signed(calls[0])


# --- session spend cap ---

def test_cap_default_is_50_cents_and_zero_is_off(monkeypatch):
    assert mcp_pkg._spend_cap_base_units() == 500_000
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", "0")
    assert mcp_pkg._spend_cap_base_units() == 0
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", "$2.00")
    assert mcp_pkg._spend_cap_base_units() == 2_000_000


@pytest.mark.parametrize("bad", ["abc", "-1", "nan", "inf", ""])
def test_bad_cap_value_falls_back_to_default_not_off(monkeypatch, bad):
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", bad)
    assert mcp_pkg._spend_cap_base_units() == 500_000


def test_cap_counts_at_signing_and_refuses_before_signing(paid_api, monkeypatch):
    state, calls = paid_api
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", "0.06")
    state["challenge"] = _challenge("50000")
    assert asyncio.run(mcp_pkg.get_sentiment_history("BTC")) == {"symbol": "BTC", "paid": True}
    assert mcp_pkg._spent_base_units == 50_000

    state["challenge"] = _challenge("10000")
    assert asyncio.run(mcp_pkg.get_sentiment("BTC")) == {"symbol": "BTC", "paid": True}
    assert mcp_pkg._spent_base_units == 60_000  # exactly at the cap is allowed

    n = len(calls)
    out = asyncio.run(mcp_pkg.get_sentiment("BTC"))
    assert out["error"] == "refused: session spend cap reached, nothing was paid"
    assert out["charged"] is False
    assert out["spent_usd"] == 0.06 and out["cap_usd"] == 0.06
    assert "MAX_SPEND_USD_PER_SESSION" in out["how_to_raise"] and "0 to turn the cap off" in out["how_to_raise"]
    assert len(calls) == n  # refused up front: no request at all
    assert mcp_pkg._spent_base_units == 60_000


def test_cap_hook_refuses_a_402_that_would_cross_it(paid_api, monkeypatch):
    """The listed price fits, but the hook sees the 402's real amount; a
    cheaper-than-listed ask is counted at what was actually signed."""
    state, calls = paid_api
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", "0.02")
    monkeypatch.setattr(mcp_pkg, "_spent_base_units", 5_000)
    state["challenge"] = _challenge("4000")  # below the $0.01 price: allowed
    assert asyncio.run(mcp_pkg.get_sentiment("BTC")) == {"symbol": "BTC", "paid": True}
    assert mcp_pkg._spent_base_units == 9_000  # counted at what was signed

    # The up-front check uses the listed price: 9000 + 10000 > 15000.
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", "0.015")
    n = len(calls)
    out = asyncio.run(mcp_pkg.get_sentiment("BTC"))
    assert out["charged"] is False and "spend cap" in out["error"]
    assert "$0.019" in out["detail"] and "($0.015)" in out["detail"]
    assert len(calls) == n


def test_usd_formatting_is_exact():
    assert mcp_pkg._usd(10_000) == "$0.01"
    assert mcp_pkg._usd(500_000) == "$0.50"
    assert mcp_pkg._usd(15_000) == "$0.015"
    assert mcp_pkg._usd(2_000_000) == "$2.00"


def test_cap_hook_direct(monkeypatch):
    """Pre-check passes but the hook must still refuse: simulate spending
    that happened between the pre-check and signing."""
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", "0.02")
    expected = {"price_usd": 0.02, "amount": 20000, "refused": None, "cap_refused": None}
    token = mcp_pkg._expected_payment.set(expected)
    try:
        class Req:
            amount = "20000"; pay_to = mcp_pkg.PAY_TO_ADDRESS
            network = mcp_pkg.CAIP2_NETWORK; asset = mcp_pkg.USDC_ASSET

        class Ctx:
            selected_requirements = Req()

        monkeypatch.setattr(mcp_pkg, "_spent_base_units", 10_000)
        result = mcp_pkg._check_requirements(Ctx())
        assert result is not None and "spend cap" in result.reason
        assert expected["cap_refused"] == 20000
        assert mcp_pkg._spent_base_units == 10_000
    finally:
        mcp_pkg._expected_payment.reset(token)


def test_cap_off_allows_any_total(paid_api, monkeypatch):
    state, _ = paid_api
    monkeypatch.setenv("MAX_SPEND_USD_PER_SESSION", "0")
    monkeypatch.setattr(mcp_pkg, "_spent_base_units", 10_000_000)
    state["challenge"] = _challenge("10000")
    assert asyncio.run(mcp_pkg.get_sentiment("BTC")) == {"symbol": "BTC", "paid": True}


def test_refusals_never_count(paid_api):
    state, _ = paid_api
    state["challenge"] = _challenge("20000")  # over the $0.01 price
    asyncio.run(mcp_pkg.get_sentiment("BTC"))
    assert mcp_pkg._spent_base_units == 0


# --- release files ---

def test_version_020_everywhere():
    assert PROJECT["version"] == VERSION
    assert SERVER["version"] == VERSION
    assert [p["version"] for p in SERVER["packages"]] == [VERSION]
    assert mcp_pkg.__version__ == VERSION


def test_server_json_title_website_and_env():
    assert SERVER["title"] and len(SERVER["title"]) <= 100
    assert SERVER["websiteUrl"] == "https://crypto-sentiment-x402.onrender.com"
    assert len(SERVER["description"]) < 100
    env = {e["name"]: e for e in SERVER["packages"][0]["environmentVariables"]}
    assert env["CDP_API_KEY_ID"]["isSecret"] and env["CDP_API_KEY_SECRET"]["isSecret"]
    cap = env["MAX_SPEND_USD_PER_SESSION"]
    assert cap["isSecret"] is False and cap["isRequired"] is False
    assert "0.50" in cap["description"]
    text = json.dumps(SERVER)
    for old in OLD_NAMES:
        assert not re.search(rf"(?<!get_)\b{old}\b", text), old


@pytest.mark.parametrize("doc", ["README.md", "MCP.md", "app/home.py"])
def test_docs_list_all_seven_tools_and_no_old_names(doc):
    text = (ROOT / doc).read_text()
    # The migration note names the old tools on purpose; nothing else may.
    text = re.sub(r"\*\*Renamed in 0\.2\.0:\*\*.*?are\s+gone;", "", text, flags=re.S)
    for name in TOOL_NAMES:
        assert name in text, (doc, name)
    for old in OLD_NAMES:
        assert not re.search(rf"(?<!get_)\b{old}\b(?!_x402)", text), (doc, old)
