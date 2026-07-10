"""CLI entry point for contents-hub.

Subcommands:
    sub {add,remove,list}                      — subscription management
    fetch <sub-ref>                            — single fetch (URL or integer sub_id), JSON stdout
    fetch-all                                  — fetch every active/error subscription, JSON stdout
    tick                                       — collect_all_due, JSON stdout
    explore <request>                         — register an exploration recipe from natural language
    exploration {add,list,run,run-all,delete} — manage exploration lifecycle
    lens {create,list,update,delete}           — manage Lens definitions
    raw add <url_or_text>                      — manually insert raw items
    daemon {run,loop,install,uninstall,status} — background collector (text default, --json optional)
    deliver {pending,prepare}                  — adapter-ready delivery cards
    web                                        — launch FastAPI dashboard
    init                                       — scaffold a new vault
    browser {open,status,kill}                 — manage the contents-hub browser profile

Designed to be thin: every command delegates to ``contents_hub.api`` or the
relevant module.

JSON output (R-U2.2 / R-U3.1 / contracts.md): both ``fetch`` and ``tick``
emit a single JSON object to stdout matching the frozen schema:

    {"ok": bool, "subscription_id": int, "new_items": int,
     "skipped": int, "items": [...], "error": str|null,
     "failure_reason": str|null}

For ``tick`` (multi-subscription) ``subscription_id`` is ``-1`` (sentinel
for "aggregate"); the aggregate roll-up is preserved under
``per_subscription``.

INV-9 / R-U5.1: nothing other than the single JSON object is printed to
stdout from ``fetch`` / ``tick``.  Agent diagnostics, executor turn
traces, tool calls — all funnel into the resolved metadata directory's
``cli.log`` via the file handler attached in :func:`_attach_cli_logging`.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from contents_hub.api import collect_all_active, collect_all_due, fetch_subscription
from contents_hub.config import load_config
from contents_hub.db import get_db
from contents_hub.digest import run_digest
from contents_hub.models import FetchResult
from contents_hub.naming import CLI_COMMAND
from contents_hub.subscriptions import (
    SubscriptionStatus,
    SubscriptionStore,
)
from contents_hub.timeutil import now_iso


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_sub_parser(sub_parsers) -> None:
    """Build the ``sub`` subparser.

    ``sub add`` mirrors the web form's URL classification and recipe pinning,
    but does not run the validating trial fetch; collection remains explicit
    via ``fetch`` / ``tick``.
    """
    sub_p = sub_parsers.add_parser("sub", help="Manage subscriptions")
    sub_sub = sub_p.add_subparsers(dest="sub_command", required=True)

    add_p = sub_sub.add_parser("add", help="Add a subscription")
    add_p.add_argument("url", help="URL to subscribe to")
    add_p.add_argument("--title", default="", help="Optional display title")
    add_p.add_argument(
        "--type",
        "--source-type",
        dest="source_type",
        default="",
        help="Optional source type override, e.g. youtube.channel, x.profile, threads.profile",
    )
    add_p.add_argument(
        "--collection-prompt",
        default="",
        help="Optional natural-language guidance for browser-backed collection",
    )

    rm_p = sub_sub.add_parser("remove", help="Remove a subscription")
    rm_p.add_argument("url")

    ls_p = sub_sub.add_parser("list", help="List subscriptions")
    ls_p.add_argument("--type", dest="source_type", default=None)
    ls_p.add_argument("--status", default=None, choices=["active", "paused", "error"])
    ls_p.add_argument("--format", dest="output_format", default="table", choices=["table", "json"])


def _build_fetch_parser(sub_parsers) -> None:
    """``fetch <sub-ref>`` — R-U1.2 / R-U2.1 / R-U2.2 / R-B1.1 / R-B1.2."""
    fetch_p = sub_parsers.add_parser(
        "fetch",
        help="Fetch a single subscription (URL or integer sub_id); JSON to stdout",
    )
    fetch_p.add_argument("sub_ref", help="Subscription URL or integer sub_id")
    fetch_p.add_argument(
        "--max-items",
        type=int,
        default=10,
        help="Safety cap on items returned by the agent path (default: 10)",
    )


def _build_fetch_all_parser(sub_parsers) -> None:
    """``fetch-all`` — force-fetch active and error subscriptions; JSON stdout."""
    fetch_all_p = sub_parsers.add_parser(
        "fetch-all",
        help="Fetch every active or error subscription, ignoring tick due schedule; JSON summary to stdout",
    )
    fetch_all_p.add_argument(
        "--timeout-per-sub",
        type=float,
        default=120.0,
        help="Seconds before one subscription is marked timed out (default: 120)",
    )
    fetch_all_p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Maximum subscriptions to fetch concurrently (default: 1)",
    )


def _build_tick_parser(sub_parsers) -> None:
    """``tick`` — R-U1.2 / R-U2.2 / R-B1.2 — collect_all_due, JSON stdout."""
    sub_parsers.add_parser(
        "tick",
        help="Collect all due subscriptions; JSON summary to stdout",
    )


def _build_daemon_parser(sub_parsers) -> None:
    """R-U2.3: daemon run/loop output contract is unchanged."""
    daemon_p = sub_parsers.add_parser("daemon", help="Background collector daemon")
    daemon_sub = daemon_p.add_subparsers(dest="daemon_command", required=True)

    run_p = daemon_sub.add_parser("run", help="Run a single tick and exit")
    run_p.add_argument("--json", action="store_true", dest="json_output")

    loop_p = daemon_sub.add_parser("loop", help="Run continuously")
    loop_p.add_argument("--interval", type=float, default=30, help="Minutes (default: 30)")
    loop_p.add_argument("--json", action="store_true", dest="json_output")

    daemon_sub.add_parser("install", help="Install macOS launchd agent")
    daemon_sub.add_parser("uninstall")
    daemon_sub.add_parser("status")


def _build_web_parser(sub_parsers) -> None:
    web_p = sub_parsers.add_parser("web", help="Launch the web dashboard")
    web_p.add_argument("--port", type=int, default=8585)


def _build_browser_parser(sub_parsers) -> None:
    browser_p = sub_parsers.add_parser(
        "browser",
        help="Open or inspect the contents-hub browser profile",
    )
    browser_sub = browser_p.add_subparsers(dest="browser_command", required=True)

    open_p = browser_sub.add_parser(
        "open",
        help="Open the contents-hub browser profile for manual sign-in",
    )
    open_p.add_argument("url", nargs="?", default=None, help="Optional URL to open")
    open_p.add_argument(
        "--confirm",
        action="store_true",
        help="Allow stopping a headless background profile before opening Chrome",
    )
    open_p.add_argument("--json", action="store_true", dest="json_output")

    status_p = browser_sub.add_parser("status", help="Show browser profile status")
    status_p.add_argument("--json", action="store_true", dest="json_output")

    kill_p = browser_sub.add_parser("kill", help="Stop the contents-hub browser profile")
    kill_p.add_argument("--json", action="store_true", dest="json_output")


def _build_init_parser(sub_parsers) -> None:
    init_p = sub_parsers.add_parser("init", help="Initialize a new vault")
    init_p.add_argument("path", nargs="?", default=".", help="Vault directory")


def _build_digest_parser(sub_parsers) -> None:
    """``digest`` — R-U1.1 / R-U1.3 / R-T14.1.

    Zero required arguments and zero optional flags in the MVP.  The
    --lens-id and --force flags are deliberately omitted (R-U1.3); their
    deferral is intentional and will be re-evaluated post-MVP — no stub
    arguments are added.
    """
    sub_parsers.add_parser(
        "digest",
        help="Produce a one-shot Obsidian digest of recent Lens-matched raw items; JSON stdout",
    )


def _build_delivery_parser(sub_parsers) -> None:
    delivery_p = sub_parsers.add_parser("delivery", help="Manage outbound delivery mappings")
    delivery_sub = delivery_p.add_subparsers(dest="delivery_command", required=True)

    record_p = delivery_sub.add_parser("record", help="Record an adapter outbound message id")
    record_p.add_argument("--platform", required=True)
    record_p.add_argument("--message-id", required=True)
    record_p.add_argument("--workspace-id", default="")
    record_p.add_argument("--channel-id", default="")
    record_p.add_argument("--thread-id", default="")
    record_p.add_argument("--payload-type", default="raw_item", choices=["raw_item", "digest"])
    record_p.add_argument("--raw-item-id", type=int, default=None)
    record_p.add_argument("--digest-id", type=int, default=None)

    list_p = delivery_sub.add_parser("list", help="List outbound delivery mappings")
    list_p.add_argument("--platform", default=None)
    list_p.add_argument("--limit", type=int, default=50)


def _build_interaction_parser(sub_parsers) -> None:
    interaction_p = sub_parsers.add_parser("interaction", help="Handle normalized channel interactions")
    interaction_sub = interaction_p.add_subparsers(dest="interaction_command", required=True)

    handle_p = interaction_sub.add_parser("handle", help="Handle one normalized interaction event")
    handle_p.add_argument("--event-json", default="", help="JSON object containing normalized event fields")
    handle_p.add_argument("--platform", default="")
    handle_p.add_argument("--event-id", default="")
    handle_p.add_argument("--workspace-id", default="")
    handle_p.add_argument("--channel-id", default="")
    handle_p.add_argument("--thread-id", default="")
    handle_p.add_argument("--message-id", default="")
    handle_p.add_argument("--user-id", default="")
    handle_p.add_argument("--kind", default="reaction")
    handle_p.add_argument("--value", default="")
    handle_p.add_argument("--format", dest="output_format", default="json", choices=["json"])

    rules_p = interaction_sub.add_parser("rules", help="Inspect interaction rules")
    rules_sub = rules_p.add_subparsers(dest="interaction_rules_command", required=True)
    rules_sub.add_parser("list", help="List active interaction rules")


def _build_deliver_parser(sub_parsers) -> None:
    deliver_p = sub_parsers.add_parser("deliver", help="Generate adapter-ready delivery payloads")
    deliver_sub = deliver_p.add_subparsers(dest="deliver_command", required=True)

    pending_p = deliver_sub.add_parser("pending", help="Emit pending raw item or digest cards")
    _add_deliver_payload_args(pending_p)

    prepare_p = deliver_sub.add_parser(
        "prepare",
        help="Optionally collect, then emit pending raw item or digest cards",
    )
    _add_deliver_payload_args(prepare_p)
    prepare_p.add_argument(
        "--collect",
        default="none",
        choices=["none", "fetch-all", "tick"],
        help="Collector to run before selecting delivery cards (default: none)",
    )
    prepare_p.add_argument(
        "--timeout-per-sub",
        type=float,
        default=120.0,
        help="Seconds before one collected subscription times out (default: 120)",
    )
    prepare_p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Maximum subscriptions to fetch concurrently for --collect fetch-all (default: 1)",
    )


def _add_deliver_payload_args(parser) -> None:
    parser.add_argument("--format", dest="output_format", default="json", choices=["json"])
    parser.add_argument("--payload-type", default="all", choices=["all", "raw_item", "digest"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--origin",
        default=None,
        help="Raw item origin filter, e.g. subscription",
    )
    parser.add_argument(
        "--lens-matched",
        action="store_true",
        help="Only include raw items matched to at least one Lens",
    )
    parser.add_argument(
        "--first-seen-only",
        action="store_true",
        help="Suppress newer raw item rows whose URL appeared earlier",
    )


def _build_item_parser(sub_parsers) -> None:
    """``item {open,signal,signals}`` — engagement signal ledger access."""
    item_p = sub_parsers.add_parser("item", help="Record and inspect item engagement signals")
    item_sub = item_p.add_subparsers(dest="item_command", required=True)

    open_p = item_sub.add_parser("open", help="Record an open signal and launch the item URL")
    open_p.add_argument("id_or_url", help="Raw item id or URL")
    open_p.add_argument("--source", default="cli", help="Signal source label (default: cli)")
    open_p.add_argument("--no-launch", action="store_true", help="Record only; do not open a browser")
    open_p.add_argument("--format", dest="output_format", default="json", choices=["json"])

    signal_p = item_sub.add_parser("signal", help="Record one engagement signal")
    signal_p.add_argument("id_or_url", help="Raw item id or URL")
    signal_p.add_argument(
        "--signal",
        required=True,
        choices=["impression", "open", "read", "save", "promote", "note", "unsave", "archive"],
    )
    signal_p.add_argument("--source", default="cli")
    signal_p.add_argument("--dedup-key", default="", help="Idempotency key ('' = always insert)")
    signal_p.add_argument("--metadata", default="", help="JSON object with signal context")
    signal_p.add_argument("--format", dest="output_format", default="json", choices=["json"])

    signals_p = item_sub.add_parser("signals", help="Show an item's signal ledger and engagement score")
    signals_p.add_argument("id_or_url", help="Raw item id or URL")
    signals_p.add_argument("--format", dest="output_format", default="json", choices=["json"])


def _build_query_parser(sub_parsers) -> None:
    """``query`` — knowledge-store search with engagement-aware ranking."""
    query_p = sub_parsers.add_parser(
        "query",
        help="Search collected items (keyword + optional semantic + engagement ranking)",
    )
    query_p.add_argument("text", help="Query text")
    query_p.add_argument("--limit", type=int, default=10)
    query_p.add_argument("--since", default=None, help="Recency window, e.g. 90d, 4w, 12h")
    query_p.add_argument("--lens", default=None, help="Only items matched to this Lens id")
    query_p.add_argument("--saved-only", action="store_true", help="Only saved items")
    query_p.add_argument(
        "--min-engagement",
        type=float,
        default=None,
        help="Only items whose decayed engagement score is at least this value",
    )
    query_p.add_argument("--explain", action="store_true", help="Include per-ranker score contributions")
    query_p.add_argument(
        "--context",
        action="store_true",
        help="Emit a markdown context pack instead of JSON",
    )
    query_p.add_argument("--format", dest="output_format", default="json", choices=["json"])


def _build_index_parser(sub_parsers) -> None:
    """``index`` — build/refresh the FTS index and embedding vectors."""
    index_p = sub_parsers.add_parser("index", help="Build the query index (FTS + optional embeddings)")
    index_p.add_argument("--rebuild-fts", action="store_true", help="Rebuild the FTS index from scratch")
    index_p.add_argument(
        "--backfill",
        action="store_true",
        help="Embed items missing vectors (requires CONTENTS_HUB_EMBEDDINGS)",
    )
    index_p.add_argument("--batch-size", type=int, default=32)
    index_p.add_argument("--format", dest="output_format", default="json", choices=["json"])


def _build_raw_parser(sub_parsers) -> None:
    """``raw add`` — manual raw item insertion for ad-hoc reading queues."""
    raw_p = sub_parsers.add_parser("raw", help="Manage manually added raw items")
    raw_sub = raw_p.add_subparsers(dest="raw_command", required=True)

    add_p = raw_sub.add_parser("add", help="Add a URL or text snippet as a manual raw item")
    add_p.add_argument("value", help="HTTP(S) URL or free-form text to add")
    add_p.add_argument("--title", default="", help="Optional title")
    add_p.add_argument("--body", default="", help="Optional body/content text")
    add_p.add_argument(
        "--summary",
        "--content-summary",
        dest="content_summary",
        default="",
        help="Optional short summary/preview",
    )
    add_p.add_argument("--published-at", default=None, help="Optional original publish timestamp")
    add_p.add_argument(
        "--metadata-json",
        default="{}",
        help="Optional JSON object merged into raw_items.metadata_json",
    )
    add_p.add_argument(
        "--lens-id",
        dest="lens_ids",
        action="append",
        default=[],
        help="Existing Lens id to attach, repeatable; when omitted attaches to all enabled automatic Lenses",
    )
    # Backward-compatible hidden no-op. URL inputs fetch page body by default.
    add_p.add_argument("--fetch-page", action="store_true", help=argparse.SUPPRESS)


def _add_exploration_create_args(parser) -> None:
    parser.add_argument(
        "request",
        nargs="+",
        help="Natural-language exploration request",
    )
    parser.add_argument(
        "--display-name",
        default="",
        help="Optional display name; defaults to a short request-derived label",
    )
    parser.add_argument(
        "--surface",
        dest="target_surfaces",
        action="append",
        default=[],
        help="Target surface hint, repeatable. Example: threads.feed",
    )
    parser.add_argument(
        "--lens-id",
        dest="lens_ids",
        action="append",
        default=[],
        help="Lens id to evaluate against, repeatable",
    )
    parser.add_argument(
        "--recipe",
        default="",
        help="Path to the approved Markdown or YAML recipe to register",
    )


def _build_explore_parser(sub_parsers) -> None:
    """``explore <request>`` — convenience recipe registration alias."""
    explore_p = sub_parsers.add_parser(
        "explore",
        help="Register an exploration from a natural-language request and recipe file",
    )
    _add_exploration_create_args(explore_p)


def _build_exploration_parser(sub_parsers) -> None:
    """``exploration ...`` — registered recipe/manual-run lifecycle."""
    exploration_p = sub_parsers.add_parser(
        "exploration",
        help="Manage exploration recipes and manual runs",
    )
    exploration_sub = exploration_p.add_subparsers(
        dest="exploration_command",
        required=True,
    )

    add_p = exploration_sub.add_parser(
        "add",
        help="Register an exploration from a natural-language request and recipe file",
    )
    _add_exploration_create_args(add_p)

    list_p = exploration_sub.add_parser("list", help="List explorations")
    list_p.add_argument(
        "--status",
        choices=["draft", "registered", "failed"],
        default=None,
    )
    list_p.add_argument(
        "--format",
        dest="output_format",
        default="table",
        choices=["table", "json"],
    )

    run_p = exploration_sub.add_parser(
        "run",
        help="Run a registered exploration once",
    )
    run_p.add_argument("exploration_id", type=int)
    run_p.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Seconds before the manual run is marked failed (default: 600)",
    )

    run_all_p = exploration_sub.add_parser(
        "run-all",
        help="Run every registered exploration once, sequentially",
    )
    run_all_p.add_argument(
        "--timeout-per-exploration",
        type=float,
        default=600.0,
        help="Seconds before one exploration run is marked failed (default: 600)",
    )

    delete_p = exploration_sub.add_parser("delete", help="Delete an exploration")
    delete_p.add_argument("exploration_id", type=int)


def _build_lens_parser(sub_parsers) -> None:
    """``lens ...`` — CRUD for Lens definitions used by subscriptions/explorations."""
    lens_p = sub_parsers.add_parser("lens", help="Manage Lens definitions")
    lens_sub = lens_p.add_subparsers(dest="lens_command", required=True)

    create_p = lens_sub.add_parser("create", help="Create a Lens")
    create_p.add_argument("lens_id", help="Lens id slug, e.g. ai-research")
    create_p.add_argument("--name", default="", help="Human-readable Lens name")
    create_p.add_argument("--description", default="", help="Lens criteria description")
    create_p.add_argument(
        "--keyword",
        dest="keywords",
        action="append",
        default=[],
        help="Keyword to match before LLM classification, repeatable",
    )
    create_p.add_argument("--disabled", action="store_true", help="Create disabled")

    list_p = lens_sub.add_parser("list", help="List Lenses")
    list_p.add_argument("--format", dest="output_format", default="table", choices=["table", "json"])
    state = list_p.add_mutually_exclusive_group()
    state.add_argument("--enabled", action="store_true", help="Show enabled Lenses only")
    state.add_argument("--disabled", action="store_true", help="Show disabled Lenses only")

    update_p = lens_sub.add_parser("update", help="Update a Lens")
    update_p.add_argument("lens_id", help="Lens id")
    update_p.add_argument("--name", default=None, help="Replace Lens name")
    update_p.add_argument("--description", default=None, help="Replace Lens description")
    update_p.add_argument(
        "--keyword",
        dest="keywords",
        action="append",
        default=None,
        help="Replace keywords with this repeatable list",
    )
    update_p.add_argument(
        "--clear-keywords",
        action="store_true",
        help="Replace keywords with an empty list",
    )
    state = update_p.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true", help="Enable the Lens")
    state.add_argument("--disable", action="store_true", help="Disable the Lens")

    delete_p = lens_sub.add_parser("delete", help="Delete a Lens")
    delete_p.add_argument("lens_id", help="Lens id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI_COMMAND.canonical,
        description="contents-hub: collect sources into an Obsidian vault",
    )
    parser.add_argument(
        "--vault",
        type=str,
        default=None,
        help="Vault path (default: environment vault or CWD)",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    _build_sub_parser(sub)
    _build_fetch_parser(sub)
    _build_fetch_all_parser(sub)
    _build_tick_parser(sub)
    _build_explore_parser(sub)
    _build_exploration_parser(sub)
    _build_lens_parser(sub)
    _build_raw_parser(sub)
    _build_daemon_parser(sub)
    _build_web_parser(sub)
    _build_browser_parser(sub)
    _build_init_parser(sub)
    _build_digest_parser(sub)
    _build_delivery_parser(sub)
    _build_interaction_parser(sub)
    _build_deliver_parser(sub)
    _build_item_parser(sub)
    _build_query_parser(sub)
    _build_index_parser(sub)
    return parser


# ---------------------------------------------------------------------------
# Subscription handlers
# ---------------------------------------------------------------------------


def _default_schedule_arg(config, source_type: str) -> dict[str, Any]:
    cron = config.schedule.cron_for(source_type)
    interval = config.schedule.interval_for(source_type)
    schedule_arg: dict[str, Any] = {"preset": "daily"}
    if cron:
        schedule_arg["cron"] = cron
    elif interval:
        schedule_arg["interval_minutes"] = int(interval)
    return schedule_arg


def _handle_sub_add(config, store: SubscriptionStore, args) -> int:
    from contents_hub.source_router import classify
    from contents_hub.source_types import classify_url, is_supported_source_type

    source_type_override = (args.source_type or "").strip()
    if source_type_override and not is_supported_source_type(source_type_override):
        _emit_json(
            {
                "ok": False,
                "subscription_id": -1,
                "url": args.url,
                "error": f"unknown source_type: {source_type_override}",
            }
        )
        return 1

    try:
        info = (
            classify_url(args.url, source_type_override)
            if source_type_override
            else classify(args.url)
        )
        source_type = str(info["source_type"])
        config_data: dict[str, Any] = {
            "recipe_base": info.get("recipe_base"),
            "recipe_id": info.get("recipe_id"),
            "recipe_version": info.get("recipe_version"),
            "recipe_channel": info.get("recipe_channel", "stable"),
            "fetch_method": info.get("execution_method"),
            "recipe_capabilities": info.get("capabilities") or [],
        }
        collection_prompt = (args.collection_prompt or "").strip()
        if collection_prompt:
            config_data["collection_prompt"] = collection_prompt

        title = (args.title or "").strip() or str(info.get("suggested_title") or "")
        sub = store.add(
            url=args.url,
            title=title,
            source_type=source_type,
            schedule=_default_schedule_arg(config, source_type),
            config=config_data,
        )
    except Exception as exc:  # noqa: BLE001 - CLI reports validation/duplicate errors uniformly
        _emit_json(
            {
                "ok": False,
                "subscription_id": -1,
                "url": args.url,
                "error": str(exc),
            }
        )
        return 1

    try:
        subscription_id = int(sub.id)
    except (TypeError, ValueError):
        subscription_id = -1

    _emit_json(
        {
            "ok": True,
            "subscription_id": subscription_id,
            "url": sub.url,
            "title": sub.title,
            "source_type": sub.source_type,
            "status": sub.status.value,
            "recipe_id": sub.config.get("recipe_id"),
            "recipe_version": sub.config.get("recipe_version"),
            "fetch_method": sub.config.get("fetch_method"),
            "collection_prompt": sub.config.get("collection_prompt", ""),
        }
    )
    return 0


def _handle_sub_remove(store: SubscriptionStore, args) -> int:
    try:
        sub = store.remove(args.url)
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Removed: {sub.url}")
    return 0


def _handle_sub_list(store: SubscriptionStore, args) -> int:
    if args.status:
        subs = store.list_by_status(SubscriptionStatus(args.status))
    elif args.source_type:
        subs = store.list_by_source_type(args.source_type)
    else:
        subs = store.list_all()

    if args.output_format == "json":
        print(json.dumps([s.to_dict() for s in subs], indent=2, ensure_ascii=False))
        return 0

    if not subs:
        print("No subscriptions found.")
        return 0
    print(f"{'STATUS':<8} {'TYPE':<10} {'TITLE':<30} {'URL':<50}")
    print("-" * 100)
    for s in subs:
        status = s.status.value
        stype = s.source_type or "auto"
        title = (s.title or "(untitled)")[:28]
        url = s.url[:48]
        print(f"{status:<8} {stype:<10} {title:<30} {url:<50}")
    print(f"\nTotal: {len(subs)}")
    return 0


def _handle_sub(config, args) -> int:
    store = SubscriptionStore(config)
    cmd = args.sub_command
    if cmd == "add":
        return _handle_sub_add(config, store, args)
    if cmd == "remove":
        return _handle_sub_remove(store, args)
    if cmd == "list":
        return _handle_sub_list(store, args)
    return 1


# ---------------------------------------------------------------------------
# fetch / tick handlers
# ---------------------------------------------------------------------------


def _coerce_sub_ref(raw: str) -> str | int:
    """Turn the CLI string into the union ``sub_ref`` accepts.

    ``api.fetch_subscription`` itself is tolerant of stringified ints,
    but converting at the CLI boundary keeps the logged ref shape
    predictable.
    """
    s = raw.strip()
    if s and s.lstrip("-").isdigit():
        try:
            return int(s)
        except ValueError:  # pragma: no cover - guarded by isdigit check
            return s
    return s


def _item_to_record(item: Any) -> dict[str, Any]:
    """Trimmed FetchedItem → ItemRecord (contracts.md OD-2 resolution)."""
    published = getattr(item, "published_at", None)
    published_iso: str | None
    if isinstance(published, datetime):
        published_iso = published.isoformat()
    elif isinstance(published, str) and published:
        published_iso = published
    else:
        published_iso = None
    return {
        "url": getattr(item, "url", "") or "",
        "title": getattr(item, "title", "") or "",
        "summary": getattr(item, "summary", "") or "",
        "published_at": published_iso,
        "author": getattr(item, "author", "") or "",
    }


def _fetch_result_to_json(
    result: FetchResult,
    *,
    subscription_id: int,
) -> dict[str, Any]:
    """Map :class:`FetchResult` to the frozen CLI JSON schema."""
    items = [_item_to_record(it) for it in (result.items or [])]
    new_items = len(items) if result.ok else 0
    skipped = max(0, int(result.total_available or 0) - new_items) if result.ok else 0
    return {
        "ok": bool(result.ok),
        "subscription_id": int(subscription_id),
        "new_items": int(new_items),
        "skipped": int(skipped),
        "items": items,
        "error": (result.error or None) if not result.ok else None,
        "failure_reason": (result.failure_reason or None) if not result.ok else None,
    }


def _resolve_sub_id_for_output(config, sub_ref: str | int) -> int:
    """Best-effort lookup of the int subscription_id to embed in JSON.

    Returns ``-1`` when the ref does not resolve (e.g. unknown URL); the
    caller still emits ``ok: false`` with ``failure_reason: not_found``.
    """
    try:
        store = SubscriptionStore(config)
        if isinstance(sub_ref, int) or (isinstance(sub_ref, str) and sub_ref.lstrip("-").isdigit()):
            sub = store.get_by_id(str(sub_ref))
            if sub is not None and str(sub.id).isdigit():
                return int(sub.id)
        if isinstance(sub_ref, str):
            sub = store.get(sub_ref)
            if sub is not None and str(sub.id).isdigit():
                return int(sub.id)
    except Exception:  # noqa: BLE001 - best-effort only
        pass
    return -1


def _emit_json(payload: dict[str, Any]) -> None:
    """Single-line JSON to stdout — INV-9 / R-U2.2 / R-U5.1."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _handle_fetch(config, args) -> int:
    sub_ref = _coerce_sub_ref(args.sub_ref)
    max_items = int(getattr(args, "max_items", 10))

    try:
        result = asyncio.run(fetch_subscription(config, sub_ref, max_items=max_items))
    except Exception:
        # R-U3.3: optimistic concurrency — surface tracebacks as-is to
        # stderr and exit 1.  Stdout stays clean of partial JSON.
        import traceback
        traceback.print_exc()
        return 1

    sub_id = _resolve_sub_id_for_output(config, sub_ref)
    payload = _fetch_result_to_json(result, subscription_id=sub_id)
    _emit_json(payload)
    return 0 if result.ok else 1


def _tick_to_json(tick) -> dict[str, Any]:
    """Map :class:`api.DaemonTickResult` to the frozen CLI JSON schema.

    Required surface (contracts.md): ``{ok, subscription_id, new_items,
    skipped, items, error, failure_reason}``.  ``tick`` aggregates
    multiple subscriptions, so:

    - ``subscription_id`` = ``-1`` (aggregate sentinel)
    - ``items``           = ``[]`` (per-subscription items omitted from
      stdout to keep the surface stable; raw_items DB has the detail)
    - ``error`` / ``failure_reason`` = null on overall success even if
      individual subs failed; per-sub roll-up lives under
      ``per_subscription``
    """
    per_sub = []
    for entry in getattr(tick, "per_subscription", []) or []:
        try:
            per_sub.append(dataclasses.asdict(entry))
        except TypeError:
            # Tolerate non-dataclass fallbacks (e.g. dict-like).
            per_sub.append(dict(entry) if hasattr(entry, "keys") else {"value": str(entry)})

    overall_ok = int(getattr(tick, "errors", 0)) == 0
    return {
        "ok": overall_ok,
        "subscription_id": -1,
        "new_items": int(getattr(tick, "new", 0)),
        "skipped": int(getattr(tick, "skipped", 0)),
        "items": [],
        "error": None if overall_ok else f"{tick.errors} subscription(s) failed",
        "failure_reason": None,
        # Convenience telemetry — not in the frozen schema but useful for
        # operators tailing CLI output.  Keeps the top-level keys stable.
        "total": int(getattr(tick, "total", 0)),
        "errors": int(getattr(tick, "errors", 0)),
        "duration_seconds": round(float(getattr(tick, "duration_seconds", 0.0)), 3),
        "per_subscription": per_sub,
    }


def _handle_tick(config) -> int:
    try:
        result = asyncio.run(collect_all_due(config))
    except Exception:
        import traceback
        traceback.print_exc()
        return 1

    payload = _tick_to_json(result)
    _emit_json(payload)
    # R-U3.2: tick is "successful" iff the orchestration completed; per-sub
    # failures are surfaced inside the JSON, not via exit code.  This
    # mirrors `daemon run` semantics.
    return 0


def _handle_fetch_all(config, args) -> int:
    try:
        result = asyncio.run(
            collect_all_active(
                config,
                include_error=True,
                per_subscription_timeout_seconds=float(getattr(args, "timeout_per_sub", 120.0)),
                concurrency=int(getattr(args, "concurrency", 1) or 1),
            )
        )
    except Exception:
        import traceback
        traceback.print_exc()
        return 1

    payload = _tick_to_json(result)
    _emit_json(payload)
    return 0 if payload.get("ok") is True else 1


# ---------------------------------------------------------------------------
# digest handler — R-U1 / R-U2 / R-U3 / R-T12.2 / R-T14
# ---------------------------------------------------------------------------


def _handle_digest(config, args) -> int:
    """Run the digest pipeline and emit the frozen JSON contract.

    Mirrors the ``_handle_fetch`` pattern (cli.py line 284):

    - R-U1.2 / R-T14.2: dispatches ``run_digest`` via ``asyncio.run``.
    - R-U2.1 / R-U2.2 / R-U2.3 / R-T12.2: stdout always contains exactly
      ONE JSON line ``{ok, digest_id, path, item_count}`` emitted via
      ``_emit_json``.  No partial/extra stdout is written on any path —
      tracebacks and diagnostics route to the resolved metadata ``cli.log`` via
      the file handler attached by ``_attach_cli_logging`` (R-T12.2 keeps
      stdout JSON-only).
    - R-U3.1: exit 0 on success (including the zero-candidate case where
      ``run_digest`` returns ``{ok:True, digest_id:None, path:None,
      item_count:0}``).
    - R-U3.2 / R-U3.3: any caught exception OR ``ok=False`` from
      ``run_digest`` (LLM blowup, DB lock retry exhausted) maps to a
      non-zero exit code.
    """
    del args  # zero optional flags in MVP (R-U1.3); accepted for parity

    try:
        result = asyncio.run(run_digest(config))
    except Exception:  # noqa: BLE001 — uniform failure surface (R-U2.3)
        # R-U2.3 / R-T12.2: stdout still gets exactly one JSON line; the
        # traceback is logged to the resolved metadata cli.log via the file handler
        # attached by _attach_cli_logging.  Crucially we do NOT emit any
        # JSON before this point, so the failure line is the first and
        # only line on stdout.
        logger = logging.getLogger(__name__)
        logger.exception("digest run failed with unhandled exception")
        _emit_json({"ok": False, "digest_id": None, "path": None, "item_count": 0})
        return 1

    _emit_json(result)
    return 0 if bool(result.get("ok")) else 1


# ---------------------------------------------------------------------------
# Exploration handlers
# ---------------------------------------------------------------------------


def _join_request(parts: Sequence[str]) -> str:
    return " ".join(str(part).strip() for part in parts if str(part).strip()).strip()


def _suggest_exploration_display_name(request: str) -> str:
    words = [w.strip(" ,.;:!?()[]{}\"'") for w in request.split()]
    label = " ".join(w for w in words[:8] if w)
    return (label or "Exploration")[:80]


def _dataclass_record(value: Any) -> dict[str, Any]:
    return dataclasses.asdict(value)


def _exploration_payload(exploration) -> dict[str, Any]:
    payload = _dataclass_record(exploration)
    payload["exploration_id"] = payload.pop("id")
    return payload


def _strategy_payload(strategy) -> dict[str, Any]:
    payload = _dataclass_record(strategy)
    payload["strategy_version_id"] = payload.pop("id")
    return payload


def _run_payload(run) -> dict[str, Any]:
    payload = _dataclass_record(run)
    payload["run_id"] = payload.pop("id")
    return payload


def _read_recipe_file(recipe_path: str) -> tuple[str | None, str | None]:
    if not recipe_path:
        return None, "--recipe is required"
    path = Path(recipe_path).expanduser()
    try:
        recipe = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"could not read recipe file: {exc}"
    if not recipe:
        return None, "recipe Markdown or YAML is required"
    return recipe, None


def _handle_exploration_create(config, args) -> int:
    from contents_hub.explorations import ExplorationStore

    request = _join_request(getattr(args, "request", []))
    if not request:
        _emit_json({"ok": False, "error": "exploration request is required"})
        return 1

    recipe, error = _read_recipe_file(getattr(args, "recipe", "") or "")
    if error is not None:
        _emit_json({"ok": False, "error": error})
        return 1

    store = ExplorationStore(config)
    try:
        exploration, strategy = store.create_registered_with_recipe(
            display_name=(getattr(args, "display_name", "") or "").strip()
            or _suggest_exploration_display_name(request),
            original_request=request,
            recipe_markdown=recipe or "",
            target_surfaces=list(getattr(args, "target_surfaces", []) or []),
            lens_ids=list(getattr(args, "lens_ids", []) or []),
        )
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "error": str(exc)})
        return 1

    _emit_json(
        {
            "ok": True,
            "exploration": _exploration_payload(exploration),
            "strategy_version": _strategy_payload(strategy),
        }
    )
    return 0


def _handle_exploration_list(config, args) -> int:
    from contents_hub.explorations import ExplorationStore

    store = ExplorationStore(config)
    explorations = store.list_all(status=getattr(args, "status", None))
    if getattr(args, "output_format", "table") == "json":
        print(
            json.dumps(
                [_exploration_payload(exploration) for exploration in explorations],
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if not explorations:
        print("No explorations found.")
        return 0
    print(f"{'ID':<5} {'STATUS':<12} {'NAME':<32} {'SURFACES':<30}")
    print("-" * 86)
    for exploration in explorations:
        surfaces = ",".join(exploration.target_surfaces)[:28]
        name = exploration.display_name[:30]
        print(f"{exploration.id:<5} {exploration.status:<12} {name:<32} {surfaces:<30}")
    print(f"\nTotal: {len(explorations)}")
    return 0


def _handle_exploration_run(config, args) -> int:
    from contents_hub.explorations import ExplorationStrategyRunner

    try:
        run = asyncio.run(
            ExplorationStrategyRunner(config).run_registered(
                args.exploration_id,
                timeout=float(getattr(args, "timeout", 600.0)),
            )
        )
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json(
            {
                "ok": False,
                "exploration_id": args.exploration_id,
                "error": str(exc),
            }
        )
        return 1

    _emit_json({"ok": run.status == "succeeded", "run": _run_payload(run)})
    return 0 if run.status == "succeeded" else 1


async def _run_registered_explorations(config, explorations, *, timeout: float):
    from contents_hub.explorations import ExplorationStrategyRunner

    runner = ExplorationStrategyRunner(config)
    results = []
    for exploration in explorations:
        try:
            run = await runner.run_registered(exploration.id, timeout=timeout)
            run_payload = _run_payload(run)
            ok = run.status == "succeeded"
            results.append(
                {
                    "ok": ok,
                    "exploration_id": exploration.id,
                    "display_name": exploration.display_name,
                    "run": run_payload,
                    "error": run.error or None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - continue the batch
            results.append(
                {
                    "ok": False,
                    "exploration_id": exploration.id,
                    "display_name": exploration.display_name,
                    "run": None,
                    "error": str(exc),
                }
            )
    return results


def _handle_exploration_run_all(config, args) -> int:
    from contents_hub.explorations import ExplorationStore

    explorations = ExplorationStore(config).list_all(status="registered")
    results = asyncio.run(
        _run_registered_explorations(
            config,
            explorations,
            timeout=float(getattr(args, "timeout_per_exploration", 600.0)),
        )
    )
    succeeded = sum(1 for result in results if result["ok"])
    failed = len(results) - succeeded
    items_found = sum(
        int((result.get("run") or {}).get("items_found", 0))
        for result in results
    )
    items_inserted = sum(
        int((result.get("run") or {}).get("items_inserted", 0))
        for result in results
    )
    _emit_json(
        {
            "ok": failed == 0,
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "items_found": items_found,
            "items_inserted": items_inserted,
            "per_exploration": results,
        }
    )
    return 0 if failed == 0 else 1


def _handle_exploration_delete(config, args) -> int:
    from contents_hub.explorations import ExplorationStore

    deleted = ExplorationStore(config).delete_exploration(args.exploration_id)
    _emit_json({"ok": deleted, "exploration_id": args.exploration_id})
    return 0 if deleted else 1


def _handle_exploration(config, args) -> int:
    cmd = args.exploration_command
    if cmd == "add":
        return _handle_exploration_create(config, args)
    if cmd == "list":
        return _handle_exploration_list(config, args)
    if cmd == "run":
        return _handle_exploration_run(config, args)
    if cmd == "run-all":
        return _handle_exploration_run_all(config, args)
    if cmd == "delete":
        return _handle_exploration_delete(config, args)
    return 1


# ---------------------------------------------------------------------------
# Lens handlers
# ---------------------------------------------------------------------------


def _normalize_lens_id(raw: str) -> str:
    lens_id = (raw or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if (
        not lens_id
        or len(lens_id) > 80
        or lens_id[0] in "._-"
        or any(ch not in allowed for ch in lens_id)
    ):
        raise ValueError(
            "lens_id must be 1-80 chars and use letters, numbers, '.', '_', or '-'"
        )
    return lens_id


def _normalize_keywords(raw_keywords: Sequence[str] | None) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in raw_keywords or []:
        for part in str(raw).split(","):
            keyword = part.strip()
            if keyword and keyword not in seen:
                seen.add(keyword)
                keywords.append(keyword)
    return keywords


def _lens_row_to_payload(row) -> dict[str, Any]:
    try:
        keywords = json.loads(row["keywords"] or "[]")
    except json.JSONDecodeError:
        keywords = []
    if not isinstance(keywords, list):
        keywords = []
    return {
        "id": row["id"],
        "name": row["name"] or "",
        "description": row["description"] or "",
        "keywords": [kw for kw in keywords if isinstance(kw, str)],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_lens_payload(config, lens_id: str) -> dict[str, Any] | None:

    with get_db(config) as conn:
        row = conn.execute(
            """SELECT id, name, description, keywords, enabled, created_at, updated_at
               FROM lenses WHERE id = ?""",
            (lens_id,),
        ).fetchone()
        return _lens_row_to_payload(row) if row is not None else None


def _emit_lens_payload(ok: bool, payload: dict[str, Any] | None, *, error: str = "") -> None:
    body: dict[str, Any] = {"ok": ok, "lens": payload}
    if error:
        body["error"] = error
    _emit_json(body)


def _handle_lens_create(config, args) -> int:

    try:
        lens_id = _normalize_lens_id(args.lens_id)
    except ValueError as exc:
        _emit_lens_payload(False, None, error=str(exc))
        return 1

    now = now_iso()
    name = (getattr(args, "name", "") or "").strip() or lens_id
    description = (getattr(args, "description", "") or "").strip()
    keywords = _normalize_keywords(getattr(args, "keywords", []))
    enabled = 0 if getattr(args, "disabled", False) else 1

    try:
        with get_db(config) as conn:
            conn.execute(
                """INSERT INTO lenses
                   (id, name, description, keywords, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    lens_id,
                    name,
                    description,
                    json.dumps(keywords, ensure_ascii=False),
                    enabled,
                    now,
                    now,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_lens_payload(False, None, error=str(exc))
        return 1

    _emit_lens_payload(True, _get_lens_payload(config, lens_id))
    return 0


def _handle_lens_list(config, args) -> int:

    where = ""
    params: tuple[Any, ...] = ()
    if getattr(args, "enabled", False):
        where = "WHERE enabled = 1"
    elif getattr(args, "disabled", False):
        where = "WHERE enabled = 0"

    with get_db(config) as conn:
        rows = conn.execute(
            f"""SELECT id, name, description, keywords, enabled, created_at, updated_at
                FROM lenses
                {where}
                ORDER BY COALESCE(NULLIF(name, ''), id), id""",
            params,
        ).fetchall()
    lenses = [_lens_row_to_payload(row) for row in rows]

    if getattr(args, "output_format", "table") == "json":
        print(json.dumps(lenses, indent=2, ensure_ascii=False))
        return 0

    if not lenses:
        print("No lenses found.")
        return 0
    print(f"{'ID':<24} {'ENABLED':<8} {'NAME':<28} {'KEYWORDS'}")
    print("-" * 86)
    for lens in lenses:
        keywords = ", ".join(lens["keywords"])[:24]
        print(
            f"{lens['id'][:22]:<24} {str(lens['enabled']).lower():<8} "
            f"{lens['name'][:26]:<28} {keywords}"
        )
    print(f"\nTotal: {len(lenses)}")
    return 0


def _handle_lens_update(config, args) -> int:

    try:
        lens_id = _normalize_lens_id(args.lens_id)
    except ValueError as exc:
        _emit_lens_payload(False, None, error=str(exc))
        return 1

    assignments: list[str] = []
    params: list[Any] = []
    if getattr(args, "name", None) is not None:
        assignments.append("name = ?")
        params.append(str(args.name).strip())
    if getattr(args, "description", None) is not None:
        assignments.append("description = ?")
        params.append(str(args.description).strip())
    if getattr(args, "clear_keywords", False):
        assignments.append("keywords = ?")
        params.append("[]")
    elif getattr(args, "keywords", None) is not None:
        assignments.append("keywords = ?")
        params.append(json.dumps(_normalize_keywords(args.keywords), ensure_ascii=False))
    if getattr(args, "enable", False):
        assignments.append("enabled = ?")
        params.append(1)
    elif getattr(args, "disable", False):
        assignments.append("enabled = ?")
        params.append(0)

    if not assignments:
        payload = _get_lens_payload(config, lens_id)
        _emit_lens_payload(payload is not None, payload, error="" if payload else "lens not found")
        return 0 if payload is not None else 1

    assignments.append("updated_at = ?")
    params.append(now_iso())
    params.append(lens_id)

    with get_db(config) as conn:
        cur = conn.execute(
            f"UPDATE lenses SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )
        conn.commit()
        if cur.rowcount == 0:
            _emit_lens_payload(False, None, error="lens not found")
            return 1

    _emit_lens_payload(True, _get_lens_payload(config, lens_id))
    return 0


def _handle_lens_delete(config, args) -> int:

    try:
        lens_id = _normalize_lens_id(args.lens_id)
    except ValueError as exc:
        _emit_json({"ok": False, "lens_id": args.lens_id, "deleted": False, "error": str(exc)})
        return 1

    with get_db(config) as conn:
        cur = conn.execute("DELETE FROM lenses WHERE id = ?", (lens_id,))
        conn.commit()
        deleted = cur.rowcount > 0
    _emit_json({"ok": deleted, "lens_id": lens_id, "deleted": deleted})
    return 0 if deleted else 1


def _handle_lens(config, args) -> int:
    cmd = args.lens_command
    if cmd == "create":
        return _handle_lens_create(config, args)
    if cmd == "list":
        return _handle_lens_list(config, args)
    if cmd == "update":
        return _handle_lens_update(config, args)
    if cmd == "delete":
        return _handle_lens_delete(config, args)
    return 1


# ---------------------------------------------------------------------------
# Manual raw item handlers
# ---------------------------------------------------------------------------


def _handle_raw_add(config, args) -> int:
    from contents_hub.raw_items import (
        LensNotFoundError,
        MetadataError,
        add_manual_raw_item,
        result_to_payload,
    )

    try:
        result = add_manual_raw_item(
            config,
            value=args.value,
            title=getattr(args, "title", "") or "",
            body=getattr(args, "body", "") or "",
            content_summary=getattr(args, "content_summary", "") or "",
            published_at=getattr(args, "published_at", None),
            metadata_json=getattr(args, "metadata_json", "{}") or "{}",
            lens_ids=list(getattr(args, "lens_ids", []) or []),
        )
    except (ValueError, MetadataError, LensNotFoundError) as exc:
        _emit_json({"ok": False, "inserted": False, "item": None, "error": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "inserted": False, "item": None, "error": str(exc)})
        return 1

    _emit_json(result_to_payload(result))
    return 0


def _handle_raw(config, args) -> int:
    cmd = args.raw_command
    if cmd == "add":
        return _handle_raw_add(config, args)
    return 1


# ---------------------------------------------------------------------------
# Delivery / interaction handlers
# ---------------------------------------------------------------------------


def _handle_delivery(config, args) -> int:
    from contents_hub.interactions import list_outbound_messages, record_outbound_message

    try:
        if args.delivery_command == "record":
            payload = record_outbound_message(
                config,
                platform=args.platform,
                message_id=args.message_id,
                workspace_id=getattr(args, "workspace_id", "") or "",
                channel_id=getattr(args, "channel_id", "") or "",
                thread_id=getattr(args, "thread_id", "") or "",
                payload_type=getattr(args, "payload_type", "raw_item") or "raw_item",
                raw_item_id=getattr(args, "raw_item_id", None),
                digest_id=getattr(args, "digest_id", None),
            )
        elif args.delivery_command == "list":
            payload = list_outbound_messages(
                config,
                platform=getattr(args, "platform", None),
                limit=int(getattr(args, "limit", 50) or 50),
            )
        else:
            payload = {"ok": False, "error": "unknown delivery command"}
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "error": str(exc)})
        return 1

    _emit_json(payload)
    return 0 if bool(payload.get("ok")) else 1


def _event_payload_from_args(args) -> tuple[dict[str, Any] | None, str | None]:
    event_json = (getattr(args, "event_json", "") or "").strip()
    if event_json:
        try:
            payload = json.loads(event_json)
        except json.JSONDecodeError as exc:
            return None, f"event-json must be a JSON object: {exc.msg}"
        if not isinstance(payload, dict):
            return None, "event-json must be a JSON object"
    else:
        payload = {}
    for key in (
        "platform",
        "event_id",
        "workspace_id",
        "channel_id",
        "thread_id",
        "message_id",
        "user_id",
        "kind",
        "value",
    ):
        value = getattr(args, key, None)
        if value:
            payload[key] = value
    return payload, None


def _handle_interaction(config, args) -> int:
    from contents_hub.interactions import handle_interaction, interaction_rules_payload

    try:
        if args.interaction_command == "rules":
            payload = interaction_rules_payload()
        elif args.interaction_command == "handle":
            event, error = _event_payload_from_args(args)
            if error is not None:
                payload = {"ok": False, "error": error}
            else:
                payload = handle_interaction(config, event or {})
        else:
            payload = {"ok": False, "error": "unknown interaction command"}
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "error": str(exc)})
        return 1

    _emit_json(payload)
    return 0 if bool(payload.get("ok")) else 1


def _handle_item(config, args) -> int:
    from contents_hub.signals import open_item, record_signal, signals_payload

    try:
        if args.item_command == "open":
            payload = open_item(
                config,
                args.id_or_url,
                source=getattr(args, "source", "cli") or "cli",
                launch=not bool(getattr(args, "no_launch", False)),
            )
        elif args.item_command == "signal":
            metadata_raw = (getattr(args, "metadata", "") or "").strip()
            metadata: dict[str, Any] | None = None
            if metadata_raw:
                parsed = json.loads(metadata_raw)
                if not isinstance(parsed, dict):
                    raise ValueError("--metadata must be a JSON object")
                metadata = parsed
            payload = record_signal(
                config,
                id_or_url=args.id_or_url,
                signal=args.signal,
                source=getattr(args, "source", "cli") or "cli",
                dedup_key=getattr(args, "dedup_key", "") or "",
                metadata=metadata,
            )
        elif args.item_command == "signals":
            payload = signals_payload(config, args.id_or_url)
        else:
            payload = {"ok": False, "error": "unknown item command"}
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "error": str(exc)})
        return 1

    _emit_json(payload)
    return 0 if bool(payload.get("ok")) else 1


def _handle_query(config, args) -> int:
    from contents_hub.query import context_pack, query_items

    try:
        payload = query_items(
            config,
            args.text,
            limit=int(getattr(args, "limit", 10) or 10),
            since=getattr(args, "since", None),
            lens=getattr(args, "lens", None),
            saved_only=bool(getattr(args, "saved_only", False)),
            min_engagement=getattr(args, "min_engagement", None),
            explain=bool(getattr(args, "explain", False)),
        )
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "error": str(exc)})
        return 1

    if bool(getattr(args, "context", False)):
        print(context_pack(payload), end="")
        return 0 if bool(payload.get("ok")) else 1
    _emit_json(payload)
    return 0 if bool(payload.get("ok")) else 1


def _handle_index(config, args) -> int:
    from contents_hub.query import build_index

    try:
        payload = build_index(
            config,
            rebuild_fts_index=bool(getattr(args, "rebuild_fts", False)),
            backfill_embeddings=bool(getattr(args, "backfill", False)),
            batch_size=int(getattr(args, "batch_size", 32) or 32),
        )
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "error": str(exc)})
        return 1

    _emit_json(payload)
    return 0 if bool(payload.get("ok")) else 1


def _handle_deliver(config, args) -> int:
    from contents_hub.delivery import pending_delivery_payload, prepare_delivery_payload

    try:
        if args.deliver_command == "pending":
            payload = pending_delivery_payload(
                config,
                payload_type=getattr(args, "payload_type", "all") or "all",
                limit=int(getattr(args, "limit", 20) or 20),
                origin=getattr(args, "origin", None),
                lens_matched=bool(getattr(args, "lens_matched", False)),
                first_seen_only=bool(getattr(args, "first_seen_only", False)),
            )
        elif args.deliver_command == "prepare":
            payload = asyncio.run(
                prepare_delivery_payload(
                    config,
                    collect=getattr(args, "collect", "none") or "none",
                    payload_type=getattr(args, "payload_type", "all") or "all",
                    limit=int(getattr(args, "limit", 20) or 20),
                    origin=getattr(args, "origin", None),
                    lens_matched=bool(getattr(args, "lens_matched", False)),
                    first_seen_only=bool(getattr(args, "first_seen_only", False)),
                    timeout_per_sub=float(getattr(args, "timeout_per_sub", 120.0)),
                    concurrency=int(getattr(args, "concurrency", 1) or 1),
                    collect_all_active_func=collect_all_active,
                    collect_all_due_func=collect_all_due,
                )
            )
        else:
            payload = {"ok": False, "error": "unknown deliver command"}
    except Exception as exc:  # noqa: BLE001 - CLI failure surface
        _emit_json({"ok": False, "error": str(exc)})
        return 1

    _emit_json(payload)
    return 0 if bool(payload.get("ok")) else 1


# ---------------------------------------------------------------------------
# Daemon handlers — UNCHANGED OUTPUT CONTRACT (R-U2.3 / INV-10)
# ---------------------------------------------------------------------------


def _handle_daemon(config, args) -> int:
    from contents_hub.daemon import DaemonTickResult, daemon_loop, daemon_tick

    cmd = args.daemon_command
    if cmd == "run":
        # R-U2.3 / INV-10: daemon run output remains text by default with
        # an optional --json flag.  Field names map onto the post-refactor
        # api.DaemonTickResult shape (total / new / skipped / errors /
        # per_subscription); the textual layout is preserved (Checked …,
        # New items …, Duration …, per-sub bullets).
        result = asyncio.run(daemon_tick(config))
        if getattr(args, "json_output", False):
            print(json.dumps({
                "total": int(getattr(result, "total", 0)),
                "new": int(getattr(result, "new", 0)),
                "skipped": int(getattr(result, "skipped", 0)),
                "errors": int(getattr(result, "errors", 0)),
                "duration_seconds": round(float(getattr(result, "duration_seconds", 0.0)), 2),
            }, indent=2))
        else:
            total = int(getattr(result, "total", 0))
            errs = int(getattr(result, "errors", 0))
            print(f"Checked {total} subscriptions ({total - errs} ok, {errs} error)")
            print(f"New items: {int(getattr(result, 'new', 0))}, Skipped: {int(getattr(result, 'skipped', 0))}")
            print(f"Duration: {float(getattr(result, 'duration_seconds', 0.0)):.1f}s")
            for ps in getattr(result, "per_subscription", []) or []:
                ok_flag = bool(getattr(ps, "ok", False))
                status = "ok" if ok_flag else "x"
                err = getattr(ps, "error", "") or ""
                new_items = int(getattr(ps, "new_items", 0))
                detail = err if err else f"+{new_items} new"
                label = getattr(ps, "url", "") or f"sub#{getattr(ps, 'subscription_id', '?')}"
                print(f"  [{status}] {label}: {detail}")
        return 0

    if cmd == "loop":
        interval = getattr(args, "interval", 30)

        def _print(r: DaemonTickResult) -> None:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            total = int(getattr(r, "total", 0))
            new = int(getattr(r, "new", 0))
            errs = int(getattr(r, "errors", 0))
            print(f"[{ts}] {total} subs, +{new} new, {errs} errors")

        asyncio.run(daemon_loop(config, interval_minutes=interval, on_complete=_print))
        return 0

    if cmd == "install":
        from contents_hub.launchd import install
        print(install(config))
        return 0
    if cmd == "uninstall":
        from contents_hub.launchd import uninstall
        print(uninstall())
        return 0
    if cmd == "status":
        from contents_hub.launchd import status
        print(status())
        return 0
    return 1


# ---------------------------------------------------------------------------
# Web handler
# ---------------------------------------------------------------------------


def _handle_web(config, args) -> int:
    import importlib.util
    missing = [m for m in ("uvicorn", "fastapi") if importlib.util.find_spec(m) is None]
    if missing:
        print(f"ERROR: missing packages: {', '.join(missing)}", file=sys.stderr)
        return 1

    import uvicorn
    from contents_hub.web.app import create_app

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    logging.getLogger("contents_hub").setLevel(logging.INFO)

    log_path = config.meta_path / "web.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(fh)

    app = create_app(config)
    port = getattr(args, "port", 8585)
    print(f"Starting contents-hub dashboard on http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    return 0


# ---------------------------------------------------------------------------
# Browser handler
# ---------------------------------------------------------------------------


def _print_browser_result(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False))
        return

    status = payload.get("status", "unknown")
    profile = payload.get("profile") or "contents-hub"
    if payload.get("error"):
        print(f"browser {status}: {payload['error']}")
        return
    if "state" in payload:
        print(f"browser profile {profile}: {payload['state']}")
        return
    if payload.get("url"):
        print(f"browser {status}: {payload['url']} (profile: {profile})")
        return
    print(f"browser {status} (profile: {profile})")


def _handle_browser(config, args) -> int:
    from contents_hub.chromux import (
        CHROMUX_PROFILE_NAME,
        chromux_profile_state,
        kill_chromux_profile,
        open_chromux_headed,
    )

    cmd = args.browser_command
    json_output = bool(getattr(args, "json_output", False))
    profile = CHROMUX_PROFILE_NAME

    if cmd == "status":
        payload = {
            "ok": True,
            "profile": profile,
            "state": chromux_profile_state(profile),
        }
        _print_browser_result(payload, json_output=json_output)
        return 0

    if cmd == "open":
        payload = open_chromux_headed(
            getattr(args, "url", None),
            session="contents-hub",
            confirmed=bool(getattr(args, "confirm", False)),
            profile=profile,
        )
        payload = {
            "ok": payload.get("status") not in {"error", "needs_confirm"},
            "profile": profile,
            **payload,
        }
        _print_browser_result(payload, json_output=json_output)
        return 0 if payload.get("ok") and payload.get("status") != "needs_confirm" else 1

    if cmd == "kill":
        payload = kill_chromux_profile(profile)
        payload = {"ok": payload.get("status") != "error", "profile": profile, **payload}
        _print_browser_result(payload, json_output=json_output)
        return 0 if payload.get("ok") else 1

    return 1


# ---------------------------------------------------------------------------
# Init handler
# ---------------------------------------------------------------------------


def _handle_init(args) -> int:
    from contents_hub.db import init_db
    from contents_hub.config import WikiConfig

    target = Path(args.path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    from contents_hub.naming import CLI_COMMAND, METADATA_DIR, PYTHON_PACKAGE

    meta = target / METADATA_DIR.canonical
    meta.mkdir(exist_ok=True)
    (target / "sources").mkdir(exist_ok=True)

    cfg = WikiConfig(vault_path=target)
    init_db(cfg)

    print(f"Initialized vault at {target}")
    print(f"  meta:    {meta}")
    print(f"  sources: {target / 'sources'}")
    print(f"  db:      {meta / 'state.db'}")
    print("\nNext: register subscriptions via the web dashboard:")
    print(f"      python -m {PYTHON_PACKAGE.canonical} --vault {target} web")
    print(f"      {CLI_COMMAND.canonical} --vault {target} web")
    return 0


# ---------------------------------------------------------------------------
# Logging — file-only for fetch/tick (INV-9), console allowed elsewhere
# ---------------------------------------------------------------------------


def _attach_cli_logging(config, *, quiet_stdout: bool) -> None:
    """Configure logging for a CLI invocation.

    INV-9 / R-U5.1: when ``quiet_stdout`` is True (i.e. the command emits
    JSON to stdout), the root logger receives ONLY a file handler
    pointed at the resolved metadata ``cli.log``.  No StreamHandler attaches to
    stdout/stderr so library logging cannot pollute the JSON.

    For non-JSON commands (``daemon run`` text mode, ``sub list``, etc.)
    we still attach the file handler but allow library code to log to
    stderr via the default behavior.
    """
    root = logging.getLogger()
    # Avoid duplicating handlers in long-lived processes / tests.
    have_file = any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", "").endswith("cli.log")
        for h in root.handlers
    )
    if root.level == logging.WARNING or root.level == 0:
        root.setLevel(logging.INFO)

    if not have_file:
        try:
            lp = config.meta_path / "cli.log"
            lp.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(lp, encoding="utf-8")
            fh.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            root.addHandler(fh)
        except Exception:
            # Logging setup must not abort the command.
            pass

    if quiet_stdout:
        # Strip any pre-existing stream handlers so JSON stays clean.
        for h in list(root.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                root.removeHandler(h)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return _handle_init(args)

    try:
        config = load_config(args.vault)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    quiet_stdout = args.command in (
        "fetch",
        "fetch-all",
        "tick",
        "digest",
        "explore",
        "exploration",
        "lens",
        "raw",
        "delivery",
        "interaction",
        "deliver",
        "browser",
    ) or (
        args.command == "sub" and getattr(args, "sub_command", "") == "add"
    )
    _attach_cli_logging(config, quiet_stdout=quiet_stdout)

    if args.command == "sub":
        return _handle_sub(config, args)
    if args.command == "fetch":
        return _handle_fetch(config, args)
    if args.command == "fetch-all":
        return _handle_fetch_all(config, args)
    if args.command == "tick":
        return _handle_tick(config)
    if args.command == "explore":
        return _handle_exploration_create(config, args)
    if args.command == "exploration":
        return _handle_exploration(config, args)
    if args.command == "lens":
        return _handle_lens(config, args)
    if args.command == "raw":
        return _handle_raw(config, args)
    if args.command == "delivery":
        return _handle_delivery(config, args)
    if args.command == "interaction":
        return _handle_interaction(config, args)
    if args.command == "item":
        return _handle_item(config, args)
    if args.command == "query":
        return _handle_query(config, args)
    if args.command == "index":
        return _handle_index(config, args)
    if args.command == "deliver":
        return _handle_deliver(config, args)
    if args.command == "daemon":
        return _handle_daemon(config, args)
    if args.command == "web":
        return _handle_web(config, args)
    if args.command == "browser":
        return _handle_browser(config, args)
    if args.command == "digest":
        return _handle_digest(config, args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
