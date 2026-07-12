"use client";

import { useState } from "react";

export function CopyButton({ value, label = "Copy", className = "button secondary" }: { value: string; label?: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className={className}
      onClick={async () => {
        await navigator.clipboard.writeText(value);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? "Copied" : label}
    </button>
  );
}

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const normalized = status.replaceAll("_", "-");
  return <span className={`status-pill status-${normalized}`}>{label ?? status.replaceAll("_", " ")}</span>;
}

export function Metric({ label, value, detail, tone }: { label: string; value: string | number; detail?: string; tone?: "good" | "warn" | "bad" }) {
  return (
    <div className={`metric-card${tone ? ` tone-${tone}` : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  );
}

export function Progress({ value, tone = "good" }: { value: number; tone?: "good" | "warn" | "bad" }) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <span className="progress-track" aria-label={`${pct}%`}>
      <span className={`progress-fill ${tone}`} style={{ width: `${pct}%` }} />
    </span>
  );
}

export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) return <div className="empty-state">No changes were produced.</div>;
  return (
    <pre className="diff-view">
      {diff.split("\n").map((line, index) => {
        let kind = "context";
        if (line.startsWith("diff --git")) kind = "file";
        else if (line.startsWith("@@")) kind = "hunk";
        else if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("index ")) kind = "meta";
        else if (line.startsWith("+")) kind = "add";
        else if (line.startsWith("-")) kind = "delete";
        return <span key={index} className={`diff-${kind}`}>{line || " "}</span>;
      })}
    </pre>
  );
}

export function relativeTime(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

export function shortRepo(url: string): string {
  return url.replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/^\/fixtures\//, "fixture: ");
}
