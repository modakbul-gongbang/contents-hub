"""Promote a raw_item into an immutable source summary under vault/sources/.

Promotion reads the raw_item from SQLite, writes a processed human-readable
Markdown note with Obsidian frontmatter, flips the row's status to `promoted`,
and leaves the full captured body in SQLite for later reprocessing.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.frontmatter import Frontmatter, assemble_markdown
from contents_hub.timeutil import now_iso

logger = logging.getLogger(__name__)


class PromoteError(RuntimeError):
    """Raised when a raw_item cannot be promoted."""


_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_SKIP_TAGS = {"head", "script", "style", "svg", "template"}
_MAX_SUMMARY_CHARS = 240
_MAX_KEY_POINTS = 5
_MAX_KEY_POINT_CHARS = 220
_MAX_LENS_SUMMARY_CHARS = 700
_MAX_LENS_BULLETS = 8


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def _line_break(self) -> None:
        if self._parts and self._parts[-1] != "\n":
            self._parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if name == "br" or name in _BLOCK_TAGS:
            self._line_break()

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if name in _BLOCK_TAGS:
            self._line_break()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _html_fragment_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        logger.debug("Fell back to regex HTML stripping during promotion", exc_info=True)
        return re.sub(r"<[^>]+>", " ", value)
    return parser.text()


def _slugify(title: str, *, max_len: int = 60) -> str:
    """Produce a filesystem-safe slug from a title."""
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    s = s.strip("-")
    return s[:max_len] or "untitled"


def _short_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def source_filename(title: str, url: str, collected_at: str) -> str:
    """Deterministic source note filename: ``{YYYYMMDD}-{slug}-{hash}.md``.

    Public so Lens Inbox can derive the relative path text for promoted
    candidates without persisting it (R-T2.5).
    """
    try:
        dt = datetime.fromisoformat(collected_at)
    except ValueError:
        dt = datetime.now(timezone.utc)
    return f"{dt.strftime('%Y%m%d')}-{_slugify(title or url)}-{_short_hash(url)}.md"


def _clean_text(value: str | None) -> str:
    text = html.unescape(str(value or "")).strip()
    text = re.sub(r"\r\n?", "\n", text)
    if _HTML_TAG_RE.search(text):
        text = _html_fragment_to_text(text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_inline(value: str | None) -> str:
    return re.sub(r"\s+", " ", _clean_text(value)).strip()


def _truncate_text(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0].strip()
    if len(clipped) < max_chars // 2:
        clipped = text[:max_chars].strip()
    return clipped.rstrip(".,;:") + "..."


def _content_segments(value: str) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    chunks: list[str] = []
    for paragraph in re.split(r"\n{2,}|\n", text):
        paragraph = re.sub(r"\s+", " ", paragraph).strip(" -\t")
        if not paragraph:
            continue
        chunks.extend(
            segment.strip()
            for segment in re.split(r"(?<=[.!?])\s+", paragraph)
            if segment.strip()
        )
    return chunks or [re.sub(r"\s+", " ", text)]


def _build_frontmatter(row: dict, sub_row: dict | None) -> Frontmatter:
    source_type = (sub_row or {}).get("source_type") or "webpage"
    title = _clean_inline(row.get("title")) or row["url"]
    return Frontmatter(
        source_type=source_type,
        url=row["url"],
        title=title,
        collected_at=row["collected_at"],
        status="pending",
        extra={
            "origin": row.get("origin") or "subscription",
            "raw_item_id": row.get("id"),
        },
    )


def _parse_bullets(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    bullets: list[str] = []
    for item in parsed:
        bullet = _clean_inline(str(item))
        if bullet:
            bullets.append(_truncate_text(bullet, _MAX_KEY_POINT_CHARS))
    return bullets


def _sameish(a: str, b: str) -> bool:
    left = re.sub(r"\s+", " ", _clean_text(a)).strip()
    right = re.sub(r"\s+", " ", _clean_text(b)).strip()
    return bool(left and right and (left == right or left in right or right in left))


def _dedupe_points(points: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for point in points:
        cleaned = _truncate_text(_clean_inline(point), _MAX_KEY_POINT_CHARS)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            out.append(cleaned)
            seen.add(key)
    return out


def _fallback_summary(title: str, content: str) -> str:
    for segment in _content_segments(content):
        if segment:
            return _truncate_text(segment, _MAX_SUMMARY_CHARS)
    return _truncate_text(title, _MAX_SUMMARY_CHARS)


def _key_points(
    *,
    title: str,
    content: str,
    summary: str,
    lenses: list[dict],
) -> list[str]:
    points: list[str] = []
    for lens in lenses:
        points.extend(_parse_bullets(lens.get("bullets_json")))
    if not points:
        points.extend(_clean_inline(lens.get("summary")) for lens in lenses)
    if not points:
        for segment in _content_segments(content):
            if not _sameish(segment, summary):
                points.append(segment)
            if len(points) >= _MAX_KEY_POINTS:
                break
    if not points and title and not _sameish(title, summary):
        points.append(title)
    return _dedupe_points(points)[:_MAX_KEY_POINTS]


def _lens_rows(conn: sqlite3.Connection, raw_item_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT lens_id, summary, bullets_json
        FROM raw_item_lenses
        WHERE raw_item_id = ?
        ORDER BY lens_id
        """,
        (raw_item_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _build_body(row: dict, lenses: list[dict]) -> str:
    title = _clean_inline(row.get("title")) or row["url"]
    content = _clean_text(row.get("body"))
    summary = _clean_text(row.get("content_summary")) or _fallback_summary(title, content)
    points = _key_points(title=title, content=content, summary=summary, lenses=lenses)

    parts = [f"# {title}", "", "## 한줄 요약", "", summary]
    if points:
        parts += ["", "## 핵심 내용", ""] + [f"- {point}" for point in points]

    lens_sections: list[str] = []
    for lens in lenses:
        lens_id = _clean_inline(lens.get("lens_id")) or "lens"
        lens_summary = _clean_text(lens.get("summary"))
        bullets = _parse_bullets(lens.get("bullets_json"))[:_MAX_LENS_BULLETS]
        include_summary = bool(lens_summary and not _sameish(lens_summary, summary))
        if not include_summary and not bullets:
            continue
        lens_sections += ["", f"### {lens_id}"]
        if include_summary:
            lens_sections += ["", _truncate_text(lens_summary, _MAX_LENS_SUMMARY_CHARS)]
        if bullets:
            lens_sections += [""] + [f"- {bullet}" for bullet in bullets]

    if lens_sections:
        parts += ["", "## Lens Notes"] + lens_sections

    parts += [
        "",
        "## Source",
        "",
        f"- URL: {row['url']}",
        f"- Raw item id: {row['id']}",
        "- Full raw content: kept in SQLite `raw_items.body` for reprocessing; "
        "this source note stores the processed summary.",
        "",
    ]
    return "\n".join(parts)


def _promote_with_conn(
    config: WikiConfig, raw_item_id: int, conn: sqlite3.Connection
) -> Path:
    row = conn.execute(
        "SELECT * FROM raw_items WHERE id = ?", (raw_item_id,)
    ).fetchone()
    if row is None:
        raise PromoteError(f"raw_item {raw_item_id} not found")
    row = dict(row)

    if row["status"] == "promoted":
        raise PromoteError(f"raw_item {raw_item_id} already promoted")

    sub_row = None
    if row.get("subscription_id"):
        sr = conn.execute(
            "SELECT source_type FROM subscriptions WHERE id = ?",
            (row["subscription_id"],),
        ).fetchone()
        if sr is not None:
            sub_row = dict(sr)

    lenses = _lens_rows(conn, raw_item_id)
    fm = _build_frontmatter(row, sub_row)
    fm.lenses = [str(lens["lens_id"]) for lens in lenses if lens.get("lens_id")]
    title = row["title"] or row["url"]

    sources_dir = config.sources_path
    sources_dir.mkdir(parents=True, exist_ok=True)
    filename = source_filename(title, row["url"], row["collected_at"])
    path = sources_dir / filename

    path.write_text(
        assemble_markdown(fm.to_dict(), _build_body(row, lenses)),
        encoding="utf-8",
    )

    conn.execute(
        "UPDATE raw_items SET status='promoted', updated_at=? WHERE id=?",
        (now_iso(), raw_item_id),
    )
    conn.commit()
    logger.info("Promoted raw_item %d -> %s", raw_item_id, path)
    return path


def promote_raw_item(
    config: WikiConfig,
    raw_item_id: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> Path:
    """Promote one raw_item to a vault/sources/*.md file.

    Args:
        config: Wiki configuration.
        raw_item_id: Primary key of the row to promote.
        conn: Optional existing DB connection (for use inside daemon tick
            where a connection is already open). If omitted, opens a new one.

    Returns:
        Absolute path to the written source file.

    Raises:
        PromoteError: raw_item not found or already promoted.
    """
    if conn is not None:
        return _promote_with_conn(config, raw_item_id, conn)
    with get_db(config) as owned_conn:
        return _promote_with_conn(config, raw_item_id, owned_conn)
