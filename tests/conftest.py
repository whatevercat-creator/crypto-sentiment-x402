"""
Shared test setup.

The rolling story store (app/news_store.py) is off by default in tests:
stored items would otherwise carry over from one test into the next through
the shared test database. tests/test_news_store.py turns it on, each test
with its own empty database.
"""

import pytest


@pytest.fixture(autouse=True)
def _news_store_off(monkeypatch):
    # Imported here, not at module level, so app.billing reads
    # BILLING_DB_PATH only after the test modules have set it.
    from app import news_store

    monkeypatch.setattr(news_store, "ENABLED", False)
