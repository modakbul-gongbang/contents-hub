---
name: contents-hub-install
description: Install the single contents-hub skill, then let the agent install and operate the contents-hub CLI from that skill.
---

# contents-hub Installation

contents-hub is meant to be installed skill-first.

Install the single `contents-hub` skill in your agent runtime. After that, ask
the agent to install, initialize, subscribe, fetch, digest, deliver, or handle
interactions. The skill contains the CLI install and operating playbook, and the
actual product behavior lives in the `contents-hub` CLI.

The reliable first launch path is manual content, local digest generation, and
the dashboard. RSS feeds are reliable after the user creates at least one Lens
for matching. Browser-backed social sources, explorations, and real
Telegram/Slack/Discord bot adapters are optional or follow-up paths.

## Recommended: Skill-First

Use your agent runtime's skill installer and point it at the single
`contents-hub` skill.

Hermes:

```bash
hermes skills install skills-sh/yansfil/contents-hub/skills/contents-hub --yes
```

OpenClaw:

```bash
git clone https://github.com/yansfil/contents-hub "$HOME/contents-hub"
cd "$HOME/contents-hub"
openclaw skills install ./skills/contents-hub --as contents-hub --global
```

Generic target:

```text
https://github.com/yansfil/contents-hub
skills/contents-hub/SKILL.md
```

If your runtime supports an `npx skills` style installer, install only this
single skill. Do not install the repository root as a skill unless that runtime
explicitly supports nested skill paths.

Then start a new agent session and ask:

```text
Install contents-hub, initialize a vault at ~/contents-vault, add a manual note,
run a digest, and open the dashboard.
```

The agent should:

1. Read the `contents-hub` skill.
2. Check whether `contents-hub` is already installed.
3. Clone or update a durable checkout.
4. Install the editable CLI with `uv tool install`.
5. Initialize or reuse the requested vault.
6. Use CLI commands such as `sub add`, `fetch-all`, `digest`, `deliver pending`,
   `deliver prepare`, `delivery record`, and `interaction handle`.

## Runtime-Specific Skill Registration

contents-hub ships the skill at `skills/contents-hub/SKILL.md`, not at the repo
root. Use the runtime's local-skill flow when a Git installer expects `SKILL.md`
at the source root.

### OpenClaw

OpenClaw's native Git skill install expects `SKILL.md` at the repository root.
For this repo, clone first and install the skill subdirectory:

```bash
git clone https://github.com/yansfil/contents-hub "$HOME/contents-hub"
cd "$HOME/contents-hub"
openclaw skills install ./skills/contents-hub --as contents-hub --global
```

Omit `--global` if you want the skill installed only into the active OpenClaw
workspace. Start a new OpenClaw session after installing so it reloads skills.

### Hermes

Current Hermes installs from skills.sh and GitHub-style skill paths:

```bash
hermes skills install skills-sh/yansfil/contents-hub/skills/contents-hub --yes
hermes skills list
```

Start a new Hermes session after installing so the skill is loaded. Do not point
Hermes at only `https://github.com/yansfil/contents-hub`; the install target must
include the nested skill path.

## Agent Contract

When an agent has the `contents-hub` skill, it should do the work end to end
unless a user-owned action is required, such as installing Python, installing
`uv`, authenticating to GitHub, choosing a vault path, or resolving uncommitted
changes in an existing checkout.

The default install target is macOS/Linux. Prefer a durable checkout path such
as `$HOME/contents-hub`, not `/tmp`.

## CLI Install Fallback

If you do not use skills, install the CLI directly:

```bash
git clone https://github.com/yansfil/contents-hub "$HOME/contents-hub"
cd "$HOME/contents-hub"
uv tool install -e "$PWD" --force
uv tool update-shell
contents-hub --help
```

The base install is runtime-neutral.
For browser subscription collection with local Aside, install its CLI, open the browser, and sign in to the source sites:

```bash
aside --version
CONTENTS_HUB_AGENT_RUNNER=aside contents-hub --vault ~/contents-vault fetch-all
```

No Python extra is needed for Aside.
See [Aside setup and limits](docs/aside.md).
For local development or Claude-backed browser/agent features, install optional extras explicitly:

```bash
uv sync --extra claude --extra dev
CONTENTS_HUB_AGENT_RUNNER=claude-sdk contents-hub --help
```

## Runtime Shape

All runtimes use the same CLI contract.

Hermes, OpenClaw, Codex, Claude Code, cron, launchd, Slack, Discord, Telegram,
or another gateway should own scheduling, credentials, webhooks, and message
transport. contents-hub owns local state and content actions.

Common runtime commands:

```bash
contents-hub --vault "$HOME/contents-vault" raw add "A pasted note" --title "Manual note"
contents-hub --vault "$HOME/contents-vault" lens create ai --name "AI" --keyword ai
contents-hub --vault "$HOME/contents-vault" sub add <rss-feed-url> --type rss.feed --title "Example"
contents-hub --vault "$HOME/contents-vault" browser open https://x.com/login
contents-hub --vault "$HOME/contents-vault" fetch-all
contents-hub --vault "$HOME/contents-vault" digest
contents-hub --vault "$HOME/contents-vault" deliver pending --format json
contents-hub --vault "$HOME/contents-vault" deliver prepare --collect fetch-all --payload-type raw_item --origin subscription --lens-matched --first-seen-only --format json
contents-hub --vault "$HOME/contents-vault" delivery record ...
contents-hub --vault "$HOME/contents-vault" interaction handle --event-json '<json>'
```

For runtime-specific setup, use:

- `docs/hermes-setup.md` for Hermes profiles, gateway, cron, Telegram delivery,
  no-agent jobs, vault safety, and adapter delivery.
- `docs/openclaw-setup.md` for OpenClaw skill scope, CLI install, task topology,
  vault safety, and external gateway integration.

## Smoke Test

Run this after installing or updating:

```bash
VAULT="$(mktemp -d)/vault"
contents-hub --vault "$VAULT" init "$VAULT"
contents-hub --vault "$VAULT" raw add "Example story body" --title "Example story"
contents-hub --vault "$VAULT" digest
PENDING="$(contents-hub --vault "$VAULT" deliver prepare --collect none --payload-type raw_item --format json)"
RAW_ITEM_ID="$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); print(p["delivery"]["items"][0]["raw_item_id"])' "$PENDING")"
contents-hub --vault "$VAULT" delivery record \
  --platform demo \
  --channel-id demo-channel \
  --message-id demo-message \
  --payload-type raw_item \
  --raw-item-id "$RAW_ITEM_ID"
contents-hub --vault "$VAULT" interaction handle \
  --platform demo \
  --channel-id demo-channel \
  --message-id demo-message \
  --kind reaction \
  --value "⭐" \
  --format json
```

Expected result: the CLI creates a vault, accepts a raw item, produces a
runtime-neutral digest response, emits delivery JSON through `deliver prepare`,
records a demo outbound message, and handles the reaction without provider
credentials. In real Telegram/Slack/Discord integrations, reactions only map
when the adapter records the platform message id returned by the send API.

## What To Install

Install one skill:

```text
contents-hub
```

Do not install another contents-hub skill for exploration. Recipe design lives
inside the same `contents-hub` skill, and execution happens through the CLI.
