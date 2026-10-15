# Initialization

This document explains the smallest durable setup: install the CLI, initialize a
vault, and make repo-outside commands target the same vault.

## 1. Install The CLI

For agent-operated setup, install the single `contents-hub` skill first. The
skill tells the agent how to install the CLI, initialize a vault, and run smoke
tests.

From a checkout:

```bash
uv sync
uv tool install -e "$PWD" --force
uv tool update-shell
contents-hub --help
python -m contents_hub --help
```

For local Aside browser subscription collection, install its CLI, open Aside, and sign in to the sites you want to collect.
The base Python install is sufficient:

```bash
aside --version
CONTENTS_HUB_AGENT_RUNNER=aside contents-hub --vault ~/contents-vault fetch-all
```

See [Aside setup](aside.md), including browser login and supported CLI requirements.
Claude-backed browser/agent features are also optional:

```bash
uv sync --extra claude --extra dev
CONTENTS_HUB_AGENT_RUNNER=claude-sdk contents-hub --help
```

## 2. Initialize A Vault

Run this once for a new vault:

```bash
contents-hub --vault "$HOME/contents-vault" init "$HOME/contents-vault"
```

The vault receives:

```text
<vault>/
  .contents-hub/
    state.db
  sources/
```

`sources/` contains processed summary notes. Full fetched or pasted bodies stay
in SQLite `raw_items.body` for later reprocessing.

Vault configuration uses `.contents-hub.yaml` when present.

## 3. Target The Same Vault From Anywhere

Every vault command except `init` accepts `--vault PATH`. Resolution order is:

1. `--vault PATH`
2. `CONTENTS_HUB_VAULT`
3. current working directory

`init` initializes its positional path. Pass the path explicitly:
`contents-hub init "$HOME/contents-vault"` or
`contents-hub --vault "$HOME/contents-vault" init "$HOME/contents-vault"`.

Set a default in your shell profile:

```bash
export CONTENTS_HUB_VAULT="$HOME/contents-vault"
```

Then run commands from any directory:

```bash
contents-hub sub list
contents-hub raw add https://example.com/article
contents-hub fetch-all
contents-hub digest
```

For login-required browser-backed sources, open the dedicated browser profile
and sign in manually:

```bash
contents-hub browser open https://x.com/login
contents-hub browser status
```

The browser profile is named `contents-hub`. contents-hub does not store
passwords or tokens.

Use `--vault` when you intentionally want another vault:

```bash
contents-hub --vault "$HOME/other-vault" sub list
```

## 4. Add Content

Manual URL/text is the shortest first-launch source path. If no Lens exists
yet, contents-hub creates and attaches a `manual-inbox` Lens automatically so
manual items can appear in the next digest.

Add ad-hoc read-later content:

```bash
contents-hub raw add "A pasted note" --title "Manual note"
```

For RSS, create at least one Lens first so fetched items can be matched into a
digest:

```bash
contents-hub lens create ai --name "AI" --keyword ai
contents-hub sub add <rss-feed-url> --type rss.feed --title "Example"
contents-hub sub list --format json
```

URL input is canonicalized and deduped. contents-hub tries static HTTP
extraction first and uses the selected browser/agent runner only when needed.
Browser-backed social sources and explorations are optional paths and may
require a signed-in browser profile.

## 5. Fetch, Digest, And View

```bash
contents-hub fetch-all
contents-hub digest
contents-hub web --port 8585
```

Open `http://localhost:8585` for the dashboard.

Digest output is stored in SQLite and viewed through the dashboard or CLI
responses. The fetch loop collects raw items; digest scheduling is separate
unless your scheduler invokes `contents-hub digest`.

## 6. Background Fetch

macOS launchd:

```bash
contents-hub daemon install
contents-hub daemon status
```

Any external scheduler can call:

```bash
contents-hub fetch-all
contents-hub digest
contents-hub deliver pending --format json
```

See `docs/schedulers.md` for cron, launchd, Hermes, OpenClaw, Claude Code, and
Codex loop examples.

## 7. Agent Skill Installation

Agent skill registration is optional but recommended. The repo ships one skill:

- `skills/contents-hub/SKILL.md` for CLI, vault, subscription, exploration,
  digest, delivery, and interaction operations

Use your runtime's skill installer to register that one skill. See `install.md`
for the skill-first workflow.

## 8. Delivery And Interaction Smoke

This smoke uses a demo platform. Real Telegram, Slack, or Discord transport
belongs in an external gateway or agent runtime.

```bash
PENDING="$(contents-hub deliver pending --format json)"
RAW_ITEM_ID="$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); print(p["items"][0]["raw_item_id"])' "$PENDING")"
contents-hub delivery record \
  --platform demo \
  --channel-id demo-channel \
  --message-id demo-message \
  --payload-type raw_item \
  --raw-item-id "$RAW_ITEM_ID"
contents-hub interaction handle \
  --platform demo \
  --channel-id demo-channel \
  --message-id demo-message \
  --kind reaction \
  --value "⭐" \
  --format json
```

Adapters can use the same shape for Telegram, Slack, Discord, Hermes, OpenClaw,
or any other gateway.
