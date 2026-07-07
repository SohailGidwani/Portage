"use client";

// Job detail — the "what migrated, what changed, how" view.
// The hero is the route: the agent's actual state machine drawn as a waypoint trail
// between the two waters (source framework → target framework). Every light on it is
// derived from real state (job row, task rows, report) — nothing is decorative.

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  AttemptEntry,
  Job,
  Report,
  Task,
  TestSummary,
  getJob,
  getJobReport,
  getJobTasks,
} from "../../lib/api";

type WpState = "done" | "active" | "pending" | "off" | "detour";
type Waypoint = { name: string; state: WpState; diamond?: boolean };

// Derive each pipeline stage's state from what the run has actually produced.
// While running: tasks appear when Plan persists them; attempts_log streams during
// Execute/Recover; the report exists only once the run finishes.
function deriveRoute(job: Job | null, tasks: Task[], report: Report | null): Waypoint[] {
  const fileTasks = tasks.filter((t) => t.target_path);
  const planned = fileTasks.length > 0;
  const allSettled =
    planned && fileTasks.every((t) => t.status === "done" || t.status === "skipped");
  const recovered =
    (report?.recovery?.visits ?? 0) > 0 ||
    tasks.some((t) =>
      t.attempts_log.some((a) => (a.action ?? "").startsWith("rollback"))
    );
  const finished = job?.status === "done" || job?.status === "failed";
  const running = job?.status === "running";

  const st = (done: boolean, active: boolean): WpState =>
    done ? "done" : active ? "active" : "pending";

  return [
    { name: "ingest", state: st(planned || finished, running && !planned) },
    { name: "plan", state: st(planned || finished, false) },
    { name: "execute", state: st(allSettled || finished, running && planned && !allSettled) },
    { name: "verify", state: st(finished, running && allSettled) },
    {
      name: "recover",
      state: recovered ? "detour" : "off",
      diamond: true,
    },
    { name: "integrate", state: st(finished, false) },
    { name: "report", state: st(finished, false) },
  ];
}

function Route({ job, tasks, report }: { job: Job | null; tasks: Task[]; report: Report | null }) {
  const migrating = job?.migration_recipe === "flask_to_fastapi";
  const src = migrating ? "flask" : "repo";
  const dst = migrating ? "fastapi" : "report";
  return (
    <div className="panel route" aria-label="migration pipeline">
      <span className="water water-src">{src}</span>
      <ol className="waypoints">
        {deriveRoute(job, tasks, report).map((w) => (
          <li key={w.name} className={`wp is-${w.state}${w.diamond ? " wp-diamond" : ""}`}>
            <span className="wp-dot" />
            <span className="wp-name">{w.name}</span>
          </li>
        ))}
      </ol>
      <span className="water water-dst">{dst}</span>
    </div>
  );
}

function DiffBlock({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="muted">No changes.</p>;
  return (
    <pre className="diff">
      {diff.split("\n").map((line, i) => {
        let cls = "dl-ctx";
        if (line.startsWith("diff --git")) cls = "dl-file";
        else if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("index "))
          cls = "dl-meta";
        else if (line.startsWith("@@")) cls = "dl-hunk";
        else if (line.startsWith("+")) cls = "dl-add";
        else if (line.startsWith("-")) cls = "dl-del";
        return (
          <span key={i} className={cls}>
            {line || " "}
          </span>
        );
      })}
    </pre>
  );
}

function TestsCell({ s }: { s: TestSummary | null | undefined }) {
  if (!s || !s.total) return <span className="muted">—</span>;
  const ok = s.failed + s.errors === 0;
  return (
    <span className={`mono ${ok ? "s-done" : "s-failed"}`} style={{ fontWeight: 650 }}>
      {s.passed}/{s.total}
    </span>
  );
}

function AttemptLine({ a }: { a: AttemptEntry }) {
  const when = a.at ? new Date(a.at).toLocaleTimeString() : "";
  if (a.action === "migrate") {
    const esc = a.tier === "escalation";
    return (
      <li className={esc ? "t-escalation" : ""}>
        <span className="tag">{esc ? "escalation" : "driver"}</span>
        attempt {a.attempt} · {a.model} <span className="muted">{when}</span>
      </li>
    );
  }
  return (
    <li className="t-recover">
      <span className="tag">{(a.action ?? "recover").replaceAll("_", " ")}</span>
      {a.reason ? `${a.reason} failure` : ""} · visit {a.visit}{" "}
      <span className="muted">{when}</span>
    </li>
  );
}

export default function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<Job | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!id) return;
    try {
      const [j, t] = await Promise.all([getJob(id), getJobTasks(id)]);
      setJob(j);
      setTasks(t);
      if (j.report_path) setReport(await getJobReport(id));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [id]);

  // Poll while the job is live so Execute/Recover progress streams in.
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [refresh]);

  const recovery = report?.recovery;
  const fileTasks = tasks.filter((t) => t.target_path);
  const done = fileTasks.filter((t) => t.status === "done").length;

  return (
    <main>
      <header className="masthead">
        <h1 className="wordmark">
          <Link href="/">PORTAGE</Link>
          <span className="tagline mono">
            run {id?.slice(0, 8)} · {job?.migration_recipe ?? "…"} ·{" "}
            {job?.repo_url ?? ""}
          </span>
        </h1>
        {job && <span className={`status s-${job.status}`}>{job.status}</span>}
      </header>

      {error && <p className="errband">{error}</p>}
      {job?.error && <p className="errband">{job.error}</p>}

      <Route job={job} tasks={tasks} report={report} />

      <div className="detail-grid">
      <section>
      <h2 className="eyebrow" style={{ marginTop: 0 }}>
        Migrated files
      </h2>
      {fileTasks.length === 0 && (
        <div className="panel muted">
          No migration tasks — the recipe didn&apos;t apply, so the run verified the repo
          as-is.
        </div>
      )}
      {fileTasks.map((t) => (
        <div className="taskcard" key={t.id}>
          <div className="taskhead">
            <span className="taskpath">
              {t.target_path}
              <span className="rolelabel">{t.type}</span>
            </span>
            <span className="row" style={{ gap: 14 }}>
              <span className="muted mono" style={{ fontSize: 12 }}>
                {t.attempts} attempt{t.attempts === 1 ? "" : "s"}
              </span>
              <span className={`status s-${t.status}`}>{t.status}</span>
            </span>
          </div>
          <div className="chips">
            {t.subtasks.map((s) => (
              <span className="chip" key={s.id} title={s.title}>
                {s.type.replaceAll("_", " ")}
              </span>
            ))}
          </div>
          {t.error && <p className="errband">{t.error}</p>}
          {t.attempts_log.length > 0 && (
            <details>
              <summary>attempt log · {t.attempts_log.length}</summary>
              <ul className="timeline">
                {t.attempts_log.map((a, i) => (
                  <AttemptLine a={a} key={i} />
                ))}
              </ul>
            </details>
          )}
          {t.diff && (
            <details>
              <summary>diff</summary>
              <DiffBlock diff={t.diff} />
            </details>
          )}
        </div>
      ))}

      {report?.diff ? (
        <>
          <h2 className="eyebrow">Full migration diff</h2>
          <div className="panel" style={{ padding: "6px 10px 10px" }}>
            <DiffBlock diff={report.diff} />
          </div>
        </>
      ) : null}
      </section>

      <aside className="sidebar">
        <div className="stat">
          <div className="stat-label">full suite</div>
          <div className="stat-value">
            <TestsCell s={job?.test_summary} />
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">affected subset (verify)</div>
          <div className="stat-value">
            <TestsCell s={report?.verify_summary} />
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">files migrated</div>
          <div className="stat-value">
            {fileTasks.length ? (
              <>
                {done}/{fileTasks.length}
              </>
            ) : (
              <span className="muted">—</span>
            )}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">recovery</div>
          <div className="stat-value">
            {recovery && recovery.visits > 0 ? (
              <>
                {recovery.visits}{" "}
                <small>
                  visit{recovery.visits === 1 ? "" : "s"}
                  {recovery.escalation_attempted > 0 &&
                    ` · escalation ${recovery.escalation_rescued}/${recovery.escalation_attempted}`}
                  {recovery.tasks_skipped > 0 && ` · ${recovery.tasks_skipped} skipped`}
                </small>
              </>
            ) : (
              <span className="muted">{recovery ? "not needed" : "—"}</span>
            )}
          </div>
        </div>
        {report && report.affected_tests.length > 0 && (
          <div className="stat">
            <div className="stat-label">blast-radius tests</div>
            <div style={{ marginTop: 6 }}>
              {report.affected_tests.map((t) => (
                <div className="mono muted" style={{ fontSize: 12 }} key={t}>
                  {t}
                </div>
              ))}
            </div>
          </div>
        )}
      </aside>
      </div>
    </main>
  );
}
