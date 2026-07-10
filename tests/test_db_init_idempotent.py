"""init_db must be safe to run repeatedly on the same vault.

The schema/migration ladder in contents_hub.db is inline and versioned;
re-initializing an existing vault (every CLI invocation does this) must
neither error nor lose data.
"""

from __future__ import annotations

import sqlite3

from contents_hub.config import WikiConfig
from contents_hub.db import get_db, init_db


def _make_cfg(tmp_path) -> WikiConfig:
    cfg = WikiConfig(vault_path=tmp_path)
    (tmp_path / ".contents-hub").mkdir(parents=True, exist_ok=True)
    return cfg


def test_init_db_twice_is_idempotent(tmp_path):
    cfg = _make_cfg(tmp_path)
    init_db(cfg)
    init_db(cfg)  # must not raise on existing schema

    with get_db(cfg) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for expected in ("subscriptions", "raw_items", "lenses", "digests"):
        assert expected in tables, f"missing table {expected}"


def test_init_db_preserves_existing_rows(tmp_path):
    cfg = _make_cfg(tmp_path)
    init_db(cfg)
    db_path = cfg.meta_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO subscriptions "
            "(url, title, source_type, status, created_at, updated_at) "
            "VALUES ('https://example.com/feed', 'E', 'rss.feed', 'active', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()

    init_db(cfg)  # re-running migrations must not drop user data

    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    assert n == 1
