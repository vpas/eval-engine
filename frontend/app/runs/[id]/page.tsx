"use client";
import { useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { AccBar, Empty, Progress, Provider, StatusPill } from "@/components/ui";
import {
  getRun, getResults, getTranscript, rerunRun, getRunLogsUrl, getRunLive, ago, fmtCost, fmtN, pct,
  type RunDetail, type Results, type RunLive, type LiveSample,
} from "@/lib/api";

const ACTIVE = new Set(["queued", "expanding", "running", "finalizing"]);

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [res, setRes] = useState<Results | null>(null);
  const [openSample, setOpenSample] = useState<Results["samples"][number] | null>(null);
  const [logsUrl, setLogsUrl] = useState<string | null>(null);
  const [live, setLive] = useState<RunLive | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { getRunLogsUrl(id).then((r) => setLogsUrl(r.url)).catch(() => {}); }, [id]);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const r = await getRun(id);
        if (stop) return;
        setRun(r);
        if (ACTIVE.has(r.status)) {
          getRunLive(id).then((x) => !stop && setLive(x)).catch(() => {});
        } else {
          getResults(id).then((x) => !stop && setRes(x)).catch(() => {});
        }
      } catch (e: any) {
        setErr(String(e?.message || e));
      }
    };
    load();
    const t = setInterval(load, 2500);
    return () => { stop = true; clearInterval(t); };
  }, [id]);

  if (err) return <div className="page"><Empty icon="warn">{err}</Empty></div>;
  if (!run) return <div className="page"><Empty icon="pulse"><span className="spin" /> loading run…</Empty></div>;

  const isActive = ACTIVE.has(run.status);
  const p = run.progress || {};

  const rerun = async () => {
    const r = await rerunRun(id);
    router.push(`/runs/${r.run_id}`);
  };

  return (
    <div className="page wide">
      <button className="btn ghost sm" onClick={() => router.push("/")} style={{ marginBottom: 14 }}><Icon name="arrowleft" />all runs</button>

      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-b">
          <div className="between wrap" style={{ gap: 12 }}>
            <div className="vcenter gap12 wrap">
              <span className="mono linklike" style={{ fontSize: 18, fontWeight: 600 }}>{run.id}</span>
              <StatusPill status={run.status} />
              <span className="tag b"><Icon name="flask" size={12} />{run.eval_id} <span className="hash">@{run.eval_version}</span></span>
              <Provider id={run.model} />
            </div>
            <div className="vcenter gap8">
              {logsUrl && <a className="btn ghost sm" href={logsUrl} target="_blank" rel="noreferrer"><Icon name="external" size={12} />Worker logs</a>}
              {!isActive && (
                <>
                  <button className="btn sm" onClick={() => router.push(`/compare?ids=${run.id}`)}><Icon name="compare" />Compare</button>
                  <button className="btn sm" onClick={rerun}><Icon name="refresh" />Re-run</button>
                </>
              )}
            </div>
          </div>
          <div className="vcenter gap16 wrap" style={{ marginTop: 12, fontSize: 11.5 }}>
            <span className="subtle vcenter gap6"><Icon name="user" size={12} />{run.created_by || "—"}</span>
            <span className="subtle vcenter gap6"><Icon name="clock" size={12} />started {ago(run.created_at)}</span>
            <span className="subtle vcenter gap6"><Icon name="box" size={12} />{run.lane || "—"} lane</span>
            <span className="subtle vcenter gap6"><Icon name="layers" size={12} />{fmtN(run.total)} samples</span>
            {run.image_digest && <span className="subtle vcenter gap6"><Icon name="shield" size={12} />{run.image_digest}</span>}
            {run.provider_fingerprint && <span className="subtle vcenter gap6 mono"><Icon name="copy" size={12} />{run.provider_fingerprint}</span>}
          </div>
        </div>

        {isActive && (
          <div className="panel-b" style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
            <div className="between" style={{ marginBottom: 8 }}>
              <span className="eyebrow">live progress</span>
              <span className="mono subtle" style={{ fontSize: 11 }}><span className="spin" style={{ marginRight: 6 }} />streaming · ledger FOR UPDATE SKIP LOCKED</span>
            </div>
            <Progress p={p} total={run.total} />
            <div className="between" style={{ marginTop: 10 }}>
              <div className="leg">
                <span><i style={{ background: "var(--success)" }} />done <b className="num" style={{ color: "var(--fg)" }}>{fmtN(p.done || 0)}</b></span>
                <span><i style={{ background: "var(--attention)" }} />running <b className="num" style={{ color: "var(--fg)" }}>{p.running || 0}</b></span>
                <span><i style={{ background: "var(--panel-3)", border: "1px solid var(--border)" }} />queued <b className="num" style={{ color: "var(--fg)" }}>{fmtN(p.queued || 0)}</b></span>
                <span><i style={{ background: "var(--danger)" }} />failed <b className="num" style={{ color: "var(--fg)" }}>{p.failed || run.failed || 0}</b></span>
              </div>
              <span className="num" style={{ fontSize: 13 }}>{pct((p.done || 0) / (run.total || 1))}%</span>
            </div>
          </div>
        )}
      </div>

      <RunSpecPanel run={run} />

      {isActive && live && <LiveSamples live={live} />}

      {run.status === "failed" && (
        <div className="panel" style={{ marginBottom: 16, borderColor: "var(--danger-emph)" }}>
          <div className="panel-b vcenter gap10" style={{ color: "var(--danger)" }}>
            <Icon name="warn" /><b>Run failed.</b><span className="subtle">{run.failed} samples errored after retries.</span>
          </div>
        </div>
      )}

      {!isActive && res && <Analysis run={run} res={res} onOpen={setOpenSample} />}

      {openSample && <TranscriptDrawer sample={openSample} onClose={() => setOpenSample(null)} />}
    </div>
  );
}

// The exact, reproducible inputs the run executed with (the stored RunSpec). Read-only — "Re-run"
// clones this verbatim. Shows every knob explicitly (incl. the ones left at their default) so there's
// no ambiguity about what actually ran; a raw-JSON toggle gives the literal spec.
function RunSpecPanel({ run }: { run: RunDetail }) {
  const [open, setOpen] = useState(true);
  const [asJson, setAsJson] = useState(false);
  const sp = run.spec;
  if (!sp) return null;
  const cfg = (o?: Record<string, any>) => (o && Object.keys(o).length ? JSON.stringify(o) : "");
  const harness = `${sp.harness?.type ?? "—"}${sp.harness?.version ? ` @${sp.harness.version}` : ""}` +
    (cfg(sp.harness?.config) ? ` · ${cfg(sp.harness?.config)}` : "");
  const scorers = (sp.scorers || []).map((s) => s.type + (cfg(s.config) ? ` (${cfg(s.config)})` : "")).join(", ") || "—";
  const dim = (s: string) => <span className="subtle">{s}</span>;
  const rows: [string, React.ReactNode][] = [
    ["eval", `${sp.eval} @${sp.eval_version ?? 1}`],
    ["dataset", <span className="mono" style={{ wordBreak: "break-all" }}>{sp.dataset}</span>],
    ["model", sp.model],
    ["harness", harness],
    ["scorers", scorers],
    ["slice", sp.limit != null ? `subset · limit ${fmtN(sp.limit)}` : "full dataset"],
    ["epochs", `${sp.epochs ?? 1}×`],
    ["batch size", String(sp.batch_size ?? 50)],
    ["temperature", sp.temperature != null ? String(sp.temperature) : dim("— provider default")],
    ["seed", sp.seed != null ? String(sp.seed) : dim("— unset")],
    ["budget cap", sp.budget_usd != null ? fmtCost(sp.budget_usd) : dim("— uncapped")],
    ["transcript", sp.transcript_sample_rate != null
      ? `keep ${pct(sp.transcript_sample_rate)}% of passes + all failures`
      : dim("— env default (all failures + sampled passes)")],
    ["lane", sp.lane || <>{run.lane || "—"} {dim("· auto-classified")}</>],
    ["team", sp.team || dim("—")],
    ...(sp.mock_output ? ([["mock output", sp.mock_output]] as [string, React.ReactNode][]) : []),
  ];
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-h">
        <Icon name="settings" className="ic" /><h2>RunSpec</h2>
        <span className="tag" style={{ marginLeft: 6 }}><Icon name="shield" size={11} />pinned · reproducible</span>
        <span className="grow" />
        <button className="btn ghost sm" onClick={() => setAsJson((j) => !j)}><Icon name="doc" size={12} />{asJson ? "fields" : "raw JSON"}</button>
        <button className="btn ghost sm" onClick={() => setOpen((o) => !o)}><Icon name={open ? "chevdown" : "chevright"} size={12} /></button>
      </div>
      {open && (
        <div className="panel-b">
          {asJson ? (
            <pre className="code" style={{ margin: 0 }}>{JSON.stringify(sp, null, 2)}</pre>
          ) : (
            <div className="kv">
              {rows.map(([k, v]) => (
                <span key={k} style={{ display: "contents" }}><span className="k">{k}</span><span className="v">{v}</span></span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const LSTAT: Record<string, string> = {
  running: "var(--attention)", queued: "var(--queue)", done: "var(--success)",
  failed: "var(--danger)", budget_skipped: "var(--done)",
};

function LiveSamples({ live }: { live: RunLive }) {
  const [filter, setFilter] = useState<string>("all");
  const c = live.counts || {};
  const rows = live.samples.filter((s) => filter === "all" || s.status === filter);
  return (
    <div className="panel flush" style={{ marginBottom: 16 }}>
      <div className="panel-h">
        <Icon name="list" className="ic" />
        <h2>Live samples</h2>
        {live.agentic && <span className="badge" style={{ marginLeft: 4 }}>agentic{live.sandbox ? ` · ${live.sandbox}` : ""}</span>}
        <span className="grow" />
        <div className="seg">
          {["all", "running", "queued", "done", "failed"].map((s) => (
            <button key={s} className={filter === s ? "on" : ""} onClick={() => setFilter(s)}>
              {s}{s !== "all" && c[s] != null ? ` ${c[s]}` : ""}
            </button>
          ))}
        </div>
      </div>
      <table className="grid">
        <thead><tr>
          <th>Sample</th><th>Status</th><th className="right">Try</th><th>Worker</th><th>Category</th><th>Logs</th>
        </tr></thead>
        <tbody>
          {rows.slice(0, 500).map((s: LiveSample) => {
            const dot: React.CSSProperties = {
              width: 7, height: 7, borderRadius: "50%", display: "inline-block",
              background: LSTAT[s.status] || "var(--fg-muted)",
              ...(s.status === "running" ? { animation: "blink 1.1s ease-in-out infinite" } : {}),
            };
            return (
              <tr key={s.sample_id}>
                <td className="mono" style={{ fontSize: 11.5 }}>{s.sample_id}</td>
                <td>
                  <span className="vcenter gap6">
                    <span style={dot} />
                    <span className="mono" style={{ fontSize: 11.5 }}>{s.status}</span>
                    {s.status === "running" && s.lease_s != null && <span className="subtle" style={{ fontSize: 10.5 }}>· lease {s.lease_s}s</span>}
                    {s.error_type && <span className="subtle" style={{ fontSize: 10.5, color: "var(--danger)" }}>· {s.error_type}</span>}
                  </span>
                </td>
                <td className="right num cellmuted">{s.attempts}</td>
                <td className="mono cellmuted" style={{ fontSize: 11 }} title={s.claimed_by || ""}>{s.claimed_by ? s.claimed_by.split("-").slice(-2).join("-") : "—"}</td>
                <td className="cellmuted mono" style={{ fontSize: 11 }}>{s.group_key || "—"}</td>
                <td>
                  <span className="vcenter gap6">
                    {s.worker_logs_url && <a className="btn sm ghost" href={s.worker_logs_url} target="_blank" rel="noreferrer" title="claiming worker logs"><Icon name="external" size={12} />worker</a>}
                    {s.sandbox_logs_url && <a className="btn sm ghost" href={s.sandbox_logs_url} target="_blank" rel="noreferrer" title="sandbox namespace logs"><Icon name="box" size={12} />sandbox</a>}
                    {!s.worker_logs_url && !s.sandbox_logs_url && <span className="subtle">—</span>}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && <div className="panel-b"><Empty icon="list">No {filter === "all" ? "" : filter + " "}samples in the ledger.</Empty></div>}
    </div>
  );
}

function Analysis({ run, res, onOpen }: { run: RunDetail; res: Results; onOpen: (s: Results["samples"][number]) => void }) {
  const s = res.summary;
  const ci = s.accuracy_ci;
  const [filter, setFilter] = useState<"all" | "fail" | "pass">("all");
  const [cat, setCat] = useState("all");
  const [q, setQ] = useState("");
  const [minScore, setMinScore] = useState(0);

  const cats = ["all", ...Array.from(new Set(res.by_category.map((c) => c.category || "—")))];
  const failCount = res.samples.filter((r) => !r.passed).length;
  const rows = res.samples.filter((r) => {
    if (filter === "fail" && r.passed) return false;
    if (filter === "pass" && !r.passed) return false;
    if (cat !== "all" && (r.category || "—") !== cat) return false;
    if ((r.score ?? 0) < minScore) return false;
    if (q && !`${r.sample_id}${r.category ?? ""}`.toLowerCase().includes(q.toLowerCase())) return false;
    return true;
  });
  // score histogram over the retained sample rows (20 bins)
  const hist = useMemo(() => {
    const bins = new Array(20).fill(0);
    for (const r of res.samples) bins[Math.min(19, Math.max(0, Math.floor((r.score ?? 0) * 20)))]++;
    return bins;
  }, [res.samples]);

  return (
    <>
      <div className="stat-row" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="k"><Icon name="target" className="ic" />accuracy</div>
          <div className="v" style={{ color: "var(--success)" }}>{pct(s.accuracy)}%</div>
          <div className="d"><span className="subtle mono">{fmtN(s.passed)}/{fmtN(s.samples)} passed</span></div>
          {ci && (
            <div style={{ marginTop: 10 }}>
              <CIBar lo={ci[0]} hi={ci[1]} acc={s.accuracy} />
              <div className="subtle mono" style={{ fontSize: 10.5, marginTop: 2 }}>95% CI [{(ci[0] * 100).toFixed(1)}%, {(ci[1] * 100).toFixed(1)}%]</div>
            </div>
          )}
        </div>
        <MetricTile k="mean score" icon="slice" v={s.mean_score?.toFixed(3) ?? "—"} sub={`${res.by_category.length} categories`} />
        <MetricTile k="tokens" icon="layers" v={fmtN(s.tokens)} sub="in + out" />
        <MetricTile k="cost" icon="dollar" v={fmtCost(s.cost_usd ?? run.cost_usd ?? 0)} sub={`${run.harness ?? ""} · catalog price`} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr", gap: 16, marginBottom: 16 }}>
        <div className="panel">
          <div className="panel-h"><Icon name="chart" className="ic" /><h2>Accuracy by category</h2><span className="grow" /><span className="sub mono">{res.by_category.length} slices</span></div>
          <div className="panel-b">
            {res.by_category.length === 0 ? <Empty icon="chart">No category breakdown.</Empty> : (
              <div className="bars">
                {res.by_category.slice().sort((a, b) => b.accuracy - a.accuracy).map((c) => (
                  <div className="bar-row" key={c.category} style={{ cursor: "pointer" }} onClick={() => { setCat(c.category || "—"); setFilter("all"); }}>
                    <span className="lbl">{c.category || "—"}</span>
                    <div className="bar-track"><div className={"bar-fill" + (c.accuracy >= 0.8 ? " success" : "")} style={{ width: pct(c.accuracy) + "%" }} /></div>
                    <span className="pct">{pct(c.accuracy)}% <span className="subtle">· {c.n}</span></span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="panel">
          <div className="panel-h"><Icon name="gauge" className="ic" /><h2>Score distribution</h2></div>
          <div className="panel-b">
            <Histogram bins={hist} />
            <div className="between" style={{ marginTop: 8, fontSize: 11 }}>
              <span className="subtle mono">0.0</span><span className="subtle">score</span><span className="subtle mono">1.0</span>
            </div>
          </div>
        </div>
      </div>

      {/* full-width sample explorer */}
      <div className="panel flush">
        <div className="panel-h"><Icon name="list" className="ic" /><h2>Sample explorer</h2><span className="grow" /><span className="sub mono">{rows.length} shown</span></div>
        <div className="panel-b" style={{ borderBottom: "1px solid var(--border)" }}>
          <div className="filterbar">
            <div className="fsearch" style={{ maxWidth: 240 }}><Icon name="search" className="ic" /><input placeholder="sample id / category…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
            <div className="seg">
              <button className={filter === "all" ? "on" : ""} onClick={() => setFilter("all")}>all <span className="subtle">{res.samples.length}</span></button>
              <button className={filter === "fail" ? "on" : ""} onClick={() => setFilter("fail")} style={filter === "fail" ? { color: "var(--danger)" } : undefined}>failures <span className="subtle">{failCount}</span></button>
              <button className={filter === "pass" ? "on" : ""} onClick={() => setFilter("pass")}>passes</button>
            </div>
            <select className="input" style={{ width: "auto", fontFamily: "var(--mono)" }} value={cat} onChange={(e) => setCat(e.target.value)}>
              {cats.map((c) => <option key={c} value={c}>{c === "all" ? "all categories" : c}</option>)}
            </select>
            <div className="vcenter gap8" style={{ marginLeft: "auto" }}>
              <span className="subtle" style={{ fontSize: 11.5 }}>min score {minScore.toFixed(1)}</span>
              <input type="range" min="0" max="1" step="0.1" value={minScore} onChange={(e) => setMinScore(+e.target.value)} style={{ width: 110, accentColor: "var(--accent)" }} />
            </div>
          </div>
        </div>
        <table className="grid">
          <thead><tr><th>Sample</th><th>Result</th><th>Category</th><th className="right">Score</th><th className="right">Tokens</th><th className="right">Latency</th><th>Error</th><th></th></tr></thead>
          <tbody>
            {rows.map((sm) => (
              <tr key={sm.sample_id} className={sm.transcript_uri ? "click" : ""} onClick={() => sm.transcript_uri && onOpen(sm)}>
                <td className="mono">{sm.sample_id}</td>
                <td>{sm.passed ? <span className="mono" style={{ color: "var(--success)", fontSize: 11.5 }}>● pass</span> : <span className="mono" style={{ color: "var(--danger)", fontSize: 11.5 }}>○ fail</span>}</td>
                <td className="cellmuted">{sm.category || "—"}</td>
                <td className="right num">{sm.score?.toFixed(2)}</td>
                <td className="right num cellmuted">{sm.tokens ? fmtN(sm.tokens) : "—"}</td>
                <td className="right num cellmuted">{sm.latency_ms ? (sm.latency_ms / 1000).toFixed(1) + "s" : "—"}</td>
                <td>{sm.error_type ? <span className="mono" style={{ color: "var(--attention-fg)", fontSize: 11 }}>{sm.error_type}</span> : <span className="subtle">—</span>}</td>
                <td className="right">{sm.transcript_uri
                  ? <span className="linklike" style={{ fontSize: 11 }}>view ›</span>
                  : <span className="subtle" style={{ fontSize: 11 }} title="Transcript not retained — sample-by-default retention keeps all failures + a fraction of passes (DESIGN §13).">not kept</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <Empty icon="filter">No samples match these filters.</Empty>}
      </div>
    </>
  );
}

function MetricTile({ k, v, sub, icon }: { k: string; v: string; sub: string; icon: string }) {
  return (
    <div className="stat">
      <div className="k"><Icon name={icon} className="ic" />{k}</div>
      <div className="v">{v}</div>
      <div className="d"><span className="subtle">{sub}</span></div>
    </div>
  );
}

function CIBar({ lo, hi, acc }: { lo: number; hi: number; acc: number }) {
  return (
    <div className="cibar">
      <div className="axis" />
      {[0, 0.25, 0.5, 0.75, 1].map((t) => <div key={t} className="tick" style={{ left: t * 100 + "%" }} />)}
      <div className="range" style={{ left: lo * 100 + "%", width: (hi - lo) * 100 + "%" }} />
      <div className="point" style={{ left: acc * 100 + "%" }} />
    </div>
  );
}

function Histogram({ bins }: { bins: number[] }) {
  const max = Math.max(...bins) || 1;
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 110 }}>
      {bins.map((b, i) => {
        const t = i / bins.length;
        const color = t < 0.4 ? "var(--danger)" : t < 0.7 ? "var(--attention)" : "var(--success)";
        return <div key={i} title={`${t.toFixed(2)}–${(t + 0.05).toFixed(2)}: ${b}`} style={{ flex: 1, height: Math.max(2, (b / max) * 100) + "%", background: color, opacity: 0.55, borderRadius: "2px 2px 0 0" }} />;
      })}
    </div>
  );
}

function TranscriptDrawer({ sample, onClose }: { sample: Results["samples"][number]; onClose: () => void }) {
  const [body, setBody] = useState<any>(null);
  const [raw, setRaw] = useState<string>("");
  useEffect(() => {
    getTranscript(sample.transcript_uri).then((t) => {
      setRaw(t);
      try { setBody(JSON.parse(t)); } catch { setBody(null); }
    }).catch((e) => setRaw(String(e)));
  }, [sample.transcript_uri]);

  // Deep-link into the embedded Inspect viewer for THIS sample's .eval log. The viewer is launched
  // with `--log-dir gs://…/eval-logs`, and its client expects a RELATIVE basename (no scheme/`//`, so
  // oauth2-proxy's path.Clean can't collapse it) + `inspect_server=true` to force the server API
  // (see eval_engine/view_main.py). Without this the link fell back to the viewer root ("/inspect/").
  const logFile = body?.eval_log_uri ? String(body.eval_log_uri).split("/").pop() : "";
  const viewerHref = logFile
    ? `/inspect/?log_file=${encodeURIComponent(logFile)}&inspect_server=true`
    : "";

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer">
        <div className="drawer-h">
          <Icon name="doc" className="ic" style={{ color: "var(--accent-fg)" }} />
          <strong style={{ fontSize: 13 }}>Transcript · <span className="mono">{sample.sample_id}</span></strong>
          <span className="grow" />
          {viewerHref && <a className="btn ghost sm" href={viewerHref} target="_blank" rel="noreferrer"><Icon name="external" size={12} />viewer</a>}
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div style={{ padding: "14px 16px" }}>
          {!raw && <Empty icon="doc"><span className="spin" /> loading…</Empty>}
          {body ? (
            <>
              <Msg role="input" body={String(body.input ?? "")} />
              <Msg role="output" body={String(body.output ?? "")} />
              <Msg role="target" body={String(body.target ?? "")} />
              {body.scores && (
                <div className="panel" style={{ marginTop: 10 }}>
                  <div className="panel-b">
                    <div className="eyebrow" style={{ marginBottom: 6 }}>scores</div>
                    <div className="kv">
                      {Object.entries(body.scores).map(([k, v]) => (
                        <span key={k} style={{ display: "contents" }}><span className="k">{k}</span><span className="v">{String(v)}</span></span>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </>
          ) : (
            raw && <pre className="code">{raw}</pre>
          )}
        </div>
      </aside>
    </>
  );
}

function Msg({ role, body }: { role: string; body: string }) {
  const cls = role === "output" ? "assistant" : role === "input" ? "user" : "system";
  return (
    <div className={`msg ${cls}`} style={{ marginBottom: 8 }}>
      <div className="role"><Icon name={role === "output" ? "cpu" : role === "target" ? "target" : "user"} className="ic" />{role}</div>
      <div className="body">{body || "—"}</div>
    </div>
  );
}
