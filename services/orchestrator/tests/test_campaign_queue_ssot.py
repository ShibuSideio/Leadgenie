"""V27.4.0 campaign queue dual-path pure tests (no Firestore)."""
from __future__ import annotations

import os
import sys

_SERVICES = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

from shared.campaign_queue import (  # noqa: E402
    approx_queue_bytes,
    load_queued_urls,
    queue_mode,
    url_item_id,
)


class _FakeSnap:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeCol:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def stream(self):
        return list(self._docs)[:50]


class _FakeCamp:
    def __init__(self, items):
        self._items = items

    def collection(self, name):
        assert name == "queue_items"
        snaps = [_FakeSnap(d) for d in self._items]
        return _FakeCol(snaps)

    def document(self, cid):
        return self


class _FakeDB:
    def __init__(self, items):
        self._camp = _FakeCamp(items)

    def collection(self, name):
        assert name == "campaigns"
        return self._camp


def test_url_item_id_stable():
    assert url_item_id("https://a.com") == url_item_id("https://a.com")
    assert len(url_item_id("https://a.com")) == 16


def test_load_merges_array_and_items_hybrid(monkeypatch=None):
    os.environ["CAMPAIGN_QUEUE_MODE"] = "hybrid"
    try:
        camp = {"unprocessed_queue": ["https://a.com", "https://b.com"]}
        db = _FakeDB([
            {"url": "https://b.com", "status": "queued"},
            {"url": "https://c.com", "status": "queued"},
            {"url": "https://d.com", "status": "consumed"},
        ])
        urls = load_queued_urls(db, "camp1", camp)
        assert urls[0] == "https://a.com"
        assert "https://b.com" in urls
        assert "https://c.com" in urls
        assert "https://d.com" not in urls
        # b not duplicated
        assert urls.count("https://b.com") == 1
    finally:
        os.environ.pop("CAMPAIGN_QUEUE_MODE", None)


def test_load_subcollection_falls_back_to_array():
    os.environ["CAMPAIGN_QUEUE_MODE"] = "subcollection"
    try:
        camp = {"unprocessed_queue": ["https://legacy.com"]}
        db = _FakeDB([])  # no items
        urls = load_queued_urls(db, "camp1", camp)
        assert urls == ["https://legacy.com"]
    finally:
        os.environ.pop("CAMPAIGN_QUEUE_MODE", None)


def test_load_array_mode_ignores_items():
    os.environ["CAMPAIGN_QUEUE_MODE"] = "array"
    try:
        camp = {"unprocessed_queue": ["https://only-array.com"]}
        db = _FakeDB([{"url": "https://items.com", "status": "queued"}])
        urls = load_queued_urls(db, "camp1", camp)
        assert urls == ["https://only-array.com"]
    finally:
        os.environ.pop("CAMPAIGN_QUEUE_MODE", None)


def test_queue_mode_default_hybrid():
    os.environ.pop("CAMPAIGN_QUEUE_MODE", None)
    assert queue_mode() == "hybrid"


def test_approx_bytes():
    assert approx_queue_bytes(["ab", "cd"]) > 0
