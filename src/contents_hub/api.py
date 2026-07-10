"""Internal entry-point library for CLI / Web UI / daemon.

Two async functions are exposed:

- :func:`fetch_subscription` — single-subscription fetch.  Resolves a
  URL string or integer subscription ID, runs the production
  LIST → DIFF → CONTENT executor pipeline, persists the returned items via
  :func:`tools.storage.persist_raw`, and returns the :class:`FetchResult`.
- :func:`collect_all_due` — daemon-tick equivalent.  Iterates all due
  subscriptions, dispatches each through ``fetch_subscription``-style
  semantics, and returns an aggregate :class:`DaemonTickResult`.

Contract guardrails (see ``contracts.md``):

- INV-13 / R-B5.1: this module is *internal-only* in P0.  It is **not**
  reachable through any HTTP route and is **not** listed in
  ``__all__``.  CLI, Web UI, and daemon import these names directly as a
  library; HTTP and ``import contents_hub`` surfaces are deferred to P1.
- R-T3.1: ``fetch_subscription(config, sub_ref, *, max_items=10)`` —
  ``sub_ref`` accepts either a URL string or an integer subscription
  ID.  Default ``max_items=10`` is intentionally smaller than
  ``executor.execute``'s default of 50; this is a safety cap for agent
  timeout protection on the agent path (R-U2.1).
- R-B5.1 separation of concerns: this module owns the SQLite connection,
  performs DB-backed diffing between LIST and CONTENT, and calls
  ``tools.storage.persist_raw(items, sub_id, conn=conn)`` after the
  executor returns.  The executor stays pure (no DB side effects).
- R-B2.2 / R-U2.1: per-subscription errors never crash the tick.  In
  ``collect_all_due``, exceptions raised by a single subscription's
  fetch/persist are captured into the per-subscription FetchResult and
  the loop continues with the remaining subscriptions.
- INV-3: ``persist_raw`` uses ``INSERT OR IGNORE`` against the existing
  ``UNIQUE(subscription_id, url)`` constraint.  This module passes the
  caller-owned connection through to that helper without bypassing the
  constraint.
- INV-9: nothing is written to stdout from this module.  All
  diagnostics flow through ``logging.getLogger(__name__)`` which the
  CLI / daemon route to ``cli.log`` / ``daemon.log``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import asyncio
from dataclasses import dataclass, field
from typing import Any

from contents_hub.config import WikiConfig
from contents_hub.chromux import (
    chromux_fetch_session_cleanup,
    chromux_profile_override,
    prepare_chromux_for_background_fetch,
)
from contents_hub.db import init_db
from contents_hub.executor import (
    content_items as _executor_content_items,
    execute as _executor_execute,
    list_items as _executor_list_items,
)
from contents_hub.item_key import normalize_url
from contents_hub.models import FetchFailureReason, FetchResult, ListItem, infer_from_error
from contents_hub.recipes import RecipeRegistry
from contents_hub.subscriptions import Subscription, SubscriptionStore
from contents_hub.timeutil import now_iso
from contents_hub.tools.storage import persist_raw

logger = logging.getLogger(__name__)

DEFAULT_PER_SUBSCRIPTION_TIMEOUT_SECONDS = 120.0
# Lens evaluation is LLM-backed and may run once per enabled default Lens.
# A 30s post-fetch envelope cancels normal two-Lens routing before any
# raw_item_lenses rows are inserted, because matches are persisted only after
# classification returns. Keep this below the watchdog/script timeout, but long
# enough for the configured automatic Lens set.
DEFAULT_POST_FETCH_LENS_TIMEOUT_SECONDS = 180.0

# ---------------------------------------------------------------------------
# Aggregate tick result
# ---------------------------------------------------------------------------


@dataclass
class _PerSubscriptionResult:
    """Per-subscription roll-up for :class:`DaemonTickResult`.

    Field set is intentionally narrower than executor's :class:`FetchResult`
    so that the aggregate can be JSON-serialized cheaply by callers (the
    daemon log line, the CLI ``tick`` output schema, etc.).
    """

    subscription_id: int
    url: str
    ok: bool
    new_items: int
    skipped: int
    error: str = ""
    failure_reason: str = ""


@dataclass
class DaemonTickResult:
    """Aggregate result of :func:`collect_all_due`.

    Required surface (per contracts.md):

    - ``total``  — subscriptions processed
    - ``new``    — total new ``raw_items`` rows inserted
    - ``errors`` — subscriptions that returned ``ok=False``

    Additional fields are convenience telemetry consumed by ``daemon.py``
    (T9) and the CLI ``tick`` JSON schema (T11).  They are *not* part of
    the contracts.md frozen surface; downstream callers should treat
    them as best-effort observability.
    """

    total: int = 0
    new: int = 0
    errors: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    per_subscription: list[_PerSubscriptionResult] = field(default_factory=list)


@dataclass(frozen=True)
class _PersistenceOutcome:
    """Internal persistence result with row identities for post-commit hooks."""

    inserted: int = 0
    skipped: int = 0
    inserted_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class _CollectionOutcome:
    per_subscription: _PerSubscriptionResult
    new: int = 0
    skipped: int = 0
    errors: int = 0
    inserted_ids: tuple[int, ...] = ()


# ---------------------------------------------------------------------------
# Sub-ref resolution
# ---------------------------------------------------------------------------


def _looks_like_int(value: Any) -> bool:
    """Return True iff ``value`` is an int (or an all-digit string)."""
    if isinstance(value, bool):
        return False  # bool is an int subclass — exclude defensively
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        s = value.strip()
        return bool(s) and s.lstrip("-").isdigit()
    return False


def _resolve_subscription(
    store: SubscriptionStore,
    sub_ref: str | int,
) -> Subscription | None:
    """Resolve ``sub_ref`` to a :class:`Subscription` or ``None``.

    Resolution order (R-U2.1):

    1. If ``sub_ref`` parses as an integer, look up by primary key.
       Falls through to URL lookup on miss so a numeric domain like
       ``http://1.2.3.4`` would still resolve.
    2. Otherwise, treat as a URL.
    """
    if _looks_like_int(sub_ref):
        sub = store.get_by_id(str(sub_ref))
        if sub is not None:
            return sub
        # Fall through — could be a stringified URL coincidentally numeric

    if isinstance(sub_ref, str):
        return store.get(sub_ref)

    return None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def _persist_and_record(
    *,
    conn: sqlite3.Connection,
    sub: Subscription,
    result: FetchResult,
) -> _PersistenceOutcome:
    """Persist ``result.items`` and update subscription bookkeeping.

    Returns an internal typed result from :func:`persist_raw`, including
    inserted raw-item ids for post-commit Lens evaluation.

    The executor mutates ``sub.config`` in place for ordinary fetch
    bookkeeping (for example ``consecutive_failures`` and ``fetch_method``).
    We write that back to the DB here so the next tick sees the updated state.
    This is the boundary the executor refused to cross (R-B5.1: executor stays
    pure).
    """
    # Subscription.id stores the DB integer as a string (see
    # SubscriptionStore.add() — it overwrites the UUID with row['id']).
    try:
        sub_id_int = int(sub.id)
    except (TypeError, ValueError):
        # Defensive: an unsaved Subscription would still have its UUID;
        # nothing to persist against in that case.
        logger.warning(
            "api: cannot persist items — subscription id is non-integer (%r)",
            sub.id,
        )
        return _PersistenceOutcome()

    inserted = 0
    skipped = 0
    inserted_ids: tuple[int, ...] = ()

    if result.ok and result.items:
        summary = await persist_raw(result.items, sub_id_int, conn=conn)
        inserted = int(summary.get("inserted", 0))
        skipped = int(summary.get("skipped", 0))
        inserted_ids = tuple(int(i) for i in summary.get("inserted_ids", []))

    # Persist mutated config + fetch status on the caller-owned connection.
    # Opening SubscriptionStore's own connection here can deadlock SQLite
    # while raw_items writes are still in this transaction.
    now_str = now_iso()
    config_data = dict(sub.config)
    if sub.tags:
        config_data["tags"] = sub.tags

    if result.ok:
        config_data["last_fetched_count"] = inserted
        conn.execute(
            """UPDATE subscriptions
               SET status = 'active',
                   last_error = '',
                   consecutive_errors = 0,
                   last_fetched_at = ?,
                   updated_at = ?,
                   config = ?
               WHERE id = ?""",
            (now_str, now_str, json.dumps(config_data), sub_id_int),
        )
    else:
        row = conn.execute(
            "SELECT consecutive_errors FROM subscriptions WHERE id = ?",
            (sub_id_int,),
        ).fetchone()
        current_errors = (row["consecutive_errors"] if row else 0) or 0
        new_errors = int(current_errors) + 1
        new_status = "broken" if new_errors >= 5 else "error"
        conn.execute(
            """UPDATE subscriptions
               SET status = ?,
                   last_error = ?,
                   consecutive_errors = ?,
                   updated_at = ?,
                   config = ?
               WHERE id = ?""",
            (
                new_status,
                result.error or "fetch failed",
                new_errors,
                now_str,
                json.dumps(config_data),
                sub_id_int,
            ),
        )

    return _PersistenceOutcome(
        inserted=inserted,
        skipped=skipped,
        inserted_ids=inserted_ids,
    )


async def _evaluate_post_fetch_lenses(
    *,
    config: WikiConfig,
    subscription_id: int,
    inserted_ids: tuple[int, ...],
) -> None:
    """Run optional Lens evaluation after raw-item writes are committed.

    The helper is loaded lazily because Lens implementation work is owned by a
    separate slice. Missing Lens support is a no-op, and Lens failures are
    isolated from fetch/tick public result semantics.
    """
    if not inserted_ids:
        return

    try:
        from contents_hub.lenses import evaluate_post_fetch_lenses
    except (ImportError, AttributeError):
        logger.debug("post-fetch Lens evaluation unavailable")
        return

    try:
        await asyncio.wait_for(
            evaluate_post_fetch_lenses(
                config=config,
                subscription_id=subscription_id,
                raw_item_ids=list(inserted_ids),
            ),
            timeout=DEFAULT_POST_FETCH_LENS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "post-fetch Lens evaluation timed out for subscription %s",
            subscription_id,
        )
    except Exception:  # noqa: BLE001 - post-commit hook is best-effort
        logger.exception(
            "post-fetch Lens evaluation failed for subscription %s",
            subscription_id,
        )


def _failure_result(
    *,
    sub_url: str,
    error: str,
    failure_reason: FetchFailureReason | None = None,
) -> FetchResult:
    """Build a uniform failure :class:`FetchResult`."""
    reason = (
        failure_reason.value
        if failure_reason is not None
        else infer_from_error(error).value
    )
    return FetchResult(
        ok=False,
        source_url=sub_url,
        error=error,
        error_type="API_ERROR",
        failure_reason=reason,
    )


def _filter_new_list_items(
    *,
    conn: sqlite3.Connection,
    subscription_id: int,
    items: list[ListItem],
    max_items: int,
) -> tuple[list[ListItem], int]:
    """Return list candidates that are not already present in raw_items."""
    if not items:
        return [], 0

    urls = [normalize_url(item.url) for item in items if item.url]
    if not urls:
        return [], 0

    placeholders = ",".join("?" for _ in urls)
    rows = conn.execute(
        f"""SELECT url FROM raw_items
            WHERE subscription_id = ? AND url IN ({placeholders})""",
        (subscription_id, *urls),
    ).fetchall()
    existing = {str(row[0]) for row in rows}

    new_items = [item for item in items if normalize_url(item.url) not in existing]
    skipped = len(items) - len(new_items)
    return new_items[:max_items], skipped


def _mark_no_new_success(sub: Subscription) -> None:
    """Mirror executor success bookkeeping when LIST finds only old items."""
    sub.config["consecutive_failures"] = 0
    sub.config.pop("needs_error_status", None)
    sub.config.pop("relearn_count", None)
    sub.config.pop("allow_relearn", None)
    sub.config.pop("allow_explore", None)


async def _incremental_executor_execute(
    *,
    conn: sqlite3.Connection,
    sub: Subscription,
    sub_id_int: int,
    max_items: int,
) -> tuple[FetchResult, int]:
    """Production subscription invariant: LIST -> DIFF -> CONTENT."""
    list_result = await _executor_list_items(sub, max_pagination=0)
    if not list_result.ok:
        return (
            FetchResult(
                ok=False,
                source_url=sub.url,
                error=list_result.error,
                error_type="AGENT_ERROR",
                failure_reason=list_result.failure_reason,
            ),
            0,
        )

    candidates, skipped = _filter_new_list_items(
        conn=conn,
        subscription_id=sub_id_int,
        items=list_result.items,
        max_items=max_items,
    )
    logger.info(
        "api.fetch_subscription: diff url=%s listed=%d new=%d skipped=%d",
        sub.url,
        len(list_result.items),
        len(candidates),
        skipped,
    )

    if not candidates:
        _mark_no_new_success(sub)
        return (
            FetchResult(
                ok=True,
                items=[],
                source_url=sub.url,
                source_title=sub.url,
                total_available=len(list_result.items),
            ),
            skipped,
        )

    return (
        await _executor_content_items(
            sub,
            candidates,
            max_items=max_items,
            total_available=len(list_result.items),
        ),
        skipped,
    )


def _subscription_uses_chromux(sub: Subscription) -> bool:
    meta = RecipeRegistry.get_recipe_metadata(sub)
    capabilities = set(meta.get("capabilities") or [])
    fetch_method = meta.get("fetch_method") or (getattr(sub, "config", None) or {}).get("fetch_method")
    config = getattr(sub, "config", None) or {}
    capabilities.update(config.get("recipe_capabilities") or [])
    return (
        fetch_method == "browser"
        or "chromux_navigate" in capabilities
        or "chromux_extract" in capabilities
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def fetch_subscription(
    config: WikiConfig,
    sub_ref: str | int,
    *,
    max_items: int = 10,
) -> FetchResult:
    """Run a single-subscription fetch and persist returned items.

    Args:
        config: Resolved :class:`WikiConfig` for the target vault.  Used
            to open the SQLite connection and resolve the subscription.
        sub_ref: Either the subscription URL (string) or the integer DB
            primary key (R-U2.1).  Numeric strings are accepted and
            treated as IDs first, falling back to URL lookup.
        max_items: Safety cap forwarded to the content-fetch phase.
            Defaults to ``10`` — *intentionally* smaller than the
            executor's own default of 50.  This protects the
            per-subscription wall clock budget (R-T3.1).

    Returns:
        :class:`FetchResult` from the executor pipeline.  On
        unresolved ``sub_ref`` or unexpected error, returns
        ``FetchResult(ok=False, …)`` with ``failure_reason`` populated.
    """
    store = SubscriptionStore(config)
    sub = _resolve_subscription(store, sub_ref)
    if sub is None:
        ref_str = str(sub_ref)
        logger.warning("api.fetch_subscription: unresolved sub_ref=%r", sub_ref)
        return _failure_result(
            sub_url=ref_str if isinstance(sub_ref, str) else "",
            error=f"subscription not found: {ref_str}",
            failure_reason=FetchFailureReason.NOT_FOUND,
        )

    chromux_profile: str | None = None
    if _subscription_uses_chromux(sub):
        prep = prepare_chromux_for_background_fetch()
        if not prep.get("ok"):
            return _failure_result(sub_url=sub.url, error=str(prep.get("error") or "chromux setup failed"))
        chromux_profile = str(prep.get("profile") or "") or None

    conn = init_db(config)
    try:
        try:
            try:
                sub_id_int = int(sub.id)
            except (TypeError, ValueError):
                result = await _executor_execute(sub, max_items=max_items)
            else:
                if _subscription_uses_chromux(sub):
                    with chromux_profile_override(chromux_profile):
                        async with chromux_fetch_session_cleanup(profile=chromux_profile):
                            result, _diff_skipped = await _incremental_executor_execute(
                                conn=conn,
                                sub=sub,
                                sub_id_int=sub_id_int,
                                max_items=max_items,
                            )
                else:
                    result, _diff_skipped = await _incremental_executor_execute(
                        conn=conn,
                        sub=sub,
                        sub_id_int=sub_id_int,
                        max_items=max_items,
                    )
        except Exception as exc:  # noqa: BLE001 - log and surface as ok=False
            logger.exception(
                "api.fetch_subscription: executor raised for %s", sub.url,
            )
            result = _failure_result(
                sub_url=sub.url,
                error=f"executor error: {exc}",
            )

        persistence = await _persist_and_record(
            conn=conn,
            sub=sub,
            result=result,
        )
        conn.commit()
        if result.ok and persistence.inserted_ids:
            await _evaluate_post_fetch_lenses(
                config=config,
                subscription_id=int(sub.id),
                inserted_ids=persistence.inserted_ids,
            )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _collect_one_subscription_row(
    *,
    store: SubscriptionStore,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    log_label: str,
) -> _CollectionOutcome:
    sub_url = row["url"]
    sub_id_int = int(row["id"])

    sub = _row_to_subscription(store, row)
    if sub is None:
        raise RuntimeError(
            f"subscription row id={sub_id_int} hydrated to None",
        )

    chromux_profile: str | None = None
    if _subscription_uses_chromux(sub):
        prep = prepare_chromux_for_background_fetch()
        if not prep.get("ok"):
            raise RuntimeError(str(prep.get("error") or "chromux setup failed"))
        chromux_profile = str(prep.get("profile") or "") or None

    if _subscription_uses_chromux(sub):
        with chromux_profile_override(chromux_profile):
            async with chromux_fetch_session_cleanup(profile=chromux_profile):
                fetch_result, diff_skipped = await _incremental_executor_execute(
                    conn=conn,
                    sub=sub,
                    sub_id_int=sub_id_int,
                    max_items=10,
                )
    else:
        fetch_result, diff_skipped = await _incremental_executor_execute(
            conn=conn,
            sub=sub,
            sub_id_int=sub_id_int,
            max_items=10,
        )
    persistence = await _persist_and_record(
        conn=conn,
        sub=sub,
        result=fetch_result,
    )
    conn.commit()

    if fetch_result.ok:
        inserted = persistence.inserted
        skipped = diff_skipped + persistence.skipped
        return _CollectionOutcome(
            new=inserted,
            skipped=skipped,
            inserted_ids=persistence.inserted_ids,
            per_subscription=_PerSubscriptionResult(
                subscription_id=sub_id_int,
                url=sub_url,
                ok=True,
                new_items=inserted,
                skipped=skipped,
            ),
        )

    return _CollectionOutcome(
        errors=1,
        per_subscription=_PerSubscriptionResult(
            subscription_id=sub_id_int,
            url=sub_url,
            ok=False,
            new_items=0,
            skipped=0,
            error=fetch_result.error,
            failure_reason=fetch_result.failure_reason,
        ),
    )


async def _collect_subscription_rows(
    config: WikiConfig,
    query,
    *,
    log_label: str,
    per_subscription_timeout_seconds: float = DEFAULT_PER_SUBSCRIPTION_TIMEOUT_SECONDS,
    concurrency: int = 1,
) -> DaemonTickResult:
    """Fetch subscriptions returned by ``query`` and aggregate the results."""
    import time

    started = time.monotonic()
    concurrency = max(int(concurrency or 1), 1)

    if concurrency > 1:
        conn = init_db(config)
        try:
            rows = [dict(row) for row in query(conn)]
        finally:
            conn.close()

        result = DaemonTickResult(total=len(rows))
        semaphore = asyncio.Semaphore(concurrency)

        async def _run(row: dict[str, Any]) -> _CollectionOutcome:
            async with semaphore:
                return await _collect_one_subscription_row_isolated(
                    config=config,
                    row=row,
                    log_label=log_label,
                    per_subscription_timeout_seconds=per_subscription_timeout_seconds,
                )

        outcomes = await asyncio.gather(*[_run(row) for row in rows])
        for outcome in outcomes:
            result.new += outcome.new
            result.skipped += outcome.skipped
            result.errors += outcome.errors
            result.per_subscription.append(outcome.per_subscription)

        result.duration_seconds = time.monotonic() - started
        logger.info(
            "%s: total=%d new=%d skipped=%d errors=%d concurrency=%d in %.1fs",
            log_label, result.total, result.new, result.skipped, result.errors,
            concurrency, result.duration_seconds,
        )
        return result

    store = SubscriptionStore(config)
    conn = init_db(config)
    try:
        rows = query(conn)
        result = DaemonTickResult(total=len(rows))
        for row in rows:
            sub_url = row["url"]
            sub_id_int = int(row["id"])

            try:
                outcome = await asyncio.wait_for(
                    _collect_one_subscription_row(
                        store=store,
                        conn=conn,
                        row=row,
                        log_label=log_label,
                    ),
                    timeout=per_subscription_timeout_seconds,
                )
                result.new += outcome.new
                result.skipped += outcome.skipped
                result.errors += outcome.errors
                result.per_subscription.append(outcome.per_subscription)
                if outcome.inserted_ids:
                    await _evaluate_post_fetch_lenses(
                        config=config,
                        subscription_id=sub_id_int,
                        inserted_ids=outcome.inserted_ids,
                    )
            except asyncio.TimeoutError:
                error = f"timed out after {per_subscription_timeout_seconds:g}s"
                logger.warning(
                    "%s: timeout on subscription %s (id=%s): %s",
                    log_label, sub_url, sub_id_int, error,
                )
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                result.errors += 1
                result.per_subscription.append(
                    _PerSubscriptionResult(
                        subscription_id=sub_id_int,
                        url=sub_url,
                        ok=False,
                        new_items=0,
                        skipped=0,
                        error=error,
                        failure_reason=FetchFailureReason.TIMEOUT.value,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-sub isolation
                logger.exception(
                    "%s: error on subscription %s (id=%s)",
                    log_label, sub_url, sub_id_int,
                )
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                result.errors += 1
                inferred = infer_from_error(str(exc)).value
                result.per_subscription.append(
                    _PerSubscriptionResult(
                        subscription_id=sub_id_int,
                        url=sub_url,
                        ok=False,
                        new_items=0,
                        skipped=0,
                        error=str(exc),
                        failure_reason=inferred,
                    )
                )

        result.duration_seconds = time.monotonic() - started
        logger.info(
            "%s: total=%d new=%d skipped=%d errors=%d in %.1fs",
            log_label, result.total, result.new, result.skipped, result.errors,
            result.duration_seconds,
        )
        return result
    finally:
        conn.close()


async def _collect_one_subscription_row_isolated(
    *,
    config: WikiConfig,
    row: dict[str, Any],
    log_label: str,
    per_subscription_timeout_seconds: float,
) -> _CollectionOutcome:
    """Collect one subscription using task-local DB state.

    Parallel fetch-all cannot share the SQLite connection used by the
    sequential path. Each worker opens its own connection for fetch/persist,
    then runs post-fetch Lens evaluation outside the per-subscription timeout
    to preserve the sequential timeout boundary.
    """
    sub_url = str(row["url"])
    sub_id_int = int(row["id"])
    store = SubscriptionStore(config)
    conn = init_db(config)
    try:
        try:
            outcome = await asyncio.wait_for(
                _collect_one_subscription_row(
                    store=store,
                    conn=conn,
                    row=row,  # type: ignore[arg-type]
                    log_label=log_label,
                ),
                timeout=per_subscription_timeout_seconds,
            )
        except asyncio.TimeoutError:
            error = f"timed out after {per_subscription_timeout_seconds:g}s"
            logger.warning(
                "%s: timeout on subscription %s (id=%s): %s",
                log_label, sub_url, sub_id_int, error,
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return _CollectionOutcome(
                errors=1,
                per_subscription=_PerSubscriptionResult(
                    subscription_id=sub_id_int,
                    url=sub_url,
                    ok=False,
                    new_items=0,
                    skipped=0,
                    error=error,
                    failure_reason=FetchFailureReason.TIMEOUT.value,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - per-sub isolation
            logger.exception(
                "%s: error on subscription %s (id=%s)",
                log_label, sub_url, sub_id_int,
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            inferred = infer_from_error(str(exc)).value
            return _CollectionOutcome(
                errors=1,
                per_subscription=_PerSubscriptionResult(
                    subscription_id=sub_id_int,
                    url=sub_url,
                    ok=False,
                    new_items=0,
                    skipped=0,
                    error=str(exc),
                    failure_reason=inferred,
                ),
            )
    finally:
        conn.close()

    if outcome.inserted_ids:
        await _evaluate_post_fetch_lenses(
            config=config,
            subscription_id=sub_id_int,
            inserted_ids=outcome.inserted_ids,
        )
    return outcome


async def collect_all_due(
    config: WikiConfig,
    *,
    per_subscription_timeout_seconds: float = DEFAULT_PER_SUBSCRIPTION_TIMEOUT_SECONDS,
    concurrency: int = 1,
) -> DaemonTickResult:
    """Fetch all due subscriptions and aggregate the results.

    A subscription is "due" by the same criteria that ``daemon.py``
    used pre-refactor: ``status='active'`` AND
    ``schedule_interval_minutes > 0`` AND
    (``last_fetched_at IS NULL`` OR
    ``last_fetched_at + interval <= now``).

    Per-subscription errors are *isolated* (R-B2.2 / R-U2.1): if one
    subscription raises, the exception is captured into its
    :class:`_PerSubscriptionResult` and the loop continues with the
    remaining due subscriptions.

    Args:
        config: Resolved :class:`WikiConfig` for the target vault.

    Returns:
        :class:`DaemonTickResult` with aggregate counters and a
        per-subscription roll-up suitable for the CLI ``tick`` JSON
        schema (T11) and the daemon's status log line (T9).
    """
    return await _collect_subscription_rows(
        config,
        _query_due_rows,
        log_label="collect_all_due",
        per_subscription_timeout_seconds=per_subscription_timeout_seconds,
        concurrency=concurrency,
    )


async def collect_all_active(
    config: WikiConfig,
    *,
    include_error: bool = False,
    per_subscription_timeout_seconds: float = DEFAULT_PER_SUBSCRIPTION_TIMEOUT_SECONDS,
    concurrency: int = 1,
) -> DaemonTickResult:
    """Fetch every active subscription, ignoring schedule due state.

    This is the cron/manual "force all" primitive. It remains idempotent
    because the same incremental diff and raw-item uniqueness path used by
    ``collect_all_due`` is still applied for each subscription.
    """
    query = _query_fetch_all_rows if include_error else _query_active_rows
    log_label = "collect_all_fetch_all" if include_error else "collect_all_active"
    return await _collect_subscription_rows(
        config,
        query,
        log_label=log_label,
        per_subscription_timeout_seconds=per_subscription_timeout_seconds,
        concurrency=concurrency,
    )


# ---------------------------------------------------------------------------
# Internal helpers (DB row → Subscription hydration; due-row query)
# ---------------------------------------------------------------------------


_SUBSCRIPTION_COLUMNS = """id, url, title, source_type, status,
                      schedule_cron, schedule_interval_minutes,
                      default_lens_ids, config,
                      last_fetched_at, last_error, consecutive_errors,
                      created_at, updated_at"""


def _query_due_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return active subscriptions whose next fetch is due.

    Mirrors ``daemon._query_due_subscriptions`` so this module can take
    over without changing the due-detection semantics (R-T7.1 / T9
    will then delete the daemon-side copy).
    """
    now_str = now_iso()
    return list(
        conn.execute(
            f"""SELECT {_SUBSCRIPTION_COLUMNS}
               FROM subscriptions
               WHERE status = 'active'
                 AND schedule_interval_minutes > 0
                 AND (
                     last_fetched_at IS NULL
                     OR datetime(
                          last_fetched_at,
                          '+' || schedule_interval_minutes || ' minutes'
                        ) <= datetime(?)
                 )
               ORDER BY last_fetched_at ASC NULLS FIRST""",
            (now_str,),
        ).fetchall()
    )


def _query_active_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all active subscriptions regardless of due schedule."""
    return list(
        conn.execute(
            f"""SELECT {_SUBSCRIPTION_COLUMNS}
               FROM subscriptions
               WHERE status = 'active'
               ORDER BY id ASC"""
        ).fetchall()
    )


def _query_fetch_all_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return subscriptions eligible for a manual force fetch."""
    return list(
        conn.execute(
            f"""SELECT {_SUBSCRIPTION_COLUMNS}
               FROM subscriptions
               WHERE status IN ('active', 'error')
               ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id ASC"""
        ).fetchall()
    )


def _row_to_subscription(
    store: SubscriptionStore,
    row: sqlite3.Row,
) -> Subscription | None:
    """Hydrate a Subscription from a due-query row.

    We delegate to ``store.get(url)`` so the canonical row → Subscription
    mapping (used everywhere else in :mod:`subscriptions`) is the single
    source of truth.  The fallback path constructs a Subscription
    directly from the row if for some reason the lookup misses (e.g.
    the row was deleted in a race).
    """
    sub = store.get(row["url"])
    if sub is not None:
        # Make sure sub.id is the integer DB id (string-encoded), not a
        # legacy UUID — required for persist_raw.
        if not str(sub.id).isdigit():
            sub.id = str(row["id"])
        return sub

    # Fallback: hydrate inline from the row.
    try:
        cfg_raw = row["config"]
        cfg = json.loads(cfg_raw) if cfg_raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        cfg = {}

    sub = Subscription(
        url=row["url"],
        title=row["title"] or "",
        source_type=row["source_type"] or "",
        config=cfg if isinstance(cfg, dict) else {},
    )
    sub.id = str(row["id"])
    return sub


# Intentionally NOT defining __all__ here.  Per INV-13 / R-B5.1, this
# module is internal-only in P0; importers reach in by name (e.g.
# ``from contents_hub.api import fetch_subscription``) and we don't
# advertise a stable public surface.
