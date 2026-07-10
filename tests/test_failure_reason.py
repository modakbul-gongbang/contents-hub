"""Tests for the typed FetchFailureReason plumbing (re-targeted to executor).

Covers:
- Enum parsing from agent JSON (explicit failure_reason).
- Heuristic fallback when the agent omits the field.
- Executor-level ``_record_failure`` propagating the enum onto FetchResult.
- Web layer: needs_auth is decided by the enum, not substring matching.

Pre-refactor these tests imported ``BrowserFetcher`` and the helpers from
``contents_hub.fetchers.browser``.  Post-refactor (T13/R-T7.3) the equivalent
parsing helpers and failure-recording logic live in :mod:`contents_hub.executor`,
and the canonical ``FetchFailureReason`` enum lives in :mod:`contents_hub.models`.
"""

from __future__ import annotations


import pytest

from contents_hub.executor import (
    _parse_failure_json,
    _parse_items_json,
    _record_failure,
)
from contents_hub.models import FetchFailureReason, infer_from_error


class TestEnumParsing:
    def test_parse_known_value(self):
        assert FetchFailureReason.parse("login_required") == FetchFailureReason.LOGIN_REQUIRED

    def test_parse_case_insensitive(self):
        assert FetchFailureReason.parse(" Login_Required ") == FetchFailureReason.LOGIN_REQUIRED

    def test_parse_none_for_empty(self):
        assert FetchFailureReason.parse("") is None
        assert FetchFailureReason.parse(None) is None

    def test_parse_unknown_returns_none(self):
        assert FetchFailureReason.parse("wat") is None


class TestInferFromError:
    @pytest.mark.parametrize("msg,expected", [
        ("Login required: /uas/login", FetchFailureReason.LOGIN_REQUIRED),
        ("execute agent timed out", FetchFailureReason.TIMEOUT),
        ("HTTP 404 Not Found", FetchFailureReason.NOT_FOUND),
        ("Got captcha page", FetchFailureReason.BLOCKED),
        ("connection refused", FetchFailureReason.NETWORK),
        ("selector not found in DOM", FetchFailureReason.STRUCTURE_CHANGED),
        ("something weird", FetchFailureReason.UNKNOWN),
        ("", FetchFailureReason.UNKNOWN),
    ])
    def test_heuristic_mapping(self, msg, expected):
        assert infer_from_error(msg) == expected


class TestParseItemsJson:
    def test_explicit_failure_reason(self):
        agent_out = '{"items": [], "errors": ["blocked"], "failure_reason": "blocked"}'
        items, errors, reason = _parse_items_json(agent_out)
        assert items == []
        assert errors == ["blocked"]
        assert reason == FetchFailureReason.BLOCKED

    def test_missing_failure_reason_is_none(self):
        agent_out = '{"items": [], "errors": ["oops"]}'
        items, errors, reason = _parse_items_json(agent_out)
        assert errors == ["oops"]
        assert reason is None

    def test_success_case_no_reason(self):
        agent_out = '{"items": [{"url":"https://a","title":"t"}], "errors": []}'
        items, errors, reason = _parse_items_json(agent_out)
        assert len(items) == 1
        assert errors == []
        assert reason is None

    def test_parse_failure_json_helper(self):
        out = '{"failure_reason": "login_required", "error": "bounced"}'
        assert _parse_failure_json(out) == FetchFailureReason.LOGIN_REQUIRED


class TestRecordFailurePropagation:
    """``executor._record_failure`` should emit the enum value on
    ``FetchResult.failure_reason`` — either the explicit one, or an inferred
    fallback based on the error string."""

    def test_explicit_reason_wins(self):
        cfg: dict = {}
        res = _record_failure(
            "https://example.com",
            cfg,
            "agent bounced",
            failure_reason=FetchFailureReason.LOGIN_REQUIRED,
        )
        assert res.ok is False
        assert res.failure_reason == "login_required"

    def test_heuristic_fallback_from_text(self):
        cfg: dict = {}
        res = _record_failure(
            "https://example.com",
            cfg,
            "execute agent timed out",
        )
        assert res.failure_reason == "timeout"

    def test_unknown_bucket_for_opaque_error(self):
        cfg: dict = {}
        res = _record_failure(
            "https://example.com",
            cfg,
            "something weird happened",
        )
        assert res.failure_reason == "unknown"


class TestWebLayerNeedsAuth:
    """Exercise the needs_auth decision path end-to-end via the /subscriptions/{id}
    HTML response. The mock fetcher never runs — we poke the sub's stored
    config directly."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from contents_hub.config import WikiConfig
        from contents_hub.db import get_db
        from contents_hub.web.app import create_app

        vault = tmp_path
        (vault / ".contents-hub").mkdir()
        cfg = WikiConfig(vault_path=vault)

        # Schema bootstrap
        with get_db(cfg) as conn:
            pass

        # Insert one sub with a login_required failure_reason in its last_manual_fetch
        import json as _json
        now = "2026-04-18T00:00:00+00:00"
        with get_db(cfg) as conn:
            conn.execute(
                """INSERT INTO subscriptions (url, title, source_type, status,
                   schedule_cron, schedule_interval_minutes, default_lens_ids,
                   config, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "https://linkedin.com/in/test",
                    "Test LinkedIn",
                    "linkedin",
                    "error",
                    None, 60, "[]",
                    _json.dumps({
                        "last_manual_fetch": {
                            "ok": False,
                            "error": "execute agent timed out",  # substring wouldn't match login hints
                            "failure_reason": "login_required",  # but enum says it's auth
                            "finished_at": now,
                            "items": 0,
                        },
                    }),
                    now, now,
                ),
            )

        app = create_app(cfg)
        return TestClient(app)

    def test_enum_login_required_shows_signin_card(self, client):
        # Find the sub id
        resp = client.get("/subscriptions")
        assert resp.status_code == 200
        import re
        m = re.search(r'href="/subscriptions/(\d+)"', resp.text)
        assert m, "subscription row not rendered"
        sub_id = m.group(1)

        resp = client.get(f"/subscriptions/{sub_id}")
        assert resp.status_code == 200
        # The sign-in card is what the user sees
        assert "Sign-in required" in resp.text
