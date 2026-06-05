"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { getRun, getResults, getTranscript, type RunDetail, type Results } from "@/lib/api";
import { StatusPill, fmtCost } from "@/components/ui";

const ACTIVE = new Set(["queued", "expanding", "running", "finalizing"]);

export default function RunDetailPage({ params }: { params: { id: string } }) {
  const id = params.id;
  const [run, setRun] = useState<RunDetail | null>(null);
  const [res, setRes] = useState<Results | null>(null);
  const [open, setOpen] = useState<{ sid: string; uri: string } | null>(null);

  const refresh = useCallback(() => {
    getRun(id).then(setRun).catch(() => {});
    getResults(id).then(setRes).catch(() => {});
  }, [id]);

  useEffect(() => {
    refresh();
    const t = setInterval(() => {
      getRun(id).then((r) => {
        setRun(r);
        if (ACTIVE.has(r.status)) getResults(id).then(setRes).catch(() => {});
        else { getResults(id).then(setRes).catch(() => {}); clearInterval(t); }
      }).catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, [id, refresh]);

  const su = res?.summary;
  const active = run ? ACTIVE.has(run.status) : false;
  const p = run?.progress ?? {};
  const total = run?.total ?? 0;
  const seg = (n: number) => (total ? `${(n / total) * 100}%` : "0%");

  return (
    <main>
      <Link href="/" className="back">‹ all runs</Link>

      <div className="panel" style={{ padding: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <span className="mono" style={{ fontSize: 18, color: "var(--signal)", fontWeight: 600 }}>{id}</span>
          {run && <StatusPill status={run.status} />}
          <span style={{ flex: 1 }} />
          {run && <span className="tag">{run.eval_id}</span>}
          {run && <span className="mono muted" style={{ fontSize: 13 }}>{run.model}</span>}
        </div>
        {active && run && (
          <div style={{ marginTop: 16 }}>
            <div className="progress" style={{ height: 10 }}>
              <span className="done" style={{ width: seg(p.done || 0) }} />
              <span className="fail" style={{ width: seg(p.failed || 0) }} />
              <span className="run" style={{ width: seg(p.running || 0) }} />
            </div>
            <div className="mono dim" style={{ fontSize: 11, marginTop: 6 }}>
              {p.done || 0} done · {p.running || 0} running · {p.queued || 0} queued · {p.failed || 0} failed / {total}
            </div>
          </div>
        )}
      </div>

      <div className="metrics">
        <Metric k="accuracy" v={su ? `${Math.round(su.accuracy * 100)}%` : "—"} sub={su ? `${su.passed}/${su.samples} passed` : ""} big />
        <Metric k="mean score" v={su ? su.mean_score.toFixed(3) : "—"} />
        <Metric k="tokens" v={su ? su.tokens.toLocaleString() : "—"} />
        <Metric k="cost" v={su ? fmtCost(su.cost_usd) : "—"} sub="via gateway" />
      </div>

      {res && res.by_category.length > 0 && (
        <div className="panel" style={{ marginBottom: 18 }}>
          <div className="panel-h"><h2>accuracy by category</h2></div>
          <div className="bars">
            {res.by_category.map((c) => (
              <div className="bar-row" key={c.category || "—"}>
                <span className="lbl">{c.category || "—"}</span>
                <div className="bar-track"><div className="bar-fill" style={{ width: `${Math.round(c.accuracy * 100)}%` }} /></div>
                <span className="pct">{Math.round(c.accuracy * 100)}% <span className="dim">·{c.n}</span></span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-h"><h2>samples</h2><span style={{ flex: 1 }} /><span className="dim mono" style={{ fontSize: 11 }}>{res ? `${res.samples.length}` : ""}</span></div>
        {!res && <div className="empty"><span className="spin" /> loading…</div>}
        {res && res.samples.length === 0 && <div className="empty">no samples yet</div>}
        {res && res.samples.length > 0 && (
          <table className="grid">
            <thead><tr><th>sample</th><th>result</th><th>category</th><th className="right">score</th><th></th></tr></thead>
            <tbody>
              {res.samples.map((s) => (
                <tr key={s.sample_id} onClick={() => s.transcript_uri && setOpen({ sid: s.sample_id, uri: s.transcript_uri })}>
                  <td className="mono">{s.sample_id}</td>
                  <td style={{ color: s.passed ? "var(--pass)" : "var(--fail)", fontFamily: "var(--mono)", fontSize: 12 }}>{s.passed ? "● pass" : "○ fail"}</td>
                  <td className="muted">{s.category || "—"}</td>
                  <td className="right num">{s.score.toFixed(2)}</td>
                  <td className="right dim mono" style={{ fontSize: 11 }}>{s.transcript_uri ? "view ›" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {open && <TranscriptDrawer sid={open.sid} uri={open.uri} onClose={() => setOpen(null)} />}
    </main>
  );
}

function Metric({ k, v, sub, big }: { k: string; v: string; sub?: string; big?: boolean }) {
  return (
    <div className="metric">
      <div className="k">{k}</div>
      <div className="v" style={big ? { color: "var(--signal)", fontSize: 34 } : undefined}>{v}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function TranscriptDrawer({ sid, uri, onClose }: { sid: string; uri: string; onClose: () => void }) {
  const [data, setData] = useState<any | null>(null);
  const [raw, setRaw] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    getTranscript(uri).then((t) => { setRaw(t); try { setData(JSON.parse(t)); } catch { setData(null); } }).catch((e) => setErr(String(e)));
  }, [uri]);

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer">
        <div className="drawer-h">
          <strong style={{ fontFamily: "var(--mono)", fontSize: 13 }}>{sid}</strong>
          <span style={{ flex: 1 }} />
          {data?.eval_log_uri && (
            <a className="btn" target="_blank"
               href={`/inspect/?log_file=${encodeURIComponent(String(data.eval_log_uri))}`}>
              full trace ↗
            </a>
          )}
          <button className="btn ghost" onClick={onClose}>close</button>
        </div>
        {err && <div className="empty" style={{ color: "var(--fail)" }}>{err}</div>}
        {!err && !raw && <div className="empty"><span className="spin" /> loading transcript…</div>}
        {data ? (
          <>
            <Section title="input" body={String(data.input ?? "")} />
            <Section title="output" body={String(data.output ?? "")} accent />
            {data.target != null && <Section title="target" body={String(data.target)} />}
            {data.scores && <div className="kv"><span className="k">scores</span><span className="v">{JSON.stringify(data.scores)}</span></div>}
          </>
        ) : raw && <pre className="code">{raw}</pre>}
      </aside>
    </>
  );
}

function Section({ title, body, accent }: { title: string; body: string; accent?: boolean }) {
  return (
    <div>
      <div className="kv" style={{ paddingBottom: 4 }}><span className="k">{title}</span><span /></div>
      <pre className="code" style={accent ? { borderColor: "var(--signal-dim)" } : undefined}>{body}</pre>
    </div>
  );
}
