"use client";
import { useEffect, useState } from "react";
import { getMe } from "@/lib/api";
import { Icon } from "@/components/icons";

export { Icon };

// ---- status pill -------------------------------------------------------------------------------
export function StatusPill({ status }: { status: string }) {
  // map a couple of backend statuses to the prototype's pill classes
  const s = status === "budget_exceeded" ? "completed" : status === "training" ? "running" : status;
  return (
    <span className={`pill ${s}`}>
      <span className="led" />
      {status}
    </span>
  );
}

// ---- accuracy bar ------------------------------------------------------------------------------
export function AccBar({ value, ci }: { value: number | null | undefined; ci?: number | null }) {
  if (value == null) return <span className="subtle mono">—</span>;
  const p = Math.round(value * 100);
  const cls = value >= 0.8 ? "" : value >= 0.6 ? "mid" : "low";
  return (
    <span className="acc">
      <span className="track">
        <span className={`fill ${cls}`} style={{ width: p + "%" }} />
      </span>
      <span>
        {p}%{ci != null && <span className="subtle"> ±{(ci * 100).toFixed(1)}</span>}
      </span>
    </span>
  );
}

// ---- sparkline ---------------------------------------------------------------------------------
export function Sparkline({ data, w = 90, h = 26, color = "var(--accent-fg)", fill = true }:
  { data: number[]; w?: number; h?: number; color?: string; fill?: boolean }) {
  if (!data || !data.length) return null;
  const max = Math.max(...data), min = Math.min(...data), rng = max - min || 1;
  const pts = data.map((d, i) => [(i / (data.length - 1 || 1)) * w, h - 2 - ((d - min) / rng) * (h - 4)]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = line + ` L${w} ${h} L0 ${h} Z`;
  return (
    <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      {fill && <path d={area} fill={color} opacity="0.12" />}
      <path d={line} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

// ---- progress bar (done/running/failed) --------------------------------------------------------
export function Progress({ p, total, thin }: { p: Record<string, number>; total: number; thin?: boolean }) {
  const seg = (n: number) => (total ? (n / total) * 100 + "%" : "0%");
  return (
    <div className={`prog ${thin ? "thin" : ""}`}>
      <span className="s-done" style={{ width: seg(p.done || 0) }} />
      <span className="s-run" style={{ width: seg(p.running || 0) }} />
      <span className="s-fail" style={{ width: seg(p.failed || 0) }} />
    </div>
  );
}

// ---- kind chip (harness / scorer / dataset) ----------------------------------------------------
export function Kind({ kind, children }: { kind: string; children: React.ReactNode }) {
  return (
    <span className={`kind ${kind}`}>
      <span className="sq" />
      {children}
    </span>
  );
}

// ---- provider chip (derive from "provider/model" id) -------------------------------------------
const PROV_COLORS: Record<string, { short: string; color: string }> = {
  openai: { short: "AI", color: "#10a37f" },
  anthropic: { short: "AN", color: "#d4a27f" },
  openrouter: { short: "OR", color: "#6566f1" },
  google: { short: "Go", color: "#4285f4" },
  meta: { short: "ME", color: "#0668e1" },
  mistral: { short: "MI", color: "#fa520f" },
  vllm: { short: "vL", color: "#a371f7" },
  mockllm: { short: "MK", color: "#7d8590" },
};

export function Provider({ id, showLabel }: { id: string; showLabel?: boolean }) {
  const inhouse = /atlas|step-|checkpoint:/.test(id);
  if (inhouse) {
    const name = id.startsWith("checkpoint:") ? id.split(":").slice(1).join(":") : id;
    return (
      <span className="prov" title={id}>
        <span className="pi" style={{ background: "linear-gradient(135deg,#a371f7,#2f81f7)", color: "#fff" }}>A</span>
        {showLabel ? id : name}
      </span>
    );
  }
  const provider = id.includes("/") ? id.split("/")[0] : "";
  const pr = PROV_COLORS[provider] || { short: "?", color: "var(--panel-3)" };
  const name = id.includes("/") ? id.split("/").slice(1).join("/") : id;
  return (
    <span className="prov" title={id}>
      <span className="pi" style={{ background: pr.color, color: provider === "mockllm" ? "var(--fg-muted)" : "#fff" }}>{pr.short}</span>
      {showLabel ? id : name}
    </span>
  );
}

// ---- delta indicator ---------------------------------------------------------------------------
export function Delta({ value, suffix = "pp", invert }: { value: number | null | undefined; suffix?: string; invert?: boolean }) {
  if (value == null || Math.abs(value) < 0.0005) return <span className="delta flat">·0</span>;
  const up = value > 0, good = invert ? !up : up;
  return (
    <span className={`delta ${good ? "up" : "down"}`}>
      <Icon name={up ? "arrowup" : "arrowdown"} className="ic" size={11} />
      {up ? "+" : ""}
      {(value * 100).toFixed(1)}
      {suffix}
    </span>
  );
}

// ---- empty state -------------------------------------------------------------------------------
export function Empty({ icon = "list", children }: { icon?: string; children: React.ReactNode }) {
  return (
    <div className="empty">
      <Icon name={icon} className="ic" />
      <div>{children}</div>
    </div>
  );
}

// ---- user chip (OIDC identity) -----------------------------------------------------------------
export function UserChip() {
  const [email, setEmail] = useState<string | null>(null);
  useEffect(() => {
    getMe().then((m) => setEmail(m.email)).catch(() => {});
  }, []);
  const initial = (email?.[0] ?? "?").toUpperCase();
  return (
    <div className="avatar" title={email ?? "unauthenticated"}>{initial}</div>
  );
}
