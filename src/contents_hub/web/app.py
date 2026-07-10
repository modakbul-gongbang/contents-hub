"""FastAPI application factory for the contents-hub web dashboard."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from contents_hub.api import fetch_subscription
from contents_hub.chromux import (
    chromux_fetch_session_cleanup,
    chromux_foreground_fetch,
    kill_chromux_profile,
    open_chromux_headed as _open_chromux,
)
from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.source_types import SOURCE_TYPES, source_type_options
from contents_hub.subscriptions import SubscriptionStatus, SubscriptionStore
from contents_hub.timeutil import now_iso

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


_SOURCE_TYPE_ICONS: dict[str, str] = {spec.id: spec.icon for spec in SOURCE_TYPES}
_SOURCE_TYPE_LABELS: dict[str, str] = {spec.id: spec.label for spec in SOURCE_TYPES}
for _spec in SOURCE_TYPES:
    for _alias in _spec.aliases:
        _SOURCE_TYPE_ICONS[_alias] = _spec.icon
        _SOURCE_TYPE_LABELS[_alias] = _spec.label


_LOGIN_ERROR_HINTS = (
    "login required",
    "not authenticated",
    "/login",
    "/uas/login",
    "sign in",
    "로그인",
)


def _is_login_required_error(err: str | None) -> bool:
    """Heuristic: does this fetch error mean the agent was bounced to a
    sign-in page? The LinkedIn seed recipe formats these with
    'Login required:' so we match that and a few close variants."""
    if not err:
        return False
    low = err.lower()
    return any(h in low for h in _LOGIN_ERROR_HINTS)


async def _extract_title(url: str) -> str | None:
    """Fetch <og:title> or <title> from a URL; returns None on failure."""
    import re

    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception:
        return None

    if resp.status_code != 200:
        return None

    text = resp.text[:20_000]
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip() or None
    m = re.search(r"<title[^>]*>([^<]+)</title>", text, re.IGNORECASE)
    if m:
        return m.group(1).strip() or None
    return None


def _url_path_fallback(url: str) -> str:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = parsed.hostname or ""
    path = (parsed.path or "").strip("/").split("/", 1)[0]
    return f"{host}/{path}" if path else (host or url)


_BROWSER_PRESETS: list[dict[str, str]] = [
    {"key": "linkedin", "label": "LinkedIn", "url": "https://www.linkedin.com/login"},
    {"key": "twitter", "label": "Twitter / X", "url": "https://twitter.com/login"},
    {"key": "medium", "label": "Medium", "url": "https://medium.com/m/signin"},
    {"key": "substack", "label": "Substack", "url": "https://substack.com/sign-in"},
    {"key": "reddit", "label": "Reddit", "url": "https://www.reddit.com/login"},
    {
        "key": "youtube",
        "label": "YouTube",
        "url": "https://accounts.google.com/ServiceLogin?service=youtube",
    },
]


_EXPLORATION_SURFACE_OPTIONS: list[dict[str, str | bool]] = [
    {
        "id": "threads.feed",
        "label": "Threads home feed",
        "description": "Discover from the signed-in Threads feed.",
        "disabled": False,
    },
    {
        "id": "threads.search",
        "label": "Threads search",
        "description": "Use Threads search terms from the request.",
        "disabled": False,
    },
    {
        "id": "x.search",
        "label": "X exploration",
        "description": "Later extension; not available in the MVP.",
        "disabled": True,
    },
]


def _suggest_exploration_display_name(original_request: str) -> str:
    words = re.findall(r"[A-Za-z0-9가-힣]+", original_request)
    if not words:
        return "Exploration"
    return " ".join(words[:6])[:80]


def _clean_exploration_surfaces(raw_surfaces: list[str] | None) -> list[str]:
    allowed = {
        str(opt["id"]) for opt in _EXPLORATION_SURFACE_OPTIONS if not opt["disabled"]
    }
    surfaces: list[str] = []
    for surface in raw_surfaces or []:
        if surface in allowed and surface not in surfaces:
            surfaces.append(surface)
    return surfaces or ["threads.feed"]


def _clean_lens_ids(
    raw_lens_ids: list[str] | None,
    available_ids: set[str],
) -> list[str]:
    lens_ids: list[str] = []
    for lens_id in raw_lens_ids or []:
        lens_id = lens_id.strip()
        if lens_id in available_ids and lens_id not in lens_ids:
            lens_ids.append(lens_id)
    return lens_ids


def _json_loads_or(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _json_pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _strip_markdown_inline(value: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def _markdownish_inline(value: str) -> str:
    escaped = html.escape(value, quote=True)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        (
            r'<a href="\2" target="_blank" rel="noopener noreferrer">'
            r"\1</a>"
        ),
        escaped,
    )
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def _markdownish(value: str | None) -> Markup:
    """Render a conservative markdown subset used by digest narratives."""
    text = (value or "").strip()
    if not text:
        return Markup("")

    blocks: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def close_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            body = "<br>".join(_markdownish_inline(line) for line in paragraph)
            blocks.append(f"<p>{body}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            blocks.append("</ul>")
            in_list = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            close_paragraph()
            close_list()
            continue
        if stripped == "---":
            close_paragraph()
            close_list()
            blocks.append("<hr>")
            continue
        heading_match = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading_match:
            close_paragraph()
            close_list()
            level = min(4, len(heading_match.group(1)) + 2)
            blocks.append(
                f"<h{level}>{_markdownish_inline(heading_match.group(2))}</h{level}>"
            )
            continue
        if stripped.startswith(">"):
            close_paragraph()
            close_list()
            quote = stripped.lstrip(">").strip()
            blocks.append(f"<blockquote>{_markdownish_inline(quote)}</blockquote>")
            continue
        if stripped.startswith("- "):
            close_paragraph()
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_markdownish_inline(stripped[2:].strip())}</li>")
            continue
        close_list()
        paragraph.append(stripped)

    close_paragraph()
    close_list()
    return Markup("\n".join(blocks))


def _strategy_pretty(value) -> str:
    if isinstance(value, dict):
        recipe_markdown = value.get("recipe_markdown")
        if isinstance(recipe_markdown, str) and recipe_markdown.strip():
            return recipe_markdown.strip()
    return _json_pretty(value)


def _read_exploration_artifact(config: WikiConfig, artifact_ref: str | None) -> str:
    if not artifact_ref:
        return ""
    base = (config.meta_path / "exploration-artifacts").resolve()
    path = (config.meta_path / artifact_ref).resolve()
    if path.parent != base:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def create_app(config: WikiConfig) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: Resolved WikiConfig with vault path and settings.

    Returns:
        Configured FastAPI instance with Jinja2 templates.
    """
    app = FastAPI(title="contents-hub Dashboard", docs_url=None, redoc_url=None)
    app.state.config = config

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["markdownish"] = _markdownish

    async def _fetch_with_running_guard(cfg: WikiConfig, url: str) -> None:
        """Dispatch ``fetch_subscription`` and unconditionally clear the
        ``schedules.running`` flag in a finally block.

        The legacy ``_run_fetch_now`` had this finally. The post-T10 routes
        (collect / ground / confirm_auth) all schedule
        ``fetch_subscription`` directly and would otherwise leave
        ``running = 1`` set forever after the first click on routes that
        flip it. ``UPDATE running = 0`` is a no-op on routes that never
        flipped it (ground / confirm_auth as of writing), so it's
        safe to apply uniformly.
        """

        try:
            await fetch_subscription(cfg, url)
        finally:
            try:
                with get_db(cfg) as conn:
                    conn.execute(
                        "UPDATE schedules SET running = 0 WHERE subscription_url = ?",
                        (url,),
                    )
            except Exception as exc:
                logger.warning("failed to clear schedules.running for %s: %s", url, exc)

    def _safe_next_url(next_url: str | None, default: str = "/") -> str:
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return next_url
        return default

    def _digest_title(row: dict) -> str:
        title = str(row.get("title") or "").strip()
        if title:
            return title
        content = str(row.get("content_md") or "").strip()
        if content:
            content = re.sub(r"^\s*📌\s*오늘의\s*핵심\s*", "", content).strip()
            for line in content.splitlines():
                cleaned = _strip_markdown_inline(line.strip(" #\t"))
                if cleaned:
                    return cleaned[:78] + ("..." if len(cleaned) > 78 else "")
        created = str(row.get("created_at") or "").replace("T", " ")[:16]
        return f"Digest {created}" if created else f"Digest #{row.get('id')}"

    def _section_narrative_for_web(markdown: str | None) -> str:
        text = (markdown or "").strip()
        if not text:
            return ""
        cleaned: list[str] = []
        skipping_related = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("📎") and "관련 아티클" in stripped:
                skipping_related = True
                continue
            if skipping_related:
                if stripped == "---":
                    skipping_related = False
                    if cleaned and cleaned[-1].strip():
                        cleaned.append("")
                    cleaned.append("---")
                    cleaned.append("")
                    continue
                if (
                    not stripped
                    or stripped.startswith("-")
                    or line.startswith((" ", "\t"))
                    or " via " in stripped
                ):
                    continue
                skipping_related = False
            cleaned.append(line)
        return "\n".join(cleaned).strip()

    def _digest_executive_for_web(content_md: str | None) -> str:
        text = (content_md or "").strip()
        if "\n🎯 " in text:
            text = text.split("\n🎯 ", 1)[0].rstrip()
        return text

    def _parse_sections_json(value: str | None) -> list[dict]:
        raw = _json_loads_or(value, [])
        if not isinstance(raw, list):
            return []
        sections: list[dict] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            section = dict(item)
            section["section_index"] = index
            section["item_ids"] = [
                int(rid)
                for rid in (section.get("item_ids") or [])
                if isinstance(rid, int) or str(rid).isdigit()
            ]
            section["narrative_web"] = _section_narrative_for_web(
                section.get("narrative_md")
            )
            sections.append(section)
        return sections

    def _load_digest_items_by_section(
        conn,
        digest_id: int,
        sections: list[dict],
    ) -> dict[int, list[dict]]:
        rows = conn.execute(
            """SELECT dsi.section_index, dsi.sort_order,
                      ri.id, ri.title, ri.url, ri.content_summary,
                      ri.published_at, ri.collected_at,
                      COALESCE(NULLIF(s.title, ''), s.url, '') AS source_label,
                      saved.saved_at
               FROM digest_section_items dsi
               JOIN raw_items ri ON ri.id = dsi.raw_item_id
               LEFT JOIN subscriptions s ON s.id = ri.subscription_id
               LEFT JOIN saved_items saved ON saved.raw_item_id = ri.id
               WHERE dsi.digest_id = ?
               ORDER BY dsi.section_index ASC, dsi.sort_order ASC, ri.id ASC""",
            (digest_id,),
        ).fetchall()
        if rows:
            grouped: dict[int, list[dict]] = {}
            for row in rows:
                item = dict(row)
                item["is_saved"] = bool(item.get("saved_at"))
                grouped.setdefault(int(item["section_index"]), []).append(item)
            return grouped

        # Backfill path for digests created before v13: sections_json has ids,
        # so join those ids to raw_items and preserve the section-local order.
        raw_ids: list[int] = []
        for section in sections:
            raw_ids.extend(int(rid) for rid in section.get("item_ids", []))
        raw_ids = list(dict.fromkeys(raw_ids))
        if not raw_ids:
            return {}
        placeholders = ",".join("?" * len(raw_ids))
        item_rows = conn.execute(
            f"""SELECT ri.id, ri.title, ri.url, ri.content_summary,
                       ri.published_at, ri.collected_at,
                       COALESCE(NULLIF(s.title, ''), s.url, '') AS source_label,
                       saved.saved_at
                FROM raw_items ri
                LEFT JOIN subscriptions s ON s.id = ri.subscription_id
                LEFT JOIN saved_items saved ON saved.raw_item_id = ri.id
                WHERE ri.id IN ({placeholders})""",
            raw_ids,
        ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in item_rows}
        grouped: dict[int, list[dict]] = {}
        for section in sections:
            section_index = int(section.get("section_index", 0))
            grouped[section_index] = []
            for raw_id in section.get("item_ids", []):
                item = by_id.get(int(raw_id))
                if item is None:
                    continue
                item = dict(item)
                item["is_saved"] = bool(item.get("saved_at"))
                grouped[section_index].append(item)
        return grouped

    def _saved_item_rows(conn) -> list[dict]:
        rows = conn.execute(
            """SELECT saved.saved_at,
                      ri.id, ri.title, ri.url, ri.content_summary,
                      ri.published_at, ri.collected_at,
                      COALESCE(NULLIF(s.title, ''), s.url, '') AS source_label
               FROM saved_items saved
               JOIN raw_items ri ON ri.id = saved.raw_item_id
               LEFT JOIN subscriptions s ON s.id = ri.subscription_id
               ORDER BY saved.saved_at DESC, ri.id DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # T2: Overview dashboard (/)
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):

        msg = request.query_params.get("msg", "")
        cfg = request.app.state.config

        subscription_count = 0
        lens_count = 0
        saved_this_week = 0
        recent_saved: list[dict] = []

        try:
            with get_db(cfg) as conn:
                row = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()
                subscription_count = row[0] if row else 0

                row = conn.execute("SELECT COUNT(*) FROM lenses").fetchone()
                lens_count = row[0] if row else 0

                row = conn.execute(
                    "SELECT COUNT(*) FROM saved_items "
                    "WHERE datetime(saved_at) >= datetime('now', '-7 days')"
                ).fetchone()
                saved_this_week = row[0] if row else 0

                rows = conn.execute(
                    """SELECT ri.id, ri.title, ri.url, saved.saved_at
                       FROM saved_items saved
                       JOIN raw_items ri ON ri.id = saved.raw_item_id
                       ORDER BY saved.saved_at DESC LIMIT 10"""
                ).fetchall()
                recent_saved = [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("DB error on overview: %s", exc)

        return templates.TemplateResponse(
            request=request,
            name="overview.html",
            context={
                "title": "Home",
                "msg": msg,
                "subscription_count": subscription_count,
                "lens_count": lens_count,
                "saved_this_week": saved_this_week,
                "recent_saved": recent_saved,
            },
        )

    # ------------------------------------------------------------------
    # Digests and Saved items
    # ------------------------------------------------------------------

    @app.get("/digests", response_class=HTMLResponse)
    async def digests_list(request: Request):

        cfg = request.app.state.config
        digests: list[dict] = []
        try:
            with get_db(cfg) as conn:
                rows = conn.execute(
                    """SELECT id, created_at, title, item_count, content_md,
                              sections_json, status, error
                       FROM digests
                       ORDER BY created_at DESC, id DESC
                       LIMIT 100"""
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["title"] = _digest_title(item)
                    preview = str(item.get("content_md") or "")
                    preview = re.sub(
                        r"^\s*📌\s*오늘의\s*핵심\s*",
                        "",
                        preview,
                    ).strip()
                    item["preview"] = _strip_markdown_inline(preview)
                    item["section_count"] = len(
                        _parse_sections_json(item.get("sections_json"))
                    )
                    digests.append(item)
        except Exception as exc:
            logger.warning("digests list failed: %s", exc)

        return templates.TemplateResponse(
            request=request,
            name="digests.html",
            context={
                "title": "Digests",
                "msg": request.query_params.get("msg", ""),
                "digests": digests,
            },
        )

    @app.get("/digests/{digest_id}", response_class=HTMLResponse)
    async def digest_detail(request: Request, digest_id: int):

        cfg = request.app.state.config
        selected: dict | None = None
        executive_md = ""
        try:
            with get_db(cfg) as conn:
                row = conn.execute(
                    """SELECT id, created_at, title, item_count, content_md,
                              sections_json, status, error
                       FROM digests
                       WHERE id = ?""",
                    (digest_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Digest not found")
                selected = dict(row)
                selected["title"] = _digest_title(selected)
                sections = _parse_sections_json(selected.get("sections_json"))
                items_by_section = _load_digest_items_by_section(
                    conn,
                    digest_id,
                    sections,
                )
                for section in sections:
                    section["article_items"] = items_by_section.get(
                        int(section["section_index"]),
                        [],
                    )
                selected["sections"] = sections
                executive_md = _digest_executive_for_web(selected.get("content_md"))
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("digest detail failed for %d: %s", digest_id, exc)
            raise HTTPException(status_code=500, detail=str(exc))

        return templates.TemplateResponse(
            request=request,
            name="digest_detail.html",
            context={
                "title": selected["title"] if selected else "Digest",
                "msg": request.query_params.get("msg", ""),
                "digest": selected,
                "executive_md": executive_md,
            },
        )

    @app.get("/saved", response_class=HTMLResponse)
    async def saved_items(request: Request):

        cfg = request.app.state.config
        items: list[dict] = []
        try:
            with get_db(cfg) as conn:
                items = _saved_item_rows(conn)
        except Exception as exc:
            logger.warning("saved page failed: %s", exc)

        return templates.TemplateResponse(
            request=request,
            name="saved.html",
            context={
                "title": "Saved",
                "msg": request.query_params.get("msg", ""),
                "items": items,
            },
        )

    @app.post("/raw-items/{item_id}/toggle-saved")
    async def raw_item_toggle_saved(
        item_id: int,
        request: Request,
        next_url: str = Form(""),
    ):


        cfg = request.app.state.config
        try:
            with get_db(cfg) as conn:
                row = conn.execute(
                    "SELECT id FROM raw_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Raw item not found")
                existing = conn.execute(
                    "SELECT saved_at FROM saved_items WHERE raw_item_id = ?",
                    (item_id,),
                ).fetchone()
                if existing:
                    conn.execute(
                        "DELETE FROM saved_items WHERE raw_item_id = ?",
                        (item_id,),
                    )
                    saved = False
                else:
                    conn.execute(
                        "INSERT INTO saved_items (raw_item_id, saved_at) VALUES (?, ?)",
                        (item_id, now_iso()),
                    )
                    saved = True
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("toggle saved failed for item %d: %s", item_id, exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        accept = request.headers.get("accept", "")
        if "application/json" in accept and not next_url:
            return JSONResponse(
                {"ok": True, "raw_item_id": item_id, "saved": saved}
            )
        target = _safe_next_url(next_url, default="/saved")
        suffix = "Saved" if saved else "Removed+from+Saved"
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            url=f"{target}{separator}msg={suffix}",
            status_code=303,
        )

    # ------------------------------------------------------------------
    # T3: Subscriptions CRUD (/subscriptions)
    # ------------------------------------------------------------------

    @app.get("/subscriptions", response_class=HTMLResponse)
    async def subscriptions_list(request: Request):

        msg = request.query_params.get("msg", "")
        cfg = request.app.state.config
        store = SubscriptionStore(cfg)

        try:
            subs = store.list_all()
        except Exception as exc:
            logger.warning("Error listing subscriptions: %s", exc)
            subs = []

        # Decorate each subscription with icon + labels for the table.
        decorated = []
        for sub in subs:
            sub_config = getattr(sub, "config", None) or {}
            source_type = getattr(sub, "source_type", "") or ""
            decorated.append(
                {
                    "obj": sub,
                    "icon": _SOURCE_TYPE_ICONS.get(source_type, "🌐"),
                    "source_label": _SOURCE_TYPE_LABELS.get(
                        source_type, source_type or "unknown"
                    ),
                    "fetch_method": sub_config.get("fetch_method", ""),
                }
            )

        return templates.TemplateResponse(
            request=request,
            name="subscriptions.html",
            context={
                "title": "Subscriptions",
                "msg": msg,
                "subscriptions": subs,
                "decorated": decorated,
                "source_type_options": source_type_options(),
            },
        )

    @app.post("/subscriptions/classify")
    async def classify_url(url: str = Form(...), source_type: str = Form("")):
        """Real-time URL classification for the add-subscription form."""
        from contents_hub.source_router import classify

        info = classify(url)
        if source_type:
            from contents_hub.source_types import classify_url

            info = classify_url(url, source_type)
        info["icon"] = _SOURCE_TYPE_ICONS.get(info["source_type"], "🌐")
        info["label"] = _SOURCE_TYPE_LABELS.get(
            info["source_type"],
            info["source_type"],
        )
        return JSONResponse(info)

    @app.post("/subscriptions/add")
    async def subscriptions_add(
        background_tasks: BackgroundTasks,
        url: str = Form(...),
        source_type: str = Form(""),
        collection_prompt: str = Form(""),
    ):
        from contents_hub.source_router import classify

        cfg = app.state.config
        store = SubscriptionStore(cfg)

        try:
            info = classify(url)
            if source_type:
                from contents_hub.source_types import classify_url

                info = classify_url(url, source_type)
            source_type = str(info["source_type"])

            title = (
                await _extract_title(url)
                or info.get("suggested_title")
                or _url_path_fallback(url)
            )

            # Inherit the user-configured default schedule for this source type.
            default_interval = cfg.schedule.interval_for(source_type)
            default_cron = cfg.schedule.cron_for(source_type)
            schedule_arg: dict = {"preset": "daily"}
            if default_cron:
                schedule_arg["cron"] = default_cron
            elif default_interval:
                schedule_arg["interval_minutes"] = int(default_interval)

            store.add(
                url=url,
                title=title,
                source_type=source_type,
                schedule=schedule_arg,
            )
            # Mark as validating right away so the detail page knows to show
            # the Pending-approval UI instead of the normal fetch banner.
            store.set_status(url, SubscriptionStatus.VALIDATING)

            recipe_base = info.get("recipe_base")
            config_updates: dict = {
                "recipe_base": recipe_base,
                "recipe_id": info.get("recipe_id"),
                "recipe_version": info.get("recipe_version"),
                "recipe_channel": info.get("recipe_channel", "stable"),
                "fetch_method": info.get("execution_method"),
                "recipe_capabilities": info.get("capabilities") or [],
            }
            prompt = (collection_prompt or "").strip()
            if prompt:
                config_updates["collection_prompt"] = prompt
            if config_updates:
                try:
                    store.update_config(url, config_updates)
                except Exception as cfg_exc:
                    logger.warning("update_config failed for %s: %s", url, cfg_exc)

            background_tasks.add_task(_run_trial_fetch, cfg, url)

            sub = store.get(url)
            sub_id = getattr(sub, "id", None)
            if sub_id is None:
                return RedirectResponse(
                    url="/subscriptions?msg=Subscription+added", status_code=303
                )
            return RedirectResponse(
                url=f"/subscriptions/{sub_id}?msg=Validating+%E2%80%94+running+trial+fetch",
                status_code=303,
            )
        except (ValueError, Exception) as exc:
            return RedirectResponse(
                url=f"/subscriptions?msg=Error:+{exc}", status_code=303
            )

    @app.post("/subscriptions/{sub_id}/open_login")
    async def subscription_open_login(
        sub_id: str, request: Request, confirmed: bool = False
    ):
        """Launch a HEADED chromux window at the source's sign-in page.

        If the profile is currently driving a headless agent fetch we return
        ``status="needs_confirm"`` so the UI can prompt before we kill the
        running fetch.
        """
        from contents_hub.source_router import AUTH_SIGNIN_HOMEPAGES

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")

        target = AUTH_SIGNIN_HOMEPAGES.get(sub.source_type) or sub.url
        result = _open_chromux(target, session=f"login-{sub_id}", confirmed=confirmed)
        if result["status"] == "error":
            logger.warning("open_login failed for %s: %s", sub_id, result["error"])
            return JSONResponse(result, status_code=500)
        if result["status"] == "needs_confirm":
            return JSONResponse(result, status_code=409)
        return JSONResponse(result)

    @app.post("/subscriptions/{sub_id}/confirm_auth")
    async def subscription_confirm_auth(
        sub_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        """User confirms they've signed in — kick off baseline fetch."""

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        background_tasks.add_task(_fetch_with_running_guard, cfg, sub.url)
        return JSONResponse({"status": "started"})

    @app.get("/subscriptions/{sub_id}", response_class=HTMLResponse)
    async def subscription_detail(request: Request, sub_id: str):
        from contents_hub.recipes import RecipeRegistry

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")

        sub_config = getattr(sub, "config", None) or {}
        source_type = getattr(sub, "source_type", "") or ""
        icon = _SOURCE_TYPE_ICONS.get(source_type, "🌐")
        source_label = _SOURCE_TYPE_LABELS.get(source_type, source_type)

        recipe_text = RecipeRegistry.get_recipe(sub) or ""
        recipe_meta = RecipeRegistry.get_recipe_metadata(sub)
        has_override = bool((sub_config or {}).get("recipe"))
        last_explore_note = sub_config.get("last_explore_note", "")
        fetch_method = recipe_meta.get("fetch_method") or sub_config.get(
            "fetch_method", ""
        )
        consecutive_failures = int(sub_config.get("consecutive_failures", 0) or 0)
        last_manual_fetch = sub_config.get("last_manual_fetch") or {}
        collection_prompt = sub_config.get("collection_prompt", "") or ""
        trial_result = sub_config.get("trial_result") or {}

        from contents_hub.models import FetchFailureReason
        from contents_hub.source_router import AUTH_SIGNIN_HOMEPAGES

        sub_status_value = (
            sub.status.value if hasattr(sub.status, "value") else str(sub.status)
        )
        # Primary signal: the agent's typed failure_reason enum. Secondary:
        # subscription status. Tertiary (legacy): substring match on raw
        # error text for older agent outputs that predate the enum.
        manual_reason = (last_manual_fetch or {}).get("failure_reason") or ""
        needs_auth = (
            manual_reason == FetchFailureReason.LOGIN_REQUIRED.value
            or sub_status_value == "needs_auth"
            or (
                not manual_reason
                and _is_login_required_error(last_manual_fetch.get("error"))
            )
        )
        signin_url = AUTH_SIGNIN_HOMEPAGES.get(source_type)

        schedule_row: dict = {}
        # Canonical schedule source: subscriptions.schedule_* (what the daemon
        # reads). Overlay the legacy `schedules` row fields (next_run_at,
        # last_run_at, running flag, etc.) when they exist.
        sub_cron = getattr(sub.schedule, "cron", None)
        sub_interval = getattr(sub.schedule, "interval_minutes", None)
        schedule_row = {
            "cron_expr": sub_cron,
            "interval_minutes": sub_interval,
        }
        history: list[dict] = []
        items: list[dict] = []
        items_total = 0
        items_saved = 0
        started_at: str | None = None
        try:
            with get_db(cfg) as conn:
                row = conn.execute(
                    "SELECT id, cron_expr, interval_minutes, enabled, running, "
                    "next_run_at, last_run_at, last_run_ok, last_error, "
                    "consecutive_errors "
                    "FROM schedules WHERE subscription_url = ?",
                    (sub.url,),
                ).fetchone()
                if row is not None:
                    row_d = dict(row)
                    # Preserve sub_cron / sub_interval as the display source;
                    # pull in the operational fields (running, next_run_at, …).
                    for k in (
                        "id",
                        "enabled",
                        "running",
                        "next_run_at",
                        "last_run_at",
                        "last_run_ok",
                        "last_error",
                        "consecutive_errors",
                    ):
                        schedule_row[k] = row_d.get(k)
                    rows = conn.execute(
                        "SELECT started_at, finished_at, status, new_items, "
                        "error_message FROM schedule_runs "
                        "WHERE schedule_id = ? ORDER BY started_at DESC LIMIT 50",
                        (schedule_row["id"],),
                    ).fetchall()
                    history = [dict(r) for r in rows]

                # Content tab: items for this subscription + matched lens ids.
                rows = conn.execute(
                    "SELECT id, title, url, status, collected_at, updated_at, "
                    "content_summary, body, published_at "
                    "FROM raw_items WHERE subscription_id = ? "
                    "ORDER BY COALESCE(published_at, collected_at) DESC LIMIT 100",
                    (sub.id,),
                ).fetchall()
                items = [dict(r) for r in rows]
                if items:
                    item_ids = [i["id"] for i in items]
                    placeholder = ",".join("?" * len(item_ids))
                    lens_rows = conn.execute(
                        f"""SELECT raw_item_id, lens_id, summary, bullets_json
                            FROM raw_item_lenses
                            WHERE raw_item_id IN ({placeholder})""",
                        item_ids,
                    ).fetchall()
                    lens_map: dict[int, list[dict]] = {}
                    for lr in lens_rows:
                        try:
                            bullets = json.loads(lr["bullets_json"] or "[]")
                        except json.JSONDecodeError:
                            bullets = []
                        if not isinstance(bullets, list):
                            bullets = []
                        lens_map.setdefault(lr["raw_item_id"], []).append(
                            {
                                "id": lr["lens_id"],
                                "summary": lr["summary"] or "",
                                "bullets": [b for b in bullets if isinstance(b, str)],
                            }
                        )
                    for it in items:
                        it["lenses"] = lens_map.get(it["id"], [])
                        it["lens_ids"] = [lens["id"] for lens in it["lenses"]]

                items_total = len(items)
                items_saved = sum(1 for i in items if i.get("status") == "promoted")
                row = conn.execute(
                    "SELECT MIN(collected_at) AS started FROM raw_items "
                    "WHERE subscription_id = ?",
                    (sub.id,),
                ).fetchone()
                if row and row["started"]:
                    started_at = row["started"]
        except Exception as exc:
            logger.warning("detail DB read failed for %s: %s", sub.url, exc)

        status_str = (
            sub.status.value
            if sub.status is not None and hasattr(sub.status, "value")
            else str(sub.status)
        )
        show_error_banner = bool(last_explore_note) and (
            status_str == "error" or consecutive_failures > 0
        )

        default_interval = cfg.schedule.interval_for(source_type or "webpage")
        default_cron = cfg.schedule.cron_for(source_type or "webpage")
        # Override = user set a cron, or set an interval different from default.
        is_schedule_override = bool(sub_cron) or (
            sub_interval is not None and sub_interval != default_interval
        )

        return templates.TemplateResponse(
            request=request,
            name="subscription_detail.html",
            context={
                "title": f"Subscription: {sub.title or sub.url}",
                "msg": request.query_params.get("msg", ""),
                "sub": sub,
                "status_str": status_str,
                "icon": icon,
                "source_label": source_label,
                "recipe_text": recipe_text,
                "recipe_meta": recipe_meta,
                "has_override": has_override,
                "fetch_method": fetch_method,
                "last_explore_note": last_explore_note,
                "consecutive_failures": consecutive_failures,
                "show_error_banner": show_error_banner,
                "schedule": schedule_row,
                "default_interval": default_interval,
                "default_cron": default_cron,
                "is_schedule_override": is_schedule_override,
                "history": history,
                "items": items,
                "items_total": items_total,
                "items_saved": items_saved,
                "started_at": started_at,
                "last_manual_fetch": last_manual_fetch,
                "collection_prompt": collection_prompt,
                "trial_result": trial_result,
                "is_validating": status_str == "validating",
                "needs_auth": needs_auth,
                "signin_url": signin_url,
            },
        )

    async def _run_trial_fetch(cfg: WikiConfig, sub_url: str) -> None:
        """Background task: run a Pending-approval (validating) trial fetch.

        Trial runs are distinct from a normal fetch: items are NOT persisted
        to ``raw_items`` and the subscription stays in ``status=validating``.
        Instead the executor's :class:`FetchResult` is rendered into a
        ``trial_result`` block on ``subscription.config`` so the
        Pending-approval UI can show the user a preview before they commit
        via Keep / Discard / Retry.

        Implementation note (R-T7.2 / R-T9.1): the legacy ``_run_fetch_now``
        helper is gone — non-trial collect/ground/confirm_auth
        endpoints route through :func:`api.fetch_subscription` directly.
        Trial mode lives here because :func:`api.fetch_subscription` always
        persists, which is incorrect for a preview run.
        """

        from contents_hub.executor import execute_trial as _executor_trial
        from contents_hub.recipes import RecipeRegistry

        store = SubscriptionStore(cfg)
        sub = store.get(sub_url)
        if sub is None:
            return

        def _item_to_dict(it) -> dict:
            body = getattr(it, "content_html", "") or getattr(it, "summary", "") or ""
            pa = getattr(it, "published_at", None)
            return {
                "url": (getattr(it, "url", "") or "").strip(),
                "title": getattr(it, "title", "") or "",
                "body": body,
                "published_at": pa.isoformat()
                if pa is not None and hasattr(pa, "isoformat")
                else None,
            }

        meta = RecipeRegistry.get_recipe_metadata(sub)
        run_headed = meta.get(
            "fetch_method"
        ) == "browser" and "chromux_navigate" in set(meta.get("capabilities") or [])

        try:
            if run_headed:
                trial_session = f"trial-{getattr(sub, 'id', '') or 'linkedin'}"
                launch = _open_chromux(
                    sub.url,
                    session=trial_session,
                    confirmed=True,
                )
                if launch["status"] == "error":
                    raise RuntimeError(launch["error"])
                async with chromux_fetch_session_cleanup(
                    fallback_sessions=[trial_session],
                ), chromux_foreground_fetch():
                    result = await _executor_trial(sub)
            else:
                async with chromux_fetch_session_cleanup():
                    result = await _executor_trial(sub)
        except Exception as exc:
            logger.warning("trial fetch failed for %s: %s", sub_url, exc)
            payload = {
                "ok": False,
                "items": 0,
                "error": str(exc),
                "finished_at": now_iso(),
                "recipe_mode": "failed",
                "samples": [],
            }
            try:
                store.update_config(sub_url, {"trial_result": payload})
            except Exception as rec_exc:
                logger.warning("record trial_result failed: %s", rec_exc)
            return

        sample_dicts = (
            [_item_to_dict(it) for it in result.items]
            if (result.ok and result.items)
            else []
        )

        ok = bool(result.ok)
        recipe_mode = "catalog" if ok else "failed"

        payload = {
            "ok": ok,
            "items": len(result.items),
            "error": "" if ok else (getattr(result, "error", "") or "unknown"),
            "finished_at": now_iso(),
            "recipe_mode": recipe_mode,
            # Staged preview — committed to raw_items only on Keep.
            "samples": sample_dicts[:20],
        }
        try:
            store.update_config(sub_url, {"trial_result": payload})
        except Exception as rec_exc:
            logger.warning("record trial_result failed: %s", rec_exc)
        # Do NOT flip status — Pending-approval UI stays up.

    @app.post("/subscriptions/{sub_id}/keep")
    async def subscription_keep(sub_id: str, request: Request):
        """Approve a validating subscription: commit sampled items to raw_items,
        flip status to active, clear trial state."""

        from contents_hub.item_key import item_key

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if sub.status != SubscriptionStatus.VALIDATING:
            return JSONResponse(
                {"status": "skipped", "reason": f"not validating ({sub.status.value})"}
            )

        trial = (sub.config or {}).get("trial_result") or {}
        samples: list[dict] = trial.get("samples") or []
        now_str = now_iso()
        inserted = 0

        try:
            with get_db(cfg) as conn:
                for s in samples:
                    key = item_key(s, int(sub_id))
                    if not key:
                        continue
                    body = (s.get("body") or "")[:20000]
                    summary_preview = (s.get("body") or "")[:500]
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO raw_items "
                        "(url, title, body, origin, priority, status, "
                        "subscription_id, content_summary, published_at, "
                        "collected_at, updated_at) "
                        "VALUES (?, ?, ?, 'subscription', 50, 'raw', ?, ?, ?, ?, ?)",
                        (
                            key,
                            s.get("title") or "",
                            body,
                            int(sub_id),
                            summary_preview,
                            s.get("published_at"),
                            now_str,
                            now_str,
                        ),
                    )
                    if cur.rowcount and cur.lastrowid:
                        inserted += 1
                conn.execute(
                    """UPDATE subscriptions
                       SET status = 'active',
                           last_fetched_at = ?,
                           config = json_remove(config, '$.trial_result'),
                           updated_at = ?
                       WHERE id = ?""",
                    (now_str, now_str, int(sub_id)),
                )
        except Exception as exc:
            logger.warning("keep failed for %s: %s", sub.url, exc)
            raise HTTPException(status_code=500, detail=str(exc))
        return JSONResponse({"status": "active", "inserted": inserted})

    @app.post("/subscriptions/{sub_id}/discard")
    async def subscription_discard(sub_id: str, request: Request):
        """Reject a validating subscription: delete the sub + its schedules.
        Trial samples live in config JSON (not raw_items), so nothing else
        to clean — the sub delete tears everything down."""

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if sub.status != SubscriptionStatus.VALIDATING:
            return JSONResponse(
                {"status": "skipped", "reason": f"not validating ({sub.status.value})"}
            )
        try:
            with get_db(cfg) as conn:
                conn.execute("DELETE FROM subscriptions WHERE id = ?", (int(sub_id),))
                conn.execute(
                    "DELETE FROM schedules WHERE subscription_url = ?", (sub.url,)
                )
        except Exception as exc:
            logger.warning("discard failed for %s: %s", sub.url, exc)
            raise HTTPException(status_code=500, detail=str(exc))
        return JSONResponse({"status": "discarded"})

    @app.post("/subscriptions/{sub_id}/retry_validation")
    async def subscription_retry_validation(
        sub_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        """Clear the current trial_result and run another trial fetch."""

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if sub.status != SubscriptionStatus.VALIDATING:
            return JSONResponse(
                {"status": "skipped", "reason": f"not validating ({sub.status.value})"}
            )
        try:
            with get_db(cfg) as conn:
                # Drop the stale trial_result (samples live inside it).
                # raw_items is untouched — trials don't write there.
                conn.execute(
                    """UPDATE subscriptions
                       SET config = json_remove(config, '$.trial_result')
                       WHERE id = ?""",
                    (int(sub_id),),
                )
        except Exception as exc:
            logger.warning("retry_validation prep failed for %s: %s", sub.url, exc)
        background_tasks.add_task(_run_trial_fetch, cfg, sub.url)
        return JSONResponse({"status": "retrying"})

    @app.post("/subscriptions/{sub_id}/collect")
    async def subscription_collect(
        sub_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ):

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")

        # Check running flag first
        try:
            with get_db(cfg) as conn:
                row = conn.execute(
                    "SELECT running FROM schedules WHERE subscription_url = ?",
                    (sub.url,),
                ).fetchone()
                if row and row["running"]:
                    return JSONResponse({"status": "already_running"})
                conn.execute(
                    "UPDATE schedules SET running = 1 WHERE subscription_url = ?",
                    (sub.url,),
                )
        except Exception as exc:
            logger.warning("collect: schedule check failed for %s: %s", sub.url, exc)

        background_tasks.add_task(_fetch_with_running_guard, cfg, sub.url)
        return JSONResponse({"status": "started"})

    @app.post("/subscriptions/{sub_id}/ground")
    async def subscription_ground(
        sub_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        """Reset to baseline: clear last_fetched_at and trigger a fresh fetch."""

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        # Clear last_fetched_at so the next fetch starts from a clean
        # baseline (the daemon / api.fetch_subscription path uses
        # last_fetched_at to drive its `since` cursor).
        try:
            with get_db(cfg) as conn:
                conn.execute(
                    "UPDATE subscriptions SET last_fetched_at = NULL WHERE url = ?",
                    (sub.url,),
                )
        except Exception as exc:
            logger.warning("clear last_fetched_at failed: %s", exc)
        background_tasks.add_task(_fetch_with_running_guard, cfg, sub.url)
        return JSONResponse({"status": "started"})

    @app.post("/subscriptions/{sub_id}/schedule")
    async def subscription_update_schedule(
        sub_id: str,
        request: Request,
        cron: str = Form(""),
        interval: str = Form(""),
    ):

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")

        cron_val: str | None = cron.strip() or None
        interval_val: int | None = None
        if interval.strip():
            try:
                interval_val = int(interval.strip())
            except ValueError:
                interval_val = None

        # Both-table update: keep the legacy `schedules` table in sync for the
        # detail-view display (next_run_at / last_run_at / running flag live
        # there), but `subscriptions.schedule_*` is the canonical source the
        # daemon reads. If both cron and interval are blank, fall back to the
        # user-configured default for this source_type.
        effective_cron = cron_val
        effective_interval = interval_val
        if effective_cron is None and effective_interval is None:
            effective_cron = cfg.schedule.cron_for(sub.source_type or "webpage")
            if effective_cron is None:
                effective_interval = cfg.schedule.interval_for(
                    sub.source_type or "webpage"
                )

        try:
            with get_db(cfg) as conn:
                conn.execute(
                    "UPDATE schedules SET cron_expr = ?, interval_minutes = ? "
                    "WHERE subscription_url = ?",
                    (effective_cron, effective_interval or 30, sub.url),
                )
                conn.execute(
                    "UPDATE subscriptions SET schedule_cron = ?, "
                    "schedule_interval_minutes = ? WHERE url = ?",
                    (effective_cron, effective_interval or 30, sub.url),
                )
        except Exception as exc:
            logger.warning("update schedule failed for %s: %s", sub.url, exc)
            return RedirectResponse(
                url=f"/subscriptions/{sub_id}?msg=Error:+{exc}",
                status_code=303,
            )

        return RedirectResponse(
            url=f"/subscriptions/{sub_id}?msg=Schedule+updated",
            status_code=303,
        )

    @app.post("/subscriptions/{sub_id}/delete")
    async def subscriptions_delete(sub_id: str):

        cfg = app.state.config
        store = SubscriptionStore(cfg)

        try:
            store.remove_by_id(sub_id)
            return RedirectResponse(
                url="/subscriptions?msg=Subscription+deleted", status_code=303
            )
        except (KeyError, Exception) as exc:
            return RedirectResponse(
                url=f"/subscriptions?msg=Error:+{exc}", status_code=303
            )

    @app.post("/subscriptions/{sub_id}/collection-prompt")
    async def subscription_update_collection_prompt(
        sub_id: str,
        request: Request,
        collection_prompt: str = Form(""),
    ):

        cfg = request.app.state.config
        store = SubscriptionStore(cfg)
        sub = store.get_by_id(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")

        cleaned = (collection_prompt or "").strip()
        current = dict(getattr(sub, "config", None) or {})
        if cleaned:
            current["collection_prompt"] = cleaned
        else:
            current.pop("collection_prompt", None)
        try:
            store.update_config(sub.url, current)
        except Exception as exc:
            logger.warning("collection prompt update failed: %s", exc)
            return RedirectResponse(
                url=f"/subscriptions/{sub_id}?msg=Error:+{exc}", status_code=303
            )

        msg = "Collection prompt saved" if cleaned else "Collection prompt cleared"
        return RedirectResponse(
            url=f"/subscriptions/{sub_id}?msg={msg}", status_code=303
        )

    # ------------------------------------------------------------------
    # Explorations (/explorations) — recipe registration
    # ------------------------------------------------------------------

    def _load_explorations_index(cfg: WikiConfig) -> dict:

        lens_options: list[dict] = []
        drafts: list[dict] = []
        try:
            with get_db(cfg) as conn:
                lens_rows = conn.execute(
                    """SELECT id, name, description
                       FROM lenses
                       WHERE enabled = 1
                       ORDER BY COALESCE(NULLIF(name, ''), id)"""
                ).fetchall()
                lens_options = [dict(row) for row in lens_rows]

                rows = conn.execute(
                    """SELECT id, display_name, original_request, target_surfaces,
                              lens_ids, status, created_at, updated_at
                       FROM explorations
                       ORDER BY updated_at DESC, id DESC
                       LIMIT 20"""
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["target_surfaces"] = _json_loads_or(
                        item.get("target_surfaces"),
                        [],
                    )
                    item["lens_ids"] = _json_loads_or(item.get("lens_ids"), [])
                    last_run = conn.execute(
                        """SELECT status, finished_at, started_at
                           FROM exploration_runs
                           WHERE exploration_id = ?
                           ORDER BY started_at DESC, id DESC
                           LIMIT 1""",
                        (item["id"],),
                    ).fetchone()
                    item["last_run_status"] = last_run["status"] if last_run else ""
                    item["last_run_time"] = (
                        (last_run["finished_at"] or last_run["started_at"])
                        if last_run
                        else ""
                    )
                    drafts.append(item)
        except Exception as exc:
            logger.warning("explorations page failed: %s", exc)

        return {"lens_options": lens_options, "drafts": drafts}

    def _load_exploration_detail(exploration_id: int) -> dict | None:

        with get_db(config) as conn:
            row = conn.execute(
                """SELECT id, display_name, original_request, target_surfaces,
                          lens_ids, status, approved_strategy_version_id,
                          created_at, updated_at
                   FROM explorations
                   WHERE id = ?""",
                (exploration_id,),
            ).fetchone()
            if row is None:
                return None
            exploration = dict(row)
            exploration["target_surfaces"] = _json_loads_or(
                exploration.get("target_surfaces"),
                [],
            )
            exploration["lens_ids"] = _json_loads_or(exploration.get("lens_ids"), [])

            strategy = None
            if exploration["approved_strategy_version_id"] is not None:
                strategy_row = conn.execute(
                    """SELECT id, exploration_id, version, strategy_snapshot,
                              validation_attempt_id, approved_at
                       FROM exploration_strategy_versions
                       WHERE id = ?""",
                    (exploration["approved_strategy_version_id"],),
                ).fetchone()
                if strategy_row is not None:
                    strategy = dict(strategy_row)
                    strategy["strategy_snapshot"] = _json_loads_or(
                        strategy.get("strategy_snapshot"),
                        {},
                    )
                    strategy["strategy_pretty"] = _strategy_pretty(
                        strategy["strategy_snapshot"]
                    )

        with get_db(config) as conn:
            run_rows = conn.execute(
                """SELECT id, exploration_id, strategy_version_id, status,
                          items_found, items_inserted, error,
                          raw_trace_artifact_path, chromux_session_ids,
                          started_at, finished_at
                   FROM exploration_runs
                   WHERE exploration_id = ?
                   ORDER BY started_at DESC, id DESC""",
                (exploration_id,),
            ).fetchall()
            runs: list[dict] = []
            for run_row in run_rows:
                run = dict(run_row)
                run["chromux_session_ids"] = _json_loads_or(
                    run.get("chromux_session_ids"),
                    [],
                )
                run["raw_trace"] = _read_exploration_artifact(
                    config,
                    run.get("raw_trace_artifact_path"),
                )
                run["raw_trace_pretty"] = run["raw_trace"]
                if run["raw_trace"]:
                    parsed_trace = _json_loads_or(run["raw_trace"], None)
                    if parsed_trace is not None:
                        run["raw_trace_pretty"] = _json_pretty(parsed_trace)
                item_rows = conn.execute(
                    """SELECT ri.id, ri.title, ri.url, ri.status
                       FROM raw_item_discoveries d
                       JOIN raw_items ri ON ri.id = d.raw_item_id
                       WHERE d.owner_type = 'exploration_run'
                         AND d.owner_run_id = ?
                         AND d.deleted_at IS NULL
                       ORDER BY ri.id DESC""",
                    (run["id"],),
                ).fetchall()
                run["raw_items"] = [dict(item_row) for item_row in item_rows]
                runs.append(run)

        status_label = (
            "Registered"
            if exploration["status"] == "registered"
            else str(exploration["status"])
        )
        return {
            "exploration": exploration,
            "strategy": strategy,
            "runs": runs,
            "latest_run": runs[0] if runs else None,
            "status_label": status_label,
        }

    @app.get("/explorations", response_class=HTMLResponse)
    async def explorations_page(request: Request):
        cfg = request.app.state.config
        index_context = _load_explorations_index(cfg)

        return templates.TemplateResponse(
            request=request,
            name="explorations.html",
            context={
                "title": "Explorations",
                "msg": request.query_params.get("msg", ""),
                "error": request.query_params.get("error", ""),
                "surface_options": _EXPLORATION_SURFACE_OPTIONS,
                "lens_options": index_context["lens_options"],
                "drafts": index_context["drafts"],
                "suggested_display_name": "Exploration",
                "selected": None,
            },
        )

    @app.get("/explorations/new", response_class=HTMLResponse)
    async def explorations_new(request: Request):
        return await explorations_page(request)

    @app.post("/explorations")
    async def explorations_create_registered(
        request: Request,
        original_request: str = Form(...),
        recipe_markdown: str = Form(""),
        display_name: str = Form(""),
        target_surfaces: list[str] | None = Form(None),
        lens_ids: list[str] | None = Form(None),
    ):
        from contents_hub.explorations import ExplorationStore

        cfg = request.app.state.config
        cleaned_request = (original_request or "").strip()
        if not cleaned_request:
            return RedirectResponse(
                url="/explorations?error=Exploration+request+is+required",
                status_code=303,
            )
        cleaned_recipe = (recipe_markdown or "").strip()
        if not cleaned_recipe:
            return RedirectResponse(
                url="/explorations?error=Recipe+Markdown+is+required",
                status_code=303,
            )

        selected_surfaces = _clean_exploration_surfaces(target_surfaces)
        display = (display_name or "").strip() or _suggest_exploration_display_name(
            cleaned_request
        )

        available_lens_ids: set[str] = set()
        try:
            with get_db(cfg) as conn:
                rows = conn.execute(
                    "SELECT id FROM lenses WHERE enabled = 1"
                ).fetchall()
                available_lens_ids = {str(row["id"]) for row in rows}
        except Exception as exc:
            logger.warning("exploration lens lookup failed: %s", exc)

        selected_lens_ids = _clean_lens_ids(lens_ids, available_lens_ids)
        try:
            exploration, _strategy = ExplorationStore(cfg).create_registered_with_recipe(
                display_name=display,
                original_request=cleaned_request,
                recipe_markdown=cleaned_recipe,
                target_surfaces=selected_surfaces,
                lens_ids=selected_lens_ids,
            )
        except Exception as exc:
            logger.warning("exploration registration failed: %s", exc)
            return RedirectResponse(
                url=f"/explorations?error=Error:+{exc}",
                status_code=303,
            )

        return RedirectResponse(
            url=(
                f"/explorations?msg=Exploration+registered:+"
                f"{quote_plus(exploration.display_name)}"
            ),
            status_code=303,
        )

    @app.get("/explorations/{exploration_id}", response_class=HTMLResponse)
    async def exploration_detail(request: Request, exploration_id: int):
        selected = _load_exploration_detail(exploration_id)
        if selected is None:
            raise HTTPException(status_code=404, detail="Exploration not found")
        cfg = request.app.state.config
        index_context = _load_explorations_index(cfg)
        return templates.TemplateResponse(
            request=request,
            name="explorations.html",
            context={
                "title": selected["exploration"]["display_name"],
                "msg": request.query_params.get("msg", ""),
                "error": request.query_params.get("error", ""),
                "surface_options": _EXPLORATION_SURFACE_OPTIONS,
                "lens_options": index_context["lens_options"],
                "drafts": index_context["drafts"],
                "suggested_display_name": "Exploration",
                "selected": selected,
            },
        )

    @app.post("/explorations/{exploration_id}/run")
    async def exploration_run(request: Request, exploration_id: int):
        from contents_hub.explorations import ExplorationStrategyRunner

        cfg = request.app.state.config
        try:
            run = await ExplorationStrategyRunner(cfg).run_registered(exploration_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Exploration not found")
        except ValueError as exc:
            return RedirectResponse(
                url=f"/explorations/{exploration_id}?error={quote_plus(str(exc))}",
                status_code=303,
            )
        except Exception as exc:
            logger.warning("exploration manual run failed: %s", exc)
            return RedirectResponse(
                url=f"/explorations/{exploration_id}?error=Run+error:+{quote_plus(str(exc))}",
                status_code=303,
            )

        return RedirectResponse(
            url=(
                f"/explorations/{exploration_id}?msg=Manual+run+{run.status}:"
                f"+{run.items_found}+found,+{run.items_inserted}+new"
            ),
            status_code=303,
        )

    # ------------------------------------------------------------------
    # Settings (/settings) — global browser + schedule defaults
    # ------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        cfg = request.app.state.config
        sched = cfg.schedule
        interval_options = [
            {"label": "15 min", "value": 15},
            {"label": "Hourly", "value": 60},
            {"label": "Every 3 hours", "value": 180},
            {"label": "Every 6 hours", "value": 360},
            {"label": "Daily", "value": 1440},
            {"label": "Weekly", "value": 10080},
        ]
        # Show a concrete selection: user override wins, else the rss default
        # (the most-common "active" interval) as a reasonable fallback.
        current_interval = sched.global_interval or sched.interval_for("rss") or 60
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "title": "Settings",
                "msg": request.query_params.get("msg", ""),
                "global_interval": current_interval,
                "interval_options": interval_options,
                "browser_presets": _BROWSER_PRESETS,
            },
        )

    @app.post("/settings/schedule")
    async def settings_save_schedule(
        request: Request,
        global_interval: str = Form(""),
        global_cron: str = Form(""),
    ):
        from contents_hub.config import load_config, save_schedule_defaults

        cfg = request.app.state.config

        def _int_or_none(s: str) -> int | None:
            s = (s or "").strip()
            if not s:
                return None
            try:
                v = int(s)
                return v if v > 0 else None
            except ValueError:
                return None

        try:
            save_schedule_defaults(
                cfg.vault_path,
                global_interval=_int_or_none(global_interval),
                global_cron=(global_cron.strip() or None),
                per_type={},
            )
            request.app.state.config = load_config(cfg.vault_path)
        except Exception as exc:
            logger.warning("save schedule defaults failed: %s", exc)
            return RedirectResponse(
                url=f"/settings?msg=Error:+{exc}",
                status_code=303,
            )
        return RedirectResponse(
            url="/settings?msg=Schedule+defaults+saved",
            status_code=303,
        )

    @app.post("/settings/browser/open")
    async def settings_browser_open(
        url: str = Form(...), confirmed: bool = Form(False)
    ):
        """Open a URL in a HEADED contents-hub window.

        If a headless agent fetch is currently using the same profile, the
        first call returns ``409 needs_confirm`` so the UI can warn the user
        before we kill that fetch and relaunch headed.
        """
        import uuid

        target = (url or "").strip()
        if not target:
            return JSONResponse(
                {"status": "error", "error": "url required"}, status_code=400
            )
        if not target.startswith(("http://", "https://")):
            target = "https://" + target
        result = _open_chromux(
            target, session=f"settings-{uuid.uuid4().hex[:8]}", confirmed=confirmed
        )
        if result["status"] == "error":
            logger.warning("settings/browser/open failed: %s", result["error"])
            return JSONResponse(result, status_code=500)
        if result["status"] == "needs_confirm":
            return JSONResponse(result, status_code=409)
        return JSONResponse(result)

    @app.post("/settings/browser/launch")
    async def settings_browser_launch(confirmed: bool = Form(False)):
        """Launch a HEADED contents-hub window so the user can navigate freely.

        Returns ``409 needs_confirm`` if a headless agent fetch is already
        running on the profile.
        """
        result = _open_chromux(None, confirmed=confirmed)
        if result["status"] == "error":
            logger.warning("settings/browser/launch failed: %s", result["error"])
            return JSONResponse(result, status_code=500)
        if result["status"] == "needs_confirm":
            return JSONResponse(result, status_code=409)
        return JSONResponse(result)

    @app.post("/settings/browser/resume-background")
    async def settings_browser_resume_background():
        """Release the foreground profile so the next fetch can run headless."""
        result = kill_chromux_profile()
        if result["status"] == "error":
            logger.warning(
                "settings/browser/resume-background failed: %s", result["error"]
            )
            return JSONResponse(result, status_code=500)
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # Lens Inbox (/lens-inbox)
    # ------------------------------------------------------------------

    def _candidate_to_dict(c) -> dict:
        return {
            "id": c.id,
            "title": c.title,
            "url": c.url,
            "status": c.status,
            "collected_at": c.collected_at,
            "body_preview": c.body_preview,
            "subscription_id": c.subscription_id,
            "subscription_label": c.subscription_label,
            "lenses": [
                {"id": lm.id, "summary": lm.summary, "bullets": list(lm.bullets)}
                for lm in c.lenses
            ],
            "lens_ids": [lm.id for lm in c.lenses],
            "representative": {
                "id": c.representative.id,
                "summary": c.representative.summary,
                "bullets": list(c.representative.bullets),
            },
            "source_note_path": c.source_note_path,
        }

    def _serialize_view(view: dict) -> dict:
        return {
            "view_mode": view["view_mode"],
            "scope_status": view["scope_status"],
            "candidate_count": view["candidate_count"],
            "is_empty": view["is_empty"],
            "applied_filters": view["applied_filters"],
            "candidates": [_candidate_to_dict(c) for c in view["candidates"]],
            "groups": [
                {
                    "lens_id": g["lens_id"],
                    "lens_name": g["lens_name"],
                    "candidates": [_candidate_to_dict(c) for c in g["candidates"]],
                }
                for g in view["groups"]
            ],
        }

    @app.get("/lens-inbox", response_class=HTMLResponse)
    async def lens_inbox_page(request: Request):
        from contents_hub.lens_inbox import (
            ALLOWED_STATUSES,
            ALLOWED_VIEW_MODES,
            DEFAULT_STATUS,
            VIEW_MODE_LIST,
            list_lens_filter_options,
            list_subscription_filter_options,
            query_lens_inbox,
        )

        cfg = request.app.state.config
        sources_dirname = cfg.sources_dir

        view: dict
        lens_options: list[dict] = []
        sub_options: list[dict] = []
        try:
            with get_db(cfg) as conn:
                view = query_lens_inbox(
                    conn,
                    sources_dirname=sources_dirname,
                    status=DEFAULT_STATUS,
                    view_mode=VIEW_MODE_LIST,
                )
                lens_options = list_lens_filter_options(conn)
                sub_options = list_subscription_filter_options(conn)
        except Exception as exc:
            logger.warning("lens-inbox initial render failed: %s", exc)
            view = {
                "view_mode": VIEW_MODE_LIST,
                "scope_status": DEFAULT_STATUS,
                "candidates": [],
                "groups": [],
                "candidate_count": 0,
                "is_empty": True,
                "applied_filters": {
                    "status": DEFAULT_STATUS,
                    "lens_id": None,
                    "subscription_id": None,
                },
            }

        return templates.TemplateResponse(
            request=request,
            name="lens_inbox.html",
            context={
                "title": "Lens Inbox",
                "msg": request.query_params.get("msg", ""),
                "view": view,
                "lens_options": lens_options,
                "subscription_options": sub_options,
                "status_options": list(ALLOWED_STATUSES),
                "view_mode_options": list(ALLOWED_VIEW_MODES),
                "default_status": DEFAULT_STATUS,
                "default_view_mode": VIEW_MODE_LIST,
            },
        )

    @app.get("/lens-inbox/data")
    async def lens_inbox_data(request: Request):
        """Return Lens Inbox candidate data as JSON for client-side filtering.

        Used by the page's filter/view-mode controls so they can refresh the
        candidate region without changing the URL (R-U2.5, R-T3.4). The
        ``html`` field carries the rendered region partial so the client can
        drop it into ``#li-region`` without re-implementing the template.
        """
        from contents_hub.lens_inbox import query_lens_inbox

        cfg = request.app.state.config
        params = request.query_params
        try:
            with get_db(cfg) as conn:
                view = query_lens_inbox(
                    conn,
                    sources_dirname=cfg.sources_dir,
                    status=params.get("status") or None,
                    lens_id=params.get("lens_id") or None,
                    subscription_id=params.get("subscription_id") or None,
                    view_mode=params.get("view_mode") or None,
                )
        except Exception as exc:
            logger.warning("lens-inbox data fetch failed: %s", exc)
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=500
            )
        payload = _serialize_view(view)
        payload["ok"] = True
        try:
            payload["html"] = templates.get_template(
                "_lens_inbox_region.html"
            ).render({"view": view})
        except Exception as exc:
            logger.warning("lens-inbox region render failed: %s", exc)
            payload["html"] = ""
        return JSONResponse(payload)

    # ------------------------------------------------------------------
    # Raw item → source promotion (manual "Save" button)
    # ------------------------------------------------------------------

    @app.post("/raw-items/{item_id}/save")
    async def raw_item_save(item_id: int, request: Request):
        from contents_hub.promote import promote_raw_item, PromoteError

        cfg = request.app.state.config
        try:
            path = promote_raw_item(cfg, item_id)
        except PromoteError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            logger.warning("promote failed for item %d: %s", item_id, exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse(
            {
                "ok": True,
                "path": str(path.relative_to(cfg.vault_path)),
                "status": "promoted",
            }
        )

    @app.post("/raw-items/{item_id}/archive")
    async def raw_item_archive(item_id: int, request: Request):
        """Archive a ``raw`` item.

        Returns 200 ``{ok, status: "archived"}`` on success, 404 if no row,
        409 if the item is already promoted (R-T2.3 — promoted source files
        must not be deleted or modified).
        """


        cfg = request.app.state.config
        try:
            with get_db(cfg) as conn:
                row = conn.execute(
                    "SELECT id, status FROM raw_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    return JSONResponse(
                        {"ok": False, "error": f"raw item {item_id} not found"},
                        status_code=404,
                    )
                current = row["status"] or ""
                if current == "promoted":
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "promoted items cannot be archived",
                            "status": "promoted",
                        },
                        status_code=409,
                    )
                if current == "archived":
                    return JSONResponse(
                        {"ok": True, "status": "archived", "noop": True}
                    )
                if current != "raw":
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": f"cannot archive item in status {current!r}",
                            "status": current,
                        },
                        status_code=409,
                    )
                now_str = now_iso()
                conn.execute(
                    "UPDATE raw_items SET status = 'archived', "
                    "updated_at = ? WHERE id = ?",
                    (now_str, item_id),
                )
        except Exception as exc:
            logger.warning("archive failed for item %d: %s", item_id, exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "status": "archived"})

    @app.post("/raw-items/{item_id}/restore")
    async def raw_item_restore(item_id: int, request: Request):
        """Restore an ``archived`` item back to ``raw``.

        Returns 200 ``{ok, status: "raw"}`` on success. Refuses to restore
        promoted or already-raw items. ``raw_item_lenses`` rows are untouched
        — Lens metadata persists across the round-trip (R-B2.6).
        """


        cfg = request.app.state.config
        try:
            with get_db(cfg) as conn:
                row = conn.execute(
                    "SELECT id, status FROM raw_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    return JSONResponse(
                        {"ok": False, "error": f"raw item {item_id} not found"},
                        status_code=404,
                    )
                current = row["status"] or ""
                if current == "raw":
                    return JSONResponse({"ok": True, "status": "raw", "noop": True})
                if current != "archived":
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": f"only archived items can be restored "
                            f"(current status: {current!r})",
                            "status": current,
                        },
                        status_code=409,
                    )
                now_str = now_iso()
                conn.execute(
                    "UPDATE raw_items SET status = 'raw', "
                    "updated_at = ? WHERE id = ?",
                    (now_str, item_id),
                )
        except Exception as exc:
            logger.warning("restore failed for item %d: %s", item_id, exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "status": "raw"})

    return app
