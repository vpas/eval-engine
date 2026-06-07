"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { Kind, Provider } from "@/components/ui";
import { getEvals, getModels, launchFromEval, fmtN, fmtCost, type Entity } from "@/lib/api";

const MODEL_SUGGESTIONS = ["mockllm/model", "openai/gpt-4o-mini", "openrouter/meta-llama/llama-3.1-8b-instruct"];

// A faithful port of the prototype's launch composer, wired to POST /evals/{id}/launch — the eval
// supplies the (pinned) dataset + default harness/scorers; the caller picks model(s) + run knobs.
export function LaunchDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [evals, setEvals] = useState<Entity[]>([]);
  const [models, setModels] = useState<Entity[]>([]);
  const [evalId, setEvalId] = useState<string>("");
  const [mode, setMode] = useState<"single" | "matrix">("single");
  const [model, setModel] = useState("mockllm/model");
  const [multi, setMulti] = useState<string[]>(["mockllm/model"]);
  const [slice, setSlice] = useState<"full" | "subset">("full");
  // Numeric fields are kept as STRINGS so the input can be fully cleared while typing (coercing with
  // `+value` turns "" into 0, which snaps the field back to a stuck leading 0). Coerced at submit, the
  // same way `seed` already is.
  const [subsetN, setSubsetN] = useState("200");
  const [epochs, setEpochs] = useState("1");
  const [temp, setTemp] = useState("0");
  const [seed, setSeed] = useState<string>("");
  const [budget, setBudget] = useState("60");
  const [keepAll, setKeepAll] = useState(false);
  const [mock, setMock] = useState("Paris");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getEvals().then((e) => { setEvals(e); if (e[0]) setEvalId(e[0].id); }).catch(() => {});
    getModels().then(setModels).catch(() => {});
  }, []);

  const ev = useMemo(() => evals.find((e) => e.id === evalId), [evals, evalId]);
  const chosen = mode === "single" ? (model ? [model] : []) : multi;
  const isMock = chosen.every((m) => m.startsWith("mockllm"));
  // Coerced numerics (empty/invalid → sensible fallback); used at submit + for the estimate panel.
  const epochsN = Math.max(1, Number(epochs) || 1);
  const subsetNum = subsetN === "" ? undefined : Number(subsetN);
  const budgetN = Number(budget) || 0;
  const limit = slice === "subset" ? subsetNum : undefined;
  const modelOpts = Array.from(new Set([...MODEL_SUGGESTIONS, ...models.map((m) => `${m.body.provider}/${m.body.model_id}`)]));

  const toggle = (id: string) => setMulti((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const launch = async () => {
    if (!evalId || chosen.length === 0) return;
    setBusy(true);
    setErr(null);
    try {
      const ids: string[] = [];
      for (const m of chosen) {
        const r = await launchFromEval(evalId, {
          model: m, limit, epochs: epochsN, budget_usd: budgetN || undefined,
          temperature: Number(temp) || undefined, seed: seed === "" ? undefined : Number(seed),
          transcript_sample_rate: keepAll ? 1.0 : undefined,
          mock_output: m.startsWith("mockllm") ? mock : undefined,
        });
        ids.push(r.run_id);
      }
      onClose();
      if (mode === "single" && ids[0]) router.push(`/runs/${ids[0]}`);
      else router.refresh();
    } catch (e: any) {
      setErr(String(e?.message || e));
      setBusy(false);
    }
  };

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="fs-dialog">
        <div className="fs-top">
          <div className="ttl"><Icon name="rocket" className="ic" />Launch run</div>
          <span className="grow" />
          <div className="seg">
            <button className={mode === "single" ? "on" : ""} onClick={() => setMode("single")}><Icon name="play" size={12} />Single run</button>
            <button className={mode === "matrix" ? "on" : ""} onClick={() => setMode("matrix")}><Icon name="grid" size={12} />Matrix sweep</button>
          </div>
          <span className="grow" />
          <button className="btn ghost" onClick={onClose}><Icon name="x" />esc</button>
        </div>

        <div className="fs-body">
          <div className="launch-grid">
            <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
              <Section n="1" title="Eval" hint="A registered, versioned bundle of dataset + harness + scorers.">
                {evals.length === 0 ? (
                  <div className="subtle" style={{ fontSize: 12 }}>No registered evals. Register one via <span className="mono">POST /evals</span>.</div>
                ) : (
                  <div className="choice-cols">
                    {evals.map((e) => (
                      <div key={e.id} className={`choice ${evalId === e.id ? "on" : ""}`} onClick={() => setEvalId(e.id)}>
                        <span className="ck" />
                        <div style={{ minWidth: 0 }}>
                          <div className="ti mono-id">{e.id} <span className="hash">@{e.version}</span></div>
                          <div className="de">{e.body.description || "—"}</div>
                          <div className="vcenter gap6" style={{ marginTop: 6 }}>
                            <Kind kind="dataset">{e.body.dataset}</Kind>
                            <Kind kind="harness">{e.body.default_harness?.type}</Kind>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Section>

              <Section n="2" title="Dataset slice" hint="Full or a capped subset (interactive lane is auto-selected for subsets).">
                <div className="vcenter gap8">
                  <div className={`chip ${slice === "full" ? "on" : ""}`} onClick={() => setSlice("full")}>full</div>
                  <div className={`chip ${slice === "subset" ? "on" : ""}`} onClick={() => setSlice("subset")}>subset</div>
                  {slice === "subset" && (
                    <div className="vcenter gap8">
                      <input className="input" type="number" style={{ width: 110 }} value={subsetN} onChange={(e) => setSubsetN(e.target.value)} />
                      <span className="subtle mono" style={{ fontSize: 11 }}>samples</span>
                    </div>
                  )}
                </div>
              </Section>

              <Section n="3" title={mode === "single" ? "Target model" : "Target models"} hint={mode === "single" ? "What we evaluate (provider/model id)." : "Fan out across models — one run each."}>
                {mode === "single" ? (
                  <>
                    <input className="input" value={model} onChange={(e) => setModel(e.target.value)} placeholder="openai/gpt-4o-mini" />
                    <div className="vcenter gap6 wrap" style={{ marginTop: 8 }}>
                      {modelOpts.map((m) => (
                        <div key={m} className={`chip ${model === m ? "on" : ""}`} onClick={() => setModel(m)}><Provider id={m} /></div>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="vcenter gap6 wrap">
                    {modelOpts.map((m) => (
                      <div key={m} className={`chip ${multi.includes(m) ? "on" : ""}`} onClick={() => toggle(m)}>
                        <span className={`chk ${multi.includes(m) ? "on" : ""}`} style={{ width: 13, height: 13 }}>{multi.includes(m) && <Icon name="check" size={10} />}</span>
                        <Provider id={m} />
                      </div>
                    ))}
                  </div>
                )}
                {isMock && (
                  <div className="field" style={{ marginTop: 10, maxWidth: 280 }}>
                    <label>Mock output <span className="subtle">(deterministic, no key)</span></label>
                    <input className="input" value={mock} onChange={(e) => setMock(e.target.value)} />
                  </div>
                )}
              </Section>

              <Section n="4" title="Sampling & budget" hint="Epochs repeat each sample for CIs; the budget cap is a terminal BudgetExceeded signal.">
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, maxWidth: 420 }}>
                  <div className="field"><label>Epochs <span className="subtle">(repeat for CIs)</span></label>
                    <input className="input" type="number" min="1" value={epochs} onChange={(e) => setEpochs(e.target.value)} />
                  </div>
                  <div className="field"><label>Temperature</label>
                    <input className="input" type="number" step="0.1" min="0" max="2" value={temp} onChange={(e) => setTemp(e.target.value)} />
                  </div>
                  <div className="field"><label>Seed <span className="subtle">(optional)</span></label>
                    <input className="input" type="number" placeholder="—" value={seed} onChange={(e) => setSeed(e.target.value)} />
                  </div>
                  <div className="field"><label>Budget cap ($)</label>
                    <input className="input" type="number" value={budget} onChange={(e) => setBudget(e.target.value)} />
                  </div>
                </div>
                <div className="vcenter gap8" style={{ marginTop: 12 }}>
                  <span className={`switch ${keepAll ? "on" : ""}`} onClick={() => setKeepAll((k) => !k)} />
                  <span style={{ fontSize: 12 }}>{keepAll ? "keep all transcripts" : "sample-by-default retention"}</span>
                  <span className="subtle" style={{ fontSize: 11 }}>{keepAll ? "every transcript persisted" : "all failures + a fraction of passes"}</span>
                </div>
              </Section>
            </div>

            <div className="lg-aside">
              <div className="panel">
                <div className="panel-h"><Icon name="doc" className="ic" /><h2>RunSpec</h2><span className="grow" /><span className="tag"><Icon name="shield" size={11} />pinned</span></div>
                <div className="panel-b">
                  <div className="kv">
                    <span className="k">eval</span><span className="v">{ev ? `${ev.id} @${ev.version}` : "—"}</span>
                    <span className="k">dataset</span><span className="v">{ev?.body.dataset || "—"}</span>
                    <span className="k">harness</span><span className="v">{ev?.body.default_harness?.type || "—"}</span>
                    <span className="k">scorers</span><span className="v">{(ev?.body.default_scorers || []).map((s: any) => s.type).join(", ") || "—"}</span>
                    <span className="k">slice</span><span className="v">{slice === "full" ? "full" : `subset n=${subsetN}`}{epochsN > 1 ? ` · ${epochsN}×` : ""}</span>
                    <span className="k">{mode === "single" ? "target" : "targets"}</span><span className="v">{mode === "single" ? model : `${multi.length} models`}</span>
                  </div>
                </div>
                <div className="panel-f vcenter gap8"><Icon name="copy" size={12} />reproducible by spec — "re-run" clones this exactly</div>
              </div>

              <div className="panel">
                <div className="panel-h"><Icon name="gauge" className="ic" /><h2>Estimate</h2></div>
                <div className="panel-b" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <Est k="runs" v={String(chosen.length)} />
                  <Est k="slice" v={slice === "full" ? "full" : fmtN(subsetNum ?? 0)} />
                  <Est k="epochs" v={`${epochsN}×`} />
                  <Est k="budget" v={fmtCost(budgetN)} />
                </div>
              </div>
              {err && <div className="panel" style={{ borderColor: "var(--danger-emph)" }}><div className="panel-b vcenter gap8" style={{ color: "var(--danger)", fontSize: 12 }}><Icon name="warn" size={13} />{err}</div></div>}
            </div>
          </div>
        </div>

        <div className="fs-foot">
          <span className="grow" />
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary lg" disabled={busy || !evalId || chosen.length === 0} onClick={launch}>
            <Icon name="rocket" />{busy ? "Launching…" : mode === "matrix" ? `Launch ${chosen.length} runs` : "Launch run"}
          </button>
        </div>
      </div>
    </>
  );
}

function Section({ n, title, hint, children }: { n: string; title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="vcenter gap10" style={{ marginBottom: 10 }}>
        <span className="num" style={{ width: 22, height: 22, borderRadius: "50%", border: "1px solid var(--border)", display: "grid", placeItems: "center", fontSize: 11, color: "var(--fg-muted)", flex: "none" }}>{n}</span>
        <div>
          <div style={{ fontSize: 13.5, fontWeight: 600 }}>{title}</div>
          {hint && <div className="subtle" style={{ fontSize: 11.5 }}>{hint}</div>}
        </div>
      </div>
      <div style={{ paddingLeft: 32 }}>{children}</div>
    </div>
  );
}

function Est({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <div className="subtle" style={{ fontSize: 11 }}>{k}</div>
      <div className="num" style={{ fontSize: 18, marginTop: 2 }}>{v}</div>
    </div>
  );
}
