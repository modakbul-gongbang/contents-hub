"""Direct regression tests for SubscriptionStore CRUD and status invariants.

The store is the core state engine every other module builds on, but until
now it was only exercised indirectly through CLI/web tests. These tests pin
the behaviors the structural refactors must preserve.
"""

from __future__ import annotations

import pytest

from contents_hub.config import WikiConfig
from contents_hub.db import init_db
from contents_hub.subscriptions import (
    ScheduleConfig,
    SubscriptionStatus,
    SubscriptionStore,
)

URL = "https://example.com/feed"


@pytest.fixture
def store(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)
    (tmp_path / ".contents-hub").mkdir(parents=True, exist_ok=True)
    init_db(cfg)
    return SubscriptionStore(cfg)


def test_add_and_get_round_trip(store):
    added = store.add(url=URL, title="Example", source_type="rss.feed")
    fetched = store.get(URL)
    assert fetched is not None
    assert fetched.url == added.url
    assert fetched.title == "Example"
    assert fetched.source_type == "rss.feed"
    assert fetched.status == SubscriptionStatus.ACTIVE


def test_add_duplicate_url_raises(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    with pytest.raises(Exception):
        store.add(url=URL, title="Again", source_type="rss.feed")


def test_get_by_id_matches_get_by_url(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    by_url = store.get(URL)
    by_id = store.get_by_id(str(by_url.id))
    assert by_id is not None
    assert by_id.url == by_url.url
    assert store.get_by_id("999999") is None


def test_remove_deletes_subscription(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    removed = store.remove(URL)
    assert removed.url.rstrip("/") == URL.rstrip("/")
    assert store.get(URL) is None
    assert store.count == 0


def test_update_title_and_lenses_persist(store):
    store.add(url=URL, title="Old", source_type="rss.feed")
    store.update_title(URL, "New")
    store.update_lenses(URL, ["ai", "tools"])
    sub = store.get(URL)
    assert sub.title == "New"
    assert sub.lenses == ["ai", "tools"]


def test_update_config_merges_keys(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    store.update_config(URL, {"a": 1})
    store.update_config(URL, {"b": 2})
    sub = store.get(URL)
    assert sub.config["a"] == 1
    assert sub.config["b"] == 2


def test_status_transitions_round_trip(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    for status in (
        SubscriptionStatus.VALIDATING,
        SubscriptionStatus.PAUSED,
        SubscriptionStatus.ERROR,
        SubscriptionStatus.ACTIVE,
    ):
        store.set_status(URL, status)
        assert store.get(URL).status == status


def test_record_fetch_success_resets_errors(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    store.record_fetch(URL, item_count=0, error="boom")
    sub = store.get(URL)
    assert sub.status == SubscriptionStatus.ERROR
    assert sub.error_message == "boom"
    # R2.1: a failed run must NOT advance last_fetched_at.
    assert sub.last_fetched_at is None

    store.record_fetch(URL, item_count=3)
    sub = store.get(URL)
    assert sub.error_message in ("", None)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.last_fetched_at is not None


def test_repeated_fetch_errors_flip_status_to_broken(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    for _ in range(5):
        store.record_fetch(URL, item_count=0, error="boom")
    assert store.get(URL).status == SubscriptionStatus.BROKEN


def test_list_by_lens_is_case_insensitive(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    store.update_lenses(URL, ["AI"])
    assert [s.url for s in store.list_by_lens("ai")] == [store.get(URL).url]
    assert store.list_by_lens("missing") == []


def test_list_by_status_filters(store):
    store.add(url=URL, title="Example", source_type="rss.feed")
    store.add(url="https://other.example/feed", title="Other", source_type="rss.feed")
    store.set_status(URL, SubscriptionStatus.PAUSED)
    paused = store.list_by_status(SubscriptionStatus.PAUSED)
    active = store.list_by_status(SubscriptionStatus.ACTIVE)
    assert len(paused) == 1 and len(active) == 1


def test_schedule_config_validation():
    with pytest.raises(ValueError):
        ScheduleConfig(cron="0 9 * * *", interval_minutes=30)
    with pytest.raises(ValueError):
        ScheduleConfig(cron="not a cron")
    with pytest.raises(ValueError):
        ScheduleConfig(interval_minutes=1)
    assert ScheduleConfig(cron="0 9 * * *").effective_interval_minutes is None
    assert ScheduleConfig(interval_minutes=30).effective_interval_minutes == 30
