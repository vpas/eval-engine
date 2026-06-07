-- Initial control-plane schema (SCHEMA §1, ORCHESTRATION §4–§10).
-- Managed by yoyo-migrations: this consolidates what used to be a hand-rolled CREATE + idempotent-ALTER
-- string in control.py. Every statement is still IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so applying
-- it over a DB created by the old code is a safe no-op (yoyo just records it as applied). Future schema
-- changes get their OWN numbered migration file rather than appending ALTERs here.

CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, eval_id TEXT, eval_version INT, model TEXT, provider TEXT,
  model_id TEXT, harness TEXT, scorers JSONB, status TEXT, total INT, done INT, failed INT,
  accuracy DOUBLE PRECISION, cost_usd DOUBLE PRECISION DEFAULT 0, dataset_hash TEXT, spec_json TEXT,
  created_by TEXT, team TEXT, image_digest TEXT, lane TEXT, max_inflight INT, provider_fingerprint TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ);

-- idempotent migrations for tables created before these columns existed
ALTER TABLE runs ADD COLUMN IF NOT EXISTS created_by TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS team TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS image_digest TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lane TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS max_inflight INT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS provider_fingerprint TEXT;

-- Training-monitor provenance (docs/TRAINING_MONITOR.md §2).
ALTER TABLE runs ADD COLUMN IF NOT EXISTS training_run_id TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS checkpoint_id TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS step INT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS sweep TEXT;
CREATE INDEX IF NOT EXISTS ix_runs_ckpt ON runs(checkpoint_id);
CREATE INDEX IF NOT EXISTS ix_runs_train ON runs(training_run_id);

CREATE TABLE IF NOT EXISTS sample_tasks(
  run_id TEXT, sample_id TEXT, status TEXT DEFAULT 'queued', attempts INT DEFAULT 0,
  claimed_by TEXT, lease_expires_at TIMESTAMPTZ, not_before TIMESTAMPTZ, group_key TEXT,
  passed INT, primary_score DOUBLE PRECISION, scores JSONB, tokens_in INT, tokens_out INT,
  cost_usd DOUBLE PRECISION, latency_ms INT, error_type TEXT, transcript_uri TEXT,
  loaded BOOLEAN DEFAULT false,
  PRIMARY KEY(run_id, sample_id));
CREATE INDEX IF NOT EXISTS ix_tasks_claim ON sample_tasks(run_id, status);
CREATE INDEX IF NOT EXISTS ix_tasks_load ON sample_tasks(run_id) WHERE status='done' AND NOT loaded;

CREATE TABLE IF NOT EXISTS failed_task_archive(
  run_id TEXT, sample_id TEXT, error_type TEXT, attempts INT,
  PRIMARY KEY(run_id, sample_id));

-- Registered, versioned entities (datasets / evals / models — DESIGN §7, FR1–3).
CREATE TABLE IF NOT EXISTS entities(
  kind TEXT, id TEXT, version INT, body JSONB, created_by TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(kind, id, version));

-- Append-only audit log (DESIGN §8/§13).
CREATE TABLE IF NOT EXISTS audit_log(
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
  actor TEXT, action TEXT, target TEXT, detail JSONB);

-- Liveness heartbeats (ops dashboard).
CREATE TABLE IF NOT EXISTS heartbeats(
  component TEXT, instance TEXT, ts TIMESTAMPTZ DEFAULT now(), detail JSONB,
  PRIMARY KEY(component, instance));

-- ===== Training monitor (docs/TRAINING_MONITOR.md) ========================================
CREATE TABLE IF NOT EXISTS training_runs(
  id TEXT PRIMARY KEY, model TEXT, base TEXT, status TEXT DEFAULT 'watching',
  current_step INT DEFAULT 0, planned_steps INT, source TEXT, owner TEXT, body JSONB,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, training_run_id TEXT, step INT, model_ref TEXT, tokens BIGINT,
  wall_time TIMESTAMPTZ, status TEXT DEFAULT 'discovered', train_metrics JSONB,
  discovered_at TIMESTAMPTZ DEFAULT now(), UNIQUE(training_run_id, step));
CREATE INDEX IF NOT EXISTS ix_ckpt_run ON checkpoints(training_run_id, step);

CREATE TABLE IF NOT EXISTS checkpoint_scores(
  training_run_id TEXT, eval_id TEXT, step INT, run_id TEXT, n INT, passed INT,
  accuracy DOUBLE PRECISION, ci_lo DOUBLE PRECISION, ci_hi DOUBLE PRECISION,
  sample_errors INT, expected DOUBLE PRECISION,
  PRIMARY KEY(training_run_id, eval_id, step));

CREATE TABLE IF NOT EXISTS anomalies(
  id TEXT PRIMARY KEY, training_run_id TEXT, eval_id TEXT, step INT, kind TEXT, severity TEXT,
  delta DOUBLE PRECISION, from_step INT, diagnosis TEXT, cause TEXT, signals JSONB,
  categories JSONB, samples JSONB, created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(training_run_id, eval_id, step));

-- MOCK-ONLY: the checkpoint-ref → real-model resolver (§4).
CREATE TABLE IF NOT EXISTS checkpoint_models(
  model_ref TEXT PRIMARY KEY, real_model TEXT, mock_output TEXT, params JSONB);
