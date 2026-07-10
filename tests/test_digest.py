"""Tests for the digest pipeline (T8).

Covers contracts.md invariants INV-1..INV-8 and sub-requirements
R-B1.1, R-B6.4, R-T10.1, R-T16.1, R-U5.2 (per task T8 charter), plus
supporting evidence for R-T3, R-T6, R-B5, R-U2, R-U3, R-U5.

All tests inject a synchronous stub :class:`AgentRunner` via
``set_default_runner`` so the suite never touches the real
``claude_agent_sdk`` or provider API credentials.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from contents_hub.cli import main as cli_main
from contents_hub.config import WikiConfig
from contents_hub.db import get_db, init_db
from contents_hub.digest import (
    run_digest,
)
from contents_hub.runners import get_default_runner, set_default_runner
from contents_hub.runners.no_agent import NoAgentRunner


# ---------------------------------------------------------------------------
# Stub runner — counts ``runner.run(...)`` calls and records kwargs.
# ---------------------------------------------------------------------------


class CountingRunner:
    """Hermetic stub that records every ``runner.run`` invocation."""

    def __init__(self, *, fail_on_call: int | None = None):
        self.calls: list[dict] = []
        self.fail_on_call = fail_on_call

    async def run(self, prompt, *, max_turns=30, timeout=600.0):
        self.calls.append(
            {"prompt": prompt, "max_turns": max_turns, "timeout": timeout}
        )
        if self.fail_on_call is not None and len(self.calls) >= self.fail_on_call:
            raise RuntimeError("LLM blew up (stub failure)")
        # Return a deterministic body so prompt assembly can be verified.
        return f"narrative-{len(self.calls)}"


@pytest.fixture
def stub_runner():
    """Install a fresh ``CountingRunner`` for the duration of one test."""
    original = get_default_runner()
    runner = CountingRunner()
    set_default_runner(runner)  # type: ignore[arg-type]
    try:
        yield runner
    finally:
        set_default_runner(original)


# ---------------------------------------------------------------------------
# Vault fixture — fresh tmp_path-backed config + initialized DB.
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Fresh vault with .contents-hub/ + schema-v8 DB.

    Also sets ``$CONTENTS_HUB_VAULT`` so the CLI can resolve the vault even
    when ``--vault`` is not passed on argv.
    """
    cfg = WikiConfig(vault_path=tmp_path)
    (tmp_path / ".contents-hub").mkdir(parents=True, exist_ok=True)
    init_db(cfg)
    monkeypatch.setenv("CONTENTS_HUB_VAULT", str(tmp_path))
    return cfg


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _seed_lensed_items(
    cfg: WikiConfig,
    *,
    item_lens_pairs: list[tuple[int, str]],
    lenses: list[str] | None = None,
    extra_no_lens_ids: list[int] | None = None,
) -> None:
    """Insert raw_items + lenses + raw_item_lenses rows.

    Args:
        item_lens_pairs: ``[(raw_item_id, lens_id), ...]`` — one row per
            pair inserted into ``raw_item_lenses``. The same raw_item id may
            appear multiple times (multi-lens duplication scenarios).
        lenses: explicit lens ids to insert. Defaults to the unique set
            derived from ``item_lens_pairs``.
        extra_no_lens_ids: raw_item ids inserted with status='raw' but NO
            row in raw_item_lenses. Digest excludes these from candidates.
            The fixture also inserts a subscription #1.
    """
    now = _now()
    raw_ids = {rid for rid, _ in item_lens_pairs}
    if extra_no_lens_ids:
        raw_ids.update(extra_no_lens_ids)
    lens_ids = lenses if lenses is not None else sorted({lid for _, lid in item_lens_pairs})
    with get_db(cfg) as conn:
        conn.execute(
            "INSERT INTO subscriptions (id,url,title,created_at,updated_at)"
            " VALUES (?,?,?,?,?)",
            (1, "https://example.com/", "Sub", now, now),
        )
        for lid in lens_ids:
            conn.execute(
                "INSERT INTO lenses (id,name,created_at,updated_at)"
                " VALUES (?,?,?,?)",
                (lid, lid.upper(), now, now),
            )
        for rid in sorted(raw_ids):
            conn.execute(
                "INSERT INTO raw_items"
                " (id,url,title,body,status,subscription_id,content_summary,"
                "collected_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    f"https://example.com/{rid}",
                    f"Title {rid}",
                    f"body {rid}",
                    "raw",
                    1,
                    f"summary {rid}",
                    now,
                    now,
                ),
            )
        for rid, lid in item_lens_pairs:
            conn.execute(
                "INSERT INTO raw_item_lenses"
                " (raw_item_id,lens_id,summary,bullets_json) VALUES (?,?,?,?)",
                (rid, lid, f"lens-{lid}-summary", '["b1","b2"]'),
            )


def _statuses(cfg: WikiConfig, ids: list[int]) -> dict[int, str]:
    """Return ``{id: status}`` for the given raw_item ids."""
    placeholders = ",".join("?" * len(ids))
    with get_db(cfg) as conn:
        rows = conn.execute(
            f"SELECT id, status FROM raw_items WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    return {row["id"]: row["status"] for row in rows}


def _digest_ids(cfg: WikiConfig, ids: list[int]) -> dict[int, int | None]:
    placeholders = ",".join("?" * len(ids))
    with get_db(cfg) as conn:
        rows = conn.execute(
            f"SELECT id, digest_id FROM raw_items WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    return {row["id"]: row["digest_id"] for row in rows}


def _digest_row_count(cfg: WikiConfig) -> int:
    with get_db(cfg) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM digests").fetchone()["c"]


def _digest_content(cfg: WikiConfig, digest_id: int) -> str:
    with get_db(cfg) as conn:
        row = conn.execute(
            "SELECT content_md FROM digests WHERE id = ?",
            (digest_id,),
        ).fetchone()
    return "" if row is None else row["content_md"]


def _digest_row(cfg: WikiConfig, digest_id: int) -> dict:
    with get_db(cfg) as conn:
        row = conn.execute(
            "SELECT * FROM digests WHERE id = ?",
            (digest_id,),
        ).fetchone()
    return {} if row is None else dict(row)


def _digest_section_item_rows(cfg: WikiConfig, digest_id: int) -> list[dict]:
    with get_db(cfg) as conn:
        rows = conn.execute(
            """SELECT digest_id, section_index, lens_id, raw_item_id, sort_order
               FROM digest_section_items
               WHERE digest_id = ?
               ORDER BY section_index, sort_order, raw_item_id""",
            (digest_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Test class — covers R-B1.1, R-B6.4, R-T10.1, R-T16.1, R-U5.2 + INV 1..8.
# ---------------------------------------------------------------------------


class TestDigestEmptyCandidates:
    """INV-6 / R-U2.2 / R-U3.1 — empty candidate set short-circuit."""

    async def test_run_digest_empty_returns_null_payload_no_writes(
        self, vault, stub_runner, tmp_path
    ):
        """No raw_items → run_digest returns the canonical empty payload."""
        result = await run_digest(vault)

        # JSON contract: exit 0 surface, all nullable fields are null.
        assert result == {
            "ok": True,
            "digest_id": None,
            "path": None,
            "item_count": 0,
        }
        # ZERO LLM calls (INV-6 short-circuit).
        assert stub_runner.calls == []
        # ZERO file written (no digests/ created, or empty if created).
        digests_dir = vault.digests_path
        if digests_dir.exists():
            assert list(digests_dir.glob("*.md")) == []
        # ZERO digests row.
        assert _digest_row_count(vault) == 0

    def test_cli_empty_emits_single_json_line_exit_zero(
        self, vault, stub_runner, tmp_path
    ):
        """CLI: stdout = single JSON line, exit code 0 (R-U2.2 / R-U3.1)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli_main(["--vault", str(tmp_path), "digest"])
        assert exit_code == 0
        stdout = buf.getvalue()
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected exactly 1 JSON line, got: {stdout!r}"
        payload = json.loads(lines[0])
        assert payload == {
            "ok": True,
            "digest_id": None,
            "path": None,
            "item_count": 0,
        }


class TestLensZeroUnmatchedGroup:
    """R-U5.1 / R-U5.2 — lens-zero environment with eligible candidates.

    R-U5.1 says: when ``raw_item_lenses contains no rows (or Lenses table
    is empty) and at least one raw_item meets the candidate filter``,
    all candidates collapse to a single ``"Unmatched"`` group. R-U5.2
    asserts the JSON contract (``ok=true``, ``item_count`` = N) and exit
    code 0 in that case.

    Two angles are tested:

    1. **Unit-level Unmatched grouping** — drive
       ``_group_candidates_by_lens`` directly with synthetic candidates
       whose ``.lenses`` tuple is empty. This is the only path that
       remains hermetic against the FK constraint on raw_item_lenses
       (which forbids true orphan rows).
    2. **Single-lens end-to-end** — the single-section flow used in
       practice when only one Lens exists: exactly 1 group + 1 executive
       = 2 LLM calls, ok=true, item_count = N.
    """

    def test_group_candidates_by_lens_synthesizes_unmatched_for_empty_lenses(
        self,
    ):
        """Direct unit test on the grouping helper (R-U5.1)."""
        from contents_hub.digest import (
            _UNMATCHED_LENS_ID,
            _UNMATCHED_LENS_NAME,
            _group_candidates_by_lens,
        )
        from contents_hub.lens_inbox import LensInboxCandidate

        def _mk(rid: int) -> LensInboxCandidate:
            return LensInboxCandidate(
                id=rid,
                title=f"t{rid}",
                url=f"u{rid}",
                status="raw",
                collected_at=_now(),
                subscription_id=1,
                subscription_label="Sub",
                body_preview="",
                source_note_path=None,
                lenses=(),  # ← lens-zero: empty
                representative=None,
            )

        groups = _group_candidates_by_lens([_mk(1), _mk(2)])
        assert len(groups) == 1
        lens_id, lens_name, cands = groups[0]
        assert lens_id == _UNMATCHED_LENS_ID
        assert lens_name == _UNMATCHED_LENS_NAME
        assert [c.id for c in cands] == [1, 2]

    async def test_single_lens_yields_two_llm_calls_ok_true(
        self, vault, stub_runner
    ):
        """End-to-end single-section run: 1 group + 1 executive = 2 calls."""
        _seed_lensed_items(
            vault,
            item_lens_pairs=[(10, "solo"), (11, "solo")],
        )

        result = await run_digest(vault)

        # R-U5.2 JSON contract (also covers single-lens MVP path).
        assert result["ok"] is True
        assert result["item_count"] == 2
        assert isinstance(result["digest_id"], int)
        assert result["path"] is None

        # 1 group narrative + 1 executive = 2 runner.run calls.
        assert len(stub_runner.calls) == 2
        for call in stub_runner.calls:
            assert call["max_turns"] == 5
            assert call["timeout"] == 60

    def test_single_lens_cli_exit_zero(self, vault, stub_runner, tmp_path):
        """R-U5.2 CLI surface — exit 0 in single-group case."""
        _seed_lensed_items(vault, item_lens_pairs=[(1, "solo")])

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli_main(["--vault", str(tmp_path), "digest"])
        assert exit_code == 0
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["ok"] is True
        assert payload["item_count"] == 1

    async def test_no_agent_digest_uses_extractive_fallback_and_writes_db(
        self, vault
    ):
        """Runtime-neutral install can create a digest without provider calls."""
        original = get_default_runner()
        set_default_runner(NoAgentRunner())  # type: ignore[arg-type]
        try:
            _seed_lensed_items(vault, item_lens_pairs=[(1, "solo")])
            result = await run_digest(vault)
        finally:
            set_default_runner(original)

        assert result["ok"] is True
        assert result["item_count"] == 1
        assert isinstance(result["digest_id"], int)
        content = _digest_content(vault, int(result["digest_id"]))
        assert "Title 1" in content
        assert _digest_ids(vault, [1])[1] == result["digest_id"]


class TestTwoPassLLMCallCount:
    """INV-4 / R-T3.1 / R-T3.2 — exactly N group + 1 executive calls."""

    async def test_n_groups_yield_n_plus_one_llm_calls(
        self, vault, stub_runner
    ):
        # 3 distinct lenses → 3 group narrative + 1 executive = 4 calls.
        _seed_lensed_items(
            vault,
            item_lens_pairs=[
                (1, "ai"),
                (2, "rust"),
                (3, "ml"),
            ],
        )

        result = await run_digest(vault)

        assert result["ok"] is True
        assert result["item_count"] == 3
        # INV-4 exact call count.
        assert len(stub_runner.calls) == 4, (
            f"expected 3 group + 1 executive = 4 calls, "
            f"got {len(stub_runner.calls)}"
        )
        # INV-4 exact LLM parameter contract on every call.
        for call in stub_runner.calls:
            assert call["max_turns"] == 5
            assert call["timeout"] == 60


class TestIdempotencyTransaction:
    """INV-2 / INV-5 / R-B5.1 / R-B5.2 / R-B6.2 — digest_id stamped, status untouched."""

    async def test_digest_id_stamped_status_unchanged_then_no_re_inclusion(
        self, vault, stub_runner
    ):
        ids = [1, 2, 3]
        _seed_lensed_items(
            vault,
            item_lens_pairs=[(1, "ai"), (2, "ai"), (3, "rust")],
        )
        # Snapshot statuses BEFORE the digest run.
        before_status = _statuses(vault, ids)
        assert all(s == "raw" for s in before_status.values())

        first = await run_digest(vault)
        assert first["ok"] is True
        assert first["item_count"] == 3
        digest_id = first["digest_id"]
        assert isinstance(digest_id, int)

        # INV-2 — every included raw_item has digest_id stamped exactly.
        stamped = _digest_ids(vault, ids)
        assert all(d == digest_id for d in stamped.values()), stamped

        # INV-5 / R-B5.1 — status column UNCHANGED for every included item.
        after_status = _statuses(vault, ids)
        assert after_status == before_status

        # R-B6.2 — subsequent run finds zero candidates (idempotency).
        stub_runner.calls.clear()
        second = await run_digest(vault)
        assert second == {
            "ok": True,
            "digest_id": None,
            "path": None,
            "item_count": 0,
        }
        # Subsequent run made ZERO additional LLM calls.
        assert stub_runner.calls == []

        # Statuses STILL unchanged after the second run (INV-5 sticky).
        assert _statuses(vault, ids) == before_status


class TestJSONStdoutShape:
    """R-U2.1 / R-U2.3 / R-T12.2 — exactly one JSON line on stdout."""

    def test_success_stdout_single_json_line_db_only_path_null(
        self, vault, stub_runner, tmp_path
    ):
        _seed_lensed_items(vault, item_lens_pairs=[(1, "ai"), (2, "rust")])

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli_main(["--vault", str(tmp_path), "digest"])
        assert exit_code == 0

        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected 1 JSON line, got: {buf.getvalue()!r}"
        payload = json.loads(lines[0])
        # Frozen schema (R-U2.1) — exactly these four keys.
        assert set(payload.keys()) == {"ok", "digest_id", "path", "item_count"}
        assert payload["ok"] is True
        assert isinstance(payload["digest_id"], int)
        assert payload["item_count"] == 2
        assert payload["path"] is None

    def test_failure_stdout_single_json_line(self, vault, tmp_path):
        """LLM failure → stdout has exactly one ``ok:false`` JSON line."""
        _seed_lensed_items(vault, item_lens_pairs=[(1, "ai")])

        original = get_default_runner()
        # Fail on the very first LLM call (group narrative pass).
        failing = CountingRunner(fail_on_call=1)
        set_default_runner(failing)  # type: ignore[arg-type]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = cli_main(["--vault", str(tmp_path), "digest"])
        finally:
            set_default_runner(original)

        # R-U3.2 — non-zero exit on LLM failure.
        assert exit_code != 0

        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected 1 JSON line, got: {buf.getvalue()!r}"
        payload = json.loads(lines[0])
        assert payload == {
            "ok": False,
            "digest_id": None,
            "path": None,
            "item_count": 0,
        }


class TestLegacyStyleBriefingFormat:
    """The assembled note should match the Nudget-style briefing shape."""

    async def test_digest_content_starts_with_highlights_and_lens_sections(
        self, vault
    ):
        now = _now()
        enriched = {
            "shortTitle": "2인으로 $2M ARR 달성한 AI",
            "oneLiner": "두 명의 창업자가 AI 에이전트로 연 200만 달러 ARR을 달성했다.",
            "keyPoints": [
                "AI 에이전트가 필요한 도구를 직접 만들며 업무를 확장한다.",
                "풍부한 비즈니스 컨텍스트가 결과 품질을 좌우한다.",
            ],
            "details": [
                "Answer This는 단 2명의 팀으로 연 200만 달러 ARR을 만들었다.",
                "Skyvern은 고객 통화, 이메일, 슬랙 등 내부 맥락을 에이전트에게 제공했다.",
            ],
            "quotes": [
                {
                    "text": "에이전트가 저품질 결과물을 만드는 이유는 컨텍스트가 없기 때문입니다.",
                    "speaker": "Sachin",
                }
            ],
            "entities": [{"name": "Skyvern", "type": "company"}],
            "narrativeHook": "AI 창업의 차별점은 모델보다 컨텍스트 운영 체계다.",
        }
        with get_db(vault) as conn:
            conn.execute(
                "INSERT INTO subscriptions (id,url,title,created_at,updated_at)"
                " VALUES (?,?,?,?,?)",
                (1, "https://yc.example/", "YC Root Access", now, now),
            )
            conn.execute(
                "INSERT INTO lenses (id,name,description,created_at,updated_at)"
                " VALUES (?,?,?,?,?)",
                (
                    "ai-business",
                    "현재 AI 시대에 어떤 창업을 해야할지. 비즈니스적 관점에서 제시",
                    "AI 창업 전략과 비즈니스 관점",
                    now,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO raw_items"
                " (id,url,title,body,status,subscription_id,content_summary,"
                "collected_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        1,
                        "https://yc.example/answer-this",
                        "2인으로 $2M ARR 달성한 AI",
                        "AI 에이전트 창업 사례",
                        "raw",
                        1,
                        "두 명의 창업자가 AI 에이전트로 ARR을 만들었다.",
                        now,
                        now,
                    ),
                    (
                        2,
                        "https://yc.example/context",
                        "AI 에이전트 성공의 핵심: 컨텍스트",
                        "컨텍스트가 없으면 에이전트 결과가 낮아진다.",
                        "raw",
                        1,
                        "AI에게 비즈니스 맥락을 제공해야 한다.",
                        now,
                        now,
                    ),
                ],
            )
            conn.executemany(
                "INSERT INTO raw_item_lenses"
                " (raw_item_id,lens_id,summary,bullets_json,enriched_json)"
                " VALUES (?,?,?,?,?)",
                [
                    (
                        1,
                        "ai-business",
                        enriched["oneLiner"],
                        json.dumps(enriched["keyPoints"], ensure_ascii=False),
                        json.dumps(enriched, ensure_ascii=False),
                    ),
                    (
                        2,
                        "ai-business",
                        "에이전트 성능은 비즈니스 컨텍스트 제공 방식에 달려 있다.",
                        '["컨텍스트가 없으면 결과 품질이 낮아진다."]',
                        json.dumps(
                            {
                                **enriched,
                                "shortTitle": "AI 에이전트 성공의 핵심: 컨텍스트",
                                "oneLiner": "에이전트 성능은 비즈니스 컨텍스트 제공 방식에 달려 있다.",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ],
            )

        class BriefingRunner(CountingRunner):
            async def run(self, prompt, *, max_turns=30, timeout=600.0):
                self.calls.append(
                    {"prompt": prompt, "max_turns": max_turns, "timeout": timeout}
                )
                if "Per-Lens Briefing Narrative Prompt" in prompt:
                    assert "short_title: 2인으로 $2M ARR 달성한 AI" in prompt
                    assert "why_it_matters: AI 창업의 차별점" in prompt
                    return (
                        "AI가 만드는 '1인 군단', 연 매출 27억의 비밀\n\n"
                        "AI는 더 이상 단순한 보조 도구가 아닙니다. 핵심은 AI에게 회사의 모든 맥락을 제공하는 것입니다.\n\n"
                        "- Answer This는 단 2명의 팀으로 연 200만 달러 ARR을 만들었습니다.\n"
                        "- Skyvern 사례는 내부 데이터 접근 권한이 에이전트 품질을 좌우한다는 점을 보여줍니다.\n\n"
                        "> \"에이전트가 저품질 결과물을 만드는 이유는 컨텍스트가 없기 때문입니다.\" -- Sachin\n\n"
                        "📎 관련 아티클\n\n"
                        "- **[2인으로 $2M ARR 달성한 AI](https://yc.example/answer-this)** via YC Root Access\n"
                        "  두 명의 창업자가 AI 에이전트로 연 200만 달러 ARR을 달성한 전략.\n"
                        "- **[AI 에이전트 성공의 핵심: 컨텍스트](https://yc.example/context)** via YC Root Access\n"
                        "  AI 에이전트 성능을 끌어올리는 비즈니스 컨텍스트 운영 방식."
                    )
                assert "Executive Summary Prompt" in prompt
                assert "topic_count: 1" in prompt
                assert "item_count: 2" in prompt
                return (
                    "📌 오늘의 핵심\n\n"
                    "AI가 단순한 보조 도구를 넘어 비즈니스 전반을 주도하는 시대가 도래했습니다. "
                    "\"에이전트가 저품질 결과물을 만드는 이유는 컨텍스트가 없기 때문입니다\"라는 통찰을 중심으로, "
                    "총 1가지 주제와 2건의 아이템을 전해드립니다."
                )

        original = get_default_runner()
        runner = BriefingRunner()
        set_default_runner(runner)  # type: ignore[arg-type]
        try:
            result = await run_digest(vault)
        finally:
            set_default_runner(original)

        assert result["ok"] is True
        content = _digest_content(vault, int(result["digest_id"]))
        assert content.startswith("📌 오늘의 핵심\n\n")
        assert "# Digest" not in content
        assert "## Executive summary" not in content
        assert (
            "🎯 현재 AI 시대에 어떤 창업을 해야할지. 비즈니스적 관점에서 제시 "
            "(focus: AI 창업 전략과 비즈니스 관점) (관심 주제) (2건)"
        ) in content
        assert "AI가 만드는 '1인 군단', 연 매출 27억의 비밀" in content
        assert "📎 관련 아티클" in content
        assert "[2인으로 $2M ARR 달성한 AI](https://yc.example/answer-this)" in content
        assert len(runner.calls) == 2


class TestNonZeroExitOnLLMFailure:
    """R-U3.2 / R-B6.3 / INV-3 — LLM failure ⇒ exit non-zero AND zero DB mutation."""

    async def test_llm_failure_leaves_db_pristine(self, vault):
        """No digests row, no digest_id stamps, no file written."""
        ids = [1, 2]
        _seed_lensed_items(vault, item_lens_pairs=[(1, "ai"), (2, "rust")])

        original = get_default_runner()
        failing = CountingRunner(fail_on_call=1)  # fail on first call
        set_default_runner(failing)  # type: ignore[arg-type]
        try:
            result = await run_digest(vault)
        finally:
            set_default_runner(original)

        # Uniform failure payload (R-U2.3).
        assert result == {
            "ok": False,
            "digest_id": None,
            "path": None,
            "item_count": 0,
        }
        # INV-3 / R-B6.3 — ZERO digest rows committed.
        assert _digest_row_count(vault) == 0
        # INV-2 contrapositive — digest_id NEVER stamped on failure.
        stamped = _digest_ids(vault, ids)
        assert all(d is None for d in stamped.values()), stamped
        # No file written (digests/ may not even exist).
        digests_dir = vault.digests_path
        if digests_dir.exists():
            assert list(digests_dir.glob("*.md")) == []

    async def test_llm_failure_on_executive_pass_also_rolls_back(self, vault):
        """Failure on the executive (final) call still rolls back fully."""
        ids = [1, 2]
        _seed_lensed_items(vault, item_lens_pairs=[(1, "ai"), (2, "rust")])

        original = get_default_runner()
        # 2 group calls succeed; 3rd (executive) fails.
        failing = CountingRunner(fail_on_call=3)
        set_default_runner(failing)  # type: ignore[arg-type]
        try:
            result = await run_digest(vault)
        finally:
            set_default_runner(original)

        assert result["ok"] is False
        assert _digest_row_count(vault) == 0
        stamped = _digest_ids(vault, ids)
        assert all(d is None for d in stamped.values()), stamped


class TestTimeoutFallback:
    async def test_group_and_executive_timeout_fall_back_to_markdown(
        self, vault
    ):
        _seed_lensed_items(vault, item_lens_pairs=[(1, "ai"), (2, "ai")])

        class TimeoutRunner:
            def __init__(self):
                self.calls = []

            async def run(self, prompt, *, max_turns=30, timeout=600.0):
                self.calls.append(
                    {"prompt": prompt, "max_turns": max_turns, "timeout": timeout}
                )
                raise TimeoutError("too slow")

        original = get_default_runner()
        runner = TimeoutRunner()
        set_default_runner(runner)  # type: ignore[arg-type]
        try:
            result = await run_digest(vault)
        finally:
            set_default_runner(original)

        assert result["ok"] is True
        assert result["item_count"] == 2
        assert len(runner.calls) == 2
        assert all(call["timeout"] == 60 for call in runner.calls)
        content = _digest_content(vault, int(result["digest_id"]))
        assert "제한 시간" in content
        assert "📎 관련 아티클" in content


# ---------------------------------------------------------------------------
# R-T16.1 / R-T10.1 / INV-8 — do-not-touch + no-scheduling regression.
# ---------------------------------------------------------------------------


# Repository root resolved relative to this test file (tests/test_digest.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src" / "contents_hub"


class TestDoNotTouchRegression:
    """R-T16.1 — non-web do-not-touch files have NO digest references.

    We assert by string scan rather than git-blob comparison because the
    test must not invoke git (INV-1). A negative search for the token
    ``digest`` is strict enough for the non-web runtime surfaces. The web
    dashboard is now the intended digest display surface.
    """

    DO_NOT_TOUCH = [
        _SRC / "api.py",
        _SRC / "daemon.py",
        _SRC / "executor.py",
        _SRC / "filter.py",
        _SRC / "lenses.py",
        _SRC / "promote.py",
    ]

    def test_listed_files_contain_no_digest_references(self):
        offenders: list[tuple[Path, list[str]]] = []
        for path in self.DO_NOT_TOUCH:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            hits = [
                line
                for line in text.splitlines()
                # ``hexdigest`` (used in promote.py for short hashes) is
                # NOT a digest-feature reference — strip it.
                if "digest" in line.lower()
                and "hexdigest" not in line.lower()
            ]
            if hits:
                offenders.append((path, hits))
        assert not offenders, (
            "R-T16.1 violation — do-not-touch files contain digest refs: "
            f"{offenders}"
        )

    def test_pyproject_dependencies_unchanged(self):
        """INV-8 / R-T13.1 — zero new pip deps for the digest feature."""
        text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        # Expected deps frozen at v0.2; any addition for the digest
        # feature would violate INV-8. The set below is the pre-feature
        # canonical baseline.
        expected = {
            "httpx",
            "pyyaml",
            "python-dateutil",
            "youtube-transcript-api",
            "fastapi",
            "uvicorn",
            "jinja2",
            "python-multipart",
        }
        # Crude but robust: each expected dep name must appear in the
        # dependencies block; no NEW names allowed.
        for name in expected:
            assert name in text, f"missing expected dep: {name}"
        # Scan the [project] dependencies list for unknown package names.
        import re
        deps_block_match = re.search(
            r"dependencies\s*=\s*\[(?P<body>.*?)\]",
            text,
            re.DOTALL,
        )
        assert deps_block_match, "pyproject.toml dependencies block missing"
        body = deps_block_match.group("body")
        # Extract dep names — each entry is a quoted string like
        # '"anthropic>=0.39"'.  Pull the leading identifier before any
        # version specifier / extras marker.
        names: set[str] = set()
        for raw in re.findall(r'"([^"]+)"', body):
            head = re.split(r"[<>=!~\[\s]", raw, maxsplit=1)[0].strip()
            if head:
                names.add(head)
        assert names == expected, (
            f"INV-8 violation: unexpected dep set {names} (expected {expected})"
        )
        optional = re.search(
            r"claude\s*=\s*\[(?P<body>.*?)\]",
            text,
            re.DOTALL,
        )
        assert optional, "claude optional dependency block missing"
        optional_names = {
            re.split(r"[<>=!~\[\s]", raw, maxsplit=1)[0].strip()
            for raw in re.findall(r'"([^"]+)"', optional.group("body"))
        }
        assert optional_names == {"anthropic", "claude-agent-sdk"}


class TestNoSchedulingCodeIntroduced:
    """R-T10.1 / INV-8 — no in-process loop/timer for digest scheduling.

    The digest module and the CLI handler must NOT import or invoke
    ``schedule``, ``APScheduler``, ``asyncio.sleep`` (except the single
    100 ms DB-lock retry per R-T11.1), or a daemon-loop construct.
    """

    def test_digest_py_no_scheduling_constructs(self):
        text = (_SRC / "digest.py").read_text(encoding="utf-8")
        # Forbidden libraries.
        assert "import schedule" not in text
        assert "from schedule " not in text
        assert "apscheduler" not in text.lower()
        # The only legitimate asyncio.sleep call is the single 100 ms
        # DB-lock retry (R-T11.1). Count occurrences — exactly 1 allowed.
        sleep_count = text.count("asyncio.sleep")
        assert sleep_count <= 1, (
            f"digest.py contains {sleep_count} asyncio.sleep calls; "
            "R-T10.1 allows at most 1 (for R-T11.1 DB-lock retry)."
        )
        # No while-True / cron-style loop.
        assert "while True" not in text

    def test_cli_handle_digest_no_scheduling_constructs(self):
        text = (_SRC / "cli.py").read_text(encoding="utf-8")
        # _handle_digest body lives between its def and the next def.
        import re
        match = re.search(
            r"def _handle_digest\(.*?\n(?P<body>(?:    .*\n|\n)+)",
            text,
        )
        assert match, "_handle_digest not found in cli.py"
        body = match.group("body")
        assert "asyncio.sleep" not in body
        assert "schedule" not in body
        assert "while True" not in body


# ---------------------------------------------------------------------------
# Additional invariants — INV-1 candidate definition, DB-only digest output.
# ---------------------------------------------------------------------------


class TestCandidateDefinitionINV1:
    """INV-1 / R-B3.* — candidate filter excludes status!='raw' and stamped."""

    async def test_only_raw_lensed_unstamped_items_are_candidates(
        self, vault, stub_runner
    ):
        now = _now()
        with get_db(vault) as conn:
            conn.execute(
                "INSERT INTO subscriptions (id,url,title,created_at,updated_at)"
                " VALUES (?,?,?,?,?)",
                (1, "https://example.com/", "Sub", now, now),
            )
            conn.execute(
                "INSERT INTO lenses (id,name,created_at,updated_at)"
                " VALUES (?,?,?,?)",
                ("ai", "AI", now, now),
            )
            conn.executemany(
                "INSERT INTO raw_items"
                " (id,url,title,body,status,subscription_id,content_summary,"
                "collected_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    # eligible: raw + lens + digest_id NULL
                    (1, "u1", "ok", "b", "raw", 1, "", now, now),
                    # excluded by status filter (R-B3.1)
                    (2, "u2", "promoted", "b", "promoted", 1, "", now, now),
                    (3, "u3", "archived", "b", "archived", 1, "", now, now),
                    # excluded by digest — no raw_item_lenses row
                    (4, "u4", "nolens", "b", "raw", 1, "", now, now),
                    # excluded by R-B3.3 — already stamped
                    (5, "u5", "stamped", "b", "raw", 1, "", now, now),
                ],
            )
            conn.executemany(
                "INSERT INTO raw_item_lenses"
                " (raw_item_id,lens_id,summary,bullets_json) VALUES (?,?,?,?)",
                [
                    (1, "ai", "s1", "[]"),
                    (2, "ai", "s2", "[]"),
                    (3, "ai", "s3", "[]"),
                    (5, "ai", "s5", "[]"),
                ],
            )
            # Pre-stamp item 5 with a fake digest_id so R-B3.3 applies.
            conn.execute(
                "INSERT INTO digests (created_at,item_count) VALUES (?,?)",
                (now, 0),
            )
            existing_digest_id = conn.execute(
                "SELECT last_insert_rowid() AS i"
            ).fetchone()["i"]
            conn.execute(
                "UPDATE raw_items SET digest_id = ? WHERE id = 5",
                (existing_digest_id,),
            )

        result = await run_digest(vault)
        assert result["ok"] is True
        # Only item 1 is lens-matched and unstamped.
        assert result["item_count"] == 1
        stamped = _digest_ids(vault, [1, 2, 3, 4, 5])
        assert stamped[1] == result["digest_id"]
        assert stamped[2] is None
        assert stamped[3] is None
        assert stamped[4] is None
        # Item 5 still carries its original (fake) stamp — NOT the new one.
        assert stamped[5] == existing_digest_id
        assert stamped[5] != result["digest_id"]


class TestDatabaseOnlyDigestOutput:
    """Digest runs persist DB rows and do not write markdown files."""

    async def test_run_digest_stores_structured_rows_without_markdown_file(
        self, vault, stub_runner
    ):
        _seed_lensed_items(
            vault,
            item_lens_pairs=[(1, "ai"), (2, "ai"), (3, "rust")],
        )
        result = await run_digest(vault)
        assert result["ok"] is True
        assert result["path"] is None

        row = _digest_row(vault, int(result["digest_id"]))
        assert row["output_path"] == ""
        assert row["title"]
        assert row["item_count"] == 3

        section_items = _digest_section_item_rows(vault, int(result["digest_id"]))
        assert [
            (r["section_index"], r["lens_id"], r["raw_item_id"], r["sort_order"])
            for r in section_items
        ] == [
            (0, "ai", 1, 0),
            (0, "ai", 2, 1),
            (1, "rust", 3, 0),
        ]

        if vault.digests_path.exists():
            assert list(vault.digests_path.glob("*.md")) == []


# ---------------------------------------------------------------------------
# R-B1.1 — single-user scope: no multi-user routing / timezone branches.
# ---------------------------------------------------------------------------


class TestSingleUserScopeRB11:
    """R-B1.1 — single local vault, no multi-user routing or per-user TZ."""

    async def test_run_digest_signature_takes_only_config(self, vault, stub_runner):
        """run_digest accepts (config, *, force=False) — NO user_id, NO tz."""
        import inspect
        sig = inspect.signature(run_digest)
        params = sig.parameters
        # ``config`` is the sole positional/keyword parameter; ``force`` is
        # the only kwarg (reserved). NO user_id / tenant_id / timezone.
        assert "config" in params
        # Whitelist of allowed kwargs.
        for name in params:
            assert name in {"config", "force"}, (
                f"R-B1.1 violation: unexpected run_digest param {name!r}"
            )

    async def test_no_user_or_timezone_routing_in_digest_module(self):
        """digest.py contains no per-user / tenant / non-UTC TZ branches."""
        text = (_SRC / "digest.py").read_text(encoding="utf-8")
        # No multi-user routing tokens.
        for token in ("user_id", "tenant_id", "owner_id"):
            assert token not in text, (
                f"R-B1.1 violation: digest.py references {token!r}"
            )
        # The only timezone used must be UTC (R-U4.2). Any other tz keyword
        # like 'pytz', 'zoneinfo' indicates per-user TZ logic.
        assert "pytz" not in text
        assert "zoneinfo" not in text
        # datetime.now(timezone.utc) is the canonical clock — must appear.
        assert "timezone.utc" in text
