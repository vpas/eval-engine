"use client";
import { useEffect, useState } from "react";
import { getMe } from "@/lib/api";

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`pill ${status}`}>
      <span className="led" />
      {status}
    </span>
  );
}

export function AccuracyBar({ value }: { value: number | null }) {
  if (value == null) return <span className="dim mono">—</span>;
  const pct = Math.round(value * 100);
  return (
    <span className="acc">
      <span className="track"><span className="fill" style={{ width: `${pct}%` }} /></span>
      <span>{pct}%</span>
    </span>
  );
}

export function UserChip() {
  const [email, setEmail] = useState<string | null>(null);
  useEffect(() => {
    getMe().then((m) => setEmail(m.email)).catch(() => {});
  }, []);
  if (!email) return <span className="user dim">unauthenticated</span>;
  const initial = email[0]?.toUpperCase() ?? "?";
  return (
    <span className="user">
      <span className="av">{initial}</span>
      {email}
    </span>
  );
}

// time-ago, mono and compact
export function ago(iso: string): string {
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return iso;
  const s = Math.max(0, (Date.now() - d) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fmtCost(c: number): string {
  if (!c) return "$0";
  if (c < 0.01) return `$${c.toExponential(2)}`;
  return `$${c.toFixed(4)}`;
}
