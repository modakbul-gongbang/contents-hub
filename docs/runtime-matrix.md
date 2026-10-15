# Runtime Matrix

| Runtime | Owns | contents-hub commands |
| --- | --- | --- |
| Manual shell | Human-triggered runs | `browser open`, `fetch-all`, `digest`, `web` |
| cron | Schedule | `fetch-all`, `digest`, `exploration run-all`, `deliver pending` |
| macOS launchd | Long-running daemon | `daemon loop` |
| Hermes | Schedule and Telegram gateway | `fetch-all`, `digest`, `deliver pending`, `delivery record`, `interaction handle` |
| OpenClaw | Schedule and gateway | `fetch-all`, `digest`, delivery and interaction CLI |
| Claude Code loop | Agent loop and scheduling | `fetch-all`, `exploration run-all`, `digest` |
| Codex loop | Agent loop and scheduling | `fetch-all`, `exploration run-all`, `digest` |

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

Local Aside browser subscription collection needs the base install and a signed-in Aside browser:

```bash
CONTENTS_HUB_AGENT_RUNNER=aside
```

This path supports subscription collection through `fetch`, `fetch-all`, `tick`, and collection-driven delivery.
It uses Aside's browser sessions, keeps deterministic HTTP/feed paths, and requires no Claude SDK.
Text synthesis uses deterministic fallbacks; autonomous explorations require the Claude runner.
See [Aside](aside.md) for requirements, resource limits, and recovery.

Agent-backed collection or synthesis uses an optional runner:

```bash
uv sync --extra claude --extra dev
CONTENTS_HUB_AGENT_RUNNER=claude-sdk
```

Core modules must not import provider SDKs unless the optional runner path is
selected or imported directly.
