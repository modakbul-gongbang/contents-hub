"""Manual raw item ingestion helpers."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.item_key import normalize_url
from contents_hub.timeutil import now_iso

MANUAL_PRIORITY = 100
DEFAULT_MANUAL_LENS_ID = "manual-inbox"
DEFAULT_MANUAL_LENS_NAME = "Manual Inbox"
DEFAULT_MANUAL_LENS_DESCRIPTION = "Ad-hoc items manually saved by the user for the next digest."


@dataclass(frozen=True)
class RawAddResult:
    ok: bool
    inserted: bool
    item: dict[str, Any]
    lens_ids: list[str]
    warnings: list[str]


class MetadataError(ValueError):
    """Raised when user-supplied metadata JSON is invalid."""


class LensNotFoundError(ValueError):
    """Raised when one or more requested Lens ids do not exist."""


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self.meta_description = ""
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "meta":
            name = attr_map.get("name", "").lower()
            prop = attr_map.get("property", "").lower()
            if name == "description" or prop == "og:description":
                self.meta_description = attr_map.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        text = " ".join(html.unescape(data).split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        elif not self._skip_depth:
            self.body_parts.append(text)


def is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _manual_text_key(*, body: str) -> str:
    digest = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]
    return f"content://manual/{digest}"


def _ensure_default_manual_lens(conn: sqlite3.Connection, *, now: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO lenses
           (id, name, description, keywords, enabled, created_at, updated_at)
           VALUES (?, ?, ?, '[]', 1, ?, ?)""",
        (
            DEFAULT_MANUAL_LENS_ID,
            DEFAULT_MANUAL_LENS_NAME,
            DEFAULT_MANUAL_LENS_DESCRIPTION,
            now,
            now,
        ),
    )


def _row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _loads_object(row["metadata_json"] or "{}")
    return {
        "id": int(row["id"]),
        "url": row["url"],
        "title": row["title"],
        "body": row["body"],
        "origin": row["origin"],
        "priority": int(row["priority"]),
        "status": row["status"],
        "subscription_id": row["subscription_id"],
        "content_summary": row["content_summary"] or "",
        "metadata": metadata,
        "published_at": row["published_at"],
        "collected_at": row["collected_at"],
        "updated_at": row["updated_at"],
    }


def _loads_object(blob: str) -> dict[str, Any]:
    try:
        value = json.loads(blob or "{}")
    except json.JSONDecodeError as exc:
        raise MetadataError(f"metadata-json must be a JSON object: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise MetadataError("metadata-json must be a JSON object")
    return value


def enrich_url_static(url: str) -> dict[str, str]:
    """Best-effort static page enrichment without browser automation.

    This deliberately uses the cheapest deterministic path. Browser/agent-backed
    extraction can be layered above this later, but manual raw add must remain
    useful when network or browser access fails.
    """
    req = Request(
        url,
        headers={
            "User-Agent": "contents-hub/0.2 manual-raw-add (+https://github.com/yansfil/contents-hub)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=20) as resp:  # noqa: S310 - user-requested URL fetch
        content_type = resp.headers.get("content-type", "")
        raw = resp.read(1_000_000)
    if "html" not in content_type.lower():
        text = raw.decode("utf-8", errors="replace")
        return {"body": text[:5000], "content_summary": " ".join(text.split())[:500]}

    text = raw.decode("utf-8", errors="replace")
    parser = _PageTextParser()
    parser.feed(text)
    title = " ".join(" ".join(parser.title_parts).split())
    body = " ".join(parser.body_parts)
    body = re.sub(r"\s+", " ", body).strip()
    summary = parser.meta_description or body[:500]
    return {
        "title": title[:300],
        "body": body[:20000],
        "content_summary": summary[:500],
    }


def _has_meaningful_body(enriched: dict[str, str]) -> bool:
    return bool((enriched.get("body") or "").strip())


async def _enrich_url_browser_async(url: str) -> dict[str, str]:
    from contents_hub.chromux import chromux_fetch_session_cleanup
    from contents_hub.tools.browser import chromux_extract_handler, chromux_navigate_handler

    async with chromux_fetch_session_cleanup():
        nav_raw = await chromux_navigate_handler(url=url, wait_ms=1500)
        nav = json.loads(nav_raw or "{}")
        if not isinstance(nav, dict) or not nav.get("ok"):
            raise RuntimeError(str(nav.get("error") if isinstance(nav, dict) else nav_raw))
        session_id = str(nav.get("session_id") or "")
        if not session_id:
            raise RuntimeError("chromux navigate did not return a session_id")

        html_raw = await chromux_extract_handler(session_id=session_id, mode="html")
        payload = json.loads(html_raw or "{}")
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") if isinstance(payload, dict) else html_raw))
        document = str(payload.get("data") or "")
        if not document.strip():
            raise RuntimeError("chromux extraction returned an empty document")

    parser = _PageTextParser()
    parser.feed(document)
    title = " ".join(" ".join(parser.title_parts).split())
    body = re.sub(r"\s+", " ", " ".join(parser.body_parts)).strip()
    summary = parser.meta_description or body[:500]
    return {
        "title": title[:300],
        "body": body[:20000],
        "content_summary": summary[:500],
    }


def enrich_url_browser(url: str) -> dict[str, str]:
    """Browser-backed enrichment fallback using the contents-hub Chromux profile."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_enrich_url_browser_async(url))
    raise RuntimeError("browser enrichment cannot run inside an active event loop")


def enrich_url(url: str, *, allow_browser_fallback: bool = True) -> tuple[dict[str, str], list[str], str]:
    """Fetch URL content: static first, then browser fallback if static fails or yields no body."""
    warnings: list[str] = []
    try:
        enriched = enrich_url_static(url)
    except Exception as exc:  # noqa: BLE001 - fallback path
        warnings.append(f"static fetch failed: {exc}")
    else:
        if _has_meaningful_body(enriched):
            return enriched, warnings, "static"
        warnings.append("static fetch produced no body")

    if allow_browser_fallback:
        try:
            enriched = enrich_url_browser(url)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            warnings.append(f"browser fetch failed: {exc}")
        else:
            if _has_meaningful_body(enriched):
                return enriched, warnings, "browser"
            warnings.append("browser fetch produced no body")

    return {}, warnings, "none"


def add_manual_raw_item(
    config: WikiConfig,
    *,
    value: str,
    title: str = "",
    body: str = "",
    content_summary: str = "",
    published_at: str | None = None,
    metadata_json: str = "{}",
    lens_ids: list[str] | None = None,
) -> RawAddResult:
    value = (value or "").strip()
    if not value:
        raise ValueError("raw add requires a URL or text value")

    requested_lens_ids = [lens_id.strip() for lens_id in (lens_ids or []) if lens_id.strip()]
    use_default_enabled_lenses = not requested_lens_ids
    metadata = _loads_object(metadata_json or "{}")
    warnings: list[str] = []
    is_url = is_http_url(value)

    key = normalize_url(value) if is_url else ""
    if not is_url:
        body = body.strip() or value
        title = title.strip() or body[:80]
        key = _manual_text_key(body=body)

    body = body.strip()
    content_summary = content_summary.strip()
    now = now_iso()
    metadata = {
        **metadata,
        "manual": True,
        "input_type": "url" if is_url else "text",
    }

    with get_db(config) as conn:
        if use_default_enabled_lenses:
            requested_lens_ids = [
                row["id"]
                for row in conn.execute(
                    """SELECT id FROM lenses
                       WHERE enabled = 1 AND id != ?
                       ORDER BY id""",
                    (DEFAULT_MANUAL_LENS_ID,),
                ).fetchall()
            ]
            if not requested_lens_ids:
                _ensure_default_manual_lens(conn, now=now)
                requested_lens_ids = [DEFAULT_MANUAL_LENS_ID]
        else:
            found = {
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM lenses WHERE id IN ({','.join('?' for _ in requested_lens_ids)})",
                    requested_lens_ids,
                ).fetchall()
            }
            missing = [lens_id for lens_id in requested_lens_ids if lens_id not in found]
            if missing:
                raise LensNotFoundError(f"lens not found: {', '.join(missing)}")

        row = conn.execute(
            """SELECT * FROM raw_items
               WHERE subscription_id IS NULL AND url = ?
               ORDER BY id LIMIT 1""",
            (key,),
        ).fetchone()
        inserted = False
        if row is None:
            if is_url and not body.strip():
                enriched, enrich_warnings, fetch_mode = enrich_url(key)
                warnings.extend(enrich_warnings)
                if enriched:
                    title = title or enriched.get("title", "")
                    body = body or enriched.get("body", "")
                    content_summary = content_summary or enriched.get("content_summary", "")
                    metadata["fetch_page"] = {"ok": fetch_mode != "none", "mode": fetch_mode}
            if is_url:
                title = title.strip() or key
            body = body.strip()
            content_summary = content_summary.strip()
            cur = conn.execute(
                """INSERT INTO raw_items
                   (url, title, body, origin, priority, status, subscription_id,
                    content_summary, metadata_json, published_at, collected_at, updated_at)
                   VALUES (?, ?, ?, 'manual', ?, 'raw', NULL, ?, ?, ?, ?, ?)""",
                (
                    key,
                    title,
                    body,
                    MANUAL_PRIORITY,
                    content_summary,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    published_at,
                    now,
                    now,
                ),
            )
            inserted = True
            row = conn.execute("SELECT * FROM raw_items WHERE id = ?", (cur.lastrowid,)).fetchone()
        elif is_url and not str(row["body"] or "").strip():
            existing_metadata = _loads_object(row["metadata_json"] or "{}")
            enriched, enrich_warnings, fetch_mode = enrich_url(key)
            warnings.extend(enrich_warnings)
            if enriched:
                updated_title = title.strip() or enriched.get("title", "") or row["title"] or key
                updated_body = (enriched.get("body", "") or "").strip()
                updated_summary = (
                    content_summary.strip()
                    or row["content_summary"]
                    or enriched.get("content_summary", "")
                    or ""
                )
                existing_metadata["fetch_page"] = {"ok": fetch_mode != "none", "mode": fetch_mode}
                conn.execute(
                    """UPDATE raw_items
                       SET title = ?, body = ?, content_summary = ?, metadata_json = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        updated_title,
                        updated_body,
                        updated_summary,
                        json.dumps(existing_metadata, ensure_ascii=False, sort_keys=True),
                        now,
                        row["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM raw_items WHERE id = ?", (row["id"],)).fetchone()

        content_summary = content_summary.strip() or str(row["content_summary"] or "")

        raw_item_id = int(row["id"])
        for lens_id in requested_lens_ids:
            conn.execute(
                """INSERT OR IGNORE INTO raw_item_lenses
                   (raw_item_id, lens_id, summary, bullets_json, enriched_json)
                   VALUES (?, ?, ?, '[]', '{}')""",
                (raw_item_id, lens_id, content_summary),
            )
        if requested_lens_ids:
            attached_lens_ids = [
                r["lens_id"]
                for r in conn.execute(
                    """SELECT lens_id FROM raw_item_lenses
                       WHERE raw_item_id = ? ORDER BY lens_id""",
                    (raw_item_id,),
                ).fetchall()
            ]
        else:
            attached_lens_ids = []
        conn.commit()
        row = conn.execute("SELECT * FROM raw_items WHERE id = ?", (raw_item_id,)).fetchone()

    return RawAddResult(
        ok=True,
        inserted=inserted,
        item=_row_to_payload(row),
        lens_ids=attached_lens_ids,
        warnings=warnings,
    )


def result_to_payload(result: RawAddResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "inserted": result.inserted,
        "item": result.item,
        "lens_ids": result.lens_ids,
        "warnings": result.warnings,
    }
