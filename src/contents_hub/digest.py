"""
Digest pipeline — single-file module.

This module owns the digest feature surface end-to-end. The main contracts and
invariants are enforced by the digest tests and public CLI behavior.

Public API:
    - ``build_digest(config, conn) -> Digest``     — T4 (this file)
    - ``dispatch_digest(config, digest) -> Path``  — optional legacy file writer
    - ``run_digest(config, *, force=False) -> dict``  — T6 (separate task)

Provider-backed synthesis goes through
``runners.get_default_text_runner().run(...)``. In no-agent mode, digest uses
deterministic extractive fallbacks and still writes a DB digest row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from contents_hub.config import WikiConfig
from contents_hub.db import get_db
from contents_hub.frontmatter import Frontmatter, assemble_markdown
from contents_hub.lens_inbox import (
    LensInboxCandidate,
    VIEW_MODE_LIST,
    query_lens_inbox,
)
from contents_hub.runners import get_default_text_runner
from contents_hub.runners.no_agent import NoAgentRunner

logger = logging.getLogger(__name__)

# Local module constants -----------------------------------------------------

# R-B4.1 — "no upper-count truncation": pass a very large limit to
# query_lens_inbox so its default 100-row cap (Lens-Inbox web surface) does
# not artificially bound digest candidates. _coerce_limit honors explicit
# values without re-clamping.
_DIGEST_CANDIDATE_LIMIT = 10**9

# R-T3.1 / R-T3.2 / INV-4 — every LLM call uses these exact parameters.
_LLM_MAX_TURNS = 5
_LLM_TIMEOUT = 60
_GROUP_PROMPT_MAX_CHARS = 5500
_GROUP_MAX_ITEMS = 8
_GROUP_CONCURRENCY = 3

# R-U5.1 — synthetic group label when no real Lens groups can be formed.
_UNMATCHED_LENS_ID = "Unmatched"
_UNMATCHED_LENS_NAME = "기타"

# R-T5.1 / R-T5.2 — prompt files live as raw markdown next to this module.
_RECIPES_DIR = Path(__file__).resolve().parent / "recipes" / "digest"
_GROUP_PROMPT_PATH = _RECIPES_DIR / "group.md"
_EXECUTIVE_PROMPT_PATH = _RECIPES_DIR / "executive.md"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DigestSection:
    """One per-lens section of a digest (R-B2.1, R-B2.2, R-T3.1).

    Fields mirror ``contracts.md#DigestSection``:
        - ``lens_id``: lens identifier, or the literal string ``"Unmatched"``
          when synthesized for the lens-zero environment (R-U5.1).
        - ``lens_name``: human-readable label used in the markdown heading.
        - ``narrative_md``: LLM-produced group narrative (one ``runner.run``
          call per section in build_digest's first pass — R-T3.1).
        - ``item_ids``: raw_items.id values represented in this section.
          The same raw_item may appear in multiple sections when matched by
          multiple lenses (R-B2.2).
    """

    lens_id: str
    lens_name: str
    narrative_md: str
    item_ids: list[int] = field(default_factory=list)
    lens_description: str = ""


@dataclass
class Digest:
    """Assembled digest, pre- or post-dispatch (R-T2.1, R-T2.3, R-B2).

    Fields mirror ``contracts.md#Digest``:
        - ``id``: digests.id primary key — ``None`` until ``run_digest``
          INSERTs into the ``digests`` table.
        - ``created_at``: UTC timestamp marking when ``build_digest`` finished
          assembling the digest in memory.
        - ``item_count``: number of unique raw_items represented (cardinality
          of the union of ``DigestSection.item_ids`` across sections).
        - ``content_md``: full assembled markdown body (without frontmatter;
          frontmatter is layered on in ``dispatch_digest`` via
          ``frontmatter.Frontmatter`` per R-T7.1).
        - ``sections``: ordered per-lens sections, plus the synthetic
          "Unmatched" section when applicable.
        - ``output_path``: legacy note path when the optional file writer is
          called. The default run path is DB-only and leaves this as ``None``.
        - ``included_raw_item_ids``: deduplicated union of all
          ``DigestSection.item_ids``; the set bulk-UPDATEd with
          ``digest_id`` in ``run_digest`` (R-B5.2, R-T6.2).
    """

    created_at: datetime
    item_count: int
    content_md: str
    title: str = ""
    sections: list[DigestSection] = field(default_factory=list)
    included_raw_item_ids: list[int] = field(default_factory=list)
    id: Optional[int] = None
    output_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_prompt(path: Path) -> str:
    """R-T5 — simple file read for the prompt template; no RecipeRegistry."""
    return path.read_text(encoding="utf-8")


def _load_lens_context(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    rows = conn.execute("SELECT id, name, description FROM lenses").fetchall()
    context: dict[str, dict[str, str]] = {}
    for row in rows:
        lens_id = str(row["id"] or "")
        if not lens_id:
            continue
        context[lens_id] = {
            "name": str(row["name"] or "").strip() or lens_id,
            "description": str(row["description"] or "").strip(),
        }
    return context


def _json_dict(blob: str | None) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        value = json.loads(blob)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _format_prompt_list(label: str, values: list[str], lines: list[str]) -> None:
    if not values:
        return
    lines.append(f"- {label}:")
    for value in values:
        lines.append(f"  - {value}")


def _format_prompt_quotes(raw: object, lines: list[str]) -> None:
    if not isinstance(raw, list):
        return
    quotes: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            quotes.append(item.strip())
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            speaker = str(item.get("speaker") or "").strip()
            quotes.append(f"{text} -- {speaker}" if speaker else text)
    _format_prompt_list("quotes", quotes, lines)


def _format_prompt_entities(raw: object, lines: list[str]) -> None:
    if not isinstance(raw, list):
        return
    entities: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        kind = str(item.get("type") or "").strip()
        entities.append(f"{name} ({kind})" if kind else name)
    _format_prompt_list("entities", entities, lines)


def _format_items_block(
    candidates: list[LensInboxCandidate],
    lens_id: str,
) -> str:
    """Render the items block injected into the group prompt.

    R-T17.1 — the only fields exposed to the LLM are those already exposed
    during lens classification: title, url, content_summary (body_preview),
    body (carried by body_preview only for now — query_lens_inbox already
    collapses body+summary into ``body_preview`` to bound prompt size), and
    the per-lens summary + bullets sourced from raw_item_lenses.
    """
    lines: list[str] = []
    for idx, cand in enumerate(candidates, start=1):
        # Find the LensMetadata block for THIS lens group, so per-lens
        # summary / bullets are surfaced (and not mixed with other lenses
        # that happen to also match this item).
        meta = next((lm for lm in cand.lenses if lm.id == lens_id), None)
        title = cand.title or "(untitled)"
        url = cand.url or ""
        preview = cand.body_preview or ""
        source = cand.subscription_label or ""
        enriched = _json_dict(meta.enriched_json if meta is not None else None)
        lines.append(f"### Item {idx}: {title}")
        if url:
            lines.append(f"- url: {url}")
        if source:
            lines.append(f"- source: {source}")
        if preview:
            lines.append(f"- preview: {preview}")
        if meta is not None:
            if meta.summary:
                lines.append(f"- lens_summary: {meta.summary}")
            if meta.bullets:
                lines.append("- lens_bullets:")
                for b in meta.bullets:
                    lines.append(f"  - {b}")
        short_title = str(enriched.get("shortTitle") or "").strip()
        one_liner = str(enriched.get("oneLiner") or "").strip()
        narrative_hook = str(enriched.get("narrativeHook") or "").strip()
        quality = str(enriched.get("contentQuality") or "").strip()
        tags = _string_list(enriched.get("tags"))
        if short_title:
            lines.append(f"- short_title: {short_title}")
        if one_liner:
            lines.append(f"- one_liner: {one_liner}")
        _format_prompt_list("key_points", _string_list(enriched.get("keyPoints")), lines)
        _format_prompt_list("details", _string_list(enriched.get("details")), lines)
        _format_prompt_quotes(enriched.get("quotes"), lines)
        _format_prompt_entities(enriched.get("entities"), lines)
        if narrative_hook:
            lines.append(f"- why_it_matters: {narrative_hook}")
        if quality:
            lines.append(f"- content_quality: {quality}")
        if tags:
            lines.append(f"- tags: {', '.join(tags)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_group_prompt(
    template: str,
    *,
    lens_id: str,
    lens_name: str,
    lens_description: str,
    candidates: list[LensInboxCandidate],
) -> str:
    """Substitute group-prompt placeholders.

    Uses str.replace rather than str.format so any stray ``{`` / ``}`` in
    the template body (e.g. markdown code samples) is harmless.
    """
    items_block = _format_items_block(candidates, lens_id=lens_id)
    return (
        template.replace("{lens_id}", lens_id)
        .replace("{lens_name}", lens_name)
        .replace(
            "{lens_description}",
            lens_description if lens_id != _UNMATCHED_LENS_ID else "",
        )
        .replace("{item_count}", str(len(candidates)))
        .replace("{items_block}", items_block)
    )


def _render_executive_prompt(
    template: str,
    *,
    sections: list[DigestSection],
    item_count: int,
) -> str:
    """Substitute executive-prompt placeholders."""
    parts: list[str] = []
    for sec in sections:
        focus = (
            f" (focus: {sec.lens_description})"
            if sec.lens_description.strip()
            else ""
        )
        parts.append(
            f"## {sec.lens_name}{focus} ({sec.lens_id}, {len(sec.item_ids)} items)\n"
        )
        parts.append(sec.narrative_md.strip())
        parts.append("")
    block = "\n".join(parts).rstrip()
    return (
        template.replace("{group_narratives_block}", block)
        .replace("{topic_count}", str(len(sections)))
        .replace("{item_count}", str(item_count))
    )


def _fallback_group_narrative(
    *,
    lens_name: str,
    lens_description: str,
    candidates: list[LensInboxCandidate],
    reason: str = "timeout",
) -> str:
    focus = lens_description.strip() or lens_name
    reason_text = (
        "에이전트 러너가 설정되지 않아(CONTENTS_HUB_AGENT_RUNNER=none), "
        "원문 제목과 렌즈 요약을 근거 중심으로 제공합니다."
        if reason == "no_runner"
        else "모델 합성이 제한 시간 안에 끝나지 않아, 원문 제목과 렌즈 요약을 근거 중심으로 제공합니다."
    )
    lines = [
        f"{lens_name} 관련 핵심 흐름",
        "",
        f"{focus} 관점에서 이번 묶음은 {len(candidates)}건의 원문을 우선 보존해 정리했습니다. "
        + reason_text,
        "",
    ]
    for cand in candidates[:8]:
        meta = cand.representative
        summary = meta.summary.strip() if meta is not None else ""
        preview = summary or cand.body_preview or cand.title
        lines.append(f"- {cand.title}: {preview}")
    lines.extend(["", "📎 관련 아티클", ""])
    for cand in candidates:
        source = cand.subscription_label or "source"
        title = cand.title or cand.url or "Untitled"
        if cand.url:
            lines.append(f"- **[{title}]({cand.url})** via {source}")
        else:
            lines.append(f"- **{title}** via {source}")
    return "\n".join(lines).rstrip()


def _fallback_executive_summary(
    *,
    sections: list[DigestSection],
    item_count: int,
    reason: str = "timeout",
) -> str:
    topic_count = len(sections)
    names = ", ".join(sec.lens_name for sec in sections[:4])
    reason_text = (
        "에이전트 러너가 설정되지 않아(CONTENTS_HUB_AGENT_RUNNER=none) "
        if reason == "no_runner"
        else "일부 합성 호출이 제한 시간 안에 끝나지 않아 "
    )
    return (
        "📌 오늘의 핵심\n\n"
        f"총 {topic_count}가지 주제와 {item_count}건의 아이템을 정리했습니다. "
        f"이번 묶음은 {names}를 중심으로 들어왔고, {reason_text}"
        "각 섹션의 원문 근거와 렌즈 요약을 우선 보존했습니다."
    )


def _candidate_chunks(
    candidates: list[LensInboxCandidate],
    *,
    render_prompt,
    max_chars: int = _GROUP_PROMPT_MAX_CHARS,
    max_items: int = _GROUP_MAX_ITEMS,
) -> list[list[LensInboxCandidate]]:
    chunks: list[list[LensInboxCandidate]] = []
    current: list[LensInboxCandidate] = []
    for cand in candidates:
        proposed = [*current, cand]
        if current and (
            len(proposed) > max_items
            or len(render_prompt(proposed)) > max_chars
        ):
            chunks.append(current)
            current = [cand]
        else:
            current = proposed
    if current:
        chunks.append(current)
    return chunks


def _group_candidates_by_lens(
    candidates: list[LensInboxCandidate],
) -> list[tuple[str, str, list[LensInboxCandidate]]]:
    """Return ``[(lens_id, lens_name, candidates), ...]`` in deterministic order.

    R-B2.2 — a raw_item matched by N lenses appears in N groups (one per
    matching lens). Lens ordering is alphabetical on ``lens_id`` for
    deterministic note layout and test stability.

    R-U5.1 — if NO candidate exposes any lens metadata (the lens-zero
    environment: ``lenses`` table empty / orphan rows / pathological edge),
    synthesize a single ``"Unmatched"`` group containing every candidate.
    """
    by_lens: dict[str, list[LensInboxCandidate]] = {}
    # Best-known display name per lens_id; representative carries the same
    # id as one of cand.lenses entries (deterministic per select_representative).
    lens_name_for_id: dict[str, str] = {}

    for cand in candidates:
        if not cand.lenses:
            # No metadata at all → handle as Unmatched below.
            by_lens.setdefault(_UNMATCHED_LENS_ID, []).append(cand)
            lens_name_for_id.setdefault(_UNMATCHED_LENS_ID, _UNMATCHED_LENS_NAME)
            continue
        for meta in cand.lenses:
            by_lens.setdefault(meta.id, []).append(cand)
            # LensInboxCandidate does not carry lens names — use the lens id
            # itself as the display name; the digest note heading shows
            # "lens_id" which is recognizable and stable.
            lens_name_for_id.setdefault(meta.id, meta.id)

    # R-U5.1 corner case: every candidate ended up under "Unmatched" because
    # none had lens metadata. Nothing more to do — the single group will
    # produce one group narrative call (R-T3.3).
    return [
        (lid, lens_name_for_id[lid], by_lens[lid])
        for lid in sorted(
            by_lens.keys(),
            key=lambda lid: (lid == _UNMATCHED_LENS_ID, lid),
        )
    ]


def _assemble_markdown_body(
    *,
    created_at: datetime,
    item_count: int,
    executive_summary: str,
    sections: list[DigestSection],
) -> str:
    """Compose the human-readable markdown body (no frontmatter — T5 adds it)."""
    parts: list[str] = []
    del created_at
    del item_count
    summary = executive_summary.strip() or "_(empty)_"
    if summary and not summary.startswith("📌"):
        summary = f"📌 오늘의 핵심\n\n{summary}"
    parts.append(summary)
    parts.append("")
    for sec in sections:
        focus = (
            f" (focus: {sec.lens_description})"
            if sec.lens_description.strip()
            else ""
        )
        count = len(dict.fromkeys(sec.item_ids))
        parts.append(f"🎯 {sec.lens_name}{focus} (관심 주제) ({count}건)")
        parts.append("")
        parts.append(sec.narrative_md.strip() or "_(no narrative)_")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _suggest_digest_title(
    *,
    executive_summary: str,
    sections: list[DigestSection],
    created_at: datetime,
) -> str:
    """Derive a compact list title without adding another LLM call."""
    text = executive_summary.strip()
    text = re.sub(r"^\s*📌\s*오늘의\s*핵심\s*", "", text).strip()
    for line in text.splitlines():
        line = line.strip(" #\t")
        if not line:
            continue
        sentence = re.split(
            r"(?<=[.!?。！？])\s+|(?<=[다요죠함음임됨])\.\s*",
            line,
        )[0]
        title = sentence.strip().strip('"')
        if title:
            return title[:78] + ("..." if len(title) > 78 else "")

    names = [sec.lens_name for sec in sections if sec.lens_name.strip()]
    if names:
        head = names[0]
        if len(names) > 1:
            return f"{head} 외 {len(names) - 1}개 주제"
        return head
    return f"Digest {created_at.strftime('%Y-%m-%d %H:%M UTC')}"


# ---------------------------------------------------------------------------
# Public API: build_digest (T4)
# ---------------------------------------------------------------------------


async def build_digest(config: WikiConfig, conn: sqlite3.Connection) -> Digest:
    """Collect candidates, run the N+1 LLM pipeline, return a populated Digest.

    Contract (contracts.md#digest.build_digest):

    1. Queries lens-matched, unclaimed candidates via
       ``query_lens_inbox(conn, ..., status='raw', digest_id_null=True,
       view_mode='list')`` (R-T9.2, R-B3.*).
    2. Groups candidates by lens in Python (multi-lens duplication per
       R-B2.2); raw items without Lens metadata are not digest candidates.
    3. Issues EXACTLY N per-lens group narrative LLM calls + 1 executive
       summary call (INV-4 / R-T3) — each via the text runner with
       ``max_turns=5`` and a 60-second timeout/fallback policy.
    4. Empty candidate set → returns a ``Digest(item_count=0, ...)`` with
       ZERO LLM calls and no sections (INV-6).
    5. Returns a Digest with ``output_path=None`` and ``id=None``; this
       function performs no DB writes and no file writes. ``run_digest``
       (T6) owns the DB transaction.
    """
    created_at = datetime.now(timezone.utc)

    # ---- Step 1: collect candidates -------------------------------------
    # R-T9.2 — reuse query_lens_inbox; do NOT add a parallel SQL path.
    # R-B4.1 — pass a very large limit so the Lens-Inbox 100-row default
    # cap does not truncate digest input.
    view = query_lens_inbox(
        conn,
        sources_dirname=config.sources_dir,
        status="raw",
        digest_id_null=True,
        view_mode=VIEW_MODE_LIST,
        limit=_DIGEST_CANDIDATE_LIMIT,
    )
    candidates: list[LensInboxCandidate] = list(view.get("candidates") or [])

    # ---- INV-6: empty candidate set short-circuit (zero LLM calls) ------
    if not candidates:
        logger.info("build_digest: zero candidates, skipping LLM pipeline")
        return Digest(
            created_at=created_at,
            item_count=0,
            content_md=_assemble_markdown_body(
                created_at=created_at,
                item_count=0,
                executive_summary="_(no items in this digest)_",
                sections=[],
            ),
            sections=[],
            included_raw_item_ids=[],
        )

    # ---- Step 2: group by lens (Python-side) ----------------------------
    groups = _group_candidates_by_lens(candidates)
    lens_context = _load_lens_context(conn)
    logger.info(
        "build_digest: %d candidates → %d group(s) (incl. Unmatched=%s)",
        len(candidates),
        len(groups),
        any(g[0] == _UNMATCHED_LENS_ID for g in groups),
    )

    # ---- Step 3a: N per-lens group narrative LLM calls ------------------
    runner = get_default_text_runner()
    use_extractive_fallback = isinstance(runner, NoAgentRunner)
    group_prompt_template = _load_prompt(_GROUP_PROMPT_PATH)
    group_semaphore = asyncio.Semaphore(_GROUP_CONCURRENCY)

    async def _build_section(
        lens_id: str,
        lens_name: str,
        group_cands: list[LensInboxCandidate],
    ) -> DigestSection:
        ctx = lens_context.get(lens_id, {})
        display_name = ctx.get("name") or lens_name
        lens_description = ctx.get("description") or ""

        def _prompt_for(chunk: list[LensInboxCandidate]) -> str:
            return _render_group_prompt(
                group_prompt_template,
                lens_id=lens_id,
                lens_name=display_name,
                lens_description=lens_description,
                candidates=chunk,
            )

        chunks = _candidate_chunks(
            group_cands,
            render_prompt=_prompt_for,
        )

        async def _run_chunk(chunk: list[LensInboxCandidate]) -> str:
            if use_extractive_fallback:
                return _fallback_group_narrative(
                    lens_name=display_name,
                    lens_description=lens_description,
                    candidates=chunk,
                    reason="no_runner",
                )
            prompt = _prompt_for(chunk)
            try:
                async with group_semaphore:
                    return (
                        await runner.run(
                            prompt,
                            max_turns=_LLM_MAX_TURNS,
                            timeout=_LLM_TIMEOUT,
                        )
                        or ""
                    ).strip()
            except TimeoutError:
                logger.warning(
                    "build_digest: group narrative timed out for %s (%d items)",
                    lens_id,
                    len(chunk),
                )
                return _fallback_group_narrative(
                    lens_name=display_name,
                    lens_description=lens_description,
                    candidates=chunk,
                )

        narratives = await asyncio.gather(
            *[_run_chunk(chunk) for chunk in chunks]
        )
        narrative = "\n\n---\n\n".join(
            narrative for narrative in narratives if narrative.strip()
        )
        return DigestSection(
            lens_id=lens_id,
            lens_name=display_name,
            narrative_md=(narrative or "").strip(),
            item_ids=[c.id for c in group_cands],
            lens_description=lens_description,
        )

    sections = list(
        await asyncio.gather(
            *[
                _build_section(lens_id, lens_name, group_cands)
                for lens_id, lens_name, group_cands in groups
            ]
        )
    )

    # ---- Step 3b: 1 executive summary LLM call --------------------------
    executive_prompt = _render_executive_prompt(
        _load_prompt(_EXECUTIVE_PROMPT_PATH),
        sections=sections,
        item_count=len({c.id for c in candidates}),
    )
    try:
        if use_extractive_fallback:
            executive_summary = _fallback_executive_summary(
                sections=sections,
                item_count=len({c.id for c in candidates}),
                reason="no_runner",
            )
        else:
            executive_summary = (
                await runner.run(
                    executive_prompt,
                    max_turns=_LLM_MAX_TURNS,
                    timeout=_LLM_TIMEOUT,
                )
                or ""
            ).strip()
    except TimeoutError:
        logger.warning("build_digest: executive summary timed out")
        executive_summary = _fallback_executive_summary(
            sections=sections,
            item_count=len({c.id for c in candidates}),
        )

    # ---- Step 4: assemble ------------------------------------------------
    # Deduplicate raw_item_ids across sections (multi-lens duplication
    # produces repeats in section.item_ids by design — R-B2.2 — but the
    # bulk UPDATE in run_digest must stamp each raw_item once).
    seen: set[int] = set()
    included_ids: list[int] = []
    for sec in sections:
        for rid in sec.item_ids:
            if rid not in seen:
                seen.add(rid)
                included_ids.append(rid)

    content_md = _assemble_markdown_body(
        created_at=created_at,
        item_count=len(included_ids),
        executive_summary=executive_summary,
        sections=sections,
    )
    title = _suggest_digest_title(
        executive_summary=executive_summary,
        sections=sections,
        created_at=created_at,
    )

    return Digest(
        created_at=created_at,
        item_count=len(included_ids),
        content_md=content_md,
        title=title,
        sections=sections,
        included_raw_item_ids=included_ids,
    )


# ---------------------------------------------------------------------------
# Public API: dispatch_digest (T5)
# ---------------------------------------------------------------------------


# R-U4.2 / R-T18.1 — UTC timestamp filename format. No subdirectory nesting.
_DIGEST_FILENAME_FMT = "%Y%m%d-%H%M%S"


def _build_digest_frontmatter(digest: "Digest") -> dict:
    """Assemble the YAML frontmatter dict for the digest note.

    Uses ``Frontmatter`` so the YAML keys / formatting match the rest of the
    vault (R-T7.1, CLAUDE.md "Frontmatter via contents_hub.frontmatter" rule).
    The standard wiki-page fields are reused (``type``, ``title``, ``tags``,
    ``lenses``, ``created_at``); the digest-specific ``item_count`` value is
    carried via the extensible ``extra`` bag so we do not have to widen the
    shared dataclass for this single feature.
    """
    lens_ids = [
        s.lens_id
        for s in digest.sections
        if s.lens_id and s.lens_id != _UNMATCHED_LENS_ID
    ]
    title = (
        digest.title
        or f"Digest {digest.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    fm = Frontmatter(
        type="digest",
        title=title,
        tags=["digest"],
        lenses=lens_ids,
        created_at=digest.created_at.isoformat(),
        extra={"item_count": int(digest.item_count)},
    )
    return fm.to_dict()


def dispatch_digest(config: WikiConfig, digest: Digest) -> Path:
    """Write the assembled digest to ``<vault>/digests/YYYYMMDD-HHMMSS.md``.

    Contract (contracts.md#digest.dispatch_digest, R-T2.2, R-U4, R-T18):

    1. Lazily creates ``config.digests_path`` with ``mkdir(parents=True,
       exist_ok=True)`` (R-U4.1) — no error surfaced for a missing parent.
    2. Filename ``YYYYMMDD-HHMMSS.md`` is derived from
       ``datetime.now(timezone.utc)`` at the moment of dispatch (R-U4.2 /
       R-T18.1). There is no subdirectory nesting under ``digests/``
       (INV-7 / R-T18.1).
    3. Frontmatter is layered on via ``Frontmatter`` + ``assemble_markdown``
       (R-T7.1 — the piece deferred from T4) — never hand-rolled YAML
       (CLAUDE.md). The body is ``digest.content_md`` (already composed by
       ``build_digest`` from the DigestSection structure).
    4. Returns the absolute output path. The path is confined under
       ``config.digests_path`` (INV-7); ``Digest.output_path`` is mutated in
       place so callers see the dispatched location.

    Empty digests (``digest.item_count == 0``) — the caller (``run_digest``
    in T6) decides NOT to call this function in the first place per INV-6.
    This function may therefore assume a non-empty Digest, but is robust to
    being called with one anyway (no special-casing — it would simply write a
    near-empty note).

    Permissions (R-U6.1): no special entitlements are required; only standard
    vault-owner filesystem permissions are used for the mkdir + write.

    Args:
        config: Resolved WikiConfig (provides ``digests_path``).
        digest: Populated ``Digest`` returned by ``build_digest``.

    Returns:
        Absolute ``Path`` to the written ``.md`` file under
        ``config.digests_path``. The returned path is also assigned to
        ``digest.output_path``.

    Raises:
        OSError: If the vault is not writable by the current user.
    """
    # R-U4.1 — lazy directory creation. mkdir(exist_ok=True) silently passes
    # when the directory already exists; parents=True covers the case where
    # the vault was just initialized without a prior /digests/ touch.
    digests_dir = config.digests_path
    digests_dir.mkdir(parents=True, exist_ok=True)

    # R-U4.2 / R-T18.1 — UTC timestamp filename, no subdir nesting.
    now_utc = datetime.now(timezone.utc)
    filename = now_utc.strftime(_DIGEST_FILENAME_FMT) + ".md"
    output_path = (digests_dir / filename).resolve()

    # INV-7 — defensive confinement check. digests_dir.resolve() is the only
    # legal prefix; reject anything else (paranoia guard, since filename is
    # generated from strftime and cannot escape).
    digests_root = digests_dir.resolve()
    try:
        output_path.relative_to(digests_root)
    except ValueError as exc:  # pragma: no cover — strftime cannot escape
        raise RuntimeError(
            f"INV-7 violation: digest path {output_path} escapes {digests_root}"
        ) from exc

    # R-T7.1 — Frontmatter via contents_hub.frontmatter (no hand-rolled YAML).
    fm_dict = _build_digest_frontmatter(digest)
    full_markdown = assemble_markdown(fm_dict, digest.content_md)

    # R-U6.1 — plain write_text uses standard vault-owner permissions.
    # Pattern mirrors promote.py:106 and frontmatter.py:597.
    output_path.write_text(full_markdown, encoding="utf-8")

    logger.info(
        "dispatch_digest: wrote %d-item digest to %s",
        digest.item_count,
        output_path,
    )

    # Mutate the in-memory Digest so callers (T6 run_digest) can read the
    # dispatched location without a second strftime / Path join.
    digest.output_path = output_path
    return output_path


# ---------------------------------------------------------------------------
# Public API: run_digest (T6)
# ---------------------------------------------------------------------------


# R-T11.1 / R-U6.2 — one in-process retry after this delay before giving up.
_DB_LOCK_RETRY_DELAY_S = 0.1


def _empty_result() -> dict:
    """INV-6 / R-U2.2 — zero-candidate success payload (no DB row, no file)."""
    return {"ok": True, "digest_id": None, "path": None, "item_count": 0}


def _failure_result() -> dict:
    """R-U2.3 — uniform failure payload for any unrecoverable error."""
    return {"ok": False, "digest_id": None, "path": None, "item_count": 0}


def _serialize_sections(sections: list[DigestSection]) -> str:
    """Encode DigestSection list for the `digests.sections_json` column."""
    return json.dumps(
        [
            {
                "lens_id": s.lens_id,
                "lens_name": s.lens_name,
                "lens_description": s.lens_description,
                "narrative_md": s.narrative_md,
                "item_ids": list(s.item_ids),
            }
            for s in sections
        ],
        ensure_ascii=False,
    )


async def _run_digest_once(config: WikiConfig) -> dict:
    """Single attempt at the full digest pipeline inside one DB tx.

    Implements the contract in contracts.md#digest.run_digest:

    1. Opens exactly ONE ``with get_db(config) as conn:`` block (INV-3).
    2. Inside the block, runs ``build_digest`` FIRST — before any DB write —
       so that any LLM exception leaves zero DB mutation (INV-3, R-T6.1,
       R-B6.3): when ``build_digest`` raises, ``get_db`` rolls back (no
       INSERT was issued) and re-raises the exception to the caller.
    3. Empty candidate set → returns the INV-6 zero payload without
       writing a row, inserting structured links, or stamping anything.
    4. Otherwise: INSERT INTO digests → INSERT structured section/item rows
       → bulk UPDATE raw_items SET digest_id atomically in the SAME
       transaction (R-T6.2). Commit fires on block exit.

    Critical invariants enforced here:

    - INV-2 — every included raw_item gets ``digest_id`` stamped exactly
      once; deduplication of ``included_raw_item_ids`` already happened in
      ``build_digest``.
    - INV-5 / R-B5.1 / R-B5.2 — the UPDATE touches ONLY ``digest_id``; no
      other column on ``raw_items`` is set, so ``raw_items.status`` is
      identical before and after.
    - DB-only output — no ``digests/*.md`` file is written by the default
      pipeline. ``content_md`` remains in SQLite for audit and web rendering.
    """
    with get_db(config) as conn:
        # ---- Step 1: LLM-first (R-T6.1 / R-B6.3) ----------------------
        # Any exception here propagates out of the ``with`` block,
        # triggering rollback in ``get_db``; no INSERT has been issued.
        digest = await build_digest(config, conn)

        # ---- Step 2: empty candidate set short-circuit (INV-6 / R-U3.1)
        if digest.item_count == 0 or not digest.included_raw_item_ids:
            logger.info(
                "run_digest: zero candidates — no DB row, no file written"
            )
            return _empty_result()

        # ---- Step 3: INSERT digests row first to obtain digest_id -----
        # sections_json keeps the narrative snapshot; content_md persists
        # the assembled body for audit/QA and the web UI.
        sections_json = _serialize_sections(digest.sections)
        cur = conn.execute(
            """
            INSERT INTO digests (
                created_at, title, item_count, content_md, sections_json,
                status, error, output_path
            ) VALUES (?, ?, ?, ?, ?, 'ok', '', '')
            """,
            (
                digest.created_at.isoformat(),
                digest.title,
                int(digest.item_count),
                digest.content_md,
                sections_json,
            ),
        )
        digest_id = int(cur.lastrowid)
        digest.id = digest_id

        # ---- Step 4: structured per-section item links for web --------
        # sections_json keeps the narrative snapshot; digest_section_items
        # is the relational surface the web UI can join against for URLs and
        # save toggles without parsing markdown.
        section_item_rows: list[tuple[int, int, str, int, int]] = []
        for section_index, section in enumerate(digest.sections):
            seen_in_section: set[int] = set()
            sort_order = 0
            for raw_item_id in section.item_ids:
                if raw_item_id in seen_in_section:
                    continue
                seen_in_section.add(raw_item_id)
                section_item_rows.append(
                    (
                        digest_id,
                        section_index,
                        section.lens_id,
                        int(raw_item_id),
                        sort_order,
                    )
                )
                sort_order += 1
        conn.executemany(
            """INSERT INTO digest_section_items
               (digest_id, section_index, lens_id, raw_item_id, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            section_item_rows,
        )

        # ---- Step 5: atomic bulk UPDATE — INV-2 / R-T6.2 / R-B5.* -----
        # Touch ONLY digest_id; do NOT mention status anywhere (INV-5).
        # Use an executemany over single-row UPDATEs to keep the SQL
        # uniform regardless of how many ids are involved (avoids the
        # SQLite "too many host parameters" cap and keeps the statement
        # simple to read).
        conn.executemany(
            "UPDATE raw_items SET digest_id = ? WHERE id = ?",
            [(digest_id, rid) for rid in digest.included_raw_item_ids],
        )

        # Commit auto-fires on block exit (R-T6.2 / get_db contract).
        return {
            "ok": True,
            "digest_id": digest_id,
            "path": None,
            "item_count": int(digest.item_count),
        }


async def run_digest(config: WikiConfig, *, force: bool = False) -> dict:
    """Run the full digest pipeline end-to-end.

    Contract (contracts.md#digest.run_digest — R-T2.3, R-T6, R-B6.3, R-U3):

    - Opens exactly one ``with get_db(config) as conn:`` block (INV-3).
    - The LLM pipeline (``build_digest``) runs BEFORE any DB write so an
      LLM exception leaves the DB unchanged (R-T6.1, R-B6.3).
    - On success: INSERT INTO digests → structured section/item rows → bulk
      UPDATE ``raw_items.digest_id`` — all atomic inside the single
      transaction (R-T6.2). The block exits normally and ``get_db`` commits.
    - On LLM failure: returns ``{ok:False, ...}``. The transaction is
      rolled back by ``get_db``; no ``digests`` row exists; no
      ``raw_items.digest_id`` was stamped. The CLI (T7) will translate
      ``ok:False`` into a non-zero exit code (R-U3.2, R-B6.3).
    - On ``sqlite3.OperationalError`` whose message contains 'locked':
      retry exactly once after 100 ms (R-T11.1, R-U6.2). If the retry
      also fails, return ``{ok:False, ...}`` and the CLI emits a non-zero
      exit (R-U3.3). This process never implements its own file-lock —
      external ``flock`` is the recommended caller-side guard (R-U6.2).
    - Empty candidate set: returns ``{ok:True, digest_id:None,
      path:None, item_count:0}``; no file is written, no DB row is
      inserted (INV-6, R-U3.1).

    NEVER mutates ``raw_items.status`` (INV-5 / R-B5.1).

    Args:
        config: Resolved ``WikiConfig`` (vault, sources_dir, digests_path).
        force: Reserved for future use (--force flag deferred from MVP per
            R-U1.3); currently no-op. Accepting it keeps the signature
            stable for the CLI surface coming in T7.

    Returns:
        ``{"ok": bool, "digest_id": int | None, "path": str | None,
        "item_count": int}``. The CLI (T7) emits this dict verbatim via
        ``_emit_json`` and sets exit code = 0 when ``ok`` else non-zero.
    """
    # ``force`` is accepted for signature stability (T7 / R-U1.3) but has
    # no behavioural effect in MVP — every invocation collects the full
    # unclaimed candidate set anyway.
    del force

    # ---- LLM failure path (R-U3.2 / R-B6.3) ------------------------------
    # We catch broad Exception ONLY when it originated from the LLM
    # pipeline (build_digest). sqlite3.OperationalError("locked") is
    # caught separately below so the retry policy can engage.
    try:
        return await _run_digest_once(config)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" not in msg:
            # Non-lock OperationalError (schema/syntax issue) — treat as
            # an unrecoverable failure, no retry. Logged and reported.
            logger.exception(
                "run_digest: non-lock OperationalError — treating as failure"
            )
            return _failure_result()

        # R-T11.1 — exactly one retry after 100 ms, then give up.
        logger.warning(
            "run_digest: database locked — retrying once after %.0f ms",
            _DB_LOCK_RETRY_DELAY_S * 1000,
        )
        await asyncio.sleep(_DB_LOCK_RETRY_DELAY_S)
        try:
            return await _run_digest_once(config)
        except sqlite3.OperationalError:
            logger.exception(
                "run_digest: database locked on retry — aborting (R-U3.3)"
            )
            return _failure_result()
        except Exception:
            # If the retry attempt fails in the LLM pipeline (or anywhere
            # else), report uniform failure. get_db rolled back on the way
            # out so no partial digest row was committed (R-B6.3).
            logger.exception(
                "run_digest: retry attempt failed after lock — aborting"
            )
            return _failure_result()
    except Exception:
        # Any other exception — most importantly an LLM failure raised by
        # runner.run() inside build_digest — leaves zero DB mutation
        # (get_db rolled back) and is surfaced as a uniform failure
        # payload for the CLI to translate into a non-zero exit code.
        logger.exception("run_digest: pipeline failed — aborting (R-U3.2)")
        return _failure_result()
