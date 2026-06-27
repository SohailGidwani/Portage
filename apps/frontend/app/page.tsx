"use client";

import { useCallback, useEffect, useState } from "react";
import { API_BASE, Job, createJob, getHealth, listJobs } from "./lib/api";

export default function Home() {
  const [health, setHealth] = useState<string>("…");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [repoUrl, setRepoUrl] = useState("https://github.com/acme/widgets");
  const [recipe, setRecipe] = useState("pydantic_v1_to_v2");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, j] = await Promise.all([getHealth(), listJobs()]);
      setHealth(`${h.status} (db: ${h.db})`);
      setJobs(j);
      setError(null);
    } catch (e) {
      setHealth("unreachable");
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

  return (
    <main>
      <h1 style={{ marginBottom: 4 }}>Portage</h1>
      <p className="muted" style={{ marginTop: 0 }}>
        Autonomous code-migration agent — Phase 0 skeleton. API:{" "}
        <code>{API_BASE}</code> · health: <code>{health}</code>
      </p>

      <form onSubmit={submit} className="panel" style={{ marginTop: 24 }}>
        <div className="row">
          <input
            style={{ flex: 1, minWidth: 280 }}
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="repo url"
          />
          <select value={recipe} onChange={(e) => setRecipe(e.target.value)}>
            <option value="pydantic_v1_to_v2">pydantic_v1_to_v2</option>
          </select>
          <button type="submit" disabled={busy}>
            {busy ? "Submitting…" : "Submit job"}
          </button>
        </div>
      </form>

      {error && (
        <p className="s-failed badge" style={{ marginTop: 16 }}>
          {error}
        </p>
      )}

      <h2 style={{ marginTop: 32 }}>Jobs</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>id</th>
              <th>recipe</th>
              <th>status</th>
              <th>updated</th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No jobs yet.
                </td>
              </tr>
            )}
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>
                  <code>{j.id.slice(0, 8)}</code>
                </td>
                <td>{j.migration_recipe}</td>
                <td>
                  <span className={`badge s-${j.status}`}>{j.status}</span>
                </td>
                <td className="muted">
                  {new Date(j.updated_at).toLocaleTimeString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
