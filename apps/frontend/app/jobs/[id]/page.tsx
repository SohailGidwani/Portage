"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "../../components/AppShell";
import { ReviewGuide } from "../../components/ReviewGuide";
import { CopyButton, DiffView, Metric, Progress, StatusPill, shortRepo } from "../../components/ui";
import { AttemptEntry, Job, Report, Task, getJob, getJobReport, getJobTasks } from "../../lib/api";

type Tab = "overview" | "files" | "diff" | "recovery";

const stages = ["ingest", "plan", "execute", "verify", "recover", "integrate", "report"];

function taskStage(job: Job | null, tasks: Task[], report: Report | null, stage: string): "done" | "active" | "pending" | "optional" {
  const fileTasks = tasks.filter((task) => task.target_path);
  const settled = fileTasks.length > 0 && fileTasks.every((task) => ["done", "skipped"].includes(task.status));
  const finished = job?.status === "done" || job?.status === "failed";
  if (stage === "recover") return (report?.recovery?.visits ?? 0) > 0 ? "done" : "optional";
  const done: Record<string, boolean> = {
    ingest: fileTasks.length > 0 || finished,
    plan: fileTasks.length > 0 || finished,
    execute: settled || finished,
    verify: finished,
    integrate: finished,
    report: Boolean(report),
  };
  if (done[stage]) return "done";
  if (job?.status !== "running") return "pending";
  if (stage === "ingest" && !fileTasks.length) return "active";
  if (stage === "execute" && fileTasks.length && !settled) return "active";
  if (stage === "verify" && settled) return "active";
  return "pending";
}

function splitDiff(diff: string): { path: string; diff: string; additions: number; deletions: number }[] {
  if (!diff.trim()) return [];
  const chunks = diff.split(/(?=^diff --git )/m).filter(Boolean);
  return chunks.map((chunk, index) => {
    const match = chunk.match(/^diff --git a\/(.+?) b\/(.+)$/m);
    const lines = chunk.split("\n");
    return {
      path: match?.[2] ?? `change-${index + 1}`,
      diff: chunk,
      additions: lines.filter((line) => line.startsWith("+") && !line.startsWith("+++")).length,
      deletions: lines.filter((line) => line.startsWith("-") && !line.startsWith("---")).length,
    };
  });
}

function Attempt({ entry }: { entry: AttemptEntry }) {
  const action = (entry.action ?? "attempt").replaceAll("_", " ");
  const cost = entry.cost_usd ? ` · $${entry.cost_usd.toFixed(4)}` : "";
  return (
    <li className="timeline-item">
      <span className={`timeline-dot ${entry.tier === "escalation" ? "warn" : ""}`} />
      <div><strong>{action}</strong><p>{entry.model ?? entry.tier ?? "deterministic"}{entry.attempt ? ` · attempt ${entry.attempt}` : ""}{cost}</p>{entry.reason && <small>{entry.reason}</small>}</div>
      {entry.at && <time>{new Date(entry.at).toLocaleTimeString()}</time>}
    </li>
  );
}

export default function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<Job | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [selectedFile, setSelectedFile] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!id) return;
    try {
      const [nextJob, nextTasks] = await Promise.all([getJob(id), getJobTasks(id)]);
      setJob(nextJob);
      setTasks(nextTasks);
      if (nextJob.report_path) setReport(await getJobReport(id));
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }, [id]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 2500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const files = useMemo(() => splitDiff(report?.diff ?? ""), [report?.diff]);
  useEffect(() => { if (!selectedFile && files[0]) setSelectedFile(files[0].path); }, [files, selectedFile]);
  const activeFile = files.find((file) => file.path === selectedFile) ?? files[0];
  const fileTasks = tasks.filter((task) => task.target_path);
  const done = fileTasks.filter((task) => task.status === "done").length;
  const outcome = report?.migration_outcome ?? job?.status ?? "queued";
  const tests = report?.test_summary ?? job?.test_summary;
  const testRate = tests?.total ? tests.passed / tests.total : 0;
  const totalAdds = files.reduce((sum, file) => sum + file.additions, 0);
  const totalDels = files.reduce((sum, file) => sum + file.deletions, 0);

  function downloadDiff() {
    if (!report?.diff) return;
    const blob = new Blob([report.diff], { type: "text/x-diff" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `portage-${id.slice(0, 8)}.patch`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <AppShell
      eyebrow="Migration run"
      title={id.slice(0, 8)}
      description={job ? `${shortRepo(job.repo_url)} · ${job.migration_recipe.replaceAll("_", " ")}` : "Loading run…"}
      actions={<><CopyButton value={id} label="Copy run ID" /><button className="button primary" disabled={!report?.diff} onClick={downloadDiff}>Download patch</button></>}
    >
      <div className="breadcrumbs"><Link href="/">Runs</Link><span>/</span><span>{id.slice(0, 8)}</span></div>
      {error && <div className="notice error">{error}</div>}
      {job?.error && <div className="notice error">{job.error}</div>}

      <section className={`outcome-banner outcome-${outcome}`}>
        <div><StatusPill status={outcome} /><h2>{outcome === "success" ? "Migration complete and verified" : outcome === "running" ? "Portage is working through the plan" : outcome === "unsupported" ? "The migration reached an unsupported test seam" : outcome === "failed" ? "The migration needs attention" : "Run is preparing"}</h2><p>{outcome === "success" ? "Every planned task completed, the protected oracle stayed intact, and the full suite passed." : outcome === "running" ? "This page updates automatically as tasks, verification, and recovery progress." : "Inspect the task and recovery evidence below before deciding the next step."}</p></div>
        <div className="outcome-tests"><strong>{tests?.total ? `${tests.passed}/${tests.total}` : "—"}</strong><span>full-suite tests</span><Progress value={testRate} tone={outcome === "failed" ? "bad" : "good"} /></div>
      </section>

      <ol className="pipeline" aria-label="Migration pipeline">
        {stages.map((stage) => { const state = taskStage(job, tasks, report, stage); return <li key={stage} className={state}><span>{state === "done" ? "✓" : stage === "recover" ? "↻" : ""}</span><strong>{stage}</strong></li>; })}
      </ol>

      <nav className="tabs" aria-label="Run details">
        {(["overview", "files", "diff", "recovery"] as Tab[]).map((value) => <button key={value} className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{value}{value === "files" && fileTasks.length ? ` ${fileTasks.length}` : ""}</button>)}
      </nav>

      {tab === "overview" && <>
        <section className="metrics-grid run-metrics">
          <Metric label="Files completed" value={fileTasks.length ? `${done}/${fileTasks.length}` : "—"} detail={`${report?.tasks_total ?? tasks.length} plan tasks`} tone={done === fileTasks.length && done > 0 ? "good" : undefined} />
          <Metric label="Oracle integrity" value={report?.oracle_integrity ? `${Math.round(report.oracle_integrity.integrity_rate * 100)}%` : "—"} detail={report?.oracle_integrity ? `${report.oracle_integrity.clean_files}/${report.oracle_integrity.protected_files} protected files clean` : "Not checked yet"} tone={report?.oracle_integrity?.violations.length ? "bad" : "good"} />
          <Metric label="Verified batches" value={report?.verified_batches?.length ?? "—"} detail="Incremental verification gates" />
          <Metric label="Recovery visits" value={report?.recovery?.visits ?? "—"} detail={report?.recovery?.last_classification?.replaceAll("_", " ") ?? "No recovery evidence yet"} tone={(report?.recovery?.visits ?? 0) > 0 ? "warn" : undefined} />
          <Metric label="LLM usage" value={report?.llm_usage ? `$${report.llm_usage.cost_usd.toFixed(3)}` : "—"} detail={report?.llm_usage ? `${report.llm_usage.calls} calls · ${(report.llm_usage.prompt_tokens + report.llm_usage.completion_tokens).toLocaleString()} tokens` : "Available after reporting"} />
        </section>

        <div className="run-grid">
          <section className="surface">
            <div className="section-heading"><div><span className="kicker">Plan</span><h2>Task progress</h2></div><button className="button ghost" onClick={() => setTab("files")}>Open files</button></div>
            <div className="task-list">
              {fileTasks.map((task) => <div className="task-row" key={task.id}><span className={`file-icon file-${task.status}`}>{task.status === "done" ? "✓" : task.status === "running" ? "…" : "·"}</span><div><strong>{task.target_path}</strong><small>{task.type.replaceAll("_", " ")} · {task.attempts} attempt{task.attempts === 1 ? "" : "s"}</small></div><StatusPill status={task.status} /></div>)}
              {!fileTasks.length && <div className="empty-state">The migration plan has not produced file tasks yet.</div>}
            </div>
          </section>
          <aside className="surface summary-card">
            <span className="kicker">Change summary</span><h2>{files.length} changed file{files.length === 1 ? "" : "s"}</h2>
            <div className="change-counts"><span className="add">+{totalAdds}</span><span className="delete">−{totalDels}</span></div>
            <p>The browser view is for inspection. Export the patch before reviewing or applying it locally.</p>
            <button className="button secondary full" disabled={!report?.diff} onClick={() => setTab("diff")}>Open diff workspace</button>
          </aside>
        </div>
        {report && <ReviewGuide jobId={id} compact />}
      </>}

      {tab === "files" && <section className="surface">
        <div className="section-heading"><div><span className="kicker">Execution plan</span><h2>Files and attempts</h2></div></div>
        <div className="file-cards">{fileTasks.map((task) => <article className="file-card" key={task.id}><header><div><strong>{task.target_path}</strong><small>{task.type.replaceAll("_", " ")}</small></div><StatusPill status={task.status} /></header>{task.subtasks.length > 0 && <div className="tag-list">{task.subtasks.map((subtask) => <span key={subtask.id}>{subtask.type.replaceAll("_", " ")}</span>)}</div>}{task.error && <div className="notice error">{task.error}</div>}{task.attempts_log.length > 0 && <ol className="timeline">{task.attempts_log.map((entry, index) => <Attempt entry={entry} key={index} />)}</ol>}</article>)}</div>
      </section>}

      {tab === "diff" && <>
        <section className="diff-workbench surface">
          <header className="diff-toolbar"><div><span className="kicker">Generated patch</span><h2>{activeFile?.path ?? "No diff available"}</h2></div><div><span className="change-counts"><span className="add">+{totalAdds}</span><span className="delete">−{totalDels}</span></span><CopyButton value={report?.diff ?? ""} label="Copy patch" /><button className="button primary" disabled={!report?.diff} onClick={downloadDiff}>Download .patch</button></div></header>
          <div className="diff-layout"><aside className="diff-files">{files.map((file) => <button key={file.path} className={activeFile?.path === file.path ? "active" : ""} onClick={() => setSelectedFile(file.path)}><span>{file.path}</span><small><i>+{file.additions}</i> <b>−{file.deletions}</b></small></button>)}{!files.length && <div className="empty-state small">No files changed.</div>}</aside><div className="diff-code"><DiffView diff={activeFile?.diff ?? ""} /></div></div>
        </section>
        <ReviewGuide jobId={id} />
      </>}

      {tab === "recovery" && <div className="run-grid">
        <section className="surface"><div className="section-heading"><div><span className="kicker">Recovery</span><h2>Decisions and retries</h2></div><StatusPill status={(report?.recovery?.visits ?? 0) ? "running" : "success"} label={(report?.recovery?.visits ?? 0) ? `${report?.recovery?.visits} visits` : "Not needed"} /></div>{report?.recovery?.actions.length ? <ol className="timeline recovery-timeline">{report.recovery.actions.map((action, index) => <li className="timeline-item" key={index}><span className="timeline-dot warn" /><div><strong>{action.action?.replaceAll("_", " ") ?? "recovery decision"}</strong><p>{action.classification?.replaceAll("_", " ") ?? "unclassified"}</p>{action.targets?.length ? <small>Targets: {action.targets.join(", ")}</small> : null}</div><time>visit {action.visit ?? index + 1}</time></li>)}</ol> : <div className="empty-state">No recovery was required for this run.</div>}</section>
        <aside className="surface summary-list"><span className="kicker">Recovery counters</span><dl><div><dt>Integration visits</dt><dd>{report?.recovery?.integration_visits ?? 0}</dd></div><div><dt>No-progress retries</dt><dd>{report?.recovery?.no_progress_retries ?? 0}</dd></div><div><dt>Escalation rescued</dt><dd>{report?.recovery ? `${report.recovery.escalation_rescued}/${report.recovery.escalation_attempted}` : "—"}</dd></div><div><dt>Tasks skipped</dt><dd>{report?.recovery?.tasks_skipped ?? 0}</dd></div></dl></aside>
      </div>}
    </AppShell>
  );
}
