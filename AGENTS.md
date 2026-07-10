# AGENTS.md

This file guides Codex and other coding agents working in this repository.

## Operational Docs

- Public setup and vault targeting: `docs/initialization.md`
- Skill-first install and CLI fallback: `install.md`
- Agent operating skill: `skills/contents-hub/SKILL.md`
- Architecture and runtime boundaries: `docs/architecture.md`,
  `docs/runtime-matrix.md`, `docs/schedulers.md`, `docs/channels.md`,
  `docs/launch.md`

When the CLI surface changes, update README, docs, and the relevant skill in
the same change.

## What This Is

contents-hub is a Python CLI and FastAPI dashboard that collects subscriptions,
manual raw items, Lenses, digests, delivery mappings, and interaction events
into a user-owned vault.

The core package is `contents_hub`; the public command is `contents-hub`.
Base operation must not require provider SDKs, channel SDKs, or credentials.
Provider-backed browser/agent behavior belongs behind the `AgentRunner`
boundary and optional extras.

## Common Commands

Use the `./dev` wrapper; it runs inside the `uv` environment.

```bash
./dev sync              # uv sync --all-extras
./dev test              # pytest
./dev web               # dashboard on http://localhost:8585
./dev daemon run        # one daemon tick
```

Single test:

```bash
./dev test tests/test_file.py::test_name
```

`pyproject.toml` sets `asyncio_mode = "auto"`, so async tests do not need
`@pytest.mark.asyncio`.

## CLI Surface

`contents-hub` and `python -m contents_hub` expose:

- `init [path]` - scaffold a vault
- `sub {add,remove,list}` - subscription CRUD
- `raw add <url_or_text>` - manual read-later queue insertion
- `fetch <id_or_url>` - fetch one subscription
- `fetch-all` - fetch every active or error subscription
- `tick` - collect due subscriptions
- `daemon {run,loop,install,uninstall,status}` - background collector
- `browser {open,status,kill}` - open or inspect the fixed contents-hub browser
  profile for manual sign-in
- `digest` - one-shot DB-backed digest generation
- `lens {create,list,update,delete}` - Lens definitions
- `explore` and `exploration {add,list,run,run-all,delete}` - exploration
  lifecycle
- `deliver pending` - adapter-ready raw item or digest cards
- `deliver prepare` - optional `none|fetch-all|tick` collection plus adapter-ready cards
- `delivery {record,list}` - outbound message mappings
- `interaction handle` and `interaction rules list` - normalized interaction
  logging and action routing
- `item {open,signal,signals}` - engagement signal ledger
- `query` - knowledge-store search (keyword + optional semantic + engagement
  ranking); `--context` emits a markdown context pack
- `index` - build the FTS index and optional embedding vectors
- `web [--port N]` - dashboard

Every subcommand accepts `--vault PATH`. Resolution order is:

1. `--vault PATH`
2. `CONTENTS_HUB_VAULT`
3. current working directory

## Vault Layout

```text
<vault>/
  .contents-hub.yaml
  .contents-hub/
    state.db
    cli.log
    daemon.log
    web.log
    plugins/
  sources/
  digests/
```

`sources/` and `digests/` are user data and are ignored by this repo.

## Architecture

The app has four practical boundaries:

- Vault state: SQLite, logs, metadata, generated user content.
- Collection and digest: subscriptions, raw items, Lenses, digest rows.
- Delivery and interaction: adapter-ready payloads, outbound message refs,
  normalized events, idempotent content actions.
- Runtime: external schedulers and gateways own cron, webhooks, credentials,
  and message transport.

Keep contents-hub a local state engine. Cron, launchd, Hermes, OpenClaw, Claude
Code, Codex, Slack, Discord, and Telegram should drive it through CLI contracts
instead of becoming mandatory runtime dependencies.

## Runners

- `contents_hub.runners.base.AgentRunner` defines
  `run(prompt, *, max_turns, timeout) -> str`.
- `contents_hub.runners.no_agent.NoAgentRunner` is the default runtime-neutral
  runner and returns actionable errors for agent-only features.
- `contents_hub.runners.claude_sdk.ClaudeSDKRunner` is optional and must only be
  imported through the runner selection path or direct optional usage.
- `CONTENTS_HUB_AGENT_RUNNER=none|claude-sdk` selects the default runner.

Do not import provider SDKs outside optional runner modules. Core imports should
work after `uv sync` without optional extras.

## Database

SQLite lives at `<vault>/.contents-hub/state.db`. Schema is inline in
`contents_hub.db`; use the existing migration style when adding tables or
columns.

Important tables include subscriptions, schedules, raw items, Lenses, digests,
saved items, outbound message mappings, and interaction events. Interaction
events should be logged even when the resulting action is a safe no-op.

## Delivery And Interaction

Adapters should follow this flow:

1. `contents-hub deliver prepare --collect fetch-all --payload-type raw_item --origin subscription --lens-matched --first-seen-only --format json` for collect-and-send loops, or `contents-hub deliver pending --format json` when collection already ran
2. send each returned card through the channel
3. `contents-hub delivery record ...`
4. normalize the user interaction
5. `contents-hub interaction handle --event-json '<json>' --format json`

The default reaction rules are:

- `👍`, `⭐`, `❤️`, `❤` -> `save_and_promote`
- `✅` -> `mark_read`
- `🗑` -> `archive`

Platform reaction *names* resolve through the same table (Slack sends
`star`, `+1`, `white_check_mark`, `wastebasket`); `interaction rules list`
exposes the alias map.

Action handlers must be idempotent. Replaying the same event must not duplicate
saved rows, promoted source notes, or engagement-signal rows.

Delivery cards carry Lens-generated output verbatim: per-lens `lenses`
entries (name, summary, bullets), `markdown`/`plain_text` built from that
content, a per-Lens `groups` list, and a `delivery_instruction`. Adapters
and agents must send card content as-is — never re-summarize it.

## Knowledge Store

Engagement signals (impression < open < read < save < promote; archive and
unsave negative) live in the append-only `item_signals` ledger
(`contents_hub.signals`), written by interaction handlers, `delivery
record`, the web `/r/{id}` open-redirect, and `item` CLI commands.
`contents-hub query` ranks items by FTS5 keyword match fused with optional
embeddings (`CONTENTS_HUB_EMBEDDINGS=openai-compat` + URL/MODEL/KEY env)
and the time-decayed engagement score; defaults are tunable in the
`query:` block of `.contents-hub.yaml`. Design: `docs/knowledge-store-plan.md`.

## Web UI

FastAPI + Jinja2 lives under `contents_hub.web`.

Key routes:

- `GET /` - overview
- `GET /subscriptions` and subscription mutation routes
- `GET /digests`, `GET /digests/{id}`
- `GET /saved`
- raw item save/promote/archive routes
- Lens inbox routes

When changing action semantics, preserve web route behavior with regression
tests.

## Conventions

- Prefer existing module patterns and CLI JSON shapes.
- Keep core imports lightweight.
- Use structured parsers and existing helpers rather than ad hoc string parsing.
- Preserve promoted source-note immutability.
- Use `contents_hub.frontmatter` for Markdown frontmatter.
- Keep provider SDK and channel SDK imports isolated behind optional paths.
- For browser work, use `chromux` when available and preserve reusable browser
  lessons as recipes or playbooks rather than one-off notes.
