"use client";

// Jobs overview — submit a migration, watch the fleet.
// Stats are computed from the jobs list itself; nothing here invents numbers.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Job, TestSummary, createJob, getHealth, listJobs } from "./lib/api";

function relTime(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

function TestsMeter({ s }: { s: TestSummary | null }) {
  if (!s || !s.total) return <span className="muted">—</span>;
  const ok = s.failed + s.errors === 0;
  const pct = Math.round((s.passed / s.total) * 100);
  return (
    <span className="meter">
      {s.passed}/{s.total}
      <span className="meter-bar">
        <span
          className={`meter-fill${ok ? "" : " bad"}`}
          style={{ width: `${pct}%` }}
        />
      </span>
    </span>
  );
}

export default function Home() {
  const [health, setHealth] = useState<{ api: boolean; db: boolean } | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [repoUrl, setRepoUrl] = useState("/fixtures/flask_app");
  const [recipe, setRecipe] = useState("flask_to_fastapi");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, j] = await Promise.all([getHealth(), listJobs()]);
      setHealth({ api: h.status === "ok", db: h.db === "ok" });
      setJobs(j);
      setError(null);
    } catch (e) {
      setHealth(null);
      setError(String(e));
    }
  }, []);

  // Poll every 2s so a running job (and the kill/resume demo) updates live.
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [refresh]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await createJob({ repo_url: repoUrl, migration_recipe: recipe });
      await refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  const green = jobs.filter(
    (j) =>
      j.status === "done" &&
      j.test_summary &&
      j.test_summary.total > 0 &&
      j.test_summary.passed === j.test_summary.total
  ).length;
  const running = jobs.filter((j) => j.status === "running").length;
  const failed = jobs.filter((j) => j.status === "failed").length;

  return (
    <main>
      <header className="masthead">
        <h1 className="wordmark">
          PORTAGE
          <span className="tagline">
            carries a codebase between frameworks — plans, migrates, verifies,
            recovers
          </span>
        </h1>
        <span className="syscheck">
          {health ? (
            <>
              api <span className="ok">ok</span> · db{" "}
              <span className={health.db ? "ok" : "bad"}>
                {health.db ? "ok" : "down"}
              </span>
            </>
          ) : (
            <span className="bad">api unreachable</span>
          )}
        </span>
      </header>

      <form onSubmit={submit} className="panel">
        <div className="row">
          <input
            style={{ flex: 1, minWidth: 260 }}
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="/fixtures/flask_app or a git URL"
            aria-label="repository"
          />
          <select
            value={recipe}
            onChange={(e) => setRecipe(e.target.value)}
            aria-label="migration recipe"
          >
            <option value="flask_to_fastapi">flask → fastapi</option>
            <option value="pydantic_v1_to_v2">pydantic v1 → v2 (verify only)</option>
          </select>
          <button type="submit" disabled={busy}>
            {busy ? "Starting…" : "Start migration"}
          </button>
        </div>
      </form>

      {error && <p className="errband">{error}</p>}

      <div className="statgrid">
        <div className="stat">
          <div className="stat-label">runs</div>
          <div className="stat-value">{jobs.length}</div>
        </div>
        <div className="stat">
          <div className="stat-label">running</div>
          <div className="stat-value">{running}</div>
        </div>
        <div className="stat">
          <div className="stat-label">suites green</div>
          <div className="stat-value">{green}</div>
        </div>
        <div className="stat">
          <div className="stat-label">failed</div>
          <div className="stat-value">{failed}</div>
        </div>
      </div>

      <h2 className="eyebrow">Runs</h2>
      <div className="panel tablewrap">
        <table>
          <thead>
            <tr>
              <th>job</th>
              <th>recipe</th>
              <th>status</th>
              <th>tests</th>
              <th>graph</th>
              <th>updated</th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No runs yet. Start one above — the bundled fixture is{" "}
                  <code>/fixtures/flask_app</code>.
                </td>
              </tr>
            )}
            {jobs.map((j) => (
              <tr key={j.id}>
                <td className="mono">
                  <Link href={`/jobs/${j.id}`}>{j.id.slice(0, 8)}</Link>
                </td>
                <td className="mono muted">{j.migration_recipe}</td>
                <td>
                  <span className={`status s-${j.status}`}>{j.status}</span>
                </td>
                <td>
                  <TestsMeter s={j.test_summary} />
                </td>
                <td className="mono muted">
                  {j.graph_summary
                    ? `${j.graph_summary.total_nodes}n · ${j.graph_summary.total_edges}e`
                    : "—"}
                </td>
                <td className="muted">{relTime(j.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
