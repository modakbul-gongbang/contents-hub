"""Recipe registry for source-specific collection strategies.

A "recipe" is a natural-language specification describing how to:
    - LIST_STRATEGY: obtain new content URLs for a source
    - CONTENT_STRATEGY: fetch body/metadata for a single URL
    - METADATA: extract title/author/published_at

Seed recipes live in ``recipes/seed/{recipe_base}.md`` (one per featured
source type). A subscription may still carry a compatibility override in
``subscription.config['recipe']``, but the runtime no longer learns or
rewrites recipes automatically.

Prompt templates for the compatibility execute path and split LIST/CONTENT
agent fallbacks live in ``recipes/templates/{name}.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contents_hub.source_types import get_source_type_spec

_HERE = Path(__file__).resolve().parent
_SEED_DIR = _HERE / "seed"
_TEMPLATES_DIR = _HERE / "templates"


@dataclass(frozen=True)
class RecipeSpec:
    """Versioned recipe metadata used to pin deterministic subscriptions."""

    recipe_id: str
    version: int
    source_type: str
    seed_name: str
    execution_method: str
    capabilities: tuple[str, ...]
    channel: str = "stable"


_RECIPE_SEEDS: dict[str, str] = {
    "rss.feed.default": "rss",
    "youtube.channel.default": "youtube",
    "github.releases.default": "rss",
    "x.profile.default": "twitter",
    "linkedin.profile.default": "linkedin",
    "threads.profile.default": "threads",
    "substack.publication.default": "substack",
    "substack.tag.default": "substack",
    "medium.publication.default": "medium",
    "reddit.subreddit.default": "reddit",
    "webpage.generic.default": "webpage",
}


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


class RecipeRegistry:
    """Registry for per-source recipes and prompt templates."""

    @staticmethod
    def get_seed(recipe_base: str) -> str | None:
        """Load seed recipe content for a recipe_base (e.g. "youtube").

        Returns None if no seed file exists.
        """
        if not recipe_base:
            return None
        spec = RecipeRegistry.get_recipe_spec_for_id(recipe_base)
        if spec is not None:
            recipe_base = spec.seed_name
        return _safe_read(_SEED_DIR / f"{recipe_base}.md")

    @staticmethod
    def get_recipe_spec_for_id(recipe_id: str, version: int | None = None) -> RecipeSpec | None:
        """Return recipe metadata for a recipe ID.

        Only v1 seed recipes exist today. The explicit version parameter keeps
        the public shape ready for future catalog versions.
        """
        seed_name = _RECIPE_SEEDS.get(recipe_id)
        if seed_name is None:
            return None
        requested_version = int(version or 1)
        if requested_version != 1:
            return None

        for source_type in (
            "rss.feed",
            "youtube.channel",
            "github.releases",
            "x.profile",
            "linkedin.profile",
            "threads.profile",
            "substack.publication",
            "substack.tag",
            "medium.publication",
            "reddit.subreddit",
            "webpage",
        ):
            spec = get_source_type_spec(source_type)
            if spec is not None and spec.recipe_id == recipe_id:
                return RecipeSpec(
                    recipe_id=recipe_id,
                    version=1,
                    source_type=spec.id,
                    seed_name=seed_name,
                    execution_method=spec.execution_method,
                    capabilities=spec.capabilities,
                )
        return None

    @staticmethod
    def get_recipe_spec(subscription: Any) -> RecipeSpec | None:
        """Resolve the active recipe spec from config pin or source type."""
        config = _get_config(subscription)
        recipe_id = config.get("recipe_id")
        recipe_version = config.get("recipe_version")
        if isinstance(recipe_id, str) and recipe_id.strip():
            spec = RecipeRegistry.get_recipe_spec_for_id(recipe_id, recipe_version)
            if spec is not None:
                return spec

        source_type = (
            getattr(subscription, "recipe_base", None)
            or config.get("recipe_base")
            or getattr(subscription, "source_type", None)
        )
        source_spec = get_source_type_spec(source_type)
        if source_spec is None:
            return None
        return RecipeSpec(
            recipe_id=source_spec.recipe_id,
            version=source_spec.recipe_version,
            source_type=source_spec.id,
            seed_name=_RECIPE_SEEDS.get(source_spec.recipe_id, ""),
            execution_method=source_spec.execution_method,
            capabilities=source_spec.capabilities,
        )

    @staticmethod
    def ensure_recipe_pin(subscription: Any) -> RecipeSpec | None:
        """Ensure config carries the selected recipe ID/version metadata."""
        spec = RecipeRegistry.get_recipe_spec(subscription)
        if spec is None:
            return None
        config = _get_config(subscription)
        config.setdefault("recipe_base", spec.source_type)
        config.setdefault("recipe_id", spec.recipe_id)
        config.setdefault("recipe_version", spec.version)
        config.setdefault("recipe_channel", spec.channel)
        config.setdefault("fetch_method", spec.execution_method)
        config.setdefault("recipe_capabilities", list(spec.capabilities))
        _set_config(subscription, config)
        return spec

    @staticmethod
    def get_recipe_metadata(subscription: Any) -> dict[str, Any]:
        """Return active recipe metadata as a template/UI friendly dict."""
        spec = RecipeRegistry.ensure_recipe_pin(subscription)
        if spec is None:
            return {}
        return {
            "recipe_id": spec.recipe_id,
            "recipe_version": spec.version,
            "recipe_channel": spec.channel,
            "source_type": spec.source_type,
            "fetch_method": spec.execution_method,
            "capabilities": list(spec.capabilities),
        }

    @staticmethod
    def get_recipe(subscription: Any) -> str | None:
        """Return the active recipe for a subscription.

        Priority:
            1. ``subscription.config['recipe']`` (user/agent override)
            2. seed for ``subscription.source_type`` (or ``recipe_base``)
        Returns None if neither is available.
        """
        override = _get_config(subscription).get("recipe")
        if isinstance(override, str) and override.strip():
            return override

        spec = RecipeRegistry.ensure_recipe_pin(subscription)
        if spec and spec.seed_name:
            return RecipeRegistry.get_seed(spec.seed_name)

        base = getattr(subscription, "recipe_base", None) or getattr(subscription, "source_type", None)
        if base:
            return RecipeRegistry.get_seed(str(base))
        return None

    @staticmethod
    def set_override(subscription: Any, recipe: str) -> None:
        """Persist a recipe override on the subscription config."""
        config = _get_config(subscription)
        config["recipe"] = recipe
        _set_config(subscription, config)

    @staticmethod
    def clear_override(subscription: Any) -> None:
        """Remove the recipe override so the seed is used again."""
        config = _get_config(subscription)
        if "recipe" in config:
            del config["recipe"]
            _set_config(subscription, config)

    @staticmethod
    def get_template(name: str) -> str:
        """Load a prompt template by name (without extension).

        Raises FileNotFoundError if the template is missing.
        """
        path = _TEMPLATES_DIR / f"{name}.md"
        content = _safe_read(path)
        if content is None:
            raise FileNotFoundError(f"Template not found: {path}")
        return content

    @staticmethod
    def render_prompt(name: str, *, browser_backend: str = "chromux", **fields: Any) -> str:
        """Render transport instructions separately from source collection rules.

        Aside's agent uses its native browser tools. Recipe tool names are
        translated as operation hints; contents-hub still owns all persistence.
        Subscription overrides receive the same translation as seed recipes.
        """
        if browser_backend not in {"chromux", "aside"}:
            raise ValueError("Unsupported browser backend")
        instructions = RecipeRegistry.get_template(f"browser_{browser_backend}")
        template = RecipeRegistry.get_template(name).replace(
            "{browser_instructions}", instructions,
        )
        if browser_backend == "aside":
            recipe = fields["recipe"]
            for old, new in (
                ("chromux_scroll_extract", "browser_scroll_extract"),
                ("chromux_navigate", "browser_navigate"),
                ("chromux_extract", "browser_extract"),
                ("chromux_scroll", "browser_scroll"),
                ("extract_metadata", "browser_metadata"),
                ("persist_raw", "caller_owned_storage_do_not_call"),
                ("`contents-hub` chromux 프로필", "Aside 브라우저"),
                ("contents-hub chromux 프로필", "Aside 브라우저"),
            ):
                recipe = recipe.replace(old, new)
            fields["recipe"] = recipe
        return template.format(**fields)


def _get_config(subscription: Any) -> dict[str, Any]:
    config = getattr(subscription, "config", None)
    if isinstance(config, dict):
        return config
    return {}


def _set_config(subscription: Any, config: dict[str, Any]) -> None:
    try:
        setattr(subscription, "config", config)
    except Exception:
        pass


__all__ = ["RecipeRegistry", "RecipeSpec"]
