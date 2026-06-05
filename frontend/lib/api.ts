// Typed client for the eval-engine backend (proxied at /be/*).

export type Run = {
  id: string;
  eval: string;
  model: string;
  accuracy: number | null;
  total: number;
  created_at: string;
  created_by: string | null;
  status?: string;
};

export type RunDetail = {
  id: string;
  eval_id: string;
  model: string;
  status: "queued" | "expanding" | "running" | "finalizing" | "completed" | "failed" | "cancelled";
  total: number;
  done: number;
  failed: number;
  progress: Record<string, number>;
};

export type Results = {
  summary: { samples: number; passed: number; accuracy: number; mean_score: number; tokens: number; cost_usd: number };
  by_category: { category: string; n: number; passed: number; accuracy: number }[];
  samples: { sample_id: string; passed: number; category: string | null; score: number; transcript_uri: string }[];
};

export type Plugin = { kind: string; name: string; version: string; description: string };

const j = async (r: Response) => {
  if (!r.ok) throw new Error((await r.text()) || `${r.status}`);
  return r.json();
};

export const getRuns = (): Promise<Run[]> => fetch("/be/runs", { cache: "no-store" }).then(j);
export const getRun = (id: string): Promise<RunDetail> => fetch(`/be/runs/${id}`, { cache: "no-store" }).then(j);
export const getResults = (id: string): Promise<Results> => fetch(`/be/runs/${id}/results`, { cache: "no-store" }).then(j);
export const getCatalog = (): Promise<Plugin[]> => fetch("/be/catalog", { cache: "no-store" }).then(j);
export const getMe = (): Promise<{ email: string | null }> => fetch("/be/me", { cache: "no-store" }).then(j);
export const getTranscript = (uri: string): Promise<string> =>
  fetch(`/be/transcript?uri=${encodeURIComponent(uri)}`, { cache: "no-store" }).then((r) => r.text());

export type LaunchSpec = {
  eval: string;
  dataset: string;
  model: string;
  harness: { type: string };
  scorers: { type: string; config?: Record<string, unknown> }[];
  batch_size?: number;
  mock_output?: string;
};

export const launchRun = (spec: LaunchSpec): Promise<{ run_id: string; status: string }> =>
  fetch("/be/runs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(spec),
  }).then(j);
