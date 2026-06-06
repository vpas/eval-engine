"use client";
import { useEffect, useRef, useState } from "react";
import { fmtStep, fmtTok } from "@/lib/api";

export type Track = { id: string; color: string; role?: string };
export type Ckpt = {
  step: number;
  idx: number;
  tokens: number;
  loss?: number;
  evals: Record<string, number | undefined>;
  expected: Record<string, number | undefined>;
};

function useMeasure(): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(940);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((ents) => { for (const e of ents) setW(e.contentRect.width); });
    ro.observe(ref.current);
    setW(ref.current.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

// Multi-eval training chart (ported from training-chart.jsx): hover scrubber, loss overlay, expected
// band corridor (expected ± threshold), anomaly markers, "now" marker + future shading.
export function TrainingChart({
  checkpoints, track, visible, xMax, showLoss, showBand, xMode, thr, anomalySteps, onPick,
}: {
  checkpoints: Ckpt[];
  track: Track[];
  visible: Set<string>;
  xMax: number;
  showLoss: boolean;
  showBand: boolean;
  xMode: "steps" | "tokens";
  thr: number;
  anomalySteps: Record<string, Set<number>>;
  onPick: (idx: number) => void;
}) {
  const [ref, W] = useMeasure();
  const [hover, setHover] = useState<number | null>(null);
  const H = 340;
  const pad = { l: 44, r: showLoss ? 48 : 18, t: 16, b: 38 };
  const pw = Math.max(200, W - pad.l - pad.r);
  const ph = H - pad.t - pad.b;
  const cps = checkpoints;
  if (!cps.length) return <div ref={ref} style={{ width: "100%", height: H }} />;

  const vis = track.filter((e) => visible.has(e.id));
  const accs = cps.flatMap((c) => vis.map((e) => c.evals[e.id]).filter((v): v is number => v != null));
  const yLo = Math.max(0, Math.min(0.6, Math.floor((Math.min(1, ...accs) - 0.05) * 10) / 10));
  const yHi = 1.0;
  const losses = cps.map((c) => c.loss).filter((v): v is number => v != null);
  const lossLo = losses.length ? Math.min(...losses) - 0.1 : 0;
  const lossHi = losses.length ? Math.max(...losses) + 0.1 : 1;

  const x = (step: number) => pad.l + (step / (xMax || 1)) * pw;
  const y = (acc: number) => pad.t + (1 - (acc - yLo) / (yHi - yLo)) * ph;
  const yLoss = (l: number) => pad.t + ((l - lossLo) / (lossHi - lossLo || 1)) * ph;

  const nowX = x(cps[cps.length - 1].step);
  const hc = hover != null ? cps[hover] : null;

  const gy: number[] = [];
  for (let v = Math.ceil(yLo * 10) / 10; v <= yHi + 1e-9; v += 0.1) gy.push(Math.round(v * 100) / 100);
  const gx: number[] = [];
  for (let s = 0; s <= xMax; s += Math.max(1, Math.round(xMax / 6))) gx.push(s);

  return (
    <div ref={ref} style={{ position: "relative", width: "100%" }}>
      <svg width={W} height={H} style={{ display: "block" }} onMouseLeave={() => setHover(null)}>
        <rect x={nowX} y={pad.t} width={pad.l + pw - nowX} height={ph} fill="rgba(110,118,129,0.05)" />
        {gy.map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={pad.l + pw} y1={y(v)} y2={y(v)} stroke="var(--border-muted)" strokeWidth="1" />
            <text x={pad.l - 8} y={y(v) + 3} textAnchor="end" fontSize="10" fill="var(--fg-subtle)" fontFamily="var(--mono)">{Math.round(v * 100)}</text>
          </g>
        ))}
        {gx.map((v) => (
          <text key={v} x={x(v)} y={H - 14} textAnchor="middle" fontSize="10" fill="var(--fg-subtle)" fontFamily="var(--mono)">
            {xMode === "tokens" ? fmtTok(v * 2.1e6) : fmtStep(v)}
          </text>
        ))}
        <text x={pad.l} y={H - 2} fontSize="9.5" fill="var(--fg-subtle)" fontFamily="var(--mono)">{xMode === "tokens" ? "tokens" : "training step"}</text>

        {showBand && vis.map((e) => {
          const top = cps.map((c, i) => (c.expected[e.id] == null ? "" : (i ? "L" : "M") + x(c.step) + " " + y(Math.min(yHi, (c.expected[e.id] as number) + thr)))).join(" ");
          const bot = cps.slice().reverse().map((c) => (c.expected[e.id] == null ? "" : "L" + x(c.step) + " " + y(Math.max(yLo, (c.expected[e.id] as number) - thr)))).join(" ");
          const expLine = cps.map((c, i) => (c.expected[e.id] == null ? "" : (i ? "L" : "M") + x(c.step) + " " + y(c.expected[e.id] as number))).join(" ");
          if (!expLine.trim()) return null;
          return (
            <g key={"band-" + e.id}>
              <path d={top + " " + bot + " Z"} fill={e.color} opacity="0.07" />
              <path d={expLine} fill="none" stroke={e.color} strokeWidth="1" strokeDasharray="2 3" opacity="0.5" />
            </g>
          );
        })}

        <line x1={nowX} x2={nowX} y1={pad.t} y2={pad.t + ph} stroke="var(--border)" strokeWidth="1" strokeDasharray="3 3" />
        <text x={nowX + 4} y={pad.t + 10} fontSize="9" fill="var(--fg-muted)" fontFamily="var(--mono)">now · {fmtStep(cps[cps.length - 1].step)}</text>

        {showLoss && losses.length > 0 && (
          <>
            <path d={cps.map((c, i) => (c.loss == null ? "" : (i ? "L" : "M") + x(c.step) + " " + yLoss(c.loss))).join(" ")} fill="none" stroke="var(--fg-subtle)" strokeWidth="1.4" strokeDasharray="4 3" opacity="0.8" />
            <text x={pad.l + pw + 8} y={pad.t + 9} fontSize="9" fill="var(--fg-subtle)" fontFamily="var(--mono)">loss</text>
          </>
        )}

        {vis.map((e) => {
          const line = cps.map((c, i) => (c.evals[e.id] == null ? "" : (i ? "L" : "M") + x(c.step) + " " + y(c.evals[e.id] as number))).join(" ");
          return (
            <g key={e.id}>
              <path d={line} fill="none" stroke={e.color} strokeWidth="2" strokeLinejoin="round" />
              {cps.map((c) => {
                const v = c.evals[e.id];
                if (v == null) return null;
                const anom = anomalySteps[e.id]?.has(c.step);
                const exp = c.expected[e.id];
                const below = exp != null && v < exp - thr;
                return (
                  <g key={c.step}>
                    {anom && <circle cx={x(c.step)} cy={y(v)} r="6.5" fill="none" stroke="var(--danger)" strokeWidth="1.6" />}
                    <circle cx={x(c.step)} cy={y(v)} r={anom ? 3.5 : 2.6} fill={anom || below ? "var(--danger)" : e.color} />
                  </g>
                );
              })}
            </g>
          );
        })}

        {hc && <line x1={x(hc.step)} x2={x(hc.step)} y1={pad.t} y2={pad.t + ph} stroke="var(--accent-muted)" strokeWidth="1" />}
        {cps.map((c, i) => {
          const left = i === 0 ? pad.l : (x(cps[i - 1].step) + x(c.step)) / 2;
          const right = i === cps.length - 1 ? x(c.step) + 16 : (x(c.step) + x(cps[i + 1].step)) / 2;
          return <rect key={c.step} x={left} y={pad.t} width={Math.max(2, right - left)} height={ph} fill="transparent"
            style={{ cursor: "pointer" }} onMouseEnter={() => setHover(i)} onClick={() => onPick(i)} />;
        })}
      </svg>

      {hc && (
        <div style={{ position: "absolute", top: 8, left: Math.min(W - 196, Math.max(8, x(hc.step) + 12)), width: 184, background: "var(--overlay)", border: "1px solid var(--border)", borderRadius: 8, padding: "9px 11px", pointerEvents: "none", boxShadow: "0 8px 24px rgba(0,0,0,.5)", zIndex: 5 }}>
          <div className="between" style={{ marginBottom: 6 }}>
            <span className="mono" style={{ fontSize: 11.5, fontWeight: 600 }}>step {fmtStep(hc.step)}</span>
            <span className="subtle mono" style={{ fontSize: 10 }}>{fmtTok(hc.tokens)}</span>
          </div>
          {vis.map((e) => (
            <div key={e.id} className="between" style={{ fontSize: 11, marginBottom: 2 }}>
              <span className="vcenter gap6"><i style={{ width: 7, height: 7, borderRadius: 2, background: e.color, display: "inline-block" }} /><span className="mono subtle" style={{ fontSize: 10 }}>{e.id.replace(/_.*/, "")}</span></span>
              <span className="num">{hc.evals[e.id] == null ? "—" : Math.round((hc.evals[e.id] as number) * 100) + "%"}</span>
            </div>
          ))}
          {showLoss && hc.loss != null && <div className="between" style={{ fontSize: 11, marginTop: 4, borderTop: "1px solid var(--border)", paddingTop: 4 }}><span className="subtle mono" style={{ fontSize: 10 }}>loss</span><span className="num">{hc.loss.toFixed(2)}</span></div>}
          <div className="subtle" style={{ fontSize: 9.5, marginTop: 5 }}>click to inspect →</div>
        </div>
      )}
    </div>
  );
}
