"""
2026-11-06 polish: Bazaar discovery copy in the 402, OpenAPI tags/schemas/
symbol enums, one shape for every "not enough news" refusal, the
free_signup log line, the root page's /history link, robots.txt/sitemap.xml
and the public pages' head tags.
"""

import base64
import json
import logging
import re
from pathlib import Path

import pytest
from x402.extensions.bazaar.facilitator import _is_valid_service_name, _sanitize_tags

from tests.test_home import BROWSER_ACCEPT, client  # noqa: F401  (shared fixture)
from tests.test_free_signup import api  # noqa: F401
from tests.test_insufficient_news import fake_payload, key  # noqa: F401
from tests.test_paid_call_log import _payment_header, paid  # noqa: F401
from app import billing, home, main
from app.coins import COIN_NAMES

ROUTES = ("GET /sentiment/:symbol", "GET /history/:symbol")


# --------------------------------------------------------------------------
# a. Bazaar discovery metadata
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
def test_discovery_copy_reads_well(route):
    cfg = main.routes[route]
    assert "Use when an agent needs" in cfg.description
    assert len(cfg.description) < 500
    assert "not charged" in cfg.description
    # Survives the facilitator's own sanitizing unchanged, so the Bazaar
    # lists exactly this.
    assert _is_valid_service_name(cfg.service_name)
    assert _sanitize_tags(cfg.tags) == cfg.tags


@pytest.mark.parametrize("path", ["/sentiment/BTC", "/history/BTC"])
def test_402_resource_carries_service_name_and_tags(client, path):  # noqa: F811
    r = client.get(path)
    assert r.status_code == 402
    resource = json.loads(base64.b64decode(r.headers["payment-required"]))["resource"]
    assert resource["serviceName"] == "Crypto Sentiment API"
    assert resource["tags"] == ["crypto", "sentiment", "news", "trading", "bitcoin"]
    assert "Use when an agent needs" in resource["description"]


def test_sentiment_description_counts_match_code():
    d = main.SENTIMENT_ROUTE_DESCRIPTION
    assert f"{len(COIN_NAMES)} coins" in d


# --------------------------------------------------------------------------
# b. OpenAPI
# --------------------------------------------------------------------------

def test_openapi_symbol_enums_come_from_coins(client):  # noqa: F811
    spec = client.get("/openapi.json").json()
    for path in ("/sentiment/{symbol}", "/history/{symbol}"):
        [symbol] = [p for p in spec["paths"][path]["get"]["parameters"] if p["name"] == "symbol"]
        assert symbol["schema"]["enum"] == list(COIN_NAMES), path


def test_openapi_200_schemas_and_window_example(client):  # noqa: F811
    spec = client.get("/openapi.json").json()
    for path, required in (
        ("/sentiment/{symbol}", ["symbol", "overall_sentiment"]),
        ("/history/{symbol}", ["symbol", "start", "end", "count", "rows"]),
    ):
        media = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
        assert media["schema"]["type"] == "object"
        assert media["schema"]["required"] == required
        assert media["example"]
    example = spec["paths"]["/sentiment/{symbol}"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    assert example["overall_sentiment"]["window"] == main.WINDOW_VERSION


def test_openapi_tags(client):  # noqa: F811
    spec = client.get("/openapi.json").json()
    tags = {p: m["get"].get("tags") for p, m in spec["paths"].items() if "get" in m}
    assert tags["/sentiment/{symbol}"] == tags["/history/{symbol}"] == ["Paid (x402)"]
    assert tags["/v1/sentiment/{symbol}"] == ["Subscription (API key)"]
    for path in ("/", "/archive", "/validation", "/health", "/llms.txt",
                 "/.well-known/x402", "/.well-known/security.txt", "/transparency"):
        assert tags[path] == ["Free"], path
    assert [t["name"] for t in spec["tags"]] == ["Paid (x402)", "Subscription (API key)", "Free"]
    assert "/robots.txt" not in spec["paths"] and "/sitemap.xml" not in spec["paths"]


def test_home_history_link_matches_docs_deep_link(client):  # noqa: F811
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/history/{symbol}"]["get"]
    tag = op["tags"][0].replace(" ", "%20")
    assert home.HISTORY_DOCS_PATH == f"/docs#/{tag}/{op['operationId']}"


# --------------------------------------------------------------------------
# c. "Not enough news" refusals: one status, one shape, "Not charged"
# --------------------------------------------------------------------------

def test_insufficient_news_refusals_match_across_lanes(paid, client, key, monkeypatch):  # noqa: F811
    paid_client, _ = paid
    monkeypatch.setattr(main, "compute_sentiment_payload", fake_payload)
    x402 = paid_client.get("/sentiment/LINK", headers={"PAYMENT-SIGNATURE": _payment_header(paid_client, "LINK")})
    v1 = client.get("/v1/sentiment/LINK", headers={"X-API-Key": key})
    assert x402.status_code == v1.status_code == 422
    assert x402.json() == v1.json()
    assert set(x402.json()) == {"detail"} and "Not charged." in x402.json()["detail"]


# /history's no_headlines_in_range message ("... Not charged. ...") is pinned
# word for word in tests/test_history_coverage.py.


# --------------------------------------------------------------------------
# d. .well-known
# --------------------------------------------------------------------------

def test_well_known_x402_updated_is_current(client):  # noqa: F811
    body = client.get("/.well-known/x402").json()
    assert body["updated"] == "2026-11-09T00:00:00Z"
    assert [r["url"] for r in body["resources"]] == [
        f"{main.PUBLIC_BASE_URL}/sentiment/{{symbol}}",
        f"{main.PUBLIC_BASE_URL}/history/{{symbol}}",
        f"{main.PUBLIC_BASE_URL}/movers",
    ]


# --------------------------------------------------------------------------
# e. Root page links /history
# --------------------------------------------------------------------------

def test_root_html_links_history(client):  # noqa: F811
    html = client.get("/", headers={"Accept": BROWSER_ACCEPT}).text
    assert html.count(f'href="{home.HISTORY_DOCS_PATH}"') == 2
    assert 'href="/archive"' in html


def test_root_json_points_to_history(client):  # noqa: F811
    body = client.get("/").json()
    assert body["history"].startswith("/history/{symbol}")
    assert main.archive.HISTORY_PRICE_USD in body["history"]


# --------------------------------------------------------------------------
# f. README
# --------------------------------------------------------------------------

def test_readme_top_is_for_buyers():
    readme = (Path(main.__file__).parent.parent / "README.md").read_text()
    top = readme.split("\n---\n")[0]
    assert ", ".join(COIN_NAMES) in " ".join(top.split())
    assert "/history/{symbol}" in top and "never charged" in top
    assert "`404`" in top and "`422`" in top
    assert "## 1. Get a Base wallet address" in readme  # developer sections kept


# --------------------------------------------------------------------------
# g. robots.txt, sitemap.xml, head tags
# --------------------------------------------------------------------------

def test_robots_txt(client):  # noqa: F811
    r = client.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    disallowed = re.findall(r"^Disallow: (\S+)$", r.text, re.M)
    assert disallowed == ["/v1/", "/billing/", "/alerts/", "/rapidapi/", "/integrations/"]
    # The paid routes must stay findable by directories and agent crawlers.
    assert not any(p in ("/sentiment/", "/history/", "/") for p in disallowed)
    assert f"Sitemap: {main.PUBLIC_BASE_URL}/sitemap.xml" in r.text


def test_sitemap_xml(client):  # noqa: F811
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    base = main.PUBLIC_BASE_URL
    assert locs == [base + p for p in ("/", "/archive", "/docs", "/llms.txt", "/validation", "/transparency", "/market-sentiment")]


@pytest.mark.parametrize("path,canonical", [("/", "/"), ("/archive", "/archive")])
def test_public_pages_have_head_tags(client, path, canonical):  # noqa: F811
    html = client.get(path, headers={"Accept": BROWSER_ACCEPT}).text
    assert f'<link rel="canonical" href="{main.PUBLIC_BASE_URL}{canonical}">' in html
    for prop in ("og:title", "og:description", "og:url", "og:type", "og:site_name"):
        assert f'property="{prop}"' in html, prop
    title = re.search(r"<title>([^<]+)</title>", html).group(1)
    assert "Crypto Sentiment" in title and len(title) > len("Crypto Sentiment API")
    assert html.count("<title>") == 1 and 'name="robots"' not in html


# --------------------------------------------------------------------------
# h. free_signup log line
# --------------------------------------------------------------------------

def _signup_lines(caplog):
    out = []
    for record in caplog.records:
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "free_signup":
            out.append(data)
    return out


@pytest.mark.parametrize("as_form", [False, True], ids=["json", "form"])
def test_free_signup_logs_created_with_domain_only(api, caplog, as_form):  # noqa: F811
    email = "Someone@Mail.Test-Shop.dev"
    kwargs = {"data": {"email": email}} if as_form else {"json": {"email": email}}
    with caplog.at_level(logging.INFO):
        r = api.post("/billing/signup-free", **kwargs)
    assert r.status_code == 200
    [line] = _signup_lines(caplog)
    assert set(line) == {"event", "ts", "email_domain", "result"}
    assert line["email_domain"] == "mail.test-shop.dev" and line["result"] == "created"
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "someone" not in logged.lower()
    assert "csk_" not in logged


@pytest.mark.parametrize("email,domain,reason", [
    ("bot@example.com", "example.com", "sample_domain"),
    ("not an email", None, "invalid_email"),
])
@pytest.mark.parametrize("as_form", [False, True], ids=["json", "form"])
def test_free_signup_logs_rejections(api, caplog, as_form, email, domain, reason):  # noqa: F811
    kwargs = {"data": {"email": email}} if as_form else {"json": {"email": email}}
    with caplog.at_level(logging.INFO):
        assert api.post("/billing/signup-free", **kwargs).status_code == 400
    [line] = _signup_lines(caplog)
    assert line == {"event": "free_signup", "ts": line["ts"], "email_domain": domain,
                    "result": "rejected", "reason": reason}
    assert "not an email" not in "\n".join(r.getMessage() for r in caplog.records)


def test_free_signup_logs_rate_limited(api, caplog):  # noqa: F811
    ip = {"cf-connecting-ip": "203.0.113.9"}
    for i in range(billing.SIGNUP_LIMIT):
        assert api.post("/billing/signup-free", json={"email": f"u{i}@shop.dev"}, headers=ip).status_code == 200
    with caplog.at_level(logging.INFO):
        assert api.post("/billing/signup-free", json={"email": "late@shop.dev"}, headers=ip).status_code == 429
    [line] = _signup_lines(caplog)
    assert (line["email_domain"], line["result"], line["reason"]) == ("shop.dev", "rejected", "rate_limited")
