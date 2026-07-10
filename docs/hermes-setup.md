# Hermes Setup Runbook

Use this runbook when a Hermes agent is asked to install contents-hub, configure
scheduled digest jobs, or connect a Hermes gateway or external adapter delivery
path.

Hermes owns profiles, gateway lifecycle, cron scheduling, and message delivery.
contents-hub owns local CLI state, the SQLite vault, subscriptions, raw items,
digests, outbound message mappings, and interaction handling.

## Setup Decision

First decide which Hermes pattern the user wants:

| Pattern | Use When | contents-hub state |
| --- | --- | --- |
| Manual smoke | First install or troubleshooting | Vault only |
| Cron final response | Daily digest delivered by Hermes | Digests only |
| Adapter delivery | Per-card message ids and reactions | Digests, outbound mappings, interaction events |
| Script-only cron | Silent watchdogs or deterministic fetch jobs | Vault only unless script calls delivery commands |

Do not promise reaction round-trips unless you are implementing adapter delivery.
Hermes cron final-response delivery sends the final agent response; it does not
automatically call `delivery record` for each contents-hub card.

## Profile-Aware Install

Hermes profiles can have separate skills, config, gateway state, and cron jobs.
The default profile and the profile that owns the gateway may be different.

Inspect before changing anything:

```bash
hermes profile list
hermes gateway list
hermes cron list
```

If the user names a profile, use it explicitly:

```bash
hermes --profile <profile> skills install skills-sh/yansfil/contents-hub/skills/contents-hub --yes
hermes --profile <profile> skills list
```

If your Hermes version does not accept global `--profile` before `skills`, switch
profiles first or install from the target profile's session. For cron jobs, pin
the run profile with `--profile <profile>`:

```bash
hermes cron create "0 8 * * *" \
  "Use the contents-hub skill. Run contents-hub --vault ~/contents-vault fetch-all. If it succeeds, run contents-hub --vault ~/contents-vault digest. Report the digest id and any fetch errors." \
  --skill contents-hub \
  --profile <profile> \
  --workdir "$HOME/contents-hub" \
  --deliver local \
  --name contents-hub-daily
```

After creating jobs:

```bash
hermes cron list
hermes cron status
```

## CLI And Vault Setup

Install the local CLI once per machine:

```bash
git clone https://github.com/yansfil/contents-hub "$HOME/contents-hub"
cd "$HOME/contents-hub"
uv tool install -e "$PWD" --force
uv tool update-shell
contents-hub --help
```

Initialize or reuse one vault:

```bash
contents-hub --vault "$HOME/contents-vault" init "$HOME/contents-vault"
contents-hub --vault "$HOME/contents-vault" raw add "Hermes setup smoke" --title "Hermes setup"
contents-hub --vault "$HOME/contents-vault" digest
```

For RSS feeds, create a Lens first and force the RSS source type:

```bash
contents-hub --vault "$HOME/contents-vault" lens create ai --name "AI" --keyword ai
contents-hub --vault "$HOME/contents-vault" sub add <rss-feed-url> --type rss.feed --title "Example"
contents-hub --vault "$HOME/contents-vault" fetch-all
contents-hub --vault "$HOME/contents-vault" digest
```

For browser-backed sources, open the one fixed profile and let the user sign in:

```bash
contents-hub --vault "$HOME/contents-vault" browser open
contents-hub --vault "$HOME/contents-vault" browser status
```

## Existing Vault Safety

Default rule: preserve the vault.

Before reset, migration, or cron rewiring:

```bash
VAULT="$HOME/contents-vault"
cp "$VAULT/.contents-hub/state.db" "$VAULT/.contents-hub/state.db.backup.$(date +%Y%m%d%H%M%S)"
contents-hub --vault "$VAULT" sub list --format json
contents-hub --vault "$VAULT" lens list --format json
contents-hub --vault "$VAULT" delivery list --limit 20
```

Do not delete these tables during setup:

- `raw_items`
- `digests`
- `lenses`
- `raw_item_lenses`
- `saved_items`
- `outbound_messages`
- `interaction_events`

If the user asks to reset subscriptions only, prefer removing subscriptions
through CLI commands. If direct SQLite work is unavoidable, stop and ask for
explicit confirmation after backing up `state.db`.

Remember that Hermes profile state and contents-hub vault state are separate:
removing a Hermes cron job does not delete `.contents-hub/state.db`, and deleting
the vault does not remove Hermes cron jobs.

## Recommended Cron Topology

### Hourly Fetch Watchdog

Use an agent cron when you want a readable report:

```bash
hermes cron create "0 * * * *" \
  "Use the contents-hub skill. Run contents-hub --vault ~/contents-vault fetch-all. If it succeeds with no errors, respond with [SILENT]. Otherwise report the failed subscription details from the JSON." \
  --skill contents-hub \
  --profile <profile> \
  --workdir "$HOME/contents-hub" \
  --deliver telegram \
  --name contents-hub-fetch-hourly
```

Use script-only no-agent cron when the output is deterministic and you want less
LLM involvement. Scripts must live under the Hermes scripts directory for the
profile that owns the job.

```bash
PROFILE="<profile>"
if [ "$PROFILE" = "default" ]; then
  HERMES_SCRIPTS_DIR="$HOME/.hermes/scripts"
else
  HERMES_SCRIPTS_DIR="$HOME/.hermes/profiles/$PROFILE/scripts"
fi
mkdir -p "$HERMES_SCRIPTS_DIR"

cat > "$HERMES_SCRIPTS_DIR/contents-hub-fetch.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
OUT="$(contents-hub --vault "$HOME/contents-vault" fetch-all)"
python3 -c 'import json,sys; p=json.loads(sys.argv[1]); sys.exit(0 if p.get("ok") else 1)' "$OUT"
SH
chmod +x "$HERMES_SCRIPTS_DIR/contents-hub-fetch.sh"

hermes cron create "0 * * * *" \
  --script contents-hub-fetch.sh \
  --no-agent \
  --profile "$PROFILE" \
  --deliver telegram \
  --name contents-hub-fetch-watchdog
```

### Production-Like No-Agent Topology

For a channel that should feel alive throughout the day, use two separate
Hermes no-agent cron jobs:

| Job | Schedule | Delivery | Responsibility |
| --- | --- | --- | --- |
| Hourly fetch and notify | `0 * * * *` | Direct adapter send, empty stdout on success | Run `deliver prepare --collect fetch-all`, send each returned card as its own message, then call `delivery record`. |
| Daily digest | `0 9 * * *` | Hermes `--deliver origin` or a configured gateway target | Run `digest`, print a concise digest/failure report, and let Hermes deliver the script stdout. |
| Optional exploration | User-approved cadence | Usually local or origin | Run `exploration run-all` or a specific exploration id only after the user has approved the recipe. |

Keep the hourly fetch job silent when there are no new matched items. In
Hermes no-agent mode, empty stdout means no message is delivered. Print only
when the notification or mapping layer fails, or when the user explicitly wants
health reports every run.

The hourly per-card job is the one that enables reactions. The daily digest job
can be useful without reaction mapping because it is a final report. If the user
wants reactions on each digest card, send cards through the adapter flow instead
of relying on final-response delivery.

### Reference Hourly Adapter Script Shape

Use this shape when Hermes should send each new card through `hermes send` and
then persist the returned message id in contents-hub. For adapter delivery, use
an explicit target such as `telegram:<chat_id>` or
`telegram:<chat_id>:<thread_id>` so the script can record the same channel id
that reactions will later contain.

```python
#!/usr/bin/env python3
import json
import os
import subprocess
import sys

VAULT = os.environ.get("CONTENTS_HUB_VAULT", os.path.expanduser("~/contents-vault"))
HERMES_PROFILE = os.environ.get("HERMES_PROFILE", "default")
HERMES_SEND_TARGET = os.environ["HERMES_SEND_TARGET"]  # e.g. telegram:-1001234567890


def target_parts():
    parts = HERMES_SEND_TARGET.split(":")
    platform = parts[0]
    channel_id = parts[1] if len(parts) >= 2 else os.environ.get("HERMES_SEND_CHANNEL_ID", "")
    thread_id = parts[2] if len(parts) >= 3 else ""
    if not platform or not channel_id:
        raise RuntimeError("HERMES_SEND_TARGET must include an explicit channel id for adapter delivery")
    return platform, channel_id, thread_id


def extract_message_id(payload):
    candidates = [
        payload,
        payload.get("result") if isinstance(payload.get("result"), dict) else {},
        payload.get("message") if isinstance(payload.get("message"), dict) else {},
    ]
    for obj in candidates:
        for key in ("message_id", "messageId", "id", "ts"):
            value = obj.get(key)
            if value:
                return str(value)
    raise RuntimeError(f"could not find message id in hermes send result: {payload!r}")


def format_message(item):
    body = item.get("plain_text") or item.get("markdown") or item.get("title") or ""
    return "\n".join(part for part in ["🆕 contents-hub", body] if part)


def send_platform_message(item):
    proc = subprocess.run(
        [
            "hermes", "--profile", HERMES_PROFILE,
            "send", "--to", HERMES_SEND_TARGET, "--json",
            format_message(item),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "hermes send failed").strip())
    payload = json.loads(proc.stdout or "{}")
    if payload.get("error") or not payload.get("success", True):
        raise RuntimeError(f"hermes send returned failure: {payload!r}")
    platform, channel_id, thread_id = target_parts()
    return {
        "platform": platform,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "message_id": extract_message_id(payload),
    }


def record_delivery(ref, item):
    subprocess.run(
        [
            "contents-hub", "--vault", VAULT,
            "delivery", "record",
            "--platform", ref["platform"],
            "--channel-id", ref.get("channel_id", ""),
            "--thread-id", ref.get("thread_id", ""),
            "--message-id", ref["message_id"],
            "--payload-type", "raw_item",
            "--raw-item-id", str(item["raw_item_id"]),
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def prepare_cards():
    proc = subprocess.run(
        [
            "contents-hub", "--vault", VAULT,
            "deliver", "prepare",
            "--collect", "fetch-all",
            "--payload-type", "raw_item",
            "--origin", "subscription",
            "--lens-matched",
            "--first-seen-only",
            "--limit", "20",
            "--timeout-per-sub", "180",
            "--format", "json",
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=1800,
    )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        print("contents-hub deliver prepare returned non-JSON output")
        print((proc.stdout or "")[-2000:])
        return []
    if proc.returncode != 0 or payload.get("ok") is False:
        print(json.dumps(payload, ensure_ascii=False)[:3000])
        return []
    return list((payload.get("delivery") or {}).get("items") or [])


def main():
    for item in prepare_cards():
        if item.get("payload_type") != "raw_item" or not item.get("raw_item_id"):
            continue
        ref = send_platform_message(item)
        record_delivery(ref, item)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"contents-hub hourly notification failed: {exc}")
        raise SystemExit(0)
```

Install it as a Hermes no-agent cron after saving it in the target profile's
Hermes scripts directory:

```bash
PROFILE="<profile>"
if [ "$PROFILE" = "default" ]; then
  HERMES_SCRIPTS_DIR="$HOME/.hermes/scripts"
else
  HERMES_SCRIPTS_DIR="$HOME/.hermes/profiles/$PROFILE/scripts"
fi
mkdir -p "$HERMES_SCRIPTS_DIR"
# write the script above to "$HERMES_SCRIPTS_DIR/contents-hub-fetch-notify.py"

hermes cron create "0 * * * *" \
  --script contents-hub-fetch-notify.py \
  --no-agent \
  --profile "$PROFILE" \
  --deliver origin \
  --name contents-hub-fetch-hourly
```

Set `CONTENTS_HUB_VAULT`, `HERMES_PROFILE`, and `HERMES_SEND_TARGET` in the
profile environment or script wrapper. Use `hermes cron create --help` and
`hermes send --help` to confirm the exact script directory and delivery targets
for the installed Hermes version.

### Daily Digest

```bash
hermes cron create "0 8 * * *" \
  "Use the contents-hub skill. Run contents-hub --vault ~/contents-vault fetch-all. If fetch-all reports ok:false, report the error and stop. Otherwise run contents-hub --vault ~/contents-vault digest and summarize the digest id and item count." \
  --skill contents-hub \
  --profile <profile> \
  --workdir "$HOME/contents-hub" \
  --deliver telegram \
  --name contents-hub-daily-digest
```

### Optional Exploration

Only schedule approved explorations:

```bash
hermes cron create "30 7 * * *" \
  "Use the contents-hub skill. Run contents-hub --vault ~/contents-vault exploration run-all. Report inserted item counts and errors." \
  --skill contents-hub \
  --profile <profile> \
  --workdir "$HOME/contents-hub" \
  --deliver local \
  --name contents-hub-explorations
```

## Gateway Setup

Configure the gateway in the Hermes profile that will deliver messages:

```bash
hermes --profile <profile> gateway setup
hermes --profile <profile> gateway install
hermes --profile <profile> gateway start
hermes --profile <profile> gateway status
```

For cron delivery, Hermes supports targets such as:

```text
origin
local
telegram
discord
signal
platform:chat_id
```

Run `hermes cron create --help` on the user's machine before using a
platform-specific target. Hermes versions can expose additional gateway targets.
Use the delivery target that the user already configured in Hermes. If no
gateway target is configured, keep `--deliver local` until messaging is ready.

## Adapter Delivery

Use adapter delivery only when the user wants per-card message tracking and
reaction handling.

Adapter loop:

```bash
contents-hub --vault "$HOME/contents-vault" deliver prepare \
  --collect fetch-all \
  --payload-type raw_item \
  --origin subscription \
  --lens-matched \
  --first-seen-only \
  --format json
# send each item with a runtime adapter or platform bot
contents-hub --vault "$HOME/contents-vault" delivery record \
  --platform telegram \
  --channel-id <chat_id> \
  --message-id <message_id> \
  --payload-type raw_item \
  --raw-item-id <raw_item_id>
contents-hub --vault "$HOME/contents-vault" interaction handle --event-json '<normalized-event>' --format json
```

The adapter must preserve platform message ids. Without `delivery record`, a
later reaction cannot be mapped back to the original raw item or digest.
Hermes final-response delivery is useful for reports, but it is insufficient for
reaction handling because it does not create per-card `outbound_messages` rows.
contents-hub decides which cards are deliverable; adapters send those cards and
record the returned message ids. Telegram SDKs, bot tokens, and channel
credentials remain outside contents-hub core.

If an older local setup wrote a custom Telegram mapping table directly, run any
normal contents-hub command once against that vault after upgrading. The DB
migration copies compatible `telegram_raw_item_messages` rows into the official
`outbound_messages` table. New adapters should call `delivery record` instead
of writing custom mapping tables.

**Verbatim rule for Hermes prompts:** delivery cards embed the
Lens-generated output in `markdown`/`plain_text` (digest cards carry the
full digest in `content_md`), grouped per Lens in `delivery.groups`. The
Hermes prompt must send that content to the channel exactly as-is, one Lens
group per message — never summarize, rewrite, or merge cards. The payload's
`delivery_instruction` field states the same rule; repeat it in the prompt.

Reaction handling flow:

1. Platform event arrives at Hermes, a bot, or another gateway.
2. The gateway normalizes it into `platform`, `channel_id`, optional
   `thread_id`, `message_id`, `user_id`, `kind`, and `value`.
3. The gateway calls `contents-hub interaction handle --event-json '<json>'`.
4. contents-hub looks up `outbound_messages`.
5. `👍`, `⭐`, `❤️`, and `❤` save and promote the raw item into a markdown source
   document; `✅` marks read; `🗑` archives.

The same loop works on any Hermes gateway platform — Telegram, Slack,
Discord, WhatsApp — with `--platform <name>` on `delivery record`. Slack
sends reaction *names* (`star`, `+1`, `white_check_mark`, `wastebasket`);
they resolve through the same rules without translation
(`interaction rules list` shows the alias map). See `docs/channels.md` for
the Slack walkthrough.

## Knowledge Store Queries

Hermes agents can answer "what have I collected/saved about X" from the
vault instead of searching the web:

```bash
contents-hub --vault "$HOME/contents-vault" query "vector databases" --limit 10 --format json
contents-hub --vault "$HOME/contents-vault" query "vector databases" --context
```

Ranking blends keyword match with the user's own engagement history
(opens, saves, promotes; time-decayed). When the agent opens an item on the
user's behalf, record the signal with `contents-hub item open <id> --no-launch`.

## Agent Checklist

Before setup:

- Confirm Hermes profile: `hermes profile list`.
- Confirm gateway owner: `hermes gateway list`.
- Confirm vault path.
- Back up existing `state.db` if it exists.
- List existing cron jobs: `hermes cron list`.
- List existing subscriptions and delivery mappings.

After setup:

- `contents-hub --vault <vault> sub list --format json`.
- `contents-hub --vault <vault> fetch-all`.
- `contents-hub --vault <vault> digest`.
- `contents-hub --vault <vault> web --port 8585`.
- `hermes cron list`.
- `hermes gateway status` if delivery is not local.
- Send one demo delivery/interaction smoke before enabling real adapter
  reaction handling.
