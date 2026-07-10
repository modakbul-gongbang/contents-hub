"""Pending-approval (validating) subscription flow.

Covers:
- The VALIDATING status round-trips through the store.
- /subscriptions/{id}/keep flips a validating sub to active and drops
  trial-only config keys.
- /subscriptions/{id}/discard deletes the subscription and its raw_items.
- /subscriptions/{id}/retry_validation clears trial_result and sample items
  while preserving status=validating.
- The three endpoints are no-ops for non-validating subs.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from contents_hub.config import WikiConfig
from contents_hub.db import init_db
from contents_hub.subscriptions import (
    SubscriptionStatus,
    SubscriptionStore,
)
from contents_hub.web.app import create_app


@dataclass
class _StubFetchResult:
    """Mimics the subset of ``contents_hub.models.FetchResult`` that the web
    layer's ``_run_trial_fetch`` reads."""

    ok: bool = True
    items: list = field(default_factory=list)
    error: str = ""


async def _stub_executor_trial(*args, **kwargs):
    """No-op ``executor.execute_trial``: the trial-fetch background task must not
    hit the real agent during these endpoint tests.

    Post-refactor (T13/R-T7.3) the web app's ``_run_trial_fetch`` calls
    :func:`contents_hub.executor.execute_trial` directly (its local binding is
    imported inside the function body), so this is the correct stub site.
    """
    return _StubFetchResult()


@pytest.fixture
def vault(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)
    (tmp_path / ".contents-hub").mkdir(parents=True, exist_ok=True)
    init_db(cfg)
    return cfg


@pytest.fixture(autouse=True)
def _stub_executor(monkeypatch):
    """Globally stub :func:`contents_hub.executor.execute_trial` so any background
    trial fetch is a no-op."""
    monkeypatch.setattr(
        "contents_hub.executor.execute_trial", _stub_executor_trial, raising=True
    )


@pytest.fixture
def client(vault):
    app = create_app(vault)
    return TestClient(app)


def _seed_validating_sub(
    cfg,
    *,
    url: str = "https://example.com/",
    source_type: str = "webpage",
    with_trial_result: bool = True,
    trial_ok: bool = True,
) -> int:
    store = SubscriptionStore(cfg)
    store.add(url=url, title="Example", source_type=source_type)
    store.set_status(url, SubscriptionStatus.VALIDATING)
    extras: dict = {}
    if with_trial_result:
        samples = (
            [
                {
                    "url": "https://example.com/a",
                    "title": "Sample A",
                    "body": "body of a" * 10,
                    "published_at": "2026-04-10T00:00:00+00:00",
                },
                {
                    "url": "https://example.com/b",
                    "title": "Sample B",
                    "body": "body of b",
                    "published_at": "2026-04-11T00:00:00+00:00",
                },
            ]
            if trial_ok
            else []
        )
        extras["trial_result"] = {
            "ok": trial_ok,
            "items": len(samples),
            "error": "" if trial_ok else "trial timed out",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "recipe_mode": "catalog" if trial_ok else "failed",
            "samples": samples,
        }
    store.update_config(url, extras)
    sub = store.get(url)
    return int(sub.id)


def _add_raw_item(cfg, sub_id: int, url: str = "https://example.com/a") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(cfg.meta_path / "state.db") as conn:
        conn.execute(
            "INSERT INTO raw_items (url, title, origin, priority, status, "
            "subscription_id, collected_at, updated_at) "
            "VALUES (?, 'T', 'subscription', 50, 'raw', ?, ?, ?)",
            (url, sub_id, now, now),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Enum wiring
# ---------------------------------------------------------------------------


def test_validating_status_round_trips(vault):
    store = SubscriptionStore(vault)
    store.add(url="https://example.com/", title="E", source_type="webpage")
    store.set_status("https://example.com/", SubscriptionStatus.VALIDATING)
    sub = store.get("https://example.com/")
    assert sub.status == SubscriptionStatus.VALIDATING


def test_subscription_relearn_endpoint_is_removed(vault, client):
    sub_id = _seed_validating_sub(vault)

    resp = client.post(f"/subscriptions/{sub_id}/relearn")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Keep
# ---------------------------------------------------------------------------


def test_keep_activates_and_commits_samples(vault, client):
    sub_id = _seed_validating_sub(vault)

    resp = client.post(f"/subscriptions/{sub_id}/keep")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["inserted"] == 2  # seeded 2 samples

    store = SubscriptionStore(vault)
    sub = store.get_by_id(str(sub_id))
    assert sub.status == SubscriptionStatus.ACTIVE
    assert "trial_result" not in sub.config
    assert "trial_pre_recipe" not in sub.config
    assert "trial_pre_had_override" not in sub.config

    # Samples landed in raw_items with body + published_at populated.
    with sqlite3.connect(vault.meta_path / "state.db") as conn:
        rows = conn.execute(
            "SELECT url, title, body, published_at FROM raw_items "
            "WHERE subscription_id = ? ORDER BY url",
            (sub_id,),
        ).fetchall()
    urls = [r[0] for r in rows]
    assert urls == ["https://example.com/a", "https://example.com/b"]
    assert all(len(r[2]) > 0 for r in rows)  # body populated
    assert all(r[3] is not None for r in rows)  # published_at populated


def test_keep_no_samples_still_activates(vault, client):
    """A successful-but-zero-items trial can still be Kept (becomes an empty
    active sub that future fetches will populate)."""
    store = SubscriptionStore(vault)
    store.add(url="https://example.com/", title="E", source_type="webpage")
    store.set_status("https://example.com/", SubscriptionStatus.VALIDATING)
    store.update_config(
        "https://example.com/",
        {
            "trial_result": {
                "ok": True, "items": 0, "error": "",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "recipe_mode": "catalog", "samples": [],
            },
        },
    )
    sub = store.get("https://example.com/")

    resp = client.post(f"/subscriptions/{sub.id}/keep")
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 0

    with sqlite3.connect(vault.meta_path / "state.db") as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM raw_items WHERE subscription_id = ?", (int(sub.id),)
        ).fetchone()[0]
    assert n == 0


def test_keep_is_noop_for_active_sub(vault, client):
    store = SubscriptionStore(vault)
    store.add(url="https://example.com/", title="E", source_type="webpage")
    # default status is ACTIVE — don't flip to validating
    sub = store.get("https://example.com/")

    resp = client.post(f"/subscriptions/{sub.id}/keep")
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


def test_discard_deletes_sub(vault, client):
    """Trial never inserts into raw_items, so discard only needs to nuke the sub."""
    sub_id = _seed_validating_sub(vault)

    resp = client.post(f"/subscriptions/{sub_id}/discard")
    assert resp.status_code == 200
    assert resp.json()["status"] == "discarded"

    store = SubscriptionStore(vault)
    assert store.get_by_id(str(sub_id)) is None


def test_discard_is_noop_for_active_sub(vault, client):
    store = SubscriptionStore(vault)
    store.add(url="https://example.com/", title="E", source_type="webpage")
    sub = store.get("https://example.com/")

    resp = client.post(f"/subscriptions/{sub.id}/discard")
    assert resp.json()["status"] == "skipped"
    # sub still exists
    assert store.get_by_id(sub.id) is not None


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_clears_trial_result_and_reruns(vault, client, monkeypatch):
    sub_id = _seed_validating_sub(vault, with_trial_result=True, trial_ok=False)

    # source_type=webpage runs its trial headed (browser + chromux_navigate),
    # so stub the chromux launch/cleanup helpers like the LinkedIn test does.
    monkeypatch.setattr(
        "contents_hub.web.app._open_chromux",
        lambda url, *, session=None, confirmed=False: {
            "status": "opened", "url": url, "previous_state": "headless", "error": None,
        },
    )
    monkeypatch.setattr(
        "contents_hub.chromux.close_chromux_session",
        lambda session_id, **kwargs: subprocess.CompletedProcess(
            ["chromux", "close", session_id], 0
        ),
    )
    monkeypatch.setattr(
        "contents_hub.chromux.list_chromux_sessions", lambda profile=None: set()
    )

    resp = client.post(f"/subscriptions/{sub_id}/retry_validation")
    assert resp.status_code == 200
    assert resp.json()["status"] == "retrying"

    # After retry, the stub fetcher re-populates trial_result (ok, 0 items).
    store = SubscriptionStore(vault)
    sub = store.get_by_id(str(sub_id))
    assert sub.status == SubscriptionStatus.VALIDATING
    tr = sub.config["trial_result"]
    assert tr["ok"] is True
    assert tr["items"] == 0
    assert "trial_pre_recipe" not in sub.config
    assert tr["recipe_mode"] == "catalog"


def test_linkedin_retry_runs_trial_in_headed_mode(vault, client, monkeypatch):
    from contents_hub.chromux import is_foreground_fetch_allowed

    sub_id = _seed_validating_sub(
        vault,
        url="https://www.linkedin.com/in/example/",
        source_type="linkedin.profile",
        with_trial_result=True,
        trial_ok=False,
    )
    launches: list[dict] = []
    closed_sessions: list[str] = []

    def fake_open_chromux(url, *, session=None, confirmed=False):
        launches.append({"url": url, "session": session, "confirmed": confirmed})
        return {"status": "opened", "url": url, "previous_state": "headless", "error": None}

    def fake_close(session_id, **kwargs):
        closed_sessions.append(session_id)
        return subprocess.CompletedProcess(["chromux", "close", session_id], 0)

    async def assert_foreground_executor(*args, **kwargs):
        assert is_foreground_fetch_allowed() is True
        return _StubFetchResult()

    monkeypatch.setattr("contents_hub.web.app._open_chromux", fake_open_chromux)
    monkeypatch.setattr("contents_hub.executor.execute_trial", assert_foreground_executor)
    monkeypatch.setattr("contents_hub.chromux.close_chromux_session", fake_close)
    monkeypatch.setattr("contents_hub.chromux.list_chromux_sessions", lambda profile=None: set())

    resp = client.post(f"/subscriptions/{sub_id}/retry_validation")

    assert resp.status_code == 200
    assert resp.json()["status"] == "retrying"
    assert launches == [
        {
            "url": "https://www.linkedin.com/in/example",
            "session": f"trial-{sub_id}",
            "confirmed": True,
        }
    ]
    assert closed_sessions == [f"trial-{sub_id}"]


def test_retry_is_noop_for_active_sub(vault, client):
    store = SubscriptionStore(vault)
    store.add(url="https://example.com/", title="E", source_type="webpage")
    sub = store.get("https://example.com/")

    resp = client.post(f"/subscriptions/{sub.id}/retry_validation")
    assert resp.json()["status"] == "skipped"
