"use client";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { Delta, Empty, Provider } from "@/components/ui";
import { getRun, getResults, getRuns, fmtCost, fmtN, pct, type RunDetail, type Results, type Run } from "@/lib/api";

type Loaded = { id: string; run?: RunDetail; res?: Results; err?: string };
const ciHalf = (l?: Loaded) => { const c = l?.res?.summary.accuracy_ci; return c ? (c[1] - c[0]) / 2 : 0; };

function CompareInner() {
  const sp = useSearchParams();
  const router = useRouter();
  const initial = (sp.get("ids") || "").split(",").map((s) => s.trim()).filter(Boolean);
  const [ids, setIds] = useState<string[]>(initial);
  const [data, setData] = useState<Record<string, Loaded>>({});
  const [mode, setMode] = useState<"leaderboard" | "ab" | "sample">("leaderboard");
  const [picker, setPicker] = useState(false);
  const [allRuns, setAllRuns] = useState<Run[]>([]);
  const [aIdx, setA] = useState(0);
  const [bIdx, setB] = useState(1);

  useEffect(() => {
    ids.forEach((id) => {
      if (data[id]) return;
      setData((d) => ({ ...d, [id]: { id } }));
      Promise.all([getRun(id), getResults(id).catch(() => undefined)])
        .then(([run, res]) => setData((d) => ({ ...d, [id]: { id, run, res } })))
        .catch((e) => setData((d) => ({ ...d, [id]: { id, err: String(e?.message || e) } })));
    });
  }, [ids]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { getRuns().then(setAllRuns).catch(() => {}); }, []);

  const runs = ids.map((id) => data[id]).filter((l): l is Loaded => !!l && !!l.run);
  const evalsInSet = Array.from(new Set(runs.map((r) => r.run!.eval_id)));
  const mixedEval = evalsInSet.length > 1;
  const A = runs[aIdx], B = runs[bIdx];

  const add = (id: string) => setIds((s) => (s.includes(id) ? s : [...s, id]));
  const remove = (id: string) => setIds((s) => s.filter((x) => x !== id));

  return (
    <div className="page wide">
      <div className="between" style={{ marginBottom: 16 }}>
        <div>
          <div className="eyebrow">analysis</div>
          <h1 className="title" style={{ marginTop: 4 }}>Compare runs</h1>
        </div>
        <div className="seg">
          <button className={mode === "leaderboard" ? "on" : ""} onClick={() => setMode("leaderboard")}><Icon name="trophy" size={13} />Leaderboard</button>
          <button className={mode === "ab" ? "on" : ""} onClick={() => setMode("ab")}><Icon name="diff" size={13} />A/B diff</button>
          <button className={mode === "sample" ? "on" : ""} onClick={() => setMode("sample")}><Icon name="list" size={13} />Per-sample</button>
        </div>
      </div>

      {/* selected runs bar */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-b vcenter gap8 wrap">
          <span className="eyebrow" style={{ marginRight: 4 }}>runs</span>
          {runs.map((r) => (
            <span key={r.id} className="chip on" style={{ paddingRight: 6 }}>
              <span className="mono">{r.id}</span><span className="subtle">·</span><Provider id={r.run!.model} />
              <span className="x" style={{ cursor: "pointer", marginLeft: 2, display: "inline-flex" }} onClick={() => remove(r.id)}><Icon name="x" size={12} /></span>
            </span>
          ))}
          <button className="chip" onClick={() => setPicker((p) => !p)}><Icon name="plus" size={12} />add run</button>
          <span className="grow" />
          {mixedEval
            ? <span className="tag" style={{ color: "var(--attention-fg)", borderColor: "rgba(210,153,34,.35)" }}><Icon name="warn" size={11} />mixed evals — scores not directly comparable</span>
            : evalsInSet[0] && <span className="tag b"><Icon name="flask" size={11} />{evalsInSet[0]}</span>}
        </div>
        {picker && (
          <div className="panel-b" style={{ borderTop: "1px solid var(--border)" }}>
            <div className="vcenter gap6 wrap">
              {allRuns.filter((r) => !ids.includes(r.id)).slice(0, 40).map((r) => (
                <span key={r.id} className="chip" onClick={() => add(r.id)}><Icon name="plus" size={11} /><span className="mono">{r.id}</span> {r.eval} · {r.model.split("/").slice(-1)[0]}</span>
              ))}
            </div>
          </div>
        )}
      </div>

      {runs.length < 2 && <Empty icon="compare">Select at least two runs to compare (dashboard checkboxes → Compare, or add ids above).</Empty>}

      {runs.length >= 2 && mode === "leaderboard" && <Leaderboard runs={runs} onOpen={(id) => router.push(`/runs/${id}`)} />}
      {runs.length >= 2 && mode === "ab" && (
        <>
          <div className="vcenter gap10" style={{ marginBottom: 14 }}>
            <ABSelect label="A" runs={runs} value={aIdx} onChange={setA} color="var(--accent-fg)" />
            <Icon name="diff" className="ic" style={{ color: "var(--fg-subtle)" }} />
            <ABSelect label="B" runs={runs} value={bIdx} onChange={setB} color="var(--done)" />
          </div>
          {A && B && A !== B ? <ABDiff A={A} B={B} /> : <Empty icon="diff">Pick two different runs for A and B.</Empty>}
        </>
      )}
      {runs.length >= 2 && mode === "sample" && <SampleDiff runs={runs} />}
    </div>
  );
}

/* ---------- LEADERBOARD ---------- */
function Leaderboard({ runs, onOpen }: { runs: Loaded[]; onOpen: (id: string) => void }) {
  const ranked = runs.slice().sort((a, b) => (b.res?.summary.accuracy ?? -1) - (a.res?.summary.accuracy ?? -1));
  const best = ranked[0]?.res?.summary.accuracy ?? 0;
  return (
    <div className="panel flush">
      <div className="panel-h"><Icon name="trophy" className="ic" /><h2>Leaderboard</h2><span className="grow" /><span className="sub mono">ranked by accuracy · 95% CI</span></div>
      <table className="grid">
        <thead><tr><th style={{ width: 40 }}>#</th><th>Model</th><th>Run</th><th style={{ width: 280 }}>Accuracy (95% CI)</th><th className="right">Δ vs best</th><th className="right">Samples</th><th className="right">Cost</th><th className="right" style={{ width: 90 }}>$/pt</th></tr></thead>
        <tbody>
          {ranked.map((r, i) => {
            const acc = r.res?.summary.accuracy ?? 0;
            const half = ciHalf(r);
            const cost = r.res?.summary.cost_usd ?? r.run?.cost_usd ?? 0;
            return (
              <tr key={r.id} className="click" onClick={() => onOpen(r.id)}>
                <td>{i === 0 ? <Icon name="trophy" className="ic" style={{ color: "var(--attention-fg)" }} /> : <span className="num subtle">{i + 1}</span>}</td>
                <td><Provider id={r.run!.model} /></td>
                <td><span className="linklike mono" style={{ fontSize: 11.5 }}>{r.id}</span></td>
                <td>
                  <div className="vcenter gap10">
                    <div style={{ flex: 1 }}><CIRow acc={acc} half={half} max={Math.max(best + 0.05, 1)} /></div>
                    <span className="num" style={{ width: 78, textAlign: "right" }}>{pct(acc)}% <span className="subtle" style={{ fontSize: 10.5 }}>±{(half * 100).toFixed(1)}</span></span>
                  </div>
                </td>
                <td className="right">{i === 0 ? <span className="subtle mono">—</span> : <Delta value={acc - best} />}</td>
                <td className="right num cellmuted">{fmtN(r.res?.summary.samples ?? r.run?.total ?? 0)}</td>
                <td className="right num cellmuted">{fmtCost(cost)}</td>
                <td className="right num subtle">{acc > 0 ? fmtCost(cost / (acc * 100)) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CIRow({ acc, half, max }: { acc: number; half: number; max: number }) {
  const sc = (x: number) => Math.max(0, Math.min(100, (x / max) * 100));
  return (
    <div className="cibar" style={{ height: 20 }}>
      <div className="axis" />
      <div className="range" style={{ left: sc(acc - half) + "%", width: (sc(acc + half) - sc(acc - half)) + "%" }} />
      <div className="point" style={{ left: sc(acc) + "%" }} />
    </div>
  );
}

/* ---------- A/B DIFF ---------- */
function ABSelect({ label, runs, value, onChange, color }: { label: string; runs: Loaded[]; value: number; onChange: (n: number) => void; color: string }) {
  return (
    <div className="vcenter gap8" style={{ flex: 1 }}>
      <span className="num" style={{ width: 22, height: 22, borderRadius: 5, background: color, color: "#0d1117", display: "grid", placeItems: "center", fontWeight: 700, fontSize: 12 }}>{label}</span>
      <select className="input" style={{ fontFamily: "var(--mono)" }} value={value} onChange={(e) => onChange(+e.target.value)}>
        {runs.map((r, i) => <option key={r.id} value={i}>{r.id} · {r.run!.model}</option>)}
      </select>
    </div>
  );
}

function ABDiff({ A, B }: { A: Loaded; B: Loaded }) {
  const ra = A.res!, rb = B.res!;
  const verdict = (rb.summary.accuracy ?? 0) - (ra.summary.accuracy ?? 0);
  const significant = ciHalf(A) + ciHalf(B) < Math.abs(verdict);
  const metrics: [string, number, number, "pct" | "num3" | "cost" | "ntok", boolean?][] = [
    ["accuracy", ra.summary.accuracy, rb.summary.accuracy, "pct"],
    ["mean score", ra.summary.mean_score, rb.summary.mean_score, "num3"],
    ["cost", ra.summary.cost_usd, rb.summary.cost_usd, "cost", true],
    ["tokens", ra.summary.tokens, rb.summary.tokens, "ntok", true],
  ];
  const catMap: Record<string, { a?: number; b?: number }> = {};
  ra.by_category.forEach((c) => (catMap[c.category || "—"] = { a: c.accuracy }));
  rb.by_category.forEach((c) => (catMap[c.category || "—"] = { ...catMap[c.category || "—"], b: c.accuracy }));
  const cats = Object.entries(catMap).filter(([, v]) => v.a != null && v.b != null)
    .map(([k, v]) => ({ cat: k, delta: (v.b as number) - (v.a as number) })).sort((x, y) => y.delta - x.delta);

  const f = (v: number, fmt: string) => fmt === "pct" ? pct(v) + "%" : fmt === "num3" ? (v ?? 0).toFixed(3) : fmt === "cost" ? fmtCost(v) : fmtN(v);

  return (
    <div>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-b vcenter gap12">
          <Icon name={Math.abs(verdict) < 0.005 ? "scale" : verdict > 0 ? "arrowup" : "arrowdown"} style={{ width: 22, height: 22, color: Math.abs(verdict) < 0.005 ? "var(--fg-muted)" : verdict > 0 ? "var(--success)" : "var(--danger)" }} />
          <div>
            <div style={{ fontSize: 15, fontWeight: 600 }}>
              {Math.abs(verdict) < 0.005 ? "Statistical tie" : <>Run <span style={{ color: verdict > 0 ? "var(--done)" : "var(--accent-fg)" }}>{verdict > 0 ? "B" : "A"}</span> wins by {Math.abs(verdict * 100).toFixed(1)}pp</>}
            </div>
            <div className="subtle" style={{ fontSize: 12 }}>{A.run!.model.split("/").slice(-1)[0]} vs {B.run!.model.split("/").slice(-1)[0]} on {A.run!.eval_id} · {fmtN(ra.summary.samples)} samples · CIs {significant ? "do not overlap (significant)" : "overlap (not significant)"}</div>
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div className="panel">
          <div className="panel-h"><Icon name="gauge" className="ic" /><h2>Headline metrics</h2></div>
          <div className="panel-b">
            <div className="vcenter" style={{ paddingBottom: 8, borderBottom: "1px solid var(--border)", fontSize: 10.5 }}>
              <span style={{ flex: 1 }} />
              <span className="num" style={{ width: 90, textAlign: "right", color: "var(--accent-fg)" }}>A</span>
              <span className="num" style={{ width: 90, textAlign: "right", color: "var(--done)" }}>B</span>
              <span style={{ width: 70 }} />
            </div>
            {metrics.map(([k, a, b, fmt, invert]) => {
              const d = fmt === "pct" || fmt === "num3" ? b - a : (b - a) / (a || 1);
              return (
                <div key={k} className="vcenter" style={{ padding: "9px 0", borderBottom: "1px solid var(--border-muted)" }}>
                  <span style={{ flex: 1, fontSize: 12.5 }}>{k}</span>
                  <span className="num" style={{ width: 90, textAlign: "right" }}>{f(a, fmt)}</span>
                  <span className="num" style={{ width: 90, textAlign: "right" }}>{f(b, fmt)}</span>
                  <span style={{ width: 70, textAlign: "right" }}><Delta value={d} suffix={fmt === "pct" || fmt === "num3" ? "pp" : "%"} invert={invert} /></span>
                </div>
              );
            })}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h"><Icon name="chart" className="ic" /><h2>Category deltas <span className="subtle" style={{ fontWeight: 400 }}>(B − A)</span></h2></div>
          <div className="panel-b">
            {cats.length === 0 ? <Empty icon="chart">No shared categories.</Empty> : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {cats.map((c) => <DivergeBar key={c.cat} cat={c.cat} delta={c.delta} />)}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function DivergeBar({ cat, delta }: { cat: string; delta: number }) {
  const w = Math.min(50, Math.abs(delta) * 100 * 2), pos = delta >= 0;
  return (
    <div className="vcenter gap10" style={{ fontSize: 12 }}>
      <span className="mono" style={{ width: 110, color: "var(--fg-muted)", textAlign: "right", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{cat}</span>
      <div style={{ flex: 1, position: "relative", height: 16, background: "var(--panel-3)", borderRadius: 4 }}>
        <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "var(--border)" }} />
        <div style={{ position: "absolute", top: 2, bottom: 2, borderRadius: 3, background: pos ? "var(--done)" : "var(--accent-fg)", left: pos ? "50%" : `calc(50% - ${w}%)`, width: w + "%" }} />
      </div>
      <span className="num" style={{ width: 56, textAlign: "right", color: pos ? "var(--success)" : "var(--danger)" }}>{pos ? "+" : ""}{(delta * 100).toFixed(1)}</span>
    </div>
  );
}

/* ---------- PER-SAMPLE DIFF (real analytics.samples aligned by sample_id) ---------- */
function SampleDiff({ runs }: { runs: Loaded[] }) {
  const [only, setOnly] = useState<"all" | "disagree">("all");
  const aligned = useMemo(() => {
    const map = new Map<string, { cat: string; cells: (number | null)[] }>();
    runs.forEach((r, ri) => {
      (r.res?.samples ?? []).forEach((s) => {
        if (!map.has(s.sample_id)) map.set(s.sample_id, { cat: s.category || "—", cells: runs.map(() => null) });
        map.get(s.sample_id)!.cells[ri] = s.passed;
      });
    });
    return Array.from(map.entries()).map(([id, v]) => ({ id, ...v }));
  }, [runs]);
  const rows = aligned.filter((r) => {
    if (only !== "disagree") return true;
    const seen = r.cells.filter((c) => c != null);
    return seen.length > 1 && new Set(seen).size > 1;
  });

  return (
    <div className="panel flush">
      <div className="panel-h">
        <Icon name="diff" className="ic" /><h2>Per-sample diff</h2><span className="grow" />
        <div className="seg">
          <button className={only === "all" ? "on" : ""} onClick={() => setOnly("all")}>all <span className="subtle">{aligned.length}</span></button>
          <button className={only === "disagree" ? "on" : ""} onClick={() => setOnly("disagree")}>disagreements</button>
        </div>
      </div>
      <div className="panel-b" style={{ overflowX: "auto" }}>
        {rows.length === 0 ? <Empty icon="filter">No samples in common across these runs (transcripts are sampled, so overlap may be sparse).</Empty> : (
          <table className="grid">
            <thead>
              <tr><th>Sample</th><th>Category</th>{runs.map((r) => <th key={r.id} className="right mono" style={{ fontSize: 10.5 }}>{r.id}</th>)}</tr>
            </thead>
            <tbody>
              {rows.slice(0, 200).map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.id}</td>
                  <td className="cellmuted">{r.cat}</td>
                  {r.cells.map((c, i) => (
                    <td key={i} className="right">
                      {c == null ? <span className="cellmark na" style={{ width: 16, height: 16, display: "inline-grid" }}>·</span>
                        : c ? <span className="cellmark pass" style={{ width: 16, height: 16 }}><Icon name="check" size={11} /></span>
                          : <span className="cellmark fail" style={{ width: 16, height: 16 }}><Icon name="x" size={11} /></span>}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default function ComparePage() {
  return (
    <Suspense fallback={<div className="page"><Empty icon="compare"><span className="spin" /> loading…</Empty></div>}>
      <CompareInner />
    </Suspense>
  );
}
