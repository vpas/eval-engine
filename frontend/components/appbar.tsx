"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Icon } from "@/components/icons";
import { UserChip } from "@/components/ui";
import { LaunchDialog } from "@/components/launch";

export function AppBar() {
  const path = usePathname() || "/";
  const [launch, setLaunch] = useState(false);

  const on = (prefix: string) =>
    prefix === "/" ? path === "/" || path.startsWith("/runs") : path.startsWith(prefix);

  // close launch on Escape
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setLaunch(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // crumb for detail pages
  const runId = path.startsWith("/runs/") ? path.split("/")[2] : null;
  const trainId = path.startsWith("/training/") ? path.split("/")[2] : null;

  return (
    <>
      <header className="appbar">
        <Link href="/" className="brand">
          <svg className="glyph" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 11l3-3 2.5 2.5L13 5" />
            <circle cx="13" cy="5" r="1.4" fill="currentColor" stroke="none" />
            <path d="M2 14h12" />
          </svg>
          <span>eval<b>engine</b></span>
          <span className="ver">v1</span>
        </Link>
        <nav className="nav">
          <Link className={on("/") && !runId ? "on" : ""} href="/"><Icon name="grid" className="ic" />Dashboard</Link>
          <Link className={on("/training") && !trainId ? "on" : ""} href="/training"><Icon name="spark" className="ic" />Training</Link>
          <Link className={on("/compare") ? "on" : ""} href="/compare"><Icon name="compare" className="ic" />Compare</Link>
          {runId && (
            <span className="crumb">
              <Icon name="chevright" className="ic" />
              <span className="crumb-cur"><Icon name="pulse" className="ic" />{runId}</span>
            </span>
          )}
          {trainId && (
            <span className="crumb">
              <Icon name="chevright" className="ic" />
              <span className="crumb-cur"><Icon name="spark" className="ic" />{trainId}</span>
            </span>
          )}
        </nav>
        <span className="grow" />
        <div className="search">
          <Icon name="search" className="ic" />
          <input placeholder="Search runs, evals, models…" />
          <kbd>/</kbd>
        </div>
        <a href="/inspect/" target="_blank" className="btn sm ghost" rel="noreferrer"><Icon name="external" size={13} />traces</a>
        <button className="btn primary sm" onClick={() => setLaunch(true)}><Icon name="rocket" size={14} />New run</button>
        <UserChip />
      </header>
      {launch && <LaunchDialog onClose={() => setLaunch(false)} />}
    </>
  );
}
