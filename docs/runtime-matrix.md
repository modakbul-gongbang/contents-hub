# Runtime Matrix

| Runtime | Owns | contents-hub commands |
| --- | --- | --- |
| Manual shell | Human-triggered runs | `browser open`, `fetch-all`, `digest`, `web`, `query`, `item open` |
| cron | Schedule | `fetch-all`, `digest`, `exploration run-all`, `deliver pending` |
| macOS launchd | Long-running daemon | `daemon loop` |
| Hermes | Schedule and gateway (Telegram/Slack/Discord/...) | `fetch-all`, `digest`, `deliver pending`, `delivery record`, `interaction handle`, `query` |
| OpenClaw | Schedule and gateway (Telegram/Slack/Discord/...) | `fetch-all`, `digest`, delivery and interaction CLI, `query` |
| Claude Code loop | Agent loop and scheduling | `fetch-all`, `exploration run-all`, `digest`, `query` |
| Codex loop | Agent loop and scheduling | `fetch-all`, `exploration run-all`, `digest`, `query` |

Every agent runtime (Hermes, OpenClaw, Claude Code, Codex, plain cron) and
every chat platform (Telegram, Slack, Discord) uses the same four-call
adapter contract — `deliver prepare|pending` → send verbatim →
`delivery record` → `interaction handle` — so agent choice and platform
choice are independent. Reaction rules accept emoji and platform reaction
names (Slack) alike; see `docs/channels.md`.

## Skill Registration Notes

The repo-local skill lives at `skills/contents-hub/SKILL.md`.

- OpenClaw: Git skill installs expect `SKILL.md` at the source root, so install
  from a local checkout subdirectory:
  `openclaw skills install ./skills/contents-hub --as contents-hub --global`.
- Hermes: install the skills.sh path:
  `hermes skills install skills-sh/yansfil/contents-hub/skills/contents-hub --yes`.
  Start a new Hermes session after installing so the skill is loaded.
- Other coding agents: register the same `SKILL.md` file or ask the agent to
  read it from the checkout, then let the CLI own behavior.

Detailed runbooks:

- `docs/hermes-setup.md`
- `docs/openclaw-setup.md`

## Runner Selection

The base install is runtime-neutral:

```bash
CONTENTS_HUB_AGENT_RUNNER=none
```

Agent-backed collection or synthesis uses an optional runner:

```bash
uv sync --extra claude --extra dev
CONTENTS_HUB_AGENT_RUNNER=claude-sdk
```

Core modules must not import provider SDKs unless the optional runner path is
selected or imported directly.
