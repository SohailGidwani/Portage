"use client";

// Job detail — the "what migrated, what changed, how" view.
// Task tree (file tasks + transformation subtasks), per-file diffs, the per-attempt
// tier/model timeline (the measured-escalation record), and the full migration diff.

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

function DiffBlock({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="muted">No changes.</p>;
  return (
    <pre className="diff">
      {diff.split("\n").map((line, i) => {
        let cls = "";
        if (line.startsWith("+++") || line.startsWith("---")) cls = "dl-meta";
        else if (line.startsWith("diff --git") || line.startsWith("index ")) cls = "dl-meta";
        else if (line.startsWith("@@")) cls = "dl-hunk";
        else if (line.startsWith("+")) cls = "dl-add";
        else if (line.startsWith("-")) cls = "dl-del";
        return (
          <span key={i} className={cls}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

function TestsBadge({ s }: { s: TestSummary | null | undefined }) {
  if (!s || !s.total) return <span className="muted">—</span>;
  const ok = s.failed + s.errors === 0;
  return (
    <span className={`badge ${ok ? "s-done" : "s-failed"}`}>
      {s.passed}/{s.total}
    </span>
  );
}

function AttemptLine({ a }: { a: AttemptEntry }) {
  const when = a.at ? new Date(a.at).toLocaleTimeString() : "";
  if (a.action === "migrate") {
    return (
      <li>
        <span className={`badge ${a.tier === "escalation" ? "s-running" : "s-queued"}`}>
          attempt {a.attempt} · {a.tier}
        </span>{" "}
        <code>{a.model}</code> <span className="muted">{when}</span>
      </li>
    );
  }
  return (
    <li>
      <span className="badge s-failed">{a.action}</span>{" "}
      <span className="muted">
        {a.reason ? `after ${a.reason} failure` : ""} · recover visit {a.visit} · {when}
      </span>
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

  return (
    <main>
      <p style={{ marginBottom: 8 }}>
        <Link href="/" className="muted">
          ← Jobs
        </Link>
      </p>
      <h1 style={{ marginBottom: 4 }}>
        Job <code>{id?.slice(0, 8)}</code>{" "}
        {job && <span className={`badge s-${job.status}`}>{job.status}</span>}
      </h1>
      {job && (
        <p className="muted" style={{ marginTop: 0 }}>
          <code>{job.migration_recipe}</code> on <code>{job.repo_url}</code> · updated{" "}
          {new Date(job.updated_at).toLocaleTimeString()}
        </p>
      )}
      {error && <p className="s-failed badge">{error}</p>}
      {job?.error && (
        <p className="s-failed badge" style={{ whiteSpace: "pre-wrap" }}>
          {job.error}
        </p>
      )}

      <div className="cards">
        <div className="panel card">
          <div className="muted">Full suite</div>
          <div className="big">
            <TestsBadge s={job?.test_summary} />
          </div>
        </div>
        <div className="panel card">
          <div className="muted">Affected subset (Verify)</div>
          <div className="big">
            <TestsBadge s={report?.verify_summary} />
          </div>
        </div>
        <div className="panel card">
          <div className="muted">Tasks</div>
          <div className="big">
            {fileTasks.filter((t) => t.status === "done").length}/{fileTasks.length}{" "}
            <span className="muted" style={{ fontSize: 13 }}>
              done
            </span>
          </div>
        </div>
        <div className="panel card">
          <div className="muted">Recovery</div>
          <div className="big">
            {recovery ? (
              <>
                {recovery.visits}{" "}
                <span className="muted" style={{ fontSize: 13 }}>
                  visit{recovery.visits === 1 ? "" : "s"}
                  {recovery.escalation_attempted > 0 &&
                    ` · escalation rescued ${recovery.escalation_rescued}/${recovery.escalation_attempted}`}
                  {recovery.tasks_skipped > 0 && ` · ${recovery.tasks_skipped} skipped`}
                </span>
              </>
            ) : (
              <span className="muted">—</span>
            )}
          </div>
        </div>
      </div>

      <h2 style={{ marginTop: 32 }}>Migration tasks</h2>
      {fileTasks.length === 0 && (
        <div className="panel muted">
          No migration tasks — this recipe didn&apos;t apply to the repo (verify-only run).
        </div>
      )}
      {fileTasks.map((t) => (
        <div className="panel" style={{ marginBottom: 12 }} key={t.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <code>{t.target_path}</code>{" "}
              <span className="badge s-queued">{t.type}</span>{" "}
              <span className={`badge s-${t.status === "skipped" ? "failed" : t.status}`}>
                {t.status}
              </span>
            </div>
            <div className="muted">
              {t.attempts} attempt{t.attempts === 1 ? "" : "s"}
            </div>
          </div>
          <div style={{ marginTop: 8 }}>
            {t.subtasks.map((s) => (
              <span className="chip" key={s.id} title={s.title}>
                {s.type}
              </span>
            ))}
          </div>
          {t.error && (
            <p className="s-failed badge" style={{ marginTop: 8 }}>
              {t.error}
            </p>
          )}
          {t.attempts_log.length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary className="muted">
                Attempt timeline ({t.attempts_log.length} entries)
              </summary>
              <ul className="timeline">
                {t.attempts_log.map((a, i) => (
                  <AttemptLine a={a} key={i} />
                ))}
              </ul>
            </details>
          )}
          {t.diff && (
            <details style={{ marginTop: 8 }}>
              <summary className="muted">What changed (diff)</summary>
              <DiffBlock diff={t.diff} />
            </details>
          )}
        </div>
      ))}

      {report?.diff ? (
        <>
          <h2 style={{ marginTop: 32 }}>Full migration diff</h2>
          <div className="panel">
            <DiffBlock diff={report.diff} />
          </div>
        </>
      ) : null}
    </main>
  );
}
