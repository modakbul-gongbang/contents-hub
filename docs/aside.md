# Aside Browser Collection

Use `CONTENTS_HUB_AGENT_RUNNER=aside` to collect browser-backed subscriptions through your local Aside browser.
The base Python installation is sufficient; provider SDKs and credentials are managed by Aside.

## Setup

1. Install the CLI from the [Aside developer documentation](https://docs.aside.com/help/developers) and ensure `aside` is on the collector process's `PATH`.
2. Open Aside, select your account and model, and sign in to X, LinkedIn, or the sites you want to collect.
3. Select the runner for the command or in the scheduler's environment.

```bash
aside --version
CONTENTS_HUB_AGENT_RUNNER=aside contents-hub --vault ~/contents-vault fetch <subscription-id>
CONTENTS_HUB_AGENT_RUNNER=aside contents-hub --vault ~/contents-vault fetch-all --concurrency 3
CONTENTS_HUB_AGENT_RUNNER=aside contents-hub --vault ~/contents-vault tick
```

Aside selection also applies to `deliver prepare` when it invokes collection.
Only the local browser is targeted, using its default account and model with Guard permissions.
Collection prompts request read-only browsing and JSON results; contents-hub owns storage and deduplication.
The `browser open`, `browser status`, and `browser kill` commands continue to manage chromux.
For Aside login issues, use the Aside browser directly.

## Collection Contract

The production pipeline remains LIST, database DIFF, CONTENT, and persistence.
Previously saved URLs are skipped before content extraction and are not inserted twice.
X and LinkedIn use Aside instead of the chromux direct DOM path; deterministic HTTP/feed and content snapshot paths remain available.
Source rules, subscription-specific collection guidance, and failure reasons are preserved.
Only a completed final assistant answer is returned; terminal progress, thinking, tool output, truncated answers, and nonzero CLI exits are not accepted as collection results.

Text-only Lens routing and digest generation use existing deterministic fallbacks.
Aside does not receive contents-hub's custom tools, so autonomous exploration returns an actionable error requiring `claude-sdk`.

## CLI Compatibility And Limits

The adapter requires `aside exec --log-dump <path>`, `aside session stop <id>`, and `aside session delete <id>`.
The raw event contract and session header were verified with Aside CLI **1.26.916.1741**.
`--log-dump` is currently a hidden CLI option; it is required because the ordinary terminal output mixes progress with results and MCP only returns the session ID after completion.
Missing or changed event contracts fail explicitly instead of falling back to transcript parsing.

Each invocation runs in a separate temporary directory with private event-file permissions and a dedicated session.
The supervisor removes that event file and deletes its owned session after success or failure.
On timeout, cancellation, or owner input EOF, it terminates the CLI, stops the owned session, and deletes it.
The parent uses a dedicated process group as a final termination fallback and never kills the user's Aside browser.
Cleanup failures are reported to the caller.

Limits are constants in code:

| Resource | Limit |
| --- | --- |
| Concurrent invocations per collector process | 3 |
| Execution time per invocation | Caller timeout, capped at 600 seconds |
| Cleanup grace after execution | 15 seconds |
| Prompt | 512 KiB |
| Final answer | 1 MiB |
| Temporary event stream | 8 MiB and 10,000 events |
| CLI stderr | 64 KiB |
| Assistant turns | Caller's `max_turns`, default 30 |

Aside has no native maximum-turn CLI option.
The supervisor counts completed assistant events and stops when a tool-use turn consumes the last permitted turn, or when the event stream exceeds the budget.
Execution and cleanup have separate bounded time windows.

## Recovery

- CLI missing: install Aside's CLI and add it to the scheduler or shell `PATH`.
- `login_required`: open Aside, sign in to the source site, and retry.
- `cli_failed`, `missing_final_answer`, or `invalid_cli_events`: check Aside is running and signed in, then confirm the CLI supports the required event contract.
- `turn_limit`, `event_size_limit`, `event_limit`, or `result_limit`: reduce item counts or the subscription collection scope and retry.
- Capacity reached: use `fetch-all --concurrency 3` or fewer and retry after running collections finish.
- Session cleanup failure: inspect your recent Aside sessions, stop/delete only the session created by the failed collection, and retry after restoring the local CLI connection.

Collector logs contain run references, outcome types, and counts rather than collected page text or credentials.
