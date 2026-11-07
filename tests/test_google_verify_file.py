"""
2026-11-07: Google Search Console HTML-file verification. The file Google
issued is served byte for byte at /<its filename>, free, for any Accept
header, and kept out of /openapi.json and the sitemap.
"""

from pathlib import Path

import pytest

from tests.test_home import BROWSER_ACCEPT, client  # noqa: F401  (shared fixture)
from app import main

STATIC = Path(main.__file__).parent / "static"
[FILE] = sorted(STATIC.glob("google*.html"))


def test_path_is_the_files_own_name():
    assert main.GOOGLE_VERIFY_PATH == "/" + FILE.name
    assert FILE.name.startswith("google") and FILE.name.endswith(".html")


@pytest.mark.parametrize("headers", [{"Accept": BROWSER_ACCEPT}, {"Accept": "text/html"}, {"Accept": "*/*"}, {}],
                         ids=["browser", "text-html", "wildcard", "none"])
def test_serves_file_byte_for_byte(client, headers):  # noqa: F811
    r = client.get("/" + FILE.name, headers=headers)
    assert r.status_code == 200
    assert r.content == FILE.read_bytes()
    assert r.headers["content-type"].startswith("text/html")
    assert "payment-required" not in {k.lower() for k in r.headers}


def test_content_is_googles_format():
    assert FILE.read_text() == f"google-site-verification: {FILE.name}"


def test_not_in_openapi_or_sitemap(client):  # noqa: F811
    assert FILE.name not in client.get("/openapi.json").text
    assert FILE.name not in client.get("/sitemap.xml").text


def test_meta_tag_still_on_home(client):  # noqa: F811
    html = client.get("/", headers={"Accept": BROWSER_ACCEPT}).text
    assert 'name="google-site-verification"' in html
