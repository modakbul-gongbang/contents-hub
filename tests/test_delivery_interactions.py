from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone

import pytest

from contents_hub.channels import FakeAdapter, normalize_discord_event, normalize_slack_event
from contents_hub.cli import build_parser, main as cli_main
from contents_hub.config import WikiConfig
from contents_hub.db import SCHEMA_VERSION, get_db, init_db
from contents_hub.delivery import pending_delivery_payload, prepare_delivery_payload
from contents_hub.interactions import handle_interaction, record_outbound_message
from contents_hub.promote import promote_raw_item


def _cfg(tmp_path) -> WikiConfig:
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()
    return cfg


def _seed_raw_item(cfg: WikiConfig) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_db(cfg) as conn:
        cur = conn.execute(
            """
            INSERT INTO raw_items
                (url, title, body, origin, priority, status,
                 content_summary, metadata_json, collected_at, updated_at)
            VALUES (?, ?, ?, 'manual', 100, 'raw', ?, '{}', ?, ?)
            """,
            (
                "https://example.com/story",
                "Example Story",
                "Full body",
                "Short summary",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def _seed_subscription(cfg: WikiConfig) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_db(cfg) as conn:
        cur = conn.execute(
            """
            INSERT INTO subscriptions
                (url, title, source_type, status, default_lens_ids,
                 config, created_at, updated_at)
            VALUES (?, 'Feed', 'rss.feed', 'active', '[]', '{}', ?, ?)
            """,
            ("https://example.com/feed.xml", now, now),
        )
        return int(cur.lastrowid)


def _insert_raw_item(
    cfg: WikiConfig,
    *,
    url: str,
    title: str,
    origin: str = "subscription",
    priority: int = 50,
    subscription_id: int | None = None,
    collected_at: str,
) -> int:
    with get_db(cfg) as conn:
        cur = conn.execute(
            """
            INSERT INTO raw_items
                (url, title, body, origin, priority, status, subscription_id,
                 content_summary, metadata_json, collected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'raw', ?, ?, '{}', ?, ?)
            """,
            (
                url,
                title,
                f"Body for {title}",
                origin,
                priority,
                subscription_id,
                f"Summary for {title}",
                collected_at,
                collected_at,
            ),
        )
        return int(cur.lastrowid)


def _attach_lens(cfg: WikiConfig, raw_item_id: int, lens_id: str = "agent-tech") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db(cfg) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO lenses
                (id, name, description, keywords, enabled, created_at, updated_at)
            VALUES (?, ?, '', '[]', 1, ?, ?)
            """,
            (lens_id, lens_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO raw_item_lenses
                (raw_item_id, lens_id, summary, bullets_json)
            VALUES (?, ?, ?, '[]')
            """,
            (raw_item_id, lens_id, f"Lens summary for {lens_id}"),
        )
        conn.commit()


def test_schema_v14_adds_delivery_and_interaction_tables(tmp_path):
    cfg = _cfg(tmp_path)
    with get_db(cfg) as conn:
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert version == SCHEMA_VERSION
    assert {"outbound_messages", "interaction_events"}.issubset(tables)


def test_existing_telegram_mapping_table_is_migrated_to_outbound_messages(tmp_path):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)
    now = datetime.now(timezone.utc).isoformat()
    with get_db(cfg) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_raw_item_messages (
                chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                raw_item_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'send_message',
                PRIMARY KEY(chat_id, message_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO telegram_raw_item_messages
                (chat_id, message_id, thread_id, raw_item_id, created_at)
            VALUES ('chat-1', 'msg-1', 'thread-1', ?, ?)
            """,
            (raw_item_id, now),
        )
        conn.execute("UPDATE schema_version SET version=13")
        conn.commit()

    init_db(cfg).close()

    with get_db(cfg) as conn:
        row = conn.execute(
            """
            SELECT platform, channel_id, thread_id, message_id, raw_item_id
            FROM outbound_messages
            WHERE platform='telegram'
            """
        ).fetchone()
    assert dict(row) == {
        "platform": "telegram",
        "channel_id": "chat-1",
        "thread_id": "thread-1",
        "message_id": "msg-1",
        "raw_item_id": raw_item_id,
    }


def test_pending_delivery_payload_and_fake_adapter_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)
    payload = pending_delivery_payload(cfg, payload_type="raw_item")

    assert payload["ok"] is True
    assert payload["items"][0]["raw_item_id"] == raw_item_id
    assert payload["items"][0]["payload_type"] == "raw_item"

    adapter = FakeAdapter()
    outbound = adapter.send_item(payload["items"][0])
    result = record_outbound_message(
        cfg,
        platform=outbound.platform,
        message_id=outbound.message_id,
        payload_type="raw_item",
        raw_item_id=raw_item_id,
    )
    assert result["ok"] is True

    pending_after = pending_delivery_payload(cfg, payload_type="raw_item")
    assert pending_after["items"] == []


def test_pending_delivery_filters_subscription_lens_and_first_seen(tmp_path):
    cfg = _cfg(tmp_path)
    subscription_id = _seed_subscription(cfg)

    _insert_raw_item(
        cfg,
        url="https://example.com/a",
        title="Older A",
        subscription_id=None,
        collected_at="2026-01-01T00:00:00+00:00",
    )
    newer_duplicate = _insert_raw_item(
        cfg,
        url="https://example.com/a",
        title="Newer A",
        subscription_id=subscription_id,
        collected_at="2026-01-02T00:00:00+00:00",
    )
    no_lens = _insert_raw_item(
        cfg,
        url="https://example.com/b",
        title="No Lens",
        subscription_id=subscription_id,
        collected_at="2026-01-03T00:00:00+00:00",
    )
    manual = _insert_raw_item(
        cfg,
        url="https://example.com/c",
        title="Manual",
        origin="manual",
        priority=100,
        subscription_id=None,
        collected_at="2026-01-04T00:00:00+00:00",
    )
    included = _insert_raw_item(
        cfg,
        url="https://example.com/d",
        title="Included",
        subscription_id=subscription_id,
        collected_at="2026-01-05T00:00:00+00:00",
    )
    _attach_lens(cfg, newer_duplicate)
    _attach_lens(cfg, manual)
    _attach_lens(cfg, included)

    payload = pending_delivery_payload(
        cfg,
        payload_type="raw_item",
        origin="subscription",
        lens_matched=True,
        first_seen_only=True,
        limit=20,
    )

    assert [item["raw_item_id"] for item in payload["items"]] == [included]
    assert no_lens not in [item["raw_item_id"] for item in payload["items"]]
    card = payload["items"][0]
    assert card["delivery_key"] == f"raw_item:{included}"
    assert card["dedupe_key"] == "url:https://example.com/d"
    assert card["plain_text"] == (
        "Included\n\nSummary for Included\n\nhttps://example.com/d"
    )
    assert card["markdown"] == (
        "**Included**\n\nSummary for Included\n\nhttps://example.com/d"
    )
    assert card["lens_ids"] == ["agent-tech"]
    assert card["source_type"] == "rss.feed"


def test_pending_delivery_excludes_already_recorded_raw_items(tmp_path):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)
    record_outbound_message(
        cfg,
        platform="fake",
        message_id="already-sent",
        payload_type="raw_item",
        raw_item_id=raw_item_id,
    )

    payload = pending_delivery_payload(cfg, payload_type="raw_item")

    assert payload["ok"] is True
    assert payload["items"] == []


def test_prepare_delivery_card_round_trip_preserves_reaction_mapping(tmp_path):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)

    payload = asyncio.run(
        prepare_delivery_payload(
            cfg,
            collect="none",
            payload_type="raw_item",
            limit=20,
        )
    )
    card = payload["delivery"]["items"][0]
    adapter = FakeAdapter()
    outbound = adapter.send_item(card)
    record_outbound_message(
        cfg,
        platform=outbound.platform,
        message_id=outbound.message_id,
        payload_type=card["payload_type"],
        raw_item_id=card["raw_item_id"],
    )

    result = handle_interaction(
        cfg,
        {
            "platform": outbound.platform,
            "message_id": outbound.message_id,
            "kind": "reaction",
            "value": "⭐",
        },
    )

    assert result["ok"] is True
    assert result["action"] == "save_and_promote"
    assert result["raw_item_id"] == raw_item_id


def test_reaction_save_promote_is_idempotent_and_logged(tmp_path):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)
    record_outbound_message(
        cfg,
        platform="telegram",
        channel_id="chat-1",
        thread_id="thread-1",
        message_id="msg-1",
        payload_type="raw_item",
        raw_item_id=raw_item_id,
    )

    event = {
        "platform": "telegram",
        "event_id": "evt-1",
        "channel_id": "chat-1",
        "thread_id": "thread-1",
        "message_id": "msg-1",
        "user_id": "user-1",
        "kind": "reaction",
        "value": "👍",
    }
    first = handle_interaction(cfg, event)
    second = handle_interaction(cfg, event)

    assert first["ok"] is True
    assert first["saved"] is True
    assert first["promoted"] is True
    assert first["action"] == "save_and_promote"
    assert second["status"] == "already_handled"

    with get_db(cfg) as conn:
        saved_count = conn.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM interaction_events").fetchone()[0]
        status = conn.execute(
            "SELECT status FROM raw_items WHERE id=?",
            (raw_item_id,),
        ).fetchone()[0]
    assert saved_count == 1
    assert event_count == 1
    assert status == "promoted"
    assert len(list(cfg.sources_path.glob("*.md"))) == 1


def test_reaction_promote_writes_processed_summary_note(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with get_db(cfg) as conn:
        cur = conn.execute(
            """
            INSERT INTO raw_items
                (url, title, body, origin, priority, status,
                 content_summary, metadata_json, collected_at, updated_at)
            VALUES (?, ?, ?, 'subscription', 100, 'raw', ?, '{}', ?, ?)
            """,
            (
                "https://example.com/deep",
                "Deep Item",
                "<main><nav>Page chrome</nav><p>Full captured "
                "<em>content</em> with extra detail.</p>"
                "<div>Second clean point.</div></main>",
                "<p>Short <strong>public</strong> summary.</p>",
                now,
                now,
            ),
        )
        raw_item_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO lenses
                (id, name, description, keywords, enabled, created_at, updated_at)
            VALUES ('agent-tech', 'Agent Tech', '', '[]', 1, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO raw_item_lenses
                (raw_item_id, lens_id, summary, bullets_json)
            VALUES (?, 'agent-tech', ?, ?)
            """,
            (
                raw_item_id,
                "<p>Lens-specific <b>interpretation</b>.</p>",
                json.dumps(
                    ["<span>First implication.</span>", "<li>Second implication.</li>"]
                ),
            ),
        )
        conn.commit()
    record_outbound_message(
        cfg,
        platform="telegram",
        channel_id="chat-1",
        message_id="msg-rich",
        payload_type="raw_item",
        raw_item_id=raw_item_id,
    )

    out = handle_interaction(
        cfg,
        {
            "platform": "telegram",
            "channel_id": "chat-1",
            "message_id": "msg-rich",
            "kind": "reaction",
            "value": "⭐",
        },
    )

    path = cfg.vault_path / out["path"]
    content = path.read_text(encoding="utf-8")
    assert "raw_item_id:" in content
    assert f"raw_item_id: {raw_item_id}" in content
    assert "lenses:" in content
    assert "- agent-tech" in content
    assert "## 한줄 요약\n\nShort public summary." in content
    assert "## 핵심 내용" in content
    assert "- First implication." in content
    assert "- Second implication." in content
    assert "## Lens Notes" in content
    assert "### agent-tech" in content
    assert "Lens-specific interpretation." in content
    assert "## Source" in content
    assert f"- Raw item id: {raw_item_id}" in content
    assert "`raw_items.body`" in content
    assert "## Content" not in content
    assert "Page chrome" not in content
    assert "<p" not in content
    assert "<div" not in content
    assert "<strong" not in content
    assert "<li" not in content


def test_promoted_source_note_does_not_dump_long_body(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    long_body = "A" * 13050
    with get_db(cfg) as conn:
        cur = conn.execute(
            """
            INSERT INTO raw_items
                (url, title, body, origin, priority, status,
                 content_summary, metadata_json, collected_at, updated_at)
            VALUES (?, ?, ?, 'manual', 100, 'raw', ?, '{}', ?, ?)
            """,
            (
                "https://example.com/long",
                "Long Item",
                long_body,
                "Useful summary.",
                now,
                now,
            ),
        )
        raw_item_id = int(cur.lastrowid)
    record_outbound_message(
        cfg,
        platform="telegram",
        channel_id="chat-1",
        message_id="msg-long",
        payload_type="raw_item",
        raw_item_id=raw_item_id,
    )

    out = handle_interaction(
        cfg,
        {
            "platform": "telegram",
            "channel_id": "chat-1",
            "message_id": "msg-long",
            "kind": "reaction",
            "value": "⭐",
        },
    )

    content = (cfg.vault_path / out["path"]).read_text(encoding="utf-8")
    assert "## 한줄 요약\n\nUseful summary." in content
    assert "## 핵심 내용" in content
    assert "## Source" in content
    assert "## Content" not in content
    assert "Content Excerpt" not in content
    assert "A" * 300 not in content
    assert len(content) < 2000


def test_direct_promote_and_reaction_promote_use_same_renderer(tmp_path):
    cfg_direct = _cfg(tmp_path / "direct")
    cfg_reaction = _cfg(tmp_path / "reaction")
    collected_at = "2026-01-02T03:04:05+00:00"

    def seed(cfg: WikiConfig) -> int:
        with get_db(cfg) as conn:
            cur = conn.execute(
                """
                INSERT INTO raw_items
                    (url, title, body, origin, priority, status,
                     content_summary, metadata_json, collected_at, updated_at)
                VALUES (?, ?, ?, 'manual', 100, 'raw', '', '{}', ?, ?)
                """,
                (
                    "https://example.com/same",
                    "Same Item",
                    "Opening paragraph explains the useful idea. "
                    "Second sentence adds context.",
                    collected_at,
                    collected_at,
                ),
            )
            return int(cur.lastrowid)

    direct_id = seed(cfg_direct)
    reaction_id = seed(cfg_reaction)
    direct_path = promote_raw_item(cfg_direct, direct_id)
    record_outbound_message(
        cfg_reaction,
        platform="telegram",
        channel_id="chat-1",
        message_id="msg-same",
        payload_type="raw_item",
        raw_item_id=reaction_id,
    )
    out = handle_interaction(
        cfg_reaction,
        {
            "platform": "telegram",
            "channel_id": "chat-1",
            "message_id": "msg-same",
            "kind": "reaction",
            "value": "⭐",
        },
    )
    reaction_path = cfg_reaction.vault_path / out["path"]

    assert direct_path.read_text(encoding="utf-8") == reaction_path.read_text(
        encoding="utf-8"
    )


def test_unknown_and_unsupported_interactions_are_safe_json(tmp_path):
    cfg = _cfg(tmp_path)
    ignored = handle_interaction(
        cfg,
        {
            "platform": "telegram",
            "event_id": "evt-ignore",
            "channel_id": "chat-1",
            "message_id": "msg-404",
            "kind": "reaction",
            "value": "👀",
        },
    )
    missing = handle_interaction(
        cfg,
        {
            "platform": "telegram",
            "event_id": "evt-missing",
            "channel_id": "chat-1",
            "message_id": "msg-404",
            "kind": "reaction",
            "value": "⭐",
        },
    )

    assert ignored == {
        "ok": True,
        "status": "ignored",
        "noop": True,
        "action": "",
        "interaction_event_id": ignored["interaction_event_id"],
    }
    assert missing["ok"] is False
    assert missing["status"] == "not_found"
    assert missing["error"] == "outbound_message_not_found"


def test_cli_delivery_and_interaction_flow(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)

    rc = cli_main(
        [
            "--vault",
            str(cfg.vault_path),
            "delivery",
            "record",
            "--platform",
            "telegram",
            "--channel-id",
            "chat-1",
            "--thread-id",
            "thread-1",
            "--message-id",
            "msg-1",
            "--raw-item-id",
            str(raw_item_id),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True

    event = {
        "platform": "telegram",
        "event_id": "evt-cli",
        "channel_id": "chat-1",
        "thread_id": "thread-1",
        "message_id": "msg-1",
        "kind": "reaction",
        "value": "❤",
    }
    rc = cli_main(
        [
            "--vault",
            str(cfg.vault_path),
            "interaction",
            "handle",
            "--event-json",
            json.dumps(event, ensure_ascii=False),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["action"] == "save_and_promote"


def test_deliver_help_exposes_prepare_and_filters(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["deliver", "--help"])
    assert exc.value.code == 0
    deliver_help = capsys.readouterr().out
    assert "prepare" in deliver_help
    assert "pending" in deliver_help

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["deliver", "pending", "--help"])
    assert exc.value.code == 0
    pending_help = capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["deliver", "prepare", "--help"])
    assert exc.value.code == 0
    prepare_help = capsys.readouterr().out

    for help_text in (pending_help, prepare_help):
        assert "--origin" in help_text
        assert "--lens-matched" in help_text
        assert "--first-seen-only" in help_text
        assert "--payload-type" in help_text
    assert "--collect" in prepare_help
    assert "fetch-all" in prepare_help


def test_deliver_prepare_collect_fetch_all_returns_collector_and_delivery(
    monkeypatch,
    tmp_path,
    capsys,
):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)
    calls: dict[str, object] = {}

    async def fake_collect_all_active(
        config,
        *,
        include_error,
        per_subscription_timeout_seconds,
        concurrency,
    ):
        calls["vault"] = config.vault_path
        calls["include_error"] = include_error
        calls["timeout"] = per_subscription_timeout_seconds
        calls["concurrency"] = concurrency

        class Tick:
            total = 1
            new = 1
            skipped = 0
            errors = 0
            duration_seconds = 0.01
            per_subscription = []

        return Tick()

    monkeypatch.setattr("contents_hub.cli.collect_all_active", fake_collect_all_active)

    rc = cli_main(
        [
            "--vault",
            str(cfg.vault_path),
            "deliver",
            "prepare",
            "--collect",
            "fetch-all",
            "--payload-type",
            "raw_item",
            "--timeout-per-sub",
            "5",
            "--concurrency",
            "2",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert captured.err == ""
    assert payload["ok"] is True
    assert payload["collector"]["command"] == "fetch-all"
    assert payload["collector"]["ok"] is True
    assert payload["collector"]["errors"] == []
    assert payload["collector"]["summary"]["new_items"] == 1
    assert payload["delivery"]["ok"] is True
    assert payload["delivery"]["payload_type"] == "raw_item"
    assert payload["delivery"]["items"][0]["raw_item_id"] == raw_item_id
    assert calls == {
        "vault": cfg.vault_path,
        "include_error": True,
        "timeout": 5.0,
        "concurrency": 2,
    }


def test_deliver_prepare_collect_tick_returns_collector_and_delivery(
    monkeypatch,
    tmp_path,
    capsys,
):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)
    calls: dict[str, object] = {}

    async def fake_collect_all_due(
        config,
        *,
        per_subscription_timeout_seconds,
    ):
        calls["vault"] = config.vault_path
        calls["timeout"] = per_subscription_timeout_seconds

        class Tick:
            total = 1
            new = 1
            skipped = 0
            errors = 0
            duration_seconds = 0.02
            per_subscription = []

        return Tick()

    monkeypatch.setattr("contents_hub.cli.collect_all_due", fake_collect_all_due)

    rc = cli_main(
        [
            "--vault",
            str(cfg.vault_path),
            "deliver",
            "prepare",
            "--collect",
            "tick",
            "--payload-type",
            "raw_item",
            "--timeout-per-sub",
            "7",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert captured.err == ""
    assert payload["ok"] is True
    assert payload["collector"]["command"] == "tick"
    assert payload["collector"]["ok"] is True
    assert payload["collector"]["summary"]["new_items"] == 1
    assert payload["delivery"]["items"][0]["raw_item_id"] == raw_item_id
    assert calls == {"vault": cfg.vault_path, "timeout": 7.0}


def test_deliver_prepare_collect_failure_emits_single_json_and_nonzero(
    monkeypatch,
    tmp_path,
    capsys,
):
    cfg = _cfg(tmp_path)

    async def failing_collect_all_active(
        config,
        *,
        include_error,
        per_subscription_timeout_seconds,
        concurrency,
    ):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr("contents_hub.cli.collect_all_active", failing_collect_all_active)

    rc = cli_main(
        [
            "--vault",
            str(cfg.vault_path),
            "deliver",
            "prepare",
            "--collect",
            "fetch-all",
            "--payload-type",
            "raw_item",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    payload = json.loads(captured.out)

    assert rc == 1
    assert captured.err == ""
    assert len(lines) == 1
    assert payload["ok"] is False
    assert payload["collector"] == {
        "command": "fetch-all",
        "ok": False,
        "summary": {},
        "errors": ["collector exploded"],
    }
    assert payload["delivery"]["ok"] is True
    assert payload["error"] == "collector exploded"


def test_slack_and_discord_fixtures_normalize_to_shared_shape():
    slack = normalize_slack_event(
        {
            "event_id": "slack-evt",
            "team_id": "team-1",
            "item": {"channel": "chan-1", "ts": "123.4"},
            "user": "user-1",
            "reaction": "⭐",
        }
    )
    discord = normalize_discord_event(
        {
            "id": "discord-evt",
            "guild_id": "guild-1",
            "channel_id": "chan-2",
            "message_id": "msg-2",
            "user_id": "user-2",
            "emoji": {"name": "⭐"},
        }
    )

    for payload in (slack, discord):
        assert set(payload) == {
            "platform",
            "event_id",
            "workspace_id",
            "channel_id",
            "thread_id",
            "message_id",
            "user_id",
            "kind",
            "value",
            "raw_payload",
        }
        assert payload["kind"] == "reaction"
        assert payload["value"] == "⭐"
    assert slack["workspace_id"] == "team-1"
    assert slack["channel_id"] == "chan-1"
    assert slack["thread_id"] == ""
    assert slack["message_id"] == "123.4"


def test_slack_reaction_shape_resolves_default_delivery_thread(tmp_path):
    cfg = _cfg(tmp_path)
    raw_item_id = _seed_raw_item(cfg)
    record_outbound_message(
        cfg,
        platform="slack",
        workspace_id="team-1",
        channel_id="chan-1",
        message_id="123.4",
        payload_type="raw_item",
        raw_item_id=raw_item_id,
    )

    event = normalize_slack_event(
        {
            "event_id": "slack-evt",
            "team_id": "team-1",
            "item": {"channel": "chan-1", "ts": "123.4"},
            "user": "user-1",
            "reaction": "⭐",
        }
    )
    result = handle_interaction(cfg, event)

    assert result["ok"] is True
    assert result["action"] == "save_and_promote"
    assert result["raw_item_id"] == raw_item_id
