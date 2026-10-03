"""
Human-readable home page for GET / -- served only when the request's Accept
header asks for text/html (a browser). Every other client keeps getting the
JSON index from app/main.py's root().

Every fact on the page is passed in from the code/config that owns it
(price from X402_PRICE_USD, plans from /billing/pricing, outlets from
app/sources/news.py, validation status from app/validation.json), so the
page can't drift from what the API actually does. Self-contained: inline
CSS, no scripts, fonts or trackers.
"""

import json
from html import escape

GITHUB_URL = "https://github.com/whatevercat-creator/crypto-sentiment-x402"
RAPIDAPI_URL = "https://rapidapi.com/whatevercat/api/crypto-sentiment-analysis"
PYPI_URL = "https://pypi.org/project/crypto-sentiment-x402-mcp/"

_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--accent:#58a6ff;--bull:#3fb950;--bear:#f85149;--warn:#d29922;--code:#0b0f14}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px 24px}
h1{font-size:2rem;line-height:1.2;margin:0 0 12px}
h2{font-size:1.25rem;margin:40px 0 12px;padding-top:8px;border-top:1px solid var(--border)}
p{margin:0 0 12px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.lede{font-size:1.15rem;color:var(--muted)}
.tags span{display:inline-block;font-size:.8rem;font-weight:600;padding:2px 8px;border-radius:999px;
border:1px solid var(--border);margin:0 6px 6px 0}
.bull{color:var(--bull)}.bear{color:var(--bear)}.neu{color:var(--muted)}
ul{padding-left:20px;margin:0 0 12px}
li{margin:4px 0}
.price{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
.price strong{font-size:1.6rem}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.plan{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;
display:flex;flex-direction:column}
.plan h3{margin:0 0 4px;font-size:1rem}
.plan .amt{font-size:1.3rem;font-weight:700}
.plan .small{font-size:.85rem;color:var(--muted)}
.plan code{overflow-wrap:anywhere;font-size:.8rem}
.plan form{margin:auto 0 0;padding-top:8px}
.plan input{width:100%;margin:0 0 8px;padding:8px 10px;font:inherit;font-size:.9rem;color:var(--text);
background:var(--code);border:1px solid var(--border);border-radius:6px}
button{width:100%;padding:9px 12px;font:inherit;font-weight:600;color:#0d1117;background:var(--accent);
border:0;border-radius:6px;cursor:pointer}
button:hover{filter:brightness(1.1)}
.dev{font-size:.75rem;color:var(--muted);margin:8px 0 0}
.key{font-size:1rem;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px;
overflow-wrap:anywhere;margin:0 0 12px}
pre{background:var(--code);border:1px solid var(--border);border-radius:8px;padding:12px;
overflow-x:auto;font-size:.85rem;line-height:1.45;margin:0 0 12px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
p code,li code{background:var(--panel);padding:1px 5px;border-radius:4px;font-size:.88em}
.box{margin-top:32px;border:1px solid var(--warn);border-left-width:4px;border-radius:8px;padding:14px 16px;background:var(--panel)}
.box h2{border:0;margin:0 0 8px;padding:0}
.muted{color:var(--muted)}
footer{max-width:760px;margin:40px auto 0;padding:20px 16px 40px;border-top:1px solid var(--border);
color:var(--muted);font-size:.9rem}
footer a{margin-right:16px;display:inline-block}
"""


def _validation_html(validation: dict) -> str:
    rows = []
    for symbol, entry in (validation.get("symbols") or {}).items():
        if entry.get("status") == "measured":
            rows.append(
                f"<li><strong>{escape(symbol)}</strong>: {escape(entry.get('verdict', ''))}"
                f" &mdash; {escape(entry.get('summary', ''))}</li>"
            )
        else:
            expected = entry.get("first_results_expected")
            when = f" First results expected {escape(expected)}." if expected else ""
            rows.append(
                f"<li><strong>{escape(symbol)}</strong>: still measuring on hourly data.{when}</li>"
            )
    if not rows:
        rows.append(f"<li>{escape(validation.get('detail', 'Measuring on hourly data.'))}</li>")
    prior = validation.get("honest_prior", "")
    return (
        '<section class="box" id="validation">'
        "<h2>Does it predict price?</h2>"
        "<p>Unknown yet, and we're not going to pretend otherwise. Whether the score "
        "leads, coincides with, or lags price is being measured on hourly data and "
        f'published at <a href="/validation">/validation</a>, whatever it shows.</p>'
        f"<ul>{''.join(rows)}</ul>"
        + (f'<p class="muted">{escape(prior)}</p>' if prior else "")
        + "</section>"
    )


def _plan_form(tier: str) -> str:
    """Plain HTML forms, no JavaScript: app/billing.py answers form posts
    with a page (free key) or a redirect to Stripe Checkout (paid plans)."""
    if tier == "free":
        return (
            '<form method="post" action="/billing/signup-free">'
            '<input type="email" name="email" required autocomplete="email" '
            'placeholder="you@example.com" aria-label="Email">'
            '<button type="submit">Get free key</button></form>'
        )
    return (
        f'<form method="post" action="/billing/checkout/{escape(tier)}">'
        '<button type="submit">Subscribe</button></form>'
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{escape(title)} - Crypto Sentiment API</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>{escape(title)}</h1>
{body}
<p><a href="/">&larr; Back to Crypto Sentiment API</a></p>
</main>
</body>
</html>
"""


def render_message_page(title: str, message: str) -> str:
    return _page(title, f"<p>{escape(message)}</p>")


def render_api_key_page(
    *, plan_label: str, api_key: str, calls_per_month: int, base_url: str, save_note: str
) -> str:
    curl = escape(f'curl -H "X-API-Key: {api_key}" {base_url}/v1/sentiment/BTC')
    return _page(
        f"Your {plan_label} API key",
        f'<p class="key"><code>{escape(api_key)}</code></p>'
        f"<p><strong>Save this key now.</strong> {escape(save_note)}</p>"
        f"<p>Your {escape(plan_label)} plan gives you {calls_per_month:,} calls a month on "
        "<code>GET /v1/sentiment/{symbol}</code>. Send the key in the <code>X-API-Key</code> header:</p>"
        f"<pre><code>{curl}</code></pre>",
    )


def render_home(
    *,
    price_usd: str,
    network_mode: str,
    base_url: str,
    outlets: list,
    pricing: dict,
    alert_limits: dict,
    dataset_plans: list,
    example_response: dict,
    validation: dict,
) -> str:
    network = "Base" if network_mode == "mainnet" else "Base Sepolia (testnet)"
    ppc = pricing["pay_per_call"]

    plans = "".join(
        '<div class="plan">'
        f"<h3>{escape(p['label'])}</h3>"
        f'<div class="amt">${p["price_usd_per_month"]}<span class="small">/mo</span></div>'
        f"<p class=\"small\">{escape(p['includes'])}</p>"
        f"{_plan_form(tier)}"
        f'<p class="dev">For developers: <code>{escape(pricing["signup"].get(tier, ""))}</code></p>'
        "</div>"
        for tier, p in pricing["subscriptions"].items()
    )

    alert_tiers = " and ".join(
        f"{escape(label)} ({n} watches)" for label, n in alert_limits.items() if n
    )
    example = escape(json.dumps(example_response, indent=2))
    curl = escape(f"curl -i {base_url}/sentiment/BTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Sentiment API</title>
<meta name="description" content="Crypto sentiment scores for AI agents and trading bots, paid per call with x402.">
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>Crypto Sentiment API</h1>
<p class="lede">Crypto sentiment scores for AI agents and trading bots, paid per call with x402.</p>

<h2>How it works</h2>
<p>For each ticker, the API pulls recent headlines from {len(outlets)} crypto news outlets
({escape(", ".join(outlets))}) plus the Fear &amp; Greed Index, and scores them with VADER
sentiment analysis plus a crypto slang lexicon.</p>
<p class="tags">You get back a
<span class="bull">bullish</span><span class="bear">bearish</span><span class="neu">neutral</span>
label, a numeric score, and a per-source breakdown.</p>

<h2>Pricing</h2>
<div class="price">
<strong>{escape(price_usd)}</strong> per call, in USDC on {network}. No signup.
<div class="muted">Endpoint: <code>{escape(ppc["endpoint"])}</code></div>
</div>
<p>Prefer a monthly bill and an API key? Subscription plans (from
<a href="/billing/pricing">/billing/pricing</a>), all on <code>GET /v1/sentiment/{{symbol}}</code>
with an <code>X-API-Key</code> header:</p>
<div class="plans">{plans}</div>

<h2>Call it</h2>
<pre><code>{curl}</code></pre>
<p>An unpaid request returns HTTP 402 with the payment requirements. Any x402 client pays
{escape(price_usd)} and retries automatically, and gets JSON like this (trimmed):</p>
<pre><code>{example}</code></pre>

{_validation_html(validation)}

<h2>Other ways to use it</h2>
<ul>
<li><a href="{GITHUB_URL}/blob/main/ALERTS.md">Alerts</a>: get a webhook, Discord or Telegram
message when a coin's sentiment shifts. Included with {alert_tiers}.</li>
<li><a href="/dataset/info">Daily dataset</a>: one sentiment reading per tracked coin per day,
exportable as CSV or JSON. Included with {escape(" and ".join(dataset_plans))}.</li>
<li><a href="{RAPIDAPI_URL}">RapidAPI</a>: subscribe and pay through RapidAPI instead.</li>
<li><a href="{GITHUB_URL}/blob/main/INTEGRATIONS.md">TradingView alert relay</a>: forward
TradingView alerts enriched with current sentiment to your alert channels.</li>
<li><a href="{PYPI_URL}">MCP server on PyPI</a>: <code>pip install crypto-sentiment-x402-mcp</code>
to give an MCP client the sentiment tool.</li>
</ul>
</main>
<footer>
<a href="/docs">API docs</a>
<a href="/billing/pricing">Pricing (JSON)</a>
<a href="/validation">Validation</a>
<a href="{GITHUB_URL}">GitHub</a>
<p>News sentiment only. Not a price prediction or financial advice.</p>
</footer>
</body>
</html>
"""
