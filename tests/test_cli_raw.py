from __future__ import annotations

import json
import pytest

from contents_hub.cli import build_parser, main
from contents_hub.config import WikiConfig
from contents_hub.db import get_db, init_db
from contents_hub.lens_inbox import query_lens_inbox


def _read_json(capsys):
    out = capsys.readouterr().out.strip()
    return json.loads(out)


def test_raw_add_url_inserts_manual_item_and_dedupes_tracking_params(monkeypatch, tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()
    monkeypatch.setattr(
        "contents_hub.raw_items.enrich_url",
        lambda url: (
            {
                "title": "Fetched title",
                "body": "Fetched article body",
                "content_summary": "Fetched summary",
            },
            [],
            "static",
        ),
    )

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://Example.com/post/?utm_source=newsletter&x=1#frag",
        "--title",
        "Manual read",
        "--summary",
        "Worth reading later",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["inserted"] is True
    assert payload["item"]["url"] == "https://example.com/post?x=1"
    assert payload["item"]["origin"] == "manual"
    assert payload["item"]["priority"] == 100

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/post?x=1&utm_campaign=again",
    ])
    duplicate = _read_json(capsys)

    assert rc == 0
    assert duplicate["ok"] is True
    assert duplicate["inserted"] is False
    assert duplicate["item"]["id"] == payload["item"]["id"]

    with get_db(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM raw_items").fetchone()
        assert row["title"] == "Manual read"
        assert row["body"] == "Fetched article body"
        assert row["content_summary"] == "Worth reading later"
        assert row["subscription_id"] is None


def test_raw_add_without_lens_attaches_only_keyword_matching_lenses(monkeypatch, tmp_path, capsys):
    """Manual adds route to Lenses whose keywords match — never to every
    enabled Lens (that cross-pollutes per-lens digests and delivery groups)."""
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()
    with get_db(cfg) as conn:
        conn.execute(
            """INSERT INTO lenses
               (id, name, description, keywords, enabled, created_at, updated_at)
               VALUES ('ai-trends', 'AI Trends', '', '["agent", "llm"]', 1, 'now', 'now')"""
        )
        conn.execute(
            """INSERT INTO lenses
               (id, name, description, keywords, enabled, created_at, updated_at)
               VALUES ('climbing', 'Climbing', '', '["hangboard", "crimp"]', 1, 'now', 'now')"""
        )
        conn.commit()
    monkeypatch.setattr(
        "contents_hub.raw_items.enrich_url",
        lambda url: (
            {"title": "Agent digest", "body": "New LLM agent workflow ideas", "content_summary": "S"},
            [],
            "static",
        ),
    )

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/next-digest",
        "--summary",
        "Digest me tomorrow",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    # Enrichment happened before routing, and only the matching lens attached.
    assert payload["lens_ids"] == ["ai-trends"]
    with get_db(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lenses WHERE id = 'manual-inbox'").fetchone()[0] == 0
        attached = conn.execute(
            "SELECT lens_id, summary FROM raw_item_lenses WHERE raw_item_id = ? ORDER BY lens_id",
            (payload["item"]["id"],),
        ).fetchall()
        assert [dict(row) for row in attached] == [
            {"lens_id": "ai-trends", "summary": "Digest me tomorrow"},
        ]
        view = query_lens_inbox(
            conn,
            sources_dirname="Sources",
            status="raw",
            digest_id_null=True,
        )
        assert view["candidate_count"] == 1
        assert view["candidates"][0].id == payload["item"]["id"]


def test_raw_add_without_matching_lens_falls_back_to_manual_inbox(tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()
    with get_db(cfg) as conn:
        conn.execute(
            """INSERT INTO lenses
               (id, name, description, keywords, enabled, created_at, updated_at)
               VALUES ('ai-trends', 'AI Trends', '', '["agent", "llm"]', 1, 'now', 'now')"""
        )
        conn.commit()

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "Hangboard training note: half-crimp repeaters",
        "--title",
        "Hangboard note",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["lens_ids"] == ["manual-inbox"]


def test_raw_add_without_any_lens_creates_manual_inbox(tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "Manual first launch note",
        "--title",
        "First launch",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["lens_ids"] == ["manual-inbox"]
    with get_db(cfg) as conn:
        lens = conn.execute("SELECT name, enabled FROM lenses WHERE id = 'manual-inbox'").fetchone()
        assert dict(lens) == {"name": "Manual Inbox", "enabled": 1}
        attached = conn.execute(
            "SELECT lens_id FROM raw_item_lenses WHERE raw_item_id = ?",
            (payload["item"]["id"],),
        ).fetchall()
        assert [row["lens_id"] for row in attached] == ["manual-inbox"]


def test_raw_add_text_synthesizes_stable_content_key(tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    args = [
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "Claude Code 메모: agentic workflow 실험하기",
        "--title",
        "실험 메모",
    ]
    rc = main(args)
    payload = _read_json(capsys)
    assert rc == 0
    assert payload["inserted"] is True
    assert payload["item"]["url"].startswith("content://manual/")
    assert payload["item"]["title"] == "실험 메모"
    assert payload["item"]["body"] == "Claude Code 메모: agentic workflow 실험하기"

    rc = main(args)
    duplicate = _read_json(capsys)
    assert rc == 0
    assert duplicate["inserted"] is False
    assert duplicate["item"]["id"] == payload["item"]["id"]

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "Claude Code 메모: agentic workflow 실험하기",
        "--title",
        "다른 제목",
    ])
    retitled_duplicate = _read_json(capsys)
    assert rc == 0
    assert retitled_duplicate["inserted"] is False
    assert retitled_duplicate["item"]["id"] == payload["item"]["id"]

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "Claude Code 메모: agentic workflow 실험하기",
        "--published-at",
        "2026-01-01T00:00:00+00:00",
    ])
    dated_duplicate = _read_json(capsys)
    assert rc == 0
    assert dated_duplicate["inserted"] is False
    assert dated_duplicate["item"]["id"] == payload["item"]["id"]



def test_raw_add_attaches_existing_lens(monkeypatch, tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()
    monkeypatch.setattr(
        "contents_hub.raw_items.enrich_url",
        lambda url: ({"title": "T", "body": "Body", "content_summary": "S"}, [], "static"),
    )
    main([
        "--vault",
        str(tmp_path),
        "lens",
        "create",
        "manual-inbox",
        "--name",
        "Manual Inbox",
    ])
    _read_json(capsys)

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/lensed",
        "--lens-id",
        "manual-inbox",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["lens_ids"] == ["manual-inbox"]
    with get_db(cfg) as conn:
        rows = conn.execute(
            "SELECT lens_id FROM raw_item_lenses WHERE raw_item_id = ?",
            (payload["item"]["id"],),
        ).fetchall()
    assert [row["lens_id"] for row in rows] == ["manual-inbox"]



def test_raw_add_rejects_missing_lens(tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/missing-lens",
        "--lens-id",
        "missing-lens",
    ])
    payload = _read_json(capsys)

    assert rc == 1
    assert payload["ok"] is False
    assert "lens not found: missing-lens" in payload["error"]
    with get_db(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 0



def test_raw_add_rejects_invalid_metadata_json(tmp_path, capsys):
    init_db(WikiConfig(vault_path=tmp_path)).close()

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/bad-meta",
        "--metadata-json",
        "[]",
    ])
    payload = _read_json(capsys)

    assert rc == 1
    assert payload["ok"] is False
    assert "metadata-json" in payload["error"]



def test_raw_add_default_static_fetch_populates_body(monkeypatch, tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    monkeypatch.setattr(
        "contents_hub.raw_items.enrich_url_static",
        lambda url: {
            "title": "Static title",
            "body": "Static body text",
            "content_summary": "Static summary",
        },
    )

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/static-body",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["inserted"] is True
    assert payload["warnings"] == []
    assert payload["item"]["title"] == "Static title"
    assert payload["item"]["body"] == "Static body text"
    assert payload["item"]["content_summary"] == "Static summary"
    assert payload["item"]["metadata"]["fetch_page"] == {"ok": True, "mode": "static"}


def test_raw_add_existing_empty_body_is_refetched(monkeypatch, tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    monkeypatch.setattr(
        "contents_hub.raw_items.enrich_url",
        lambda url: ({}, ["static fetch failed: down", "browser fetch failed: down"], "none"),
    )
    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/refetch-empty",
    ])
    first = _read_json(capsys)
    assert rc == 0
    assert first["item"]["body"] == ""

    monkeypatch.setattr(
        "contents_hub.raw_items.enrich_url",
        lambda url: (
            {"title": "Recovered", "body": "Recovered body", "content_summary": "Recovered summary"},
            [],
            "browser",
        ),
    )
    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/refetch-empty",
    ])
    second = _read_json(capsys)

    assert rc == 0
    assert second["inserted"] is False
    assert second["item"]["id"] == first["item"]["id"]
    assert second["item"]["title"] == "Recovered"
    assert second["item"]["body"] == "Recovered body"
    assert second["item"]["metadata"]["fetch_page"] == {"ok": True, "mode": "browser"}


def test_raw_add_static_failure_falls_back_to_browser(monkeypatch, tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    def static_boom(url: str):
        raise RuntimeError("static down")

    monkeypatch.setattr("contents_hub.raw_items.enrich_url_static", static_boom)
    monkeypatch.setattr(
        "contents_hub.raw_items.enrich_url_browser",
        lambda url: {
            "title": "Browser title",
            "body": "Browser-rendered body",
            "content_summary": "Browser summary",
        },
    )

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/browser-fallback",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["inserted"] is True
    assert payload["warnings"] == ["static fetch failed: static down"]
    assert payload["item"]["title"] == "Browser title"
    assert payload["item"]["body"] == "Browser-rendered body"
    assert payload["item"]["metadata"]["fetch_page"] == {"ok": True, "mode": "browser"}


def test_raw_add_fetch_failures_still_insert(monkeypatch, tmp_path, capsys):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    def static_boom(url: str):
        raise RuntimeError("static down")

    def browser_boom(url: str):
        raise RuntimeError("browser down")

    monkeypatch.setattr("contents_hub.raw_items.enrich_url_static", static_boom)
    monkeypatch.setattr("contents_hub.raw_items.enrich_url_browser", browser_boom)

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "https://example.com/fallback",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["inserted"] is True
    assert payload["warnings"] == [
        "static fetch failed: static down",
        "browser fetch failed: browser down",
    ]
    assert payload["item"]["url"] == "https://example.com/fallback"
    assert payload["item"]["title"] == "https://example.com/fallback"
    assert payload["item"]["body"] == ""


def test_raw_add_help_hides_fetch_page_option(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["raw", "add", "--help"])

    help_text = capsys.readouterr().out
    assert exc.value.code == 0
    assert "--fetch-page" not in help_text
    assert "HTTP(S) URL or free-form text to add" in help_text


def test_raw_add_hidden_fetch_page_flag_remains_backward_compatible(tmp_path, capsys):
    init_db(WikiConfig(vault_path=tmp_path)).close()

    rc = main([
        "--vault",
        str(tmp_path),
        "raw",
        "add",
        "plain text note",
        "--fetch-page",
    ])
    payload = _read_json(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["warnings"] == []
    assert payload["item"]["body"] == "plain text note"
