"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Icon } from "@/components/icons";
import { Empty, StatusPill } from "@/components/ui";
import { getTrainingRuns, ago, fmtN, pct, type TrainingRun } from "@/lib/api";

export default function TrainingList() {
  const router = useRouter();
  const [runs, setRuns] = useState<TrainingRun[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const load = () => getTrainingRuns().then((r) => { setRuns(r); setLoaded(true); }).catch(() => setLoaded(true));
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="page wide">
      <div className="between" style={{ marginBottom: 18 }}>
        <div>
          <div className="eyebrow">continuous evaluation</div>
          <h1 className="title" style={{ marginTop: 4 }}>Training runs</h1>
        </div>
      </div>

      {loaded && runs.length === 0 && (
        <div className="panel"><div className="panel-b"><Empty icon="spark">
          No training runs registered. Register one via <span className="mono">POST /training</span> (the
          monitor then polls its checkpoint stream and evaluates the suite per checkpoint).
        </Empty></div></div>
      )}
      {!loaded && <Empty icon="spark"><span className="spin" /> loading…</Empty>}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(420px, 1fr))", gap: 16 }}>
        {runs.map((r) => {
          const pctDone = r.planned_steps ? r.current_step / r.planned_steps : 0;
          const suite = r.body?.suite || [];
          return (
            <div key={r.id} className="panel click" style={{ cursor: "pointer" }} onClick={() => router.push(`/training/${r.id}`)}>
              <div className="panel-b">
                <div className="between" style={{ marginBottom: 12 }}>
                  <div className="vcenter gap10">
                    <span className="pi" style={{ width: 30, height: 30, borderRadius: 7, background: "linear-gradient(135deg,#a371f7,#2f81f7)", display: "grid", placeItems: "center", color: "#fff", fontWeight: 700, fontSize: 12, fontFamily: "var(--mono)" }}>{r.body?.glyph || r.model.slice(0, 2).toUpperCase()}</span>
                    <div>
                      <div className="vcenter gap8"><span className="mono" style={{ fontSize: 15, fontWeight: 600 }}>{r.model}</span><StatusPill status={r.status} /></div>
                      <div className="subtle mono" style={{ fontSize: 11, marginTop: 2 }}>{r.id} · base {r.base || "—"}</div>
                    </div>
                  </div>
                </div>
                <div className="between" style={{ marginBottom: 5 }}>
                  <span className="eyebrow">progress</span>
                  <span className="mono subtle" style={{ fontSize: 11 }}>{r.current_step.toLocaleString()} / {(r.planned_steps || 0).toLocaleString()} · {pct(pctDone)}%</span>
                </div>
                <div className="prog"><span className="s-run" style={{ width: pctDone * 100 + "%", background: r.status === "completed" ? "var(--success)" : "var(--accent-emph)" }} /></div>
                <div className="vcenter gap16 wrap" style={{ marginTop: 12, fontSize: 11.5 }}>
                  <span className="subtle vcenter gap6"><Icon name="flask" size={12} />{suite.length} evals</span>
                  <span className="subtle vcenter gap6"><Icon name="cpu" size={12} />{r.body?.hardware || "—"}</span>
                  <span className="subtle vcenter gap6"><Icon name="clock" size={12} />{ago(r.created_at)}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
