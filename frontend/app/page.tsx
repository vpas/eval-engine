"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { getRuns, getCatalog, launchRun, getEvals, launchFromEval, type Run, type Plugin, type Entity } from "@/lib/api";
import { StatusPill, AccuracyBar, ago } from "@/components/ui";

const ACTIVE = new Set(["queued", "expanding", "running", "finalizing"]);

export default function Home() {
  const router = useRouter();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [launch, setLaunch] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = () => getRuns().then((r) => alive && (setRuns(r), setErr(null))).catch((e) => alive && setErr(String(e)));
    tick();
    const t = setInterval(tick, 4000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const stats = useMemo(() => {
    if (!runs) return null;
    const done = runs.filter((r) => r.accuracy != null);
    const avg = done.length ? done.reduce((a, r) => a + (r.accuracy || 0), 0) / done.length : null;
    const active = runs.filter((r) => ACTIVE.has(r.status ?? "")).length;
    const models = new Set(runs.map((r) => r.model)).size;
    return { total: runs.length, avg, active, models };
  }, [runs]);

  return (
    <main>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16, marginBottom: 22 }}>
        <div>
          <div className="eyebrow">control plane</div>
          <h1 style={{ margin: "4px 0 0", fontSize: 26, fontWeight: 600, letterSpacing: "-0.4px" }}>Evaluation Runs</h1>
        </div>
        <span style={{ flex: 1 }} />
        <button className="btn primary" onClick={() => setLaunch(true)}>+ NEW RUN</button>
      </div>

      <div className="stats">
        <Stat k="total runs" v={stats ? String(stats.total) : "—"} />
        <Stat k="avg accuracy" v={stats?.avg != null ? `${Math.round(stats.avg * 100)}%` : "—"} signal />
        <Stat k="active now" v={stats ? String(stats.active) : "—"} />
        <Stat k="models" v={stats ? String(stats.models) : "—"} />
      </div>

      <div className="panel">
        <div className="panel-h">
          <h2>runs</h2>
          <span style={{ flex: 1 }} />
          <span className="dim mono" style={{ fontSize: 11 }}>{runs ? `${runs.length} total · auto-refresh 4s` : "loading…"}</span>
        </div>
        {err && <div className="empty" style={{ color: "var(--fail)" }}>error: {err}</div>}
        {!err && runs && runs.length === 0 && <div className="empty">no runs yet — launch one with “+ NEW RUN”.</div>}
        {!err && !runs && <div className="empty"><span className="spin" /> loading runs…</div>}
        {runs && runs.length > 0 && (
          <table className="grid">
            <thead>
              <tr>
                <th>run</th><th>eval</th><th>model</th><th>status</th><th>accuracy</th>
                <th className="right">n</th><th>by</th><th className="right">age</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} onClick={() => router.push(`/runs/${r.id}`)}>
                  <td className="mono" style={{ color: "var(--signal)" }}>{r.id}</td>
                  <td>{r.eval}</td>
                  <td className="mono muted" style={{ fontSize: 12 }}>{r.model}</td>
                  <td>{r.status ? <StatusPill status={r.status} /> : <span className="dim">—</span>}</td>
                  <td><AccuracyBar value={r.accuracy} /></td>
                  <td className="right num">{r.total}</td>
                  <td className="mono muted" style={{ fontSize: 12 }} title={r.created_by || ""}>{r.created_by ? r.created_by.split("@")[0] : "—"}</td>
                  <td className="right mono dim" style={{ fontSize: 12 }}>{ago(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {launch && <LaunchDrawer onClose={() => setLaunch(false)} onLaunched={(id) => { setLaunch(false); router.push(`/runs/${id}`); }} />}
    </main>
  );
}

function Stat({ k, v, signal }: { k: string; v: string; signal?: boolean }) {
  return <div className="stat"><div className="k">{k}</div><div className={`v${signal ? " signal" : ""}`}>{v}</div></div>;
}

function LaunchDrawer({ onClose, onLaunched }: { onClose: () => void; onLaunched: (id: string) => void }) {
  const [cat, setCat] = useState<Plugin[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<"adhoc" | "eval">("adhoc");
  const [evals, setEvals] = useState<Entity[]>([]);
  const [f, setF] = useState({ eval: "capitals_qa", dataset: "examples/qa.jsonl", model: "openai/meta-llama/llama-3.1-8b-instruct", harness: "single_turn", scorer: "includes", batch_size: 20 });
  const [ef, setEf] = useState({ evalId: "", model: "mockllm/model", batch_size: 20 });

  useEffect(() => { getCatalog().then(setCat).catch(() => setCat([])); }, []);
  useEffect(() => { getEvals().then((es) => { setEvals(es); if (es[0]) setEf((s) => ({ ...s, evalId: es[0].id })); }).catch(() => setEvals([])); }, []);
  const harnesses = cat?.filter((p) => p.kind === "harness") ?? [];
  const scorers = cat?.filter((p) => p.kind === "scorer") ?? [];
  const pickedEval = evals.find((e) => e.id === ef.evalId);

  const submit = async () => {
    setBusy(true); setErr(null);
    try {
      let run_id: string;
      if (mode === "eval") {
        const body: any = { model: ef.model, batch_size: Number(ef.batch_size) || 20 };
        if (ef.model.startsWith("mockllm")) body.mock_output = "Paris";
        ({ run_id } = await launchFromEval(ef.evalId, body));
      } else {
        const spec: any = {
          eval: f.eval, dataset: f.dataset, model: f.model,
          harness: { type: f.harness },
          scorers: [{ type: f.scorer, config: { ignore_case: true } }],
          batch_size: Number(f.batch_size) || 20,
        };
        if (f.model.startsWith("mockllm")) spec.mock_output = "Paris";
        ({ run_id } = await launchRun(spec));
      }
      onLaunched(run_id);
    } catch (e) { setErr(String(e)); setBusy(false); }
  };

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer">
        <div className="drawer-h">
          <strong style={{ fontFamily: "var(--mono)", fontSize: 13, letterSpacing: 1, textTransform: "uppercase", color: "var(--muted)" }}>Launch run</strong>
          <span style={{ flex: 1 }} />
          <button className="btn ghost" onClick={onClose}>esc</button>
        </div>
        <div style={{ padding: 20 }}>
          <div className="seg" style={{ display: "flex", gap: 6, marginBottom: 16 }}>
            <button className={`btn ${mode === "adhoc" ? "primary" : "ghost"}`} style={{ flex: 1 }} onClick={() => setMode("adhoc")}>ad-hoc</button>
            <button className={`btn ${mode === "eval" ? "primary" : "ghost"}`} style={{ flex: 1 }} onClick={() => setMode("eval")}>from registered eval</button>
          </div>
          {mode === "eval" ? (
            <>
              <div className="field"><label>registered eval</label>
                {evals.length ? (
                  <select value={ef.evalId} onChange={(e) => setEf({ ...ef, evalId: e.target.value })}>
                    {evals.map((e) => <option key={e.id} value={e.id}>{e.id} · v{e.version}</option>)}
                  </select>
                ) : <div className="hint">no evals registered yet — register one via POST /evals.</div>}
                {pickedEval && <span className="hint">dataset <b>{pickedEval.body.dataset}</b> · harness <b>{pickedEval.body.default_harness?.type}</b> · scorers <b>{(pickedEval.body.default_scorers ?? []).map((s: any) => s.type).join(", ")}</b></span>}
              </div>
              <div className="row">
                <div className="field"><label>model</label><input value={ef.model} onChange={(e) => setEf({ ...ef, model: e.target.value })} /><span className="hint">openai/&lt;id&gt; via the gateway · mockllm/model for a dry run</span></div>
                <div className="field"><label>batch size</label><input type="number" value={ef.batch_size} onChange={(e) => setEf({ ...ef, batch_size: Number(e.target.value) })} /></div>
              </div>
              {err && <div style={{ color: "var(--fail)", fontFamily: "var(--mono)", fontSize: 12, marginBottom: 12 }}>{err}</div>}
              <button className="btn primary" style={{ width: "100%", padding: 12 }} disabled={busy || !ef.evalId} onClick={submit}>
                {busy ? <><span className="spin" /> launching…</> : "▸ LAUNCH FROM EVAL"}
              </button>
            </>
          ) : (
          <>
          <div className="row">
            <div className="field"><label>eval</label><input value={f.eval} onChange={(e) => setF({ ...f, eval: e.target.value })} /></div>
            <div className="field"><label>batch size</label><input type="number" value={f.batch_size} onChange={(e) => setF({ ...f, batch_size: Number(e.target.value) })} /></div>
          </div>
          <div className="field"><label>dataset</label><input value={f.dataset} onChange={(e) => setF({ ...f, dataset: e.target.value })} /><span className="hint">path bundled in the worker image (e.g. examples/qa.jsonl)</span></div>
          <div className="field"><label>model</label><input value={f.model} onChange={(e) => setF({ ...f, model: e.target.value })} /><span className="hint">openai/&lt;id&gt; routes via the gateway · mockllm/model for a dry run</span></div>
          <div className="row">
            <div className="field"><label>harness</label>
              <select value={f.harness} onChange={(e) => setF({ ...f, harness: e.target.value })}>
                {harnesses.map((h) => <option key={h.name} value={h.name}>{h.name}</option>)}
              </select>
            </div>
            <div className="field"><label>scorer</label>
              <select value={f.scorer} onChange={(e) => setF({ ...f, scorer: e.target.value })}>
                {scorers.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
              </select>
            </div>
          </div>
          {err && <div style={{ color: "var(--fail)", fontFamily: "var(--mono)", fontSize: 12, marginBottom: 12 }}>{err}</div>}
          <button className="btn primary" style={{ width: "100%", padding: 12 }} disabled={busy} onClick={submit}>
            {busy ? <><span className="spin" /> launching…</> : "▸ LAUNCH"}
          </button>
          </>
          )}
        </div>
      </aside>
    </>
  );
}
