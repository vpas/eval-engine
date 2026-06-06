"use client";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { AccBar, Empty, Progress, Provider, StatusPill } from "@/components/ui";
import {
  getRun, getResults, getTranscript, rerunRun, ago, fmtCost, fmtN, pct,
  type RunDetail, type Results,
} from "@/lib/api";

const ACTIVE = new Set(["queued", "expanding", "running", "finalizing"]);

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [res, setRes] = useState<Results | null>(null);
  const [openSample, setOpenSample] = useState<Results["samples"][number] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const r = await getRun(id);
        if (stop) return;
        setRun(r);
        if (!ACTIVE.has(r.status)) {
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

function Analysis({ run, res, onOpen }: { run: RunDetail; res: Results; onOpen: (s: Results["samples"][number]) => void }) {
  const s = res.summary;
  const ci = s.accuracy_ci;
  const maxCat = Math.max(0.0001, ...res.by_category.map((c) => c.accuracy));
  return (
    <>
      <div className="stat-row" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="k"><Icon name="target" className="ic" />accuracy</div>
          <div className="v" style={{ color: "var(--success)" }}>{pct(s.accuracy)}%</div>
          <div className="d"><span className="subtle mono">{ci ? `95% CI ${(ci[0] * 100).toFixed(1)}–${(ci[1] * 100).toFixed(1)}%` : `${s.passed}/${s.samples} passed`}</span></div>
        </div>
        <div className="stat">
          <div className="k"><Icon name="check" className="ic" />passed</div>
          <div className="v">{fmtN(s.passed)}<span className="subtle" style={{ fontSize: 13 }}> / {fmtN(s.samples)}</span></div>
          <div className="d"><span className="subtle mono">mean score {s.mean_score?.toFixed(3)}</span></div>
        </div>
        <div className="stat">
          <div className="k"><Icon name="layers" className="ic" />tokens</div>
          <div className="v">{fmtN(s.tokens)}</div>
          <div className="d"><span className="subtle">in + out</span></div>
        </div>
        <div className="stat">
          <div className="k"><Icon name="dollar" className="ic" />cost</div>
          <div className="v">{fmtCost(s.cost_usd ?? run.cost_usd ?? 0)}</div>
          <div className="d"><span className="subtle">catalog price</span></div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: 16 }}>
        <div className="panel flush">
          <div className="panel-h"><Icon name="chart" className="ic" /><h2>Accuracy by category</h2></div>
          <div className="panel-b">
            {res.by_category.length === 0 && <Empty icon="chart">No category breakdown.</Empty>}
            <div className="bars">
              {res.by_category.map((c) => (
                <div key={c.category} className="bar-row">
                  <span className="lbl">{c.category || "—"}</span>
                  <div className="bar-track"><div className="bar-fill success" style={{ width: (c.accuracy / maxCat) * 100 + "%" }} /></div>
                  <span className="pct">{pct(c.accuracy)}% <span className="subtle">· {c.n}</span></span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="panel flush">
          <div className="panel-h"><Icon name="list" className="ic" /><h2>Samples</h2><span className="grow" /><span className="sub mono">{res.samples.length}</span></div>
          <table className="grid">
            <thead><tr><th>Sample</th><th>Result</th><th>Category</th><th className="right">Score</th><th></th></tr></thead>
            <tbody>
              {res.samples.map((sm) => (
                <tr key={sm.sample_id} className={sm.transcript_uri ? "click" : ""} onClick={() => sm.transcript_uri && onOpen(sm)}>
                  <td className="mono">{sm.sample_id}</td>
                  <td>{sm.passed ? <span className="mono" style={{ color: "var(--success)", fontSize: 11.5 }}>● pass</span> : <span className="mono" style={{ color: "var(--danger)", fontSize: 11.5 }}>○ fail</span>}</td>
                  <td className="cellmuted">{sm.category || "—"}</td>
                  <td className="right num">{sm.score?.toFixed(2)}</td>
                  <td className="right">{sm.transcript_uri ? <span className="linklike" style={{ fontSize: 11 }}>view →</span> : <span className="subtle" style={{ fontSize: 11 }}>—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {res.samples.length === 0 && <Empty icon="list">No sampled transcripts retained.</Empty>}
        </div>
      </div>
    </>
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

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer">
        <div className="drawer-h">
          <Icon name="doc" className="ic" style={{ color: "var(--accent-fg)" }} />
          <strong style={{ fontSize: 13 }}>Transcript · <span className="mono">{sample.sample_id}</span></strong>
          <span className="grow" />
          {body?.eval_log_uri && <a className="btn ghost sm" href="/inspect/" target="_blank" rel="noreferrer"><Icon name="external" size={12} />viewer</a>}
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
