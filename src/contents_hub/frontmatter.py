"""
Obsidian-native YAML frontmatter metadata utility.

Centralises frontmatter serialisation/deserialisation for the entire
contents-hub system. Every markdown file the system produces (source files,
wiki pages, lens index pages) goes through this module.

Standard metadata fields
~~~~~~~~~~~~~~~~~~~~~~~~

Source files (``sources/``)::

    source_type   str        rss | youtube | twitter | browser
    url           str        original URL
    title         str        item title
    author        str        author / channel / handle
    published     str        ISO-8601 publication timestamp
    collected_at  str        ISO-8601 collection timestamp
    feed          str        parent feed / channel / account title
    tags          list[str]  Obsidian #tags (without ``#``)
    lenses        list[str]  Lens IDs this item belongs to
    status        str        pending | compiled | skipped
    raw_item_id   int        SQLite raw_items.id for reprocessing

Wiki pages::

    type          str        wiki | lens
    title         str        note title
    aliases       list[str]  Obsidian alias search terms
    tags          list[str]  Obsidian #tags
    lenses        list[str]  Lens IDs
    sources       list[str]  relative paths to contributing source files
    source_urls   list[str]  original URLs of sources
    compiled_at   str        ISO-8601 last-compiled timestamp
    created_at    str        ISO-8601 first-created timestamp

Lens pages::

    type              str        "lens"
    id                str        lens slug ID
    name              str        human-readable name
    compile_strategy  str        merge | replace | append
    priority          int        sort order (lower = higher priority)
    enabled           bool       whether collection is active
    keywords          list[str]  matching keywords
    tags              list[str]  default tags applied to compiled notes
    wiki_directory    str        output directory in vault
    source_ids        list[str]  subscription UUIDs bound to this lens
    created_at        str        ISO-8601
    updated_at        str        ISO-8601

All serialisation is hand-rolled (no PyYAML dependency for writes) to
guarantee deterministic, Obsidian-friendly output.  Parsing uses PyYAML
when available and falls back to a simple line-based parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Standard field names (canonical constants)
# ---------------------------------------------------------------------------

# Source-file fields
F_SOURCE_TYPE = "source_type"
F_URL = "url"
F_TITLE = "title"
F_AUTHOR = "author"
F_PUBLISHED = "published"
F_COLLECTED_AT = "collected_at"
F_FEED = "feed"
F_TAGS = "tags"
F_LENSES = "lenses"
F_STATUS = "status"

# Wiki-page fields
F_TYPE = "type"
F_ALIASES = "aliases"
F_SOURCES = "sources"
F_SOURCE_URLS = "source_urls"
F_COMPILED_AT = "compiled_at"
F_CREATED_AT = "created_at"

# Status values
STATUS_PENDING = "pending"
STATUS_COMPILED = "compiled"
STATUS_SKIPPED = "skipped"

# Type values
TYPE_WIKI = "wiki"
TYPE_LENS = "lens"


# ---------------------------------------------------------------------------
# Dataclass: Frontmatter
# ---------------------------------------------------------------------------


@dataclass
class Frontmatter:
    """Structured representation of Obsidian YAML frontmatter.

    Build a ``Frontmatter`` instance, populate the fields you need, then
    call :meth:`to_dict` for an ordered dict or :meth:`serialize` for
    a ``---`` fenced YAML string.

    Works for both source files and wiki pages -- unused fields are simply
    omitted from the output.
    """

    # -- identity --
    type: str = ""               # "wiki", "lens", or "" for source files
    source_type: str = ""        # "rss", "youtube", "twitter", "browser"
    title: str = ""
    url: str = ""

    # -- authorship / provenance --
    author: str = ""
    feed: str = ""               # parent feed / channel title
    published: Optional[str] = None       # ISO-8601
    collected_at: Optional[str] = None    # ISO-8601

    # -- classification --
    tags: list[str] = field(default_factory=list)
    lenses: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    status: str = ""             # pending / compiled / skipped

    # -- wiki-page provenance --
    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    compiled_at: Optional[str] = None     # ISO-8601
    created_at: Optional[str] = None      # ISO-8601

    # -- extensible bag for source-type-specific or user-defined keys --
    extra: dict[str, Any] = field(default_factory=dict)

    # -----------------------------------------------------------------
    # Convenience constructors
    # -----------------------------------------------------------------

    @classmethod
    def for_source(
        cls,
        *,
        source_type: str,
        url: str,
        title: str,
        author: str = "",
        published: datetime | str | None = None,
        collected_at: datetime | str | None = None,
        feed: str = "",
        tags: list[str] | None = None,
        lenses: list[str] | None = None,
        status: str = STATUS_PENDING,
        extra: dict[str, Any] | None = None,
    ) -> Frontmatter:
        """Create frontmatter for a source file."""
        return cls(
            source_type=source_type,
            url=url,
            title=title,
            author=author,
            published=_to_iso(published),
            collected_at=_to_iso(collected_at or datetime.now(timezone.utc)),
            feed=feed,
            tags=list(tags) if tags else [],
            lenses=list(lenses) if lenses else [],
            status=status,
            extra=dict(extra) if extra else {},
        )

    @classmethod
    def for_wiki(
        cls,
        *,
        title: str,
        tags: list[str] | None = None,
        lenses: list[str] | None = None,
        aliases: list[str] | None = None,
        sources: list[str] | None = None,
        source_urls: list[str] | None = None,
        compiled_at: datetime | str | None = None,
        created_at: datetime | str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Frontmatter:
        """Create frontmatter for a compiled wiki page."""
        now = datetime.now(timezone.utc)
        return cls(
            type=TYPE_WIKI,
            title=title,
            tags=list(tags) if tags else [],
            lenses=list(lenses) if lenses else [],
            aliases=list(aliases) if aliases else [],
            sources=list(sources) if sources else [],
            source_urls=list(source_urls) if source_urls else [],
            compiled_at=_to_iso(compiled_at or now),
            created_at=_to_iso(created_at or now),
            extra=dict(extra) if extra else {},
        )

    @classmethod
    def for_lens(
        cls,
        *,
        lens_id: str,
        name: str,
        compile_strategy: str = "merge",
        priority: int = 0,
        enabled: bool = True,
        keywords: list[str] | None = None,
        tags: list[str] | None = None,
        wiki_directory: str = "",
        source_ids: list[str] | None = None,
        description: str = "",
        compile_instructions: str = "",
        created_at: datetime | str | None = None,
        updated_at: datetime | str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Frontmatter:
        """Create frontmatter for a Lens index page.

        Lens pages use ``type: lens`` and include lens-specific fields
        (compile_strategy, keywords, source_ids, etc.) in the ``extra`` bag.
        The ``name`` field is stored in ``extra`` (not ``title``) to match
        the Lens frontmatter convention where ``name`` is the display name.

        Args:
            lens_id: Lens slug ID (e.g., "ai-research").
            name: Human-readable lens name.
            compile_strategy: merge | replace | append.
            priority: Sort order (lower = higher priority).
            enabled: Whether collection is active.
            keywords: Matching keywords for this lens.
            tags: Default tags applied to compiled notes.
            wiki_directory: Output directory in vault.
            source_ids: Subscription UUIDs bound to this lens.
            description: Lens description text.
            compile_instructions: Custom compile instructions.
            created_at: Creation timestamp (None = omit from output).
            updated_at: Last update timestamp (None = omit from output).
            extra: Additional metadata.

        Returns:
            Frontmatter instance with type="lens" and lens-specific extras.
        """
        lens_extra: dict[str, Any] = {
            "id": lens_id,
            "name": name,
            "compile_strategy": compile_strategy,
            "priority": priority,
            "enabled": enabled,
        }
        if keywords:
            lens_extra["keywords"] = list(keywords)
        if wiki_directory:
            lens_extra["wiki_directory"] = wiki_directory
        if source_ids:
            lens_extra["source_ids"] = list(source_ids)
        if description:
            lens_extra["description"] = description
        if compile_instructions:
            lens_extra["compile_instructions"] = compile_instructions
        if created_at:
            lens_extra["created_at"] = _to_iso(created_at)
        if updated_at:
            lens_extra["updated_at"] = _to_iso(updated_at)

        if extra:
            lens_extra.update(extra)

        return cls(
            type=TYPE_LENS,
            tags=list(tags) if tags else [],
            extra=lens_extra,
        )

    # -----------------------------------------------------------------
    # Serialisation
    # -----------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return an ordered dict of non-empty fields.

        Key order follows Obsidian conventions:
            type/source_type -> title -> url -> author -> published ->
            collected_at -> feed -> tags -> lenses -> aliases ->
            status -> sources -> source_urls -> compiled_at -> created_at ->
            extra keys
        """
        d: dict[str, Any] = {}

        # type or source_type (mutually exclusive in practice)
        if self.type:
            d[F_TYPE] = self.type
        if self.source_type:
            d[F_SOURCE_TYPE] = self.source_type

        if self.title:
            d[F_TITLE] = self.title
        if self.url:
            d[F_URL] = self.url
        if self.author:
            d[F_AUTHOR] = self.author
        if self.published:
            d[F_PUBLISHED] = self.published
        if self.collected_at:
            d[F_COLLECTED_AT] = self.collected_at
        if self.feed:
            d[F_FEED] = self.feed

        if self.tags:
            d[F_TAGS] = list(self.tags)
        if self.lenses:
            d[F_LENSES] = list(self.lenses)
        if self.aliases:
            d[F_ALIASES] = list(self.aliases)

        if self.status:
            d[F_STATUS] = self.status

        if self.sources:
            d[F_SOURCES] = list(self.sources)
        if self.source_urls:
            d[F_SOURCE_URLS] = list(self.source_urls)
        if self.compiled_at:
            d[F_COMPILED_AT] = self.compiled_at
        if self.created_at:
            d[F_CREATED_AT] = self.created_at

        # Extra keys (user / source-type specific)
        for k, v in self.extra.items():
            if k not in d:
                d[k] = v

        return d

    def serialize(self) -> str:
        """Serialize to a ``---`` fenced YAML frontmatter block.

        Returns a string like::

            ---
            source_type: rss
            title: My Post
            tags:
              - python
            ---

        The trailing newline is **not** included so callers can
        concatenate ``fm.serialize() + "\\n" + body`` freely.
        """
        return serialize_frontmatter(self.to_dict())

    # -----------------------------------------------------------------
    # Mutation helpers
    # -----------------------------------------------------------------

    def set_compiled(self, at: datetime | None = None) -> None:
        """Mark as compiled with a timestamp."""
        self.status = STATUS_COMPILED
        self.compiled_at = _to_iso(at or datetime.now(timezone.utc))

    def add_tags(self, *new_tags: str) -> None:
        """Add tags (deduplicated, order-preserving)."""
        seen = set(self.tags)
        for t in new_tags:
            if t and t not in seen:
                self.tags.append(t)
                seen.add(t)

    def add_lenses(self, *new_lenses: str) -> None:
        """Add lenses (deduplicated, order-preserving)."""
        seen = set(self.lenses)
        for lens in new_lenses:
            if lens and lens not in seen:
                self.lenses.append(lens)
                seen.add(lens)

    def merge(self, other: dict[str, Any]) -> None:
        """Merge a dict of values into this frontmatter.

        List fields are extended (deduplicated). Scalar fields are
        overwritten. ``extra`` keys are merged.
        """
        _LIST_FIELDS = {F_TAGS, F_LENSES, F_ALIASES, F_SOURCES, F_SOURCE_URLS}
        for k, v in other.items():
            if k in _LIST_FIELDS and isinstance(v, list):
                existing = getattr(self, k, [])
                seen = set(existing)
                for item in v:
                    if item not in seen:
                        existing.append(item)
                        seen.add(item)
            elif hasattr(self, k) and k != "extra":
                setattr(self, k, v)
            else:
                self.extra[k] = v


# ---------------------------------------------------------------------------
# Standalone serialisation functions
# ---------------------------------------------------------------------------


def serialize_frontmatter(data: dict[str, Any]) -> str:
    """Serialize a dict to a ``---`` fenced YAML frontmatter block.

    Hand-rolls YAML to avoid PyYAML write-dependency and guarantee
    deterministic, Obsidian-friendly formatting.

    Args:
        data: Ordered dict of frontmatter key-value pairs.

    Returns:
        Fenced YAML string (``---\\n...\\n---``).
    """
    lines: list[str] = ["---"]
    for key, value in data.items():
        lines.append(_yaml_kv(key, value))
    lines.append("---")
    return "\n".join(lines)


def assemble_markdown(frontmatter: dict[str, Any], body: str) -> str:
    """Combine frontmatter dict + body into a complete markdown string.

    Ensures:
      - Proper ``---`` fencing around frontmatter
      - Blank line between frontmatter and body
      - Trailing newline

    Args:
        frontmatter: Dict of metadata fields.
        body: Markdown body content.

    Returns:
        Complete markdown string ready to write to disk.
    """
    fm_block = serialize_frontmatter(frontmatter)
    result = fm_block + "\n\n" + body
    if not result.endswith("\n"):
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content.

    Tries PyYAML first; falls back to a simple line-based parser.

    Args:
        content: Full markdown file content.

    Returns:
        Tuple of ``(frontmatter_dict, body_string)``.
        If no frontmatter block is found, returns ``({}, content)``.
    """
    if not content.startswith("---"):
        return {}, content

    lines = content.split("\n")
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, content

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])
    if body.startswith("\n"):
        body = body[1:]

    # Try PyYAML; fall back to the simple parser when it is unavailable,
    # the block is invalid YAML, or it parses to a non-mapping scalar.
    try:
        import yaml
    except ImportError:
        return _parse_simple(fm_text), body
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = _parse_simple(fm_text)
    if not isinstance(fm, dict):
        fm = _parse_simple(fm_text)

    return fm, body


def parse_to_frontmatter(content: str) -> tuple[Frontmatter, str]:
    """Parse markdown content into a ``Frontmatter`` dataclass + body.

    Convenience wrapper around :func:`parse_frontmatter` that hydrates
    the result into a ``Frontmatter`` instance.

    Args:
        content: Full markdown file content.

    Returns:
        Tuple of ``(Frontmatter, body_string)``.
    """
    raw, body = parse_frontmatter(content)
    return Frontmatter.from_dict(raw), body


# ---------------------------------------------------------------------------
# Raw frontmatter extraction
# ---------------------------------------------------------------------------


def extract_raw_frontmatter(content: str) -> str:
    """Extract the raw frontmatter block from markdown content.

    Returns the original text (including ``---`` delimiters) exactly as written.
    Useful as ``old_string`` for Edit-tool instructions where byte-exact match
    is required.

    Args:
        content: Full markdown file content.

    Returns:
        Raw frontmatter string, or empty string if no frontmatter block found.
    """
    if not content.startswith("---"):
        return ""
    lines = content.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[: i + 1])
    return ""


# ---------------------------------------------------------------------------
# File-level frontmatter update
# ---------------------------------------------------------------------------


def update_file_frontmatter(
    path: Path,
    updates: dict[str, Any],
    *,
    list_merge: bool = True,
) -> dict[str, Any]:
    """Read a markdown file, merge updates into its frontmatter, and write back.

    This is the recommended way to update frontmatter in-place.  It preserves
    the body content exactly and only rewrites the frontmatter block.

    List fields (tags, lenses, aliases, sources, source_urls) are merged via
    set-union by default (``list_merge=True``).  Set ``list_merge=False`` to
    replace list values entirely.

    Args:
        path: Absolute path to the markdown file.
        updates: Dict of frontmatter key-value pairs to merge.
        list_merge: If True, list fields are unioned; if False, replaced.

    Returns:
        The merged frontmatter dict.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    content = path.read_text(encoding="utf-8")
    fm_dict, body = parse_frontmatter(content)

    _LIST_FIELDS = {"tags", "lenses", "aliases", "sources", "source_urls",
                    "keywords", "source_ids"}

    for key, value in updates.items():
        if list_merge and key in _LIST_FIELDS and isinstance(value, list):
            existing = fm_dict.get(key, [])
            if not isinstance(existing, list):
                existing = [existing] if existing else []
            seen = set(str(x) for x in existing)
            merged = list(existing)
            for item in value:
                if str(item) not in seen:
                    merged.append(item)
                    seen.add(str(item))
            fm_dict[key] = merged
        else:
            fm_dict[key] = value

    # Reassemble and write
    new_content = assemble_markdown(fm_dict, body)
    path.write_text(new_content, encoding="utf-8")
    return fm_dict


# ---------------------------------------------------------------------------
# Frontmatter.from_dict (class method added outside class for readability)
# ---------------------------------------------------------------------------


@classmethod  # type: ignore[misc]
def _from_dict(cls, data: dict[str, Any]) -> Frontmatter:
    """Hydrate a Frontmatter from a parsed dict."""
    known_fields = {
        "type", "source_type", "title", "url", "author", "feed",
        "published", "collected_at", "tags", "lenses", "aliases",
        "status", "sources", "source_urls", "compiled_at", "created_at",
    }

    kwargs: dict[str, Any] = {}
    extra: dict[str, Any] = {}

    for k, v in data.items():
        if k in known_fields:
            # Ensure list fields are lists
            if k in ("tags", "lenses", "aliases", "sources", "source_urls"):
                kwargs[k] = list(v) if isinstance(v, list) else ([v] if v else [])
            elif v is not None:
                kwargs[k] = str(v) if not isinstance(v, str) else v
        else:
            extra[k] = v

    kwargs["extra"] = extra
    return cls(**kwargs)


Frontmatter.from_dict = _from_dict  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# YAML serialisation helpers
# ---------------------------------------------------------------------------


def _yaml_kv(key: str, value: Any) -> str:
    """Format a single YAML key-value pair."""
    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        parts = [f"{key}:"]
        for item in value:
            parts.append(f"  - {_yaml_scalar(item)}")
        return "\n".join(parts)
    return f"{key}: {_yaml_scalar(value)}"


def _yaml_scalar(value: Any) -> str:
    """Format a YAML scalar value with appropriate quoting.

    Handles:
      - None -> ``null``
      - bool -> ``true`` / ``false``
      - int/float -> unquoted numeric string
      - str -> quoted if it contains YAML-special characters
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)

    s = str(value)
    if _needs_quoting(s):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def _needs_quoting(s: str) -> bool:
    """Determine whether a YAML string needs quoting."""
    if not s:
        return True
    # Starts with YAML-special characters
    if s[0] in ("'", '"', "{", "}", "[", "]", "*", "&", "!", "|", ">", "%", "@", "`"):
        return True
    # Contains colon-space or hash-space (inline comment)
    if ": " in s or " #" in s:
        return True
    # Contains newlines
    if "\n" in s or "\r" in s:
        return True
    # Looks like a YAML boolean / null
    if s.lower() in ("true", "false", "yes", "no", "null", "~"):
        return True
    return False


# ---------------------------------------------------------------------------
# Simple frontmatter parser (PyYAML fallback)
# ---------------------------------------------------------------------------


def _parse_simple(text: str) -> dict[str, Any]:
    """Line-based YAML subset parser for flat key-value + simple lists."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # List item
        if stripped.startswith("- ") and current_key is not None:
            if current_list is None:
                current_list = []
                result[current_key] = current_list
            current_list.append(_parse_scalar_value(stripped[2:].strip()))
            continue

        # Key-value pair
        if ": " in stripped or stripped.endswith(":"):
            if ": " in stripped:
                key, raw_val = stripped.split(": ", 1)
            else:
                key = stripped[:-1]
                raw_val = ""

            current_key = key.strip()
            current_list = None

            if raw_val:
                result[current_key] = _parse_scalar_value(raw_val)

    return result


def _parse_scalar_value(raw: str) -> Any:
    """Parse a simple YAML scalar."""
    if raw in ("null", "~"):
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    # Strip quotes
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


# ---------------------------------------------------------------------------
# ISO-8601 helper
# ---------------------------------------------------------------------------


def _to_iso(value: datetime | str | None) -> str | None:
    """Convert a datetime or string to ISO-8601 string, or pass through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
