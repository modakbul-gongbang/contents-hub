"""
SQLite-based local state management for contents-hub.

Stores schedule entries, run history, and other operational state.
Kept in .contents-hub/state.db inside the vault.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from contents_hub.config import WikiConfig
from contents_hub.timeutil import now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 15

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_url TEXT NOT NULL UNIQUE,
    source_type     TEXT NOT NULL DEFAULT 'rss',
    cron_expr       TEXT,                          -- e.g. "*/30 * * * *"
    interval_minutes INTEGER NOT NULL DEFAULT 30,  -- fallback if no cron
    enabled         INTEGER NOT NULL DEFAULT 1,    -- 0 = paused
    running         INTEGER NOT NULL DEFAULT 0,    -- 1 = run in-flight
    next_run_at     TEXT,                          -- ISO 8601 UTC
    last_run_at     TEXT,                          -- ISO 8601 UTC
    last_run_ok     INTEGER,                       -- 1 = success, 0 = error
    last_error      TEXT DEFAULT '',
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id     INTEGER NOT NULL REFERENCES schedules(id),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running', -- running / ok / error
    new_items       INTEGER DEFAULT 0,
    error_message   TEXT DEFAULT '',
    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fetch_cursors (
    subscription_url TEXT NOT NULL,
    cursor_type     TEXT NOT NULL,       -- 'since_id' or 'since_timestamp'
    cursor_value    TEXT NOT NULL,        -- tweet ID or ISO 8601 timestamp
    updated_at      TEXT NOT NULL,        -- ISO 8601 UTC
    PRIMARY KEY (subscription_url, cursor_type)
);

CREATE TABLE IF NOT EXISTS collected_tweets (
    tweet_id        TEXT NOT NULL PRIMARY KEY,
    subscription_url TEXT NOT NULL,
    source_file     TEXT NOT NULL,           -- relative path in vault
    collected_at    TEXT NOT NULL             -- ISO 8601 UTC
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT 'rss',       -- rss / youtube / twitter / webpage / agent
    status          TEXT NOT NULL DEFAULT 'active',     -- active / paused / error
    schedule_cron   TEXT,                               -- e.g. "*/30 * * * *"
    schedule_interval_minutes INTEGER NOT NULL DEFAULT 30,
    default_lens_ids TEXT NOT NULL DEFAULT '[]',        -- JSON array of lens id slugs
    config          TEXT NOT NULL DEFAULT '{}',         -- JSON object for source-specific config
    last_fetched_at TEXT,                               -- ISO 8601 UTC
    last_error      TEXT DEFAULT '',
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    item_count      INTEGER NOT NULL DEFAULT 0,
    content_md      TEXT NOT NULL DEFAULT '',
    sections_json   TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'ok',
    error           TEXT NOT NULL DEFAULT '',
    output_path     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS raw_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,                         -- canonical key: normalized URL or content://<sub>/<hash>
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',              -- full body/transcript for reprocessing
    origin          TEXT NOT NULL DEFAULT 'subscription', -- manual / subscription / agent / exploration
    priority        INTEGER NOT NULL DEFAULT 50,          -- manual=100, subscription=50, agent=30
    status          TEXT NOT NULL DEFAULT 'raw',           -- raw / promoted / archived
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    content_summary TEXT DEFAULT '',                       -- short preview (≤500 chars) — may be empty
    metadata_json   TEXT NOT NULL DEFAULT '{}',            -- JSON context: metrics, links, media, comments
    published_at    TEXT,                                  -- ISO 8601 UTC (when the item was originally published)
    collected_at    TEXT NOT NULL,                         -- ISO 8601 UTC (when we fetched it)
    updated_at      TEXT NOT NULL,
    digest_id       INTEGER REFERENCES digests(id) ON DELETE SET NULL,
    UNIQUE(subscription_id, url)
);

CREATE TABLE IF NOT EXISTS digest_section_items (
    digest_id       INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
    section_index   INTEGER NOT NULL,
    lens_id         TEXT NOT NULL DEFAULT '',
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (digest_id, section_index, raw_item_id)
);

CREATE TABLE IF NOT EXISTS saved_items (
    raw_item_id     INTEGER NOT NULL PRIMARY KEY REFERENCES raw_items(id) ON DELETE CASCADE,
    saved_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    workspace_id    TEXT NOT NULL DEFAULT '',
    channel_id      TEXT NOT NULL DEFAULT '',
    thread_id       TEXT NOT NULL DEFAULT '',
    message_id      TEXT NOT NULL,
    payload_type    TEXT NOT NULL DEFAULT 'raw_item',
    raw_item_id     INTEGER REFERENCES raw_items(id) ON DELETE SET NULL,
    digest_id       INTEGER REFERENCES digests(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(platform, workspace_id, channel_id, thread_id, message_id)
);

CREATE TABLE IF NOT EXISTS interaction_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    event_id        TEXT NOT NULL DEFAULT '',
    workspace_id    TEXT NOT NULL DEFAULT '',
    channel_id      TEXT NOT NULL DEFAULT '',
    message_id      TEXT NOT NULL DEFAULT '',
    user_id         TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,
    value           TEXT NOT NULL DEFAULT '',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    handled_action  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'received',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_item_discoveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id         INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    owner_type          TEXT NOT NULL,
    owner_id            INTEGER,
    owner_label         TEXT NOT NULL DEFAULT '',
    owner_run_id        INTEGER,
    exploration_id      INTEGER,
    strategy_version    INTEGER,
    run_at              TEXT,
    discovered_at       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    deleted_at          TEXT,
    deleted_owner_id    INTEGER,
    deleted_owner_label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lenses (
    id              TEXT NOT NULL PRIMARY KEY,             -- slug, e.g. 'ai-research'
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '[]',            -- JSON array of keyword strings
    enabled         INTEGER NOT NULL DEFAULT 1,            -- 0 = disabled
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_item_lenses (
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    lens_id         TEXT NOT NULL REFERENCES lenses(id) ON DELETE CASCADE,
    summary         TEXT NOT NULL DEFAULT '',
    bullets_json    TEXT NOT NULL DEFAULT '[]',
    enriched_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (raw_item_id, lens_id)
);

CREATE TABLE IF NOT EXISTS explorations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name    TEXT NOT NULL DEFAULT '',
    original_request TEXT NOT NULL,
    target_surfaces TEXT NOT NULL DEFAULT '[]',
    lens_ids        TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'draft',
    approved_strategy_version_id INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (approved_strategy_version_id)
        REFERENCES exploration_strategy_versions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS exploration_validation_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id  INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    attempt_number  INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    strategy_snapshot TEXT NOT NULL DEFAULT '{}',
    process_summary TEXT NOT NULL DEFAULT '',
    raw_trace_artifact_path TEXT,
    preview_items_json TEXT NOT NULL DEFAULT '[]',
    preview_lens_matches_json TEXT NOT NULL DEFAULT '[]',
    error           TEXT NOT NULL DEFAULT '',
    chromux_session_ids TEXT NOT NULL DEFAULT '[]',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(exploration_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS exploration_strategy_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id  INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    strategy_snapshot TEXT NOT NULL,
    validation_attempt_id INTEGER REFERENCES exploration_validation_attempts(id)
        ON DELETE SET NULL,
    approved_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(exploration_id, version)
);

CREATE TABLE IF NOT EXISTS exploration_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id  INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    strategy_version_id INTEGER NOT NULL REFERENCES exploration_strategy_versions(id),
    status          TEXT NOT NULL DEFAULT 'running',
    items_found     INTEGER NOT NULL DEFAULT 0,
    items_inserted  INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    raw_trace_artifact_path TEXT,
    chromux_session_ids TEXT NOT NULL DEFAULT '[]',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,                        -- ISO 8601 UTC
    finished_at     TEXT,                                 -- ISO 8601 UTC
    status          TEXT NOT NULL DEFAULT 'running',       -- running / ok / error
    items_total     INTEGER NOT NULL DEFAULT 0,
    items_new       INTEGER NOT NULL DEFAULT 0,
    items_error     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    signal          TEXT NOT NULL,                        -- impression/open/read/save/promote/archive/unsave/note
    source          TEXT NOT NULL DEFAULT '',             -- web/cli/slack/discord/telegram/agent/backfill
    dedup_key       TEXT NOT NULL DEFAULT '',             -- idempotency key; '' disables dedup
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_embeddings (
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    vector          BLOB NOT NULL,                        -- float32 array, little-endian
    text_hash       TEXT NOT NULL DEFAULT '',             -- sha256 of embedded text (incremental re-embeds)
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (raw_item_id, model)
);

CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON schedules(next_run_at)
    WHERE enabled = 1;
CREATE INDEX IF NOT EXISTS idx_schedules_url ON schedules(subscription_url);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs(schedule_id);
CREATE INDEX IF NOT EXISTS idx_fetch_cursors_url ON fetch_cursors(subscription_url);
CREATE INDEX IF NOT EXISTS idx_collected_tweets_sub ON collected_tweets(subscription_url);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_url ON subscriptions(url);
CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at);
CREATE INDEX IF NOT EXISTS idx_raw_items_status ON raw_items(status);
CREATE INDEX IF NOT EXISTS idx_raw_items_origin ON raw_items(origin);
CREATE INDEX IF NOT EXISTS idx_raw_items_subscription ON raw_items(subscription_id);
CREATE INDEX IF NOT EXISTS idx_raw_items_url ON raw_items(url);
CREATE INDEX IF NOT EXISTS idx_digest_section_items_raw_item
    ON digest_section_items(raw_item_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_saved_at ON saved_items(saved_at);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_lookup
    ON outbound_messages(platform, workspace_id, channel_id, thread_id, message_id);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_raw_item
    ON outbound_messages(raw_item_id);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_digest
    ON outbound_messages(digest_id);
CREATE INDEX IF NOT EXISTS idx_interaction_events_message
    ON interaction_events(platform, workspace_id, channel_id, message_id);
CREATE INDEX IF NOT EXISTS idx_interaction_events_created_at
    ON interaction_events(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_events_event_id
    ON interaction_events(platform, event_id)
    WHERE event_id != '';
CREATE INDEX IF NOT EXISTS idx_raw_item_discoveries_raw_item
    ON raw_item_discoveries(raw_item_id);
CREATE INDEX IF NOT EXISTS idx_raw_item_discoveries_owner
    ON raw_item_discoveries(owner_type, owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_item_discoveries_active_owner
    ON raw_item_discoveries(raw_item_id, owner_type, owner_id)
    WHERE deleted_at IS NULL AND owner_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_item_lenses_lens ON raw_item_lenses(lens_id);
CREATE INDEX IF NOT EXISTS idx_explorations_status ON explorations(status);
CREATE INDEX IF NOT EXISTS idx_exploration_validation_attempts_owner
    ON exploration_validation_attempts(exploration_id);
CREATE INDEX IF NOT EXISTS idx_exploration_strategy_versions_owner
    ON exploration_strategy_versions(exploration_id);
CREATE INDEX IF NOT EXISTS idx_exploration_runs_owner ON exploration_runs(exploration_id);
CREATE INDEX IF NOT EXISTS idx_exploration_runs_strategy
    ON exploration_runs(strategy_version_id);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_item_signals_dedup
    ON item_signals(raw_item_id, signal, dedup_key)
    WHERE dedup_key != '';
CREATE INDEX IF NOT EXISTS idx_item_signals_item
    ON item_signals(raw_item_id, created_at);
CREATE INDEX IF NOT EXISTS idx_item_signals_created_at
    ON item_signals(created_at);
"""

# Migration from v4 → v5: add `running` flag to schedules
_MIGRATION_V5_SQL = """\
ALTER TABLE schedules ADD COLUMN running INTEGER NOT NULL DEFAULT 0;
"""

# Migration from v5 → v6: raw_items gains body, published_at; adds UNIQUE(subscription_id, url).
# SQLite can't add UNIQUE in-place, so we rebuild the table and copy rows
# (dedup via INSERT OR IGNORE so pre-existing duplicates collapse to one row).
_MIGRATION_V6_SQL = """\
ALTER TABLE raw_items ADD COLUMN body TEXT NOT NULL DEFAULT '';
ALTER TABLE raw_items ADD COLUMN published_at TEXT;

CREATE TABLE raw_items_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    origin          TEXT NOT NULL DEFAULT 'subscription',
    priority        INTEGER NOT NULL DEFAULT 50,
    status          TEXT NOT NULL DEFAULT 'raw',
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    content_summary TEXT DEFAULT '',
    published_at    TEXT,
    collected_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(subscription_id, url)
);

INSERT OR IGNORE INTO raw_items_new
    (id, url, title, body, origin, priority, status, subscription_id,
     content_summary, published_at, collected_at, updated_at)
SELECT
    id, url, title, body, origin, priority, status, subscription_id,
    content_summary, published_at, collected_at, updated_at
FROM raw_items;

DROP TABLE raw_items;
ALTER TABLE raw_items_new RENAME TO raw_items;

CREATE INDEX IF NOT EXISTS idx_raw_items_status ON raw_items(status);
CREATE INDEX IF NOT EXISTS idx_raw_items_origin ON raw_items(origin);
CREATE INDEX IF NOT EXISTS idx_raw_items_subscription ON raw_items(subscription_id);
CREATE INDEX IF NOT EXISTS idx_raw_items_url ON raw_items(url);
CREATE INDEX IF NOT EXISTS idx_raw_items_published ON raw_items(published_at);
"""

# Migration from v6 → v7: store Lens-specific summaries on matched raw items.
_MIGRATION_V7_SQL = """\
ALTER TABLE raw_item_lenses ADD COLUMN summary TEXT NOT NULL DEFAULT '';
ALTER TABLE raw_item_lenses ADD COLUMN bullets_json TEXT NOT NULL DEFAULT '[]';
"""

# Migration from v7 → v8: digest pipeline — digests table, raw_items.digest_id FK, and index.
# digest_id is added NULLABLE with no backfill (pre-existing rows stay NULL).
_MIGRATION_V8_SQL = """\
CREATE TABLE IF NOT EXISTS digests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    item_count      INTEGER NOT NULL DEFAULT 0,
    content_md      TEXT NOT NULL DEFAULT '',
    sections_json   TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'ok',
    error           TEXT NOT NULL DEFAULT '',
    output_path     TEXT NOT NULL DEFAULT ''
);

ALTER TABLE raw_items ADD COLUMN digest_id INTEGER REFERENCES digests(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_raw_items_digest ON raw_items(digest_id);
"""

# Migration from v8 -> v9: exploration lifecycle storage.
_MIGRATION_V9_SQL = """\
CREATE TABLE IF NOT EXISTS explorations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name    TEXT NOT NULL DEFAULT '',
    original_request TEXT NOT NULL,
    target_surfaces TEXT NOT NULL DEFAULT '[]',
    lens_ids        TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'draft',
    approved_strategy_version_id INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (approved_strategy_version_id)
        REFERENCES exploration_strategy_versions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS exploration_validation_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id  INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    attempt_number  INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    strategy_snapshot TEXT NOT NULL DEFAULT '{}',
    process_summary TEXT NOT NULL DEFAULT '',
    raw_trace_artifact_path TEXT,
    preview_items_json TEXT NOT NULL DEFAULT '[]',
    preview_lens_matches_json TEXT NOT NULL DEFAULT '[]',
    error           TEXT NOT NULL DEFAULT '',
    chromux_session_ids TEXT NOT NULL DEFAULT '[]',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(exploration_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS exploration_strategy_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id  INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    strategy_snapshot TEXT NOT NULL,
    validation_attempt_id INTEGER REFERENCES exploration_validation_attempts(id)
        ON DELETE SET NULL,
    approved_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(exploration_id, version)
);

CREATE TABLE IF NOT EXISTS exploration_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id  INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    strategy_version_id INTEGER NOT NULL REFERENCES exploration_strategy_versions(id),
    status          TEXT NOT NULL DEFAULT 'running',
    items_found     INTEGER NOT NULL DEFAULT 0,
    items_inserted  INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    raw_trace_artifact_path TEXT,
    chromux_session_ids TEXT NOT NULL DEFAULT '[]',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_explorations_status ON explorations(status);
CREATE INDEX IF NOT EXISTS idx_exploration_validation_attempts_owner
    ON exploration_validation_attempts(exploration_id);
CREATE INDEX IF NOT EXISTS idx_exploration_strategy_versions_owner
    ON exploration_strategy_versions(exploration_id);
CREATE INDEX IF NOT EXISTS idx_exploration_runs_owner ON exploration_runs(exploration_id);
CREATE INDEX IF NOT EXISTS idx_exploration_runs_strategy
    ON exploration_runs(strategy_version_id);
"""

# Migration from v9 -> v10: raw item discovery provenance / tombstones.
_MIGRATION_V10_SQL = """\
CREATE TABLE IF NOT EXISTS raw_item_discoveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id         INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    owner_type          TEXT NOT NULL,
    owner_id            INTEGER,
    owner_label         TEXT NOT NULL DEFAULT '',
    owner_run_id        INTEGER,
    exploration_id      INTEGER,
    strategy_version    INTEGER,
    run_at              TEXT,
    discovered_at       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    deleted_at          TEXT,
    deleted_owner_id    INTEGER,
    deleted_owner_label TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_raw_item_discoveries_raw_item
    ON raw_item_discoveries(raw_item_id);
CREATE INDEX IF NOT EXISTS idx_raw_item_discoveries_owner
    ON raw_item_discoveries(owner_type, owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_item_discoveries_active_owner
    ON raw_item_discoveries(raw_item_id, owner_type, owner_id)
    WHERE deleted_at IS NULL AND owner_id IS NOT NULL;
"""

# Migration from v10 -> v11: preserve structured context gathered with a raw item.
_MIGRATION_V11_SQL = """\
ALTER TABLE raw_items ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
"""

# Migration from v11 -> v12: preserve richer per-Lens analysis for briefing synthesis.
_MIGRATION_V12_SQL = """\
ALTER TABLE raw_item_lenses ADD COLUMN enriched_json TEXT NOT NULL DEFAULT '{}';
"""

# Migration from v12 -> v13: structured digest rows for web and DB-only saved items.
_MIGRATION_V13_SQL = """\
CREATE TABLE IF NOT EXISTS digest_section_items (
    digest_id       INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
    section_index   INTEGER NOT NULL,
    lens_id         TEXT NOT NULL DEFAULT '',
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (digest_id, section_index, raw_item_id)
);

CREATE TABLE IF NOT EXISTS saved_items (
    raw_item_id     INTEGER NOT NULL PRIMARY KEY REFERENCES raw_items(id) ON DELETE CASCADE,
    saved_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at);
CREATE INDEX IF NOT EXISTS idx_digest_section_items_raw_item
    ON digest_section_items(raw_item_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_saved_at ON saved_items(saved_at);
"""

# Migration from v13 -> v14: channel-neutral delivery and interaction state.
_MIGRATION_V14_SQL = """\
CREATE TABLE IF NOT EXISTS outbound_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    workspace_id    TEXT NOT NULL DEFAULT '',
    channel_id      TEXT NOT NULL DEFAULT '',
    thread_id       TEXT NOT NULL DEFAULT '',
    message_id      TEXT NOT NULL,
    payload_type    TEXT NOT NULL DEFAULT 'raw_item',
    raw_item_id     INTEGER REFERENCES raw_items(id) ON DELETE SET NULL,
    digest_id       INTEGER REFERENCES digests(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(platform, workspace_id, channel_id, thread_id, message_id)
);

CREATE TABLE IF NOT EXISTS interaction_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    event_id        TEXT NOT NULL DEFAULT '',
    workspace_id    TEXT NOT NULL DEFAULT '',
    channel_id      TEXT NOT NULL DEFAULT '',
    message_id      TEXT NOT NULL DEFAULT '',
    user_id         TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,
    value           TEXT NOT NULL DEFAULT '',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    handled_action  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'received',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbound_messages_lookup
    ON outbound_messages(platform, workspace_id, channel_id, thread_id, message_id);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_raw_item
    ON outbound_messages(raw_item_id);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_digest
    ON outbound_messages(digest_id);
CREATE INDEX IF NOT EXISTS idx_interaction_events_message
    ON interaction_events(platform, workspace_id, channel_id, message_id);
CREATE INDEX IF NOT EXISTS idx_interaction_events_created_at
    ON interaction_events(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_events_event_id
    ON interaction_events(platform, event_id)
    WHERE event_id != '';
"""

# Migration from v14 → v15: engagement signal ledger + optional embeddings
_MIGRATION_V15_SQL = """\
CREATE TABLE IF NOT EXISTS item_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    signal          TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT '',
    dedup_key       TEXT NOT NULL DEFAULT '',
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_embeddings (
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    vector          BLOB NOT NULL,
    text_hash       TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (raw_item_id, model)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_item_signals_dedup
    ON item_signals(raw_item_id, signal, dedup_key)
    WHERE dedup_key != '';
CREATE INDEX IF NOT EXISTS idx_item_signals_item
    ON item_signals(raw_item_id, created_at);
CREATE INDEX IF NOT EXISTS idx_item_signals_created_at
    ON item_signals(created_at);
"""


def _backfill_item_signals(conn: sqlite3.Connection) -> None:
    """Seed item_signals from current state tables (v15 backfill).

    State tables are the deduplicated truth, so only they are used —
    replaying interaction_events as well would double-count saves.
    INSERT OR IGNORE + the dedup unique index make re-runs no-ops.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO item_signals
            (raw_item_id, signal, source, dedup_key, created_at)
        SELECT raw_item_id, 'save', 'backfill', 'backfill:saved_items', saved_at
        FROM saved_items
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO item_signals
            (raw_item_id, signal, source, dedup_key, created_at)
        SELECT id, 'promote', 'backfill', 'backfill:status', updated_at
        FROM raw_items WHERE status = 'promoted'
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO item_signals
            (raw_item_id, signal, source, dedup_key, created_at)
        SELECT id, 'archive', 'backfill', 'backfill:status', updated_at
        FROM raw_items WHERE status = 'archived'
        """
    )


# Migration from v1 → v2: add fetch_cursors table
_MIGRATION_V2_SQL = """\
CREATE TABLE IF NOT EXISTS fetch_cursors (
    subscription_url TEXT NOT NULL,
    cursor_type     TEXT NOT NULL,
    cursor_value    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (subscription_url, cursor_type)
);
CREATE INDEX IF NOT EXISTS idx_fetch_cursors_url ON fetch_cursors(subscription_url);
"""

# Migration from v2 → v3: add collected_tweets table
_MIGRATION_V3_SQL = """\
CREATE TABLE IF NOT EXISTS collected_tweets (
    tweet_id        TEXT NOT NULL PRIMARY KEY,
    subscription_url TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    collected_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collected_tweets_sub ON collected_tweets(subscription_url);
"""

# Migration from v3 → v4: add subscriptions, raw_items, lenses, raw_item_lenses, job_runs
_MIGRATION_V4_SQL = """\
CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT 'rss',
    status          TEXT NOT NULL DEFAULT 'active',
    schedule_cron   TEXT,
    schedule_interval_minutes INTEGER NOT NULL DEFAULT 30,
    default_lens_ids TEXT NOT NULL DEFAULT '[]',
    config          TEXT NOT NULL DEFAULT '{}',
    last_fetched_at TEXT,
    last_error      TEXT DEFAULT '',
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    origin          TEXT NOT NULL DEFAULT 'subscription',
    priority        INTEGER NOT NULL DEFAULT 50,
    status          TEXT NOT NULL DEFAULT 'raw',
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    content_summary TEXT DEFAULT '',
    collected_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lenses (
    id              TEXT NOT NULL PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '[]',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_item_lenses (
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    lens_id         TEXT NOT NULL REFERENCES lenses(id) ON DELETE CASCADE,
    PRIMARY KEY (raw_item_id, lens_id)
);

CREATE TABLE IF NOT EXISTS job_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    items_total     INTEGER NOT NULL DEFAULT 0,
    items_new       INTEGER NOT NULL DEFAULT 0,
    items_error     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_url ON subscriptions(url);
CREATE INDEX IF NOT EXISTS idx_raw_items_status ON raw_items(status);
CREATE INDEX IF NOT EXISTS idx_raw_items_origin ON raw_items(origin);
CREATE INDEX IF NOT EXISTS idx_raw_items_subscription ON raw_items(subscription_id);
CREATE INDEX IF NOT EXISTS idx_raw_items_url ON raw_items(url);
CREATE INDEX IF NOT EXISTS idx_raw_item_lenses_lens ON raw_item_lenses(lens_id);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);
"""


def _db_path(config: WikiConfig) -> Path:
    """Return the SQLite database file path."""
    return config.meta_path / "state.db"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _migrate_existing_telegram_mappings(conn: sqlite3.Connection) -> None:
    """Copy existing Telegram message mappings into outbound_messages."""
    if not _table_exists(conn, "telegram_raw_item_messages"):
        return
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(telegram_raw_item_messages)").fetchall()
    }
    required = {"chat_id", "message_id", "raw_item_id", "created_at"}
    if not required.issubset(columns):
        return
    thread_expr = "thread_id" if "thread_id" in columns else "''"
    conn.execute(
        f"""
        INSERT OR IGNORE INTO outbound_messages
            (platform, workspace_id, channel_id, thread_id, message_id,
             payload_type, raw_item_id, digest_id, created_at)
        SELECT
            'telegram',
            '',
            CAST(chat_id AS TEXT),
            CAST({thread_expr} AS TEXT),
            CAST(message_id AS TEXT),
            'raw_item',
            raw_item_id,
            NULL,
            created_at
        FROM telegram_raw_item_messages
        WHERE raw_item_id IS NOT NULL
        """
    )


# Migration ladder: (target_version, script, tolerate_duplicate_column, label).
# tolerate_duplicate_column covers re-runs of ALTER TABLE ... ADD COLUMN
# scripts against a DB that already has the column (partial upgrades).
_MIGRATIONS: tuple[tuple[int, str, bool, str], ...] = (
    (2, _MIGRATION_V2_SQL, False, "fetch_cursors"),
    (3, _MIGRATION_V3_SQL, False, "collected_tweets"),
    (4, _MIGRATION_V4_SQL, False,
     "subscriptions, raw_items, lenses, raw_item_lenses, job_runs"),
    (5, _MIGRATION_V5_SQL, True, "schedules.running"),
    (6, _MIGRATION_V6_SQL, False,
     "raw_items.body, raw_items.published_at, UNIQUE(sub_id, url)"),
    (7, _MIGRATION_V7_SQL, True,
     "raw_item_lenses.summary, raw_item_lenses.bullets_json"),
    (8, _MIGRATION_V8_SQL, True,
     "digests table, raw_items.digest_id, idx_raw_items_digest"),
    (9, _MIGRATION_V9_SQL, False, "exploration lifecycle tables"),
    (10, _MIGRATION_V10_SQL, False, "raw item discovery attribution"),
    (11, _MIGRATION_V11_SQL, True, "raw item metadata JSON"),
    (12, _MIGRATION_V12_SQL, True, "raw_item_lenses.enriched_json"),
    (13, _MIGRATION_V13_SQL, False,
     "digest section items, saved items, digest titles"),
    (14, _MIGRATION_V14_SQL, False, "outbound messages and interaction events"),
    (15, _MIGRATION_V15_SQL, False, "item signals ledger and embeddings"),
)

# Indexes over migration-added columns; created after the ladder so they
# tolerate missing columns during partial upgrades.
_POST_MIGRATION_INDEX_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_raw_items_published "
    "ON raw_items(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_raw_items_digest "
    "ON raw_items(digest_id)",
    "CREATE INDEX IF NOT EXISTS idx_digests_created_at "
    "ON digests(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_digest_section_items_raw_item "
    "ON digest_section_items(raw_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_saved_items_saved_at "
    "ON saved_items(saved_at)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_messages_lookup "
    "ON outbound_messages(platform, workspace_id, channel_id, thread_id, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_messages_raw_item "
    "ON outbound_messages(raw_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_messages_digest "
    "ON outbound_messages(digest_id)",
    "CREATE INDEX IF NOT EXISTS idx_interaction_events_message "
    "ON interaction_events(platform, workspace_id, channel_id, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_interaction_events_created_at "
    "ON interaction_events(created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_events_event_id "
    "ON interaction_events(platform, event_id) WHERE event_id != ''",
)


def _run_migrations(conn: sqlite3.Connection, current_version: int) -> int:
    """Apply every pending migration and return the resulting version."""
    for version, sql, tolerate_dup, label in _MIGRATIONS:
        if current_version >= version:
            continue
        if version == 13:
            # v13 pre-step: digests.title predates the script and may
            # already exist on partially upgraded DBs.
            try:
                conn.execute(
                    "ALTER TABLE digests "
                    "ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        try:
            conn.executescript(sql)
        except sqlite3.OperationalError as e:
            if not (tolerate_dup and "duplicate column name" in str(e).lower()):
                raise
        if version == 14:
            _migrate_existing_telegram_mappings(conn)
        if version == 15:
            _backfill_item_signals(conn)
        current_version = version
        logger.info("Migrated database to v%d (%s)", version, label)
    return current_version


def init_db(config: WikiConfig) -> sqlite3.Connection:
    """Open (or create) the SQLite database and apply schema.

    Returns:
        An open sqlite3.Connection with WAL mode and foreign keys enabled.
    """
    path = _db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")

    # Apply schema
    conn.executescript(_SCHEMA_SQL)

    # Track schema version and run migrations
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    else:
        current_version = _run_migrations(conn, row["version"])
        if current_version != row["version"]:
            conn.execute(
                "UPDATE schema_version SET version = ?",
                (SCHEMA_VERSION,),
            )
            conn.commit()

    for sql in _POST_MIGRATION_INDEX_SQL:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass

    logger.debug("Database initialized at %s (v%d)", path, SCHEMA_VERSION)
    return conn


# ---------------------------------------------------------------------------
# Fetch cursor helpers
# ---------------------------------------------------------------------------


@dataclass
class FetchCursor:
    """A fetch cursor for incremental collection.

    Stores the last known position for a subscription, enabling
    incremental fetches that only retrieve new items.

    Cursor types:
        since_id: The ID of the most recent item (e.g. tweet ID).
                  Used when the source supports ID-based pagination.
        since_timestamp: ISO 8601 timestamp of the most recent item.
                        Used as a fallback when IDs are unavailable.
    """

    subscription_url: str
    cursor_type: str  # 'since_id' or 'since_timestamp'
    cursor_value: str
    updated_at: datetime


def get_fetch_cursor(
    subscription_url: str,
    config: WikiConfig,
    *,
    cursor_type: str = "since_id",
    conn: sqlite3.Connection | None = None,
) -> FetchCursor | None:
    """Retrieve the fetch cursor for a subscription.

    Args:
        subscription_url: The subscription URL to look up.
        config: Wiki configuration.
        cursor_type: The cursor type to retrieve ('since_id' or 'since_timestamp').
        conn: Optional pre-opened DB connection.

    Returns:
        FetchCursor if found, None otherwise.
    """

    def _do(c: sqlite3.Connection) -> FetchCursor | None:
        row = c.execute(
            """SELECT subscription_url, cursor_type, cursor_value, updated_at
            FROM fetch_cursors
            WHERE subscription_url = ? AND cursor_type = ?""",
            (subscription_url, cursor_type),
        ).fetchone()
        if not row:
            return None
        updated_at = datetime.fromisoformat(row["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return FetchCursor(
            subscription_url=row["subscription_url"],
            cursor_type=row["cursor_type"],
            cursor_value=row["cursor_value"],
            updated_at=updated_at,
        )

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


def save_fetch_cursor(
    subscription_url: str,
    cursor_type: str,
    cursor_value: str,
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> FetchCursor:
    """Save or update a fetch cursor for a subscription.

    Uses UPSERT (INSERT OR REPLACE) to atomically create or update.

    Args:
        subscription_url: The subscription URL.
        cursor_type: Cursor type ('since_id' or 'since_timestamp').
        cursor_value: The cursor value (tweet ID or ISO timestamp).
        config: Wiki configuration.
        conn: Optional pre-opened DB connection.

    Returns:
        The saved FetchCursor.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    def _do(c: sqlite3.Connection) -> FetchCursor:
        c.execute(
            """INSERT OR REPLACE INTO fetch_cursors
                (subscription_url, cursor_type, cursor_value, updated_at)
            VALUES (?, ?, ?, ?)""",
            (subscription_url, cursor_type, cursor_value, now_iso),
        )
        c.commit()
        return FetchCursor(
            subscription_url=subscription_url,
            cursor_type=cursor_type,
            cursor_value=cursor_value,
            updated_at=now,
        )

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


def delete_fetch_cursors(
    subscription_url: str,
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Delete all fetch cursors for a subscription.

    Used when unsubscribing or resetting a subscription's state.

    Args:
        subscription_url: The subscription URL.
        config: Wiki configuration.
        conn: Optional pre-opened DB connection.

    Returns:
        Number of cursors deleted.
    """

    def _do(c: sqlite3.Connection) -> int:
        cursor = c.execute(
            "DELETE FROM fetch_cursors WHERE subscription_url = ?",
            (subscription_url,),
        )
        c.commit()
        return cursor.rowcount

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


def get_all_cursors(
    subscription_url: str,
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, FetchCursor]:
    """Get all cursor types for a subscription.

    Returns:
        Dict mapping cursor_type → FetchCursor.
    """

    def _do(c: sqlite3.Connection) -> dict[str, FetchCursor]:
        rows = c.execute(
            """SELECT subscription_url, cursor_type, cursor_value, updated_at
            FROM fetch_cursors
            WHERE subscription_url = ?""",
            (subscription_url,),
        ).fetchall()
        result = {}
        for row in rows:
            updated_at = datetime.fromisoformat(row["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            result[row["cursor_type"]] = FetchCursor(
                subscription_url=row["subscription_url"],
                cursor_type=row["cursor_type"],
                cursor_value=row["cursor_value"],
                updated_at=updated_at,
            )
        return result

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


# ---------------------------------------------------------------------------
# Collected tweets helpers (tweet ID deduplication)
# ---------------------------------------------------------------------------


@dataclass
class CollectedTweet:
    """Record of a collected tweet for deduplication.

    Each tweet ID is stored once (globally, across all subscriptions).
    This prevents duplicate source files even when:
    - The same tweet appears in multiple subscriptions
    - A cursor is reset and tweets are re-fetched
    - API pagination returns overlapping pages
    """

    tweet_id: str
    subscription_url: str
    source_file: str
    collected_at: datetime


def is_tweet_collected(
    tweet_id: str,
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Check if a tweet ID has already been collected.

    Args:
        tweet_id: The tweet ID to check.
        config: Wiki configuration.
        conn: Optional pre-opened DB connection.

    Returns:
        True if the tweet has been collected before.
    """
    if not tweet_id:
        return False

    def _do(c: sqlite3.Connection) -> bool:
        row = c.execute(
            "SELECT 1 FROM collected_tweets WHERE tweet_id = ?",
            (tweet_id,),
        ).fetchone()
        return row is not None

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


def filter_uncollected_tweet_ids(
    tweet_ids: list[str],
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> set[str]:
    """Filter a list of tweet IDs to only those not yet collected.

    Efficient batch query using SQLite IN clause.

    Args:
        tweet_ids: Tweet IDs to check.
        config: Wiki configuration.
        conn: Optional pre-opened DB connection.

    Returns:
        Set of tweet IDs that have NOT been collected yet.
    """
    if not tweet_ids:
        return set()

    def _do(c: sqlite3.Connection) -> set[str]:
        placeholders = ",".join("?" for _ in tweet_ids)
        rows = c.execute(
            f"SELECT tweet_id FROM collected_tweets WHERE tweet_id IN ({placeholders})",
            tweet_ids,
        ).fetchall()
        already_collected = {row["tweet_id"] for row in rows}
        return set(tweet_ids) - already_collected

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


def record_collected_tweet(
    tweet_id: str,
    subscription_url: str,
    source_file: str,
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> CollectedTweet:
    """Record a tweet as collected for future deduplication.

    Uses INSERT OR IGNORE to silently skip if already recorded
    (idempotent — safe to call multiple times for the same tweet).

    Args:
        tweet_id: The tweet's Snowflake ID.
        subscription_url: The subscription that collected this tweet.
        source_file: Relative path of the source file in the vault.
        config: Wiki configuration.
        conn: Optional pre-opened DB connection.

    Returns:
        The CollectedTweet record.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    def _do(c: sqlite3.Connection) -> CollectedTweet:
        c.execute(
            """INSERT OR IGNORE INTO collected_tweets
                (tweet_id, subscription_url, source_file, collected_at)
            VALUES (?, ?, ?, ?)""",
            (tweet_id, subscription_url, source_file, now_iso),
        )
        c.commit()
        return CollectedTweet(
            tweet_id=tweet_id,
            subscription_url=subscription_url,
            source_file=source_file,
            collected_at=now,
        )

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


def record_collected_tweets_batch(
    records: list[tuple[str, str, str]],
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Record multiple tweets as collected in a single transaction.

    Args:
        records: List of (tweet_id, subscription_url, source_file) tuples.
        config: Wiki configuration.
        conn: Optional pre-opened DB connection.

    Returns:
        Number of new records inserted (excludes already-recorded tweets).
    """
    if not records:
        return 0

    now_str = now_iso()

    def _do(c: sqlite3.Connection) -> int:
        rows_before = c.execute(
            "SELECT COUNT(*) FROM collected_tweets",
        ).fetchone()[0]
        c.executemany(
            """INSERT OR IGNORE INTO collected_tweets
                (tweet_id, subscription_url, source_file, collected_at)
            VALUES (?, ?, ?, ?)""",
            [(tid, url, sf, now_str) for tid, url, sf in records],
        )
        c.commit()
        rows_after = c.execute(
            "SELECT COUNT(*) FROM collected_tweets",
        ).fetchone()[0]
        return rows_after - rows_before

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


def delete_collected_tweets(
    subscription_url: str,
    config: WikiConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Delete all collected tweet records for a subscription.

    Used when unsubscribing or resetting a subscription's state.

    Args:
        subscription_url: The subscription URL.
        config: Wiki configuration.
        conn: Optional pre-opened DB connection.

    Returns:
        Number of records deleted.
    """

    def _do(c: sqlite3.Connection) -> int:
        cursor = c.execute(
            "DELETE FROM collected_tweets WHERE subscription_url = ?",
            (subscription_url,),
        )
        c.commit()
        return cursor.rowcount

    if conn is not None:
        return _do(conn)
    with get_db(config) as c:
        return _do(c)


@contextmanager
def get_db(config: WikiConfig) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database access.

    Yields a connection that auto-commits on success, rolls back on error.
    """
    conn = init_db(config)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
