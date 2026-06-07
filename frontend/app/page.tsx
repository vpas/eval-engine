"use client";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { AccBar, Empty, Provider, StatusPill } from "@/components/ui";
import { getRuns, getMe, ago, fmtCost, fmtN, pct, type Run } from "@/lib/api";

const ACTIVE = new Set(["queued", "expanding", "running", "finalizing"]);

export default function Dashboard() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("all");
  const [mine, setMine] = useState(false);
  const [evalF, setEvalF] = useState("all");
  const [sel, setSel] = useState<string[]>([]);

  const { data: me = null } = useQuery({ queryKey: ["me"], queryFn: () => getMe().then((m) => m.email) });
  const runsQuery = useQuery({ queryKey: ["runs"], queryFn: getRuns, refetchInterval: 4000 });
  const runs: Run[] = runsQuery.data ?? [];
  const loaded = !runsQuery.isPending;

  const evalOpts = useMemo(() => ["all", ...Array.from(new Set(runs.map((r) => r.eval)))], [runs]);

  const filtered = runs.filter((r) => {
    const st = r.status || "";
    if (status === "active" && !ACTIVE.has(st)) return false;
    if (status === "completed" && st !== "completed") return false;
    if (status === "failed" && st !== "failed") return false;
    if (mine && r.created_by !== me) return false;
    if (evalF !== "all" && r.eval !== evalF) return false;
    if (q && !`${r.id}${r.eval}${r.model}${r.created_by ?? ""}`.toLowerCase().includes(q.toLowerCase())) return false;
    return true;
  });

  const stats = useMemo(() => {
    const active = runs.filter((r) => ACTIVE.has(r.status || "")).length;
    const done = runs.filter((r) => r.status === "completed" && r.accuracy != null);
    const avg = done.reduce((a, r) => a + (r.accuracy || 0), 0) / (done.length || 1);
    const samples = runs.reduce((a, r) => a + (r.total || 0), 0);
    return { active, avg, samples, count: runs.length };
  }, [runs]);

  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  return (
    <div className="page wide">
      <div className="between" style={{ marginBottom: 18 }}>
        <div>
          <div className="eyebrow">shared workspace · research</div>
          <h1 className="title" style={{ marginTop: 4 }}>Dashboard</h1>
        </div>
        <div className="vcenter gap8">
          <Link className="btn" href="/compare"><Icon name="compare" />Compare</Link>
        </div>
      </div>

      <div className="stat-row" style={{ marginBottom: 18 }}>
        <StatTile k="Active runs" icon="pulse" v={String(stats.active)} d={<span className="subtle">{runs.filter((r) => r.status === "running").length} running · {runs.filter((r) => r.status === "queued").length} queued</span>} />
        <StatTile k="Avg accuracy · completed" icon="target" v={pct(stats.avg) + "%"} signal d={<span className="subtle">{runs.filter((r) => r.status === "completed").length} completed runs</span>} />
        <StatTile k="Samples · total" icon="layers" v={fmtN(stats.samples)} d={<span className="subtle">across {stats.count} runs</span>} />
        <StatTile k="Runs" icon="list" v={String(stats.count)} d={<span className="subtle">{runs.filter((r) => r.status === "failed").length} failed</span>} />
      </div>

      <div className="panel flush">
        <div className="panel-h">
          <Icon name="list" className="ic" />
          <h2>Runs</h2>
          <span className="grow" />
          <span className="sub mono">{filtered.length} of {runs.length}</span>
        </div>
        <div className="panel-b" style={{ paddingTop: 11, paddingBottom: 11, borderBottom: "1px solid var(--border)" }}>
          <div className="filterbar">
            <div className="fsearch" style={{ maxWidth: 280 }}>
              <Icon name="search" className="ic" />
              <input placeholder="filter by id, model, eval, owner…" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <div className="seg">
              {["all", "active", "completed", "failed"].map((s) => (
                <button key={s} className={status === s ? "on" : ""} onClick={() => setStatus(s)}>{s}</button>
              ))}
            </div>
            <select className="input" style={{ width: "auto", fontFamily: "var(--mono)" }} value={evalF} onChange={(e) => setEvalF(e.target.value)}>
              {evalOpts.map((e) => <option key={e} value={e}>{e === "all" ? "all evals" : e}</option>)}
            </select>
            <div className={`chip ${mine ? "on" : ""}`} onClick={() => setMine((m) => !m)}><Icon name="user" size={12} />mine</div>
          </div>
        </div>

        <table className="grid">
          <thead>
            <tr>
              <th style={{ width: 30 }}></th>
              <th>Run</th><th>Eval</th><th>Model</th><th>Status</th>
              <th style={{ width: 220 }}>Accuracy</th>
              <th className="right">Samples</th><th className="right">Cost</th><th>By</th><th className="right">Age</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => {
              const isSel = sel.includes(r.id);
              const active = ACTIVE.has(r.status || "");
              return (
                <tr key={r.id} className={"click" + (isSel ? " sel" : "")} onClick={() => router.push(`/runs/${r.id}`)}>
                  <td onClick={(e) => { e.stopPropagation(); toggle(r.id); }}>
                    <span className={`chk ${isSel ? "on" : ""}`}>{isSel && <Icon name="check" size={12} />}</span>
                  </td>
                  <td><span className="linklike mono">{r.id}</span>{r.sweep && <span className="badge" style={{ marginLeft: 6 }}>sweep</span>}</td>
                  <td><span className="mono">{r.eval}</span>{r.eval_version != null && <span className="hash"> @{r.eval_version}</span>}</td>
                  <td><Provider id={r.model} /></td>
                  <td><StatusPill status={r.status || "queued"} /></td>
                  <td>{active ? <span className="mono subtle" style={{ fontSize: 11.5 }}><span className="spin" style={{ marginRight: 6 }} />in progress</span> : <AccBar value={r.accuracy} />}</td>
                  <td className="right num">{fmtN(r.total)}</td>
                  <td className="right num cellmuted">{fmtCost(r.cost ?? 0)}</td>
                  <td className="cellmuted mono" style={{ fontSize: 11.5 }} title={r.created_by ?? ""}>{(r.created_by ?? "—").split("@")[0]}</td>
                  <td className="right cellmuted mono" style={{ fontSize: 11.5 }}>{ago(r.created_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {loaded && filtered.length === 0 && <Empty icon="search">No runs match these filters.</Empty>}
        {!loaded && <Empty icon="list"><span className="spin" /> loading runs…</Empty>}
      </div>

      {sel.length > 0 && (
        <div className="toast" style={{ bottom: 22 }}>
          <span className="mono"><b>{sel.length}</b> selected</span>
          <button className="btn sm ghost" onClick={() => setSel([])}>clear</button>
          <button className="btn sm accent" disabled={sel.length < 2} onClick={() => router.push(`/compare?ids=${sel.join(",")}`)}>
            <Icon name="compare" size={13} />Compare {sel.length}
          </button>
        </div>
      )}
    </div>
  );
}

function StatTile({ k, v, d, icon, signal }: { k: string; v: string; d: React.ReactNode; icon: string; signal?: boolean }) {
  return (
    <div className="stat">
      <div className="between">
        <div className="k"><Icon name={icon} className="ic" />{k}</div>
      </div>
      <div className="v" style={signal ? { color: "var(--success)" } : undefined}>{v}</div>
      <div className="d">{d}</div>
    </div>
  );
}
