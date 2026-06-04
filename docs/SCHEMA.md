# Eval Engine — Data Model & Schemas (Draft v0.1)

> Companion to `DESIGN.md` v0.2. Resolves open item §14.2 (dataset versioning) and §14.3
> (concrete schemas). Postgres = control/state + **ephemeral** ledger; ClickHouse = the
> ~12B-row queryable projection; object store = immutable artifacts (`.eval` logs,
> transcripts, dataset snapshots). DDL is illustrative, not final — review before we commit.

---

## 0. Decision: dataset versioning (resolves §14.2)

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

---

## 1. Postgres — control plane & ephemeral ledger

Vanilla Postgres only (portability, D2). UUID PKs (`gen_random_uuid()`), `jsonb` for
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
  role        text NOT NULL DEFAULT 'member'   -- 'admin' | 'member'  (D6)
              CHECK (role IN ('admin','member')),
  team_id     uuid REFERENCES teams(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit trail (D6): who launched/cancelled/changed what.
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
  retention_policy    text NOT NULL DEFAULT 'keep_all',   -- D8: per-eval, default keep_all
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

### 1.6 Ephemeral sample-task ledger (D4)

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

  -- slicing dimension pulled from sample metadata (e.g. category/subject/difficulty)
  group_key           LowCardinality(String) DEFAULT '',

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

  finished_at         DateTime
)
ENGINE = ReplacingMergeTree(attempt)               -- dedup on re-load edge case (see ORCHESTRATION §11)
PARTITION BY toYYYYMM(finished_at)                 -- monthly: cheap 12-mo TTL drops
ORDER BY (eval_id, target_id, run_id, sample_id)   -- dedup key + matches "metric by model for eval" queries
TTL finished_at + INTERVAL 12 MONTH                -- D1 retention, auto-drop old partitions
SETTINGS index_granularity = 8192;
-- NOTE: ReplacingMergeTree collapses duplicate (eval_id,target_id,run_id,sample_id) keeping
-- highest `attempt`. Execution-level dedup is already handled by the Postgres PK upsert
-- (ORCHESTRATION §4); this engine only mops up the rare ResultLoader re-load (§11). Exact
-- queries use FINAL or aggregation; run-level rollups are fed once at finalize from deduped data.
```

Notes / rationale:
- **`ORDER BY (eval_id, target_id, run_id, sample_id)`** front-loads the columns most
  queries filter on (which eval, which model), making model-comparison scans fast.
- **`PARTITION BY toYYYYMM`** aligns partitions with the 12-month TTL so expiry is a cheap
  partition drop, not a row-by-row delete.
- **`Map` for scores** keeps the table flexible across arbitrary scorers without a schema
  change per eval; `passed`/`primary_score` give the common case a fast typed path.
- **`LowCardinality`** on provider/model/harness/group_key shrinks 12B rows substantially.
- **Run-level rollups** (for the runs list / dashboards) come from a **materialized view**
  aggregating into a `run_metrics` AggregatingMergeTree, so the dashboard never scans raw
  rows for headline numbers. (Sketch below.)

```sql
-- Incremental run-level aggregates (accuracy, mean cost, etc.) via MV.
CREATE TABLE run_metrics
(
  run_id UUID, eval_id UUID, target_id UUID,
  n UInt64,
  pass_rate AggregateFunction(avg, UInt8),
  mean_score AggregateFunction(avg, Float64),
  total_cost AggregateFunction(sum, Float64)
)
ENGINE = AggregatingMergeTree ORDER BY (eval_id, target_id, run_id);

CREATE MATERIALIZED VIEW run_metrics_mv TO run_metrics AS
SELECT run_id, eval_id, target_id,
       count() AS n,
       avgState(passed)        AS pass_rate,
       avgState(primary_score) AS mean_score,
       sumState(cost_usd)      AS total_cost
FROM sample_results
GROUP BY run_id, eval_id, target_id;
```

---

## 3. Object storage layout (S3/MinIO)

```
s3://eval-engine/
  datasets/<content_hash>.parquet                 # immutable dataset snapshots
  runs/<run_id>/eval.log                          # Inspect .eval (source of truth)
  runs/<run_id>/transcripts/<sample_id>.json.zst  # zstd; keep-all 12mo (D8)
```

---

## 4. Open questions on the schema (for v0.3 review)
1. **`group_key` cardinality** — single dimension now; do we need multiple slice dimensions
   (subject × difficulty × language)? Could promote to a small `Map(String,String)` of dims.
2. **Score identity** — is one `primary_score`/`passed` enough, or do some evals have no
   single "primary" (multi-objective)? May need a per-eval declared primary metric.
3. ~~**Ledger prune vs archive**~~ — **RESOLVED** (ORCHESTRATION §10): archive *failed* tasks
   to `failed_task_archive`, hard-delete the rest on finalize.
4. ~~**Idempotency token**~~ — **RESOLVED** (ORCHESTRATION §4, §11): Postgres PK upsert dedups
   at execution; ClickHouse `ReplacingMergeTree(attempt)` mops up the loader re-load edge case.
```
