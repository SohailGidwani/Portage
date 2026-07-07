"""portage CLI implementation — argparse + httpx, sync (a CLI has no need for async)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

_DEFAULT_API = os.environ.get("PORTAGE_API", "http://localhost:8000")
_TERMINAL = ("done", "failed")


def _client(api: str) -> httpx.Client:
    return httpx.Client(base_url=api, timeout=30.0)


def _die(msg: str, code: int = 2) -> int:
    print(f"portage: {msg}", file=sys.stderr)
    return code


def _fmt_tests(ts: dict | None) -> str:
    if not ts or not ts.get("total"):
        return "—"
    return f"{ts.get('passed', 0)}/{ts.get('total', 0)}"


def _job_line(j: dict) -> str:
    return (f"{j['id'][:8]}  {j['status']:8} {j['migration_recipe']:18} "
            f"tests={_fmt_tests(j.get('test_summary')):7} {j['repo_url']}")


def _print_summary(client: httpx.Client, job: dict) -> bool:
    """Print the terminal summary; return True iff the migration is honestly green."""
    tasks = client.get(f"/jobs/{job['id']}/tasks").json()
    file_tasks = [t for t in tasks if t.get("target_path")]
    done = sum(1 for t in file_tasks if t["status"] == "done")
    skipped = sum(1 for t in file_tasks if t["status"] == "skipped")
    ts = job.get("test_summary") or {}
    suite_ok = bool(ts.get("total")) and ts.get("passed") == ts.get("total")
    fully_migrated = bool(file_tasks) and done == len(file_tasks) and skipped == 0

    print(f"\njob      : {job['id']}")
    print(f"status   : {job['status']}")
    print(f"tests    : {_fmt_tests(ts)}")
    print(f"tasks    : {done}/{len(file_tasks)} done"
          + (f", {skipped} skipped (rolled back)" if skipped else ""))
    for t in file_tasks:
        print(f"  - {t['target_path']:32} {t['status']:8} attempts={t['attempts']}")
    if job.get("error"):
        print(f"error    : {job['error']}")
    verdict = "GREEN — migrated, full suite passing" if (suite_ok and fully_migrated) \
        else "RED — not a complete green migration (see tasks/report)"
    print(f"verdict  : {verdict}")
    return suite_ok and fully_migrated


def cmd_migrate(args: argparse.Namespace) -> int:
    config: dict = {}
    if args.ref:
        config["repo_ref"] = args.ref
    if args.subdir:
        config["repo_subdir"] = args.subdir
    with _client(args.api) as client:
        r = client.post("/jobs", json={
            "repo_url": args.repo,
            "migration_recipe": args.recipe,
            "config": config,
        })
        if r.status_code != 201:
            return _die(f"submit failed ({r.status_code}): {r.text[:200]}")
        job = r.json()
        print(f"submitted {job['id']}  ({args.recipe} on {args.repo})")
        if not args.watch:
            print(f"follow with: portage status {job['id']}")
            return 0

        seen_steps: set[str] = set()
        while job["status"] not in _TERMINAL:
            time.sleep(args.poll)
            job = client.get(f"/jobs/{job['id']}").json()
            # Surface task-level movement (the closest thing to a node trace over REST).
            for t in client.get(f"/jobs/{job['id']}/tasks").json():
                key = f"{t.get('target_path')}:{t['status']}:{t['attempts']}"
                if t.get("target_path") and key not in seen_steps:
                    seen_steps.add(key)
                    print(f"  {t['status']:8} {t['target_path']} (attempt {t['attempts']})")
        ok = _print_summary(client, job)
        return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    with _client(args.api) as client:
        r = client.get(f"/jobs/{args.job_id}")
        if r.status_code == 404:
            return _die("job not found")
        if r.status_code != 200:  # e.g. 422 for a malformed job id
            return _die(f"invalid job id ({r.status_code}): {args.job_id}")
        job = r.json()
        ok = _print_summary(client, job)
        return 0 if (job["status"] not in _TERMINAL or ok) else 1


def cmd_jobs(args: argparse.Namespace) -> int:
    with _client(args.api) as client:
        for j in client.get("/jobs", params={"limit": args.limit}).json():
            print(_job_line(j))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    with _client(args.api) as client:
        r = client.get(f"/jobs/{args.job_id}/report")
        if r.status_code == 404:
            return _die("no report for this job (yet)")
        if r.status_code != 200:
            return _die(f"invalid job id ({r.status_code}): {args.job_id}")
        report = r.json()
        if args.diff:
            print(report.get("diff") or "(empty diff)")
            return 0
        report.pop("diff", None)  # large; ask for it explicitly
        print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="portage",
        description="Portage — autonomous code migration (thin client over the API).",
    )
    p.add_argument("--api", default=_DEFAULT_API,
                   help=f"control-plane URL (default: {_DEFAULT_API}, env PORTAGE_API)")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="submit a migration (optionally watch to the end)")
    m.add_argument("repo", help="git URL or a path visible to the worker")
    m.add_argument("--recipe", default="flask_to_fastapi")
    m.add_argument("--ref", default="", help="pin a commit SHA (reproducible runs)")
    m.add_argument("--subdir", default="", help="app lives in this subdir of the repo")
    m.add_argument("--watch", action="store_true", help="stream progress; exit 0 iff green")
    m.add_argument("--poll", type=float, default=2.0, help=argparse.SUPPRESS)
    m.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("status", help="job status + task tree + verdict")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_status)

    lst = sub.add_parser("jobs", help="list recent jobs")
    lst.add_argument("--limit", type=int, default=20)
    lst.set_defaults(fn=cmd_jobs)

    rep = sub.add_parser("report", help="print a job's report.json (use --diff for the diff)")
    rep.add_argument("job_id")
    rep.add_argument("--diff", action="store_true")
    rep.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except httpx.ConnectError:
        return _die(f"cannot reach the API at {args.api} — is `docker compose up` running?")


if __name__ == "__main__":
    raise SystemExit(main())
