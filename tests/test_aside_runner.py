from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
import respx

from contents_hub.api import fetch_subscription
from contents_hub.cli import main as cli_main
from contents_hub.config import WikiConfig
from contents_hub.db import init_db
from contents_hub.explorations import ExplorationStore, ExplorationStrategyRunner
from contents_hub.runners import (
    AgentRunError, get_default_runner, get_default_text_runner, set_default_runner,
)
from contents_hub.runners.aside import AsideRunner
from contents_hub.runners.no_agent import NoAgentRunner
from contents_hub.subscriptions import SubscriptionStore


@pytest.fixture
def aside_cli(tmp_path, monkeypatch):
    """Fake only the external browser CLI, not collection or persistence."""
    executable = tmp_path / "aside"
    source = Path(__file__).with_name("fixtures") / "aside_cli.py"
    executable.write_text(f"#!{sys.executable}\n" + source.read_text())
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    def configure(**scenario):
        (tmp_path / "scenario.json").write_text(json.dumps(scenario))
        return AsideRunner(command=str(executable))

    return configure


def final(text, reason="stop", **extra):
    return {"type": "message_end", "message": {
        "role": "assistant", "stopReason": reason,
        "content": [{"type": "text", "text": text}], **extra,
    }}


def active_sessions(tmp_path):
    return list(tmp_path.glob("*.active"))


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_aside_returns_final_assistant_answer_and_releases_session(aside_cli, tmp_path):
    runner = aside_cli(events=[
        final("intermediate", "toolUse"),
        {"type": "message_end", "message": {
            "role": "toolResult", "content": [{"type": "text", "text": "wrong"}],
        }},
        final('{"items": []}'), {"type": "agent_end"},
    ])
    for _ in range(5):
        assert await runner.run("Collect", max_turns=2) == '{"items": []}'
        assert active_sessions(tmp_path) == []
        assert list(tmp_path.glob("contents-hub-aside-*")) == []


@pytest.mark.parametrize("scenario", [
    {"exit_code": 1},
    {"events": [final("wrong", errorMessage="provider unavailable"), {"type": "agent_end"}]},
    {"events": [final("wrong", reason="length"), {"type": "agent_end"}]},
    {"events": [{"type": "agent_end"}]},
    {"events": [final("unfinished")]},
    {"events": [{"type": "message_end", "message": {
        "role": "toolResult", "content": [{"type": "text", "text": '{"items": []}'}],
    }}, {"type": "agent_end"}]},
    {"malformed": True},
    {"flood": True},
])
async def test_aside_failures_never_return_transcript_as_success(aside_cli, tmp_path, scenario):
    runner = aside_cli(**scenario)
    with pytest.raises(AgentRunError):
        await runner.run("Collect", timeout=5)
    assert active_sessions(tmp_path) == []


async def test_aside_turn_limit_stops_tool_work_at_the_budget(aside_cli, tmp_path):
    runner = aside_cli(events=[
        final("continue", "toolUse"), final("over budget"), {"type": "agent_end"},
    ])
    with pytest.raises(AgentRunError, match="turn_limit"):
        await runner.run("Collect", max_turns=1)
    assert active_sessions(tmp_path) == []


async def test_aside_timeout_and_cancellation_stop_owned_session(aside_cli, tmp_path):
    runner = aside_cli(hang=True)
    with pytest.raises(asyncio.TimeoutError):
        await runner.run("Collect", timeout=0.5)
    assert active_sessions(tmp_path) == []

    task = asyncio.create_task(runner.run("Collect", timeout=10))
    await wait_until(lambda: bool(active_sessions(tmp_path)))
    pid = int(active_sessions(tmp_path)[0].read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active_sessions(tmp_path) == []
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_aside_owner_death_releases_browser_session(aside_cli, tmp_path):
    aside_cli(hang=True)
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import asyncio; from contents_hub.runners.aside import AsideRunner; "
        "asyncio.run(AsideRunner().run('Collect'))",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await wait_until(lambda: bool(active_sessions(tmp_path)))
        pid = int(active_sessions(tmp_path)[0].read_text())
        owner.kill()
        await owner.wait()
        await wait_until(lambda: not active_sessions(tmp_path))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if owner.returncode is None:
            owner.kill()
            await owner.wait()


async def test_aside_capacity_is_bounded_and_released(aside_cli, tmp_path):
    runner = aside_cli(hang=True)
    tasks = [asyncio.create_task(runner.run("Collect")) for _ in range(3)]
    try:
        await wait_until(lambda: len(active_sessions(tmp_path)) == 3)
        with pytest.raises(AgentRunError, match="capacity"):
            await AsideRunner().run("Collect")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert active_sessions(tmp_path) == []
    working = aside_cli(events=[final("complete"), {"type": "agent_end"}])
    assert await working.run("Collect") == "complete"


async def test_aside_reports_cleanup_failure(aside_cli):
    runner = aside_cli(delete_fails=True)
    with pytest.raises(AgentRunError, match="session_delete_failed"):
        await runner.run("Collect")


async def test_aside_cleanup_has_a_separate_grace_window(aside_cli, tmp_path):
    runner = aside_cli(events=[final("complete"), {"type": "agent_end"}], delete_delay=1.1)
    assert await runner.run("Collect", timeout=1) == "complete"
    assert active_sessions(tmp_path) == []


async def test_aside_missing_cli_is_actionable():
    with pytest.raises(AgentRunError, match="add aside to PATH"):
        await AsideRunner(command="missing-aside-executable").run("Collect")


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
async def test_aside_rejects_invalid_turn_budget(value):
    with pytest.raises(AgentRunError, match="positive integer"):
        await AsideRunner().run("Collect", max_turns=value)


@pytest.mark.parametrize("source_type,url,item_url", [
    ("x.profile", "https://x.com/reader", "https://x.com/reader/status/123"),
    ("linkedin.profile", "https://www.linkedin.com/in/reader/",
     "https://www.linkedin.com/feed/update/urn:li:activity:123/"),
    ("webpage", "https://example.test/", "https://example.test/article"),
])
@respx.mock
async def test_aside_collection_persists_once_without_chromux(
    aside_cli, tmp_path, monkeypatch, source_type, url, item_url,
):
    aside_cli(
        list_items=[{"url": item_url, "item_key": item_url}],
        detail_items=[{"url": item_url, "title": "A collected article",
                       "body_markdown": "Body captured from the browser.", "body_status": "full"}],
    )
    respx.get(item_url).respond(503)
    # No chromux executable is supplied. The complete API path must reach
    # Aside and preserve LIST -> DIFF -> CONTENT and real SQLite dedupe.
    previous = get_default_runner()
    monkeypatch.setenv("CONTENTS_HUB_AGENT_RUNNER", "aside")
    set_default_runner(None)
    try:
        config = WikiConfig(vault_path=tmp_path / "vault")
        init_db(config).close()
        sub = SubscriptionStore(config).add(url=url, title="Read", source_type=source_type)
        assert isinstance(get_default_runner(), AsideRunner)
        assert isinstance(get_default_text_runner(), NoAgentRunner)
        result = await fetch_subscription(config, sub.id)
        assert result.ok, result.error
        assert result.items[0].url == item_url
        assert result.items[0].content_html == "Body captured from the browser."
        again = await fetch_subscription(config, sub.id)
        assert again.ok and again.items == []
        conn = init_db(config)
        try:
            assert conn.execute("SELECT count(*) FROM raw_items").fetchone()[0] == 1
        finally:
            conn.close()
        prompts = [json.loads(line)["prompt"] for line in (tmp_path / "calls.jsonl").read_text().splitlines()
                   if json.loads(line)["action"] == "exec"]
        assert len(prompts) == 3
        assert all("chromux_navigate" not in prompt for prompt in prompts)
        assert all("Aside" in prompt for prompt in prompts)
    finally:
        set_default_runner(previous)


async def test_aside_login_failure_is_preserved(aside_cli, tmp_path, monkeypatch):
    aside_cli(login_required=True)
    previous = get_default_runner()
    monkeypatch.setenv("CONTENTS_HUB_AGENT_RUNNER", "aside")
    set_default_runner(None)
    try:
        config = WikiConfig(vault_path=tmp_path / "vault")
        init_db(config).close()
        sub = SubscriptionStore(config).add(url="https://x.com/reader", source_type="x.profile")
        result = await fetch_subscription(config, sub.id)
        assert not result.ok and result.failure_reason == "login_required"
    finally:
        set_default_runner(previous)


def test_aside_fetch_all_cli_keeps_json_stdout_and_dedupes(aside_cli, tmp_path, monkeypatch, capsys):
    item_url = "https://x.com/reader/status/123"
    aside_cli(
        list_items=[{"url": item_url, "item_key": item_url}],
        detail_items=[{"url": item_url, "title": "Collected", "body_markdown": "Body"}],
    )
    previous = get_default_runner()
    monkeypatch.setenv("CONTENTS_HUB_AGENT_RUNNER", "aside")
    set_default_runner(None)
    try:
        config = WikiConfig(vault_path=tmp_path / "vault")
        init_db(config).close()
        SubscriptionStore(config).add(url="https://x.com/reader", source_type="x.profile")
        args = ["--vault", str(config.vault_path), "fetch-all", "--concurrency", "3"]
        assert cli_main(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] and payload["new_items"] == 1
        assert cli_main(args) == 0
        repeated = json.loads(capsys.readouterr().out)
        assert repeated["ok"] and repeated["new_items"] == 0 and repeated["skipped"] == 1
    finally:
        set_default_runner(previous)


async def test_aside_exploration_reports_unsupported_tools_without_starting_browser(aside_cli, tmp_path):
    runner = aside_cli()
    config = WikiConfig(vault_path=tmp_path / "vault")
    init_db(config).close()
    store = ExplorationStore(config)
    draft = store.create_draft(display_name="Reading", original_request="Find articles")
    attempt = store.record_validation_attempt(
        exploration_id=draft.id, status="succeeded", strategy_snapshot={"query": "articles"},
    )
    store.approve_strategy(exploration_id=draft.id, validation_attempt_id=attempt.id)
    run = await ExplorationStrategyRunner(config=config, store=store, runner=runner).run_registered(draft.id)
    assert run.status == "failed"
    assert "claude-sdk" in run.error and "subscription collection only" in run.error
    assert not (tmp_path / "calls.jsonl").exists()
