# Personal Knowledge Store — Design & Delivery Plan

Status: proposed (2026-07). CLI-first; the MCP surface comes later and wraps
the same commands (see `docs/competitive-analysis-2026.md`, O1).

## 1. Goal

Turn the vault from a collection pipeline into a **queryable personal
knowledge store** ranked by how much the user actually cared:

- Every collected item accumulates **engagement signals** in layers:
  seen (delivered) < opened/clicked < read < saved/liked < promoted,
  with archive as a negative signal.
- A single `contents-hub query` command answers "what do I know about X?"
  by fusing **semantic similarity + keyword match + engagement score +
  time decay**, returning agent-ready JSON.

External agents (Claude Code, OpenClaw, Hermes, cron scripts) drive it
through the CLI contract, exactly like the existing deliver/interaction
loop. No new runtime dependencies for base operation.

## 2. What already exists (build on, don't duplicate)

| Signal substrate | Where |
|---|---|
| Reaction events (⭐ ❤️ 👍 → save, ✅ → read, 🗑 → archive), always logged, idempotent | `interaction_events` + `interactions.py` handlers |
| Save state | `saved_items`, `raw_items.status='promoted'` |
| Negative state | `raw_items.status='archived'` |
| Delivery exposure (an item was shown in a channel) | `outbound_messages` |
| Topical structure | `lenses`, `raw_item_lenses` (summary/bullets) |
| Full text for indexing | `raw_items.title/body/content_summary` |

Missing: open/click capture, a unified signal ledger with weights, any
search index (FTS or vector), a scoring model, and the query command.

## 3. Signal model

### 3.1 New table: `item_signals` (schema v15)

Append-only ledger; one row per signal occurrence.

```sql
CREATE TABLE IF NOT EXISTS item_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id  INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    signal       TEXT NOT NULL,     -- impression|open|read|save|promote|archive|unsave|note
    source       TEXT NOT NULL DEFAULT '',   -- web|cli|slack|discord|telegram|agent|backfill
    dedup_key    TEXT NOT NULL DEFAULT '',   -- idempotency (e.g. platform event id, or source:signal:date)
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_item_signals_dedup
    ON item_signals(raw_item_id, signal, dedup_key) WHERE dedup_key != '';
CREATE INDEX IF NOT EXISTS idx_item_signals_item ON item_signals(raw_item_id, created_at);
```

Design rules:

- **Append-only + dedup key = replay-safe**, same philosophy as
  `interaction_events`. Re-handling the same Slack event or re-running a
  backfill must not double-count (AGENTS.md idempotency invariant).
- `interactions.py` action handlers additionally write the corresponding
  signal row (save/read/archive) inside the same transaction. The
  existing tables stay the source of truth for *state*; `item_signals`
  is the source of truth for *history/score*.
- Repeat opens on different days are separate signals (they mean renewed
  interest); repeat opens within the same day dedup via
  `dedup_key = source || ':' || date`.

### 3.2 Default weights (config-overridable)

| signal | weight | notes |
|---|---|---|
| impression | 0.0 | recorded (from `delivery record`) but not scored by default |
| open | +1.0 | link click / `item open` |
| read | +0.5 | ✅ mark_read |
| save | +3.0 | ⭐/❤️/👍 reaction, web toggle-saved |
| promote | +5.0 | promoted to source note |
| note | +2.0 | user attached a comment/annotation |
| unsave | −3.0 | reverses a save |
| archive | −2.0 | 🗑 |

### 3.3 Capturing "open" honestly

Chat platforms do not report link clicks. Capture surfaces, in order of
effort:

1. **CLI**: `contents-hub item open <id_or_url>` — records `open`
   (source=cli) and prints/launches the URL. Agents use this too.
2. **Web UI**: change item links to a redirect route
   `GET /r/{item_id}` → record `open` (source=web) → 302 to the real URL.
   Digest detail, lens inbox, and saved views all route through it.
3. **Chat cards (optional, later)**: when the dashboard is reachable from
   the user's devices, adapters may substitute card URLs with the `/r/{id}`
   redirect. Off by default; documented as a knob, never required.

## 4. Scoring model

### 4.1 Engagement score (per item, computed at query time)

```
engagement(item) = Σ over signals  weight(signal) * 0.5 ^ (age_days / H_e)
```

- `H_e` = engagement half-life, default **90 days** — a save from last week
  counts ~2x a save from three months ago.
- Negative totals clamp at a floor (default −3) so archives suppress
  rather than nuke keyword-relevant results.
- Pure SQL over `item_signals` (SQLite `julianday`, `power` via
  `exp/log` or a registered Python function). Vault scale (10³–10⁵ items)
  needs no materialization; add a cached column only if profiling says so.

### 4.2 Relevance

- **Keyword**: FTS5 virtual table `raw_items_fts(title, body,
  content_summary, lens_summaries)` kept in sync with triggers
  (external-content mode). BM25 comes free. Zero new dependencies —
  FTS5 ships in CPython's bundled SQLite (verify at `init_db`, degrade
  to `LIKE` with a warning if absent).
- **Semantic (optional extra)**: `EmbeddingProvider` boundary mirroring
  `AgentRunner`:
  - `none` (default) — query runs keyword-only; no SDK imports.
  - `openai-compatible` (covers OpenAI/Ollama/LM Studio/llama.cpp via
    base URL) — `CONTENTS_HUB_EMBEDDINGS=openai-compat`,
    `CONTENTS_HUB_EMBEDDINGS_URL/MODEL/KEY`.
  - Vectors in `item_embeddings(raw_item_id, model, dim, vector BLOB,
    text_hash, updated_at)`; brute-force cosine in Python is fine at this
    scale (~50k × 768 floats ≈ 150 MB worst case, chunked scan);
    `sqlite-vec` is an optional accelerator, never a requirement.
  - Embed on collection (post-fetch hook) + `contents-hub index --backfill`
    for existing items; `text_hash` makes re-runs incremental.

### 4.3 Fusion

Combine with **Reciprocal Rank Fusion** (robust, no score-scale tuning):

```
rrf(item)  = Σ over available rankers r  1 / (60 + rank_r(item))   -- rankers: fts, vector
final(item) = rrf * (1 + λ_e * norm(engagement)) * (1 + λ_r * 0.5^(age_days(published_or_collected)/H_r))
```

- `λ_e` default 1.0 (engagement can up to double a result), `λ_r` default
  0.25 with recency half-life `H_r` = 30 days — mild freshness boost.
- With no embedder configured, RRF degrades to BM25 rank alone; the JSON
  output reports which rankers ran so agents can reason about quality.
- All constants live under a `query:` block in `.contents-hub.yaml`.

## 5. CLI contract (phase-gated)

```bash
# Signals
contents-hub item open <id_or_url> [--source cli] [--no-launch]
contents-hub item signal <id_or_url> --signal note --metadata '<json>'
contents-hub item signals <id_or_url> --format json        # ledger + current score

# Index
contents-hub index [--backfill] [--rebuild-fts] [--format json]

# Query — the headline command
contents-hub query "vector db benchmarks" \
    --limit 10 --since 90d --lens ai-research \
    --min-engagement 0 --saved-only --format json
```

`query --format json` returns, per item: id, url, title, snippet
(FTS highlight), scores `{final, fts_rank, vector_rank, engagement,
recency}`, signals summary (`{save:1, open:3, ...}`), lens tags, and
timestamps. A `--context` flag emits a markdown context-pack (top-N with
summaries) ready to paste into an agent prompt. These JSON shapes are the
future MCP tool responses — freeze them carefully.

## 6. Delivery phases

**Phase 1 — signal ledger (schema v15 + backfill).** `item_signals`
table; `interactions.py` handlers dual-write; `delivery record` writes
`impression`; `item open` / `item signal` / `item signals` commands;
backfill migration deriving signals from `saved_items` (save),
`raw_items.status` (promote/archive), and replayable `interaction_events`
(dedup_key = platform event id, so it is safe to re-run).
*Tests:* dedup/idempotency (replay same event twice → one row), backfill
correctness, handler dual-write atomicity. ~1 week.

**Phase 2 — keyword query + engagement ranking.** FTS5 table + sync
triggers + `index --rebuild-fts`; engagement/decay scoring in SQL;
`query` command (keyword-only fusion); web `/r/{item_id}` redirect.
*Tests:* FTS sync on insert/update/archive, decay math against fixed
timestamps, JSON shape snapshot. This phase alone already ships a useful
"my stuff, ranked by what I cared about" search. ~1–2 weeks.

**Phase 3 — semantic layer (optional extra).** `EmbeddingProvider` +
`item_embeddings` + post-fetch embedding + RRF fusion; `--explain` flag
showing per-ranker contributions.
*Tests:* provider selection matrix (none/openai-compat), incremental
re-embedding via text_hash, fusion determinism with a fake embedder.
~2 weeks.

**Phase 4 — MCP wrapper (deferred by decision).** Expose `query`,
`item_signals`, `item_signal` as MCP tools returning the frozen JSON
shapes. Small once phases 1–3 exist.

## 7. Invariants & risks

- **Base install stays dependency-free**: FTS5 is stdlib-SQLite; embeddings
  are opt-in env-selected, imported only inside the provider module
  (same rule as `runners/claude_sdk.py`).
- **Idempotency everywhere**: signals are append-only with dedup keys;
  backfill re-runs are no-ops; promoted note immutability untouched.
- **Score is advice, not state**: nothing deletes/hides items based on
  score; `archive` remains the only suppression, and it stays reversible.
- **Privacy**: signals never leave the vault; embedding provider choice is
  explicit and logged in `query` output (`rankers: [...]`), so the user
  always knows whether text was sent to an external endpoint.
- Risk: FTS5 absent in exotic Python builds → detect at init, warn, run
  degraded `LIKE` search. Risk: brute-force cosine slow past ~100k items →
  documented `sqlite-vec` escape hatch.
