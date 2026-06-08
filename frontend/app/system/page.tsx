"use client";
// System Diagram — the eval-engine topology (DESIGN.md §6) rendered as a live React Flow graph.
// Nodes are colored by real health and carry live metrics; edges animate when work is flowing. All
// state comes from the same GET /be/ops/status snapshot the Operations tab polls.
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  ReactFlow, ReactFlowProvider, Background, BackgroundVariant, Controls, MiniMap,
  Handle, Position, MarkerType, useNodesState, useEdgesState,
  type Node, type Edge, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Icon } from "@/components/icons";
import { Empty } from "@/components/ui";
import { getOps, ago, fmtN, type OpsStatus, type OpsComponent } from "@/lib/api";

// ── health → color (mirrors app/ops/page.tsx so the two views never disagree) ──────────────────
const HEALTH: Record<string, { c: string; bg: string }> = {
  ok:       { c: "var(--success)",     bg: "var(--success-soft)" },
  degraded: { c: "var(--attention-fg)", bg: "var(--attention-soft)" },
  down:     { c: "var(--danger)",      bg: "var(--danger-soft)" },
  idle:     { c: "var(--queue)",       bg: "var(--queue-soft)" },
  scaling:  { c: "var(--queue)",       bg: "var(--queue-soft)" },
  unknown:  { c: "var(--fg-muted)",    bg: "var(--panel-3)" },
};
const PULSE = new Set(["degraded", "down", "scaling"]);

// ── metric formatting (same conventions as the Ops tab) ─────────────────────────────────────────
const METRIC_LABEL: Record<string, string> = {
  ledger_rows: "ledger", running_runs: "running", queued_runs: "queued", connections: "conns",
  latency_ms: "latency", rows: "rows", replicas: "replicas", repl_lag_s: "repl lag",
  pods: "pods", restarts: "restarts", live: "workers", claims: "claims", tick_age_s: "last tick",
  admitted: "admitted", standbys: "standbys", http: "http", role: "role",
  connected_replicas: "replicas", pod: "pod", pods_ready: "pods up", queued_age_s: "waiting",
};
const fmtDur = (s: number) => (s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`);
function fmtMetric(k: string, v: string | number | null): string {
  if (v == null) return "—";
  if (typeof v === "number") {
    if (k.includes("age_s") || k === "repl_lag_s") return fmtDur(v);
    if (k.includes("ms")) return Math.round(v) + "ms";
    return v >= 1000 ? fmtN(v) : String(v);
  }
  return String(v);
}

// ── static topology (curated layout matching DESIGN.md §6) ──────────────────────────────────────
type NodeDef = { id: string; x: number; y: number; label: string; sub: string; icon: string; comp?: string; external?: boolean };
const NODE_DEFS: NodeDef[] = [
  { id: "dashboard",    x: 360, y: 0,   label: "Dashboard",      sub: "Next.js · this UI",            icon: "grid",     comp: undefined },
  { id: "api",          x: 250, y: 120, label: "Control API",    sub: "FastAPI control plane",        icon: "server",   comp: "api" },
  { id: "postgres",     x: 560, y: 120, label: "Postgres",       sub: "metadata + ephemeral ledger",  icon: "database", comp: "postgres" },
  { id: "orchestrator", x: 30,  y: 250, label: "Orchestrator",   sub: "leader-elected admit/finalize", icon: "spark",   comp: "orchestrator" },
  { id: "workers",      x: 360, y: 260, label: "Workers",        sub: "KEDA-scaled · Inspect tasks",  icon: "cpu",      comp: "workers" },
  { id: "litellm",      x: 70,  y: 410, label: "LiteLLM",        sub: "model gateway",                icon: "bell",     comp: "litellm" },
  { id: "clickhouse",   x: 350, y: 410, label: "ClickHouse",     sub: "analytics · ~12B rows",        icon: "layers",   comp: "clickhouse" },
  { id: "gcs",          x: 600, y: 410, label: "Object store",   sub: "S3/GCS · .eval + transcripts", icon: "box",      comp: "gcs" },
  { id: "redis",        x: 10,  y: 555, label: "Redis",          sub: "global rate-limit state",      icon: "bolt",     comp: "redis" },
  { id: "providers",    x: 195, y: 555, label: "Model providers", sub: "API + self-hosted vLLM",      icon: "cpu",      external: true },
  { id: "inspect_view", x: 600, y: 555, label: "Inspect viewer", sub: "embedded trace viewer",        icon: "doc",      comp: "inspect_view" },
];
const DEF_BY_ID: Record<string, NodeDef> = Object.fromEntries(NODE_DEFS.map((d) => [d.id, d]));

// edges: s/t = node ids, sh/th = handle sides; flow = animate when work is in flight; orch = animate
// while the orchestrator is live; dashed = secondary/lower-traffic path.
type EdgeDef = { id: string; s: string; t: string; sh: string; th: string; label: string; flow?: boolean; orch?: boolean; dashed?: boolean };
const EDGE_DEFS: EdgeDef[] = [
  { id: "dash-api",   s: "dashboard",    t: "api",          sh: "b", th: "t", label: "REST · OIDC", flow: true },
  { id: "api-pg",     s: "api",          t: "postgres",     sh: "r", th: "l", label: "write run" },
  { id: "orch-pg",    s: "orchestrator", t: "postgres",     sh: "r", th: "l", label: "ledger", orch: true },
  { id: "wk-pg",      s: "workers",      t: "postgres",     sh: "t", th: "b", label: "claim", flow: true },
  { id: "wk-ll",      s: "workers",      t: "litellm",      sh: "l", th: "r", label: "model calls", flow: true },
  { id: "wk-ch",      s: "workers",      t: "clickhouse",   sh: "b", th: "t", label: "results", flow: true },
  { id: "wk-gcs",     s: "workers",      t: "gcs",          sh: "r", th: "t", label: "transcripts", flow: true },
  { id: "ll-redis",   s: "litellm",      t: "redis",        sh: "b", th: "t", label: "rate-limit" },
  { id: "ll-prov",    s: "litellm",      t: "providers",    sh: "b", th: "t", label: "", flow: true },
  { id: "gcs-iv",     s: "gcs",          t: "inspect_view", sh: "b", th: "t", label: "serves logs" },
  { id: "api-ch",     s: "api",          t: "clickhouse",   sh: "b", th: "l", label: "analytics", dashed: true },
  { id: "dash-iv",    s: "dashboard",    t: "inspect_view", sh: "r", th: "r", label: "embeds", dashed: true },
];

// ── derive a node's live status + the metrics to surface, from the ops snapshot ─────────────────
type NData = { def: NodeDef; status: string; detail: string; metrics: [string, string][] };

function compStatus(id: string, ops: OpsStatus | undefined): string {
  if (!ops) return "unknown";
  if (id === "dashboard") return "ok";
  if (id === "providers") return ops.components.find((c) => c.name === "litellm")?.status === "ok" ? "ok" : "degraded";
  return ops.components.find((c) => c.name === DEF_BY_ID[id].comp)?.status ?? "unknown";
}

function nodeData(def: NodeDef, ops: OpsStatus | undefined): NData {
  const status = compStatus(def.id, ops);
  if (!ops) return { def, status, detail: def.sub, metrics: [] };
  if (def.id === "dashboard")
    return { def, status, detail: "Next.js dashboard (this UI)",
             metrics: [["active runs", String(ops.queues.runs.running || 0)]] };
  if (def.id === "providers")
    return { def, status, detail: "external API providers + self-hosted vLLM (gateway-fronted)", metrics: [] };
  const comp = ops.components.find((c) => c.name === def.comp);
  if (!comp) return { def, status, detail: def.sub, metrics: [] };
  const raw = Object.entries(comp.metrics).filter(([k]) => k !== "pod" && k !== "restarts");
  let metrics: [string, string][] = raw.map(([k, v]) => [METRIC_LABEL[k] || k, fmtMetric(k, v)]);
  // workers: lead with live throughput from the queue rollup
  if (def.id === "workers" && ops.queues.samples_per_s != null)
    metrics = [["thru", ops.queues.samples_per_s.toFixed(1) + "/s"], ...metrics];
  return { def, status, detail: comp.detail || def.sub, metrics: metrics.slice(0, 3) };
}

// ── custom node ─────────────────────────────────────────────────────────────────────────────────
const SIDES: [string, Position][] = [["t", Position.Top], ["r", Position.Right], ["b", Position.Bottom], ["l", Position.Left]];

function CompNode({ data, selected }: NodeProps<Node<NData>>) {
  const { def, status, metrics } = data;
  const h = HEALTH[status] || HEALTH.unknown;
  return (
    <div className={`synode${selected ? " sel" : ""}${def.external ? " ext" : ""}`} title={data.detail}>
      {SIDES.map(([id, pos]) => (
        <span key={id}>
          <Handle id={`t-${id}`} type="target" position={pos} className="rf-h" isConnectable={false} />
          <Handle id={`s-${id}`} type="source" position={pos} className="rf-h" isConnectable={false} />
        </span>
      ))}
      <div className="syhead">
        <Icon name={def.icon} />
        <span className="syname">{def.label}</span>
        <span className="syled" style={{ background: h.c, animation: PULSE.has(status) ? "blink 1.1s ease-in-out infinite" : undefined }} />
      </div>
      <div className="sysub">{def.sub}</div>
      {metrics.length > 0 && (
        <div className="symetrics">
          {metrics.map(([k, v]) => (
            <span key={k} className="symet">{k} <b>{v}</b></span>
          ))}
        </div>
      )}
    </div>
  );
}
const nodeTypes = { comp: CompNode };

// ── edges ───────────────────────────────────────────────────────────────────────────────────────
function buildEdges(ops: OpsStatus | undefined): Edge[] {
  const live = Number(ops?.components.find((c) => c.name === "workers")?.metrics.live ?? 0);
  const active = !!ops && ((ops.queues.samples_per_s ?? 0) > 0 || (ops.queues.ledger.running ?? 0) > 0 || live > 0);
  return EDGE_DEFS.map((e) => {
    const a = compStatus(e.s, ops), b = compStatus(e.t, ops);
    const bad = a === "down" || b === "down" ? "var(--danger)" : a === "degraded" || b === "degraded" ? "var(--attention)" : null;
    const flowing = !bad && ((e.flow && active) || (e.orch && a === "ok"));
    const stroke = bad || (flowing ? "var(--accent)" : "var(--border)");
    let label = e.label;
    if (e.id === "wk-ch" && ops?.queues.samples_per_s) label = ops.queues.samples_per_s.toFixed(1) + "/s";
    if (e.id === "wk-pg") { const r = ops?.queues.ledger.running || 0; label = r ? `claim · ${r}` : "claim"; }
    return {
      id: e.id, source: e.s, target: e.t, sourceHandle: `s-${e.sh}`, targetHandle: `t-${e.th}`,
      type: "smoothstep", animated: flowing, label,
      style: { stroke, strokeWidth: flowing ? 2 : 1.5, strokeDasharray: e.dashed ? "5 4" : undefined, opacity: e.dashed ? 0.55 : 1 },
      labelStyle: { fill: "var(--fg-muted)", fontSize: 10, fontFamily: "var(--mono)" },
      labelBgStyle: { fill: "var(--canvas-inset)" }, labelBgPadding: [4, 2] as [number, number], labelBgBorderRadius: 3,
      markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 13, height: 13 },
    };
  });
}

const initialNodes: Node<NData>[] = NODE_DEFS.map((d) => ({
  id: d.id, type: "comp", position: { x: d.x, y: d.y }, data: nodeData(d, undefined),
}));

// jump-to-tab + logs links shown in the detail panel
const JUMP: Record<string, { href: string; label: string; ext?: boolean }> = {
  dashboard: { href: "/", label: "Open Dashboard" },
  api: { href: "/ops", label: "Operations" },
  orchestrator: { href: "/ops", label: "Operations" },
  workers: { href: "/ops", label: "Operations" },
  postgres: { href: "/ops", label: "Operations" },
  clickhouse: { href: "/compare", label: "Compare analytics" },
  gcs: { href: "/ops", label: "Operations" },
  redis: { href: "/ops", label: "Operations" },
  litellm: { href: "/ops", label: "Operations" },
  inspect_view: { href: "/inspect/", label: "Open viewer", ext: true },
};

function Flow({ ops }: { ops: OpsStatus | undefined }) {
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState([] as Edge[]);
  const [sel, setSel] = useState<string | null>(null);

  // live refresh: update node data (positions preserved across drags) + rebuild edges each poll.
  useEffect(() => {
    setNodes((nds) => nds.map((n) => ({ ...n, data: nodeData(DEF_BY_ID[n.id], ops) })));
    setEdges(buildEdges(ops));
  }, [ops, setNodes, setEdges]);

  const selComp: OpsComponent | undefined = sel ? ops?.components.find((c) => c.name === DEF_BY_ID[sel]?.comp) : undefined;
  const selDef = sel ? DEF_BY_ID[sel] : undefined;
  const selStatus = sel ? compStatus(sel, ops) : "unknown";
  const jump = sel ? JUMP[sel] : undefined;

  return (
    <div className="sysflow" style={{ position: "absolute", inset: 0 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        onNodeClick={(_, n) => setSel(n.id)}
        onPaneClick={() => setSel(null)}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        minZoom={0.4}
        maxZoom={1.6}
        proOptions={{ hideAttribution: false }}
        nodesConnectable={false}
        edgesFocusable={false}
        defaultEdgeOptions={{ type: "smoothstep" }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
        <Controls showInteractive={false} />
        <MiniMap
          pannable zoomable
          style={{ width: 150, height: 100 }}
          maskColor="rgba(1,4,9,0.6)"
          nodeColor={(n) => (HEALTH[compStatus(n.id, ops)] || HEALTH.unknown).c}
          nodeStrokeWidth={0}
        />
      </ReactFlow>

      {selDef && (
        <div className="syside">
          <div className="panel-h" style={{ borderRadius: "10px 10px 0 0" }}>
            <Icon name={selDef.icon} className="ic" />
            <h2>{selDef.label}</h2>
            <span className="grow" />
            <button className="btn sm ghost" onClick={() => setSel(null)}><Icon name="x" size={13} /></button>
          </div>
          <div className="panel-b" style={{ display: "grid", gap: 12 }}>
            <div className="vcenter gap8">
              <span className="pill" style={{ color: (HEALTH[selStatus] || HEALTH.unknown).c, background: (HEALTH[selStatus] || HEALTH.unknown).bg, borderColor: (HEALTH[selStatus] || HEALTH.unknown).c }}>
                <span className="led" style={{ background: (HEALTH[selStatus] || HEALTH.unknown).c, animation: PULSE.has(selStatus) ? "blink 1.1s ease-in-out infinite" : undefined }} />
                {selStatus}
              </span>
              {selDef.external && <span className="badge">external</span>}
            </div>
            <div className="subtle" style={{ fontSize: 12 }}>
              {selComp?.detail || (sel === "dashboard" ? "Next.js dashboard (this UI)" : sel === "providers" ? "External API providers + self-hosted vLLM, all gateway-fronted via LiteLLM." : selDef.sub)}
            </div>
            {selComp && Object.entries(selComp.metrics).filter(([k]) => k !== "pod").length > 0 && (
              <div className="kv" style={{ gridTemplateColumns: "120px 1fr", rowGap: 6 }}>
                {Object.entries(selComp.metrics).filter(([k]) => k !== "pod").map(([k, v]) => (
                  <span key={k} style={{ display: "contents" }}>
                    <span className="k subtle" style={{ fontSize: 11.5 }}>{METRIC_LABEL[k] || k}</span>
                    <span className="v mono" style={{ fontSize: 12 }}>{fmtMetric(k, v)}</span>
                  </span>
                ))}
              </div>
            )}
            {selComp?.last_seen && <div className="subtle mono" style={{ fontSize: 11 }}>last seen {selComp.last_seen}</div>}
            <div className="vcenter gap8 wrap">
              {jump && (jump.ext
                ? <a className="btn sm" href={jump.href} target="_blank" rel="noreferrer"><Icon name="external" size={12} />{jump.label}</a>
                : <Link className="btn sm" href={jump.href}><Icon name="arrowright" size={12} />{jump.label}</Link>)}
              {selComp?.logs_url && <a className="btn sm ghost" href={selComp.logs_url} target="_blank" rel="noreferrer"><Icon name="external" size={12} />Logs</a>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SystemDiagram() {
  const { data: ops, isError } = useQuery({ queryKey: ["ops"], queryFn: getOps, refetchInterval: 4000 });

  const tiles = useMemo(() => {
    if (!ops) return null;
    const issues = ops.components.filter((c) => c.status === "down" || c.status === "degraded").length;
    const live = ops.components.find((c) => c.name === "workers")?.metrics.live ?? 0;
    return {
      issues, live,
      thru: ops.queues.samples_per_s == null ? "—" : ops.queues.samples_per_s.toFixed(1) + "/s",
      inflight: ops.queues.ledger.running || 0,
      queued: ops.queues.ledger.queued || 0,
      runs: ops.queues.runs.running || 0,
    };
  }, [ops]);

  return (
    <div style={{ height: "calc(100vh - 52px)", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "16px 20px 14px", borderBottom: "1px solid var(--border)" }}>
        <div className="between" style={{ marginBottom: 12 }}>
          <div>
            <div className="eyebrow">architecture · live</div>
            <h1 className="title" style={{ marginTop: 4 }}>System Diagram</h1>
          </div>
          <div className="vcenter gap16">
            {tiles && (
              <div className="vcenter gap16 mono" style={{ fontSize: 12 }}>
                <span className="subtle">health <b style={{ color: tiles.issues ? "var(--danger)" : "var(--success)" }}>{tiles.issues ? `${tiles.issues} issue${tiles.issues > 1 ? "s" : ""}` : "all green"}</b></span>
                <span className="subtle">throughput <b style={{ color: "var(--fg)" }}>{tiles.thru}</b></span>
                <span className="subtle">in flight <b style={{ color: "var(--fg)" }}>{fmtN(tiles.inflight)}</b> · {fmtN(tiles.queued)} queued</span>
                <span className="subtle">workers <b style={{ color: "var(--fg)" }}>{String(tiles.live)}</b></span>
                <span className="subtle">runs <b style={{ color: "var(--fg)" }}>{tiles.runs}</b></span>
              </div>
            )}
            <span className="subtle mono" style={{ fontSize: 11 }}>updated {ago(ops?.generated_at)}</span>
          </div>
        </div>
        <div className="sylegend">
          {(["ok", "degraded", "down", "scaling", "idle", "unknown"] as const).map((s) => (
            <span key={s}><i style={{ background: HEALTH[s].c }} />{s}</span>
          ))}
          <span className="grow" />
          <span style={{ color: "var(--fg-subtle)" }}>drag nodes · scroll to zoom · click a node for detail</span>
        </div>
      </div>
      <div style={{ flex: 1, position: "relative" }}>
        {isError ? (
          <Empty icon="warn">Couldn’t reach the control plane (<span className="mono">/be/ops/status</span>).</Empty>
        ) : (
          <ReactFlowProvider>
            <Flow ops={ops} />
          </ReactFlowProvider>
        )}
      </div>
    </div>
  );
}
