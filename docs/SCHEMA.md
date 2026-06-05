# Eval Engine — Data Model & Schemas (v1)

> Companion to `DESIGN.md`. Postgres = control/state + **ephemeral** ledger; ClickHouse = the
> ~12B-row queryable projection; object store = immutable artifacts (`.eval` logs, transcripts,
> dataset snapshots). DDL is illustrative, not final — review before we commit. Deferred schema
> (the `plugins` catalog) is in `docs/FUTURE.md`; reversed schema choices are in `docs/ALTERNATIVES.md`.

---

## 0. Dataset versioning

**Recommendation: content-addressed immutable snapshots in object storage + a metadata
row in Postgres.** A `dataset_version` is pinned by a `content_hash` (hash of the
normalized sample set); the samples themselves live as an immutable Parquet/JSONL snapshot
in object storage, referenced by URI. A RunSpec pins `dataset_version_id`, which pins the
hash, which pins exact bytes → full reproducibility.

- **Why:** simplest thing that gives true reproducibility; no extra service; portable
  (just object storage); dedup for free (same content → same hash).
- **Alternatives:** **LakeFS** (git-like branching/commits over object storage — powerful,
  but an extra stateful service; adopt later only if you need dataset branching/merge);
  **DVC** (git-centric, awkward at 10⁶-row scale); **HF datasets revisions** (great *if*
  data originates on HF — we support importing from an HF revision *into* a snapshot, but
  don't depend on HF as the system of record).
- **Hook for later:** if branching becomes a need, LakeFS can sit under the same
  `dataset_version` pointer without changing the app contract.
- **Uniqueness:** snapshotting **validates `sample_id` uniqueness** and rejects duplicates
  with a clear error — a duplicate `sample_id` is ambiguous, and silently dropping it would wedge
  expansion forever (ORCH §3). `sample_count` is the **deduped** count, which drives `total_samples`.

---

## 1. Postgres — control plane & ephemeral ledger

Vanilla Postgres only (portability). UUID PKs (`gen_random_uuid()`), `jsonb` for
flexible config, `timestamptz` everywhere.

### 1.1 Identity, tenancy, audit

```sql
CREATE TABLE teams (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  oidc_sub    text NOT NULL UNIQUE,            -- subject claim from the IdP
  email       text NOT NULL UNIQUE,
  name        text,
  role        text NOT NULL DEFAULT 'member'   -- 'admin' | 'member'
              CHECK (role IN ('admin','member')),
  team_id     uuid REFERENCES teams(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit trail: who launched/cancelled/changed what.
CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  user_id     uuid REFERENCES users(id),
  action      text NOT NULL,                   -- 'run.launch','run.cancel','eval.create',...
  entity_type text NOT NULL,
  entity_id   uuid,
  detail      jsonb NOT NULL DEFAULT '{}',
  ts          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (entity_type, entity_id, ts);
```

### 1.2 Datasets (versioned, content-addressed)

```sql
CREATE TABLE datasets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  description text,
  created_by  uuid REFERENCES users(id),
  team_id     uuid REFERENCES teams(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dataset_versions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id    uuid NOT NULL REFERENCES datasets(id),
  version       int  NOT NULL,                 -- monotonic per dataset
  content_hash  text NOT NULL,                 -- sha256 of normalized samples
  snapshot_uri  text NOT NULL,                 -- s3://.../<hash>.parquet  (immutable)
  sample_count  bigint NOT NULL,
  sample_schema jsonb NOT NULL,                -- field names/types of input/target/metadata
  source        jsonb NOT NULL,                -- {kind:'hf'|'s3'|'jsonl'|'db', ...}
  created_by    uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (dataset_id, version),
  UNIQUE (dataset_id, content_hash)            -- dedup identical content
);
```

### 1.3 Evals (versioned bundles)

```sql
CREATE TABLE evals (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  description text,
  created_by  uuid REFERENCES users(id),
  team_id     uuid REFERENCES teams(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE eval_versions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_id             uuid NOT NULL REFERENCES evals(id),
  version             int  NOT NULL,
  dataset_version_id  uuid NOT NULL REFERENCES dataset_versions(id),
  code_ref            text NOT NULL,           -- git sha / package version of harness+scorer code
  default_harness     jsonb NOT NULL,          -- {type, config}
  default_scorers     jsonb NOT NULL,          -- [{type, config}, ...]  (may include {type:'human'})
  config_schema       jsonb NOT NULL,          -- JSON-Schema validating RunSpec overrides
  retention_policy    text NOT NULL DEFAULT 'sample',   -- per-eval; default sample-by-default, opt-in 'keep_all'
  created_by          uuid REFERENCES users(id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (eval_id, version)
);
```

### 1.4 Targets (model registry) & model sets

```sql
CREATE TABLE targets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  label       text NOT NULL UNIQUE,            -- human handle, e.g. 'gpt-4o-2024-11'
  provider    text NOT NULL,                   -- 'openai'|'anthropic'|'vllm'|... (LiteLLM route)
  model_id    text NOT NULL,                   -- provider-side id
  params      jsonb NOT NULL DEFAULT '{}',     -- default temperature/max_tokens/...
  is_self_hosted boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE model_sets (
  id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name  text NOT NULL UNIQUE
);
CREATE TABLE model_set_members (
  model_set_id uuid NOT NULL REFERENCES model_sets(id),
  target_id    uuid NOT NULL REFERENCES targets(id),
  PRIMARY KEY (model_set_id, target_id)
);
```

### 1.5 RunSpec (the reproducible unit) & Run

```sql
CREATE TABLE run_specs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_version_id   uuid NOT NULL REFERENCES eval_versions(id),
  target_id         uuid NOT NULL REFERENCES targets(id),
  harness_config    jsonb NOT NULL,            -- resolved (defaults + overrides)
  scorer_config     jsonb NOT NULL,
  dataset_slice     jsonb NOT NULL,            -- {filter?, offset?, limit?}
  sampling          jsonb NOT NULL,            -- {n, seed, temperature, ...}
  budget            jsonb NOT NULL DEFAULT '{}',-- {max_usd?, max_tokens?}
  spec_hash         text NOT NULL UNIQUE,      -- hash of all the above → dedup/repro
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_spec_id      uuid NOT NULL REFERENCES run_specs(id),
  status           text NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','expanding','running','finalizing',
                                     'completed','failed','cancelled')),
  created_by       uuid REFERENCES users(id),
  team_id          uuid REFERENCES teams(id),
  total_samples    bigint,
  done_samples     bigint NOT NULL DEFAULT 0,
  failed_samples   bigint NOT NULL DEFAULT 0,
  aggregate_metrics jsonb,                      -- computed at finalize (scores, CIs)
  total_tokens     bigint NOT NULL DEFAULT 0,
  total_cost_usd   numeric(14,4) NOT NULL DEFAULT 0,
  eval_log_uri     text,                        -- root .eval artifact for the run
  error            text,
  queued_at        timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  finished_at      timestamptz
);
CREATE INDEX ON runs (status);
CREATE INDEX ON runs (team_id, queued_at DESC);
CREATE INDEX ON runs (run_spec_id);
```

### 1.6 Ephemeral sample-task ledger

Holds **only in-flight runs**. Pruned when a run reaches `completed`/`failed`/`cancelled`
(state then lives in ClickHouse + object store). This is what keeps Postgres small despite
12B lifetime samples.

```sql
CREATE TABLE sample_tasks (
  run_id        uuid NOT NULL REFERENCES runs(id),
  sample_id     text NOT NULL,                 -- id within the dataset_version
  status        text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed')),
  attempts      int  NOT NULL DEFAULT 0,
  claimed_by    text,                          -- worker id
  lease_expires_at timestamptz,                -- crash recovery: reclaim when past due
  last_error    text,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, sample_id)
);
-- Hot path: workers claim the next batch of queued (or lease-expired) tasks.
CREATE INDEX ON sample_tasks (run_id, status);
CREATE INDEX ON sample_tasks (status, lease_expires_at);
```

**Atomic claim** (idempotent, crash-safe) — the core correctness primitive:

```sql
WITH next AS (
  SELECT run_id, sample_id
  FROM sample_tasks
  WHERE run_id = $1
    AND (status = 'queued'
         OR (status = 'running' AND lease_expires_at < now()))   -- reclaim dead leases
  ORDER BY sample_id
  LIMIT $2
  FOR UPDATE SKIP LOCKED                                          -- no two workers grab the same row
)
UPDATE sample_tasks t
SET status='running', claimed_by=$3, attempts=attempts+1,
    lease_expires_at = now() + interval '10 minutes', updated_at=now()
FROM next
WHERE t.run_id=next.run_id AND t.sample_id=next.sample_id
RETURNING t.run_id, t.sample_id;
```

`FOR UPDATE SKIP LOCKED` + lease expiry gives at-least-once execution with no double-claim;
idempotent result writes (keyed by `(run_id,sample_id)`) make retries safe.

> **Skinny by design:** the ledger carries coordination only; results live in ClickHouse, never in
> widened ledger rows. The claim above is bounded by `headroom = max_inflight − running_count` from a
> **fixed per-run cap** (a constant — no `run_slots` table, no per-tick allocation; `SCHEDULER.md`).
> `not_before` handles poison-sample head-of-line. (Hash-sharding the claim is a deferred,
> purely-additive option for a different workload shape — `docs/FUTURE.md` §8.)

---

## 2. ClickHouse — the ~12B-row analytics projection

One row per evaluated sample, flattened from the Inspect `.eval` log after each sample
completes. Scores are dynamic per scorer → stored as a `Map`, with the primary score/pass
hoisted into typed columns for fast filtering.

```sql
CREATE TABLE sample_results
(
  -- identity / joins
  run_id              UUID,
  sample_id           String,
  eval_id             UUID,
  eval_version        UInt32,
  dataset_version_id  UUID,
  target_id           UUID,
  provider            LowCardinality(String),
  model_id            LowCardinality(String),
  harness_type        LowCardinality(String),
  team_id             UUID,
  created_by          UUID,

  -- slicing dimensions: a first-class `category` + an open Map for the heterogeneous rest
  category            LowCardinality(String) DEFAULT '',
  dimensions          Map(String, LowCardinality(String)),    -- subject/difficulty/language/...
  input_hash          String DEFAULT '',                      -- group identical prompts; feature-slice via dimensions

  -- primary outcome (hoisted for fast filters/aggregations)
  passed              UInt8,                       -- 0/1 main pass/fail
  primary_score       Float64,                     -- main scorer's numeric score
  scores              Map(String, Float64),        -- all scorer outputs by name
  scorer_meta         Map(String, String),         -- judge rationale ids, labels, etc.

  -- cost / perf
  tokens_in           UInt32,
  tokens_out          UInt32,
  cost_usd            Float64,
  latency_ms          UInt32,
  attempt             UInt8,

  -- status / pointers
  error_type          LowCardinality(String) DEFAULT '',
  transcript_uri      String,                      -- object-store path (zstd)
  review_status       LowCardinality(String) DEFAULT 'none',  -- D10: ready for human queue

  finished_at         DateTime,
  loaded_at           DateTime DEFAULT now()       -- RMT version — newest re-execution/re-load wins
)
ENGINE = ReplacingMergeTree(loaded_at)             -- dedup per (run_id,sample_id), newest load wins
PARTITION BY toYYYYMM(finished_at)                 -- monthly: cheap 12-mo TTL drops
ORDER BY (eval_id, target_id, run_id, sample_id)   -- access-pattern key; also the dedup key
TTL finished_at + INTERVAL 12 MONTH                -- D1 retention, auto-drop old partitions
SETTINGS index_granularity = 8192;
-- NOTE: collapses duplicate (eval_id,target_id,run_id,sample_id) keeping newest `loaded_at`.
-- eval_id/target_id are functionally determined by run_id, so this dedups per (run_id,sample_id):
-- a re-execution's newer result wins; a duplicate re-insert collapses. Workers async-insert
-- DIRECTLY (workers async-insert; no separate loader). Exact queries use FINAL; headline run metrics are computed ONCE at
-- finalize into run_summary (NOT an insert-time MV, which would double-count a re-load — see below).
```

Notes / rationale:
- **`ORDER BY (eval_id, target_id, run_id, sample_id)`** front-loads the columns most
  queries filter on (which eval, which model), making model-comparison scans fast.
- **`PARTITION BY toYYYYMM`** aligns partitions with the 12-month TTL so expiry is a cheap
  partition drop, not a row-by-row delete.
- **`Map` for scores** keeps the table flexible across arbitrary scorers without a schema
  change per eval; `passed`/`primary_score` give the common case a fast typed path.
- **`LowCardinality`** on provider/model/harness/`category` + the Map values shrinks 12B rows.
- **Multi-dimensional slicing:** `dimensions Map(String, LowCardinality(String))` lets evals
  carry heterogeneous dims (`subject`/`difficulty`/`language`) and slice them **independently**
  (no concatenated-key trap); hot dims are promoted to materialized columns + skip indexes later
  **without a sort-key rewrite** (the genuinely-irreversible part stays frozen on access patterns).
- **Run-level rollups:** headline numbers come from a **`run_summary` table written ONCE at
  finalize** over the deduped run partition — **not** an insert-time `AggregatingMergeTree` MV,
  which fires per insert block and would **double-count a re-load** the base table later collapses.
  **Live** numbers live on the Postgres **`runs` row** : the
  Orchestrator writes progress (ledger counts) + cost (gateway) each tick, and the live score
  (`avg(passed) FROM sample_results WHERE eval_id=E AND target_id=T AND run_id=X` — the **full
  sort-key prefix**, see the finalize query's comment for why; **~once/minute, without `FINAL`**) —
  the rare un-merged-duplicate skew is accepted for a live gauge (finalize keeps `FINAL`). Clients
  read live *and* final from `runs`. The CH `run_summary` is finalize-only. (Sketch below.)

```sql
-- headline per-run metrics written ONCE at finalize over the deduped partition.
-- NOT an insert-time MV — an AggregatingMergeTree MV fires per insert block and never sees the
-- later ReplacingMergeTree collapse, so a re-loaded batch double-counts pass_rate/total_cost.
CREATE TABLE run_summary
(
  run_id UUID, eval_id UUID, target_id UUID,
  n UInt64, passed UInt64, pass_rate Float64,
  mean_score Float64,
  total_cost_usd Float64,            -- sourced from the GATEWAY tally, not summed here
  finalized_at DateTime
)
ENGINE = ReplacingMergeTree(finalized_at) ORDER BY (eval_id, target_id, run_id);

-- Orchestrator at finalize (idempotent recompute over the deduped run partition):
INSERT INTO run_summary
SELECT run_id, any(eval_id), any(target_id),
       count() AS n, sum(passed) AS passed, avg(passed) AS pass_rate,
       avg(primary_score) AS mean_score,
       {gateway_run_cost:Float64} AS total_cost_usd,    -- canonical cost from the gateway
       now() AS finalized_at
FROM sample_results FINAL
-- Sort-key prefix, NOT `WHERE run_id` alone: run_id is the 3rd ORDER BY column, so filtering on it
-- alone can't use the primary index → full current-month partition scan (~1B rows). A run is one
-- eval × one model, so the Orchestrator holds eval_id+target_id — pass the full prefix to hit a
-- tight index range. (Same fix applies to the throttled live read.)
WHERE eval_id = {eval_id:UUID} AND target_id = {target_id:UUID} AND run_id = {run_id:UUID}
GROUP BY run_id;
```

---

## 3. Object storage layout (S3/MinIO)

```
s3://eval-engine/
  datasets/<content_hash>.parquet                 # immutable dataset snapshots
  runs/<run_id>/eval.log                          # Inspect .eval (source of truth)
  runs/<run_id>/transcripts/<sample_id>.json.zst  # zstd; sampled-by-default + storage tiering
```

---

## 4. Open questions
- **Score identity** — is one `primary_score`/`passed` enough, or do some evals have no single
  "primary" (multi-objective)? May need a per-eval declared primary metric. (See `docs/FUTURE.md` §10.)
