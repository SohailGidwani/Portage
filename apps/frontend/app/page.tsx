"use client";

// Jobs overview — submit a migration, watch the fleet.
// Stats are computed from the jobs list itself; nothing here invents numbers.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  EvalRun,
  Job,
  TestSummary,
  createJob,
  getHealth,
  listEvalRuns,
  listJobs,
} from "./lib/api";

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
  const [evalRuns, setEvalRuns] = useState<EvalRun[]>([]);
  const [repoUrl, setRepoUrl] = useState("/fixtures/flask_app");
  const [repoRef, setRepoRef] = useState("");
  const [recipe, setRecipe] = useState("flask_to_fastapi");
  const [filter, setFilter] = useState<"all" | "running" | "done" | "failed">("all");
  const [visible, setVisible] = useState(10);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  const refresh = useCallback(async () => {
    try {
      const [h, j, ev] = await Promise.all([getHealth(), listJobs(), listEvalRuns()]);
      setHealth({ api: h.status === "ok", db: h.db === "ok" });
      setJobs(j);
      setEvalRuns(ev);
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
      const config: Record<string, unknown> = {};
      if (repoRef.trim()) config.repo_ref = repoRef.trim();
      await createJob({ repo_url: repoUrl, migration_recipe: recipe, config });
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
  const greenEval = evalRuns.filter((r) => r.status === "green" && r.cost_usd > 0);
  const avgCost = greenEval.length
    ? greenEval.reduce((s, r) => s + r.cost_usd, 0) / greenEval.length
    : null;
  const matched = filter === "all" ? jobs : jobs.filter((j) => j.status === filter);
  const shown = matched.slice(0, visible);
  const shortRepo = (u: string) =>
    u.replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/^\/fixtures\//, "fixture: ");

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
          <Link href="/eval">eval proof</Link>
          {" · "}
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

      <form onSubmit={submit} className="panel launch">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ flex: 2, minWidth: 280 }}>
            <span className="field-label">repository (path or git URL)</span>
            <input
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder="https://github.com/owner/repo"
            />
          </label>
          <label className="field" style={{ flex: 1, minWidth: 160 }}>
            <span className="field-label">ref — pinned SHA (optional)</span>
            <input
              value={repoRef}
              onChange={(e) => setRepoRef(e.target.value)}
              placeholder="default branch"
            />
          </label>
          <label className="field" style={{ minWidth: 190 }}>
            <span className="field-label">recipe</span>
            <select value={recipe} onChange={(e) => setRecipe(e.target.value)}>
              <option value="flask_to_fastapi">flask → fastapi</option>
              <option value="pydantic_v1_to_v2">pydantic v1 → v2 (verify only)</option>
            </select>
          </label>
          <button type="submit" disabled={busy || !repoUrl.trim()}>
            {busy ? "Starting…" : "Start migration"}
          </button>
        </div>
        <div style={{ marginTop: 10 }}>
          <span className="muted" style={{ fontSize: 12, marginRight: 8 }}>
            quick fill:
          </span>
          <button
            type="button"
            className="chip chip-btn"
            onClick={() => {
              setRepoUrl("/fixtures/flask_app");
              setRepoRef("");
              setRecipe("flask_to_fastapi");
            }}
          >
            bundled fixture
          </button>
          <button
            type="button"
            className="chip chip-btn"
            onClick={() => {
              setRepoUrl("https://github.com/markdouthwaite/minimal-flask-api");
              setRepoRef("91ae6abe493bef44fb21e4b9c34e8e94d9d2eae9");
              setRecipe("flask_to_fastapi");
            }}
          >
            corpus: minimal-flask-api
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
        <div className="stat">
          <div className="stat-label">avg cost / green migration</div>
          <div className="stat-value">
            {avgCost !== null ? (
              `$${avgCost.toFixed(3)}`
            ) : (
              <span className="muted">—</span>
            )}
          </div>
        </div>
      </div>

      <div className="dash-grid">
      <section>
      <div className="row" style={{ justifyContent: "space-between", marginTop: 24 }}>
        <h2 className="eyebrow" style={{ margin: 0 }}>
          Migration jobs{" "}
          <span className="muted" style={{ letterSpacing: 0 }}>
            · {Math.min(visible, matched.length)} of {matched.length}
          </span>
        </h2>
        <div>
          {(["all", "running", "done", "failed"] as const).map((f) => (
            <button
              key={f}
              type="button"
              className={`chip chip-btn${filter === f ? " chip-active" : ""}`}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      <div className="panel tablewrap" style={{ marginTop: 10 }}>
        <table>
          <thead>
            <tr>
              <th>job</th>
              <th>repository</th>
              <th>recipe</th>
              <th>status</th>
              <th>tests</th>
              <th>graph</th>
              <th>updated</th>
            </tr>
          </thead>
          <tbody>
            {shown.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  {jobs.length === 0
                    ? "No jobs yet. Start one above — the bundled fixture is a one-click quick fill."
                    : `No ${filter} jobs.`}
                </td>
              </tr>
            )}
            {shown.map((j) => (
              <tr
                key={j.id}
                className="rowlink"
                onClick={() => router.push(`/jobs/${j.id}`)}
              >
                <td className="mono">
                  <Link href={`/jobs/${j.id}`} onClick={(e) => e.stopPropagation()}>
                    {j.id.slice(0, 8)}
                  </Link>
                </td>
                <td className="mono muted" title={j.repo_url}>
                  {shortRepo(j.repo_url)}
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
        {matched.length > visible && (
          <div style={{ textAlign: "center", paddingTop: 10 }}>
            <button
              type="button"
              className="chip chip-btn"
              onClick={() => setVisible((v) => v + 20)}
            >
              show {Math.min(20, matched.length - visible)} more
            </button>
          </div>
        )}
      </div>
      </section>

      <aside>
        <h2 className="eyebrow">Eval runs (harness)</h2>
        <div className="panel tablewrap">
          <table>
            <thead>
              <tr>
                <th>repo</th>
                <th>scenario</th>
                <th>result</th>
                <th>cost</th>
              </tr>
            </thead>
            <tbody>
              {evalRuns.length === 0 && (
                <tr>
                  <td colSpan={4} className="muted">
                    No eval runs yet — see <code>scripts/phase4_smoke.sh</code>.
                  </td>
                </tr>
              )}
              {evalRuns.slice(0, 12).map((r) => (
                <tr key={r.id}>
                  <td className="mono">
                    {r.job_id ? (
                      <Link href={`/jobs/${r.job_id}`}>{r.corpus_name}</Link>
                    ) : (
                      r.corpus_name
                    )}
                  </td>
                  <td className="mono muted">{r.scenario}</td>
                  <td>
                    <span
                      className={`status s-${
                        r.status === "green" ? "done" : "failed"
                      }`}
                    >
                      {r.status === "green"
                        ? `${r.tests_passed}/${r.tests_total}`
                        : r.status}
                    </span>
                  </td>
                  <td className="mono muted">${r.cost_usd.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </aside>
      </div>
    </main>
  );
}
