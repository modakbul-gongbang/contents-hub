"""Engagement signal ledger and time-decayed scoring.

Signals record how much the user cared about a collected item, in layers:
``impression`` (delivered) < ``open`` (clicked) < ``read`` < ``save`` <
``promote``, with ``archive``/``unsave`` as negative signals. The ledger is
append-only; rows with a non-empty ``dedup_key`` are idempotent via a unique
index, so replaying the same platform event or backfill never double-counts.

Scoring: ``engagement(item) = Σ weight(signal) * 0.5 ** (age_days / H)``
with half-life ``H`` (default 90 days), clamped at a floor so archives
suppress rather than bury keyword-relevant results.

Defaults are overridable from the ``query:`` block of ``.contents-hub.yaml``:

```yaml
query:
  engagement_half_life_days: 90
  engagement_floor: -3
  signal_weights:
    open: 1.5
```
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.timeutil import now_iso

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    "impression": 0.0,
    "open": 1.0,
    "read": 0.5,
    "save": 3.0,
    "promote": 5.0,
    "note": 2.0,
    "unsave": -3.0,
    "archive": -2.0,
}
VALID_SIGNALS = frozenset(DEFAULT_SIGNAL_WEIGHTS)
DEFAULT_ENGAGEMENT_HALF_LIFE_DAYS = 90.0
DEFAULT_ENGAGEMENT_FLOOR = -3.0


def query_config(config: WikiConfig) -> dict[str, Any]:
    """Read the optional ``query:`` block from ``.contents-hub.yaml``."""
    path = config.vault_path / ".contents-hub.yaml"
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - config is advisory, never fatal
        logger.warning("could not read query config from %s: %s", path, exc)
        return {}
    block = raw.get("query") if isinstance(raw, dict) else None
    return block if isinstance(block, dict) else {}


def signal_weights(config: WikiConfig) -> dict[str, float]:
    weights = dict(DEFAULT_SIGNAL_WEIGHTS)
    overrides = query_config(config).get("signal_weights")
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in weights:
                try:
                    weights[key] = float(value)
                except (TypeError, ValueError):
                    logger.warning("ignoring non-numeric signal weight %r=%r", key, value)
    return weights


def engagement_half_life_days(config: WikiConfig) -> float:
    raw = query_config(config).get("engagement_half_life_days")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ENGAGEMENT_HALF_LIFE_DAYS
    return value if value > 0 else DEFAULT_ENGAGEMENT_HALF_LIFE_DAYS


def engagement_floor(config: WikiConfig) -> float:
    raw = query_config(config).get("engagement_floor")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ENGAGEMENT_FLOOR


def resolve_raw_item_id(conn: sqlite3.Connection, id_or_url: str) -> int | None:
    """Resolve a raw item by numeric id or by URL (newest row wins)."""
    token = str(id_or_url or "").strip()
    if not token:
        return None
    if token.isdigit():
        row = conn.execute("SELECT id FROM raw_items WHERE id=?", (int(token),)).fetchone()
        return int(row["id"]) if row else None
    row = conn.execute(
        "SELECT id FROM raw_items WHERE url=? ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    return int(row["id"]) if row else None


def record_signal_conn(
    conn: sqlite3.Connection,
    *,
    raw_item_id: int,
    signal: str,
    source: str = "",
    dedup_key: str = "",
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> bool:
    """Insert one signal row inside an existing transaction.

    Returns True if a row was inserted, False when the dedup key already
    exists (replay) — callers treat both as success.
    """
    signal = str(signal or "").strip()
    if signal not in VALID_SIGNALS:
        raise ValueError(
            f"invalid signal {signal!r}; expected one of {sorted(VALID_SIGNALS)}"
        )
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO item_signals
            (raw_item_id, signal, source, dedup_key, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(raw_item_id),
            signal,
            str(source or "").strip(),
            str(dedup_key or "").strip(),
            json.dumps(metadata or {}, ensure_ascii=False),
            created_at or now_iso(),
        ),
    )
    return cur.rowcount > 0


def record_signal(
    config: WikiConfig,
    *,
    id_or_url: str,
    signal: str,
    source: str = "cli",
    dedup_key: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CLI-facing signal insert; resolves the item and commits."""
    with get_db(config) as conn:
        raw_item_id = resolve_raw_item_id(conn, id_or_url)
        if raw_item_id is None:
            return {"ok": False, "error": "raw_item_not_found", "id_or_url": id_or_url}
        inserted = record_signal_conn(
            conn,
            raw_item_id=raw_item_id,
            signal=signal,
            source=source,
            dedup_key=dedup_key,
            metadata=metadata,
        )
        conn.commit()
    return {
        "ok": True,
        "raw_item_id": raw_item_id,
        "signal": signal,
        "source": source,
        "inserted": inserted,
        "deduped": not inserted,
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def decayed_weight(
    weight: float,
    created_at: str | None,
    *,
    now: datetime,
    half_life_days: float,
) -> float:
    created = _parse_iso(created_at)
    if created is None:
        return weight
    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    return weight * 0.5 ** (age_days / half_life_days)


def engagement_scores(
    config: WikiConfig,
    conn: sqlite3.Connection,
    raw_item_ids: list[int] | None = None,
    *,
    now: datetime | None = None,
) -> dict[int, float]:
    """Time-decayed engagement score per item.

    With ``raw_item_ids=None`` every item that has signals is scored.
    """
    now = now or datetime.now(timezone.utc)
    weights = signal_weights(config)
    half_life = engagement_half_life_days(config)
    floor = engagement_floor(config)

    if raw_item_ids is not None:
        if not raw_item_ids:
            return {}
        placeholders = ",".join("?" for _ in raw_item_ids)
        rows = conn.execute(
            f"SELECT raw_item_id, signal, created_at FROM item_signals "
            f"WHERE raw_item_id IN ({placeholders})",
            tuple(int(i) for i in raw_item_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT raw_item_id, signal, created_at FROM item_signals"
        ).fetchall()

    scores: dict[int, float] = {}
    for row in rows:
        weight = weights.get(row["signal"], 0.0)
        if weight == 0.0:
            continue
        item_id = int(row["raw_item_id"])
        scores[item_id] = scores.get(item_id, 0.0) + decayed_weight(
            weight, row["created_at"], now=now, half_life_days=half_life
        )
    return {item_id: max(floor, score) for item_id, score in scores.items()}


def signals_payload(config: WikiConfig, id_or_url: str) -> dict[str, Any]:
    """Ledger + summary + current engagement score for one item."""
    with get_db(config) as conn:
        raw_item_id = resolve_raw_item_id(conn, id_or_url)
        if raw_item_id is None:
            return {"ok": False, "error": "raw_item_not_found", "id_or_url": id_or_url}
        rows = conn.execute(
            "SELECT signal, source, dedup_key, metadata_json, created_at "
            "FROM item_signals WHERE raw_item_id=? ORDER BY created_at ASC, id ASC",
            (raw_item_id,),
        ).fetchall()
        score = engagement_scores(config, conn, [raw_item_id]).get(raw_item_id, 0.0)

    summary: dict[str, int] = {}
    ledger = []
    for row in rows:
        summary[row["signal"]] = summary.get(row["signal"], 0) + 1
        ledger.append(
            {
                "signal": row["signal"],
                "source": row["source"],
                "dedup_key": row["dedup_key"],
                "created_at": row["created_at"],
            }
        )
    return {
        "ok": True,
        "raw_item_id": raw_item_id,
        "engagement": round(score, 4),
        "signals": summary,
        "ledger": ledger,
    }


def open_item(
    config: WikiConfig,
    id_or_url: str,
    *,
    source: str = "cli",
    launch: bool = True,
) -> dict[str, Any]:
    """Record an ``open`` signal and optionally launch the item URL.

    Repeat opens from the same source on the same day dedup to one signal.
    """
    with get_db(config) as conn:
        raw_item_id = resolve_raw_item_id(conn, id_or_url)
        if raw_item_id is None:
            return {"ok": False, "error": "raw_item_not_found", "id_or_url": id_or_url}
        row = conn.execute(
            "SELECT url, title FROM raw_items WHERE id=?", (raw_item_id,)
        ).fetchone()
        url = row["url"] or ""
        stamp = now_iso()
        inserted = record_signal_conn(
            conn,
            raw_item_id=raw_item_id,
            signal="open",
            source=source,
            dedup_key=f"{source}:{stamp[:10]}",
            created_at=stamp,
        )
        conn.commit()

    launched = False
    if launch and url and not url.startswith("content://"):
        try:
            import webbrowser

            launched = bool(webbrowser.open(url))
        except Exception as exc:  # noqa: BLE001 - opening a browser is best-effort
            logger.warning("could not launch browser for %s: %s", url, exc)
    return {
        "ok": True,
        "raw_item_id": raw_item_id,
        "url": url,
        "title": row["title"] or "",
        "inserted": inserted,
        "launched": launched,
    }


__all__ = [
    "DEFAULT_SIGNAL_WEIGHTS",
    "VALID_SIGNALS",
    "engagement_scores",
    "query_config",
    "open_item",
    "record_signal",
    "record_signal_conn",
    "resolve_raw_item_id",
    "signal_weights",
    "signals_payload",
]
