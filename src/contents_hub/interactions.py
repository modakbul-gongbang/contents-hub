"""Platform-neutral delivery mapping, interaction logging, and actions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.promote import PromoteError, promote_raw_item, source_filename
from contents_hub.signals import record_signal_conn
from contents_hub.timeutil import now_iso


SAVE_AND_PROMOTE_REACTIONS = {"👍", "⭐", "❤️", "❤"}
DEFAULT_REACTION_RULES = {
    "👍": "save_and_promote",
    "⭐": "save_and_promote",
    "❤️": "save_and_promote",
    "❤": "save_and_promote",
    "✅": "mark_read",
    "🗑": "archive",
}

# Slack (and Discord custom-emoji) events carry reaction *names* rather than
# emoji characters. Map the common names onto the emoji rule table so the
# same rules work on every platform.
REACTION_NAME_ALIASES = {
    "+1": "👍",
    "thumbsup": "👍",
    "thumbs_up": "👍",
    "star": "⭐",
    "star2": "⭐",
    "heart": "❤️",
    "heavy_black_heart": "❤",
    "white_check_mark": "✅",
    "heavy_check_mark": "✅",
    "ballot_box_with_check": "✅",
    "wastebasket": "🗑",
    "put_litter_in_its_place": "🗑",
}

_VARIATION_SELECTOR = "️"
# Fitzpatrick skin-tone modifiers (U+1F3FB..U+1F3FF) — Discord/Telegram send
# e.g. 👍🏽 as the base emoji plus a modifier codepoint.
_SKIN_TONE_MODIFIERS = "".join(chr(cp) for cp in range(0x1F3FB, 0x1F400))


def _normalize_reaction(value: str) -> str:
    """Normalize a reaction value: strip :colons:, Slack ``::skin-tone-N``
    suffixes, and Unicode skin-tone modifiers; map names; drop U+FE0F."""
    token = str(value or "").strip().strip(":")
    token = token.split("::", 1)[0]
    token = REACTION_NAME_ALIASES.get(token.lower(), token)
    token = token.replace(_VARIATION_SELECTOR, "")
    return "".join(ch for ch in token if ch not in _SKIN_TONE_MODIFIERS)


_NORMALIZED_REACTION_RULES = {
    _normalize_reaction(key): action for key, action in DEFAULT_REACTION_RULES.items()
}


def resolve_reaction_action(value: str) -> str:
    """Return the action for a reaction value/name, or '' when unmapped."""
    return _NORMALIZED_REACTION_RULES.get(_normalize_reaction(value), "")


@dataclass(frozen=True)
class NormalizedInteraction:
    platform: str
    event_id: str = ""
    workspace_id: str = ""
    channel_id: str = ""
    thread_id: str = ""
    message_id: str = ""
    user_id: str = ""
    kind: str = "reaction"
    value: str = ""
    raw_payload: dict[str, Any] | None = None


def _now() -> str:
    return now_iso()


def _json_object(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return json.dumps({"raw": value}, ensure_ascii=False)
        return json.dumps(parsed if isinstance(parsed, dict) else {"raw": parsed}, ensure_ascii=False)
    return json.dumps(value if isinstance(value, dict) else {"raw": value}, ensure_ascii=False)


def _clean(raw: Any) -> str:
    return str(raw or "").strip()


def normalize_interaction_event(data: dict[str, Any]) -> NormalizedInteraction:
    """Normalize adapter input into the contents-hub interaction shape."""
    raw_payload = data.get("raw_payload")
    if raw_payload is None:
        raw_payload = data.get("raw_payload_json")
    return NormalizedInteraction(
        platform=_clean(data.get("platform")),
        event_id=_clean(data.get("event_id")),
        workspace_id=_clean(data.get("workspace_id")),
        channel_id=_clean(data.get("channel_id")),
        thread_id=_clean(data.get("thread_id")),
        message_id=_clean(data.get("message_id")),
        user_id=_clean(data.get("user_id")),
        kind=_clean(data.get("kind") or "reaction"),
        value=_clean(data.get("value")),
        raw_payload=raw_payload if isinstance(raw_payload, dict) else data,
    )


def interaction_rules_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "rules": dict(DEFAULT_REACTION_RULES),
        "name_aliases": dict(REACTION_NAME_ALIASES),
    }


def record_outbound_message(
    config: WikiConfig,
    *,
    platform: str,
    message_id: str,
    workspace_id: str = "",
    channel_id: str = "",
    thread_id: str = "",
    payload_type: str = "raw_item",
    raw_item_id: int | None = None,
    digest_id: int | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Record a channel message id returned by an adapter."""
    platform = _clean(platform)
    message_id = _clean(message_id)
    workspace_id = _clean(workspace_id)
    channel_id = _clean(channel_id)
    thread_id = _clean(thread_id)
    payload_type = _clean(payload_type or "raw_item")
    if not platform:
        raise ValueError("platform is required")
    if not message_id:
        raise ValueError("message_id is required")
    if payload_type not in {"raw_item", "digest"}:
        raise ValueError("payload_type must be raw_item or digest")
    if payload_type == "raw_item" and raw_item_id is None:
        raise ValueError("raw_item_id is required for raw_item payloads")
    if payload_type == "digest" and digest_id is None:
        raise ValueError("digest_id is required for digest payloads")

    with get_db(config) as conn:
        if raw_item_id is not None:
            row = conn.execute("SELECT id FROM raw_items WHERE id=?", (int(raw_item_id),)).fetchone()
            if row is None:
                raise ValueError(f"raw_item {raw_item_id} not found")
        if digest_id is not None:
            row = conn.execute("SELECT id FROM digests WHERE id=?", (int(digest_id),)).fetchone()
            if row is None:
                raise ValueError(f"digest {digest_id} not found")
        conn.execute(
            """
            INSERT OR REPLACE INTO outbound_messages
                (platform, workspace_id, channel_id, thread_id, message_id,
                 payload_type, raw_item_id, digest_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                workspace_id,
                channel_id,
                thread_id,
                message_id,
                payload_type,
                raw_item_id,
                digest_id,
                created_at or _now(),
            ),
        )
        if raw_item_id is not None:
            # Exposure signal for the engagement ledger; the platform-scoped
            # outbound key makes re-recording the same message replay-safe
            # without colliding across platforms whose ids overlap.
            record_signal_conn(
                conn,
                raw_item_id=int(raw_item_id),
                signal="impression",
                source=platform,
                dedup_key=f"om:{platform}:{workspace_id}:{channel_id}:{thread_id}:{message_id}",
            )
        row = conn.execute(
            """
            SELECT * FROM outbound_messages
            WHERE platform=? AND workspace_id=? AND channel_id=?
              AND thread_id=? AND message_id=?
            """,
            (platform, workspace_id, channel_id, thread_id, message_id),
        ).fetchone()
    return {"ok": True, "outbound_message": _row_to_dict(row)}


def list_outbound_messages(
    config: WikiConfig,
    *,
    platform: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit or 50)))
    with get_db(config) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM outbound_messages
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return {"ok": True, "outbound_messages": [_row_to_dict(row) for row in rows]}


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _find_outbound(conn: sqlite3.Connection, event: NormalizedInteraction) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM outbound_messages
        WHERE platform = ?
          AND workspace_id = ?
          AND channel_id = ?
          AND thread_id = ?
          AND message_id = ?
        """,
        (
            event.platform,
            event.workspace_id,
            event.channel_id,
            event.thread_id,
            event.message_id,
        ),
    ).fetchone()


def _existing_event(conn: sqlite3.Connection, event: NormalizedInteraction) -> sqlite3.Row | None:
    if not event.event_id:
        return None
    return conn.execute(
        "SELECT * FROM interaction_events WHERE platform=? AND event_id=?",
        (event.platform, event.event_id),
    ).fetchone()


def _insert_event(
    conn: sqlite3.Connection,
    event: NormalizedInteraction,
    *,
    action: str,
    status: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO interaction_events
            (platform, event_id, workspace_id, channel_id, message_id, user_id,
             kind, value, raw_payload_json, handled_action, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.platform,
            event.event_id,
            event.workspace_id,
            event.channel_id,
            event.message_id,
            event.user_id,
            event.kind,
            event.value,
            _json_object(event.raw_payload),
            action,
            status,
            _now(),
        ),
    )
    return int(cur.lastrowid)


def _update_event_status(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    action: str,
    status: str,
) -> None:
    conn.execute(
        "UPDATE interaction_events SET handled_action=?, status=? WHERE id=?",
        (action, status, event_id),
    )


def _saved_insert(conn: sqlite3.Connection, raw_item_id: int) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO saved_items(raw_item_id, saved_at) VALUES (?, ?)",
        (raw_item_id, _now()),
    )
    return cur.rowcount > 0


def _promoted_path(config: WikiConfig, conn: sqlite3.Connection, raw_item_id: int) -> Path | None:
    row = conn.execute("SELECT * FROM raw_items WHERE id=?", (raw_item_id,)).fetchone()
    if row is None:
        return None
    return config.sources_path / source_filename(
        row["title"] or row["url"],
        row["url"],
        row["collected_at"],
    )


def _save_action(config: WikiConfig, conn: sqlite3.Connection, raw_item_id: int) -> dict[str, Any]:
    saved = _saved_insert(conn, raw_item_id)
    return {"ok": True, "status": "handled", "saved": True, "inserted": saved}


def _save_and_promote_action(
    config: WikiConfig,
    conn: sqlite3.Connection,
    raw_item_id: int,
) -> dict[str, Any]:
    row = conn.execute("SELECT id, status FROM raw_items WHERE id=?", (raw_item_id,)).fetchone()
    if row is None:
        return {"ok": False, "status": "not_found", "error": "raw_item_not_found"}
    _saved_insert(conn, raw_item_id)
    # Known limitation: promote_raw_item commits on its own connection, so a
    # crash between the mid-action commit below and the caller's final commit
    # can leave a promoted note whose event row stays 'received'. The event-id
    # replay guard then blocks re-running it; recover via `item signal`.
    if row["status"] == "promoted":
        path = _promoted_path(config, conn, raw_item_id)
        return {
            "ok": True,
            "status": "handled",
            "saved": True,
            "promoted": False,
            "noop": True,
            "reason": "already_promoted",
            "path": str(path.relative_to(config.vault_path)) if path else None,
        }
    conn.commit()
    try:
        path = promote_raw_item(config, raw_item_id)
    except PromoteError as exc:
        if "already promoted" in str(exc).lower():
            with get_db(config) as fresh_conn:
                existing = _promoted_path(config, fresh_conn, raw_item_id)
            return {
                "ok": True,
                "status": "handled",
                "saved": True,
                "promoted": False,
                "noop": True,
                "reason": "already_promoted",
                "path": str(existing.relative_to(config.vault_path)) if existing else None,
            }
        return {"ok": False, "status": "failed", "error": str(exc), "saved": True}
    return {
        "ok": True,
        "status": "handled",
        "saved": True,
        "promoted": True,
        "path": str(path.relative_to(config.vault_path)),
    }


def _archive_action(conn: sqlite3.Connection, raw_item_id: int, *, action_name: str) -> dict[str, Any]:
    row = conn.execute("SELECT id, status FROM raw_items WHERE id=?", (raw_item_id,)).fetchone()
    if row is None:
        return {"ok": False, "status": "not_found", "error": "raw_item_not_found"}
    current = row["status"] or ""
    if current == "promoted":
        return {
            "ok": False,
            "status": "failed",
            "error": "promoted items cannot be archived",
            "raw_item_status": current,
        }
    if current == "archived":
        return {"ok": True, "status": "handled", "noop": True, "raw_item_status": current}
    if current != "raw":
        return {
            "ok": False,
            "status": "failed",
            "error": f"cannot {action_name} item in status {current!r}",
            "raw_item_status": current,
        }
    conn.execute(
        "UPDATE raw_items SET status='archived', updated_at=? WHERE id=?",
        (_now(), raw_item_id),
    )
    return {"ok": True, "status": "handled", "raw_item_status": "archived"}


def _handle_action(
    config: WikiConfig,
    conn: sqlite3.Connection,
    *,
    action: str,
    raw_item_id: int | None,
) -> dict[str, Any]:
    if raw_item_id is None:
        return {"ok": False, "status": "failed", "error": "raw_item_id_required"}
    raw_item_id = int(raw_item_id)
    if action == "save":
        return _save_action(config, conn, raw_item_id)
    if action == "save_and_promote":
        return _save_and_promote_action(config, conn, raw_item_id)
    if action == "archive":
        return _archive_action(conn, raw_item_id, action_name="archive")
    if action == "mark_read":
        return _archive_action(conn, raw_item_id, action_name="mark_read")
    return {"ok": False, "status": "ignored", "noop": True, "error": "unsupported_action"}


_ACTION_SIGNALS = {
    "save": ("save",),
    "save_and_promote": ("save",),
    "mark_read": ("read",),
    "archive": ("archive",),
}


def _record_action_signals(
    conn: sqlite3.Connection,
    *,
    event: NormalizedInteraction,
    action: str,
    raw_item_id: int,
    promoted: bool,
) -> None:
    """Mirror a handled interaction into the engagement signal ledger.

    The dedup key is platform:message:user:reaction — deliberately NOT the
    platform event id, so removing and re-adding the same reaction (which
    mints a fresh event id every time) cannot inflate engagement, and two
    users reacting to the same message count separately.
    """
    dedup_base = (
        f"{event.platform}:{event.message_id}:{event.user_id}:{event.value}"
    )
    signals = list(_ACTION_SIGNALS.get(action, ()))
    if promoted:
        signals.append("promote")
    for signal in signals:
        record_signal_conn(
            conn,
            raw_item_id=raw_item_id,
            signal=signal,
            source=event.platform,
            dedup_key=f"ie:{dedup_base}",
        )


def handle_interaction(config: WikiConfig, data: dict[str, Any]) -> dict[str, Any]:
    """Log and handle one normalized interaction event."""
    event = normalize_interaction_event(data)
    if not event.platform:
        raise ValueError("platform is required")
    if not event.message_id:
        raise ValueError("message_id is required")

    action = resolve_reaction_action(event.value) if event.kind == "reaction" else ""

    with get_db(config) as conn:
        existing = _existing_event(conn, event)
        if existing is not None:
            return {
                "ok": True,
                "status": "already_handled",
                "noop": True,
                "interaction_event": _row_to_dict(existing),
                "action": existing["handled_action"] or "",
            }

        outbound = _find_outbound(conn, event)
        initial_status = "ignored" if not action else "received"
        event_row_id = _insert_event(conn, event, action=action, status=initial_status)

        if not action:
            conn.commit()
            return {
                "ok": True,
                "status": "ignored",
                "noop": True,
                "action": "",
                "interaction_event_id": event_row_id,
            }
        if outbound is None:
            _update_event_status(conn, event_row_id, action=action, status="not_found")
            conn.commit()
            return {
                "ok": False,
                "status": "not_found",
                "action": action,
                "error": "outbound_message_not_found",
                "interaction_event_id": event_row_id,
            }

        raw_item_id = outbound["raw_item_id"]
        result = _handle_action(config, conn, action=action, raw_item_id=raw_item_id)
        _update_event_status(conn, event_row_id, action=action, status=str(result.get("status") or "handled"))
        if result.get("ok") and raw_item_id is not None:
            _record_action_signals(
                conn,
                event=event,
                action=action,
                raw_item_id=int(raw_item_id),
                promoted=bool(result.get("promoted")),
            )
        conn.commit()

    return {
        **result,
        "action": action,
        "raw_item_id": raw_item_id,
        "digest_id": outbound["digest_id"],
        "interaction_event_id": event_row_id,
    }
