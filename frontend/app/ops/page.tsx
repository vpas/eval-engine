"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { Empty } from "@/components/ui";
import { getOps, ago, fmtN, fmtCost, type OpsStatus, type OpsComponent } from "@/lib/api";

// ops health → color. Distinct from run StatusPill (which maps run lifecycle states).
const HEALTH: Record<string, { c: string; bg: string; b: string }> = {
  ok: { c: "var(--success)", bg: "var(--success-soft)", b: "rgba(63,185,80,.3)" },
  degraded: { c: "var(--attention-fg)", bg: "var(--attention-soft)", b: "rgba(210,153,34,.35)" },
  down: { c: "var(--danger)", bg: "var(--danger-soft)", b: "rgba(248,81,73,.3)" },
  idle: { c: "var(--queue)", bg: "var(--queue-soft)", b: "rgba(88,166,255,.3)" },
  unknown: { c: "var(--fg-muted)", bg: "var(--panel-3)", b: "var(--border)" },
};

function HealthPill({ status }: { status: string }) {
  const h = HEALTH[status] || HEALTH.unknown;
  return (
    <span className="pill" style={{ color: h.c, background: h.bg, borderColor: h.b }}>
      <span className="led" style={status === "degraded" || status === "down" ? { animation: "blink 1.1s ease-in-out infinite" } : undefined} />
      {status}
    </span>
  );
}

const COMP_ICON: Record<string, string> = {
  api: "server", orchestrator: "spark", workers: "cpu", postgres: "database",
  clickhouse: "layers", redis: "bolt", litellm: "bell", gcs: "box", inspect_view: "doc",
};

function fmtMetric(k: string, v: string | number | null): string {
  if (v == null) return "—";
  if (typeof v === "number") {
    if (k.includes("ms")) return Math.round(v) + "ms";
    if (k === "samples_per_s") return v.toFixed(1) + "/s";
    return v >= 1000 ? fmtN(v) : String(v);
  }
  return String(v);
}

const METRIC_LABEL: Record<string, string> = {
  ledger_rows: "ledger", running_runs: "running", queued_runs: "queued", connections: "conns",
  latency_ms: "latency", rows: "rows", replicas: "replicas", repl_lag_s: "repl lag",
  pods: "pods", restarts: "restarts", live: "live", claims: "claims", tick_age_s: "last tick",
  admitted: "admitted", standbys: "standbys", http: "http", role: "role",
  connected_replicas: "replicas", pod: "pod",
};

export default function OpsDashboard() {
  const router = useRouter();
  const [s, setS] = useState<OpsStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState(false);

  useEffect(() => {
    const load = () => getOps().then((d) => { setS(d); setLoaded(true); setErr(false); }).catch(() => { setLoaded(true); setErr(true); });
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);

  if (!loaded) return <div className="page wide"><Empty icon="activity"><span className="spin" /> probing systems…</Empty></div>;
  if (err || !s) return <div className="page wide"><Empty icon="warn">Couldn’t reach the control plane (<span className="mono">/be/ops/status</span>).</Empty></div>;

  const q = s.queues;
  const inflight = (q.ledger.running || 0);
  const queued = (q.ledger.queued || 0);
  const issues = s.components.filter((c) => c.status === "down" || c.status === "degraded").length;
  const nsLogs = s.cluster.project
    ? `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(`resource.type="k8s_container"\nresource.labels.namespace_name="${s.cluster.namespace}"`)};duration=PT1H?project=${s.cluster.project}`
    : null;

  return (
    <div className="page wide">
      <div className="between" style={{ marginBottom: 18 }}>
        <div>
          <div className="eyebrow">infrastructure · live</div>
          <h1 className="title" style={{ marginTop: 4 }}>Operations</h1>
        </div>
        <div className="vcenter gap10">
          <HealthPill status={s.overall} />
          <span className="subtle mono" style={{ fontSize: 11 }}>
            {s.cluster.cluster} · {s.cluster.zone || "—"} · ns {s.cluster.namespace}
          </span>
          <span className="subtle mono" style={{ fontSize: 11 }}>· updated {ago(s.generated_at)}</span>
          {nsLogs && <a className="btn sm ghost" href={nsLogs} target="_blank" rel="noreferrer"><Icon name="external" size={13} />Log Explorer</a>}
        </div>
      </div>

      <div className="stat-row" style={{ marginBottom: 18 }}>
        <StatTile k="System health" icon="shield" v={issues ? `${issues} issue${issues > 1 ? "s" : ""}` : "all green"}
          tone={issues ? "down" : "ok"} d={`${s.components.length} components monitored`} />
        <StatTile k="Throughput" icon="pulse" v={q.samples_per_s == null ? "—" : q.samples_per_s.toFixed(1) + "/s"}
          d={`${s.components.find((c) => c.name === "workers")?.metrics.live ?? 0} live workers`} />
        <StatTile k="In flight" icon="bolt" v={fmtN(inflight)} d={`${fmtN(queued)} samples queued`} />
        <StatTile k="Active runs" icon="list" v={String((q.runs.running || 0))}
          d={`${q.runs.queued || 0} queued · cap ${q.admission.global_max}`} />
      </div>

      {/* component grid */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(290px, 1fr))", gap: 14, marginBottom: 18 }}>
        {s.components.map((c) => <CompCard key={c.name} c={c} />)}
      </div>

      {/* admission / queue depth */}
      <div className="panel" style={{ marginBottom: 18 }}>
        <div className="panel-h"><Icon name="slice" className="ic" /><h2>Queue &amp; admission</h2>
          <span className="grow" /><span className="sub mono">two-lane · per-run cap</span></div>
        <div className="panel-b">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 22 }}>
            <div>
              <div className="eyebrow" style={{ marginBottom: 8 }}>ledger (all runs)</div>
              <LedgerBars led={q.ledger} />
            </div>
            <div>
              <div className="eyebrow" style={{ marginBottom: 8 }}>admission lanes</div>
              <div className="kv" style={{ gridTemplateColumns: "150px 1fr" }}>
                <span className="k">interactive running</span><span className="v">{q.lanes.interactive || 0}</span>
                <span className="k">batch running</span><span className="v">{q.lanes.batch || 0}</span>
                <span className="k">global cap</span><span className="v">{q.admission.running} / {q.admission.global_max}</span>
                <span className="k">interactive reserve</span><span className="v">{q.admission.interactive_reserve}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* active runs */}
      <div className="panel flush" style={{ marginBottom: 18 }}>
        <div className="panel-h"><Icon name="pulse" className="ic" /><h2>Active runs</h2>
          <span className="grow" /><span className="sub mono">{s.active_runs.length}</span></div>
        {s.active_runs.length === 0 ? <div className="panel-b"><Empty icon="pulse">No runs in flight.</Empty></div> : (
          <table className="grid">
            <thead><tr><th>Run</th><th>Model</th><th>Lane</th><th>Status</th>
              <th className="right">Running</th><th className="right">Queued</th><th className="right">Done</th>
              <th className="right">Cost</th><th className="right">Age</th><th></th></tr></thead>
            <tbody>
              {s.active_runs.map((r) => (
                <tr key={r.id} className="click" onClick={() => router.push(`/runs/${r.id}`)}>
                  <td><span className="linklike mono">{r.id}</span></td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{r.model}</td>
                  <td><span className="badge">{r.lane}</span></td>
                  <td className="mono cellmuted" style={{ fontSize: 11.5 }}>{r.status}</td>
                  <td className="right num">{r.running}</td>
                  <td className="right num cellmuted">{r.queued}</td>
                  <td className="right num">{r.done}/{r.total}</td>
                  <td className="right num cellmuted">{fmtCost(r.cost_usd)}</td>
                  <td className="right cellmuted mono" style={{ fontSize: 11.5 }}>{ago(r.created_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {r.logs_url && <a className="btn sm ghost" href={r.logs_url} target="_blank" rel="noreferrer"><Icon name="external" size={12} />logs</a>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* k8s workloads */}
      {s.workloads.length > 0 && (
        <div className="panel flush" style={{ marginBottom: 18 }}>
          <div className="panel-h"><Icon name="box" className="ic" /><h2>Kubernetes workloads</h2>
            <span className="grow" /><span className="sub mono">live pod truth</span></div>
          <table className="grid">
            <thead><tr><th>Workload</th><th>Namespace</th><th className="right">Ready</th><th>Pods</th></tr></thead>
            <tbody>
              {s.workloads.map((w) => (
                <tr key={w.namespace + "/" + w.app}>
                  <td className="mono">{w.app}</td>
                  <td className="cellmuted mono" style={{ fontSize: 11.5 }}>{w.namespace}</td>
                  <td className="right num" style={{ color: w.desired && w.ready < w.desired ? "var(--danger)" : undefined }}>{w.ready}/{w.desired}</td>
                  <td>
                    <div className="vcenter gap8 wrap">
                      {w.pods.length === 0 && <span className="subtle">—</span>}
                      {w.pods.map((p) => (
                        <a key={p.name} className="chip" href={p.logs_url || undefined} target="_blank" rel="noreferrer"
                          title={`${p.phase}${p.node ? " · " + p.node : ""}`}
                          style={{ borderColor: p.ready ? "rgba(63,185,80,.3)" : "var(--border)" }}>
                          <span className="led" style={{ width: 6, height: 6, borderRadius: "50%", background: p.ready ? "var(--success)" : "var(--fg-muted)" }} />
                          {p.name.split("-").slice(-2).join("-")}
                          {p.restarts > 0 && <span style={{ color: "var(--attention-fg)" }}>↻{p.restarts}</span>}
                        </a>
                      ))}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* failures + audit */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div className="panel flush">
          <div className="panel-h"><Icon name="warn" className="ic" /><h2>Recent failures</h2>
            <span className="grow" /><span className="sub mono">{s.failures.length}</span></div>
          {s.failures.length === 0 ? <div className="panel-b"><Empty icon="check">No recorded failures.</Empty></div> : (
            <table className="grid">
              <thead><tr><th>Run</th><th>Sample</th><th>Error</th><th className="right">Try</th><th></th></tr></thead>
              <tbody>
                {s.failures.map((f, i) => (
                  <tr key={i} className="click" onClick={() => router.push(`/runs/${f.run_id}`)}>
                    <td className="mono linklike">{f.run_id}</td>
                    <td className="mono cellmuted" style={{ fontSize: 11.5 }}>{f.sample_id}</td>
                    <td><span className="badge" style={{ color: "var(--danger)", borderColor: "rgba(248,81,73,.3)" }}>{f.error_type || "error"}</span></td>
                    <td className="right num cellmuted">{f.attempts}</td>
                    <td onClick={(e) => e.stopPropagation()}>{f.logs_url && <a className="btn sm ghost" href={f.logs_url} target="_blank" rel="noreferrer"><Icon name="external" size={12} /></a>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="panel flush">
          <div className="panel-h"><Icon name="clock" className="ic" /><h2>Recent activity</h2>
            <span className="grow" /><span className="sub mono">audit</span></div>
          {s.audit.length === 0 ? <div className="panel-b"><Empty icon="clock">No recent activity.</Empty></div> : (
            <table className="grid">
              <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th></tr></thead>
              <tbody>
                {s.audit.map((a, i) => (
                  <tr key={i}>
                    <td className="cellmuted mono" style={{ fontSize: 11.5 }}>{ago(a.ts)}</td>
                    <td className="cellmuted mono" style={{ fontSize: 11.5 }} title={a.actor || ""}>{(a.actor || "—").split("@")[0]}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{a.action}</td>
                    <td className="mono cellmuted" style={{ fontSize: 11.5 }}>{a.target}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

function CompCard({ c }: { c: OpsComponent }) {
  const entries = Object.entries(c.metrics).filter(([k]) => k !== "pod");
  return (
    <div className="panel">
      <div className="panel-b" style={{ padding: 14 }}>
        <div className="between" style={{ marginBottom: 8 }}>
          <div className="vcenter gap8">
            <Icon name={COMP_ICON[c.name] || "dot"} className="ic" />
            <span className="mono" style={{ fontWeight: 600, fontSize: 13 }}>{c.name}</span>
          </div>
          <HealthPill status={c.status} />
        </div>
        <div className="subtle" style={{ fontSize: 11.5, minHeight: 16, marginBottom: 10 }}>{c.detail}</div>
        <div className="vcenter gap8 wrap" style={{ marginBottom: entries.length ? 10 : 0 }}>
          {entries.map(([k, v]) => (
            <span key={k} className="badge" title={k}>
              {METRIC_LABEL[k] || k} <b style={{ color: "var(--fg)" }}>{fmtMetric(k, v)}</b>
            </span>
          ))}
        </div>
        {c.logs_url && (
          <a className="btn sm ghost" href={c.logs_url} target="_blank" rel="noreferrer" style={{ width: "100%", justifyContent: "center" }}>
            <Icon name="external" size={12} />Logs
          </a>
        )}
      </div>
    </div>
  );
}

function LedgerBars({ led }: { led: Record<string, number> }) {
  const order: [string, string][] = [["done", "var(--success)"], ["running", "var(--attention)"], ["queued", "var(--queue)"], ["failed", "var(--danger)"], ["budget_skipped", "var(--done)"]];
  const total = Object.values(led).reduce((a, b) => a + b, 0) || 1;
  return (
    <div>
      <div className="prog" style={{ height: 8, marginBottom: 10 }}>
        {order.map(([k, col]) => (led[k] ? <span key={k} style={{ width: (led[k] / total) * 100 + "%", background: col }} /> : null))}
      </div>
      <div className="vcenter gap16 wrap" style={{ fontSize: 11.5 }}>
        {order.map(([k, col]) => (
          <span key={k} className="subtle vcenter gap6">
            <span style={{ width: 8, height: 8, borderRadius: 2, background: col, display: "inline-block" }} />
            {k} <b className="mono" style={{ color: "var(--fg)" }}>{led[k] || 0}</b>
          </span>
        ))}
      </div>
    </div>
  );
}

function StatTile({ k, v, d, icon, tone }: { k: string; v: string; d: React.ReactNode; icon: string; tone?: "ok" | "down" }) {
  return (
    <div className="stat">
      <div className="k"><Icon name={icon} className="ic" />{k}</div>
      <div className="v" style={tone === "ok" ? { color: "var(--success)" } : tone === "down" ? { color: "var(--danger)" } : undefined}>{v}</div>
      <div className="d">{d}</div>
    </div>
  );
}
