"""Engagement signal ledger + knowledge-store query coverage.

Covers: schema v15 dedup semantics, backfill, interaction dual-writes,
Slack reaction-name aliases, delivery-record impressions, decay math,
FTS/LIKE/vector ranking fusion, filters, CLI JSON shapes, and the web
open-redirect signal capture.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from contents_hub.cli import main
from contents_hub.config import WikiConfig
from contents_hub.db import get_db, init_db
from contents_hub.interactions import (
    handle_interaction,
    record_outbound_message,
    resolve_reaction_action,
)
from contents_hub.signals import (
    decayed_weight,
    engagement_scores,
    open_item,
    record_signal,
    signals_payload,
)
from contents_hub.timeutil import now_iso


@pytest.fixture
def cfg(tmp_path):
    config = WikiConfig(vault_path=tmp_path)
    (tmp_path / ".contents-hub").mkdir(parents=True, exist_ok=True)
    init_db(config).close()
    return config


def _insert_item(
    config,
    *,
    url="https://example.com/a",
    title="Vector databases in production",
    body="A deep dive into vector database benchmarks and tradeoffs.",
    status="raw",
    collected_at=None,
) -> int:
    now = collected_at or now_iso()
    with get_db(config) as conn:
        cur = conn.execute(
            "INSERT INTO raw_items (url, title, body, origin, priority, status, "
            "content_summary, collected_at, updated_at) "
            "VALUES (?, ?, ?, 'manual', 100, ?, ?, ?, ?)",
            (url, title, body, status, body[:100], now, now),
        )
        return int(cur.lastrowid)


def _read_json(capsys):
    return json.loads(capsys.readouterr().out.strip())


# ---------------------------------------------------------------------------
# Signal ledger
# ---------------------------------------------------------------------------


def test_record_signal_dedup_is_replay_safe(cfg):
    item_id = _insert_item(cfg)
    first = record_signal(cfg, id_or_url=str(item_id), signal="save", dedup_key="k1")
    replay = record_signal(cfg, id_or_url=str(item_id), signal="save", dedup_key="k1")
    assert first["inserted"] is True
    assert replay["inserted"] is False and replay["deduped"] is True

    payload = signals_payload(cfg, str(item_id))
    assert payload["signals"] == {"save": 1}


def test_record_signal_by_url_and_unknown_item(cfg):
    item_id = _insert_item(cfg, url="https://example.com/by-url")
    ok = record_signal(cfg, id_or_url="https://example.com/by-url", signal="read")
    assert ok["ok"] is True and ok["raw_item_id"] == item_id

    missing = record_signal(cfg, id_or_url="999999", signal="read")
    assert missing["ok"] is False and missing["error"] == "raw_item_not_found"


def test_invalid_signal_rejected(cfg):
    item_id = _insert_item(cfg)
    with pytest.raises(ValueError):
        record_signal(cfg, id_or_url=str(item_id), signal="banana")


def test_open_item_dedups_same_day_and_reports_url(cfg):
    item_id = _insert_item(cfg)
    first = open_item(cfg, str(item_id), launch=False)
    second = open_item(cfg, str(item_id), launch=False)
    assert first["ok"] and first["inserted"] is True
    assert second["inserted"] is False
    assert first["url"] == "https://example.com/a"
    assert signals_payload(cfg, str(item_id))["signals"] == {"open": 1}


def test_decay_halves_at_half_life():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=90)).isoformat()
    assert decayed_weight(3.0, old, now=now, half_life_days=90.0) == pytest.approx(1.5)
    assert decayed_weight(3.0, now.isoformat(), now=now, half_life_days=90.0) == pytest.approx(3.0, abs=0.01)


def test_engagement_score_combines_weights_and_floor(cfg):
    item_id = _insert_item(cfg)
    record_signal(cfg, id_or_url=str(item_id), signal="save", dedup_key="s")
    record_signal(cfg, id_or_url=str(item_id), signal="open", dedup_key="o")
    with get_db(cfg) as conn:
        score = engagement_scores(cfg, conn, [item_id])[item_id]
    assert score == pytest.approx(4.0, abs=0.05)  # save 3 + open 1, fresh

    negative = _insert_item(cfg, url="https://example.com/neg")
    for i in range(4):
        record_signal(cfg, id_or_url=str(negative), signal="archive", dedup_key=f"a{i}")
    with get_db(cfg) as conn:
        floored = engagement_scores(cfg, conn, [negative])[negative]
    assert floored == pytest.approx(-3.0)  # floor, not -8


# ---------------------------------------------------------------------------
# Interaction dual-write + platform parity
# ---------------------------------------------------------------------------


def _deliver_and_react(cfg, item_id, *, platform, value, event_id="ev-1"):
    record_outbound_message(
        cfg,
        platform=platform,
        message_id="m-1",
        channel_id="c-1",
        payload_type="raw_item",
        raw_item_id=item_id,
    )
    return handle_interaction(
        cfg,
        {
            "platform": platform,
            "event_id": event_id,
            "channel_id": "c-1",
            "message_id": "m-1",
            "kind": "reaction",
            "value": value,
        },
    )


def test_star_reaction_writes_save_and_promote_signals(cfg):
    item_id = _insert_item(cfg)
    result = _deliver_and_react(cfg, item_id, platform="telegram", value="⭐")
    assert result["ok"] is True and result["promoted"] is True

    signals = signals_payload(cfg, str(item_id))["signals"]
    assert signals["impression"] == 1  # from delivery record
    assert signals["save"] == 1
    assert signals["promote"] == 1

    # Replay of the same event must not add signal rows.
    replay = handle_interaction(
        cfg,
        {
            "platform": "telegram",
            "event_id": "ev-1",
            "channel_id": "c-1",
            "message_id": "m-1",
            "kind": "reaction",
            "value": "⭐",
        },
    )
    assert replay["status"] == "already_handled"
    assert signals_payload(cfg, str(item_id))["signals"]["save"] == 1


def test_slack_reaction_names_map_to_actions(cfg):
    assert resolve_reaction_action("star") == "save_and_promote"
    assert resolve_reaction_action(":star:") == "save_and_promote"
    assert resolve_reaction_action("+1") == "save_and_promote"
    assert resolve_reaction_action("white_check_mark") == "mark_read"
    assert resolve_reaction_action("wastebasket") == "archive"
    assert resolve_reaction_action("🗑️") == "archive"  # variation selector
    assert resolve_reaction_action("eyes") == ""

    item_id = _insert_item(cfg)
    result = _deliver_and_react(cfg, item_id, platform="slack", value="wastebasket")
    assert result["ok"] is True and result["action"] == "archive"
    assert signals_payload(cfg, str(item_id))["signals"]["archive"] == 1


def test_delivery_record_impression_is_idempotent(cfg):
    item_id = _insert_item(cfg)
    for _ in range(2):
        record_outbound_message(
            cfg,
            platform="slack",
            message_id="m-9",
            channel_id="c-9",
            payload_type="raw_item",
            raw_item_id=item_id,
        )
    assert signals_payload(cfg, str(item_id))["signals"] == {"impression": 1}


def test_backfill_seeds_signals_from_state(cfg):
    from contents_hub.db import _backfill_item_signals

    saved = _insert_item(cfg, url="https://example.com/saved")
    promoted = _insert_item(cfg, url="https://example.com/promoted", status="promoted")
    archived = _insert_item(cfg, url="https://example.com/archived", status="archived")
    with get_db(cfg) as conn:
        conn.execute(
            "INSERT INTO saved_items (raw_item_id, saved_at) VALUES (?, ?)",
            (saved, now_iso()),
        )
        _backfill_item_signals(conn)
        _backfill_item_signals(conn)  # re-run must be a no-op

    assert signals_payload(cfg, str(saved))["signals"] == {"save": 1}
    assert signals_payload(cfg, str(promoted))["signals"] == {"promote": 1}
    assert signals_payload(cfg, str(archived))["signals"] == {"archive": 1}


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def test_query_finds_keyword_matches_with_snippet_and_scores(cfg):
    item_id = _insert_item(cfg)
    _insert_item(cfg, url="https://example.com/other", title="Cooking pasta", body="Boil water.")

    from contents_hub.query import query_items

    result = query_items(cfg, "vector database")
    assert result["ok"] is True
    assert "fts" in result["rankers"]
    assert result["count"] == 1
    hit = result["items"][0]
    assert hit["raw_item_id"] == item_id
    assert hit["scores"]["final"] > 0
    assert hit["scores"]["keyword_rank"] == 1


def test_query_engagement_boosts_saved_item_above_unsaved(cfg):
    plain = _insert_item(
        cfg, url="https://example.com/p", title="Vector search intro", body="vector search basics"
    )
    loved = _insert_item(
        cfg, url="https://example.com/l", title="Vector search guide", body="vector search basics"
    )
    record_signal(cfg, id_or_url=str(loved), signal="save", dedup_key="s")
    record_signal(cfg, id_or_url=str(loved), signal="open", dedup_key="o")

    from contents_hub.query import query_items

    result = query_items(cfg, "vector search")
    ids = [item["raw_item_id"] for item in result["items"]]
    assert ids.index(loved) < ids.index(plain)


def test_query_filters_since_lens_saved(cfg):
    old = _insert_item(
        cfg,
        url="https://example.com/old",
        title="vector old",
        collected_at=(datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),
    )
    fresh = _insert_item(cfg, url="https://example.com/fresh", title="vector fresh")

    from contents_hub.query import query_items

    windowed = query_items(cfg, "vector", since="30d")
    ids = [item["raw_item_id"] for item in windowed["items"]]
    assert fresh in ids and old not in ids

    with pytest.raises(ValueError):
        query_items(cfg, "vector", since="30x")

    with get_db(cfg) as conn:
        conn.execute(
            "INSERT INTO lenses (id, name, created_at, updated_at) VALUES ('ai', 'AI', ?, ?)",
            (now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO raw_item_lenses (raw_item_id, lens_id) VALUES (?, 'ai')",
            (fresh,),
        )
    lensed = query_items(cfg, "vector", lens="ai")
    assert [item["raw_item_id"] for item in lensed["items"]] == [fresh]
    assert lensed["items"][0]["lens_ids"] == ["ai"]

    record_signal(cfg, id_or_url=str(old), signal="save", dedup_key="s")
    with get_db(cfg) as conn:
        conn.execute(
            "INSERT INTO saved_items (raw_item_id, saved_at) VALUES (?, ?)",
            (old, now_iso()),
        )
    saved = query_items(cfg, "vector", saved_only=True)
    assert [item["raw_item_id"] for item in saved["items"]] == [old]


def test_query_like_fallback_when_fts_unavailable(cfg, monkeypatch):
    item_id = _insert_item(cfg)
    monkeypatch.setattr("contents_hub.query.ensure_fts", lambda conn: False)

    from contents_hub.query import query_items

    result = query_items(cfg, "vector")
    assert result["rankers"] == ["like"]
    assert [item["raw_item_id"] for item in result["items"]] == [item_id]


def test_query_empty_text_and_no_matches(cfg):
    from contents_hub.query import query_items

    assert query_items(cfg, "")["ok"] is False
    _insert_item(cfg)
    result = query_items(cfg, "zzzqqqxxx")
    assert result["ok"] is True and result["count"] == 0


class _FakeEmbedder:
    name = "fake"
    model = "fake-embed-1"

    def embed(self, texts):
        # Deterministic 3-dim vectors: axis 0 = "vector"ness, axis 1 = "cooking"ness
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if "vector" in lowered else 0.0,
                    1.0 if ("cook" in lowered or "pasta" in lowered) else 0.0,
                    0.1,
                ]
            )
        return vectors


def test_index_backfill_and_semantic_ranker(cfg, monkeypatch):
    vector_item = _insert_item(cfg)
    pasta_item = _insert_item(
        cfg, url="https://example.com/pasta", title="Cooking pasta", body="Boil water and cook."
    )
    fake = _FakeEmbedder()
    monkeypatch.setattr("contents_hub.query.get_default_embedder", lambda: fake)

    from contents_hub.query import build_index, query_items

    built = build_index(cfg, backfill_embeddings=True)
    assert built["ok"] is True
    assert built["embedded"] == 2
    # Re-running skips unchanged items via text_hash.
    again = build_index(cfg, backfill_embeddings=True)
    assert again["embedded"] == 0 and again["skipped"] == 2

    result = query_items(cfg, "pasta cooking", explain=True)
    assert "vector" in result["rankers"]
    ids = [item["raw_item_id"] for item in result["items"]]
    assert ids[0] == pasta_item
    assert vector_item not in ids or ids.index(pasta_item) < ids.index(vector_item)
    assert "vector_similarity" in result["items"][0]["scores"]


def test_context_pack_renders_markdown(cfg):
    item_id = _insert_item(cfg)
    record_signal(cfg, id_or_url=str(item_id), signal="save", dedup_key="s")

    from contents_hub.query import context_pack, query_items

    text = context_pack(query_items(cfg, "vector database"))
    assert "# Context: vector database" in text
    assert "https://example.com/a" in text
    assert "save:1" in text


# ---------------------------------------------------------------------------
# CLI JSON shapes
# ---------------------------------------------------------------------------


def test_cli_item_signal_signals_and_query(cfg, tmp_path, capsys):
    item_id = _insert_item(cfg)

    rc = main(
        ["--vault", str(tmp_path), "item", "signal", str(item_id), "--signal", "save", "--dedup-key", "k"]
    )
    payload = _read_json(capsys)
    assert rc == 0 and payload["ok"] is True and payload["inserted"] is True

    rc = main(["--vault", str(tmp_path), "item", "signals", str(item_id)])
    payload = _read_json(capsys)
    assert rc == 0 and payload["signals"] == {"save": 1}
    assert payload["engagement"] > 0

    rc = main(["--vault", str(tmp_path), "item", "open", str(item_id), "--no-launch"])
    payload = _read_json(capsys)
    assert rc == 0 and payload["url"] == "https://example.com/a"

    rc = main(["--vault", str(tmp_path), "query", "vector database", "--limit", "5"])
    payload = _read_json(capsys)
    assert rc == 0 and payload["count"] == 1
    assert payload["items"][0]["raw_item_id"] == item_id
    assert payload["items"][0]["signals"] == {"save": 1, "open": 1}

    rc = main(["--vault", str(tmp_path), "query", "vector database", "--context"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("# Context:")

    rc = main(["--vault", str(tmp_path), "index", "--rebuild-fts"])
    payload = _read_json(capsys)
    assert rc == 0 and payload["fts"] is True


def test_cli_index_backfill_without_provider_reports_error(cfg, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CONTENTS_HUB_EMBEDDINGS", raising=False)
    rc = main(["--vault", str(tmp_path), "index", "--backfill"])
    payload = _read_json(capsys)
    assert rc == 1 and payload["ok"] is False
    assert "CONTENTS_HUB_EMBEDDINGS" in payload["error"]


# ---------------------------------------------------------------------------
# Web open-redirect
# ---------------------------------------------------------------------------


def test_web_redirect_records_open_signal(cfg):
    from fastapi.testclient import TestClient

    from contents_hub.web.app import create_app

    item_id = _insert_item(cfg)
    client = TestClient(create_app(cfg), follow_redirects=False)

    resp = client.get(f"/r/{item_id}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/a"
    assert signals_payload(cfg, str(item_id))["signals"] == {"open": 1}

    # Same-day repeat dedups; missing/contentless items 404.
    client.get(f"/r/{item_id}")
    assert signals_payload(cfg, str(item_id))["signals"] == {"open": 1}
    assert client.get("/r/999999").status_code == 404
