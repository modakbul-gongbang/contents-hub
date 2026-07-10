# Channel Delivery And Interaction

Channel adapters are thin. They translate between a messaging platform and the
contents-hub CLI contract.

contents-hub does not ship built-in Telegram, Slack, or Discord bot packages in
the base install. External gateways own message transport, credentials,
webhooks, and platform SDKs.

## Delivery

```bash
contents-hub --vault ~/contents-vault deliver pending --format json
contents-hub --vault ~/contents-vault deliver prepare \
  --collect fetch-all \
  --payload-type raw_item \
  --origin subscription \
  --lens-matched \
  --first-seen-only \
  --limit 20 \
  --format json
```

`deliver pending` does not collect. `deliver prepare` can first run
`fetch-all`, `tick`, or no collector, then returns one JSON object with
`collector` and `delivery` keys. The nested delivery response is an object with
`ok`, `count`, `items`, `groups`, and `delivery_instruction`. Each item
includes stable ids such as `raw_item_id`, `digest_id`, `payload_type`, and
adapter conveniences such as `delivery_key`, `dedupe_key`, `plain_text`, and
`markdown`.

### Verbatim delivery contract

Raw-item cards embed the Lens-generated output itself: each `lenses` entry
carries `lens_id`, `name`, `summary`, and `bullets`, and the card's
`markdown`/`plain_text` are built from that content. Digest cards carry the
full generated digest in `content_md` (and `markdown`). **Adapters and
agents must send `markdown` (or `plain_text`) as-is.** Do not summarize,
rewrite, merge, or reorder cards — the Lens/digest output *is* the
deliverable, and re-summarizing it in the gateway destroys the Lens layer.
The payload repeats this rule in `delivery_instruction`.

`groups` lists cards per Lens (`lens_id`, `name`, `raw_item_ids`, `count`;
unlensed items group under `lens_id: ""`). Adapters that deliver one topic
per message or per thread should iterate `groups` instead of interleaving
lenses. An item matched to several Lenses appears in each of those groups —
adapters that iterate `groups` must dedupe sends by `delivery_key` (send the
card in its first group only) so the user never receives the same card
twice.

contents-hub decides which cards are deliverable. Core candidate selection
handles outbound-message exclusion, raw item origin filters, Lens-matched
filters, and first-seen URL suppression. Runtime and channel adapters should
not query SQLite directly to rediscover pending raw items.

Adapters may send raw item cards or digest cards. Digest cards may include
section context in the JSON, but `raw_item` and `digest` are the only
first-class delivery payload types. The adapter should treat contents-hub ids as
opaque stable references and store the platform's returned message id with
`delivery record`.

After sending a card, record the returned platform message id:

```bash
contents-hub --vault ~/contents-vault delivery record \
  --platform demo \
  --channel-id <channel> \
  --message-id <message> \
  --payload-type raw_item \
  --raw-item-id <raw_item_id>
```

## Interaction

Adapters normalize platform events to:

```json
{
  "platform": "demo",
  "event_id": "event-1",
  "workspace_id": "",
  "channel_id": "channel-1",
  "thread_id": "",
  "message_id": "message-1",
  "user_id": "user-1",
  "kind": "reaction",
  "value": "⭐"
}
```

Then call:

```bash
contents-hub --vault ~/contents-vault interaction handle --event-json '<json>'
```

`interaction handle` resolves the event by matching
`platform + workspace_id + channel_id + thread_id + message_id` against
`outbound_messages`. That mapping exists only if the adapter previously called
`delivery record` after sending the card.
Final-response-only delivery is insufficient for reactions because it does not
create per-card rows in `outbound_messages`.

Default rules:

| Reaction | Action |
| --- | --- |
| `👍` | `save_and_promote` |
| `⭐` | `save_and_promote` |
| `❤️` | `save_and_promote` |
| `❤` | `save_and_promote` |
| `✅` | `mark_read` |
| `🗑` | `archive` |

Slack (and Discord custom emoji) send reaction **names**, not emoji
characters. The same rules accept the common names — `+1`/`thumbsup`,
`star`, `heart`, `white_check_mark`, `wastebasket` — with `:colons:` and
the U+FE0F variation selector normalized away. `interaction rules list`
returns both the emoji rules and the `name_aliases` map, so a Slack
`reaction_added` event can pass `event.reaction` straight through as
`value`.

Handled interactions also mirror into the engagement signal ledger
(`item_signals`): save/read/archive/promote signals with the platform event
id as the dedup key, and `delivery record` writes an `impression` per card.
This powers `contents-hub query` ranking; see `docs/knowledge-store-plan.md`.

`save_and_promote` inserts the item into `saved_items` and writes the promoted
raw item as a markdown source document in the vault. The action is idempotent:
repeat reactions to the same normalized event are logged once, and already
promoted items are treated as no-ops.

## Reference Integrations

- Telegram/Hermes: reference integration shape using `delivery record` and
  `interaction handle`.
- Slack and Discord: fixture normalizers in `contents_hub.channels` prove the
  shared event shape without requiring SDKs in the base install.

### Slack adapter walkthrough

A Slack adapter (bot token with `chat:write` + `reactions:read`, Events API
subscription for `reaction_added`) follows the same four calls as every
other platform:

1. `contents-hub deliver prepare --collect fetch-all --payload-type raw_item
   --lens-matched --format json`
2. For each group in `delivery.groups`, post the member cards' `markdown`
   verbatim via `chat.postMessage` (Slack renders `**bold**` as mrkdwn after
   a `**`→`*` swap; everything else passes through).
3. `contents-hub delivery record --platform slack --workspace-id <team_id>
   --channel-id <channel> --message-id <ts> --payload-type raw_item
   --raw-item-id <id>` — Slack's message `ts` is the message id.
4. On `reaction_added`, forward the event with
   `contents_hub.channels.normalize_slack_event` (or equivalent JSON) to
   `contents-hub interaction handle --event-json '<json>'`. Reaction names
   like `star` resolve without translation.

The base package does not import Telegram, Slack, or Discord SDKs. Full bots can
live in optional integration packages or external runtimes as long as they call
the same CLI contract.

Hermes has two usable patterns:

- Cron final-response delivery: let Hermes run `fetch-all` and `digest`, then
  deliver the final response with `hermes cron create ... --deliver telegram`.
  This is enough for a daily digest but does not create per-card message
  mappings in contents-hub.
- Adapter delivery: a Hermes prompt or external gateway reads
  `deliver prepare --collect fetch-all --format json` or
  `deliver pending --format json`, sends each item through
  Telegram/Discord/etc., records returned message ids with `delivery record`,
  and forwards reactions to `interaction handle`.

Older local Telegram integrations may have written a
`telegram_raw_item_messages` table directly. Current contents-hub migrations copy
compatible rows into `outbound_messages` when any CLI command opens the vault.
New integrations should not write that legacy table; use `delivery record`.
Telegram SDKs and credentials remain outside contents-hub core.
