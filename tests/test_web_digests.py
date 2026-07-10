from __future__ import annotations

import datetime as _dt
import json

import pytest
from fastapi.testclient import TestClient

from contents_hub.config import WikiConfig
from contents_hub.db import get_db, init_db
from contents_hub.web.app import create_app


@pytest.fixture
def vault(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)
    cfg.meta_path.mkdir(parents=True, exist_ok=True)
    init_db(cfg)
    return cfg


@pytest.fixture
def client(vault):
    return TestClient(create_app(vault))


def _now(offset_minutes: int = 0) -> str:
    return (
        _dt.datetime(2026, 5, 20, 1, 0, tzinfo=_dt.timezone.utc)
        + _dt.timedelta(minutes=offset_minutes)
    ).isoformat()


def _seed_digest_fixture(cfg: WikiConfig) -> tuple[int, int]:
    with get_db(cfg) as conn:
        conn.execute(
            "INSERT INTO subscriptions (id, url, title, created_at, updated_at) "
            "VALUES (1, 'https://source.example', 'Source Feed', ?, ?)",
            (_now(), _now()),
        )
        conn.executemany(
            """INSERT INTO raw_items
               (id, url, title, body, status, subscription_id, content_summary,
                collected_at, updated_at)
               VALUES (?, ?, ?, ?, 'raw', 1, ?, ?, ?)""",
            [
                (
                    1,
                    "https://example.com/one",
                    "Article One",
                    "Body one",
                    "Summary one",
                    _now(1),
                    _now(1),
                ),
                (
                    2,
                    "https://example.com/two",
                    "Article Two",
                    "Body two",
                    "Summary two",
                    _now(2),
                    _now(2),
                ),
            ],
        )
        old = conn.execute(
            """INSERT INTO digests
               (created_at, title, item_count, content_md, sections_json)
               VALUES (?, 'Older Digest', 0, '', '[]')""",
            (_now(-60),),
        ).lastrowid
        sections = [
            {
                "lens_id": "ai",
                "lens_name": "AI Lens",
                "lens_description": "Agent workflow updates",
                "narrative_md": (
                    "**AI Lens narrative**\n\n"
                    "📎 **관련 아티클**\n"
                    "- duplicate markdown link\n\n"
                    "---\n\n"
                    "**Second chunk**"
                ),
                "item_ids": [1, 2],
            }
        ]
        new = conn.execute(
            """INSERT INTO digests
               (created_at, title, item_count, content_md, sections_json)
               VALUES (?, 'Latest Digest', 2, ?, ?)""",
            (
                _now(30),
                "📌 오늘의 핵심\n\n**Executive summary.**\n\n🎯 AI Lens",
                json.dumps(sections),
            ),
        ).lastrowid
        conn.executemany(
            """INSERT INTO digest_section_items
               (digest_id, section_index, lens_id, raw_item_id, sort_order)
               VALUES (?, 0, 'ai', ?, ?)""",
            [(new, 1, 0), (new, 2, 1)],
        )
    return int(old), int(new)


def test_digests_list_and_detail_render_structured_items(vault, client):
    old_id, new_id = _seed_digest_fixture(vault)

    resp = client.get("/digests")
    assert resp.status_code == 200
    assert resp.text.index("Latest Digest") < resp.text.index("Older Digest")
    assert f"/digests/{new_id}" in resp.text
    assert f"/digests/{old_id}" in resp.text

    detail = client.get(f"/digests/{new_id}")
    assert detail.status_code == 200
    assert "<strong>Executive summary.</strong>" in detail.text
    assert "<strong>AI Lens narrative</strong>" in detail.text
    assert "<strong>Second chunk</strong>" in detail.text
    assert "**Executive summary.**" not in detail.text
    assert "📎" not in detail.text
    assert "duplicate markdown link" not in detail.text
    assert "Article One" in detail.text
    assert "https://example.com/one" in detail.text
    assert "/raw-items/1/toggle-saved" in detail.text


def test_saved_toggle_round_trips_from_digest_to_saved_tab(vault, client):
    _, digest_id = _seed_digest_fixture(vault)

    resp = client.post(
        "/raw-items/1/toggle-saved",
        data={"next_url": f"/digests/{digest_id}"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/digests/{digest_id}?msg=Saved")
    with get_db(vault) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM saved_items WHERE raw_item_id = 1"
            ).fetchone()[0]
            == 1
        )

    detail = client.get(f"/digests/{digest_id}")
    assert detail.status_code == 200
    assert "Saved" in detail.text

    saved = client.get("/saved")
    assert saved.status_code == 200
    assert "Article One" in saved.text
    assert "Source Feed" in saved.text

    resp = client.post(
        "/raw-items/1/toggle-saved",
        data={"next_url": "/saved"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with get_db(vault) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM saved_items WHERE raw_item_id = 1"
            ).fetchone()[0]
            == 0
        )
