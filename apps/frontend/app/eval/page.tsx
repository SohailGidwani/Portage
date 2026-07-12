"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { Metric, Progress, StatusPill, relativeTime } from "../components/ui";
import { EvalRun, Leaderboard, getLeaderboard, listEvalRuns } from "../lib/api";

export default function EvalPage() {
  const [board, setBoard] = useState<Leaderboard | null>(null);
  const [suite, setSuite] = useState("");
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextBoard, nextRuns] = await Promise.all([getLeaderboard(suite ? [suite] : undefined), listEvalRuns()]);
      setBoard(nextBoard);
      setRuns(nextRuns);
      setError(null);
    } catch (err) { setError(String(err)); }
  }, [suite]);

  useEffect(() => { refresh(); const timer = window.setInterval(refresh, 10000); return () => window.clearInterval(timer); }, [refresh]);
  useEffect(() => { if (!suite && board?.suites[0]) setSuite(board.suites[0]); }, [board, suite]);

  const rows = board?.rows ?? [];
  const baseline = rows.filter((row) => row.scenario === "baseline");
  const faultRows = rows.filter((row) => row.scenario !== "baseline");
  const total = baseline.reduce((sum, row) => sum + row.runs, 0);
  const green = baseline.reduce((sum, row) => sum + row.green, 0);
  const meanCost = total ? baseline.reduce((sum, row) => sum + row.cost_mean * row.runs, 0) / total : 0;
  const meanWall = total ? baseline.reduce((sum, row) => sum + row.wall_mean * row.runs, 0) / total : 0;

  return (
    <AppShell eyebrow="Evaluation lab" title="Migration evidence" description="Pinned repositories, repeat runs, and honest outcomes—tests alone never define a green migration." actions={<label className="suite-picker"><span>Suite</span><select value={suite} onChange={(e) => setSuite(e.target.value)}><option value="">All suites</option>{(board?.suites ?? []).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>}>
      {error && <div className="notice error">{error}</div>}
      <section className="truth-callout"><div className="truth-icon">✓</div><div><strong>What “green” means here</strong><p>The migration completed, no task was rolled back or skipped, protected test oracles stayed valid, and the full test suite passed. A passing suite after rollback remains red.</p></div></section>
      <section className="metrics-grid"><Metric label="Baseline runs" value={total} detail={`${baseline.length} corpus repositories`} /><Metric label="Green migrations" value={total ? `${Math.round(green / total * 100)}%` : "—"} detail={`${green}/${total} complete outcomes`} tone="good" /><Metric label="Mean green cost" value={total ? `$${meanCost.toFixed(3)}` : "—"} detail="Per evaluated migration" /><Metric label="Mean wall time" value={total ? `${Math.round(meanWall)}s` : "—"} detail="End-to-end harness time" /><Metric label="Fault scenarios" value={faultRows.length} detail="Deterministic recovery probes" tone={faultRows.length ? "warn" : undefined} /></section>

      <section className="surface eval-board">
        <div className="section-heading"><div><span className="kicker">Baseline</span><h2>Corpus leaderboard</h2></div><span className="muted">Selected suite: {suite || "all"}</span></div>
        <div className="eval-cards">
          {baseline.map((row) => <article className="eval-card" key={`${row.corpus_name}-${row.scenario}`}><header><div><strong>{row.corpus_name}</strong><span>{row.tier || "unclassified"}</span></div><StatusPill status={row.green === row.runs && row.runs > 0 ? "success" : row.green > 0 ? "running" : "failed"} label={`${row.green}/${row.runs} green`} /></header><div className="eval-rate"><strong>{Math.round(row.green_rate * 100)}%</strong><Progress value={row.green_rate} tone={row.green_rate === 1 ? "good" : row.green_rate > 0 ? "warn" : "bad"} /></div><dl><div><dt>Test pass</dt><dd>{Math.round(row.test_pass_mean * 100)}%</dd></div><div><dt>Task completion</dt><dd>{row.completion_mean === undefined ? "—" : `${Math.round(row.completion_mean * 100)}%`}</dd></div><div><dt>Mean cost</dt><dd>${row.cost_mean.toFixed(3)}</dd></div><div><dt>Wall time</dt><dd>{Math.round(row.wall_mean)}s</dd></div></dl></article>)}
          {!baseline.length && <div className="empty-state">No baseline runs in this suite. Start the evaluation harness to populate measured results.</div>}
        </div>
      </section>

      <div className="dashboard-grid eval-lower">
        <section className="surface">
          <div className="section-heading"><div><span className="kicker">Recovery proof</span><h2>Injected fault scenarios</h2></div></div>
          <div className="table-scroll"><table className="data-table"><thead><tr><th>Repository</th><th>Scenario</th><th>Outcome</th><th>Escalation</th><th>Recovery</th></tr></thead><tbody>{faultRows.map((row) => <tr key={`${row.corpus_name}-${row.scenario}`}><td><strong>{row.corpus_name}</strong></td><td>{row.scenario.replaceAll("_", " ")}</td><td><StatusPill status={row.green === row.runs ? "success" : "failed"} label={`${row.green}/${row.runs} green`} /></td><td className="mono">{row.escalation_attempted ? `${row.escalation_rescued}/${row.escalation_attempted}` : "—"}</td><td>{row.recover_visits_mean.toFixed(1)} visits</td></tr>)}{!faultRows.length && <tr><td colSpan={5}><div className="empty-state small">No fault-scenario aggregates for this selection.</div></td></tr>}</tbody></table></div>
        </section>
        <aside className="surface eval-snapshot"><div className="section-heading"><div><span className="kicker">Latest evidence</span><h2>Recent harness runs</h2></div></div>{runs.slice(0, 8).map((run) => <div className="snapshot-row" key={run.id}><div><strong>{run.job_id ? <Link href={`/jobs/${run.job_id}`}>{run.corpus_name}</Link> : run.corpus_name}</strong><small>{run.scenario.replaceAll("_", " ")} · {relativeTime(run.created_at)}</small></div><div className="snapshot-result"><StatusPill status={run.status === "green" ? "success" : run.status} label={run.status} /><small>${run.cost_usd.toFixed(3)}</small></div></div>)}</aside>
      </div>
    </AppShell>
  );
}
