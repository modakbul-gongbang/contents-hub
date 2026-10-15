# Architecture

contents-hub has four boundaries.

## 1. Vault State

The vault stores user-owned content and local runtime state.

```text
<vault>/
  .contents-hub/
    state.db
    cli.log
    daemon.log
  sources/
  digests/
```

SQLite owns subscriptions, schedules, raw items, Lenses, digests, saved items,
outbound message mappings, and interaction events.

Promoted files under `sources/` are processed summary notes for human reading.
Full captured page text, transcripts, and raw HTML fragments stay in SQLite
`raw_items.body` so items can be reprocessed without dumping raw capture into
the Obsidian vault.

## 2. Collection And Digest

Subscriptions and manual raw items become rows in `raw_items`. Lenses attach
topic-specific summaries. Digest generation reads Lens-routed raw items and
stores digest rows.

Deterministic sources should use HTTP/RSS paths. Browser or agent-backed
collection should use the `AgentRunner` protocol and the selected optional
runner.

The `aside` runner invokes a local CLI through a one-shot owned supervisor.
The supervisor reads structured assistant events and releases its CLI process, temporary event file, and owned browser session on success, failure, timeout, cancellation, or owner input EOF.
Browser prompts select backend instructions separately from shared source recipes.
Aside selection bypasses chromux preparation and social-platform direct DOM collection, while deterministic HTTP/feed paths and LIST, DIFF, CONTENT, and persistence stay in place.
Aside does not expose contents-hub's custom persistence tools; text synthesis uses deterministic fallbacks and autonomous exploration reports an unsupported-runner error.

## 3. Delivery And Interaction

`deliver pending` emits adapter-ready cards. Channel adapters send cards and
record returned message ids with `delivery record`.

When users interact with a message, adapters normalize the event and call
`interaction handle`. contents-hub resolves the message mapping, logs the
event, and applies an idempotent content action.

The core action set is `save`, `save_and_promote`, `archive`, and `mark_read`.
Repeated events return stable no-op or already-handled JSON instead of creating
duplicate rows or source notes.

## 4. Runtime Boundary

contents-hub owns local content state and CLI contracts.

External runtimes own:

- scheduling
- webhook or gateway lifecycle
- channel credentials
- message transport
- account permissions

This lets cron, launchd, Hermes, OpenClaw, Claude Code, Codex, and other loops
drive the same core.
