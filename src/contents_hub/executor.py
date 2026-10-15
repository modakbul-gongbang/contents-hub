"""Single Agent Executor.

`list_items(sub, ...)` and `content_items(sub, ...)` are the production
subscription primitives. The API layer runs LIST -> DB DIFF -> CONTENT so
known items do not pay the detail-fetch cost again. `execute(sub, ...)`
remains available only as a compatibility one-shot agent execution path; it
does not explore, relearn, or mutate subscription recipes at runtime.

All agent calls go through `runner.run(...)` (R-T14.1 / INV-1).  No
`claude_agent_sdk` / `anthropic` import lives here.

All diagnostic output is emitted via `logging.getLogger(__name__)` which
the CLI / daemon route to `.contents-hub/cli.log` or
`.contents-hub/daemon.log`. Nothing is written to stdout from this module.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from contents_hub.models import (
    FetchedItem,
    FetchFailureReason,
    FetchResult,
    ListFetchResult,
    ListItem,
    infer_from_error,
)
from contents_hub.platform_lists import (
    PlatformListTransientError,
    is_linkedin_profile_source,
    is_x_profile_source,
    list_linkedin_profile_items,
    list_x_profile_items,
)
from contents_hub.recipes import RecipeRegistry
from contents_hub.runners import AgentRunner, get_browser_backend, get_default_runner

logger = logging.getLogger(__name__)


DEFAULT_MAX_ITEMS = 50
_PLATFORM_DIRECT_TRANSIENT_RETRIES = 1
_PLATFORM_DIRECT_RETRY_DELAY_SECONDS = 1.0
_YOUTUBE_CHANNEL_SOURCE_TYPES = {"youtube", "youtube.channel"}
_RSS_FEED_SOURCE_TYPES = {"rss", "rss.feed"}
_GITHUB_RELEASES_SOURCE_TYPES = {"github.releases"}
_REDDIT_SUBREDDIT_SOURCE_TYPES = {"reddit", "reddit.subreddit"}
_SUBSTACK_SOURCE_TYPES = {"substack", "substack.publication", "substack.tag"}
_WEBPAGE_SOURCE_TYPES = {"agent", "webpage"}
_YOUTUBE_VIDEO_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
_YOUTUBE_CHANNEL_ID_PATTERNS = (
    re.compile(r"channel_id=(UC[A-Za-z0-9_-]{20,})"),
    re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{20,})"'),
    re.compile(r'"browseId":"(UC[A-Za-z0-9_-]{20,})"'),
)
_YOUTUBE_WATCH_SOURCE_TYPES = {"youtube", "youtube.channel", "youtube.video"}
_NO_COLLECTION_PROMPT = "No additional user collection guidance."


def _collection_prompt_from_config(config: dict[str, Any] | None) -> str:
    if not isinstance(config, dict):
        return _NO_COLLECTION_PROMPT
    prompt = str(config.get("collection_prompt") or "").strip()
    return prompt or _NO_COLLECTION_PROMPT


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    m = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _parse_items_json(
    text: str,
) -> tuple[list[dict], list[str], FetchFailureReason | None]:
    obj = _extract_json_object(text)
    if isinstance(obj, dict):
        items = obj.get("items") or []
        errors = obj.get("errors") or []
        if not isinstance(items, list):
            items = []
        if not isinstance(errors, list):
            errors = []
        reason = FetchFailureReason.parse(obj.get("failure_reason"))
        return items, [str(e) for e in errors], reason

    m = re.search(r"\[[\s\S]*\]", text or "")
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return arr, [], None
        except json.JSONDecodeError:
            pass
    return [], [], None


def _parse_failure_json(text: str) -> FetchFailureReason | None:
    obj = _extract_json_object(text)
    if isinstance(obj, dict):
        return FetchFailureReason.parse(obj.get("failure_reason"))
    return None


def _build_items(
    entries: list[dict],
    *,
    max_items: int,
    source_type: str,
    fetch_method: str,
) -> list[FetchedItem]:
    items: list[FetchedItem] = []
    for entry in entries[:max_items]:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url", "")
        if not url:
            continue

        published_at = None
        raw_date = entry.get("published_at") or entry.get("published") or ""
        if raw_date:
            try:
                from dateutil.parser import parse as parse_date

                published_at = parse_date(raw_date)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                published_at = None

        body = (
            entry.get("body_markdown")
            or entry.get("content_html")
            or entry.get("content")
            or ""
        )
        body_status = entry.get("body_status", "")
        passthrough_extra = {
            key: value
            for key, value in entry.items()
            if key
            not in {
                "url",
                "title",
                "summary",
                "author",
                "published_at",
                "published",
                "body_markdown",
                "content",
                "content_html",
                "tags",
                "source_type",
                "extra",
            }
        }
        explicit_extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}

        items.append(
            FetchedItem(
                url=url,
                title=entry.get("title", ""),
                summary=entry.get("summary", ""),
                author=entry.get("author", ""),
                published_at=published_at,
                content_html=body,
                source_type=source_type,
                extra={
                    **passthrough_extra,
                    **explicit_extra,
                    "fetch_method": fetch_method,
                    "body_status": body_status,
                },
            )
        )
    return items


# ---------------------------------------------------------------------------
# Recipe proxy — RecipeRegistry expects an object with `.config` /
# `.source_type` / `.recipe_base`. A real Subscription dataclass exposes
# the first two; recipe_base falls back to source_type if absent.
# ---------------------------------------------------------------------------


def _recipe_proxy(sub: Any) -> Any:
    config = getattr(sub, "config", None)
    if not isinstance(config, dict):
        config = {}
    source_type = getattr(sub, "source_type", "") or "webpage"
    recipe_base = config.get("recipe_base") or source_type
    return SimpleNamespace(
        config=config,
        source_type=source_type,
        recipe_base=recipe_base,
    )


def _record_failure(
    sub_url: str,
    config: dict[str, Any],
    reason: str,
    *,
    failure_reason: FetchFailureReason | None = None,
) -> FetchResult:
    config["consecutive_failures"] = (
        int(config.get("consecutive_failures", 0) or 0) + 1
    )
    enum_value = (
        failure_reason.value
        if failure_reason is not None
        else infer_from_error(reason).value
    )
    logger.warning(
        "executor: fetch failed for %s: %s (failure_reason=%s)",
        sub_url, reason, enum_value,
    )
    return FetchResult(
        ok=False,
        source_url=sub_url,
        error=reason,
        error_type="AGENT_ERROR",
        failure_reason=enum_value,
    )


def _record_success(
    config: dict[str, Any],
    items: list[FetchedItem],
    sub_url: str,
    total_available: int,
    *,
    fetch_method: str,
) -> FetchResult:
    config["consecutive_failures"] = 0
    config["fetch_method"] = fetch_method
    config.pop("relearn_count", None)
    config.pop("needs_error_status", None)
    config.pop("allow_relearn", None)
    config.pop("allow_explore", None)
    return FetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        source_title=sub_url,
        total_available=total_available,
    )


# ---------------------------------------------------------------------------
# Trial helpers — list-only / detail-only passes for preview validation
# ---------------------------------------------------------------------------


def _parse_list_urls(text: str) -> tuple[list[dict], list[str], FetchFailureReason | None]:
    """Parse a list_prompt response into (entries, errors, failure_reason).

    Each entry is a dict with at least ``url``; ``title_hint`` /
    ``published_hint`` are optional. Empty / non-string urls are dropped
    and order is preserved while de-duplicating.
    """
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        return [], [], None

    raw_items = obj.get("items") or []
    raw_errors = obj.get("errors") or []
    if not isinstance(raw_items, list):
        raw_items = []
    if not isinstance(raw_errors, list):
        raw_errors = []
    reason = FetchFailureReason.parse(obj.get("failure_reason"))

    seen: set[str] = set()
    entries: list[dict] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        entries.append(
            {
                "item_key": entry.get("item_key", "") or url,
                "url": url,
                "title_hint": entry.get("title_hint", "") or "",
                "published_hint": entry.get("published_hint", "") or "",
                "card_text": entry.get("card_text", "") or "",
                "source_payload": entry.get("source_payload") or {},
            }
        )

    return entries, [str(e) for e in raw_errors], reason


def _build_list_items(entries: list[dict]) -> list[ListItem]:
    items: list[ListItem] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        source_payload = entry.get("source_payload")
        if not isinstance(source_payload, dict):
            source_payload = {}
        items.append(
            ListItem(
                item_key=str(entry.get("item_key") or url),
                url=url,
                title_hint=str(entry.get("title_hint") or ""),
                published_hint=str(entry.get("published_hint") or ""),
                card_text=str(entry.get("card_text") or ""),
                source_payload=source_payload,
            )
        )
    return items


def _is_youtube_channel_source(source_type: str, url: str) -> bool:
    if source_type in _YOUTUBE_CHANNEL_SOURCE_TYPES:
        return True
    host = urlparse(url).netloc.lower()
    return host.endswith("youtube.com") or host == "youtu.be"


async def _fetch_json_url(url: str) -> dict[str, Any]:
    from contents_hub.tools.fetchers import fetch_url

    try:
        raw = await fetch_url(url, mode="raw", max_chars=0)
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "url": url,
            "body": "",
            "error": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "status": 0,
            "url": url,
            "body": "",
            "error": "fetch_url returned non-object JSON",
        }
    return payload


def _is_rss_feed_source(source_type: str) -> bool:
    return (source_type or "").lower() in _RSS_FEED_SOURCE_TYPES


def _url_path_parts(url: str) -> list[str]:
    try:
        return [part for part in urlparse(url).path.split("/") if part]
    except Exception:
        return []


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().lstrip("www.")
    except Exception:
        return ""


def _is_github_releases_source(source_type: str, url: str) -> bool:
    if (source_type or "").lower() in _GITHUB_RELEASES_SOURCE_TYPES:
        return True
    parts = _url_path_parts(url)
    return (
        _url_host(url) == "github.com"
        and len(parts) >= 3
        and parts[2].lower() in {"releases", "releases.atom"}
    )


def _github_releases_atom_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/releases.atom"):
        atom_path = path
    elif path.endswith("/releases"):
        atom_path = f"{path}.atom"
    else:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            atom_path = f"/{parts[0]}/{parts[1]}/releases.atom"
        else:
            atom_path = path or "/releases.atom"
    return parsed._replace(path=atom_path, query="", fragment="").geturl()


async def _list_github_releases_items(sub_url: str) -> ListFetchResult | None:
    atom_url = _github_releases_atom_url(sub_url)
    result = await _list_rss_feed_items(atom_url)
    if result is None:
        return None
    return ListFetchResult(
        ok=result.ok,
        items=result.items,
        source_url=sub_url,
        total_available=result.total_available,
        error=result.error,
        failure_reason=result.failure_reason,
    )


def _is_reddit_subreddit_source(source_type: str, url: str) -> bool:
    if (source_type or "").lower() in _REDDIT_SUBREDDIT_SOURCE_TYPES:
        return True
    parts = _url_path_parts(url)
    return _url_host(url).endswith("reddit.com") and len(parts) >= 2 and parts[0].lower() == "r"


def _reddit_subreddit_from_url(url: str) -> str:
    parts = _url_path_parts(url)
    for index, part in enumerate(parts):
        if part.lower() == "r" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _reddit_listing_url(sub_url: str) -> str:
    subreddit = _reddit_subreddit_from_url(sub_url)
    if not subreddit:
        return ""
    return f"https://www.reddit.com/r/{quote(subreddit)}/new.json?limit={DEFAULT_MAX_ITEMS}"


def _iso_from_epoch_seconds(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _reddit_permalink_url(permalink: str) -> str:
    if permalink.startswith("http://") or permalink.startswith("https://"):
        return permalink
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink}"
    return f"https://www.reddit.com/{permalink}"


def _compact_reddit_post(post: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "name",
        "title",
        "permalink",
        "created_utc",
        "selftext",
        "selftext_html",
        "url",
        "url_overridden_by_dest",
        "author",
        "subreddit",
        "subreddit_name_prefixed",
        "score",
        "ups",
        "num_comments",
        "link_flair_text",
        "over_18",
        "spoiler",
        "stickied",
        "is_self",
        "post_hint",
        "domain",
    )
    snapshot = {field: post.get(field) for field in fields if field in post}
    snapshot["created_at"] = _iso_from_epoch_seconds(post.get("created_utc"))
    return snapshot


async def _list_reddit_subreddit_items(sub_url: str) -> ListFetchResult | None:
    listing_url = _reddit_listing_url(sub_url)
    if not listing_url:
        return None

    fetched = await _fetch_json_url(listing_url)
    if not fetched.get("ok"):
        logger.info(
            "executor LIST reddit direct fetch failed url=%s error=%s; falling back to agent",
            listing_url,
            fetched.get("error") or fetched.get("status"),
        )
        return None

    try:
        payload = json.loads(str(fetched.get("body") or ""))
    except json.JSONDecodeError as exc:
        logger.info(
            "executor LIST reddit direct JSON parse failed url=%s error=%s; falling back to agent",
            listing_url,
            exc,
        )
        return None

    children = (((payload or {}).get("data") or {}).get("children") or [])
    if not isinstance(children, list):
        return None

    items: list[ListItem] = []
    seen: set[str] = set()
    for child in children:
        if not isinstance(child, dict):
            continue
        post = child.get("data")
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or post.get("name") or "").strip()
        permalink = str(post.get("permalink") or "").strip()
        url = _reddit_permalink_url(permalink) if permalink else ""
        if not post_id or not url or url in seen:
            continue
        seen.add(url)

        snapshot = _compact_reddit_post(post)
        title = str(post.get("title") or "")
        selftext = str(post.get("selftext") or "")
        external_url = str(post.get("url_overridden_by_dest") or post.get("url") or "")
        card_text = selftext.strip() or (
            f"{title}\n{external_url}" if external_url and external_url != url else title
        )
        items.append(
            ListItem(
                item_key=f"reddit:post:{post_id}",
                url=url,
                title_hint=title,
                published_hint=str(snapshot.get("created_at") or ""),
                card_text=card_text,
                source_payload={
                    "listing_url": listing_url,
                    "reddit_post": snapshot,
                },
            )
        )

    if not items:
        return ListFetchResult(
            ok=False,
            source_url=sub_url,
            error="reddit listing returned 0 posts",
            failure_reason=FetchFailureReason.STRUCTURE_CHANGED.value,
        )

    logger.info("executor LIST reddit direct url=%s items=%d", sub_url, len(items))
    return ListFetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        total_available=len(items),
    )


def _content_from_reddit_snapshots(
    *,
    sub_url: str,
    source_type: str,
    config: dict[str, Any],
    candidates: list[ListItem],
    max_items: int,
    total_available: int | None,
    fetch_method: str,
) -> FetchResult | None:
    items: list[FetchedItem] = []
    for candidate in candidates[:max_items]:
        payload = candidate.source_payload if isinstance(candidate.source_payload, dict) else {}
        post = payload.get("reddit_post") if isinstance(payload.get("reddit_post"), dict) else {}
        if not post:
            continue
        title = str(post.get("title") or candidate.title_hint or candidate.url)
        selftext = str(post.get("selftext") or "").strip()
        external_url = str(post.get("url_overridden_by_dest") or post.get("url") or "").strip()
        summary = selftext[:500] if selftext else (
            external_url if external_url and external_url != candidate.url else candidate.card_text[:500]
        )
        content = selftext or candidate.card_text or title
        if external_url and external_url != candidate.url and external_url not in content:
            content = f"{content}\n\nExternal URL: {external_url}".strip()
        flair = str(post.get("link_flair_text") or "").strip()
        items.append(
            FetchedItem(
                url=candidate.url,
                title=title,
                summary=summary,
                author=str(post.get("author") or ""),
                published_at=_parse_datetime_hint(str(post.get("created_at") or candidate.published_hint or "")),
                tags=[flair] if flair else [],
                content_html=content,
                source_type=source_type,
                extra={
                    "fetch_method": fetch_method,
                    "body_status": "full" if selftext else "metadata_only",
                    "item_key": candidate.item_key,
                    "source_payload": payload,
                    "external_url": external_url,
                    "score": post.get("score") or post.get("ups"),
                    "num_comments": post.get("num_comments"),
                    "subreddit": post.get("subreddit") or "",
                },
            )
        )

    if not items:
        return None

    logger.info("executor CONTENT reddit direct url=%s n_items=%d", sub_url, len(items))
    return _record_success(
        config,
        items,
        sub_url,
        total_available=total_available if total_available is not None else len(candidates),
        fetch_method=fetch_method,
    )


def _substack_archive_url(sub_url: str) -> str:
    parsed = urlparse(sub_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "t":
        return ""
    query = urlencode({"sort": "new", "tag": parts[1], "limit": str(DEFAULT_MAX_ITEMS)})
    return parsed._replace(path="/api/v1/archive", query=query, fragment="").geturl()


def _substack_feed_url(sub_url: str) -> str:
    parsed = urlparse(sub_url)
    return parsed._replace(path="/feed", query="", fragment="").geturl()


def _is_substack_source(source_type: str, url: str) -> bool:
    if (source_type or "").lower() in _SUBSTACK_SOURCE_TYPES:
        return True
    if _url_host(url).endswith("substack.com"):
        return True
    return bool(_substack_archive_url(url))


def _substack_posts_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [post for post in payload if isinstance(post, dict)]
    if isinstance(payload, dict):
        for key in ("posts", "items", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [post for post in value if isinstance(post, dict)]
    return []


def _substack_author_names(post: dict[str, Any]) -> str:
    bylines = post.get("publishedBylines")
    if not isinstance(bylines, list):
        return ""
    names = [
        str(byline.get("name") or byline.get("handle") or "").strip()
        for byline in bylines
        if isinstance(byline, dict)
    ]
    return ", ".join(name for name in names if name)


def _substack_tags(post: dict[str, Any]) -> list[str]:
    raw_tags = post.get("postTags")
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, dict):
                value = tag.get("name") or tag.get("slug")
            else:
                value = tag
            value = str(value or "").strip()
            if value:
                tags.append(value)
    section = str(post.get("section_name") or post.get("section_slug") or "").strip()
    if section:
        tags.append(section)
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return unique


def _compact_substack_post(post: dict[str, Any], *, host_url: str) -> dict[str, Any]:
    fields = (
        "id",
        "title",
        "subtitle",
        "description",
        "search_engine_description",
        "truncated_body_text",
        "body_html",
        "canonical_url",
        "slug",
        "post_date",
        "published_at",
        "cover_image",
        "type",
        "section_name",
        "section_slug",
        "wordcount",
        "comment_count",
        "child_comment_count",
        "reaction_count",
        "podcast_url",
        "publishedBylines",
        "postTags",
    )
    snapshot = {field: post.get(field) for field in fields if field in post}
    if not snapshot.get("canonical_url") and snapshot.get("slug"):
        parsed = urlparse(host_url)
        snapshot["canonical_url"] = parsed._replace(
            path=f"/p/{snapshot['slug']}",
            query="",
            fragment="",
        ).geturl()
    return snapshot


async def _list_substack_items(sub_url: str, source_type: str) -> ListFetchResult | None:
    archive_url = _substack_archive_url(sub_url)
    if archive_url:
        fetched = await _fetch_json_url(archive_url)
        if not fetched.get("ok"):
            logger.info(
                "executor LIST substack archive fetch failed url=%s error=%s; falling back to agent",
                archive_url,
                fetched.get("error") or fetched.get("status"),
            )
            return None
        try:
            payload = json.loads(str(fetched.get("body") or ""))
        except json.JSONDecodeError as exc:
            logger.info(
                "executor LIST substack archive JSON parse failed url=%s error=%s; falling back to agent",
                archive_url,
                exc,
            )
            return None

        posts = _substack_posts_from_payload(payload)
        items: list[ListItem] = []
        seen: set[str] = set()
        for post in posts:
            snapshot = _compact_substack_post(post, host_url=sub_url)
            item_url = str(snapshot.get("canonical_url") or post.get("url") or "").strip()
            if not item_url or item_url in seen:
                continue
            seen.add(item_url)
            title = str(snapshot.get("title") or item_url)
            summary = str(
                snapshot.get("subtitle")
                or snapshot.get("description")
                or snapshot.get("search_engine_description")
                or snapshot.get("truncated_body_text")
                or ""
            )
            item_id = str(snapshot.get("id") or snapshot.get("slug") or item_url)
            items.append(
                ListItem(
                    item_key=f"substack:post:{item_id}",
                    url=item_url,
                    title_hint=title,
                    published_hint=str(snapshot.get("post_date") or snapshot.get("published_at") or ""),
                    card_text=summary,
                    source_payload={
                        "archive_url": archive_url,
                        "substack_post": snapshot,
                    },
                )
            )

        if items:
            logger.info("executor LIST substack archive url=%s items=%d", sub_url, len(items))
            return ListFetchResult(
                ok=True,
                items=items,
                source_url=sub_url,
                total_available=len(items),
            )
        return ListFetchResult(
            ok=False,
            source_url=sub_url,
            error="substack archive returned 0 posts",
            failure_reason=FetchFailureReason.STRUCTURE_CHANGED.value,
        )

    if (source_type or "").lower() in {"substack", "substack.publication"} or _url_host(sub_url).endswith("substack.com"):
        feed_url = _substack_feed_url(sub_url)
        result = await _list_rss_feed_items(feed_url)
        if result is None:
            return None
        return ListFetchResult(
            ok=result.ok,
            items=result.items,
            source_url=sub_url,
            total_available=result.total_available,
            error=result.error,
            failure_reason=result.failure_reason,
        )
    return None


def _content_from_substack_snapshots(
    *,
    sub_url: str,
    source_type: str,
    config: dict[str, Any],
    candidates: list[ListItem],
    max_items: int,
    total_available: int | None,
    fetch_method: str,
) -> FetchResult | None:
    if candidates and all(
        isinstance(candidate.source_payload, dict)
        and isinstance(candidate.source_payload.get("feed_item"), dict)
        for candidate in candidates
    ):
        return _content_from_rss_entries(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )

    items: list[FetchedItem] = []
    for candidate in candidates[:max_items]:
        payload = candidate.source_payload if isinstance(candidate.source_payload, dict) else {}
        post = payload.get("substack_post") if isinstance(payload.get("substack_post"), dict) else {}
        if not post:
            continue
        title = str(post.get("title") or candidate.title_hint or candidate.url)
        summary = str(
            post.get("subtitle")
            or post.get("description")
            or post.get("search_engine_description")
            or candidate.card_text
            or ""
        )
        body_html = str(post.get("body_html") or "")
        partial_body = str(post.get("truncated_body_text") or summary or title)
        content = body_html or partial_body
        compact_payload = dict(payload)
        compact_post = dict(post)
        compact_post.pop("body_html", None)
        compact_payload["substack_post"] = compact_post
        items.append(
            FetchedItem(
                url=candidate.url,
                title=title,
                summary=summary,
                author=_substack_author_names(post),
                published_at=_parse_datetime_hint(
                    str(post.get("post_date") or post.get("published_at") or candidate.published_hint or "")
                ),
                tags=_substack_tags(post),
                content_html=content,
                source_type=source_type,
                extra={
                    "fetch_method": fetch_method,
                    "body_status": "full" if body_html else "partial",
                    "item_key": candidate.item_key,
                    "source_payload": compact_payload,
                    "cover_image": post.get("cover_image") or "",
                    "wordcount": post.get("wordcount"),
                    "comment_count": post.get("comment_count") or post.get("child_comment_count"),
                    "reaction_count": post.get("reaction_count"),
                },
            )
        )

    if not items:
        return None

    logger.info("executor CONTENT substack direct url=%s n_items=%d", sub_url, len(items))
    return _record_success(
        config,
        items,
        sub_url,
        total_available=total_available if total_available is not None else len(candidates),
        fetch_method=fetch_method,
    )


def _is_webpage_source(source_type: str) -> bool:
    return (source_type or "").lower() in _WEBPAGE_SOURCE_TYPES


async def _webpage_candidate_to_item(
    *,
    candidate: ListItem,
    source_type: str,
    fetch_method: str,
) -> FetchedItem | None:
    from contents_hub.tools.fetchers import fetch_url
    from contents_hub.tools.metadata import extract_metadata
    from contents_hub.tools.parse import parse_html

    try:
        fetched = json.loads(
            await fetch_url(candidate.url, mode="raw", max_chars=0, timeout=10.0)
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "executor CONTENT webpage direct fetch exception url=%s error=%s",
            candidate.url,
            exc,
        )
        return None
    if not isinstance(fetched, dict) or not fetched.get("ok"):
        logger.info(
            "executor CONTENT webpage direct fetch failed url=%s error=%s",
            candidate.url,
            fetched.get("error") if isinstance(fetched, dict) else "non-object response",
        )
        return None

    body = str(fetched.get("body") or "")
    if not body:
        return None
    try:
        parsed = json.loads(await parse_html(body, base_url=candidate.url, max_links=120))
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "executor CONTENT webpage direct parse exception url=%s error=%s",
            candidate.url,
            exc,
        )
        return None
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        return None

    try:
        metadata = json.loads(
            await extract_metadata(parsed, source_type="parsed", url=candidate.url)
        )
    except Exception:  # noqa: BLE001
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    visible_text = _decode_text(str(parsed.get("text") or ""))
    title = _decode_text(
        str(
            metadata.get("title")
            or parsed.get("title")
            or candidate.title_hint
            or candidate.url
        )
    )
    summary = _decode_text(
        str(metadata.get("summary") or candidate.card_text or visible_text[:500] or title)
    )
    content = visible_text[:20000] if visible_text else summary or title
    if not content and not title:
        return None

    raw_tags = metadata.get("tags") or []
    tags = [str(tag) for tag in raw_tags if tag] if isinstance(raw_tags, list) else []
    published_at = _parse_datetime_hint(
        str(metadata.get("published_at") or candidate.published_hint or "")
    )
    source_payload = (
        candidate.source_payload if isinstance(candidate.source_payload, dict) else {}
    )
    metadata_extra = metadata.get("extra") if isinstance(metadata.get("extra"), dict) else {}
    return FetchedItem(
        url=candidate.url,
        title=title,
        summary=summary,
        author=_decode_text(str(metadata.get("author") or "")),
        published_at=published_at,
        tags=tags,
        content_html=content,
        source_type=source_type,
        extra={
            "fetch_method": fetch_method,
            "body_status": "partial",
            "item_key": candidate.item_key,
            "source_payload": source_payload,
            "detail_fetch_method": "fetch_url_parse_html",
            "raw_body_chars": fetched.get("raw_body_chars"),
            "content_type": fetched.get("content_type") or "",
            "metadata": metadata_extra,
        },
    )


async def _content_from_webpage_fetch(
    *,
    sub_url: str,
    source_type: str,
    config: dict[str, Any],
    candidates: list[ListItem],
    max_items: int,
    total_available: int | None,
    fetch_method: str,
) -> FetchResult | None:
    if not candidates:
        return None

    semaphore = asyncio.Semaphore(4)

    async def run(candidate: ListItem) -> FetchedItem | None:
        async with semaphore:
            return await _webpage_candidate_to_item(
                candidate=candidate,
                source_type=source_type,
                fetch_method=fetch_method,
            )

    results = await asyncio.gather(
        *(run(candidate) for candidate in candidates[:max_items]),
        return_exceptions=True,
    )
    items = [
        item
        for item in results
        if isinstance(item, FetchedItem)
    ]
    if not items:
        return None

    logger.info("executor CONTENT webpage direct url=%s n_items=%d", sub_url, len(items))
    return _record_success(
        config,
        items,
        sub_url,
        total_available=total_available if total_available is not None else len(candidates),
        fetch_method=fetch_method,
    )


async def _list_rss_feed_items(sub_url: str) -> ListFetchResult | None:
    from contents_hub.tools.parse import parse_rss

    fetched = await _fetch_json_url(sub_url)
    if not fetched.get("ok"):
        logger.info(
            "executor LIST rss direct fetch failed url=%s error=%s; falling back to agent",
            sub_url,
            fetched.get("error") or fetched.get("status"),
        )
        return None

    body = str(fetched.get("body") or "")
    try:
        parsed = json.loads(await parse_rss(xml=body, feed_url=sub_url, max_items=DEFAULT_MAX_ITEMS))
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "executor LIST rss direct parse error url=%s error=%s; falling back to agent",
            sub_url,
            exc,
        )
        return None

    if not isinstance(parsed, dict) or not parsed.get("ok"):
        logger.info(
            "executor LIST rss direct parse failed url=%s error=%s; falling back to agent",
            sub_url,
            parsed.get("error") if isinstance(parsed, dict) else "rss parse failed",
        )
        return None

    items: list[ListItem] = []
    for entry in parsed.get("items") or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        title = str(entry.get("title") or "")
        published = str(entry.get("published_at") or "")
        summary = str(entry.get("summary") or entry.get("content_html") or "")
        item_key = str(entry.get("guid") or entry.get("id") or url)
        items.append(
            ListItem(
                item_key=item_key,
                url=url,
                title_hint=title,
                published_hint=published,
                card_text=summary,
                source_payload={"feed_url": sub_url, "feed_item": entry},
            )
        )

    if not items:
        return ListFetchResult(
            ok=False,
            source_url=sub_url,
            error="rss feed returned 0 items",
            failure_reason=FetchFailureReason.STRUCTURE_CHANGED.value,
        )

    logger.info("executor LIST rss direct url=%s items=%d", sub_url, len(items))
    return ListFetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        total_available=len(items),
    )


def _content_from_rss_entries(
    *,
    sub_url: str,
    source_type: str,
    config: dict[str, Any],
    candidates: list[ListItem],
    max_items: int,
    total_available: int | None,
    fetch_method: str,
) -> FetchResult | None:
    items: list[FetchedItem] = []
    for candidate in candidates[:max_items]:
        payload = candidate.source_payload if isinstance(candidate.source_payload, dict) else {}
        entry = payload.get("feed_item") if isinstance(payload.get("feed_item"), dict) else {}
        if not isinstance(entry, dict) or not entry:
            continue
        title = str(entry.get("title") or candidate.title_hint or candidate.url)
        summary = str(entry.get("summary") or candidate.card_text or "")
        content_html = str(entry.get("content_html") or summary or title)
        tags = entry.get("tags", [])
        items.append(
            FetchedItem(
                url=candidate.url,
                title=title,
                summary=summary,
                author=str(entry.get("author") or ""),
                published_at=_parse_datetime_hint(str(entry.get("published_at") or candidate.published_hint or "")),
                tags=[str(tag) for tag in tags if tag] if isinstance(tags, list) else [],
                content_html=content_html,
                source_type=source_type,
                extra={
                    "fetch_method": fetch_method,
                    "body_status": "feed_entry",
                    "item_key": candidate.item_key,
                    "source_payload": payload,
                    "enclosure_url": entry.get("enclosure_url") or "",
                    "enclosure_type": entry.get("enclosure_type") or "",
                    "enclosure_length": entry.get("enclosure_length"),
                },
            )
        )

    if not items:
        return None

    logger.info("executor CONTENT rss direct url=%s n_items=%d", sub_url, len(items))
    return _record_success(
        config,
        items,
        sub_url,
        total_available=total_available if total_available is not None else len(candidates),
        fetch_method=fetch_method,
    )


def _youtube_channel_ids_from_url_and_html(url: str, html: str) -> list[str]:
    candidates: list[str] = []
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] == "channel" and path_parts[1].startswith("UC"):
        candidates.append(path_parts[1])

    for pattern in _YOUTUBE_CHANNEL_ID_PATTERNS:
        match = pattern.search(html)
        if match:
            candidates.append(match.group(1))

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


async def _youtube_list_from_rss(channel_url: str, html: str) -> list[ListItem]:
    from contents_hub.tools.parse import parse_rss

    for channel_id in _youtube_channel_ids_from_url_and_html(channel_url, html):
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        fetched = await _fetch_json_url(feed_url)
        if not fetched.get("ok"):
            continue
        body = str(fetched.get("body") or "")
        parsed = json.loads(await parse_rss(xml=body, feed_url=feed_url, max_items=DEFAULT_MAX_ITEMS))
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            continue

        items: list[ListItem] = []
        for entry in parsed.get("items") or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            video_id = str(entry.get("video_id") or url)
            items.append(
                ListItem(
                    item_key=f"yt:video:{video_id}",
                    url=url,
                    title_hint=str(entry.get("title") or ""),
                    published_hint=str(entry.get("published_at") or ""),
                    source_payload={"channel_id": channel_id, "feed_url": feed_url},
                )
            )
        if items:
            logger.info(
                "executor LIST youtube RSS url=%s channel_id=%s items=%d",
                channel_url,
                channel_id,
                len(items),
            )
            return items
    return []


def _youtube_videos_url(channel_url: str) -> str:
    parsed = urlparse(channel_url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "www.youtube.com"
    path = parsed.path.rstrip("/") or "/"
    if path.endswith("/videos"):
        videos_path = path
    else:
        videos_path = f"{path}/videos" if path != "/" else "/videos"
    return f"{scheme}://{netloc}{videos_path}"


async def _youtube_list_from_videos_page(channel_url: str) -> list[ListItem]:
    videos_url = _youtube_videos_url(channel_url)
    fetched = await _fetch_json_url(videos_url)
    if not fetched.get("ok"):
        return []
    html = str(fetched.get("body") or "")

    video_ids: list[str] = []
    seen: set[str] = set()
    for match in _YOUTUBE_VIDEO_ID_RE.finditer(html):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        video_ids.append(video_id)
        if len(video_ids) >= DEFAULT_MAX_ITEMS:
            break

    items = [
        ListItem(
            item_key=f"yt:video:{video_id}",
            url=f"https://www.youtube.com/watch?v={video_id}",
            source_payload={"list_source": videos_url},
        )
        for video_id in video_ids
    ]
    if items:
        logger.info(
            "executor LIST youtube videos-page url=%s items=%d",
            videos_url,
            len(items),
        )
    return items


async def _list_youtube_channel_items(sub_url: str) -> ListFetchResult | None:
    channel_fetched = await _fetch_json_url(sub_url)
    if not channel_fetched.get("ok"):
        return None
    channel_html = str(channel_fetched.get("body") or "")

    items = await _youtube_list_from_rss(sub_url, channel_html)
    if not items:
        items = await _youtube_list_from_videos_page(sub_url)
    if not items:
        return None

    return ListFetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        total_available=len(items),
    )


def _decode_text(raw: str) -> str:
    value = html.unescape(raw or "")
    try:
        value = json.loads(f'"{value}"')
    except Exception:  # noqa: BLE001
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def _html_meta_content(document: str, key: str) -> str:
    escaped_key = re.escape(key)
    patterns = (
        rf'<meta[^>]+(?:property|name)=["\']{escaped_key}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{escaped_key}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, document, flags=re.IGNORECASE)
        if match:
            return _decode_text(match.group(1))
    return ""


def _json_field(document: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', document)
    return _decode_text(match.group(1)) if match else ""


def _strip_youtube_suffix(title: str) -> str:
    title = title.strip()
    return title.removesuffix(" - YouTube").strip()


def _youtube_video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("shorts", "embed", "live"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return ""


def _parse_datetime_hint(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        from dateutil.parser import parse as parse_date

        parsed = parse_date(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


async def _youtube_content_from_pages(
    *,
    sub_url: str,
    source_type: str,
    config: dict[str, Any],
    candidates: list[ListItem],
    max_items: int,
    total_available: int | None,
    fetch_method: str,
) -> FetchResult | None:
    if not candidates:
        return None

    items: list[FetchedItem] = []
    for candidate in candidates[:max_items]:
        video_id = _youtube_video_id_from_url(candidate.url)
        fetched = await _fetch_json_url(candidate.url)
        if not fetched.get("ok"):
            logger.warning(
                "executor CONTENT youtube direct fetch failed url=%s error=%s",
                candidate.url,
                fetched.get("error") or fetched.get("status"),
            )
            continue
        document = str(fetched.get("body") or "")
        title = (
            _html_meta_content(document, "og:title")
            or _html_meta_content(document, "title")
            or _json_field(document, "title")
            or candidate.title_hint
            or video_id
        )
        title = _strip_youtube_suffix(title)
        summary = (
            _json_field(document, "shortDescription")
            or _html_meta_content(document, "og:description")
            or _html_meta_content(document, "description")
            or candidate.card_text
        )
        author = (
            _json_field(document, "ownerChannelName")
            or _json_field(document, "author")
            or _html_meta_content(document, "og:video:tag")
        )
        published_at = _parse_datetime_hint(
            candidate.published_hint
            or _json_field(document, "publishDate")
            or _json_field(document, "datePublished")
            or _html_meta_content(document, "datePublished")
        )
        body = summary or title
        items.append(
            FetchedItem(
                url=candidate.url,
                title=title,
                summary=summary,
                author=author,
                published_at=published_at,
                content_html=body,
                source_type=source_type,
                extra={
                    "fetch_method": fetch_method,
                    "body_status": "metadata_only",
                    "item_key": candidate.item_key,
                    "video_id": video_id,
                    "source_payload": candidate.source_payload,
                },
            )
        )

    if not items:
        return None

    logger.info(
        "executor CONTENT youtube direct url=%s n_items=%d",
        sub_url,
        len(items),
    )
    return _record_success(
        config,
        items,
        sub_url,
        total_available=total_available if total_available is not None else len(candidates),
        fetch_method=fetch_method,
    )


def _title_from_card_text(candidate: ListItem) -> str:
    if candidate.title_hint:
        return candidate.title_hint
    for line in candidate.card_text.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return candidate.url


def _content_from_list_snapshots(
    *,
    sub_url: str,
    source_type: str,
    config: dict[str, Any],
    candidates: list[ListItem],
    max_items: int,
    total_available: int | None,
    fetch_method: str,
) -> FetchResult | None:
    items: list[FetchedItem] = []
    for candidate in candidates[:max_items]:
        body = candidate.card_text.strip()
        if not body:
            continue
        payload = candidate.source_payload if isinstance(candidate.source_payload, dict) else {}
        author = str(
            payload.get("status_author")
            or payload.get("author")
            or payload.get("reposted_by")
            or ""
        )
        items.append(
            FetchedItem(
                url=candidate.url,
                title=_title_from_card_text(candidate),
                summary=body[:500],
                author=author,
                published_at=_parse_datetime_hint(candidate.published_hint),
                content_html=body,
                source_type=source_type,
                extra={
                    "fetch_method": fetch_method,
                    "body_status": "list_card_snapshot",
                    "item_key": candidate.item_key,
                    "source_payload": payload,
                },
            )
        )

    if not items:
        return None

    logger.info(
        "executor CONTENT list-snapshot direct url=%s n_items=%d",
        sub_url,
        len(items),
    )
    return _record_success(
        config,
        items,
        sub_url,
        total_available=total_available if total_available is not None else len(candidates),
        fetch_method=fetch_method,
    )


async def _list_linkedin_profile_items(sub_url: str) -> ListFetchResult | None:
    for attempt in range(_PLATFORM_DIRECT_TRANSIENT_RETRIES + 1):
        try:
            items = await list_linkedin_profile_items(sub_url, max_items=DEFAULT_MAX_ITEMS)
            break
        except PlatformListTransientError as exc:
            if attempt < _PLATFORM_DIRECT_TRANSIENT_RETRIES:
                logger.info(
                    "executor LIST linkedin transient direct failure url=%s; retrying: %s",
                    sub_url,
                    exc,
                )
                await asyncio.sleep(_PLATFORM_DIRECT_RETRY_DELAY_SECONDS)
                continue
            return ListFetchResult(
                ok=False,
                source_url=sub_url,
                error=f"linkedin direct list transient chromux error: {exc}",
                failure_reason=FetchFailureReason.TIMEOUT.value,
            )
    if items is None:
        return None
    return ListFetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        total_available=len(items),
    )


async def _list_x_profile_items(sub_url: str) -> ListFetchResult | None:
    for attempt in range(_PLATFORM_DIRECT_TRANSIENT_RETRIES + 1):
        try:
            items = await list_x_profile_items(sub_url, max_items=DEFAULT_MAX_ITEMS)
            break
        except PlatformListTransientError as exc:
            if attempt < _PLATFORM_DIRECT_TRANSIENT_RETRIES:
                logger.info(
                    "executor LIST x transient direct failure url=%s; retrying: %s",
                    sub_url,
                    exc,
                )
                await asyncio.sleep(_PLATFORM_DIRECT_RETRY_DELAY_SECONDS)
                continue
            return ListFetchResult(
                ok=False,
                source_url=sub_url,
                error=f"x direct list transient chromux error: {exc}",
                failure_reason=FetchFailureReason.TIMEOUT.value,
            )
    if items is None:
        return None
    return ListFetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        total_available=len(items),
    )


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------


async def list_items(
    sub: Any,
    *,
    max_pagination: int = 0,
    runner: AgentRunner | None = None,
) -> ListFetchResult:
    """Run only the recipe LIST_STRATEGY and return candidate identities."""
    sub_url = getattr(sub, "url", "") or ""
    source_type = getattr(sub, "source_type", "") or "webpage"
    config = getattr(sub, "config", None)
    if not isinstance(config, dict):
        config = {}
    recipe = RecipeRegistry.get_recipe(_recipe_proxy(sub))
    if not recipe:
        return ListFetchResult(
            ok=False,
            source_url=sub_url,
            error=f"no built-in recipe for source_type={source_type}",
            failure_reason=FetchFailureReason.UNKNOWN.value,
        )

    if _is_github_releases_source(source_type, sub_url):
        direct_result = await _list_github_releases_items(sub_url)
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor LIST github releases direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_rss_feed_source(source_type):
        direct_result = await _list_rss_feed_items(sub_url)
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor LIST rss direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_youtube_channel_source(source_type, sub_url):
        direct_result = await _list_youtube_channel_items(sub_url)
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor LIST youtube direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_substack_source(source_type, sub_url):
        direct_result = await _list_substack_items(sub_url, source_type)
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor LIST substack direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_reddit_subreddit_source(source_type, sub_url):
        direct_result = await _list_reddit_subreddit_items(sub_url)
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor LIST reddit direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if is_linkedin_profile_source(source_type, sub_url) and get_browser_backend(runner) != "aside":
        direct_result = await _list_linkedin_profile_items(sub_url)
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor LIST linkedin direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if is_x_profile_source(source_type, sub_url) and get_browser_backend(runner) != "aside":
        direct_result = await _list_x_profile_items(sub_url)
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor LIST x direct path missed url=%s; falling back to agent",
            sub_url,
        )

    runner = runner if runner is not None else get_default_runner()
    prompt = RecipeRegistry.render_prompt(
        "list_prompt", browser_backend=get_browser_backend(runner),
        url=sub_url,
        source_type=source_type,
        recipe=recipe,
        collection_prompt=_collection_prompt_from_config(config),
        max_pagination=max_pagination,
    )
    logger.info("executor LIST url=%s max_pagination=%d", sub_url, max_pagination)

    try:
        response = await runner.run(prompt)
    except asyncio.TimeoutError:
        return ListFetchResult(
            ok=False,
            source_url=sub_url,
            error="list agent timed out",
            failure_reason=FetchFailureReason.TIMEOUT.value,
        )
    except Exception as e:  # noqa: BLE001
        return ListFetchResult(
            ok=False,
            source_url=sub_url,
            error=f"list agent error: {e}",
            failure_reason=infer_from_error(str(e)).value,
        )

    entries, errors, reason = _parse_list_urls(response)
    if not entries:
        return ListFetchResult(
            ok=False,
            source_url=sub_url,
            error="; ".join(errors) or "list returned 0 urls",
            failure_reason=(reason or FetchFailureReason.STRUCTURE_CHANGED).value,
        )

    items = _build_list_items(entries)
    return ListFetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        total_available=len(items),
    )


async def content_items(
    sub: Any,
    list_entries: list[ListItem],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    total_available: int | None = None,
    runner: AgentRunner | None = None,
) -> FetchResult:
    """Run only CONTENT_STRATEGY + METADATA for already-diffed candidates."""
    sub_url = getattr(sub, "url", "") or ""
    source_type = getattr(sub, "source_type", "") or "webpage"
    config = getattr(sub, "config", None)
    if not isinstance(config, dict):
        config = {}
        try:
            setattr(sub, "config", config)
        except Exception:
            pass

    proxy = _recipe_proxy(sub)
    recipe_meta = RecipeRegistry.get_recipe_metadata(proxy)
    fetch_method = str(recipe_meta.get("fetch_method") or config.get("fetch_method") or "agent")
    recipe = RecipeRegistry.get_recipe(proxy)
    if not recipe:
        return _record_failure(
            sub_url,
            config,
            f"no built-in recipe for source_type={source_type}",
            failure_reason=FetchFailureReason.UNKNOWN,
        )

    candidates = list_entries[:max_items]
    if _is_github_releases_source(source_type, sub_url):
        direct_result = _content_from_rss_entries(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor CONTENT github releases direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_rss_feed_source(source_type):
        direct_result = _content_from_rss_entries(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor CONTENT rss direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if source_type in _YOUTUBE_WATCH_SOURCE_TYPES and _is_youtube_channel_source(source_type, sub_url):
        direct_result = await _youtube_content_from_pages(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor CONTENT youtube direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_substack_source(source_type, sub_url):
        direct_result = _content_from_substack_snapshots(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor CONTENT substack direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_reddit_subreddit_source(source_type, sub_url):
        direct_result = _content_from_reddit_snapshots(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor CONTENT reddit direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if is_linkedin_profile_source(source_type, sub_url) or is_x_profile_source(source_type, sub_url):
        direct_result = _content_from_list_snapshots(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor CONTENT list-snapshot direct path missed url=%s; falling back to agent",
            sub_url,
        )

    if _is_webpage_source(source_type):
        direct_result = await _content_from_webpage_fetch(
            sub_url=sub_url,
            source_type=source_type,
            config=config,
            candidates=candidates,
            max_items=max_items,
            total_available=total_available,
            fetch_method=fetch_method,
        )
        if direct_result is not None:
            return direct_result
        logger.info(
            "executor CONTENT webpage direct path missed url=%s; falling back to agent",
            sub_url,
        )

    runner = runner if runner is not None else get_default_runner()
    urls_json = json.dumps(
        [
            {
                "item_key": item.item_key,
                "url": item.url,
                "title_hint": item.title_hint,
                "published_hint": item.published_hint,
                "card_text": item.card_text,
                "source_payload": item.source_payload,
            }
            for item in candidates
        ],
        ensure_ascii=False,
    )
    prompt = RecipeRegistry.render_prompt(
        "detail_prompt", browser_backend=get_browser_backend(runner),
        url=sub_url,
        source_type=source_type,
        recipe=recipe,
        collection_prompt=_collection_prompt_from_config(config),
        urls_json=urls_json,
        n_urls=len(candidates),
    )
    logger.info("executor CONTENT url=%s n_urls=%d", sub_url, len(candidates))

    try:
        response = await runner.run(prompt)
    except asyncio.TimeoutError:
        return _record_failure(
            sub_url, config,
            "content agent timed out",
            failure_reason=FetchFailureReason.TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return _record_failure(sub_url, config, f"content agent error: {e}")

    items_data, errors, agent_reason = _parse_items_json(response)
    items = _build_items(
        items_data,
        max_items=max_items,
        source_type=source_type,
        fetch_method=fetch_method,
    )

    if not items:
        return _record_failure(
            sub_url,
            config,
            "; ".join(errors) or f"content returned 0 items from {len(candidates)} urls",
            failure_reason=agent_reason or FetchFailureReason.STRUCTURE_CHANGED,
        )

    return _record_success(
        config,
        items,
        sub_url,
        total_available=total_available if total_available is not None else len(list_entries),
        fetch_method=fetch_method,
    )


async def _run_execute(
    *,
    runner: AgentRunner,
    sub_url: str,
    source_type: str,
    config: dict[str, Any],
    recipe: str,
    since: datetime | None,
    max_items: int,
    fetch_method: str,
) -> FetchResult:
    prompt = RecipeRegistry.render_prompt(
        "execute_prompt", browser_backend=get_browser_backend(runner),
        url=sub_url,
        source_type=source_type,
        recipe=recipe,
        collection_prompt=_collection_prompt_from_config(config),
        since=since.isoformat() if since else "",
        max_items=max_items,
    )
    logger.info("executor EXECUTE url=%s max_items=%d", sub_url, max_items)

    try:
        response = await runner.run(prompt)
    except asyncio.TimeoutError:
        return _record_failure(
            sub_url, config,
            "execute agent timed out",
            failure_reason=FetchFailureReason.TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return _record_failure(sub_url, config, f"execute agent error: {e}")

    items_data, errors, agent_reason = _parse_items_json(response)

    if errors and not items_data:
        return _record_failure(
            sub_url, config,
            "; ".join(errors) or "execute agent reported errors",
            failure_reason=agent_reason,
        )

    items = _build_items(
        items_data,
        max_items=max_items,
        source_type=source_type,
        fetch_method=fetch_method,
    )
    if since is not None:
        items = [
            it for it in items
            if it.published_at is None or it.published_at > since
        ]

    return _record_success(
        config,
        items,
        sub_url,
        total_available=len(items_data),
        fetch_method=fetch_method,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute(
    sub: Any,
    *,
    since: datetime | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    runner: AgentRunner | None = None,
) -> FetchResult:
    """Compatibility one-shot async fetch executor.

    Args:
        sub: A `Subscription` (or any object exposing `.url`, `.source_type`,
            `.config`).  ``config`` is mutated only for ordinary success/failure
            bookkeeping; recipes are no longer learned or rewritten here.
        since: Optional ISO datetime threshold; only items strictly after
            this point are returned (None disables the filter).
        max_items: Cap on returned items per call. Default 50 (R-T1.1).
        runner: AgentRunner override (None → ``get_default_runner()``).

    Returns:
        FetchResult.  On failure, ``ok=False`` and ``failure_reason`` is
        populated from the agent response or inferred from the error
        string.

    Note: there is intentionally no ``tools=`` kwarg. The runner reads
    the tool registry directly via ``tools.get_default_registry()`` at
    ``run()`` time, so test/runtime overrides should go through
    ``tools.set_default_registry()`` (or by passing ``tool_registry=`` to
    a custom ``ClaudeSDKRunner`` and injecting that as ``runner=``).
    """
    sub_url = getattr(sub, "url", "") or ""
    source_type = getattr(sub, "source_type", "") or "webpage"
    config = getattr(sub, "config", None)
    if not isinstance(config, dict):
        config = {}
        try:
            setattr(sub, "config", config)
        except Exception:
            pass

    proxy = _recipe_proxy(sub)
    recipe_meta = RecipeRegistry.get_recipe_metadata(proxy)
    fetch_method = str(recipe_meta.get("fetch_method") or config.get("fetch_method") or "agent")
    recipe = RecipeRegistry.get_recipe(proxy)

    if not recipe:
        return _record_failure(
            sub_url,
            config,
            f"no built-in recipe for source_type={source_type}",
            failure_reason=FetchFailureReason.UNKNOWN,
        )

    runner = runner if runner is not None else get_default_runner()
    return await _run_execute(
        runner=runner,
        sub_url=sub_url,
        source_type=source_type,
        config=config,
        recipe=recipe,
        max_items=max_items,
        since=since,
        fetch_method=fetch_method,
    )


async def execute_trial(
    sub: Any,
    *,
    max_pagination: int = 1,
    max_detail: int = 3,
    runner: AgentRunner | None = None,
) -> FetchResult:
    """Trial fetch: list-only pass + detail pass on first ``max_detail`` URLs.

    Designed for the validating preview shown in the web UI before a user
    commits a new subscription. Splits the fetch into two short agent
    calls so neither hits the 600s wall-clock timeout that the unified
    ``execute()`` path can run into for sites with many list items
    (LinkedIn / Reddit / etc.).

    Args:
        sub: Subscription proxy (``.url``, ``.source_type``, ``.config``).
        max_pagination: Max load-more clicks the LIST agent may issue.
            ``1`` is enough to validate that pagination works without
            harvesting the whole feed.
        max_detail: Number of URLs (taken from the head of the list)
            handed to the DETAIL agent. ``3`` validates that
            CONTENT_STRATEGY selectors hold across multiple posts.
        runner: AgentRunner override (None → default).

    Trial does **not** mutate ``consecutive_failures``. A failed preview is not
    a fetch error in the production sense, and trial mode does not learn or
    rewrite subscription recipes.
    """
    runner = runner if runner is not None else get_default_runner()

    sub_url = getattr(sub, "url", "") or ""
    source_type = getattr(sub, "source_type", "") or "webpage"
    config = getattr(sub, "config", None)
    if not isinstance(config, dict):
        config = {}
        try:
            setattr(sub, "config", config)
        except Exception:
            pass

    proxy = _recipe_proxy(sub)
    recipe_meta = RecipeRegistry.get_recipe_metadata(proxy)
    fetch_method = str(recipe_meta.get("fetch_method") or config.get("fetch_method") or "agent")
    recipe = RecipeRegistry.get_recipe(proxy)
    if not recipe:
        return FetchResult(
            ok=False,
            source_url=sub_url,
            error="no built-in recipe available for trial",
            error_type="AGENT_ERROR",
            failure_reason=FetchFailureReason.UNKNOWN.value,
        )

    # --- Pass 1: LIST ---
    list_prompt = RecipeRegistry.render_prompt(
        "list_prompt", browser_backend=get_browser_backend(runner),
        url=sub_url,
        source_type=source_type,
        recipe=recipe,
        collection_prompt=_collection_prompt_from_config(config),
        max_pagination=max_pagination,
    )
    logger.info(
        "executor TRIAL/LIST url=%s max_pagination=%d", sub_url, max_pagination,
    )

    try:
        list_response = await runner.run(list_prompt)
    except asyncio.TimeoutError:
        return FetchResult(
            ok=False,
            source_url=sub_url,
            error="trial list agent timed out",
            error_type="AGENT_ERROR",
            failure_reason=FetchFailureReason.TIMEOUT.value,
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(
            ok=False,
            source_url=sub_url,
            error=f"trial list agent error: {e}",
            error_type="AGENT_ERROR",
            failure_reason=infer_from_error(str(e)).value,
        )

    list_entries, list_errors, list_reason = _parse_list_urls(list_response)
    total_listed = len(list_entries)

    if not list_entries:
        msg = "; ".join(list_errors) or "trial list returned 0 urls"
        reason = list_reason or FetchFailureReason.STRUCTURE_CHANGED
        return FetchResult(
            ok=False,
            source_url=sub_url,
            error=msg,
            error_type="AGENT_ERROR",
            failure_reason=reason.value,
        )

    sample_entries = list_entries[:max_detail]
    sample_urls_json = json.dumps(
        [{"url": e["url"]} for e in sample_entries],
        ensure_ascii=False,
    )

    # --- Pass 2: DETAIL ---
    detail_prompt = RecipeRegistry.render_prompt(
        "detail_prompt", browser_backend=get_browser_backend(runner),
        url=sub_url,
        source_type=source_type,
        recipe=recipe,
        collection_prompt=_collection_prompt_from_config(config),
        urls_json=sample_urls_json,
        n_urls=len(sample_entries),
    )
    logger.info(
        "executor TRIAL/DETAIL url=%s n_urls=%d (of %d listed)",
        sub_url, len(sample_entries), total_listed,
    )

    try:
        detail_response = await runner.run(detail_prompt)
    except asyncio.TimeoutError:
        return FetchResult(
            ok=False,
            source_url=sub_url,
            error="trial detail agent timed out",
            error_type="AGENT_ERROR",
            failure_reason=FetchFailureReason.TIMEOUT.value,
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(
            ok=False,
            source_url=sub_url,
            error=f"trial detail agent error: {e}",
            error_type="AGENT_ERROR",
            failure_reason=infer_from_error(str(e)).value,
        )

    items_data, detail_errors, detail_reason = _parse_items_json(detail_response)
    items = _build_items(
        items_data,
        max_items=max_detail,
        source_type=source_type,
        fetch_method=fetch_method,
    )

    if not items:
        msg = (
            "; ".join(detail_errors)
            or f"trial detail returned 0 items from {len(sample_entries)} urls"
        )
        return FetchResult(
            ok=False,
            source_url=sub_url,
            error=msg,
            error_type="AGENT_ERROR",
            failure_reason=(detail_reason or FetchFailureReason.STRUCTURE_CHANGED).value,
        )

    return FetchResult(
        ok=True,
        items=items,
        source_url=sub_url,
        source_title=sub_url,
        total_available=total_listed,
    )


__all__ = ["execute", "execute_trial"]
