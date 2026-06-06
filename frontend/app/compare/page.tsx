"use client";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Icon } from "@/components/icons";
import { Empty, Provider } from "@/components/ui";
import { getRun, getResults, fmtCost, fmtN, pct, type RunDetail, type Results } from "@/lib/api";

type Loaded = { id: string; run?: RunDetail; res?: Results; err?: string };

function CompareInner() {
  const sp = useSearchParams();
  const initial = (sp.get("ids") || "").split(",").map((s) => s.trim()).filter(Boolean);
  const [ids, setIds] = useState<string[]>(initial);
  const [data, setData] = useState<Record<string, Loaded>>({});
  const [add, setAdd] = useState("");

  useEffect(() => {
    ids.forEach((id) => {
      if (data[id]) return;
      setData((d) => ({ ...d, [id]: { id } }));
      Promise.all([getRun(id), getResults(id).catch(() => undefined)])
        .then(([run, res]) => setData((d) => ({ ...d, [id]: { id, run, res } })))
        .catch((e) => setData((d) => ({ ...d, [id]: { id, err: String(e?.message || e) } })));
    });
  }, [ids]); // eslint-disable-line react-hooks/exhaustive-deps

  const cols = ids.map((id) => data[id]).filter(Boolean) as Loaded[];

  // union of categories across runs, for an aligned by-category comparison
  const cats = useMemo(() => {
    const s = new Set<string>();
    cols.forEach((c) => c.res?.by_category.forEach((b) => s.add(b.category || "—")));
    return Array.from(s).sort();
  }, [cols]);

  const remove = (id: string) => setIds((x) => x.filter((y) => y !== id));

  return (
    <div className="page wide">
      <div className="between" style={{ marginBottom: 18 }}>
        <div>
          <div className="eyebrow">side-by-side</div>
          <h1 className="title" style={{ marginTop: 4 }}>Compare</h1>
        </div>
        <div className="fsearch" style={{ maxWidth: 320 }}>
          <Icon name="plus" className="ic" />
          <input placeholder="add run id and press enter" value={add}
            onChange={(e) => setAdd(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && add.trim()) { setIds((x) => Array.from(new Set([...x, add.trim()]))); setAdd(""); } }} />
        </div>
      </div>

      {cols.length === 0 && <div className="panel"><div className="panel-b"><Empty icon="compare">Select runs from the dashboard (checkboxes → Compare), or add run ids above.</Empty></div></div>}

      {cols.length > 0 && (
        <div className="panel flush" style={{ overflowX: "auto" }}>
          <table className="grid">
            <thead>
              <tr>
                <th style={{ minWidth: 140 }}>Metric</th>
                {cols.map((c) => (
                  <th key={c.id} className="right">
                    <div className="vcenter gap6" style={{ justifyContent: "flex-end" }}>
                      <span className="mono linklike">{c.id}</span>
                      <span className="x" style={{ cursor: "pointer" }} onClick={() => remove(c.id)}><Icon name="x" size={11} /></span>
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <Row label="model">{cols.map((c) => <td key={c.id} className="right">{c.run ? <Provider id={c.run.model} /> : "—"}</td>)}</Row>
              <Row label="eval">{cols.map((c) => <td key={c.id} className="right mono">{c.run?.eval_id ?? "—"}</td>)}</Row>
              <Row label="status">{cols.map((c) => <td key={c.id} className="right mono">{c.run?.status ?? (c.err ? "error" : "…")}</td>)}</Row>
              <Row label="accuracy" emph>{cols.map((c) => <Metric key={c.id} v={c.res ? pct(c.res.summary.accuracy) + "%" : "—"} best={isBest(cols, c, (x) => x.res?.summary.accuracy)} />)}</Row>
              <Row label="passed / n">{cols.map((c) => <td key={c.id} className="right num">{c.res ? `${fmtN(c.res.summary.passed)} / ${fmtN(c.res.summary.samples)}` : "—"}</td>)}</Row>
              <Row label="tokens">{cols.map((c) => <td key={c.id} className="right num">{c.res ? fmtN(c.res.summary.tokens) : "—"}</td>)}</Row>
              <Row label="cost">{cols.map((c) => <td key={c.id} className="right num">{c.res ? fmtCost(c.res.summary.cost_usd) : "—"}</td>)}</Row>
              {cats.length > 0 && (
                <tr><td colSpan={cols.length + 1} style={{ background: "var(--panel-2)" }}><span className="eyebrow">by category</span></td></tr>
              )}
              {cats.map((cat) => (
                <Row key={cat} label={cat}>
                  {cols.map((c) => {
                    const b = c.res?.by_category.find((x) => (x.category || "—") === cat);
                    return <Metric key={c.id} v={b ? pct(b.accuracy) + "%" : "—"} best={isBest(cols, c, (x) => x.res?.by_category.find((y) => (y.category || "—") === cat)?.accuracy)} />;
                  })}
                </Row>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Row({ label, children, emph }: { label: string; children: React.ReactNode; emph?: boolean }) {
  return (
    <tr>
      <td className="cellmuted" style={emph ? { fontWeight: 600, color: "var(--fg)" } : undefined}>{label}</td>
      {children}
    </tr>
  );
}

function Metric({ v, best }: { v: string; best: boolean }) {
  return <td className="right num" style={best ? { color: "var(--success)", fontWeight: 600 } : undefined}>{v}{best && v !== "—" ? " ★" : ""}</td>;
}

function isBest(cols: Loaded[], c: Loaded, get: (l: Loaded) => number | undefined): boolean {
  const vals = cols.map(get).filter((v): v is number => v != null);
  if (vals.length < 2) return false;
  const max = Math.max(...vals);
  const mine = get(c);
  return mine != null && mine === max;
}

export default function ComparePage() {
  return (
    <Suspense fallback={<div className="page"><Empty icon="compare"><span className="spin" /> loading…</Empty></div>}>
      <CompareInner />
    </Suspense>
  );
}
