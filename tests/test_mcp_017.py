"""
0.1.7: search words first in the MCP Registry description, the PyPI summary
and three tool descriptions; list_supported_coins says the coin list is fixed
per release. (The version check moved to test_mcp_020.py.)
"""

import json
import pathlib
import tomllib

from tests.test_mcp_tool_metadata import _tools

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text())
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
def _flat(text):
    return " ".join(text.split())


def test_registry_description_under_limit_and_leads_with_search_words():
    d = SERVER["description"]
    assert len(d) < 100  # MCP Registry schema maxLength is 100
    assert d.startswith("Crypto news sentiment for 24 coins")
    assert "x402" in d


def test_pypi_summary_leads_with_search_words():
    d = PROJECT["description"]
    assert d.startswith("Crypto news sentiment MCP server")
    assert "\n" not in d and len(d) <= 512  # PyPI summary: one line, 512 max
    # 0.2.0: also names the movers and the free market-wide index, and still
    # ends with a "Use when ..." sentence.
    assert "24h sentiment movers" in d
    assert "free market-wide sentiment index" in d
    last = d.rsplit(". ", 1)[1]
    assert last.startswith("Use when ") and last.endswith(".")


def test_tool_descriptions_lead_with_search_words_then_use_when():
    tools = _tools()
    leads = {
        "get_sentiment": "Crypto news sentiment for one coin",
        "get_sentiment_history": "Historical crypto news sentiment for one coin",
        "list_supported_coins": "The 24 crypto tickers",
    }
    for name, lead in leads.items():
        paras = [_flat(p) for p in tools[name].description.strip().split("\n\n")]
        assert paras[0].startswith(lead), name
        assert paras[1].startswith("Use when "), name


def test_crypto_sentiment_trimmed():
    old_len = 1024  # 0.1.6 description, whitespace collapsed
    assert len(_flat(_tools()["get_sentiment"].description)) < old_len


def test_refusal_wording_kept():
    tools = _tools()
    assert "nothing is charged" in _flat(tools["get_sentiment"].description)
    assert "charged: false" in _flat(tools["get_sentiment"].description)
    assert "Refused and not charged" in _flat(tools["get_sentiment_history"].description)
    assert "nothing is charged" in _flat(tools["list_supported_coins"].description)


def test_coin_list_fixed_per_release_sentence():
    d = _flat(_tools()["list_supported_coins"].description)
    assert (
        "The coin list is fixed per release: new coins come with a new "
        "version of this package, never while the server runs." in d
    )
