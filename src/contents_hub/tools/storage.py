"""Storage tool — `persist_raw` ToolSpec handler.

Writes a batch of :class:`~contents_hub.models.FetchedItem` rows to the
``raw_items`` SQLite table.

Contract guardrails (from contracts.md / requirements.md):

- Subscription dedup is enforced by the existing ``UNIQUE(subscription_id,
  url)`` constraint on ``raw_items``.  This module relies on ``INSERT OR
  IGNORE`` so duplicates collapse silently.
- Discovery provenance is recorded in ``raw_item_discoveries`` without changing
  subscription-owned raw item body/status review state.
- R-T6.3: the auxiliary tables (``schedule_runs``, ``job_runs``,
  ``schedules``, ``fetch_cursors``) are not touched here — they remain
  the responsibility of ``daemon.py`` / ``scheduler``.  ``persist_raw`` is
  raw-items-only.
- R-U3.3: optimistic concurrency.  We deliberately do NOT acquire any
  table-level lock and do NOT swallow ``sqlite3.OperationalError``.  If
  two processes collide on a write, the losing process raises naturally
  (callers surface that as an exit-code-1 traceback to stderr).

The handler is exposed as a :class:`~contents_hub.tools.base.ToolSpec` so the
agent can persist results from inside the executor flow, and is also
importable as a regular Python coroutine for direct use by ``executor.py``
and ``api.py`` once those land.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, TypedDict

from contents_hub.item_key import item_key
from contents_hub.models import FetchedItem
from contents_hub.timeutil import now_iso
from contents_hub.tools.base import ToolSpec

logger = logging.getLogger(__name__)


class PersistenceResult(TypedDict):
    """Result returned by raw-item persistence helpers.

    Kept dict-shaped for the existing public caller contract while making
    the new ``inserted_ids`` field explicit for post-commit follow-up work.
    """

    inserted: int
    skipped: int
    total: int
    inserted_ids: list[int]


# ---------------------------------------------------------------------------
# Direct (non-tool) Python entry point
# ---------------------------------------------------------------------------


def _to_iso(value: Any) -> str | None:
    """Coerce a published_at value (datetime / str / None) to ISO 8601."""
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _metadata_json(item: FetchedItem) -> str:
    metadata = item.extra if isinstance(item.extra, dict) else {}
    if not metadata:
        return "{}"
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)


def _row_payload(item: FetchedItem, sub_id: int) -> tuple[Any, ...]:
    """Build the parameter tuple for the raw_items INSERT statement.

    Mirrors the columns/order used by ``daemon._insert_raw_item`` so the
    refactor is behavior-preserving.
    """
    key = item_key(item, sub_id)
    title = item.title or ""

    # Body precedence: content_html > summary. Cap at 20k chars (matches
    # daemon._insert_raw_item).
    body = (item.content_html or item.summary or "")[:20000]

    summary_preview = (item.summary or "")[:500]
    metadata_json = _metadata_json(item)
    pa_iso = _to_iso(item.published_at)
    now_str = now_iso()

    return (
        key,
        title,
        body,
        sub_id,
        summary_preview,
        metadata_json,
        pa_iso,
        now_str,
        now_str,
    )


def _record_subscription_discovery(
    conn: sqlite3.Connection,
    *,
    raw_item_id: int,
    subscription_id: int,
    discovered_at: str,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO raw_item_discoveries
           (raw_item_id, owner_type, owner_id, owner_label, discovered_at,
            created_at)
           VALUES (?, 'subscription', ?, ?, ?, ?)""",
        (
            raw_item_id,
            subscription_id,
            f"Subscription {subscription_id}",
            discovered_at,
            discovered_at,
        ),
    )


def persist_raw_sync(
    conn: sqlite3.Connection,
    items: list[FetchedItem],
    sub_id: int,
) -> PersistenceResult:
    """Synchronous workhorse — write ``items`` to ``raw_items``.

    Uses ``INSERT OR IGNORE`` so existing rows (matched by the
    ``UNIQUE(subscription_id, url)`` constraint) are skipped silently.

    No transaction handling here; the caller controls commit boundaries.
    No exception suppression — ``sqlite3.OperationalError`` from a write
    collision propagates by design (R-U3.3).

    Returns a small summary dict::

        {"inserted": N, "skipped": M, "total": N+M, "inserted_ids": [...]}
    """
    inserted = 0
    skipped = 0
    inserted_ids: list[int] = []

    sql = (
        "INSERT OR IGNORE INTO raw_items "
        "(url, title, body, origin, priority, status, subscription_id, "
        " content_summary, metadata_json, published_at, collected_at, updated_at) "
        "VALUES (?, ?, ?, 'subscription', 50, 'raw', ?, ?, ?, ?, ?, ?)"
    )

    for item in items:
        if not isinstance(item, FetchedItem):
            # Skip silently — keep this defensive but not too noisy.
            logger.debug("persist_raw: ignoring non-FetchedItem: %r", type(item))
            continue
        params = _row_payload(item, sub_id)
        cursor = conn.execute(sql, params)
        if cursor.rowcount and cursor.rowcount > 0:
            inserted += 1
            raw_item_id = int(cursor.lastrowid)
            inserted_ids.append(raw_item_id)
        else:
            # rowcount == 0 → UNIQUE(subscription_id, url) collision; that's
            # the dedup path.  Don't log per-row — too noisy on a normal poll.
            skipped += 1
            row = conn.execute(
                """SELECT id FROM raw_items
                   WHERE subscription_id = ? AND url = ?
                   ORDER BY id LIMIT 1""",
                (sub_id, params[0]),
            ).fetchone()
            if row is None:
                continue
            raw_item_id = int(row["id"])
        _record_subscription_discovery(
            conn,
            raw_item_id=raw_item_id,
            subscription_id=sub_id,
            discovered_at=str(params[7]),
        )

    return {
        "inserted": inserted,
        "skipped": skipped,
        "total": inserted + skipped,
        "inserted_ids": inserted_ids,
    }


async def persist_raw(
    items: Iterable[FetchedItem],
    sub_id: int,
    *,
    conn: sqlite3.Connection,
) -> PersistenceResult:
    """Async wrapper around :func:`persist_raw_sync`.

    The caller passes an open ``sqlite3.Connection`` (already attached to
    the right vault DB) so this module remains decoupled from
    :mod:`contents_hub.config`.

    Note: SQLite calls are blocking but typically sub-millisecond per
    row, so we run inline rather than off-thread.  If this ever becomes
    a measurable hot spot, wrap with ``asyncio.to_thread``.
    """
    items_list = list(items)
    return persist_raw_sync(conn, items_list, sub_id)


# ---------------------------------------------------------------------------
# ToolSpec handler (agent-callable form)
# ---------------------------------------------------------------------------


_PERSIST_RAW_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": (
                "List of item records. Each record is an object with at "
                "least 'url' and 'title'; optional fields: 'summary', "
                "'author', 'published_at' (ISO 8601), 'content_html', "
                "'tags', 'source_type', 'extra'."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "author": {"type": "string"},
                    "published_at": {"type": "string"},
                    "content_html": {"type": "string"},
                    "source_type": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "extra": {"type": "object"},
                },
                "required": ["url"],
            },
        },
        "subscription_id": {
            "type": "integer",
            "description": "Subscription primary key these items belong to.",
        },
    },
    "required": ["items", "subscription_id"],
}


def _coerce_published_at(raw: Any) -> datetime | None:
    """Best-effort parse of an agent-supplied published_at string.

    Returns None on any parse failure — the column is nullable.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        # Lazy import — dateutil is already a transitive dep of feedparser.
        from dateutil.parser import parse as _parse

        dt = _parse(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _record_to_item(record: dict[str, Any]) -> FetchedItem | None:
    """Convert an agent-supplied dict to a FetchedItem. None on missing url."""
    url = record.get("url")
    if not url or not isinstance(url, str):
        return None

    passthrough_extra = {
        key: value
        for key, value in record.items()
        if key
        not in {
            "url",
            "title",
            "summary",
            "author",
            "published_at",
            "tags",
            "content_html",
            "source_type",
            "extra",
        }
    }
    explicit_extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}

    return FetchedItem(
        url=url,
        title=str(record.get("title") or ""),
        summary=str(record.get("summary") or ""),
        author=str(record.get("author") or ""),
        published_at=_coerce_published_at(record.get("published_at")),
        tags=list(record.get("tags") or []),
        content_html=str(record.get("content_html") or ""),
        source_type=str(record.get("source_type") or ""),
        extra={**passthrough_extra, **explicit_extra},
    )


async def persist_raw_handler(**kwargs: Any) -> str:
    """Agent-facing entry point.

    The handler is currently a thin error-reporting shim: in P0 the
    executor calls :func:`persist_raw` directly with an already-open DB
    connection; the registered ToolSpec exists so future runners
    (Claude Code, Codex) can wire up the same persistence path through
    the tool registry.  Until a connection-injection mechanism for tools
    lands, calling the agent-side tool returns a structured error rather
    than silently no-oping.
    """
    items_payload = kwargs.get("items")
    sub_id = kwargs.get("subscription_id")

    if not isinstance(items_payload, list):
        return json.dumps(
            {"ok": False, "error": "missing or invalid 'items' (expected array)"}
        )
    if not isinstance(sub_id, int):
        return json.dumps(
            {
                "ok": False,
                "error": "missing or invalid 'subscription_id' (expected int)",
            }
        )

    # Validate inputs even though we don't have a DB handle here — surfaces
    # a useful count back to the agent.
    parsed = [
        _record_to_item(r)
        for r in items_payload
        if isinstance(r, dict)
    ]
    valid_count = sum(1 for x in parsed if x is not None)

    return json.dumps(
        {
            "ok": False,
            "error": (
                "persist_raw is currently invoked directly by the executor "
                "with an open DB connection; the agent-callable variant has "
                "no connection injected in this runtime."
            ),
            "validated": valid_count,
            "subscription_id": sub_id,
        }
    )


persist_raw_tool = ToolSpec(
    name="persist_raw",
    description=(
        "Persist a batch of fetched items to the raw_items table. Dedup "
        "is enforced by the UNIQUE(subscription_id, url) DB constraint via "
        "INSERT OR IGNORE — duplicates are skipped silently. Aux tables "
        "(job_runs, schedules, fetch_cursors) are untouched."
    ),
    input_schema=_PERSIST_RAW_INPUT_SCHEMA,
    handler=persist_raw_handler,
)


def _register_default() -> None:
    """Overwrite the default registry's placeholder spec with the rich one.

    The default registry built by ``tools/registry.py`` maps ``persist_raw``
    to the bare async ``persist_raw`` function — but that signature requires
    a ``conn=`` kwarg the agent has no way to supply. Re-register the
    agent-callable ``persist_raw_tool`` (which wraps ``persist_raw_handler``)
    so that invocations through the SDK MCP adapter get the right shim
    instead of crashing with a TypeError. Mirrors the
    ``fetchers._register_default`` / ``metadata._register_default`` pattern.
    """
    try:
        from contents_hub.tools.registry import get_default_registry

        get_default_registry().register(persist_raw_tool)
    except Exception:  # noqa: BLE001
        logger.debug("persist_raw: deferred default-registry registration")


_register_default()


__all__ = [
    "PersistenceResult",
    "persist_raw",
    "persist_raw_sync",
    "persist_raw_tool",
    "persist_raw_handler",
]
