"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { AppShell } from "./components/AppShell";
import { Metric, Progress, StatusPill, relativeTime, shortRepo } from "./components/ui";
import {
  EvalRun,
  Job,
  Me,
  Report,
  createJob,
  getHealth,
  getJobReport,
  getMe,
  listEvalRuns,
  listJobs,
  loginUrl,
  logout,
  tryRefresh,
} from "./lib/api";

type Filter = "all" | "active" | "success" | "attention";
const JOB_PAGE_SIZE = 20;

function outcomeFor(job: Job, report?: Report | null): string {
  if (job.status === "queued" || job.status === "running") return job.status;
  if (job.status === "failed") return "failed";
  return report?.migration_outcome ?? "done";
}

function testRate(job: Job): number | null {
  const tests = job.test_summary;
  return tests?.total ? tests.passed / tests.total : null;
}

export default function Home() {
  const router = useRouter();
  const [health, setHealth] = useState<{ api: boolean; db: boolean } | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [totalJobs, setTotalJobs] = useState(0);
  const [jobOffset, setJobOffset] = useState(0);
  const [reports, setReports] = useState<Record<string, Report | null>>({});
  const reportCache = useRef<Record<string, Report | null>>({});
  const [evalRuns, setEvalRuns] = useState<EvalRun[]>([]);
  const [me, setMe] = useState<Me | null>(null);
  const [repoUrl, setRepoUrl] = useState("/fixtures/flask_app");
  const [repoRef, setRepoRef] = useState("");
  const [recipe, setRecipe] = useState("flask_to_fastapi");
  const [filter, setFilter] = useState<Filter>("all");
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [authRequired, setAuthRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, runs] = await Promise.all([getHealth(), listEvalRuns()]);
      setHealth({ api: h.status === "ok", db: h.db === "ok" });
      setEvalRuns(runs);
      let nextJobs: Job[];
      try {
        const page = await listJobs(JOB_PAGE_SIZE, jobOffset);
        nextJobs = page.items;
        setTotalJobs(page.total);
        setAuthRequired(false);
      } catch (jobError) {
        if (String(jobError).includes("401")) {
          setAuthRequired(true);
          setJobs([]);
          setTotalJobs(0);
          setReports({});
          reportCache.current = {};
          setError(null);
          return;
        }
        throw jobError;
      }
      setJobs(nextJobs);
      const finished = nextJobs
        .filter((job) => job.report_path && !(job.id in reportCache.current))
        .slice(0, 24);
      const pairs = await Promise.all(
        finished.map(async (job) => [job.id, await getJobReport(job.id)] as const)
      );
      reportCache.current = { ...reportCache.current, ...Object.fromEntries(pairs) };
      setReports(reportCache.current);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }, [jobOffset]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    (async () => {
      await tryRefresh();
      setMe(await getMe());
    })();
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const config: Record<string, unknown> = {};
      if (repoRef.trim()) config.repo_ref = repoRef.trim();
      const job = await createJob({ repo_url: repoUrl.trim(), migration_recipe: recipe, config });
      router.push(`/jobs/${job.id}`);
    } catch (err) {
      setError(String(err));
      setBusy(false);
    }
  }

  const active = jobs.filter((job) => ["queued", "running"].includes(job.status)).length;
  const successful = jobs.filter((job) => reports[job.id]?.migration_outcome === "success").length;
  const attention = jobs.filter((job) => ["failed", "unsupported"].includes(outcomeFor(job, reports[job.id]))).length;
  const greenEval = evalRuns.filter((run) => run.status === "green" && run.cost_usd > 0);
  const avgCost = greenEval.length ? greenEval.reduce((sum, run) => sum + run.cost_usd, 0) / greenEval.length : null;
  const visibleJobs = jobs.filter((job) => {
    const outcome = outcomeFor(job, reports[job.id]);
    if (filter === "active") return ["queued", "running"].includes(outcome);
    if (filter === "success") return outcome === "success";
    if (filter === "attention") return ["failed", "unsupported"].includes(outcome);
    return true;
  });
  const pageStart = totalJobs ? jobOffset + 1 : 0;
  const pageEnd = Math.min(jobOffset + jobs.length, totalJobs);
  const pageDescription = filter === "all"
    ? `Showing ${pageStart}–${pageEnd} of ${totalJobs}`
    : `${visibleJobs.length} match this filter on runs ${pageStart}–${pageEnd} of ${totalJobs}`;

  const account = me?.auth_mode === "github" ? (
    <button className="button ghost" onClick={async () => { await logout(); setMe(null); setAuthRequired(true); }}>
      @{me.login} · sign out
    </button>
  ) : me === null ? <a className="button secondary" href={loginUrl()}>Sign in with GitHub</a> : null;

  return (
    <AppShell
      eyebrow="Control center"
      title="Migration runs"
      description="Launch, inspect, and review framework migrations from one workspace."
      actions={<>{account}<span className={`system-badge ${health?.api && health?.db ? "healthy" : "unhealthy"}`}><span />{health?.api && health?.db ? "System ready" : "System unavailable"}</span></>}
    >
      {error && <div className="notice error">{error}</div>}
      {authRequired && <div className="notice auth-notice">Sign in with GitHub to launch migrations and view your runs. Evaluation evidence and the review guide remain available without a session.</div>}

      <section className="launch-card">
        <div className="launch-intro">
          <span className="kicker">New migration</span>
          <h2>What should Portage migrate?</h2>
          <p>Use a local worker-visible path or a Git repository. Pin a SHA for reproducible results.</p>
        </div>
        <form onSubmit={submit} className="launch-form">
          <label className="field field-wide">
            <span>Repository</span>
            <input value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} placeholder="https://github.com/owner/repo" />
          </label>
          <label className="field">
            <span>Recipe</span>
            <select value={recipe} onChange={(e) => setRecipe(e.target.value)}>
              <option value="flask_to_fastapi">Flask → FastAPI</option>
              <option value="pydantic_v1_to_v2">Pydantic v1 → v2 · verify</option>
            </select>
          </label>
          {advanced && <label className="field"><span>Git ref</span><input value={repoRef} onChange={(e) => setRepoRef(e.target.value)} placeholder="Commit SHA or tag" /></label>}
          <div className="launch-actions">
            <button type="button" className="button ghost" onClick={() => setAdvanced(!advanced)}>{advanced ? "Hide options" : "Add git ref"}</button>
            <button type="submit" className="button primary" disabled={busy || !repoUrl.trim() || authRequired}>{busy ? "Starting…" : authRequired ? "Sign in to migrate" : "Start migration"}</button>
          </div>
        </form>
        <div className="quick-fill">
          <span>Try with</span>
          <button type="button" onClick={() => { setRepoUrl("/fixtures/flask_app"); setRepoRef(""); }}>Bundled fixture</button>
          <button type="button" onClick={() => { setRepoUrl("https://github.com/markdouthwaite/minimal-flask-api"); setRepoRef("91ae6abe493bef44fb21e4b9c34e8e94d9d2eae9"); setAdvanced(true); }}>Pinned corpus repo</button>
        </div>
      </section>

      <section className="metrics-grid">
        <Metric label="All runs" value={totalJobs} detail="Visible to this account across every page" />
        <Metric label="Active now" value={active} detail={active ? "On this page · updates every 4 seconds" : "None on this page"} tone={active ? "warn" : undefined} />
        <Metric label="Successful migrations" value={successful} detail="On this page · complete + full suite green" tone="good" />
        <Metric label="Needs attention" value={attention} detail="On this page · failed or unsupported" tone={attention ? "bad" : undefined} />
        <Metric label="Mean eval cost" value={avgCost === null ? "—" : `$${avgCost.toFixed(3)}`} detail="Green harness runs only" />
      </section>

      <div className="dashboard-grid">
        <section className="surface runs-surface">
          <div className="section-heading">
            <div><span className="kicker">Workspace</span><h2>Recent runs</h2></div>
            <div className="segmented" role="group" aria-label="Filter runs">
              {(["all", "active", "success", "attention"] as Filter[]).map((value) => (
                <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{value}</button>
              ))}
            </div>
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Run</th><th>Repository</th><th>Outcome</th><th>Tests</th><th>Progress</th><th>Updated</th></tr></thead>
              <tbody>
                {visibleJobs.map((job) => {
                  const outcome = outcomeFor(job, reports[job.id]);
                  const rate = testRate(job);
                  const report = reports[job.id];
                  const completion = report?.tasks_total ? report.tasks_done / report.tasks_total : job.status === "done" ? 1 : 0;
                  return (
                    <tr key={job.id} onClick={() => router.push(`/jobs/${job.id}`)}>
                      <td><Link href={`/jobs/${job.id}`} className="run-id" onClick={(e) => e.stopPropagation()}>{job.id.slice(0, 8)}</Link><small>{job.migration_recipe.replaceAll("_", " ")}</small></td>
                      <td><strong title={job.repo_url}>{shortRepo(job.repo_url)}</strong></td>
                      <td><StatusPill status={outcome} /></td>
                      <td className="mono">{job.test_summary?.total ? `${job.test_summary.passed}/${job.test_summary.total}` : "—"}</td>
                      <td><div className="progress-cell"><Progress value={completion} tone={outcome === "failed" ? "bad" : "good"} /><span>{rate === null ? "Planning" : `${Math.round(rate * 100)}% tests`}</span></div></td>
                      <td className="muted">{relativeTime(job.updated_at)}</td>
                    </tr>
                  );
                })}
                {!visibleJobs.length && <tr><td colSpan={6}><div className="empty-state">No runs match this filter.</div></td></tr>}
              </tbody>
            </table>
          </div>
          <div className="pagination-bar" aria-label="Run history pagination">
            <span>{pageDescription}</span>
            <div>
              <button
                className="button ghost"
                disabled={jobOffset === 0}
                onClick={() => setJobOffset(Math.max(0, jobOffset - JOB_PAGE_SIZE))}
              >Previous</button>
              <button
                className="button ghost"
                disabled={jobOffset + jobs.length >= totalJobs}
                onClick={() => setJobOffset(jobOffset + JOB_PAGE_SIZE)}
              >Next</button>
            </div>
          </div>
        </section>

        <aside className="dashboard-side">
          <section className="surface getting-started">
            <span className="kicker">After a run</span>
            <h2>Review the diff in your IDE</h2>
            <p>Export the patch, check it against a clean checkout, then apply it on a disposable branch.</p>
            <div className="mini-command"><code>portage diff &lt;job-id&gt; --output migration.patch</code></div>
            <Link className="text-link" href="/guide">Open the review guide →</Link>
          </section>
          <section className="surface eval-snapshot">
            <div className="section-heading"><div><span className="kicker">Evidence</span><h2>Latest evaluations</h2></div><Link href="/eval">View all</Link></div>
            {evalRuns.slice(0, 5).map((run) => (
              <div className="snapshot-row" key={run.id}>
                <div><strong>{run.corpus_name}</strong><small>{run.scenario.replaceAll("_", " ")} · K{run.k_index}</small></div>
                <StatusPill status={run.status === "green" ? "success" : run.status} label={run.status} />
              </div>
            ))}
            {!evalRuns.length && <div className="empty-state small">No evaluation runs recorded.</div>}
          </section>
        </aside>
      </div>
    </AppShell>
  );
}
