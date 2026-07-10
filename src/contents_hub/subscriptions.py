"""
Subscription management with SQLite-based persistence.

Stores subscription state in the SQLite database at:
    .contents-hub/state.db (subscriptions table)

Supports URL-based source types: rss, youtube, twitter, webpage (and platform-specific variants).

Each subscription tracks:
- url/query, title, source_type, status (active/paused/error)
- schedule (cron/interval), default_lens_ids, config JSON
- last_fetched_at, last_error, consecutive_errors

Schema definitions are Python-native (dataclasses + enums).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse

from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.source_types import (
    canonical_source_type,
    default_recipe_config,
    detect_source_type as detect_catalog_source_type,
    is_supported_source_type,
)
from contents_hub.timeutil import now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums (aligned with TypeScript SourceType, SubscriptionStatus, CollectionSchedule)
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    """Content source type — how content is discovered and fetched.

    All source types are URL-based. Natural-language-only subscriptions
    (prompt without URL) are not supported by design: recipes describe
    how to fetch from a given URL, not what to fetch.
    """

    RSS = "rss.feed"
    YOUTUBE = "youtube.channel"
    GITHUB_RELEASES = "github.releases"
    TWITTER = "x.profile"
    LINKEDIN = "linkedin.profile"
    THREADS = "threads.profile"
    SUBSTACK = "substack.publication"
    SUBSTACK_TAG = "substack.tag"
    MEDIUM = "medium.publication"
    REDDIT = "reddit.subreddit"
    WEBPAGE = "webpage"


class SubscriptionStatus(str, Enum):
    """Feed subscription status."""

    NEEDS_AUTH = "needs_auth"
    VALIDATING = "validating"
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    BROKEN = "broken"


class CollectionSchedule(str, Enum):
    """How often to collect new content (preset aliases)."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MANUAL = "manual"


# Preset schedule → interval minutes mapping
PRESET_INTERVAL_MINUTES: dict[str, int | None] = {
    "hourly": 60,
    "daily": 1440,
    "weekly": 10080,
    "manual": None,
}

# Cron expression validation (5-field standard cron)
_CRON_PATTERN = r"^(\*|[0-9,\-/]+)\s+(\*|[0-9,\-/]+)\s+(\*|[0-9,\-/]+)\s+(\*|[0-9,\-/]+)\s+(\*|[0-9,\-/]+)$"


def validate_cron_expression(expr: str) -> bool:
    """Validate a 5-field cron expression structurally.

    Args:
        expr: Cron expression string (e.g., "*/30 * * * *").

    Returns:
        True if the expression has valid 5-field structure.
        Does NOT validate semantic correctness (e.g., minute > 59).
    """
    import re
    return bool(re.match(_CRON_PATTERN, expr.strip()))


# ---------------------------------------------------------------------------
# ScheduleConfig dataclass — cron/interval/preset schedule
# ---------------------------------------------------------------------------


@dataclass
class ScheduleConfig:
    """Per-subscription fetch schedule configuration.

    Supports three modes (resolution priority: cron > interval_minutes > preset):

    1. **Preset** (default): Simple alias — "hourly", "daily", "weekly", "manual"
    2. **Cron**: Standard 5-field cron expression for precise scheduling
    3. **Interval**: Run every N minutes for simple periodic polling

    Maps to SQLite `schedules` table columns:
        preset → used to compute interval_minutes if neither cron nor interval set
        cron → cron_expr column
        interval_minutes → interval_minutes column

    Examples:
        ScheduleConfig(preset="daily")                          # Daily at default time
        ScheduleConfig(preset="daily", cron="0 9 * * 1-5")     # Weekdays at 9am
        ScheduleConfig(preset="hourly", interval_minutes=120)   # Every 2 hours
    """

    preset: CollectionSchedule = CollectionSchedule.DAILY
    cron: Optional[str] = None
    interval_minutes: Optional[int] = None

    def __post_init__(self) -> None:
        if self.cron is not None and self.interval_minutes is not None:
            raise ValueError(
                "Cannot set both 'cron' and 'interval_minutes' — use one or the other"
            )
        if self.cron is not None and not validate_cron_expression(self.cron):
            raise ValueError(
                f"Invalid cron expression: {self.cron!r}. "
                "Expected 5 space-separated fields (min hour dom month dow)"
            )
        if self.interval_minutes is not None:
            if self.interval_minutes < 5:
                raise ValueError("Minimum interval is 5 minutes")
            if self.interval_minutes > 10080:
                raise ValueError("Maximum interval is 10080 minutes (1 week)")

    @property
    def effective_interval_minutes(self) -> int | None:
        """Resolve to effective interval in minutes.

        Returns None for manual or cron-based schedules
        (cron uses its own next-run computation).
        """
        if self.cron:
            return None  # Cron-based: scheduler computes next_run from cron
        if self.interval_minutes:
            return self.interval_minutes
        return PRESET_INTERVAL_MINUTES.get(self.preset.value)

    @property
    def is_manual(self) -> bool:
        """True if this schedule requires manual trigger (no auto-collection)."""
        return (
            self.preset == CollectionSchedule.MANUAL
            and self.cron is None
            and self.interval_minutes is None
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to YAML-friendly dict. Only includes non-default fields."""
        d: dict[str, Any] = {"preset": self.preset.value}
        if self.cron is not None:
            d["cron"] = self.cron
        if self.interval_minutes is not None:
            d["interval_minutes"] = self.interval_minutes
        return d

    @classmethod
    def from_dict(cls, data: Any) -> ScheduleConfig:
        """Deserialize from a YAML-loaded value.

        Accepts either:
            - A string ("daily") → preset only
            - A dict ({"preset": "daily", "cron": "0 9 * * *"}) → full config
        """
        if isinstance(data, str):
            return cls(preset=_parse_schedule(data))
        if isinstance(data, dict):
            return cls(
                preset=_parse_schedule(data.get("preset", "daily")),
                cron=data.get("cron"),
                interval_minutes=data.get("interval_minutes"),
            )
        return cls()  # fallback to default

    def __str__(self) -> str:
        if self.cron:
            return f"cron({self.cron})"
        if self.interval_minutes:
            return f"every {self.interval_minutes}min"
        return self.preset.value


# ---------------------------------------------------------------------------
# Subscription dataclass
# ---------------------------------------------------------------------------


@dataclass
class Subscription:
    """A single source subscription (RSS, YouTube, Twitter, Webpage, Natural-Language).

    Type-specific fields are stored in the `config` dict.
    Type-specific fields are stored in the `config` dict.
    """

    url: str  # Source URL (required)
    title: str = ""
    source_type: str = ""  # auto-detected if empty: rss, youtube, twitter, webpage
    id: str = ""  # UUID, auto-generated if empty
    added_at: Optional[datetime] = None
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    last_fetched_at: Optional[datetime] = None
    last_fetched_count: int = 0
    error_message: str = ""
    lenses: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.added_at is None:
            self.added_at = datetime.now(timezone.utc)
        if not self.id:
            self.id = str(uuid.uuid4())
        if self.url:
            self.url = _normalize_feed_url(self.url)
        # Auto-detect source_type if not set
        if not self.source_type:
            self.source_type = _detect_source_type(self.url, self.config)
        else:
            self.source_type = canonical_source_type(self.source_type)
        defaults = default_recipe_config(self.source_type)
        for key, value in defaults.items():
            self.config.setdefault(key, value)

    @property
    def key(self) -> str:
        """Unique key for deduplication (the normalized URL)."""
        return self.url

    def to_dict(self) -> dict:
        """Serialize to a YAML-friendly dict."""
        # Serialize schedule: simple string if preset-only, dict if cron/interval
        schedule_val: Any
        if self.schedule.cron is not None or self.schedule.interval_minutes is not None:
            schedule_val = self.schedule.to_dict()
        else:
            schedule_val = self.schedule.preset.value

        d: dict = {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "source_type": self.source_type,
            "added_at": _dt_to_iso(self.added_at),
            "status": self.status.value,
            "schedule": schedule_val,
        }
        if self.last_fetched_at:
            d["last_fetched_at"] = _dt_to_iso(self.last_fetched_at)
        if self.last_fetched_count > 0:
            d["last_fetched_count"] = self.last_fetched_count
        if self.error_message:
            d["error_message"] = self.error_message
        if self.lenses:
            d["lenses"] = self.lenses
        if self.tags:
            d["tags"] = self.tags
        if self.config:
            d["config"] = self.config
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Subscription:
        """Deserialize from a YAML-loaded dict.

        Validates required fields and applies defaults for missing optional fields.
        """
        errors = _validate_subscription_dict(data)
        if errors:
            logger.warning(
                "Subscription validation warnings for %s: %s",
                data.get("url", data.get("id", "unknown")),
                "; ".join(errors),
            )

        return cls(
            url=data.get("url", ""),
            title=data.get("title", ""),
            source_type=data.get("source_type", ""),
            id=data.get("id", ""),
            added_at=_iso_to_dt(data.get("added_at")),
            status=_parse_status(data.get("status", "active")),
            schedule=ScheduleConfig.from_dict(data.get("schedule", "daily")),
            last_fetched_at=_iso_to_dt(data.get("last_fetched_at")),
            last_fetched_count=data.get("last_fetched_count", 0),
            error_message=data.get("error_message", ""),
            lenses=data.get("lenses", []),
            tags=data.get("tags", []),
            config=data.get("config", {}),
        )

    def record_fetch(
        self, item_count: int, error: str = ""
    ) -> None:
        """Update fetch metadata after a collection run.

        last_fetched_at advances ONLY on successful runs (R2.1). On error
        we update only the error fields; the timestamp of the last
        SUCCESSFUL fetch is preserved.
        """
        if error:
            self.status = SubscriptionStatus.ERROR
            self.error_message = error
        else:
            self.last_fetched_at = datetime.now(timezone.utc)
            self.last_fetched_count = item_count
            self.status = SubscriptionStatus.ACTIVE
            self.error_message = ""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_subscription_dict(data: dict) -> list[str]:
    """Validate a subscription dict, returning a list of warnings (empty = valid).

    Non-fatal validation: logs warnings but doesn't prevent loading.
    This ensures corrupted entries don't crash the entire store.
    """
    errors: list[str] = []
    source_type = data.get("source_type", "")

    # URL is required for every subscription.
    if source_type and not data.get("url"):
        errors.append(f"Missing URL for {source_type} subscription")

    # Validate status enum
    status = data.get("status", "active")
    if status not in ("active", "paused", "error", "broken", "needs_auth", "validating"):
        errors.append(f"Invalid status: {status}")

    # Validate schedule (string preset or dict with cron/interval)
    schedule = data.get("schedule", "daily")
    if isinstance(schedule, str):
        if schedule not in ("hourly", "daily", "weekly", "manual"):
            errors.append(f"Invalid schedule preset: {schedule}")
    elif isinstance(schedule, dict):
        preset = schedule.get("preset", "daily")
        if preset not in ("hourly", "daily", "weekly", "manual"):
            errors.append(f"Invalid schedule preset: {preset}")
        cron = schedule.get("cron")
        if cron is not None and not validate_cron_expression(cron):
            errors.append(f"Invalid cron expression: {cron}")
        interval = schedule.get("interval_minutes")
        if interval is not None:
            if not isinstance(interval, (int, float)) or interval < 5 or interval > 10080:
                errors.append(f"Invalid interval_minutes: {interval} (must be 5–10080)")
        if cron is not None and interval is not None:
            errors.append("Cannot set both 'cron' and 'interval_minutes'")
    else:
        errors.append(f"Invalid schedule type: {type(schedule).__name__}")

    # Validate source_type enum
    if source_type and not is_supported_source_type(source_type):
        errors.append(f"Unknown source_type: {source_type}")

    # Validate lenses is a list of strings
    lenses = data.get("lenses", [])
    if not isinstance(lenses, list):
        errors.append("lenses must be a list")

    # Validate tags is a list of strings
    tags = data.get("tags", [])
    if not isinstance(tags, list):
        errors.append("tags must be a list")

    return errors


def validate_subscription(sub: Subscription) -> list[str]:
    """Validate a Subscription object, returning a list of errors.

    Stricter than _validate_subscription_dict — used for new subscriptions.
    """
    errors: list[str] = []

    if not sub.url:
        errors.append("URL is required")
    elif not _is_valid_feed_url(sub.url):
        errors.append(f"Invalid URL: {sub.url}")

    if not is_supported_source_type(sub.source_type):
        errors.append(f"Unknown source_type: {sub.source_type}")

    return errors


# ---------------------------------------------------------------------------
# SubscriptionStore
# ---------------------------------------------------------------------------


class SubscriptionStore:
    """Manages subscriptions persisted in SQLite (subscriptions table).

    Uses db.py's init_db/get_db for database access.
    Replaces the previous YAML-based persistence.
    """

    def __init__(self, config: WikiConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        url: str,
        *,
        title: str = "",
        source_type: str = "",
        lenses: list[str] | None = None,
        tags: list[str] | None = None,
        schedule: str | dict | ScheduleConfig = "daily",
        config: dict[str, Any] | None = None,
    ) -> Subscription:
        """Add a new subscription.

        Args:
            url: Source URL to subscribe to (required).
            title: Human-readable title. Populated on first fetch if empty.
            source_type: Source type (rss, youtube, twitter, linkedin,
                         substack, medium, reddit, webpage).
                         Auto-detected from URL if empty.
            lenses: Interest categories (stored as default_lens_ids JSON array).
            tags: User-provided tags (not stored in DB, kept for API compat).
            schedule: Collection schedule.
            config: Type-specific configuration dict.

        Returns:
            The created Subscription.

        Raises:
            ValueError: If the URL is already subscribed, invalid, or validation fails.
        """

        normalized = _normalize_feed_url(url)
        if not _is_valid_feed_url(normalized):
            raise ValueError(f"Invalid feed URL: {url}")

        # Parse schedule
        if isinstance(schedule, ScheduleConfig):
            sched = schedule
        else:
            sched = ScheduleConfig.from_dict(schedule)

        source_type = canonical_source_type(source_type) if source_type else source_type
        sub = Subscription(
            url=normalized,
            title=title,
            source_type=source_type,
            lenses=lenses or [],
            tags=tags or [],
            schedule=sched,
            config=config or {},
        )

        # Validate
        errors = validate_subscription(sub)
        if errors:
            raise ValueError(f"Validation failed: {'; '.join(errors)}")

        # Check for duplicates and insert
        with get_db(self._config) as conn:
            existing = conn.execute(
                "SELECT id FROM subscriptions WHERE url = ?",
                (sub.url,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Already subscribed: {sub.key}")

            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            sub.added_at = now

            conn.execute(
                """INSERT INTO subscriptions
                    (url, title, source_type, status, schedule_cron,
                     schedule_interval_minutes, default_lens_ids, config,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sub.url,
                    sub.title,
                    sub.source_type,
                    sub.status.value,
                    sched.cron,
                    _schedule_to_interval(sched),
                    json.dumps(sub.lenses),
                    json.dumps(sub.config),
                    now_iso,
                    now_iso,
                ),
            )
            # Companion `schedules` row — drives the web Logs tab (schedule_runs
            # is FK'd to schedules.id). Daemon reads the subscriptions.* copy.
            conn.execute(
                "INSERT OR IGNORE INTO schedules "
                "(subscription_url, source_type, cron_expr, interval_minutes, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sub.url,
                    sub.source_type,
                    sched.cron,
                    _schedule_to_interval(sched),
                    now_iso,
                    now_iso,
                ),
            )
            # Get the auto-generated id
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE url = ?",
                (sub.url,),
            ).fetchone()
            if row:
                sub.id = str(row["id"])

        logger.info("Added subscription: %s (%s)", sub.title or sub.url, sub.key)
        return sub

    def remove(self, url: str) -> Subscription:
        """Remove a subscription by URL.

        Returns the removed Subscription.
        Raises KeyError if not found.
        """

        normalized = _normalize_feed_url(url)
        sub = self.get(normalized)
        if sub is None:
            raise KeyError(f"Not subscribed: {normalized}")

        with get_db(self._config) as conn:
            conn.execute("DELETE FROM subscriptions WHERE url = ?", (normalized,))

        logger.info("Removed subscription: %s", normalized)
        return sub

    def remove_by_id(self, sub_id: str) -> Subscription:
        """Remove a subscription by its ID.

        Raises KeyError if not found.
        """

        sub = self.get_by_id(sub_id)
        if sub is None:
            raise KeyError(f"No subscription with id: {sub_id}")

        with get_db(self._config) as conn:
            conn.execute("DELETE FROM subscriptions WHERE id = ?", (int(sub_id),))

        logger.info("Removed subscription by id: %s", sub_id)
        return sub

    def get(self, url: str) -> Subscription | None:
        """Get a subscription by URL, or None if not found."""

        normalized = _normalize_feed_url(url)
        with get_db(self._config) as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE url = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_subscription(row)

    def get_by_id(self, sub_id: str) -> Subscription | None:
        """Get a subscription by ID, or None if not found."""

        try:
            id_int = int(sub_id)
        except (ValueError, TypeError):
            return None

        with get_db(self._config) as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (id_int,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_subscription(row)

    def list_all(self) -> list[Subscription]:
        """List all subscriptions, sorted by created_at (newest first)."""

        with get_db(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions ORDER BY created_at DESC",
            ).fetchall()
            return [_row_to_subscription(r) for r in rows]

    def list_by_status(self, status: SubscriptionStatus) -> list[Subscription]:
        """List subscriptions filtered by status."""

        with get_db(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
            return [_row_to_subscription(r) for r in rows]

    def list_by_source_type(self, source_type: str) -> list[Subscription]:
        """List subscriptions filtered by source type."""

        canonical = canonical_source_type(source_type)
        with get_db(self._config) as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE source_type = ? ORDER BY created_at DESC",
                (canonical,),
            ).fetchall()
            return [_row_to_subscription(r) for r in rows]

    def list_by_lens(self, lens: str) -> list[Subscription]:
        """List subscriptions that include the given lens."""
        # SQLite JSON matching — filter in Python for case-insensitive match
        all_subs = self.list_all()
        lens_lower = lens.lower()
        return [
            s for s in all_subs
            if lens_lower in [name.lower() for name in s.lenses]
        ]

    def list_by_schedule(self, schedule: str) -> list[Subscription]:
        """List subscriptions filtered by schedule preset."""
        return [s for s in self.list_all() if s.schedule.preset.value == schedule]

    def update_title(self, url: str, title: str) -> None:
        """Update the title of an existing subscription."""

        normalized = _normalize_feed_url(url)
        self._require(normalized)
        with get_db(self._config) as conn:
            conn.execute(
                "UPDATE subscriptions SET title = ?, updated_at = ? WHERE url = ?",
                (title, now_iso(), normalized),
            )

    def update_lenses(self, url: str, lenses: list[str]) -> None:
        """Replace the lenses (default_lens_ids) of an existing subscription."""

        normalized = _normalize_feed_url(url)
        self._require(normalized)
        with get_db(self._config) as conn:
            conn.execute(
                "UPDATE subscriptions SET default_lens_ids = ?, updated_at = ? WHERE url = ?",
                (json.dumps(lenses), now_iso(), normalized),
            )

    def update_tags(self, url: str, tags: list[str]) -> None:
        """Update tags — stored in config JSON under 'tags' key."""

        normalized = _normalize_feed_url(url)
        sub = self._require(normalized)
        sub.config["tags"] = tags
        with get_db(self._config) as conn:
            conn.execute(
                "UPDATE subscriptions SET config = ?, updated_at = ? WHERE url = ?",
                (json.dumps(sub.config), now_iso(), normalized),
            )

    def update_schedule(
        self,
        url: str,
        schedule: str | dict | ScheduleConfig,
    ) -> None:
        """Update the collection schedule of a subscription."""

        normalized = _normalize_feed_url(url)
        self._require(normalized)
        if isinstance(schedule, ScheduleConfig):
            sched = schedule
        elif isinstance(schedule, dict):
            sched = ScheduleConfig.from_dict(schedule)
        else:
            sched = ScheduleConfig.from_dict(schedule)

        with get_db(self._config) as conn:
            conn.execute(
                """UPDATE subscriptions
                   SET schedule_cron = ?, schedule_interval_minutes = ?, updated_at = ?
                   WHERE url = ?""",
                (
                    sched.cron,
                    _schedule_to_interval(sched),
                    now_iso(),
                    normalized,
                ),
            )

    def update_config(self, url: str, config: dict[str, Any]) -> None:
        """Update the type-specific config of a subscription."""

        normalized = _normalize_feed_url(url)
        sub = self._require(normalized)
        sub.config.update(config)
        with get_db(self._config) as conn:
            conn.execute(
                "UPDATE subscriptions SET config = ?, updated_at = ? WHERE url = ?",
                (json.dumps(sub.config), now_iso(), normalized),
            )

    def set_status(self, url: str, status: SubscriptionStatus) -> None:
        """Set the status of a subscription (active/paused/error)."""

        normalized = _normalize_feed_url(url)
        self._require(normalized)
        with get_db(self._config) as conn:
            conn.execute(
                "UPDATE subscriptions SET status = ?, updated_at = ? WHERE url = ?",
                (status.value, now_iso(), normalized),
            )

    def record_fetch(self, url: str, item_count: int, error: str = "") -> None:
        """Record the result of a fetch operation for a subscription."""

        normalized = _normalize_feed_url(url)
        sub = self._require(normalized)
        now_str = now_iso()

        # Store last_fetched_count in config JSON for backward compat
        config_data = dict(sub.config)
        if sub.tags:
            config_data["tags"] = sub.tags
        if not error:
            config_data["last_fetched_count"] = item_count

        MAX_CONSECUTIVE_ERRORS = 5  # was imported from removed scheduler_engine

        with get_db(self._config) as conn:
            if error:
                # Read current consecutive_errors to decide ERROR vs BROKEN
                row = conn.execute(
                    "SELECT consecutive_errors FROM subscriptions WHERE url = ?",
                    (normalized,),
                ).fetchone()
                current_errors = (row["consecutive_errors"] if row else 0) or 0
                new_errors = current_errors + 1
                new_status = (
                    "broken" if new_errors >= MAX_CONSECUTIVE_ERRORS else "error"
                )
                # R2.1: do NOT update last_fetched_at on failed runs.
                # Preserve the timestamp of the last SUCCESSFUL fetch.
                conn.execute(
                    """UPDATE subscriptions
                       SET status = ?, last_error = ?,
                           consecutive_errors = ?,
                           updated_at = ?,
                           config = ?
                       WHERE url = ?""",
                    (new_status, error, new_errors, now_str,
                     json.dumps(config_data), normalized),
                )
            else:
                conn.execute(
                    """UPDATE subscriptions
                       SET status = 'active', last_error = '',
                           consecutive_errors = 0,
                           last_fetched_at = ?, updated_at = ?,
                           config = ?
                       WHERE url = ?""",
                    (now_str, now_str, json.dumps(config_data), normalized),
                )

    @property
    def count(self) -> int:
        """Number of subscriptions."""

        with get_db(self._config) as conn:
            row = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()
            return row[0]

    def needs_fetch(self) -> list[Subscription]:
        """List active subscriptions (candidates for the next fetch cycle)."""
        return self.list_by_status(SubscriptionStatus.ACTIVE)

    def reload(self) -> None:
        """No-op for SQLite store (each query reads fresh)."""
        pass

    def _save(self) -> None:
        """No-op for SQLite store. Backward compat stub.

        In the YAML store, this wrote all in-memory state to disk.
        The SQLite store persists on each operation, so this is a no-op.
        Tests that need to set specific field values should use the
        appropriate update methods instead.
        """
        pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, url: str) -> Subscription:
        """Get a subscription or raise KeyError."""
        normalized = _normalize_feed_url(url)
        sub = self.get(normalized)
        if sub is None:
            raise KeyError(f"Not subscribed: {normalized}")
        return sub


def _schedule_to_interval(sched: ScheduleConfig) -> int:
    """Convert a ScheduleConfig to an interval_minutes value for SQLite storage.

    Uses 0 to represent manual (no auto-collection).
    """
    if sched.is_manual:
        return 0
    eff = sched.effective_interval_minutes
    if eff is None:
        return 30  # default fallback
    return eff


def _interval_to_schedule(cron: str | None, interval: int | None) -> ScheduleConfig:
    """Reconstruct ScheduleConfig from SQLite columns."""
    if cron:
        return ScheduleConfig(cron=cron)
    if interval is not None and interval == 0:
        return ScheduleConfig(preset=CollectionSchedule.MANUAL)
    if interval is not None and interval != 30:
        return ScheduleConfig(interval_minutes=interval)
    return ScheduleConfig()  # default daily


def _row_to_subscription(row) -> Subscription:
    """Convert a SQLite Row from subscriptions table to a Subscription dataclass."""
    lens_ids = json.loads(row["default_lens_ids"]) if row["default_lens_ids"] else []
    config_data = json.loads(row["config"]) if row["config"] else {}
    tags = config_data.pop("tags", []) if isinstance(config_data, dict) else []
    last_fetched_count = config_data.pop("last_fetched_count", 0) if isinstance(config_data, dict) else 0

    # Build schedule from DB columns
    sched = _interval_to_schedule(row["schedule_cron"], row["schedule_interval_minutes"])

    sub = Subscription.__new__(Subscription)
    sub.url = row["url"]
    sub.title = row["title"] or ""
    sub.source_type = canonical_source_type(row["source_type"] or "rss.feed")
    sub.id = str(row["id"])
    sub.added_at = _iso_to_dt(row["created_at"])
    sub.status = _parse_status(row["status"])
    sub.schedule = sched
    sub.last_fetched_at = _iso_to_dt(row["last_fetched_at"])
    sub.last_fetched_count = last_fetched_count
    sub.error_message = row["last_error"] or ""
    sub.lenses = lens_ids
    sub.tags = tags
    sub.config = config_data if isinstance(config_data, dict) else {}
    for key, value in default_recipe_config(sub.source_type).items():
        sub.config.setdefault(key, value)

    return sub


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _normalize_feed_url(url: str) -> str:
    """Normalize a feed URL for consistent keying."""
    url = url.strip()
    if not url:
        return url
    # Remove trailing slash (except bare domain)
    parsed = urlparse(url)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return parsed._replace(path=path, fragment="").geturl()


def _is_valid_feed_url(url: str) -> bool:
    """Check if string is a valid HTTP(S) URL."""
    return url.startswith("http://") or url.startswith("https://")


def _detect_source_type(url: str, config: dict | None = None) -> str:
    if not url:
        return SourceType.WEBPAGE.value
    return detect_catalog_source_type(url)


def _parse_status(value: str) -> SubscriptionStatus:
    """Parse status string to enum, defaulting to ACTIVE."""
    try:
        return SubscriptionStatus(value)
    except ValueError:
        return SubscriptionStatus.ACTIVE


def _parse_schedule(value: str) -> CollectionSchedule:
    """Parse schedule string to enum, defaulting to DAILY."""
    try:
        return CollectionSchedule(value)
    except ValueError:
        return CollectionSchedule.DAILY


def _dt_to_iso(dt: datetime | None) -> str | None:
    """Convert datetime to ISO 8601 string."""
    if dt is None:
        return None
    return dt.isoformat()


def _iso_to_dt(s: str | None) -> datetime | None:
    """Parse ISO 8601 string to datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
