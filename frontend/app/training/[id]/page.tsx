"use client";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { Empty, StatusPill } from "@/components/ui";
import { TrainingChart, type Ckpt, type Track } from "@/components/training-chart";
import {
  getTrainingRun, getSeries, getCheckpoints, getAnomalies, scanTraining,
  ago, fmtStep, fmtTok, pct,
  type TrainingRun, type ScorePoint, type Checkpoint, type Anomaly,
} from "@/lib/api";

const PALETTE = ["#58a6ff", "#3fb950", "#a371f7", "#e3b341", "#f78166", "#56d4dd", "#db61a2"];
const SEV: Record<string, string> = { high: "var(--danger)", medium: "var(--attention-fg)", low: "var(--fg-muted)" };

type Drill = { type: "anomaly"; anomaly: Anomaly } | { type: "checkpoint"; idx: number };

export default function TrainingDetail() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const [visible, setVisible] = useState<Set<string>>(new Set());
  const [xMode, setXMode] = useState<"steps" | "tokens">("steps");
  const [showLoss, setShowLoss] = useState(true);
  const [showBand, setShowBand] = useState(false);
  const [thrPP, setThrPP] = useState(1.5);
  const [drill, setDrill] = useState<Drill | null>(null);

  // The run/series/checkpoints/anomalies all advance together as the monitor evaluates new checkpoints,
  // so each polls on the same 5s cadence; react-query keeps the last good data on screen between ticks.
  const poll = { refetchInterval: 5000 };
  const runQuery = useQuery({ queryKey: ["training", id], queryFn: () => getTrainingRun(id), ...poll });
  const seriesQuery = useQuery({ queryKey: ["trainingSeries", id], queryFn: () => getSeries(id), ...poll });
  const ckptsQuery = useQuery({ queryKey: ["trainingCkpts", id], queryFn: () => getCheckpoints(id), ...poll });
  const anomQuery = useQuery({ queryKey: ["trainingAnomalies", id], queryFn: () => getAnomalies(id), ...poll });
  const run = runQuery.data ?? null;
  const series: Record<string, ScorePoint[]> = seriesQuery.data ?? {};
  const ckpts: Checkpoint[] = ckptsQuery.data ?? [];
  const anomalies: Anomaly[] = anomQuery.data ?? [];

  // Default every eval in the suite to visible once the run first loads; the user toggles thereafter.
  useEffect(() => {
    if (run) setVisible((prev) => (prev.size ? prev : new Set((run.body?.suite || []).map((e) => e.eval))));
  }, [run]);

  // "Scan now" forces the monitor to poll the checkpoint stream; on settle, re-fetch the four queries.
  const scan = useMutation({
    mutationFn: () => scanTraining(id),
    onSettled: () => {
      for (const k of ["training", "trainingSeries", "trainingCkpts", "trainingAnomalies"]) {
        qc.invalidateQueries({ queryKey: [k, id] });
      }
    },
  });
  const scanning = scan.isPending;

  const track: Track[] = useMemo(() => {
    const suite = run?.body?.suite || Object.keys(series).map((eval_) => ({ eval: eval_ }));
    return suite.map((e: any, i: number) => ({ id: e.eval, color: e.color || PALETTE[i % PALETTE.length], role: e.role }));
  }, [run, series]);

  const checkpoints: Ckpt[] = useMemo(() => {
    const ckptByStep = new Map(ckpts.map((c) => [c.step, c]));
    const steps = Array.from(new Set(Object.values(series).flat().map((p) => p.step))).sort((a, b) => a - b);
    return steps.map((step, idx) => {
      const c = ckptByStep.get(step);
      const evals: Record<string, number | undefined> = {};
      const expected: Record<string, number | undefined> = {};
      for (const [ev, pts] of Object.entries(series)) {
        const p = pts.find((x) => x.step === step);
        if (p) { evals[ev] = p.accuracy ?? undefined; expected[ev] = p.expected ?? undefined; }
      }
      return { step, idx, tokens: c?.tokens || 0, loss: c?.train_metrics?.loss, evals, expected };
    });
  }, [series, ckpts]);

  const thr = thrPP / 100;
  const activeAnoms = anomalies.filter((a) => Math.abs(a.delta) * 100 >= thrPP);
  const anomalySteps = useMemo(() => {
    const m: Record<string, Set<number>> = {};
    for (const a of activeAnoms) (m[a.eval] = m[a.eval] || new Set()).add(a.step);
    return m;
  }, [activeAnoms]);

  if (!run) return <div className="page"><Empty icon="spark"><span className="spin" /> loading training run…</Empty></div>;

  const last = checkpoints[checkpoints.length - 1];
  const trainPct = run.planned_steps ? run.current_step / run.planned_steps : 0;
  const toggleEval = (e: string) => setVisible((s) => { const n = new Set(s); n.has(e) ? n.delete(e) : n.add(e); return n; });

  return (
    <div className="page wide">
      <button className="btn ghost sm" onClick={() => router.push("/training")} style={{ marginBottom: 14 }}><Icon name="arrowleft" />all training runs</button>

      {/* header */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-b">
          <div className="between wrap" style={{ gap: 12 }}>
            <div className="vcenter gap12 wrap">
              <span className="pi" style={{ width: 30, height: 30, borderRadius: 7, background: "linear-gradient(135deg,#a371f7,#2f81f7)", display: "grid", placeItems: "center", color: "#fff", fontWeight: 700, fontSize: 12, fontFamily: "var(--mono)" }}>{run.body?.glyph || run.model.slice(0, 2).toUpperCase()}</span>
              <div>
                <div className="vcenter gap8 wrap">
                  <span className="mono" style={{ fontSize: 15, fontWeight: 600 }}>{run.model}</span>
                  <StatusPill status={run.status} />
                </div>
                <div className="subtle mono" style={{ fontSize: 11.5, marginTop: 3 }}>{run.id} · base {run.base || "—"}</div>
              </div>
            </div>
            <div className="vcenter gap8">
              <button className="btn sm" disabled={!checkpoints.length} onClick={() => setDrill({ type: "checkpoint", idx: checkpoints.length - 1 })}><Icon name="slice" />Latest checkpoint</button>
              <button className="btn sm" onClick={() => scan.mutate()} disabled={scanning}><Icon name="refresh" />{scanning ? "scanning…" : "Scan now"}</button>
            </div>
          </div>

          <div style={{ marginTop: 14 }}>
            <div className="between" style={{ marginBottom: 6 }}>
              <span className="eyebrow">training progress</span>
              <span className="mono subtle" style={{ fontSize: 11 }}>{run.current_step.toLocaleString()} / {(run.planned_steps || 0).toLocaleString()} steps · {pct(trainPct)}%</span>
            </div>
            <div className="prog"><span className="s-run" style={{ width: trainPct * 100 + "%", background: run.status === "completed" ? "var(--success)" : "var(--accent-emph)" }} /></div>
          </div>

          <div className="vcenter gap16 wrap" style={{ marginTop: 14, fontSize: 11.5 }}>
            <HeaderStat icon="layers" k="tokens" v={last ? fmtTok(last.tokens) : "—"} />
            <HeaderStat icon="pulse" k="train loss" v={last?.loss != null ? last.loss.toFixed(2) : "—"} />
            <HeaderStat icon="flask" k="evals" v={String(track.length)} />
            <HeaderStat icon="cpu" k="hardware" v={run.body?.hardware || "—"} />
            <HeaderStat icon="clock" k="started" v={ago(run.created_at)} />
            <span className="grow" />
            {activeAnoms.length > 0
              ? <span className="tag" style={{ color: "var(--danger)", borderColor: "rgba(248,81,73,.35)", background: "var(--danger-soft)" }}><Icon name="warn" size={12} />{activeAnoms.length} anomal{activeAnoms.length === 1 ? "y" : "ies"} · ≥{thrPP}pp</span>
              : <span className="tag" style={{ color: "var(--success)", borderColor: "rgba(63,185,80,.3)", background: "var(--success-soft)" }}><Icon name="check" size={12} />no anomalies ≥{thrPP}pp</span>}
          </div>
        </div>
      </div>

      {/* controls */}
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-b vcenter gap10 wrap">
          <span className="eyebrow" style={{ marginRight: 2 }}>evals</span>
          {track.map((e) => (
            <span key={e.id} className={`chip ${visible.has(e.id) ? "on" : ""}`} onClick={() => toggleEval(e.id)} style={visible.has(e.id) ? { borderColor: e.color, color: e.color } : undefined}>
              <i style={{ width: 8, height: 8, borderRadius: 2, background: visible.has(e.id) ? e.color : "var(--fg-subtle)", display: "inline-block" }} />{e.id}{e.role === "canary" ? " ◆" : ""}
            </span>
          ))}
          <span className="grow" />
          <div className={`chip ${showBand ? "on" : ""}`} onClick={() => setShowBand((s) => !s)}><Icon name="slice" size={12} />expected band</div>
          <div className={`chip ${showLoss ? "on" : ""}`} onClick={() => setShowLoss((s) => !s)}><Icon name="pulse" size={12} />loss overlay</div>
          <div className="seg">
            <button className={xMode === "steps" ? "on" : ""} onClick={() => setXMode("steps")}>steps</button>
            <button className={xMode === "tokens" ? "on" : ""} onClick={() => setXMode("tokens")}>tokens</button>
          </div>
        </div>
        <div className="panel-b vcenter gap12 wrap" style={{ borderTop: "1px solid var(--border)", paddingTop: 11, paddingBottom: 11 }}>
          <span className="eyebrow vcenter gap6" style={{ marginRight: 2 }}><Icon name="bell" size={12} />alert threshold</span>
          <span className="subtle" style={{ fontSize: 11.5 }}>flag a regression when accuracy drops more than</span>
          <input type="range" min="0.5" max="10" step="0.5" value={thrPP} onChange={(e) => setThrPP(+e.target.value)} style={{ width: 200, accentColor: "var(--danger)" }} />
          <span className="num" style={{ fontSize: 14, color: "var(--danger)", width: 54 }}>{thrPP.toFixed(1)}pp</span>
          <span className="grow" />
          <span className="subtle mono" style={{ fontSize: 10.5 }}>{showBand ? `band = expected ± ${thrPP}pp` : "enable expected band to visualize the corridor"}</span>
        </div>
      </div>

      {/* hero chart */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-h">
          <Icon name="chart" className="ic" /><h2>Eval accuracy across checkpoints</h2>
          <span className="grow" />
          <span className="sub mono">hover to scrub · click a checkpoint</span>
        </div>
        <div className="panel-b">
          {checkpoints.length === 0
            ? <Empty icon="chart">No evaluated checkpoints yet. The monitor evaluates the suite as checkpoints arrive — try <b>Scan now</b>.</Empty>
            : <TrainingChart checkpoints={checkpoints} track={track} visible={visible} xMode={xMode}
                xMax={Math.max(run.planned_steps || 0, last?.step || 0)} showLoss={showLoss} showBand={showBand}
                thr={thr} anomalySteps={anomalySteps} onPick={(idx) => setDrill({ type: "checkpoint", idx })} />}
        </div>
      </div>

      {/* heatmap + anomalies */}
      <div style={{ display: "grid", gridTemplateColumns: "1.55fr 1fr", gap: 16 }}>
        <Heatmap checkpoints={checkpoints} track={track} visible={visible} thr={thr} anomalySteps={anomalySteps}
          onPick={(ev, idx) => {
            const a = activeAnoms.find((x) => x.eval === ev && x.step === checkpoints[idx].step);
            setDrill(a ? { type: "anomaly", anomaly: a } : { type: "checkpoint", idx });
          }} />
        <AnomaliesPanel anomalies={activeAnoms} thrPP={thrPP} onPick={(a) => setDrill({ type: "anomaly", anomaly: a })} />
      </div>

      {drill && <DrillDrawer drill={drill} checkpoints={checkpoints} track={track} series={series} run={run}
        onClose={() => setDrill(null)} go={(ids) => router.push(`/compare?ids=${ids.join(",")}`)} />}
    </div>
  );
}

function HeaderStat({ icon, k, v }: { icon: string; k: string; v: string }) {
  return <span className="subtle vcenter gap6 nowrap"><Icon name={icon} size={12} />{k} <b className="num" style={{ color: "var(--fg)" }}>{v}</b></span>;
}

function Heatmap({ checkpoints, track, visible, thr, anomalySteps, onPick }: {
  checkpoints: Ckpt[]; track: Track[]; visible: Set<string>; thr: number;
  anomalySteps: Record<string, Set<number>>; onPick: (ev: string, idx: number) => void;
}) {
  const heat = (a: number) => { const t = Math.max(0, Math.min(1, (a - 0.4) / 0.55)); return `hsl(${t * 130} 52% ${22 + t * 14}%)`; };
  const evals = track.filter((e) => visible.has(e.id));
  return (
    <div className="panel flush">
      <div className="panel-h"><Icon name="grid" className="ic" /><h2>Eval × checkpoint matrix</h2><span className="grow" /><span className="sub mono">drop &gt; {(thr * 100).toFixed(1)}pp outlined</span></div>
      <div className="panel-b" style={{ overflowX: "auto" }}>
        {checkpoints.length === 0 ? <Empty icon="grid">No data.</Empty> : (
          <table style={{ borderCollapse: "separate", borderSpacing: 3, width: "100%" }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", fontSize: 10, color: "var(--fg-subtle)", fontWeight: 600, padding: "0 6px 4px 0" }}>eval</th>
                {checkpoints.map((c) => <th key={c.step} style={{ fontSize: 9, color: "var(--fg-subtle)", fontWeight: 500, fontFamily: "var(--mono)", paddingBottom: 4, transform: "rotate(-45deg)", height: 28, whiteSpace: "nowrap" }}>{fmtStep(c.step)}</th>)}
              </tr>
            </thead>
            <tbody>
              {evals.map((e) => (
                <tr key={e.id}>
                  <td style={{ fontSize: 10.5, fontFamily: "var(--mono)", color: e.color, paddingRight: 8, whiteSpace: "nowrap" }}>{e.id}</td>
                  {checkpoints.map((c, i) => {
                    const a = c.evals[e.id];
                    if (a == null) return <td key={c.step} style={{ width: 26, height: 24, background: "var(--panel-3)", borderRadius: 4 }} />;
                    const anom = anomalySteps[e.id]?.has(c.step);
                    const exp = c.expected[e.id];
                    const drop = exp != null && exp - a > thr;
                    return (
                      <td key={c.step} title={`${e.id} @ ${fmtStep(c.step)} — ${Math.round(a * 100)}%`} onClick={() => onPick(e.id, i)}
                        style={{ width: 26, height: 24, background: heat(a), borderRadius: 4, textAlign: "center", fontSize: 9.5, color: "rgba(255,255,255,.9)", fontFamily: "var(--mono)", cursor: "pointer", outline: anom ? "2px solid var(--danger)" : drop ? "1px solid rgba(248,81,73,.6)" : "none", outlineOffset: -1 }}>
                        {Math.round(a * 100)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function AnomaliesPanel({ anomalies, thrPP, onPick }: { anomalies: Anomaly[]; thrPP: number; onPick: (a: Anomaly) => void }) {
  const order: Record<string, number> = { high: 0, medium: 1, low: 2 };
  const list = anomalies.slice().sort((a, b) => order[a.severity] - order[b.severity]);
  return (
    <div className="panel flush">
      <div className="panel-h"><Icon name="warn" className="ic" /><h2>Anomalies</h2><span className="grow" /><span className="badge">{list.length}</span></div>
      {list.length === 0 && <Empty icon="check">No regressions exceed the {thrPP}pp threshold.</Empty>}
      <div>
        {list.map((a) => (
          <div key={a.id} className="click" onClick={() => onPick(a)} style={{ padding: "12px 14px", borderBottom: "1px solid var(--border-muted)", cursor: "pointer" }}>
            <div className="between" style={{ marginBottom: 5 }}>
              <span className="vcenter gap8">
                <span style={{ width: 8, height: 8, borderRadius: "50%", background: SEV[a.severity] }} />
                <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>{a.eval}</span>
                <span className="tag" style={{ fontSize: 9.5 }}>{a.kind}</span>
              </span>
              <span className="num" style={{ color: "var(--danger)", fontSize: 12 }}>{(a.delta * 100).toFixed(1)}pp</span>
            </div>
            <div className="subtle" style={{ fontSize: 11, lineHeight: 1.5 }}>{a.cause}</div>
            <div className="vcenter gap10" style={{ marginTop: 7 }}>
              <span className="tag" style={{ fontSize: 9.5, color: "var(--accent-fg)" }}>{a.diagnosis}</span>
              <span className="hash">@ step {fmtStep(a.step)}{a.from != null ? ` · vs ${fmtStep(a.from)}` : ""}</span>
              <span className="grow" />
              <span className="mono" style={{ fontSize: 10.5, color: SEV[a.severity] }}>{a.severity} · inspect ›</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function DrillDrawer({ drill, checkpoints, track, series, run, onClose, go }: {
  drill: Drill; checkpoints: Ckpt[]; track: Track[]; series: Record<string, ScorePoint[]>;
  run: TrainingRun; onClose: () => void; go: (ids: string[]) => void;
}) {
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer">
        <div className="drawer-h">
          <Icon name="slice" className="ic" style={{ color: "var(--accent-fg)" }} />
          <strong style={{ fontSize: 13 }}>{drill.type === "checkpoint" ? "Checkpoint inspector" : "Anomaly drill-down"}</strong>
          <span className="grow" />
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div style={{ padding: "14px 16px" }}>
          {drill.type === "anomaly"
            ? <AnomalyDrill a={drill.anomaly} series={series} go={go} />
            : <CheckpointDrill idx={drill.idx} checkpoints={checkpoints} track={track} />}
        </div>
      </aside>
    </>
  );
}

function AnomalyDrill({ a, series, go }: { a: Anomaly; series: Record<string, ScorePoint[]>; go: (ids: string[]) => void }) {
  const curRun = series[a.eval]?.find((p) => p.step === a.step)?.run_id;
  const baseRun = a.from != null ? series[a.eval]?.find((p) => p.step === a.from)?.run_id : null;
  return (
    <>
      <div className="vcenter gap8" style={{ marginBottom: 10 }}>
        <span className="pill failed" style={{ background: a.severity === "low" ? "var(--panel-3)" : undefined }}><span className="led" style={{ background: SEV[a.severity] }} />{a.severity} {a.kind}</span>
        <span className="mono" style={{ fontSize: 14, fontWeight: 600 }}>{a.eval}</span>
      </div>
      <div className="vcenter gap10" style={{ marginBottom: 12 }}>
        <DeltaBox label="Δ vs expected" v={`${(a.delta * 100).toFixed(1)}pp`} bad />
        <DeltaBox label="at step" v={fmtStep(a.step)} />
        {a.from != null && <DeltaBox label="baseline" v={fmtStep(a.from)} />}
      </div>

      <div className="panel" style={{ marginBottom: 14, borderColor: "rgba(88,166,255,.3)" }}>
        <div className="panel-b">
          <div className="eyebrow vcenter gap6" style={{ marginBottom: 6, color: "var(--accent-fg)" }}><Icon name="spark" size={11} />diagnosis · {a.diagnosis}</div>
          <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>{a.cause}</div>
        </div>
      </div>

      <div className="eyebrow" style={{ marginBottom: 8 }}>correlated signals</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 16 }}>
        {a.signals.map((s) => (
          <div key={s.k} className="panel" style={{ background: "var(--panel-2)" }}>
            <div className="panel-b" style={{ padding: 10 }}>
              <div className="between"><span className="subtle" style={{ fontSize: 10.5 }}>{s.k}</span><span className="num" style={{ fontSize: 13, color: s.bad ? "var(--danger)" : "var(--success)" }}>{s.v}</span></div>
              <div className="subtle" style={{ fontSize: 10, marginTop: 2 }}>{s.note}</div>
            </div>
          </div>
        ))}
      </div>

      {a.categories.length > 0 && (
        <>
          <div className="eyebrow" style={{ marginBottom: 8 }}>category regression <span className="subtle" style={{ fontWeight: 400 }}>(now vs baseline)</span></div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 16 }}>
            {a.categories.slice().sort((x, y) => (x.acc - x.prev) - (y.acc - y.prev)).map((c) => <CatRegress key={c.cat} {...c} />)}
          </div>
        </>
      )}

      {a.samples.length > 0 && (
        <>
          <div className="eyebrow" style={{ marginBottom: 8 }}>regressed samples <span className="subtle" style={{ fontWeight: 400 }}>· passed before, fail now</span></div>
          <div className="panel flush" style={{ marginBottom: 12 }}>
            {a.samples.map((s) => (
              <div key={s} className="between" style={{ padding: "8px 12px", borderBottom: "1px solid var(--border-muted)" }}>
                <span className="vcenter gap8"><span className="cellmark fail" style={{ width: 16, height: 16 }}><Icon name="x" size={11} /></span><span className="mono" style={{ fontSize: 12 }}>{s}</span></span>
                <span className="vcenter gap6"><span className="mono subtle" style={{ fontSize: 10.5 }}>was pass</span><Icon name="arrowright" size={11} className="ic" style={{ color: "var(--danger)" }} /><span className="mono" style={{ fontSize: 10.5, color: "var(--danger)" }}>fail</span></span>
              </div>
            ))}
          </div>
        </>
      )}

      {curRun && baseRun && (
        <button className="btn accent sm" style={{ width: "100%" }} onClick={() => go([baseRun, curRun])}><Icon name="diff" />Diff checkpoints in Compare</button>
      )}
    </>
  );
}

function CheckpointDrill({ idx, checkpoints, track }: { idx: number; checkpoints: Ckpt[]; track: Track[] }) {
  const c = checkpoints[idx];
  const prev = checkpoints[idx - 1];
  const movers = track.map((e) => ({
    eval: e.id, color: e.color, acc: c.evals[e.id],
    delta: c.evals[e.id] != null && prev?.evals[e.id] != null ? (c.evals[e.id] as number) - (prev.evals[e.id] as number) : null,
  })).filter((m) => m.acc != null).sort((a, b) => (a.delta ?? 0) - (b.delta ?? 0));
  return (
    <>
      <div className="vcenter gap10" style={{ marginBottom: 12 }}>
        <DeltaBox label="step" v={fmtStep(c.step)} />
        <DeltaBox label="tokens" v={fmtTok(c.tokens)} />
        <DeltaBox label="loss" v={c.loss != null ? c.loss.toFixed(2) : "—"} />
      </div>
      {prev && <div className="subtle" style={{ fontSize: 11.5, marginBottom: 10 }}>Movement vs previous checkpoint (step {fmtStep(prev.step)}):</div>}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {movers.map((m) => (
          <div key={m.eval} className="vcenter gap10" style={{ fontSize: 12 }}>
            <span className="vcenter gap6" style={{ width: 150 }}><i style={{ width: 8, height: 8, borderRadius: 2, background: m.color }} /><span className="mono" style={{ fontSize: 11 }}>{m.eval}</span></span>
            <span className="num" style={{ width: 44, textAlign: "right" }}>{Math.round((m.acc as number) * 100)}%</span>
            <div style={{ flex: 1, position: "relative", height: 14, background: "var(--panel-3)", borderRadius: 4 }}>
              <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "var(--border)" }} />
              {m.delta != null && <div style={{ position: "absolute", top: 2, bottom: 2, borderRadius: 3, background: m.delta >= 0 ? "var(--success)" : "var(--danger)", left: m.delta >= 0 ? "50%" : `calc(50% - ${Math.min(48, Math.abs(m.delta) * 100 * 3)}%)`, width: Math.min(48, Math.abs(m.delta) * 100 * 3) + "%" }} />}
            </div>
            <span className="num" style={{ width: 50, textAlign: "right", color: (m.delta ?? 0) >= 0 ? "var(--success)" : "var(--danger)" }}>{m.delta == null ? "—" : (m.delta >= 0 ? "+" : "") + (m.delta * 100).toFixed(1)}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function CatRegress({ cat, acc, prev }: { cat: string; acc: number; prev: number }) {
  const d = acc - prev, down = d < 0;
  return (
    <div className="vcenter gap10" style={{ fontSize: 12 }}>
      <span className="mono" style={{ width: 86, color: "var(--fg-muted)" }}>{cat || "—"}</span>
      <div style={{ flex: 1, position: "relative", height: 16, background: "var(--panel-3)", borderRadius: 4 }}>
        <div style={{ position: "absolute", top: 2, bottom: 2, left: 2, borderRadius: 3, background: "var(--border)", width: Math.max(0, prev * 96) + "%", opacity: 0.5 }} />
        <div style={{ position: "absolute", top: 2, bottom: 2, left: 2, borderRadius: 3, background: down ? "var(--danger)" : "var(--success)", width: Math.max(0, acc * 96) + "%" }} />
      </div>
      <span className="num" style={{ width: 38, textAlign: "right" }}>{Math.round(acc * 100)}%</span>
      <span className="num" style={{ width: 52, textAlign: "right", color: down ? "var(--danger)" : "var(--success)" }}>{down ? "" : "+"}{(d * 100).toFixed(0)}pp</span>
    </div>
  );
}

function DeltaBox({ label, v, bad }: { label: string; v: string; bad?: boolean }) {
  return (
    <div style={{ flex: 1 }}>
      <div className="subtle" style={{ fontSize: 10 }}>{label}</div>
      <div className="num" style={{ fontSize: 14, marginTop: 1, color: bad ? "var(--danger)" : "var(--fg)" }}>{v}</div>
    </div>
  );
}
