"""Persistence for user-request-driven feed explorations.

Explorations are separate lifecycle objects from subscriptions. Validation
attempts keep preview data and trace references; approved strategy versions and
manual runs are versioned so later workers can attach runtime and UI behavior
without overloading subscription recipe storage.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from contents_hub.chromux import (
    chromux_fetch_session_cleanup,
    chromux_profile_override,
    resolve_chromux_profile,
)
from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.item_key import item_key
from contents_hub.models import FetchedItem
from contents_hub.runners import AgentRunner, get_default_runner
from contents_hub.timeutil import now_iso


ARTIFACTS_DIRNAME = "exploration-artifacts"
DEFAULT_STRATEGY_TARGET_SURFACES = ("threads.feed", "threads.search")


def _now_iso() -> str:
    return now_iso()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@dataclass(frozen=True)
class Exploration:
    id: int
    display_name: str
    original_request: str
    target_surfaces: list[str]
    lens_ids: list[str]
    status: str
    approved_strategy_version_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ValidationAttempt:
    id: int
    exploration_id: int
    attempt_number: int
    status: str
    strategy_snapshot: dict[str, Any]
    process_summary: str
    raw_trace_artifact_path: str | None
    preview_items: list[dict[str, Any]]
    preview_lens_matches: list[dict[str, Any]]
    error: str
    chromux_session_ids: list[str]
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class StrategyVersion:
    id: int
    exploration_id: int
    version: int
    strategy_snapshot: dict[str, Any]
    validation_attempt_id: int | None
    approved_at: str


@dataclass(frozen=True)
class ExplorationRun:
    id: int
    exploration_id: int
    strategy_version_id: int
    status: str
    items_found: int
    items_inserted: int
    error: str
    raw_trace_artifact_path: str | None
    chromux_session_ids: list[str]
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class ExplorationRawItemWriteResult:
    inserted: int
    skipped: int
    total: int
    raw_item_ids: tuple[int, ...]
    events: tuple[dict[str, Any], ...] = ()


class ExplorationStrategyRunner:
    """Run already-registered exploration recipes through AgentRunner."""

    def __init__(
        self,
        config: WikiConfig,
        *,
        store: ExplorationStore | None = None,
        runner: AgentRunner | None = None,
    ):
        self.config = config
        self.store = store if store is not None else ExplorationStore(config)
        self.runner = runner

    async def run_registered(
        self,
        exploration_id: int,
        *,
        max_turns: int = 30,
        timeout: float = 600.0,
    ) -> ExplorationRun:
        exploration = self.store.get(exploration_id)
        if exploration.approved_strategy_version_id is None:
            raise ValueError("exploration must be registered before manual run")
        strategy = self.store.get_strategy_version(
            exploration.approved_strategy_version_id
        )
        runner = self.runner if self.runner is not None else get_default_runner()
        runtime = _strategy_runtime_config(strategy.strategy_snapshot)
        effective_timeout = _effective_timeout_seconds(timeout, runtime)
        target_items = _runtime_target_items(runtime)
        run = self.store.record_run(
            exploration_id=exploration.id,
            strategy_version_id=strategy.id,
            status="running",
            raw_trace={
                "orchestration": "autonomous_agent",
                "status": "running",
                "persist_events": [],
                "runtime": {
                    "timeout_seconds": effective_timeout,
                    "target_items": target_items,
                },
            },
        )
        prompt = _strategy_autonomous_prompt(
            exploration,
            strategy.strategy_snapshot,
            timeout_seconds=effective_timeout,
            target_items=target_items,
        )
        registry = _autonomous_tool_registry(
            config=self.config,
            store=self.store,
            exploration=exploration,
            strategy=strategy,
            run=run,
        )
        status = "succeeded"
        error = ""
        summary: dict[str, Any] = {}
        chromux_session_ids: list[str] = []
        chromux_sessions_seen: set[str] = set()
        from contents_hub.tools import get_default_registry, set_default_registry

        previous_registry = get_default_registry()
        set_default_registry(registry)
        try:
            chromux_profile = resolve_chromux_profile()
            with chromux_profile_override(chromux_profile):
                async with chromux_fetch_session_cleanup(
                    profile=chromux_profile,
                ) as chromux_sessions:
                    chromux_sessions_seen = chromux_sessions
                    response = await runner.run(
                        prompt,
                        max_turns=max_turns,
                        timeout=effective_timeout,
                    )
                chromux_session_ids = sorted(chromux_sessions)
            summary = _autonomous_summary_from_agent_response(response)
            error = _coerce_text(summary.get("error"))
            if error:
                status = "failed"
        except Exception as exc:  # noqa: BLE001 - run failure is recorded
            error = _run_error_message(
                exc,
                timeout=effective_timeout,
                phase="autonomous exploration",
            )
            stopped_reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else "error"
            summary = {"stopped_reason": stopped_reason}
            status = "failed"
        finally:
            if chromux_sessions_seen:
                chromux_session_ids = sorted(chromux_sessions_seen)
            set_default_registry(previous_registry)

        trace = self.store.get_run_trace(run.id)
        counts = _persist_event_counts(trace)
        if counts["accepted"] == 0 and not error:
            error = "autonomous agent persisted no items"
            status = "failed"
        if status == "failed" and counts["accepted"] > 0:
            status = "partial"
        trace["status"] = status
        trace["agent_summary"] = summary
        trace["persist_summary"] = counts
        chromux_session_ids = _unique_strings(
            [
                *chromux_session_ids,
                *_coerce_string_list(summary.get("chromux_session_ids")),
            ]
        )
        trace["chromux_session_ids"] = chromux_session_ids
        if error:
            trace["error"] = error
        return self.store.update_run_result(
            run.id,
            status=status,
            items_found=counts["processed"],
            items_inserted=counts["inserted"],
            error=error,
            raw_trace=trace,
            chromux_session_ids=chromux_session_ids,
            finished_at=_now_iso(),
        )


class ExplorationStore:
    """SQLite-backed storage for exploration draft/validation/run lifecycle."""

    def __init__(self, config: WikiConfig):
        self.config = config

    @property
    def artifact_dir(self) -> Path:
        return self.config.meta_path / ARTIFACTS_DIRNAME

    def create_draft(
        self,
        *,
        display_name: str,
        original_request: str,
        target_surfaces: list[str] | None = None,
        lens_ids: list[str] | None = None,
        status: str = "draft",
    ) -> Exploration:
        now = _now_iso()
        with get_db(self.config) as conn:
            cur = conn.execute(
                """INSERT INTO explorations
                   (display_name, original_request, target_surfaces, lens_ids,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    display_name,
                    original_request,
                    _json_dumps(target_surfaces or []),
                    _json_dumps(lens_ids or []),
                    status,
                    now,
                    now,
                ),
            )
            conn.commit()
            return self.get(cur.lastrowid, conn=conn)

    def create_registered_with_recipe(
        self,
        *,
        display_name: str,
        original_request: str,
        recipe_markdown: str,
        target_surfaces: list[str] | None = None,
        lens_ids: list[str] | None = None,
        approved_at: str | None = None,
    ) -> tuple[Exploration, StrategyVersion]:
        recipe = recipe_markdown.strip()
        if not recipe:
            raise ValueError("recipe Markdown or YAML is required")

        now = _now_iso()
        approved = approved_at or now
        recipe_yaml = _parse_recipe_yaml(recipe)
        strategy_snapshot = (
            {"recipe_yaml": recipe_yaml, "recipe_text": recipe}
            if recipe_yaml is not None
            else {"recipe_markdown": recipe}
        )
        resolved_target_surfaces = target_surfaces or _recipe_target_surfaces(
            recipe_yaml
        )
        with get_db(self.config) as conn:
            cur = conn.execute(
                """INSERT INTO explorations
                   (display_name, original_request, target_surfaces, lens_ids,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'registered', ?, ?)""",
                (
                    display_name,
                    original_request,
                    _json_dumps(resolved_target_surfaces),
                    _json_dumps(lens_ids or []),
                    now,
                    now,
                ),
            )
            exploration_id = int(cur.lastrowid)
            strategy_cur = conn.execute(
                """INSERT INTO exploration_strategy_versions
                   (exploration_id, version, strategy_snapshot,
                    validation_attempt_id, approved_at, created_at)
                   VALUES (?, 1, ?, NULL, ?, ?)""",
                (
                    exploration_id,
                    _json_dumps(strategy_snapshot),
                    approved,
                    now,
                ),
            )
            strategy_id = int(strategy_cur.lastrowid)
            conn.execute(
                """UPDATE explorations
                   SET approved_strategy_version_id = ?, updated_at = ?
                   WHERE id = ?""",
                (strategy_id, _now_iso(), exploration_id),
            )
            conn.commit()
            return (
                self.get(exploration_id, conn=conn),
                self.get_strategy_version(strategy_id, conn=conn),
            )

    def list_all(self, *, status: str | None = None) -> list[Exploration]:
        query = "SELECT * FROM explorations"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY updated_at DESC, id DESC"
        with get_db(self.config) as conn:
            return [_exploration_from_row(row) for row in conn.execute(query, params)]

    def get(
        self,
        exploration_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> Exploration:
        def _do(c: sqlite3.Connection) -> Exploration:
            row = c.execute(
                "SELECT * FROM explorations WHERE id = ?",
                (exploration_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"exploration not found: {exploration_id}")
            return _exploration_from_row(row)

        if conn is not None:
            return _do(conn)
        with get_db(self.config) as c:
            return _do(c)

    def record_validation_attempt(
        self,
        *,
        exploration_id: int,
        status: str,
        strategy_snapshot: dict[str, Any],
        process_summary: str = "",
        raw_trace: str | dict[str, Any] | list[Any] | None = None,
        preview_items: list[dict[str, Any]] | None = None,
        preview_lens_matches: list[dict[str, Any]] | None = None,
        error: str = "",
        chromux_session_ids: list[str] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> ValidationAttempt:
        now = _now_iso()
        started = started_at or now
        with get_db(self.config) as conn:
            attempt_number = _next_validation_attempt_number(conn, exploration_id)
            cur = conn.execute(
                """INSERT INTO exploration_validation_attempts
                   (exploration_id, attempt_number, status, strategy_snapshot,
                    process_summary, preview_items_json,
                    preview_lens_matches_json, error, chromux_session_ids,
                    started_at, finished_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    exploration_id,
                    attempt_number,
                    status,
                    _json_dumps(strategy_snapshot),
                    process_summary,
                    _json_dumps(preview_items or []),
                    _json_dumps(preview_lens_matches or []),
                    error,
                    _json_dumps(chromux_session_ids or []),
                    started,
                    finished_at,
                    now,
                    now,
                ),
            )
            attempt_id = cur.lastrowid
            artifact_path = self._write_trace_artifact(
                owner_type="validation",
                owner_id=attempt_id,
                payload=raw_trace,
            )
            if artifact_path is not None:
                conn.execute(
                    """UPDATE exploration_validation_attempts
                       SET raw_trace_artifact_path = ?, updated_at = ?
                       WHERE id = ?""",
                    (artifact_path, _now_iso(), attempt_id),
                )
            conn.execute(
                "UPDATE explorations SET updated_at = ? WHERE id = ?",
                (_now_iso(), exploration_id),
            )
            conn.commit()
            return self.get_validation_attempt(attempt_id, conn=conn)

    def get_validation_attempt(
        self,
        attempt_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> ValidationAttempt:
        def _do(c: sqlite3.Connection) -> ValidationAttempt:
            row = c.execute(
                "SELECT * FROM exploration_validation_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"validation attempt not found: {attempt_id}")
            return _validation_attempt_from_row(row)

        if conn is not None:
            return _do(conn)
        with get_db(self.config) as c:
            return _do(c)

    def latest_validation_attempt(
        self,
        exploration_id: int,
        *,
        status: str | None = None,
    ) -> ValidationAttempt | None:
        query = (
            "SELECT * FROM exploration_validation_attempts "
            "WHERE exploration_id = ?"
        )
        params: list[Any] = [exploration_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY attempt_number DESC, id DESC LIMIT 1"
        with get_db(self.config) as conn:
            row = conn.execute(query, tuple(params)).fetchone()
            return _validation_attempt_from_row(row) if row is not None else None

    def approve_strategy(
        self,
        *,
        exploration_id: int,
        validation_attempt_id: int,
        approved_at: str | None = None,
    ) -> StrategyVersion:
        now = _now_iso()
        approved = approved_at or now
        with get_db(self.config) as conn:
            attempt = conn.execute(
                """SELECT strategy_snapshot, status
                   FROM exploration_validation_attempts
                   WHERE id = ? AND exploration_id = ?""",
                (validation_attempt_id, exploration_id),
            ).fetchone()
            if attempt is None:
                raise KeyError(
                    "validation attempt not found for exploration: "
                    f"{validation_attempt_id}"
                )
            if attempt["status"] != "succeeded":
                raise ValueError("only succeeded validation attempts can be approved")
            version = _next_strategy_version(conn, exploration_id)
            cur = conn.execute(
                """INSERT INTO exploration_strategy_versions
                   (exploration_id, version, strategy_snapshot,
                    validation_attempt_id, approved_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    exploration_id,
                    version,
                    attempt["strategy_snapshot"],
                    validation_attempt_id,
                    approved,
                    now,
                ),
            )
            strategy_id = cur.lastrowid
            conn.execute(
                """UPDATE explorations
                   SET status = 'registered',
                       approved_strategy_version_id = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (strategy_id, _now_iso(), exploration_id),
            )
            conn.commit()
            return self.get_strategy_version(strategy_id, conn=conn)

    def get_strategy_version(
        self,
        strategy_version_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> StrategyVersion:
        def _do(c: sqlite3.Connection) -> StrategyVersion:
            row = c.execute(
                "SELECT * FROM exploration_strategy_versions WHERE id = ?",
                (strategy_version_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"strategy version not found: {strategy_version_id}")
            return _strategy_version_from_row(row)

        if conn is not None:
            return _do(conn)
        with get_db(self.config) as c:
            return _do(c)

    def record_run(
        self,
        *,
        exploration_id: int,
        strategy_version_id: int,
        status: str,
        items_found: int = 0,
        items_inserted: int = 0,
        error: str = "",
        raw_trace: str | dict[str, Any] | list[Any] | None = None,
        chromux_session_ids: list[str] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> ExplorationRun:
        now = _now_iso()
        started = started_at or now
        with get_db(self.config) as conn:
            if not _strategy_belongs_to_exploration(
                conn,
                exploration_id=exploration_id,
                strategy_version_id=strategy_version_id,
            ):
                raise ValueError("strategy version does not belong to exploration")
            cur = conn.execute(
                """INSERT INTO exploration_runs
                   (exploration_id, strategy_version_id, status, items_found,
                    items_inserted, error, chromux_session_ids, started_at,
                    finished_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    exploration_id,
                    strategy_version_id,
                    status,
                    items_found,
                    items_inserted,
                    error,
                    _json_dumps(chromux_session_ids or []),
                    started,
                    finished_at,
                    now,
                    now,
                ),
            )
            run_id = cur.lastrowid
            artifact_path = self._write_trace_artifact(
                owner_type="run",
                owner_id=run_id,
                payload=raw_trace,
            )
            if artifact_path is not None:
                conn.execute(
                    """UPDATE exploration_runs
                       SET raw_trace_artifact_path = ?, updated_at = ?
                       WHERE id = ?""",
                    (artifact_path, _now_iso(), run_id),
                )
            conn.execute(
                "UPDATE explorations SET updated_at = ? WHERE id = ?",
                (_now_iso(), exploration_id),
            )
            conn.commit()
            return self.get_run(run_id, conn=conn)

    def persist_run_items(
        self,
        *,
        exploration_id: int,
        run_id: int,
        items: Iterable[FetchedItem],
    ) -> ExplorationRawItemWriteResult:
        """Persist registered exploration-run items and attribution.

        Validation previews never call this path. A normalized URL is first
        looked up globally so an exploration discovery can attach to an
        existing subscription-owned raw item without creating a second review
        candidate or changing the existing review state.
        """
        items_list = list(items)
        now = _now_iso()
        inserted = 0
        skipped = 0
        raw_item_ids: list[int] = []
        events: list[dict[str, Any]] = []

        with get_db(self.config) as conn:
            run_row = conn.execute(
                """SELECT r.id, r.started_at, r.strategy_version_id,
                          r.items_found, r.items_inserted,
                          e.id AS exploration_id, e.display_name,
                          sv.version AS strategy_version
                   FROM exploration_runs r
                   JOIN explorations e ON e.id = r.exploration_id
                   JOIN exploration_strategy_versions sv
                     ON sv.id = r.strategy_version_id
                   WHERE r.id = ? AND r.exploration_id = ?""",
                (run_id, exploration_id),
            ).fetchone()
            if run_row is None:
                raise KeyError(f"exploration run not found: {run_id}")

            owner_label = _display_label(
                run_row["display_name"],
                int(run_row["exploration_id"]),
            )
            for item in items_list:
                if not isinstance(item, FetchedItem):
                    skipped += 1
                    events.append(
                        {
                            "status": "rejected",
                            "reason": "invalid_item_type",
                            "raw_item_id": None,
                            "url": "",
                        }
                    )
                    continue
                key = item_key(item, None)
                if not key:
                    skipped += 1
                    events.append(
                        {
                            "status": "rejected",
                            "reason": "missing_url",
                            "raw_item_id": None,
                            "url": item.url,
                        }
                    )
                    continue
                raw_item_id, was_inserted = _find_or_insert_exploration_raw_item(
                    conn,
                    item=item,
                    key=key,
                    collected_at=now,
                )
                inserted += 1 if was_inserted else 0
                skipped += 0 if was_inserted else 1
                raw_item_ids.append(raw_item_id)
                item_extra = item.extra if isinstance(item.extra, dict) else {}
                events.append(
                    {
                        "status": "inserted" if was_inserted else "skipped",
                        "reason": "" if was_inserted else "duplicate_url",
                        "raw_item_id": raw_item_id,
                        "url": key,
                        "title": item.title or "",
                        "source_surface": str(item_extra.get("source_surface") or ""),
                        "selection_reason": str(item_extra.get("selection_reason") or ""),
                        "content_status": str(item_extra.get("content_status") or ""),
                    }
                )
                _record_exploration_discovery(
                    conn,
                    raw_item_id=raw_item_id,
                    exploration_id=int(run_row["exploration_id"]),
                    run_id=int(run_row["id"]),
                    owner_label=owner_label,
                    run_at=run_row["started_at"],
                    strategy_version=int(run_row["strategy_version"]),
                    discovered_at=now,
                )

            conn.execute(
                """UPDATE exploration_runs
                   SET items_found = ?, items_inserted = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    int(run_row["items_found"] or 0) + len(items_list),
                    int(run_row["items_inserted"] or 0) + inserted,
                    _now_iso(),
                    run_id,
                ),
            )
            conn.execute(
                "UPDATE explorations SET updated_at = ? WHERE id = ?",
                (_now_iso(), exploration_id),
            )
            conn.commit()

        return ExplorationRawItemWriteResult(
            inserted=inserted,
            skipped=skipped,
            total=inserted + skipped,
            raw_item_ids=tuple(raw_item_ids),
            events=tuple(events),
        )

    def get_run_trace(self, run_id: int) -> dict[str, Any]:
        with get_db(self.config) as conn:
            row = conn.execute(
                "SELECT raw_trace_artifact_path FROM exploration_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"exploration run not found: {run_id}")
            payload = self._read_trace_artifact(row["raw_trace_artifact_path"])
        return payload if isinstance(payload, dict) else {"trace": payload}

    def append_run_trace_events(
        self,
        run_id: int,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with get_db(self.config) as conn:
            row = conn.execute(
                """SELECT exploration_id, raw_trace_artifact_path
                   FROM exploration_runs WHERE id = ?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"exploration run not found: {run_id}")
            payload = self._read_trace_artifact(row["raw_trace_artifact_path"])
            if not isinstance(payload, dict):
                payload = {"trace": payload}
            payload.setdefault("orchestration", "autonomous_agent")
            persist_events = payload.setdefault("persist_events", [])
            if not isinstance(persist_events, list):
                persist_events = []
                payload["persist_events"] = persist_events
            persist_events.extend(events)
            artifact_path = self._write_trace_artifact(
                owner_type="run",
                owner_id=run_id,
                payload=payload,
            )
            if artifact_path is not None:
                self._delete_trace_artifact(row["raw_trace_artifact_path"])
                conn.execute(
                    """UPDATE exploration_runs
                       SET raw_trace_artifact_path = ?, updated_at = ?
                       WHERE id = ?""",
                    (artifact_path, _now_iso(), run_id),
                )
            conn.execute(
                "UPDATE explorations SET updated_at = ? WHERE id = ?",
                (_now_iso(), row["exploration_id"]),
            )
            conn.commit()
            return payload

    def update_run_result(
        self,
        run_id: int,
        *,
        status: str,
        items_found: int,
        items_inserted: int,
        error: str = "",
        raw_trace: str | dict[str, Any] | list[Any] | None = None,
        chromux_session_ids: list[str] | None = None,
        finished_at: str | None = None,
    ) -> ExplorationRun:
        with get_db(self.config) as conn:
            row = conn.execute(
                """SELECT exploration_id, raw_trace_artifact_path
                   FROM exploration_runs WHERE id = ?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"exploration run not found: {run_id}")
            artifact_path = self._write_trace_artifact(
                owner_type="run",
                owner_id=run_id,
                payload=raw_trace,
            )
            if artifact_path is not None:
                self._delete_trace_artifact(row["raw_trace_artifact_path"])
            conn.execute(
                """UPDATE exploration_runs
                   SET status = ?,
                       items_found = ?,
                       items_inserted = ?,
                       error = ?,
                       raw_trace_artifact_path = COALESCE(?, raw_trace_artifact_path),
                       chromux_session_ids = ?,
                       finished_at = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (
                    status,
                    items_found,
                    items_inserted,
                    error,
                    artifact_path,
                    _json_dumps(chromux_session_ids or []),
                    finished_at,
                    _now_iso(),
                    run_id,
                ),
            )
            conn.execute(
                "UPDATE explorations SET updated_at = ? WHERE id = ?",
                (_now_iso(), row["exploration_id"]),
            )
            conn.commit()
            return self.get_run(run_id, conn=conn)

    def get_run(
        self,
        run_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> ExplorationRun:
        def _do(c: sqlite3.Connection) -> ExplorationRun:
            row = c.execute(
                "SELECT * FROM exploration_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"exploration run not found: {run_id}")
            return _run_from_row(row)

        if conn is not None:
            return _do(conn)
        with get_db(self.config) as c:
            return _do(c)

    def update_run_counts(
        self,
        run_id: int,
        *,
        items_inserted: int,
    ) -> ExplorationRun:
        with get_db(self.config) as conn:
            conn.execute(
                """UPDATE exploration_runs
                   SET items_inserted = ?, updated_at = ?
                   WHERE id = ?""",
                (items_inserted, _now_iso(), run_id),
            )
            conn.commit()
            return self.get_run(run_id, conn=conn)

    def delete_validation_attempt(self, attempt_id: int) -> bool:
        with get_db(self.config) as conn:
            row = conn.execute(
                """SELECT raw_trace_artifact_path
                   FROM exploration_validation_attempts WHERE id = ?""",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return False
            self._delete_trace_artifact(row["raw_trace_artifact_path"])
            conn.execute(
                "DELETE FROM exploration_validation_attempts WHERE id = ?",
                (attempt_id,),
            )
            conn.commit()
            return True

    def delete_run(self, run_id: int) -> bool:
        with get_db(self.config) as conn:
            row = conn.execute(
                "SELECT raw_trace_artifact_path FROM exploration_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            self._delete_trace_artifact(row["raw_trace_artifact_path"])
            conn.execute("DELETE FROM exploration_runs WHERE id = ?", (run_id,))
            conn.commit()
            return True

    def delete_exploration(self, exploration_id: int) -> bool:
        with get_db(self.config) as conn:
            exploration = conn.execute(
                "SELECT display_name FROM explorations WHERE id = ?",
                (exploration_id,),
            ).fetchone()
            if exploration is None:
                return False
            tombstone_label = _display_label(
                exploration["display_name"],
                exploration_id,
            )
            for row in conn.execute(
                """SELECT raw_trace_artifact_path
                   FROM exploration_validation_attempts
                   WHERE exploration_id = ?""",
                (exploration_id,),
            ):
                self._delete_trace_artifact(row["raw_trace_artifact_path"])
            for row in conn.execute(
                """SELECT raw_trace_artifact_path
                   FROM exploration_runs
                   WHERE exploration_id = ?""",
                (exploration_id,),
            ):
                self._delete_trace_artifact(row["raw_trace_artifact_path"])
            conn.execute(
                """UPDATE explorations
                   SET approved_strategy_version_id = NULL
                   WHERE id = ?""",
                (exploration_id,),
            )
            conn.execute(
                """UPDATE raw_item_discoveries
                   SET owner_id = NULL,
                       owner_label = '',
                       deleted_at = COALESCE(deleted_at, ?),
                       deleted_owner_id = ?,
                       deleted_owner_label = ?
                   WHERE exploration_id = ?
                     AND owner_type = 'exploration_run'
                     AND deleted_at IS NULL""",
                (_now_iso(), exploration_id, tombstone_label, exploration_id),
            )
            conn.execute(
                "DELETE FROM exploration_runs WHERE exploration_id = ?",
                (exploration_id,),
            )
            conn.execute(
                """DELETE FROM exploration_validation_attempts
                   WHERE exploration_id = ?""",
                (exploration_id,),
            )
            conn.execute(
                "DELETE FROM exploration_strategy_versions WHERE exploration_id = ?",
                (exploration_id,),
            )
            conn.execute("DELETE FROM explorations WHERE id = ?", (exploration_id,))
            conn.commit()
            return True

    def _write_trace_artifact(
        self,
        *,
        owner_type: str,
        owner_id: int,
        payload: str | dict[str, Any] | list[Any] | None,
    ) -> str | None:
        if payload is None:
            return None
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{owner_type}-{owner_id}-{uuid.uuid4().hex}.json"
        path = self.artifact_dir / filename
        if isinstance(payload, str):
            body = payload
        else:
            body = _json_dumps(payload)
        path.write_text(body, encoding="utf-8")
        return f"{ARTIFACTS_DIRNAME}/{filename}"

    def _read_trace_artifact(self, artifact_ref: str | None) -> Any:
        if not artifact_ref:
            return {}
        base = self.artifact_dir.resolve()
        path = (self.config.meta_path / artifact_ref).resolve()
        if path.parent != base:
            return {}
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    def _delete_trace_artifact(self, artifact_ref: str | None) -> None:
        if not artifact_ref:
            return
        base = self.artifact_dir.resolve()
        path = (self.config.meta_path / artifact_ref).resolve()
        if path.parent != base:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            return


def _strategy_autonomous_prompt(
    exploration: Exploration,
    strategy_snapshot: dict[str, Any],
    *,
    timeout_seconds: float,
    target_items: int | None,
) -> str:
    recipe_markdown = _strategy_recipe_markdown(strategy_snapshot)
    target_block = (
        f"Target item budget: about {target_items} qualifying items.\n"
        if target_items is not None
        else "Target item budget: use the recipe's qualitative stop rule.\n"
    )
    return (
        "Run this approved feed exploration once as a single autonomous mission. "
        "The harness will not run harvest, enrichment, checkpoint, recipe steps, "
        "or fanout phases for you. You own browsing, judgment, and any useful "
        "delegation or parallel subagent work.\n\n"
        "Persistence boundary:\n"
        "- Save qualifying items immediately with the contents-hub tool "
        "`persist_exploration_raw`.\n"
        "- Do not use append_checkpoint or write JSONL checkpoints.\n"
        "- Do not claim an item is saved unless the persistence tool accepted it.\n"
        "- Every saved item must include url, title or summary, source_surface, "
        "selection_reason, and content_status.\n\n"
        "Browser and retrieval policy:\n"
        "- Prefer contents-hub chromux/browser tools for pages where visible state, "
        "login, scrolling, or interaction matters.\n"
        "- Use fetch_url for public RSS, JSON APIs, and static pages; it returns "
        "compact feed items, JSON previews, or readable page text by default. "
        "Use mode='raw' only for small pages where the original body is truly needed.\n"
        "- WebFetch and WebSearch are not available in this runtime; if browsing is "
        "blocked, save any already accepted items and report the blocker.\n\n"
        f"This run will be cancelled after {timeout_seconds:g} seconds.\n"
        f"{target_block}"
        f"Exploration: {exploration.display_name}\n"
        f"Request: {exploration.original_request}\n"
        f"Lens ids: {_json_dumps(exploration.lens_ids)}\n"
        f"Surface hints: {_json_dumps(exploration.target_surfaces)}\n\n"
        "Approved mission recipe:\n"
        f"{recipe_markdown}\n\n"
        "Return ONLY a compact JSON object. Do not include the saved items again; "
        "the persistence tool is the source of truth:\n"
        "{\n"
        '  "summary": "what you did",\n'
        '  "sources_attempted": ["surface or URL"],\n'
        '  "stopped_reason": "complete|timeout|blocked|error",\n'
        '  "items_saved_estimate": 0,\n'
        '  "chromux_session_ids": [],\n'
        '  "notes": [],\n'
        '  "error": ""\n'
        "}\n"
    )


def _strategy_runtime_config(strategy_snapshot: dict[str, Any]) -> dict[str, Any]:
    recipe_yaml = strategy_snapshot.get("recipe_yaml")
    if not isinstance(recipe_yaml, dict):
        parsed = _parse_recipe_yaml(_strategy_recipe_markdown(strategy_snapshot))
        recipe_yaml = parsed if parsed is not None else {}
    runtime = recipe_yaml.get("runtime") if isinstance(recipe_yaml, dict) else None
    return dict(runtime) if isinstance(runtime, dict) else {}


def _effective_timeout_seconds(cli_timeout: float, runtime: dict[str, Any]) -> float:
    max_minutes = runtime.get("max_minutes")
    try:
        recipe_timeout = float(max_minutes) * 60
    except (TypeError, ValueError):
        recipe_timeout = 0.0
    if recipe_timeout <= 0:
        return cli_timeout
    if cli_timeout <= 0:
        return recipe_timeout
    return min(cli_timeout, recipe_timeout)


def _runtime_target_items(runtime: dict[str, Any]) -> int | None:
    if "target_items" not in runtime:
        return None
    try:
        return _bounded_int(runtime.get("target_items"), 10, 1, 100)
    except (TypeError, ValueError):
        return None


def _autonomous_summary_from_agent_response(text: str) -> dict[str, Any]:
    parsed = _extract_json_object(text)
    if not parsed:
        return {
            "summary": (text or "").strip()[:1000],
            "sources_attempted": [],
            "stopped_reason": "complete",
            "notes": [],
            "error": "",
        }
    return {
        "summary": _coerce_text(parsed.get("summary")),
        "sources_attempted": _coerce_string_list(parsed.get("sources_attempted")),
        "stopped_reason": _coerce_text(parsed.get("stopped_reason")) or "complete",
        "items_saved_estimate": parsed.get("items_saved_estimate"),
        "chromux_session_ids": _coerce_string_list(parsed.get("chromux_session_ids")),
        "notes": parsed.get("notes") if isinstance(parsed.get("notes"), list) else [],
        "error": _coerce_text(parsed.get("error")),
    }


def _persist_event_counts(trace: dict[str, Any]) -> dict[str, int]:
    events = trace.get("persist_events")
    if not isinstance(events, list):
        events = []
    inserted = sum(1 for event in events if event.get("status") == "inserted")
    skipped = sum(1 for event in events if event.get("status") == "skipped")
    rejected = sum(1 for event in events if event.get("status") == "rejected")
    accepted = inserted + skipped
    return {
        "processed": accepted + rejected,
        "accepted": accepted,
        "inserted": inserted,
        "skipped": skipped,
        "rejected": rejected,
    }


def _autonomous_tool_registry(
    *,
    config: WikiConfig,
    store: ExplorationStore,
    exploration: Exploration,
    strategy: StrategyVersion,
    run: ExplorationRun,
):
    from contents_hub.tools import ToolRegistry, ToolSpec
    from contents_hub.tools import browser, fetchers

    registry = ToolRegistry()
    registry.register(fetchers.get_spec())
    registry.register(browser.chromux_navigate)
    registry.register(browser.chromux_extract)
    registry.register(browser.chromux_scroll)
    registry.register(browser.chromux_scroll_extract)
    registry.register(
        ToolSpec(
            name="persist_exploration_raw",
            description=(
                "Persist qualifying items for the current autonomous exploration "
                "run. Owner ids are injected by contents-hub and cannot be "
                "provided by the agent."
            ),
            input_schema=_PERSIST_EXPLORATION_RAW_SCHEMA,
            handler=_make_persist_exploration_raw_handler(
                config=config,
                store=store,
                exploration=exploration,
                strategy=strategy,
                run=run,
            ),
        )
    )
    return registry


_PERSIST_EXPLORATION_RAW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "content_html": {"type": "string"},
                    "author": {"type": "string"},
                    "published_at": {"type": "string"},
                    "source_surface": {"type": "string"},
                    "selection_reason": {"type": "string"},
                    "content_status": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "extra": {"type": "object"},
                },
                "required": [
                    "url",
                    "source_surface",
                    "selection_reason",
                    "content_status",
                ],
            },
        }
    },
    "required": ["items"],
}


def _make_persist_exploration_raw_handler(
    *,
    config: WikiConfig,
    store: ExplorationStore,
    exploration: Exploration,
    strategy: StrategyVersion,
    run: ExplorationRun,
):
    async def _handler(**kwargs: Any) -> str:
        raw_items = kwargs.get("items")
        if not isinstance(raw_items, list):
            event = _rejected_persist_event(
                reason="missing_items_array",
                url="",
                index=0,
            )
            store.append_run_trace_events(run.id, [event])
            return _json_dumps(
                {
                    "ok": False,
                    "inserted": 0,
                    "skipped": 0,
                    "rejected": 1,
                    "events": [event],
                    "error": "items must be an array",
                }
            )

        items: list[FetchedItem] = []
        events: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_items):
            item, reject_reason = _fetched_item_from_autonomous_payload(raw)
            if reject_reason:
                events.append(
                    _rejected_persist_event(
                        reason=reject_reason,
                        url=str(raw.get("url") or "") if isinstance(raw, dict) else "",
                        index=index,
                    )
                )
                continue
            if item is not None:
                items.append(item)

        result = store.persist_run_items(
            exploration_id=exploration.id,
            run_id=run.id,
            items=items,
        )
        persisted_events = [
            dict(
                event,
                timestamp=_now_iso(),
                tool="persist_exploration_raw",
                strategy_version_id=strategy.id,
            )
            for event in result.events
        ]
        all_events = [*events, *persisted_events]
        if all_events:
            store.append_run_trace_events(run.id, all_events)
        if result.raw_item_ids:
            from contents_hub.lenses import evaluate_exploration_run_lenses

            await evaluate_exploration_run_lenses(
                config,
                exploration.id,
                result.raw_item_ids,
            )
        return _json_dumps(
            {
                "ok": True,
                "inserted": result.inserted,
                "skipped": result.skipped,
                "rejected": len(events),
                "raw_item_ids": list(result.raw_item_ids),
                "events": all_events,
            }
        )

    return _handler


def _rejected_persist_event(*, reason: str, url: str, index: int) -> dict[str, Any]:
    return {
        "timestamp": _now_iso(),
        "tool": "persist_exploration_raw",
        "status": "rejected",
        "reason": reason,
        "raw_item_id": None,
        "url": url,
        "item_index": index,
    }


def _fetched_item_from_autonomous_payload(
    raw: Any,
) -> tuple[FetchedItem | None, str]:
    if not isinstance(raw, dict):
        return None, "invalid_item_type"
    url = _coerce_text(raw.get("url"))
    title = _coerce_text(raw.get("title"))
    summary = _coerce_text(raw.get("summary"))
    source_surface = _coerce_text(raw.get("source_surface"))
    selection_reason = _coerce_text(raw.get("selection_reason"))
    content_status = _coerce_text(raw.get("content_status"))
    if not url:
        return None, "missing_url"
    if not (title or summary):
        return None, "missing_title_or_summary"
    if not source_surface:
        return None, "missing_source_surface"
    if not selection_reason:
        return None, "missing_selection_reason"
    if not content_status:
        return None, "missing_content_status"

    explicit_extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    passthrough_extra = {
        key: value
        for key, value in raw.items()
        if key
        not in {
            "url",
            "title",
            "summary",
            "author",
            "published_at",
            "tags",
            "content_html",
            "extra",
        }
    }
    return (
        FetchedItem(
            url=url,
            title=title or url,
            summary=summary,
            author=_coerce_text(raw.get("author")),
            published_at=_parse_datetime(raw.get("published_at")),
            tags=list(raw.get("tags") or []),
            content_html=_coerce_text(raw.get("content_html")),
            source_type="exploration",
            extra={**passthrough_extra, **explicit_extra},
        ),
        "",
    )


def _unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _strategy_recipe_markdown(strategy_snapshot: dict[str, Any]) -> str:
    recipe_text = _coerce_text(strategy_snapshot.get("recipe_text"))
    if recipe_text:
        return _clean_recipe_text(recipe_text)
    recipe_yaml = strategy_snapshot.get("recipe_yaml")
    if isinstance(recipe_yaml, dict) and recipe_yaml:
        return yaml.safe_dump(
            recipe_yaml,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).strip()
    for key in ("recipe_markdown", "strategy_markdown", "markdown"):
        recipe_markdown = _coerce_text(strategy_snapshot.get(key))
        if recipe_markdown:
            return _clean_strategy_markdown(recipe_markdown)
    return _legacy_strategy_recipe_markdown(strategy_snapshot)


def _parse_recipe_yaml(recipe: str) -> dict[str, Any] | None:
    text = _clean_recipe_text(recipe)
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(value, dict):
        return None
    if not any(
        key in value
        for key in (
            "goal",
            "focus",
            "keep",
            "save",
            "skip",
            "avoid",
            "hints",
            "sources",
            "surfaces",
            "steps",
            "runtime",
        )
    ):
        return None
    return dict(value)


def _clean_recipe_text(text: str) -> str:
    stripped = _coerce_text(text)
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    opener = lines[0].strip().lower()
    if opener not in {
        "```",
        "```yaml",
        "```yml",
        "```md",
        "```markdown",
    }:
        return stripped
    if lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _clean_strategy_markdown(text: str) -> str:
    stripped = _coerce_text(text)
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    opener = lines[0].strip().lower()
    if opener not in {"```", "```md", "```markdown"}:
        return stripped
    if lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _recipe_target_surfaces(recipe_yaml: dict[str, Any] | None) -> list[str]:
    surfaces: list[str] = []
    seen: set[str] = set()
    if not isinstance(recipe_yaml, dict):
        return surfaces

    def _add(raw: Any) -> None:
        surface = _coerce_text(raw)
        if surface and surface not in seen:
            seen.add(surface)
            surfaces.append(surface)

    for key in ("target_surfaces", "sources", "surfaces"):
        raw = recipe_yaml.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _add(item.get("surface") or item.get("name") or item.get("site"))
                else:
                    _add(item)
        elif isinstance(raw, dict):
            for surface, value in raw.items():
                if isinstance(value, dict):
                    _add(value.get("surface") or surface)
                else:
                    _add(surface)

    if not surfaces:
        raw_steps = recipe_yaml.get("steps")
        if isinstance(raw_steps, list):
            for step in raw_steps:
                if isinstance(step, dict):
                    _add(step.get("surface") or step.get("name") or step.get("site"))
                    fanout = step.get("fanout")
                    if isinstance(fanout, list):
                        for item in fanout:
                            if isinstance(item, dict):
                                _add(
                                    item.get("surface")
                                    or item.get("name")
                                    or item.get("site")
                                )
                            else:
                                _add(item)
    return surfaces


def _legacy_strategy_recipe_markdown(
    strategy_snapshot: dict[str, Any],
    *,
    exploration: Exploration | None = None,
) -> str:
    snapshot = {
        key: value
        for key, value in strategy_snapshot.items()
        if key not in {"recipe_base", "recipe_id", "recipe_version"}
    }
    sections: list[str] = ["# Exploration Strategy"]

    goal = ""
    if exploration is not None:
        goal = exploration.original_request
    if goal:
        sections.append(f"## Goal\n\n{goal}")

    surfaces = _coerce_string_list(snapshot.get("target_surfaces"))
    if not surfaces and exploration is not None:
        surfaces = list(exploration.target_surfaces)
    if not surfaces:
        surfaces = list(DEFAULT_STRATEGY_TARGET_SURFACES)
    sections.append("## Surfaces\n\n" + "\n".join(f"- {surface}" for surface in surfaces))

    section_map = (
        ("collection_approach", "## Search And Navigation"),
        ("candidate_selection", "## Candidate Rules"),
        ("extraction_approach", "## Harvest And Enrichment"),
        ("lens_alignment_notes", "## Lens Alignment"),
    )
    consumed = {"target_surfaces", "stop_limits"}
    for key, title in section_map:
        consumed.add(key)
        value = _coerce_text(snapshot.get(key))
        if value:
            sections.append(f"{title}\n\n{value}")

    stop_limits = snapshot.get("stop_limits")
    if isinstance(stop_limits, dict) and stop_limits:
        lines = [
            f"- {key.replace('_', ' ')}: {value}"
            for key, value in stop_limits.items()
            if value not in (None, "")
        ]
        if lines:
            sections.append("## Run Boundaries\n\n" + "\n".join(lines))

    notes = {
        key: value
        for key, value in snapshot.items()
        if key not in consumed and value not in (None, "", [], {})
    }
    if notes:
        sections.append(
            "## Notes\n\n"
            "Legacy strategy fields not covered above:\n\n"
            "```json\n"
            f"{_json_dumps(notes)}\n"
            "```"
        )

    return "\n\n".join(sections).strip()


def _run_error_message(exc: Exception, *, timeout: float, phase: str = "manual run") -> str:
    if isinstance(exc, asyncio.TimeoutError) or not str(exc):
        return f"{phase} timed out after {timeout:g}s"
    return str(exc)


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = (text or "").strip()
    if not stripped:
        return {}
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _bounded_int(raw: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _coerce_text(raw: Any) -> str:
    return str(raw or "").strip()


def _coerce_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _parse_datetime(raw: Any):
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _next_validation_attempt_number(
    conn: sqlite3.Connection,
    exploration_id: int,
) -> int:
    row = conn.execute(
        """SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_attempt
           FROM exploration_validation_attempts
           WHERE exploration_id = ?""",
        (exploration_id,),
    ).fetchone()
    return int(row["next_attempt"])


def _next_strategy_version(conn: sqlite3.Connection, exploration_id: int) -> int:
    row = conn.execute(
        """SELECT COALESCE(MAX(version), 0) + 1 AS next_version
           FROM exploration_strategy_versions
           WHERE exploration_id = ?""",
        (exploration_id,),
    ).fetchone()
    return int(row["next_version"])


def _strategy_belongs_to_exploration(
    conn: sqlite3.Connection,
    *,
    exploration_id: int,
    strategy_version_id: int,
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM exploration_strategy_versions
           WHERE id = ? AND exploration_id = ?""",
        (strategy_version_id, exploration_id),
    ).fetchone()
    return row is not None


def _display_label(display_name: str | None, exploration_id: int) -> str:
    label = (display_name or "").strip()
    return label if label else f"Exploration {exploration_id}"


def _item_body(item: FetchedItem) -> str:
    return (item.content_html or item.summary or "")[:20000]


def _item_summary(item: FetchedItem) -> str:
    return (item.summary or "")[:500]


def _item_published_at(item: FetchedItem) -> str | None:
    if item.published_at is None:
        return None
    if hasattr(item.published_at, "isoformat"):
        return item.published_at.isoformat()
    return str(item.published_at)


def _item_metadata_json(item: FetchedItem) -> str:
    metadata = item.extra if isinstance(item.extra, dict) else {}
    if not metadata:
        return "{}"
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)


def _find_or_insert_exploration_raw_item(
    conn: sqlite3.Connection,
    *,
    item: FetchedItem,
    key: str,
    collected_at: str,
) -> tuple[int, bool]:
    existing = conn.execute(
        """SELECT id FROM raw_items
           WHERE url = ?
           ORDER BY id LIMIT 1""",
        (key,),
    ).fetchone()
    if existing is not None:
        return int(existing["id"]), False

    cur = conn.execute(
        """INSERT INTO raw_items
           (url, title, body, origin, priority, status, subscription_id,
            content_summary, metadata_json, published_at, collected_at, updated_at)
           VALUES (?, ?, ?, 'exploration', 50, 'raw', NULL, ?, ?, ?, ?, ?)""",
        (
            key,
            item.title or "",
            _item_body(item),
            _item_summary(item),
            _item_metadata_json(item),
            _item_published_at(item),
            collected_at,
            collected_at,
        ),
    )
    return int(cur.lastrowid), True


def _record_exploration_discovery(
    conn: sqlite3.Connection,
    *,
    raw_item_id: int,
    exploration_id: int,
    run_id: int,
    owner_label: str,
    run_at: str,
    strategy_version: int,
    discovered_at: str,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO raw_item_discoveries
           (raw_item_id, owner_type, owner_id, owner_label, owner_run_id,
            exploration_id, strategy_version, run_at, discovered_at, created_at)
           VALUES (?, 'exploration_run', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            raw_item_id,
            run_id,
            owner_label,
            run_id,
            exploration_id,
            strategy_version,
            run_at,
            discovered_at,
            discovered_at,
        ),
    )


def _exploration_from_row(row: sqlite3.Row) -> Exploration:
    return Exploration(
        id=int(row["id"]),
        display_name=row["display_name"],
        original_request=row["original_request"],
        target_surfaces=list(_json_loads(row["target_surfaces"], [])),
        lens_ids=list(_json_loads(row["lens_ids"], [])),
        status=row["status"],
        approved_strategy_version_id=row["approved_strategy_version_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validation_attempt_from_row(row: sqlite3.Row) -> ValidationAttempt:
    return ValidationAttempt(
        id=int(row["id"]),
        exploration_id=int(row["exploration_id"]),
        attempt_number=int(row["attempt_number"]),
        status=row["status"],
        strategy_snapshot=dict(_json_loads(row["strategy_snapshot"], {})),
        process_summary=row["process_summary"],
        raw_trace_artifact_path=row["raw_trace_artifact_path"],
        preview_items=list(_json_loads(row["preview_items_json"], [])),
        preview_lens_matches=list(_json_loads(row["preview_lens_matches_json"], [])),
        error=row["error"],
        chromux_session_ids=list(_json_loads(row["chromux_session_ids"], [])),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _strategy_version_from_row(row: sqlite3.Row) -> StrategyVersion:
    return StrategyVersion(
        id=int(row["id"]),
        exploration_id=int(row["exploration_id"]),
        version=int(row["version"]),
        strategy_snapshot=dict(_json_loads(row["strategy_snapshot"], {})),
        validation_attempt_id=row["validation_attempt_id"],
        approved_at=row["approved_at"],
    )


def _run_from_row(row: sqlite3.Row) -> ExplorationRun:
    return ExplorationRun(
        id=int(row["id"]),
        exploration_id=int(row["exploration_id"]),
        strategy_version_id=int(row["strategy_version_id"]),
        status=row["status"],
        items_found=int(row["items_found"]),
        items_inserted=int(row["items_inserted"]),
        error=row["error"],
        raw_trace_artifact_path=row["raw_trace_artifact_path"],
        chromux_session_ids=list(_json_loads(row["chromux_session_ids"], [])),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
