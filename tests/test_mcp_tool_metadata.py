"""The MCP tools' titles, annotations and descriptions as a client sees them
(tools/list). Free tools are read-only and idempotent; paid tools are
neither, since each call charges. Descriptions name the refusals that
aren't charged. No network."""

import asyncio

import pytest

import crypto_sentiment_x402_mcp as mcp_pkg

FREE = {
    "list_supported_coins": "List Supported Coins",
    "archive_coverage": "Archive Coverage",
    "price_quote": "Price Quote",
}
PAID = {
    "crypto_sentiment": "Current Crypto Sentiment",
    "get_hourly_history": "Hourly Sentiment History",
}


def _tools():
    return {t.name: t for t in asyncio.run(mcp_pkg.mcp.list_tools())}


def _wire(tool):
    """The tool as tools/list sends it (camelCase keys)."""
    return tool.model_dump(by_alias=True, exclude_none=True)


def test_every_tool_is_covered():
    assert set(_tools()) == set(FREE) | set(PAID)
    assert set(FREE) == set(mcp_pkg.FREE_TOOLS)
    assert set(PAID) == set(mcp_pkg.TOOL_PRICES_USD)


@pytest.mark.parametrize("name", sorted(FREE))
def test_free_tool_annotations(name):
    """list_supported_coins answers from a local list (closed world); the
    other free tools call the live API."""
    wire = _wire(_tools()[name])
    assert wire["title"] == FREE[name]
    assert wire["annotations"] == {
        "readOnlyHint": True,
        "idempotentHint": True,
        "destructiveHint": False,
        "openWorldHint": name != "list_supported_coins",
    }


@pytest.mark.parametrize("name", sorted(PAID))
def test_paid_tool_annotations(name):
    wire = _wire(_tools()[name])
    assert wire["title"] == PAID[name]
    assert wire["annotations"] == {
        "readOnlyHint": False,
        "idempotentHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    }


def test_crypto_sentiment_description():
    d = " ".join(_tools()["crypto_sentiment"].description.split())
    assert "$0.01" in d
    assert "list_supported_coins" in d
    assert "uppercase" in d
    assert "nothing is charged" in d
    assert "insufficient recent news" in d
    assert "charged: false" in d


def test_get_hourly_history_description():
    d = " ".join(_tools()["get_hourly_history"].description.split())
    assert "$0.05" in d
    assert "Refused and not charged" in d
    for refusal in ("400", "symbol not in the archive", "no_readings_in_range", "no_headlines_in_range"):
        assert refusal in d, refusal
    assert "start <= observed_at < end" in d
    assert "30 days" in d and "7 days" in d
    assert "check archive_coverage first" in d


def test_price_quote_description():
    d = " ".join(_tools()["price_quote"].description.split())
    assert "When not to use it" in d
    assert "$0.01" in d and "$0.05" in d
    assert "insufficient recent news" in d and "not charged" in d
    # The network/asset/payTo listing was dropped; the response carries them.
    assert "payTo" not in d


def test_list_supported_coins_description():
    d = " ".join(_tools()["list_supported_coins"].description.split())
    assert "rejected by those tools' input schema" in d
    assert "refused by the API" not in d
    assert "no network call" in d


def test_archive_coverage_description():
    d = " ".join(_tools()["archive_coverage"].description.split())
    assert d.count("Free") == 1
    assert "coverage_7d_pct" in d
    assert "not charged" in d


def test_descriptions_are_front_loaded():
    """The first line says what the tool does, and paid tools say their
    price there too."""
    tools = _tools()
    for name in PAID:
        first_para = tools[name].description.strip().split("\n\n")[0]
        assert f"${mcp_pkg.TOOL_PRICES_USD[name]:.2f}" in first_para, name
    for name in FREE:
        assert tools[name].description.strip().startswith("Free"), name


def test_history_symbol_is_still_a_free_string():
    """Only a description was added: get_hourly_history still accepts any
    string and leaves validation to the API."""
    schema = _tools()["get_hourly_history"].input_schema["properties"]["symbol"]
    assert schema["type"] == "string"
    assert "enum" not in schema
    assert schema["description"]
