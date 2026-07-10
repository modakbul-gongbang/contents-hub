"""Delivery payload generation for channel adapters."""

from __future__ import annotations

import json
from typing import Any

from contents_hub.config import WikiConfig
from contents_hub.db import get_db


def _loads_json_array(raw: str) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _join_card_parts(parts: list[str]) -> str:
    return "\n\n".join(part for part in parts if part)


# Adapters must relay generated content as-is; re-summarizing lens output
# defeats the Lens layer. Included verbatim in every payload.
DELIVERY_INSTRUCTION = (
    "Send each card's markdown (or plain_text) verbatim. Do not summarize, "
    "rewrite, merge, or reorder cards; the lens content is the deliverable."
)


def _lens_sections(title: str, url: str, lenses: list[dict[str, Any]], *, markdown: bool) -> str:
    """Render a card body from verbatim lens output, one block per Lens."""
    heading = f"**{title}**" if (markdown and title) else title
    parts: list[str] = [heading]
    for lens in lenses:
        label = lens.get("name") or lens.get("lens_id") or ""
        block_lines: list[str] = []
        summary = (lens.get("summary") or "").strip()
        header = f"[{label}]" if label else ""
        if summary:
            block_lines.append(f"{header} {summary}".strip())
        elif header:
            block_lines.append(header)
        for bullet in lens.get("bullets") or []:
            bullet_text = str(bullet).strip()
            if bullet_text:
                block_lines.append(f"- {bullet_text}")
        if block_lines:
            parts.append("\n".join(block_lines))
    parts.append(url)
    return _join_card_parts(parts)


def _raw_item_card(row, lenses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    raw_item_id = int(row["id"])
    title = row["title"] or row["url"]
    url = row["url"] or ""
    summary = row["content_summary"] or row["body"] or ""
    lenses = lenses or []
    has_lens_content = any(
        (lens.get("summary") or "").strip() or lens.get("bullets") for lens in lenses
    )
    if has_lens_content:
        plain_text = _lens_sections(title, url, lenses, markdown=False)
        markdown = _lens_sections(title, url, lenses, markdown=True)
    else:
        plain_text = _join_card_parts([title, summary, url])
        markdown = _join_card_parts([f"**{title}**" if title else "", summary, url])
    return {
        "payload_type": "raw_item",
        "raw_item_id": raw_item_id,
        "digest_id": None,
        "delivery_key": f"raw_item:{raw_item_id}",
        "dedupe_key": f"url:{url}" if url else f"raw_item:{raw_item_id}",
        "title": title,
        "url": url,
        "summary": summary,
        "plain_text": plain_text,
        "markdown": markdown,
        "source_type": row["source_type"] or "",
        "origin": row["origin"] or "",
        "status": row["status"] or "",
        "lens_ids": [lens["lens_id"] for lens in lenses],
        "lenses": lenses,
        "collected_at": row["collected_at"],
        "published_at": row["published_at"],
    }


def _digest_card(row) -> dict[str, Any]:
    sections = _loads_json_array(row["sections_json"] or "[]")
    content_md = row["content_md"] or ""
    return {
        "payload_type": "digest",
        "raw_item_id": None,
        "digest_id": int(row["id"]),
        "delivery_key": f"digest:{row['id']}",
        "dedupe_key": f"digest:{row['id']}",
        "title": row["title"] or f"Digest #{row['id']}",
        "item_count": int(row["item_count"] or 0),
        "created_at": row["created_at"],
        "status": row["status"] or "",
        "summary": content_md[:1000],
        # Full generated digest — adapters send this verbatim, never a re-summary.
        "content_md": content_md,
        "markdown": content_md,
        "plain_text": content_md,
        "sections": sections,
    }


def _lenses_by_raw_item(conn, raw_item_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Per-item verbatim lens output: id, display name, summary, bullets."""
    if not raw_item_ids:
        return {}
    placeholders = ",".join("?" for _ in raw_item_ids)
    rows = conn.execute(
        f"""
        SELECT ril.raw_item_id, ril.lens_id, ril.summary, ril.bullets_json,
               COALESCE(l.name, '') AS lens_name
        FROM raw_item_lenses ril
        LEFT JOIN lenses l ON l.id = ril.lens_id
        WHERE ril.raw_item_id IN ({placeholders})
        ORDER BY ril.raw_item_id, ril.lens_id
        """,
        tuple(raw_item_ids),
    ).fetchall()
    lenses: dict[int, list[dict[str, Any]]] = {raw_item_id: [] for raw_item_id in raw_item_ids}
    for row in rows:
        lenses.setdefault(int(row["raw_item_id"]), []).append(
            {
                "lens_id": row["lens_id"],
                "name": row["lens_name"] or row["lens_id"],
                "summary": row["summary"] or "",
                "bullets": [
                    str(bullet)
                    for bullet in _loads_json_array(row["bullets_json"] or "[]")
                ],
            }
        )
    return lenses


def _lens_groups(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group raw-item cards per Lens so adapters can deliver one lens at a time."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for card in cards:
        if card.get("payload_type") != "raw_item":
            continue
        lenses = card.get("lenses") or []
        if not lenses:
            key = ""
            if key not in groups:
                groups[key] = {"lens_id": "", "name": "unlensed", "raw_item_ids": []}
                order.append(key)
            groups[key]["raw_item_ids"].append(card["raw_item_id"])
            continue
        for lens in lenses:
            key = lens["lens_id"]
            if key not in groups:
                groups[key] = {
                    "lens_id": key,
                    "name": lens.get("name") or key,
                    "raw_item_ids": [],
                }
                order.append(key)
            groups[key]["raw_item_ids"].append(card["raw_item_id"])
    return [
        {**groups[key], "count": len(groups[key]["raw_item_ids"])} for key in order
    ]


def delivery_payload(
    config: WikiConfig,
    *,
    payload_type: str = "all",
    limit: int = 20,
    origin: str | None = None,
    lens_matched: bool = False,
    first_seen_only: bool = False,
) -> dict[str, Any]:
    """Return adapter-ready cards for undelivered raw items and digests."""
    payload_type = (payload_type or "all").strip().lower()
    if payload_type not in {"all", "raw_item", "digest"}:
        raise ValueError("payload_type must be one of: all, raw_item, digest")
    limit = max(1, int(limit or 20))
    origin = (origin or "").strip() or None

    cards: list[dict[str, Any]] = []
    with get_db(config) as conn:
        if payload_type in {"all", "raw_item"}:
            clauses = [
                "ri.status = 'raw'",
                """
                NOT EXISTS (
                    SELECT 1 FROM outbound_messages om
                    WHERE om.payload_type = 'raw_item'
                      AND om.raw_item_id = ri.id
                )
                """,
            ]
            params: list[Any] = []
            if origin:
                clauses.append("ri.origin = ?")
                params.append(origin)
            if lens_matched:
                clauses.append(
                    """
                    EXISTS (
                        SELECT 1 FROM raw_item_lenses ril
                        WHERE ril.raw_item_id = ri.id
                    )
                    """
                )
            if first_seen_only:
                clauses.append(
                    """
                    (
                        COALESCE(ri.url, '') = ''
                        OR NOT EXISTS (
                            SELECT 1 FROM raw_items old
                            WHERE old.url = ri.url
                              AND old.id < ri.id
                        )
                    )
                    """
                )
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT ri.*, s.source_type AS source_type
                FROM raw_items ri
                LEFT JOIN subscriptions s ON s.id = ri.subscription_id
                WHERE {" AND ".join(clauses)}
                ORDER BY ri.priority DESC, ri.collected_at DESC, ri.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            lenses = _lenses_by_raw_item(conn, [int(row["id"]) for row in rows])
            cards.extend(_raw_item_card(row, lenses.get(int(row["id"]), [])) for row in rows)

        if payload_type in {"all", "digest"} and len(cards) < limit:
            rows = conn.execute(
                """
                SELECT *
                FROM digests d
                WHERE d.status = 'ok'
                  AND NOT EXISTS (
                      SELECT 1 FROM outbound_messages om
                      WHERE om.payload_type = 'digest'
                        AND om.digest_id = d.id
                  )
                ORDER BY d.created_at DESC, d.id DESC
                LIMIT ?
                """,
                (limit - len(cards),),
            ).fetchall()
            cards.extend(_digest_card(row) for row in rows)

    return {
        "ok": True,
        "payload_type": payload_type,
        "count": len(cards),
        "items": cards,
        "groups": _lens_groups(cards),
        "delivery_instruction": DELIVERY_INSTRUCTION,
    }


def pending_delivery_payload(
    config: WikiConfig,
    *,
    payload_type: str = "all",
    limit: int = 20,
    origin: str | None = None,
    lens_matched: bool = False,
    first_seen_only: bool = False,
) -> dict[str, Any]:
    """Return adapter-ready cards for undelivered raw items and digests."""
    return delivery_payload(
        config,
        payload_type=payload_type,
        limit=limit,
        origin=origin,
        lens_matched=lens_matched,
        first_seen_only=first_seen_only,
    )


def _object_summary(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    per_subscription = []
    for entry in getattr(result, "per_subscription", []) or []:
        if hasattr(entry, "__dataclass_fields__"):
            from dataclasses import asdict

            per_subscription.append(asdict(entry))
        elif isinstance(entry, dict):
            per_subscription.append(dict(entry))
        else:
            per_subscription.append(
                {
                    "ok": bool(getattr(entry, "ok", True)),
                    "error": str(getattr(entry, "error", "") or ""),
                }
            )
    summary: dict[str, Any] = {
        "total": int(getattr(result, "total", 0) or 0),
        "new_items": int(getattr(result, "new", 0) or 0),
        "skipped": int(getattr(result, "skipped", 0) or 0),
        "errors": int(getattr(result, "errors", 0) or 0),
        "duration_seconds": round(float(getattr(result, "duration_seconds", 0.0) or 0.0), 3),
    }
    if per_subscription:
        summary["per_subscription"] = per_subscription
    return summary


def _collector_errors(summary: dict[str, Any]) -> list[str]:
    raw_errors = summary.get("errors")
    if isinstance(raw_errors, list):
        return [str(error) for error in raw_errors if str(error)]
    per_subscription = summary.get("per_subscription")
    errors: list[str] = []
    if isinstance(per_subscription, list):
        for entry in per_subscription:
            if isinstance(entry, dict) and entry.get("ok") is False:
                error = str(entry.get("error") or entry.get("failure_reason") or "").strip()
                if error:
                    errors.append(error)
    error_text = str(summary.get("error") or "").strip()
    if error_text:
        errors.append(error_text)
    if errors:
        return errors
    try:
        count = int(raw_errors or 0)
    except (TypeError, ValueError):
        count = 0
    return [f"{count} subscription(s) failed"] if count else []


async def prepare_delivery_payload(
    config: WikiConfig,
    *,
    collect: str = "none",
    payload_type: str = "all",
    limit: int = 20,
    origin: str | None = None,
    lens_matched: bool = False,
    first_seen_only: bool = False,
    timeout_per_sub: float = 120.0,
    concurrency: int = 1,
    collect_all_active_func: Any | None = None,
    collect_all_due_func: Any | None = None,
) -> dict[str, Any]:
    """Optionally collect, then return one adapter-ready delivery object."""
    collect = (collect or "none").strip().lower()
    if collect not in {"none", "fetch-all", "tick"}:
        raise ValueError("collect must be one of: none, fetch-all, tick")
    payload_type = (payload_type or "all").strip().lower()
    if collect != "none" and payload_type == "digest":
        raise ValueError("--collect is only valid with payload_type raw_item or all")

    collector = {
        "command": collect,
        "ok": True,
        "summary": {},
        "errors": [],
    }

    if collect != "none":
        try:
            if collect == "fetch-all":
                if collect_all_active_func is None:
                    from contents_hub.api import collect_all_active as collect_all_active_func

                result = await collect_all_active_func(
                    config,
                    include_error=True,
                    per_subscription_timeout_seconds=float(timeout_per_sub),
                    concurrency=max(1, int(concurrency or 1)),
                )
            else:
                if collect_all_due_func is None:
                    from contents_hub.api import collect_all_due as collect_all_due_func

                result = await collect_all_due_func(
                    config,
                    per_subscription_timeout_seconds=float(timeout_per_sub),
                )
            summary = _object_summary(result)
            errors = _collector_errors(summary)
            collector = {
                "command": collect,
                "ok": not errors and bool(summary.get("ok", True)),
                "summary": summary,
                "errors": errors,
            }
        except Exception as exc:  # noqa: BLE001 - prepare must emit one JSON object.
            collector = {
                "command": collect,
                "ok": False,
                "summary": {},
                "errors": [str(exc)],
            }

    delivery = delivery_payload(
        config,
        payload_type=payload_type,
        limit=limit,
        origin=origin,
        lens_matched=lens_matched,
        first_seen_only=first_seen_only,
    )
    ok = bool(collector["ok"]) and bool(delivery.get("ok"))
    payload: dict[str, Any] = {
        "ok": ok,
        "collector": collector,
        "delivery": delivery,
    }
    if not collector["ok"] and collector["errors"]:
        payload["error"] = "; ".join(collector["errors"])
    return payload
