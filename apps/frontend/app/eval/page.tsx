"use client";

// The proof page (Phase 6): aggregate eval results over the runs/metrics contract.
// Leaderboard (per repo × scenario, K-run green rate + mean±variance) and the
// chaos-recovery view (fault-injection runs + their recovery evidence).
// Aggregate-only by design — no user repo contents belong on a shared surface.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  EvalRun,
  Leaderboard,
  getLeaderboard,
  listEvalRuns,
} from "../lib/api";

function GreenRate({ green, runs }: { green: number; runs: number }) {
  const ok = green === runs && runs > 0;
  const some = green > 0;
  return (
    <span
      className={`mono ${ok ? "s-done" : some ? "s-running" : "s-failed"}`}
      style={{ fontWeight: 650 }}
    >
      {green}/{runs}
    </span>
  );
}

export default function EvalPage() {
  const [board, setBoard] = useState<Leaderboard | null>(null);
  const [suite, setSuite] = useState<string>("");
  const [faultRuns, setFaultRuns] = useState<EvalRun[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [b, f] = await Promise.all([
        getLeaderboard(suite ? [suite] : undefined),
        listEvalRuns("faults"),
      ]);
      setBoard(b);
      setFaultRuns(f);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [suite]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [refresh]);

  const baseline = (board?.rows ?? []).filter((r) => r.scenario === "baseline");
  const faults = (board?.rows ?? []).filter((r) => r.scenario !== "baseline");

  return (
    <main>
      <header className="masthead">
        <h1 className="wordmark">
          <Link href="/">PORTAGE</Link>
          <span className="tagline">
            eval proof — measured on pinned repos, K runs, honest greens (full
            migration + full suite; a rolled-back run never scores green)
          </span>
        </h1>
        <span className="syscheck">
          suite{" "}
          <select
            value={suite}
            onChange={(e) => setSuite(e.target.value)}
            aria-label="suite filter"
            style={{ padding: "4px 8px", fontSize: 12 }}
          >
            <option value="">all suites</option>
            {(board?.suites ?? []).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </span>
      </header>

      {error && <p className="errband">{error}</p>}

      <h2 className="eyebrow">Baseline migrations — leaderboard</h2>
      <div className="panel tablewrap">
        <table>
          <thead>
            <tr>
              <th>corpus repo</th>
              <th>tier</th>
              <th>green</th>
              <th>test-pass mean</th>
              <th>variance</th>
              <th>avg cost</th>
              <th>avg wall</th>
              <th>avg recover visits</th>
            </tr>
          </thead>
          <tbody>
            {baseline.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  No eval runs yet — run the harness (see docs/USAGE.md §1.10).
                </td>
              </tr>
            )}
            {baseline.map((r) => (
              <tr key={`${r.corpus_name}-${r.scenario}`}>
                <td className="mono">{r.corpus_name}</td>
                <td className="mono muted">{r.tier || "—"}</td>
                <td>
                  <GreenRate green={r.green} runs={r.runs} />
                </td>
                <td className="mono">{r.test_pass_mean.toFixed(2)}</td>
                <td className="mono muted">±{r.test_pass_variance.toFixed(4)}</td>
                <td className="mono">${r.cost_mean.toFixed(3)}</td>
                <td className="mono muted">{Math.round(r.wall_mean)}s</td>
                <td className="mono muted">{r.recover_visits_mean.toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 12.5 }}>
        The reds are documented, not hidden: each failing tier has an analyzed entry in{" "}
        <code>corpus/FINDINGS.md</code> (the failure taxonomy). JSON-API repos migrate
        green for ~$0.01–0.05; template/extension-heavy apps are the current frontier.
      </p>

      <h2 className="eyebrow">Chaos recovery — injected faults</h2>
      <div className="dash-grid">
        <section className="panel tablewrap">
          <table>
            <thead>
              <tr>
                <th>corpus repo</th>
                <th>fault scenario</th>
                <th>green</th>
                <th>escalation rescued</th>
                <th>avg recover visits</th>
              </tr>
            </thead>
            <tbody>
              {faults.length === 0 && (
                <tr>
                  <td colSpan={5} className="muted">
                    No fault-scenario aggregates in this suite selection.
                  </td>
                </tr>
              )}
              {faults.map((r) => (
                <tr key={`${r.corpus_name}-${r.scenario}`}>
                  <td className="mono">{r.corpus_name}</td>
                  <td className="mono muted">{r.scenario.replaceAll("_", " ")}</td>
                  <td>
                    <GreenRate green={r.green} runs={r.runs} />
                  </td>
                  <td className="mono">
                    {r.escalation_attempted > 0
                      ? `${r.escalation_rescued}/${r.escalation_attempted}`
                      : "—"}
                  </td>
                  <td className="mono muted">{r.recover_visits_mean.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12.5, padding: "8px 8px 0" }}>
            Faults are deterministic corruptions injected mid-migration: a corrupted
            patch (rescued by targeted rollback + regenerate), a patch corrupted until
            the escalation tier takes over (measured escalation), and a deliberately
            dropped plan task (repaired by replan).
          </p>
        </section>

        <aside>
          <h3 className="eyebrow" style={{ marginTop: 0 }}>
            Recent fault runs
          </h3>
          {faultRuns.slice(0, 8).map((r) => (
            <div className="taskcard" key={r.id} style={{ padding: "10px 14px" }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="mono" style={{ fontSize: 13 }}>
                  {r.job_id ? (
                    <Link href={`/jobs/${r.job_id}`}>{r.corpus_name}</Link>
                  ) : (
                    r.corpus_name
                  )}
                </span>
                <span
                  className={`status s-${r.status === "green" ? "done" : "failed"}`}
                >
                  {r.status === "green" ? `${r.tests_passed}/${r.tests_total}` : r.status}
                </span>
              </div>
              <div className="muted mono" style={{ fontSize: 11.5, marginTop: 4 }}>
                {r.scenario.replaceAll("_", " ")} · {r.recover_visits} recover visit
                {r.recover_visits === 1 ? "" : "s"} · ${r.cost_usd.toFixed(3)}
              </div>
            </div>
          ))}
          {faultRuns.length === 0 && (
            <div className="panel muted">No fault runs recorded yet.</div>
          )}
        </aside>
      </div>
    </main>
  );
}
