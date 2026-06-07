// Typed client for the eval-engine backend (proxied at /be/* by next.config rewrites).

export type Run = {
  id: string;
  eval: string;
  eval_version?: number;
  model: string;
  accuracy: number | null;
  total: number;
  cost?: number | null;
  created_at: string;
  created_by: string | null;
  status?: string;
  sweep?: string | null;
};

export type PluginRef = { type: string; version?: number; config?: Record<string, any> };
export type RunSpec = {
  eval: string;
  eval_version?: number;
  dataset: string;
  model: string;
  harness: PluginRef;
  scorers: PluginRef[];
  team?: string | null;
  lane?: string | null;
  limit?: number | null;
  batch_size?: number;
  epochs?: number;
  temperature?: number | null;
  seed?: number | null;
  budget_usd?: number | null;
  transcript_sample_rate?: number | null;
  mock_output?: string | null;
  mock_tool_calls?: Record<string, any>[] | null;
};

export type RunDetail = {
  id: string;
  eval_id: string;
  eval_version?: number;
  model: string;
  provider?: string;
  model_id?: string;
  harness?: string;
  scorers?: string[];
  status: "queued" | "expanding" | "running" | "finalizing" | "completed" | "failed" | "cancelled" | "budget_exceeded";
  total: number;
  done: number;
  failed: number;
  accuracy?: number | null;
  cost_usd?: number | null;
  dataset_hash?: string | null;
  created_by?: string | null;
  team?: string | null;
  image_digest?: string | null;
  lane?: string | null;
  created_at?: string;
  finished_at?: string | null;
  provider_fingerprint?: string | null;
  progress: Record<string, number>;
  spec?: RunSpec | null;
};

export type Results = {
  summary: { samples: number; passed: number; accuracy: number; accuracy_ci?: [number, number]; mean_score: number; tokens: number; cost_usd: number };
  by_category: { category: string; n: number; passed: number; accuracy: number }[];
  samples: { sample_id: string; passed: number; category: string | null; score: number; transcript_uri: string; tokens?: number; latency_ms?: number; error_type?: string }[];
};

export type Plugin = { kind: string; name: string; version: string; description: string; primary_metric?: string | null };
export type Entity = { id: string; version: number; body: Record<string, any>; created_by: string | null; created_at: string | null };

const j = async (r: Response) => {
  if (!r.ok) throw new Error((await r.text()) || `${r.status}`);
  return r.json();
};
const opts = { cache: "no-store" as const };

// --- runs / catalog / transcript ---------------------------------------------------------------
export const getRuns = (): Promise<Run[]> => fetch("/be/runs", opts).then(j);
export const getRun = (id: string): Promise<RunDetail> => fetch(`/be/runs/${id}`, opts).then(j);
export const getResults = (id: string): Promise<Results> => fetch(`/be/runs/${id}/results`, opts).then(j);
export const getCatalog = (): Promise<Plugin[]> => fetch("/be/catalog", opts).then(j);
export const getMe = (): Promise<{ email: string | null }> => fetch("/be/me", opts).then(j);
export const getTranscript = (uri: string): Promise<string> =>
  fetch(`/be/transcript?uri=${encodeURIComponent(uri)}`, opts).then((r) => r.text());
export const getEvals = (): Promise<Entity[]> => fetch("/be/evals", opts).then(j);
export const getDatasets = (): Promise<Entity[]> => fetch("/be/datasets", opts).then(j);
export const getModels = (): Promise<Entity[]> => fetch("/be/models", opts).then(j);

export type LaunchSpec = {
  eval: string;
  dataset: string;
  model: string;
  harness: { type: string; version?: string };
  scorers: { type: string; config?: Record<string, unknown> }[];
  batch_size?: number;
  limit?: number;
  epochs?: number;
  temperature?: number;
  budget_usd?: number;
  lane?: string;
  mock_output?: string;
};

const post = (url: string, body?: unknown) =>
  fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined }).then(j);

export const launchRun = (spec: LaunchSpec): Promise<{ run_id: string; status: string }> => post("/be/runs", spec);
export const rerunRun = (id: string): Promise<{ run_id: string; status: string; rerun_of: string }> => post(`/be/runs/${id}/rerun`);
export const cancelRun = (id: string): Promise<{ run_id: string; status: string; cancelled_queued: number; done: number; failed: number }> => post(`/be/runs/${id}/cancel`);
export const launchFromEval = (
  evalId: string,
  body: { model: string; batch_size?: number; limit?: number; epochs?: number; budget_usd?: number; mock_output?: string; temperature?: number; seed?: number; transcript_sample_rate?: number },
): Promise<{ run_id: string; status: string; from_eval: string; eval_version: number }> => post(`/be/evals/${evalId}/launch`, body);

// --- ops dashboard (eval_engine/ops.py) --------------------------------------------------------
export type OpsComponent = {
  name: string;
  status: "ok" | "degraded" | "down" | "idle" | "scaling" | "unknown";
  detail: string;
  metrics: Record<string, string | number | null>;
  logs_url: string | null;
  last_seen: string | null;
};
export type OpsPod = { name: string; phase: string; ready: boolean; restarts: number; reason: string | null; node: string | null; logs_url: string | null };
export type OpsWorkload = { app: string; namespace: string; ready: number; desired: number; pods: OpsPod[] };
export type OpsStatus = {
  cluster: { project: string | null; cluster: string; zone: string; namespace: string };
  generated_at: string;
  overall: "ok" | "degraded" | "down";
  components: OpsComponent[];
  workloads: OpsWorkload[];
  keda: { name: string; target: string | null; min: number | null; max: number | null; current: number | null }[];
  queues: {
    ledger: Record<string, number>;
    lanes: Record<string, number>;
    runs: Record<string, number>;
    samples_per_s: number | null;
    admission: { global_max: number; interactive_reserve: number; running: number };
  };
  active_runs: {
    id: string; lane: string; status: string; total: number; done: number; failed: number;
    cost_usd: number; created_at: string | null; model: string; queued: number; running: number;
    logs_url: string | null;
  }[];
  failures: { run_id: string; sample_id: string; error_type: string; attempts: number; eval_id: string | null; model: string | null; finished_at: string | null; logs_url: string | null }[];
  audit: { ts: string | null; actor: string | null; action: string; target: string; detail: any }[];
};

export const getOps = (): Promise<OpsStatus> => fetch("/be/ops/status", opts).then(j);
export const getRunLogsUrl = (runId: string): Promise<{ url: string | null }> =>
  fetch(`/be/ops/logs?run_id=${encodeURIComponent(runId)}`, opts).then(j);

export type LiveSample = {
  sample_id: string;
  status: string;
  attempts: number;
  claimed_by: string | null;
  group_key: string | null;
  error_type: string | null;
  lease_s: number | null;
  worker_logs_url: string | null;
  sandbox_logs_url: string | null;
};
export type RunLive = {
  agentic: boolean;
  sandbox: string | null;
  samples: LiveSample[];
  counts: Record<string, number>;
};
export const getRunLive = (id: string): Promise<RunLive> => fetch(`/be/runs/${id}/live`, opts).then(j);

// --- training monitor (docs/TRAINING_MONITOR.md) -----------------------------------------------
export type SuiteEntry = { eval: string; version?: number; role?: string; color?: string };
export type TrainingRun = {
  id: string;
  model: string;
  base: string;
  status: "watching" | "training" | "completed" | "failed" | "stopped";
  current_step: number;
  planned_steps: number | null;
  source: string;
  owner: string;
  body: {
    suite?: SuiteEntry[];
    config?: Record<string, any>;
    hardware?: string;
    precision?: string;
    glyph?: string;
    base?: string;
    [k: string]: any;
  };
  created_at: string | null;
  finished_at: string | null;
  best_checkpoints?: Record<string, { step: number; accuracy: number; run_id: string }>;
  anomaly_count?: number;
};

export type Checkpoint = {
  id: string;
  training_run_id: string;
  step: number;
  model_ref: string;
  tokens: number;
  status: "discovered" | "evaluating" | "evaluated" | "error" | "skipped";
  train_metrics: { loss?: number; grad?: number; lr?: number; throughput?: number };
  discovered_at: string | null;
};

export type ScorePoint = {
  eval_id: string;
  step: number;
  run_id: string | null;
  n: number | null;
  passed: number | null;
  accuracy: number | null;
  ci_lo: number | null;
  ci_hi: number | null;
  sample_errors: number;
  expected: number | null;
};

export type Anomaly = {
  id: string;
  eval: string;
  step: number;
  kind: "regression" | "drift" | "plateau";
  severity: "high" | "medium" | "low";
  delta: number;
  from: number | null;
  diagnosis: string;
  cause: string;
  signals: { k: string; v: string; note: string; bad: boolean }[];
  categories: { cat: string; acc: number; prev: number }[];
  samples: string[];
};

export const getTrainingRuns = (): Promise<TrainingRun[]> => fetch("/be/training", opts).then(j);
export const getTrainingRun = (id: string): Promise<TrainingRun> => fetch(`/be/training/${id}`, opts).then(j);
export const getCheckpoints = (id: string): Promise<Checkpoint[]> => fetch(`/be/training/${id}/checkpoints`, opts).then(j);
export const getSeries = (id: string): Promise<Record<string, ScorePoint[]>> => fetch(`/be/training/${id}/series`, opts).then(j);
export const getAnomalies = (id: string): Promise<Anomaly[]> => fetch(`/be/training/${id}/anomalies`, opts).then(j);
export const scanTraining = (id: string): Promise<{ fanned_out: number; evaluated: number }> => post(`/be/training/${id}/scan`);

// --- formatting helpers ------------------------------------------------------------------------
export const pct = (v: number | null | undefined) => (v == null ? "—" : Math.round(v * 100).toString());
export const fmtN = (n: number | null | undefined) => {
  if (n == null) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
};
export const fmtCost = (c: number | null | undefined) => {
  if (!c) return "$0";
  if (c < 0.01) return `$${c.toExponential(2)}`;
  if (c < 1) return `$${c.toFixed(4)}`;
  return `$${c.toFixed(2)}`;
};
export const fmtStep = (s: number) => (s >= 1000 ? s / 1000 + "k" : String(s));
export const fmtTok = (t: number) => (t >= 1e9 ? (t / 1e9).toFixed(0) + "B" : (t / 1e6).toFixed(0) + "M");
export const ago = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return iso;
  const s = Math.max(0, (Date.now() - d) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};
