---
name: contents-hub
description: Use when the user asks how to use the contents-hub CLI, initialize or target a vault, manage subscriptions, fetch/tick sources, run explorations, manage Lenses, run the daemon, produce digests, generate delivery payloads, or handle channel interactions.
version: 0.2.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [contents, vault, cli, subscriptions]
    category: productivity
---

# contents-hub CLI

Use this skill for practical `contents-hub` CLI and vault operations. Prefer
current command help and concrete commands over memory.

This skill is intentionally self-contained. If the CLI is not installed,
install it from the public repo, then use the CLI for all operations. Do not
require a second contents-hub skill for exploration design.

## Setup Mode

Use setup mode when the user asks to install contents-hub, initialize a vault,
configure Hermes/OpenClaw, add cron jobs, configure gateway delivery, choose a
browser profile, migrate an existing vault, or reset existing runtime state.

Before changing runtime state:

1. Identify the runtime: Hermes, OpenClaw, shell cron, launchd, Codex, Claude
   Code, or another gateway.
2. Confirm the vault path.
3. Check whether `.contents-hub/state.db` already exists.
4. Back up `state.db` before destructive or reset-like work.
5. Inspect existing subscriptions, Lenses, and delivery mappings.
6. Inspect existing runtime schedules before creating new ones.

Runtime runbooks:

- Hermes profile/gateway/cron setup: `docs/hermes-setup.md`
- OpenClaw skill/task/gateway setup: `docs/openclaw-setup.md`
- Generic scheduler boundary: `docs/schedulers.md`
- Channel adapter contract: `docs/channels.md`

For Hermes Telegram-style operation, distinguish final-response digest delivery
from adapter delivery. Reactions only work when a per-card adapter preserves the
returned platform message id with `delivery record` before forwarding events to
`interaction handle`.

Keep one public skill. Do not ask the user to install a separate init skill.
Use this Setup Mode plus the runtime runbooks for installation and operations.

## First Check

When accuracy matters, inspect the installed CLI before answering:

```bash
contents-hub --help
contents-hub sub --help
contents-hub raw add --help
contents-hub fetch --help
contents-hub fetch-all --help
contents-hub daemon --help
contents-hub digest --help
contents-hub browser --help
contents-hub deliver pending --help
contents-hub deliver prepare --help
contents-hub delivery record --help
contents-hub interaction handle --help
contents-hub query --help
contents-hub item --help
contents-hub index --help
```

If the command is missing, install or update a durable checkout:

```bash
INSTALL_DIR="${CONTENTS_HUB_DIR:-$HOME/contents-hub}"
REPO_URL="https://github.com/yansfil/contents-hub"

if [ -d "$INSTALL_DIR/.git" ]; then
  cd "$INSTALL_DIR"
  git pull --ff-only
else
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

uv tool install -e "$PWD" --force
uv tool update-shell
contents-hub --help
```

If the checkout has uncommitted changes, stop and report the dirty files
instead of overwriting them.

## Runtime Skill Registration

This repo stores the skill at `skills/contents-hub/SKILL.md`.

- OpenClaw: if using native skill install, clone the repo first and run
  `openclaw skills install ./skills/contents-hub --as contents-hub --global`
  from the checkout. OpenClaw Git installs expect `SKILL.md` at repo root, so
  `openclaw skills install git:yansfil/contents-hub` is not the right shape.
- Hermes: install the skills.sh path with
  `hermes skills install skills-sh/yansfil/contents-hub/skills/contents-hub --yes`.
  Start a new session after installing. Do not point Hermes at only the repo URL;
  the target must include the nested skill path.

## Vault Targeting

Every vault command except `init` accepts `--vault PATH`. Resolution order is:

1. `--vault PATH`
2. `CONTENTS_HUB_VAULT`
3. current working directory

`init` initializes its positional path. Pass the path explicitly:
`contents-hub init "$HOME/contents-vault"` or
`contents-hub --vault "$HOME/contents-vault" init "$HOME/contents-vault"`.

For repo-outside usage:

```bash
export CONTENTS_HUB_VAULT="$HOME/contents-vault"
contents-hub sub list
```

Initialize a vault once:

```bash
contents-hub --vault "$HOME/contents-vault" init "$HOME/contents-vault"
```

The metadata paths are:

- `.contents-hub/`
- `.contents-hub.yaml`

## Common Commands

Manual URL/text is the shortest reliable first-launch path. When no Lens exists,
`raw add` creates and attaches `manual-inbox` automatically so the item can be
digested. RSS is reliable after the user creates at least one matching Lens.
Treat browser-backed social sources and explorations as optional paths that may
need manual browser sign-in.

Promotion writes processed summary notes under `sources/`. Full fetched or
pasted bodies stay in SQLite `raw_items.body` for later reprocessing.

List subscriptions:

```bash
contents-hub sub list
contents-hub sub list --format json
contents-hub sub list --type rss.feed --format json
```

Add a subscription:

```bash
contents-hub sub add <rss-feed-url> --type rss.feed --title "Example"
contents-hub sub add https://www.youtube.com/@example --type youtube.channel
contents-hub sub add https://x.com/example --type x.profile
contents-hub sub add https://www.threads.net/@example --type threads.profile
contents-hub sub add https://example.substack.com --type substack.publication
contents-hub sub add https://example.com --type webpage --collection-prompt "Collect only release notes."
```

Remove a subscription:

```bash
contents-hub sub remove https://example.com/feed.xml
```

Fetch one subscription by id or URL:

```bash
contents-hub fetch 15
contents-hub fetch <rss-feed-url> --max-items 10
```

Fetch every active or error subscription:

```bash
contents-hub fetch-all
contents-hub fetch-all --timeout-per-sub 120
contents-hub fetch-all --concurrency 3
```

Collect due subscriptions:

```bash
contents-hub tick
contents-hub daemon run --json
```

Run or install the background loop:

```bash
contents-hub daemon loop --interval 30
contents-hub daemon install
contents-hub daemon status
contents-hub daemon uninstall
```

## Browser Profile

For login-required browser-backed sources, open the dedicated contents-hub
browser profile and let the user sign in manually:

```bash
contents-hub browser open
contents-hub browser open https://x.com/login
contents-hub browser status
contents-hub browser kill
```

The profile name is fixed to `contents-hub`. Do not ask for site-specific login
commands; open the requested site and let the user complete authentication in
Chrome. contents-hub does not store passwords or tokens.

Launch the dashboard:

```bash
contents-hub web --port 8585
```

Produce a digest:

```bash
contents-hub digest
```

## Manual Raw Items

Add ad-hoc reading items without creating a subscription:

```bash
contents-hub raw add https://example.com/article
contents-hub raw add https://example.com/article --title "Read later" --summary "Why this matters"
contents-hub raw add "A pasted note" --title "Manual note"
contents-hub raw add https://example.com/article --lens-id ai-research
```

`raw add` writes a manual `raw_items` row. URL input is canonicalized for
dedupe; text input uses a stable content key. When `--lens-id` is omitted,
the item attaches only to enabled Lenses whose keywords match the item's
title/summary/body (so a climbing note never lands in an AI lens). When no
Lens matches, contents-hub creates and attaches `manual-inbox`.

## Lens Management

```bash
contents-hub lens create ai-research --name "AI research" --description "AI papers and applied systems" --keyword "AI" --keyword "agents"
contents-hub lens list
contents-hub lens list --format json
contents-hub lens list --enabled
contents-hub lens update ai-research --description "Updated criteria" --keyword "agent workflow"
contents-hub lens update ai-research --clear-keywords
contents-hub lens update ai-research --enable
contents-hub lens update ai-research --disable
contents-hub lens delete ai-research
```

Lens ids are slugs. Keywords are repeatable; comma-separated values are also
split.

## Explorations

Register an exploration from an approved recipe:

```bash
contents-hub explore "Find practical agent workflow writeups" --recipe recipe.yaml
contents-hub exploration add "Find practical agent workflow writeups" --recipe recipe.yaml
```

Run registered explorations:

```bash
contents-hub exploration list
contents-hub exploration run 3
contents-hub exploration run 3 --timeout 600
contents-hub exploration run-all
contents-hub exploration run-all --timeout-per-exploration 600
```

Explorations are foreground/manual runs unless an external scheduler invokes
them.

When designing a new exploration, keep the workflow lightweight:

1. Clarify target surfaces, recency, ranking signals, and skip rules.
2. Probe important browser surfaces with `chromux` when available.
3. Produce a small `recipe.yaml` with `goal`, `keep`, `skip`, `sources`, and
   optional `runtime` limits.
4. Show the recipe summary before registering it.
5. Do not run `exploration add`, `exploration run`, or `exploration run-all`
   until the user explicitly confirms persistence or execution.

Minimal recipe shape:

```yaml
goal: Collect useful raw_items for the request.
keep:
  - Concrete item rule.
skip:
  - Explicit exclusion.
sources:
  - surface: web
    search: "agent workflow examples"
runtime:
  max_minutes: 10
  target_items: 12
```

## Delivery Payloads

Generate adapter-ready payloads:

```bash
contents-hub deliver pending --format json
contents-hub deliver pending --payload-type raw_item --limit 5 --format json
contents-hub deliver pending --payload-type raw_item --origin subscription --lens-matched --first-seen-only --limit 20 --format json
contents-hub deliver prepare --collect fetch-all --payload-type raw_item --origin subscription --lens-matched --first-seen-only --limit 20 --format json
contents-hub deliver prepare --collect tick --payload-type raw_item --format json
contents-hub deliver pending --payload-type digest --limit 5 --format json
```

`deliver pending` only selects currently pending cards. `deliver prepare` can
run `fetch-all`, `tick`, or no collector first, then emits one JSON object with
`collector` and `delivery`. The nested delivery JSON contains `payload_type`,
stable raw item or digest ids, titles, URLs, summaries, `delivery_key`,
`dedupe_key`, `plain_text`, `markdown`, per-lens `lenses` entries (name,
summary, bullets), a per-Lens `groups` list, and a `delivery_instruction`.

**Verbatim rule (mandatory):** the card `markdown`/`plain_text` already
contains the Lens-generated output; digest cards carry the full digest in
`content_md`. Send that content to Telegram/Slack/Discord exactly as-is.
Never summarize, rewrite, shorten, merge, or reorder cards — re-summarizing
destroys the Lens layer. Deliver one Lens group at a time (iterate
`groups`) so each topic stays separated in the channel, and dedupe sends by
`delivery_key` — a multi-lens item appears in every matching group but must
be sent only once.

contents-hub decides which cards are deliverable. Use `--origin subscription`,
`--lens-matched`, and `--first-seen-only` instead of writing runtime SQLite
queries for raw item candidates.

Record a sent message:

```bash
PENDING="$(contents-hub deliver pending --format json)"
RAW_ITEM_ID="$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); print(p["items"][0]["raw_item_id"])' "$PENDING")"
contents-hub delivery record \
  --platform demo \
  --channel-id demo-channel \
  --message-id demo-message \
  --payload-type raw_item \
  --raw-item-id "$RAW_ITEM_ID"
```

For digest cards, use the `digest_id` from `deliver pending --payload-type
digest --format json` and record with `--payload-type digest --digest-id
<digest_id>`.

List mappings:

```bash
contents-hub delivery list
contents-hub delivery list --platform demo --limit 20
```

## Interactions

Handle a normalized event:

```bash
contents-hub interaction handle \
  --platform demo \
  --channel-id demo-channel \
  --message-id demo-message \
  --user-id demo-user \
  --kind reaction \
  --value "⭐" \
  --format json
```

Or pass the event as JSON:

```bash
contents-hub interaction handle --event-json '{"platform":"demo","channel_id":"demo-channel","message_id":"demo-message","user_id":"demo-user","kind":"reaction","value":"⭐"}' --format json
```

Inspect rules:

```bash
contents-hub interaction rules list
```

Default reaction rules:

- `👍`, `⭐`, `❤️`, `❤` -> `save_and_promote`
- `✅` -> `mark_read`
- `🗑` -> `archive`

Slack sends reaction names instead of emoji; `star`, `+1`/`thumbsup`,
`heart`, `white_check_mark`, and `wastebasket` resolve through the same
rules, so pass Slack's `event.reaction` straight through as `value`.
`interaction rules list` returns the `name_aliases` map.

Unsupported reactions, unknown messages, and repeated events should return
machine-readable safe results instead of crashing.

## Knowledge Store (query, item, index)

Every handled reaction, delivered card, and dashboard link click also lands
in the engagement signal ledger (`item_signals`), which powers ranked
search over everything collected:

```bash
contents-hub query "vector databases" --limit 10 --format json
contents-hub query "vector databases" --since 90d --lens ai-research
contents-hub query "vector databases" --saved-only --explain
contents-hub query "vector databases" --context   # markdown context pack
```

Results fuse FTS5 keyword rank, an optional semantic ranker, and the
time-decayed engagement score (open +1, read +0.5, save +3, promote +5,
archive -2). Record signals directly when acting on behalf of the user:

```bash
contents-hub item open 42                 # record open + launch URL
contents-hub item open 42 --no-launch
contents-hub item signal 42 --signal note --metadata '{"comment":"revisit"}'
contents-hub item signals 42              # ledger + engagement score
```

Build the index (FTS is automatic; embeddings are opt-in):

```bash
contents-hub index --rebuild-fts
CONTENTS_HUB_EMBEDDINGS=openai-compat \
CONTENTS_HUB_EMBEDDINGS_URL=http://localhost:11434/v1 \
CONTENTS_HUB_EMBEDDINGS_MODEL=nomic-embed-text \
contents-hub index --backfill
```

Without an embedding provider, `query` runs keyword-only and reports
`"rankers": ["fts"]`; nothing leaves the machine.

## Channel Adapter Shape

Adapters for Telegram, Slack, Discord, Hermes, OpenClaw, or another gateway
should:

1. call `deliver prepare --collect fetch-all --format json` or `deliver pending --format json`
2. send each card's `markdown`/`plain_text` through the platform **verbatim**,
   one Lens group at a time (`delivery.groups`)
3. call `delivery record` with the returned message id
4. normalize platform events to the shared event shape
5. call `interaction handle --event-json ... --format json`

Core contents-hub should not import channel SDKs during base import.
Do not claim built-in Telegram, Slack, or Discord bot packages unless such an
external adapter package is explicitly installed.
Final-response-only delivery is insufficient for reactions because it does not
record per-card platform message ids. Telegram SDKs and credentials remain
outside contents-hub core.

## Runner Selection

Default runtime-neutral behavior:

```bash
CONTENTS_HUB_AGENT_RUNNER=none contents-hub fetch-all
```

Claude-backed features require the optional extra and explicit runner selection:

```bash
uv sync --extra claude --extra dev
CONTENTS_HUB_AGENT_RUNNER=claude-sdk contents-hub fetch 15
```

Agent-only operations should fail with actionable errors in no-agent mode.

## Source Types

Canonical source types:

- `rss.feed`
- `youtube.channel`
- `github.releases`
- `x.profile`
- `linkedin.profile`
- `threads.profile`
- `substack.publication`
- `substack.tag`
- `medium.publication`
- `reddit.subreddit`
- `webpage`

The CLI can infer many URLs, but agents should pass `--type rss.feed` for RSS
feeds because many valid feed URLs do not contain `.xml`, `.rss`, `.atom`, or
`/feed`.

## Output Contract

Commands intended for automation should emit JSON on stdout when `--format json`
or the command's JSON mode is used. Avoid parsing human-oriented text when a
JSON option exists.

When filtering JSON output in shell, avoid piping into a Python here-doc because
the here-doc becomes Python stdin. Prefer `python3 -c`, a temp file, or calling
the CLI from Python with `subprocess.check_output()`.

## Logs

Inspect logs under the resolved vault metadata directory:

```bash
tail -n 100 .contents-hub/cli.log
tail -n 100 .contents-hub/web.log
```

## Answering Rules

- In Korean conversations, answer in Korean.
- If the user asks what arguments to pass, start with the exact command shape.
- If the user asks whether it works now, run a smoke command when local access
  is available.
- For current command syntax, trust `contents-hub --help` over memory.
