from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from datetime import datetime, timezone

import pytest

from contents_hub.config import WikiConfig
from contents_hub.db import SCHEMA_VERSION, get_db, init_db
from contents_hub import explorations as explorations_module
from contents_hub.explorations import (
    ARTIFACTS_DIRNAME,
    ExplorationStore,
    ExplorationStrategyRunner,
)
from contents_hub.lens_inbox import query_lens_inbox
from contents_hub.lenses import (
    evaluate_exploration_preview_lenses,
    evaluate_exploration_run_lenses,
)
from contents_hub.models import FetchedItem
from contents_hub.runners import get_default_runner, set_default_runner
from contents_hub.tools import get_default_registry
from contents_hub.tools.storage import persist_raw_sync


@pytest.fixture
def vault(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()
    return cfg


def test_exploration_schema_is_separate_from_subscriptions(vault):
    store = ExplorationStore(vault)

    exploration = store.create_draft(
        display_name="AI builders on Threads",
        original_request="Find thoughtful Threads posts from indie AI builders",
        target_surfaces=["threads.search"],
        lens_ids=["ai"],
    )

    with get_db(vault) as conn:
        sub_count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        row = conn.execute(
            """SELECT display_name, original_request, target_surfaces, lens_ids,
                      status, approved_strategy_version_id
               FROM explorations WHERE id = ?""",
            (exploration.id,),
        ).fetchone()

    assert sub_count == 0
    assert row["display_name"] == "AI builders on Threads"
    assert row["original_request"] == (
        "Find thoughtful Threads posts from indie AI builders"
    )
    assert row["target_surfaces"] == '["threads.search"]'
    assert row["lens_ids"] == '["ai"]'
    assert row["status"] == "draft"
    assert row["approved_strategy_version_id"] is None


class _SequenceRunner:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def run(self, prompt, *, max_turns=30, timeout=600.0):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("runner called more times than expected")
        return self.responses.pop(0)


async def _persist_exploration_items(items: list[dict]) -> None:
    spec = get_default_registry().get("persist_exploration_raw")
    assert spec is not None
    await spec.handler(items=items)


def _seed_lens(
    cfg: WikiConfig,
    lens_id: str,
    *,
    keywords: list[str] | None = None,
    enabled: bool = True,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db(cfg) as conn:
        conn.execute(
            """INSERT INTO lenses
               (id, name, description, keywords, enabled, created_at, updated_at)
               VALUES (?, ?, '', ?, ?, ?, ?)""",
            (
                lens_id,
                lens_id,
                json.dumps(keywords or []),
                1 if enabled else 0,
                now,
                now,
            ),
        )
        conn.commit()


def test_validation_attempts_keep_history_and_file_backed_trace(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Frontend leads",
        original_request="Find senior frontend engineers discussing agents",
        target_surfaces=["threads.feed", "linkedin.search"],
        lens_ids=["frontend"],
    )

    first = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="failed",
        strategy_snapshot={"surface": "threads.feed", "limit": 3},
        process_summary="Opened feed and found no matching posts.",
        raw_trace={"steps": ["open", "scroll"]},
        preview_items=[],
        preview_lens_matches=[],
        error="no candidates",
        chromux_session_ids=["cx-1"],
        finished_at="2026-05-14T00:00:01+00:00",
    )
    second = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"surface": "linkedin.search", "limit": 5},
        process_summary="Searched LinkedIn and sampled relevant posts.",
        raw_trace="raw action transcript",
        preview_items=[{"url": "https://example.test/post", "title": "Post"}],
        preview_lens_matches=[{"url": "https://example.test/post", "lens": "frontend"}],
        chromux_session_ids=["cx-2", "cx-3"],
        finished_at="2026-05-14T00:01:00+00:00",
    )

    assert first.attempt_number == 1
    assert second.attempt_number == 2
    assert second.strategy_snapshot["surface"] == "linkedin.search"
    assert second.preview_items == [
        {"url": "https://example.test/post", "title": "Post"}
    ]
    assert second.preview_lens_matches == [
        {"url": "https://example.test/post", "lens": "frontend"}
    ]
    assert second.chromux_session_ids == ["cx-2", "cx-3"]
    assert second.raw_trace_artifact_path is not None
    assert second.raw_trace_artifact_path.startswith(f"{ARTIFACTS_DIRNAME}/")
    assert (vault.meta_path / second.raw_trace_artifact_path).read_text(
        encoding="utf-8"
    ) == "raw action transcript"

    with get_db(vault) as conn:
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM exploration_validation_attempts
                   WHERE exploration_id = ?""",
                (exploration.id,),
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM raw_item_lenses").fetchone()[0] == 0


def test_autonomous_prompt_rejects_builtin_web_fallbacks(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Threads browser proof",
        original_request="Find recent vibe coding tips on Threads",
        target_surfaces=["threads.feed", "threads.search"],
    )
    strategy = {"stop_limits": {"max_items": 3, "max_scrolls": 2}}

    prompt = explorations_module._strategy_autonomous_prompt(
        exploration,
        strategy,
        timeout_seconds=300,
        target_items=3,
    )

    assert "chromux" in prompt
    assert "WebFetch" in prompt
    assert "WebSearch" in prompt
    assert "not available" in prompt
    assert "single autonomous mission" in prompt


def test_exploration_preview_lens_matches_are_artifact_only(vault):
    _seed_lens(vault, "ai", keywords=["agent"])
    _seed_lens(vault, "disabled", keywords=["agent"], enabled=False)
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Agent builders",
        original_request="Find posts about agent builders",
        target_surfaces=["threads.search"],
        lens_ids=["ai", "disabled", "missing"],
    )
    runner = _SequenceRunner(
        [
            json.dumps(
                {
                    "items": [
                        {
                            "id": 1,
                            "summary": "Agent builder launch note.",
                            "bullets": ["Mentions agent workflows."],
                        }
                    ]
                }
            )
        ]
    )

    original = get_default_runner()
    try:
        set_default_runner(runner)  # type: ignore[arg-type]
        matches = asyncio.run(
            evaluate_exploration_preview_lenses(
                vault,
                exploration.id,
                [
                    {
                        "url": "https://threads.test/post/1",
                        "title": "Agent builder note",
                        "summary": "A useful agent workflow update.",
                    }
                ],
            )
        )
    finally:
        set_default_runner(original)

    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "agent builders"},
        preview_items=[{"url": "https://threads.test/post/1"}],
        preview_lens_matches=matches,
    )

    assert len(runner.prompts) == 1
    assert attempt.preview_lens_matches == [
        {
            "candidate_index": 0,
            "url": "https://threads.test/post/1",
            "title": "Agent builder note",
            "lens_id": "ai",
            "summary": "Agent builder launch note.",
            "bullets": ["Mentions agent workflows."],
        }
    ]
    with get_db(vault) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM raw_item_lenses").fetchone()[0] == 0


def test_approved_strategy_versions_are_append_only_and_runs_reference_version(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Agent infra",
        original_request="Find posts about agent infrastructure",
        target_surfaces=["threads.search"],
        lens_ids=["ai"],
    )
    attempt_one = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "agent infra", "limit": 3},
    )
    version_one = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt_one.id,
    )
    attempt_two = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "agent infra OR browser agents", "limit": 5},
    )
    version_two = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt_two.id,
    )
    run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=version_two.id,
        status="ok",
        items_found=4,
        items_inserted=3,
        raw_trace={"actions": ["search", "extract"]},
        chromux_session_ids=["run-cx"],
        finished_at="2026-05-14T00:02:00+00:00",
    )

    updated = store.get(exploration.id)
    assert version_one.version == 1
    assert version_two.version == 2
    assert updated.status == "registered"
    assert updated.approved_strategy_version_id == version_two.id
    assert run.strategy_version_id == version_two.id
    assert run.items_found == 4
    assert run.items_inserted == 3
    assert run.chromux_session_ids == ["run-cx"]
    assert run.raw_trace_artifact_path is not None
    assert (vault.meta_path / run.raw_trace_artifact_path).exists()

    with pytest.raises(ValueError):
        other = store.create_draft(
            display_name="Other",
            original_request="Find other posts",
        )
        store.record_run(
            exploration_id=other.id,
            strategy_version_id=version_two.id,
            status="ok",
        )


def test_trace_artifacts_are_deleted_with_attempt_run_or_exploration(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Cleanup",
        original_request="Find cleanup posts",
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "cleanup"},
        raw_trace="validation trace",
    )
    version = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt.id,
    )
    run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=version.id,
        status="ok",
        raw_trace="run trace",
    )
    attempt_path = vault.meta_path / attempt.raw_trace_artifact_path
    run_path = vault.meta_path / run.raw_trace_artifact_path

    assert attempt_path.exists()
    assert run_path.exists()
    assert store.delete_validation_attempt(attempt.id) is True
    assert not attempt_path.exists()
    assert run_path.exists()
    assert store.delete_run(run.id) is True
    assert not run_path.exists()

    new_attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "cleanup v2"},
        raw_trace="another validation trace",
    )
    new_version = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=new_attempt.id,
    )
    new_run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=new_version.id,
        status="ok",
        raw_trace="another run trace",
    )
    new_path = vault.meta_path / new_attempt.raw_trace_artifact_path
    new_run_path = vault.meta_path / new_run.raw_trace_artifact_path
    assert new_path.exists()
    assert new_run_path.exists()
    assert store.delete_exploration(exploration.id) is True
    assert not new_path.exists()
    assert not new_run_path.exists()


def test_exploration_run_items_converge_on_existing_subscription_raw_item(vault):
    store = ExplorationStore(vault)
    now = datetime.now(timezone.utc).isoformat()
    with get_db(vault) as conn:
        conn.execute(
            """INSERT INTO subscriptions
               (url, title, source_type, status, schedule_interval_minutes,
                default_lens_ids, config, created_at, updated_at)
               VALUES ('https://source.test/', 'Source', 'webpage', 'active',
                       60, '[]', '{}', ?, ?)""",
            (now, now),
        )
        sub_id = conn.execute("SELECT id FROM subscriptions").fetchone()[0]
        conn.execute(
            """INSERT INTO raw_items
               (url, title, body, origin, priority, status, subscription_id,
                content_summary, collected_at, updated_at)
               VALUES ('https://example.test/post', 'Existing', 'Keep body',
                       'subscription', 50, 'raw', ?, 'Keep summary', ?, ?)""",
            (sub_id, now, now),
        )
        raw_item_id = conn.execute("SELECT id FROM raw_items").fetchone()[0]
        conn.execute(
            """INSERT INTO lenses (id, name, description, keywords, enabled,
                                  created_at, updated_at)
               VALUES ('ai', 'AI', '', '[]', 1, ?, ?)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO raw_item_lenses
               (raw_item_id, lens_id, summary, bullets_json)
               VALUES (?, 'ai', 'Relevant', '["agent"]')""",
            (raw_item_id,),
        )
        conn.commit()

    exploration = store.create_draft(
        display_name="AI builders",
        original_request="Find AI builder posts",
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "ai builders"},
    )
    strategy = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt.id,
    )
    run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=strategy.id,
        status="ok",
    )

    result = store.persist_run_items(
        exploration_id=exploration.id,
        run_id=run.id,
        items=[
            FetchedItem(
                url="https://example.test/post?utm_source=threads#frag",
                title="Duplicate",
                summary="New summary",
                content_html="New body",
            )
        ],
    )

    assert result.inserted == 0
    assert result.skipped == 1
    assert result.raw_item_ids == (raw_item_id,)
    with get_db(vault) as conn:
        rows = conn.execute("SELECT * FROM raw_items").fetchall()
        assert len(rows) == 1
        assert rows[0]["origin"] == "subscription"
        assert rows[0]["body"] == "Keep body"
        discoveries = conn.execute(
            """SELECT owner_type, owner_id, owner_label, owner_run_id,
                      exploration_id, strategy_version, deleted_at
               FROM raw_item_discoveries
               WHERE raw_item_id = ?""",
            (raw_item_id,),
        ).fetchall()
        assert [d["owner_type"] for d in discoveries] == ["exploration_run"]
        assert discoveries[0]["owner_id"] == run.id
        assert discoveries[0]["owner_label"] == "AI builders"
        assert discoveries[0]["owner_run_id"] == run.id
        assert discoveries[0]["exploration_id"] == exploration.id
        assert discoveries[0]["strategy_version"] == strategy.version
        assert discoveries[0]["deleted_at"] is None

        inbox = query_lens_inbox(conn, sources_dirname="sources", status="raw")
        assert inbox["candidate_count"] == 1
        assert inbox["candidates"][0].id == raw_item_id


def test_exploration_run_items_store_exploration_origin_without_subscription(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Research leads",
        original_request="Find research leads",
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "research"},
    )
    strategy = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt.id,
    )
    run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=strategy.id,
        status="ok",
    )

    result = store.persist_run_items(
        exploration_id=exploration.id,
        run_id=run.id,
        items=[
            FetchedItem(
                url="https://research.test/item",
                title="Research item",
                summary="Summary",
                content_html="Body",
            )
        ],
    )

    assert result.inserted == 1
    with get_db(vault) as conn:
        row = conn.execute("SELECT * FROM raw_items").fetchone()
        assert row["origin"] == "exploration"
        assert row["subscription_id"] is None
        attribution = conn.execute(
            "SELECT owner_type, owner_id FROM raw_item_discoveries"
        ).fetchone()
    assert attribution["owner_type"] == "exploration_run"
    assert attribution["owner_id"] == run.id


def test_registered_exploration_manual_run_persists_tool_items_on_timeout(vault):
    class _ToolTimeoutRunner:
        def __init__(self):
            self.prompts: list[str] = []

        async def run(self, prompt, *, max_turns=30, timeout=600.0):
            self.prompts.append(prompt)
            await _persist_exploration_items(
                [
                    {
                        "url": "https://threads.test/checkpoint/1",
                        "title": "Tool item",
                        "summary": "Accepted before timeout.",
                        "source_surface": "threads.search",
                        "selection_reason": "Concrete implementation detail.",
                        "content_status": "detail_enriched",
                        "visible_metrics": {"likes": 10, "comments": 2},
                        "top_comments": [
                            {
                                "author": "commenter",
                                "text": "Use a typed API contract.",
                                "likes": 3,
                            }
                        ],
                    }
                ]
            )
            raise asyncio.TimeoutError()

    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Checkpoint run",
        original_request="Find useful implementation posts",
        target_surfaces=["threads.search"],
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "implementation posts"},
    )
    store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt.id,
    )
    runner = _ToolTimeoutRunner()

    run = asyncio.run(
        ExplorationStrategyRunner(vault, runner=runner).run_registered(
            exploration.id,
            timeout=1,
        )
    )

    assert run.status == "partial"
    assert run.items_found == 1
    assert run.items_inserted == 1
    assert run.error == "autonomous exploration timed out after 1s"
    assert "persist_exploration_raw" in runner.prompts[0]
    assert "Do not use append_checkpoint" in runner.prompts[0]
    with get_db(vault) as conn:
        raw_item = conn.execute(
            "SELECT title, url, metadata_json FROM raw_items"
        ).fetchone()
        discovery = conn.execute(
            "SELECT owner_type, owner_run_id FROM raw_item_discoveries"
        ).fetchone()
    assert raw_item["title"] == "Tool item"
    assert raw_item["url"] == "https://threads.test/checkpoint/1"
    metadata = json.loads(raw_item["metadata_json"])
    assert metadata["source_surface"] == "threads.search"
    assert metadata["visible_metrics"]["likes"] == 10
    assert metadata["top_comments"][0]["text"] == "Use a typed API contract."
    assert discovery["owner_type"] == "exploration_run"
    assert discovery["owner_run_id"] == run.id


def test_manual_run_duplicate_tool_call_records_skip_event(vault):
    class _DuplicateToolRunner:
        def __init__(self):
            self.prompts: list[str] = []

        async def run(self, prompt, *, max_turns=30, timeout=600.0):
            self.prompts.append(prompt)
            item = {
                "url": "https://threads.test/list-first/1",
                "title": "Visible card title",
                "summary": "Visible card only.",
                "source_surface": "threads.search",
                "selection_reason": "Useful visible implementation note.",
                "content_status": "list_preview",
                "rank": 1,
            }
            await _persist_exploration_items([item])
            await _persist_exploration_items([dict(item, title="Duplicate card")])
            return json.dumps(
                {
                    "summary": "saved then deduped",
                    "sources_attempted": ["threads.search"],
                    "stopped_reason": "complete",
                    "chromux_session_ids": [],
                    "error": "",
                }
            )

    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="List first run",
        original_request="Find useful implementation posts",
        target_surfaces=["threads.search"],
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "implementation posts"},
    )
    store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt.id,
    )
    runner = _DuplicateToolRunner()

    run = asyncio.run(
        ExplorationStrategyRunner(vault, runner=runner).run_registered(
            exploration.id,
            timeout=300,
        )
    )

    assert run.status == "succeeded"
    assert run.items_found == 2
    assert run.items_inserted == 1
    assert "single autonomous mission" in runner.prompts[0]
    with get_db(vault) as conn:
        raw_item = conn.execute(
            "SELECT title, body, content_summary FROM raw_items"
        ).fetchone()
        trace_path = conn.execute(
            "SELECT raw_trace_artifact_path FROM exploration_runs WHERE id = ?",
            (run.id,),
        ).fetchone()["raw_trace_artifact_path"]
    trace = json.loads((vault.meta_path / trace_path).read_text(encoding="utf-8"))
    assert raw_item["title"] == "Visible card title"
    assert raw_item["content_summary"] == "Visible card only."
    assert [event["status"] for event in trace["persist_events"]] == [
        "inserted",
        "skipped",
    ]


def test_autonomous_run_closes_and_records_new_chromux_sessions(vault, monkeypatch):
    class _ChromuxSessionRunner:
        async def run(self, prompt, *, max_turns=30, timeout=600.0):
            await _persist_exploration_items(
                [
                    {
                        "url": "https://threads.test/chromux-cleanup",
                        "title": "Cleanup candidate",
                        "summary": "Persisted while browser tab was open.",
                        "source_surface": "threads.search",
                        "selection_reason": "Useful implementation detail.",
                        "content_status": "list_preview",
                    }
                ]
            )
            return json.dumps(
                {
                    "summary": "saved through browser",
                    "sources_attempted": ["threads.search"],
                    "stopped_reason": "complete",
                    "chromux_session_ids": [],
                    "error": "",
                }
            )

    snapshots = iter(
        [
            {"existing-tab"},
            {"existing-tab", "bash-opened-tab"},
        ]
    )
    closed_sessions: list[str] = []

    monkeypatch.setattr(
        explorations_module,
        "resolve_chromux_profile",
        lambda profile=None: profile or "contents-hub",
    )
    monkeypatch.setattr(
        "contents_hub.chromux.resolve_chromux_profile",
        lambda profile=None: profile or "contents-hub",
    )
    monkeypatch.setattr(
        "contents_hub.chromux.list_chromux_sessions",
        lambda profile=None: set(next(snapshots)),
    )

    def fake_close(session_id, **kwargs):
        closed_sessions.append(session_id)
        return subprocess.CompletedProcess(["chromux", "close", session_id], 0)

    monkeypatch.setattr("contents_hub.chromux.close_chromux_session", fake_close)

    store = ExplorationStore(vault)
    exploration, _strategy = store.create_registered_with_recipe(
        display_name="Cleanup run",
        original_request="Find useful implementation posts",
        recipe_markdown="goal: collect useful implementation posts",
    )

    run = asyncio.run(
        ExplorationStrategyRunner(vault, runner=_ChromuxSessionRunner()).run_registered(
            exploration.id,
            timeout=300,
        )
    )

    assert run.status == "succeeded"
    assert run.chromux_session_ids == ["bash-opened-tab"]
    assert closed_sessions == ["bash-opened-tab"]
    trace = json.loads(
        (vault.meta_path / str(run.raw_trace_artifact_path)).read_text(encoding="utf-8")
    )
    assert trace["chromux_session_ids"] == ["bash-opened-tab"]


def test_autonomous_tool_registry_is_narrow(vault):
    store = ExplorationStore(vault)
    exploration, strategy = store.create_registered_with_recipe(
        display_name="Narrow tool run",
        original_request="Find useful implementation posts",
        recipe_markdown="goal: collect useful implementation posts",
    )
    run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=strategy.id,
        status="running",
    )

    registry = explorations_module._autonomous_tool_registry(
        config=vault,
        store=store,
        exploration=exploration,
        strategy=strategy,
        run=run,
    )

    assert set(registry.list()) == {
        "fetch_url",
        "chromux_navigate",
        "chromux_extract",
        "chromux_scroll",
        "chromux_scroll_extract",
        "persist_exploration_raw",
    }


def test_legacy_recipe_workflow_fields_are_context_only(vault):
    class _AutonomousLegacyRecipeRunner:
        def __init__(self):
            self.prompts: list[str] = []

        async def run(self, prompt, *, max_turns=30, timeout=600.0):
            self.prompts.append(prompt)
            if "Run Phase 2 of this approved feed exploration once" in prompt:
                raise AssertionError("enrich should be skipped after harvest error")
            if "Run one fanout item" in prompt:
                raise AssertionError("legacy fanout should not execute")
            await _persist_exploration_items(
                [
                    {
                        "url": "https://news.test/partial",
                        "title": "Partial direct item",
                        "summary": "Persisted before timeout.",
                        "source_surface": "news",
                        "selection_reason": "Matches the legacy recipe goal.",
                        "content_status": "list_preview",
                    }
                ]
            )
            raise asyncio.TimeoutError()

    store = ExplorationStore(vault)
    exploration, _ = store.create_registered_with_recipe(
        display_name="Partial recipe run",
        original_request="Find news and social tips",
        recipe_markdown="""
goal: Find useful items.
steps:
  - name: news
    fanout:
      - surface: news
        search: AI news
        limit: 2
""".strip(),
    )
    runner = _AutonomousLegacyRecipeRunner()

    run = asyncio.run(
        ExplorationStrategyRunner(vault, runner=runner).run_registered(
            exploration.id,
            timeout=300,
        )
    )

    assert run.status == "partial"
    assert run.items_found == 1
    assert run.items_inserted == 1
    assert run.error == "autonomous exploration timed out after 300s"
    assert len(runner.prompts) == 1
    with get_db(vault) as conn:
        raw_item = conn.execute("SELECT title, url FROM raw_items").fetchone()
        trace_path = conn.execute(
            "SELECT raw_trace_artifact_path FROM exploration_runs WHERE id = ?",
            (run.id,),
        ).fetchone()["raw_trace_artifact_path"]
    trace = json.loads((vault.meta_path / trace_path).read_text(encoding="utf-8"))
    assert raw_item["title"] == "Partial direct item"
    assert raw_item["url"] == "https://news.test/partial"
    assert trace["orchestration"] == "autonomous_agent"
    assert trace["persist_summary"]["inserted"] == 1


def test_exploration_run_lenses_write_lens_inbox_metadata_shape(vault):
    _seed_lens(vault, "ai", keywords=["agent"])
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Agent research",
        original_request="Find agent research posts",
        lens_ids=["ai"],
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "agent research"},
    )
    strategy = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt.id,
    )
    run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=strategy.id,
        status="ok",
    )
    result = store.persist_run_items(
        exploration_id=exploration.id,
        run_id=run.id,
        items=[
            FetchedItem(
                url="https://research.test/agent",
                title="Agent research item",
                summary="Agent systems research summary.",
                content_html="Agent systems research body.",
            )
        ],
    )
    runner = _SequenceRunner(
        [
            json.dumps(
                {
                    "items": [
                        {
                            "id": result.raw_item_ids[0],
                            "summary": "Agent systems research summary.",
                            "bullets": ["Preserves Lens-specific metadata."],
                        }
                    ]
                }
            )
        ]
    )

    original = get_default_runner()
    try:
        set_default_runner(runner)  # type: ignore[arg-type]
        inserted = asyncio.run(
            evaluate_exploration_run_lenses(
                vault,
                exploration.id,
                result.raw_item_ids,
            )
        )
    finally:
        set_default_runner(original)

    assert inserted == 1
    with get_db(vault) as conn:
        row = conn.execute(
            """SELECT raw_item_id, lens_id, summary, bullets_json
               FROM raw_item_lenses"""
        ).fetchone()
        assert row["raw_item_id"] == result.raw_item_ids[0]
        assert row["lens_id"] == "ai"
        assert row["summary"] == "Agent systems research summary."
        assert json.loads(row["bullets_json"]) == [
            "Preserves Lens-specific metadata."
        ]
        inbox = query_lens_inbox(conn, sources_dirname="sources", status="raw")
        assert inbox["candidate_count"] == 1
        candidate = inbox["candidates"][0]
        assert candidate.subscription_id is None
        assert candidate.lenses[0].id == "ai"
        assert candidate.lenses[0].summary == "Agent systems research summary."
        assert candidate.lenses[0].bullets == (
            "Preserves Lens-specific metadata.",
        )


def test_deleting_exploration_preserves_raw_items_and_tombstones_attribution(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Public label",
        original_request="Sensitive full request that must not be tombstoned",
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="succeeded",
        strategy_snapshot={"query": "sensitive"},
    )
    strategy = store.approve_strategy(
        exploration_id=exploration.id,
        validation_attempt_id=attempt.id,
    )
    run = store.record_run(
        exploration_id=exploration.id,
        strategy_version_id=strategy.id,
        status="ok",
    )
    result = store.persist_run_items(
        exploration_id=exploration.id,
        run_id=run.id,
        items=[
            FetchedItem(
                url="https://audit.test/item",
                title="Audit item",
                summary="Summary",
                content_html="Body",
            )
        ],
    )
    raw_item_id = result.raw_item_ids[0]
    with get_db(vault) as conn:
        conn.execute(
            "UPDATE raw_items SET status = 'promoted', body = 'Preserved' WHERE id = ?",
            (raw_item_id,),
        )
        conn.commit()

    assert store.delete_exploration(exploration.id) is True

    with get_db(vault) as conn:
        raw = conn.execute("SELECT status, body FROM raw_items WHERE id = ?", (raw_item_id,)).fetchone()
        assert raw["status"] == "promoted"
        assert raw["body"] == "Preserved"
        tombstone = conn.execute(
            """SELECT owner_type, owner_id, owner_label, owner_run_id,
                      deleted_owner_id, deleted_owner_label, deleted_at,
                      discovered_at, run_at, strategy_version
               FROM raw_item_discoveries
               WHERE raw_item_id = ?""",
            (raw_item_id,),
        ).fetchone()
        assert tombstone["owner_type"] == "exploration_run"
        assert tombstone["owner_id"] is None
        assert tombstone["owner_label"] == ""
        assert tombstone["owner_run_id"] == run.id
        assert tombstone["deleted_owner_id"] == exploration.id
        assert tombstone["deleted_owner_label"] == "Public label"
        assert tombstone["deleted_at"] is not None
        assert tombstone["discovered_at"]
        assert tombstone["run_at"]
        assert tombstone["strategy_version"] == strategy.version
        assert "Sensitive full request" not in dict(tombstone).values()


def test_create_registered_with_recipe_writes_strategy_without_validation(vault):
    store = ExplorationStore(vault)

    exploration, strategy = store.create_registered_with_recipe(
        display_name="Threads operators",
        original_request="Find operator posts about AI workflow design",
        recipe_markdown="\n# Goal\n\nFind operator posts.\n",
        target_surfaces=["threads.search"],
        lens_ids=["ops"],
    )

    assert exploration.status == "registered"
    assert exploration.approved_strategy_version_id == strategy.id
    assert strategy.version == 1
    assert strategy.validation_attempt_id is None
    assert strategy.strategy_snapshot == {"recipe_markdown": "# Goal\n\nFind operator posts."}
    with get_db(vault) as conn:
        assert conn.execute("SELECT COUNT(*) FROM exploration_validation_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 0


def test_subscription_persistence_records_discovery_attribution(vault):
    now = datetime.now(timezone.utc)
    with get_db(vault) as conn:
        conn.execute(
            """INSERT INTO subscriptions
               (url, title, source_type, status, schedule_interval_minutes,
                default_lens_ids, config, created_at, updated_at)
               VALUES ('https://source.test/', 'Source', 'webpage', 'active',
                       60, '[]', '{}', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        sub_id = conn.execute("SELECT id FROM subscriptions").fetchone()[0]
        summary = persist_raw_sync(
            conn,
            [
                FetchedItem(
                    url="https://source.test/a",
                    title="A",
                    summary="Summary",
                    published_at=now,
                    extra={
                        "content_status": "detail_enriched",
                        "outbound_urls": ["https://docs.test/contract"],
                        "visible_metrics": {"comments": 4},
                    },
                )
            ],
            sub_id,
        )
        duplicate = persist_raw_sync(
            conn,
            [
                FetchedItem(
                    url="https://source.test/a?utm_campaign=x",
                    title="A again",
                    summary="Summary",
                    published_at=now,
                )
            ],
            sub_id,
        )
        conn.commit()

        assert summary["inserted"] == 1
        assert duplicate["skipped"] == 1
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
        raw_metadata = json.loads(
            conn.execute("SELECT metadata_json FROM raw_items").fetchone()[0]
        )
        discovery_count = conn.execute(
            "SELECT COUNT(*) FROM raw_item_discoveries"
        ).fetchone()[0]
        assert raw_count == 1
        assert raw_metadata["content_status"] == "detail_enriched"
        assert raw_metadata["outbound_urls"] == ["https://docs.test/contract"]
        assert raw_metadata["visible_metrics"]["comments"] == 4
        assert discovery_count == 1


def test_v8_database_migrates_to_exploration_schema(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)
    conn = init_db(cfg)
    conn.execute("UPDATE schema_version SET version = 8")
    conn.execute("DROP TABLE explorations")
    conn.execute("DROP TABLE exploration_validation_attempts")
    conn.execute("DROP TABLE exploration_strategy_versions")
    conn.execute("DROP TABLE exploration_runs")
    conn.commit()
    conn.close()

    migrated = init_db(cfg)
    try:
        assert (
            migrated.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        )
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "explorations",
            "exploration_validation_attempts",
            "exploration_strategy_versions",
            "exploration_runs",
            "raw_item_discoveries",
        }.issubset(tables)
        raw_item_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(raw_items)")
        }
        assert "metadata_json" in raw_item_columns
    finally:
        migrated.close()


def test_deleting_missing_or_unsafe_artifact_reference_is_noop(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Unsafe",
        original_request="Find unsafe path cases",
    )
    attempt = store.record_validation_attempt(
        exploration_id=exploration.id,
        status="failed",
        strategy_snapshot={},
        raw_trace="trace",
    )

    with get_db(vault) as conn:
        conn.execute(
            """UPDATE exploration_validation_attempts
               SET raw_trace_artifact_path = '../outside.json'
               WHERE id = ?""",
            (attempt.id,),
        )
        conn.commit()

    assert store.delete_validation_attempt(attempt.id) is True


def test_schema_requires_run_strategy_version(vault):
    store = ExplorationStore(vault)
    exploration = store.create_draft(
        display_name="Bad run",
        original_request="Find bad run cases",
    )

    with pytest.raises(sqlite3.IntegrityError):
        with get_db(vault) as conn:
            conn.execute(
                """INSERT INTO exploration_runs
                   (exploration_id, strategy_version_id, status, started_at,
                    created_at, updated_at)
                   VALUES (?, ?, 'running', 'now', 'now', 'now')""",
                (exploration.id, 999),
            )
