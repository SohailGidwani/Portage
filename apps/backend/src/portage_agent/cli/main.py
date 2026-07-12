"""Portage CLI — a rich, thin client over the control-plane API."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

_DEFAULT_API = os.environ.get("PORTAGE_API", "http://localhost:8000")
_TERMINAL = ("done", "failed")
console = Console()
error_console = Console(stderr=True)


def _client(api: str) -> httpx.Client:
    headers = {}
    if key := os.environ.get("PORTAGE_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    return httpx.Client(base_url=api, timeout=30.0, headers=headers)


def _die(msg: str, code: int = 2) -> int:
    error_console.print(Panel(msg, title="[bold red]Portage error[/]", border_style="red"))
    return code


def _fmt_tests(summary: dict | None) -> str:
    if not summary or not summary.get("total"):
        return "—"
    return f"{summary.get('passed', 0)}/{summary.get('total', 0)}"


def _status_text(status: str) -> Text:
    styles = {
        "success": "bold green",
        "done": "bold green",
        "green": "bold green",
        "running": "bold yellow",
        "queued": "yellow",
        "unsupported": "bold red",
        "failed": "bold red",
        "red": "bold red",
        "error": "bold red",
        "timeout": "bold red",
        "skipped": "red",
    }
    return Text(status.replace("_", " ").upper(), style=styles.get(status, "dim"))


def _get_json(response: httpx.Response, label: str) -> dict | list:
    if response.status_code == 401:
        raise RuntimeError("authentication required; set PORTAGE_API_KEY to a valid pk_… key")
    if response.status_code >= 400:
        raise RuntimeError(f"{label} failed ({response.status_code}): {response.text[:200]}")
    return response.json()


def _report(client: httpx.Client, job_id: str) -> dict | None:
    response = client.get(f"/jobs/{job_id}/report")
    if response.status_code == 404:
        return None
    return _get_json(response, "report")  # type: ignore[return-value]


def _honest_outcome(job: dict, report: dict | None) -> str:
    if job["status"] in ("queued", "running", "failed"):
        return job["status"]
    return (report or {}).get("migration_outcome", "done")


def _task_table(tasks: list[dict], *, compact: bool = False) -> Table:
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False, show_header=not compact)
    table.add_column("File", ratio=4, overflow="fold")
    table.add_column("Status", width=13)
    table.add_column("Attempts", justify="right", width=9)
    for task in (task for task in tasks if task.get("target_path")):
        table.add_row(
            str(task["target_path"]),
            _status_text(task["status"]),
            str(task.get("attempts", 0)),
        )
    if not table.row_count:
        table.add_row("[dim]Waiting for the migration plan…[/]", "", "")
    return table


def _watch_view(job: dict, tasks: list[dict]) -> Group:
    file_tasks = [task for task in tasks if task.get("target_path")]
    done = sum(task["status"] in ("done", "skipped") for task in file_tasks)
    ratio = done / len(file_tasks) if file_tasks else 0
    header = Table.grid(expand=True)
    header.add_column(ratio=2)
    header.add_column(justify="right")
    header.add_row(
        f"[bold]Run {job['id'][:8]}[/]\n[dim]{job['migration_recipe']} · {job['repo_url']}[/]",
        _status_text(job["status"]),
    )
    header.add_row(
        ProgressBar(total=100, completed=ratio * 100, width=None),
        f"[dim]{done}/{len(file_tasks) or '—'} files[/]",
    )
    return Group(
        Panel(header, border_style="bright_black", padding=(1, 2)), _task_table(tasks, compact=True)
    )


def _summary_view(client: httpx.Client, job: dict) -> tuple[Group, bool]:
    tasks = _get_json(client.get(f"/jobs/{job['id']}/tasks"), "tasks")
    assert isinstance(tasks, list)
    report = _report(client, job["id"])
    outcome = _honest_outcome(job, report)
    file_tasks = [task for task in tasks if task.get("target_path")]
    done = sum(task["status"] == "done" for task in file_tasks)
    skipped = sum(task["status"] == "skipped" for task in file_tasks)
    tests = (report or {}).get("test_summary") or job.get("test_summary") or {}
    recovery = (report or {}).get("recovery") or {}
    usage = (report or {}).get("llm_usage") or {}
    oracle = (report or {}).get("oracle_integrity") or {}

    facts = Table.grid(expand=True, padding=(0, 2))
    facts.add_column(style="dim", width=18)
    facts.add_column()
    facts.add_row("Outcome", _status_text(outcome))
    facts.add_row("Full suite", _fmt_tests(tests))
    facts.add_row(
        "Plan",
        f"{done}/{len(file_tasks)} files completed" + (f" · {skipped} skipped" if skipped else ""),
    )
    if oracle:
        integrity = (
            f"{oracle.get('clean_files', 0)}/"
            f"{oracle.get('protected_files', 0)} protected files clean"
        )
        facts.add_row(
            "Oracle integrity",
            integrity,
        )
    if recovery:
        recovery_detail = recovery.get("last_classification") or "not needed"
        facts.add_row(
            "Recovery",
            f"{recovery.get('visits', 0)} visits · {recovery_detail}",
        )
    if usage:
        facts.add_row(
            "LLM usage", f"{usage.get('calls', 0)} calls · ${usage.get('cost_usd', 0):.3f}"
        )
    if job.get("error"):
        facts.add_row("Error", f"[red]{job['error']}[/]")

    verdict = {
        "success": "Migration complete. The plan, oracle, and full suite are green.",
        "running": "Migration is still running.",
        "queued": "Migration is queued.",
        "unsupported": "Migration stopped at an unsupported seam. Review the report.",
        "failed": "Migration is incomplete. Review failed tasks and recovery evidence.",
    }.get(outcome, "Run completed. Review the report for its migration outcome.")
    next_steps = Text()
    next_steps.append(
        f"\nReview: portage diff {job['id']} --output portage-{job['id'][:8]}.patch", style="cyan"
    )
    next_steps.append(f"\nWeb:    http://localhost:3000/jobs/{job['id']}", style="dim")
    panel = Panel(
        Group(facts, Text(f"\n{verdict}", style="bold"), next_steps),
        title=f"[bold]Portage · {job['id'][:8]}[/]",
        border_style="green"
        if outcome == "success"
        else "yellow"
        if outcome in ("running", "queued")
        else "red",
    )
    return Group(panel, _task_table(tasks)), outcome == "success"


def cmd_migrate(args: argparse.Namespace) -> int:
    config: dict = {}
    if args.ref:
        config["repo_ref"] = args.ref
    if args.subdir:
        config["repo_subdir"] = args.subdir
    with _client(args.api) as client:
        response = client.post(
            "/jobs", json={"repo_url": args.repo, "migration_recipe": args.recipe, "config": config}
        )
        if response.status_code != 201:
            return _die(f"Submit failed ({response.status_code}): {response.text[:200]}")
        job = response.json()
        if not args.watch:
            submitted = (
                f"[bold green]Run submitted[/]\n\n[bold]{job['id']}[/]\n"
                f"[dim]{args.recipe} · {args.repo}[/]\n\n"
                f"Follow it with [cyan]portage status {job['id']}[/]"
            )
            console.print(
                Panel(
                    submitted,
                    border_style="green",
                )
            )
            return 0

        tasks: list[dict] = []
        with Live(
            _watch_view(job, tasks), console=console, refresh_per_second=6, transient=True
        ) as live:
            while job["status"] not in _TERMINAL:
                time.sleep(args.poll)
                job = _get_json(client.get(f"/jobs/{job['id']}"), "job")
                tasks = _get_json(client.get(f"/jobs/{job['id']}/tasks"), "tasks")
                assert isinstance(job, dict) and isinstance(tasks, list)
                live.update(_watch_view(job, tasks))
        view, green = _summary_view(client, job)
        console.print(view)
        return 0 if green else 1


def cmd_status(args: argparse.Namespace) -> int:
    with _client(args.api) as client:
        response = client.get(f"/jobs/{args.job_id}")
        if response.status_code == 404:
            return _die("Job not found")
        job = _get_json(response, "job")
        assert isinstance(job, dict)
        view, green = _summary_view(client, job)
        console.print(view)
        return 0 if job["status"] not in _TERMINAL or green else 1


def cmd_jobs(args: argparse.Namespace) -> int:
    with _client(args.api) as client:
        jobs = _get_json(client.get("/jobs", params={"limit": args.limit}), "jobs")
        assert isinstance(jobs, list)
        table = Table(title="Portage · recent runs", box=box.ROUNDED, expand=True, pad_edge=False)
        table.add_column("Run", style="cyan", width=10)
        table.add_column("Outcome", width=14)
        table.add_column("Recipe", ratio=1)
        table.add_column("Tests", justify="right", width=8)
        table.add_column("Repository", ratio=2, overflow="fold")
        for job in jobs:
            report = _report(client, job["id"]) if job.get("report_path") else None
            table.add_row(
                job["id"][:8],
                _status_text(_honest_outcome(job, report)),
                job["migration_recipe"].replace("_", " "),
                _fmt_tests(job.get("test_summary")),
                job["repo_url"],
            )
        if not jobs:
            table.add_row(
                "—", Text("NO RUNS", style="dim"), "", "", "Start with portage migrate <repo>"
            )
        console.print(table)
    return 0


def _diff_stats(diff: str) -> tuple[int, int, int]:
    files = diff.count("\ndiff --git ") + (1 if diff.startswith("diff --git ") else 0)
    additions = sum(
        line.startswith("+") and not line.startswith("+++") for line in diff.splitlines()
    )
    deletions = sum(
        line.startswith("-") and not line.startswith("---") for line in diff.splitlines()
    )
    return files, additions, deletions


def _write_patch(diff: str, output: str) -> Path:
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(diff)
    return path


def _open_patch(path: Path) -> bool:
    configured = (
        os.environ.get("PORTAGE_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR")
    )
    command = shlex.split(configured) if configured else []
    if not command:
        for candidate in ("code", "cursor", "zed"):
            if shutil.which(candidate):
                command = [candidate]
                break
    if not command:
        return False
    subprocess.Popen([*command, str(path)])  # noqa: S603 - explicit user-requested editor
    return True


def _print_diff(
    diff: str, job_id: str, *, output: str = "", open_editor: bool = False, stat_only: bool = False
) -> int:
    files, additions, deletions = _diff_stats(diff)
    path = _write_patch(diff, output) if output else None
    summary = Text.assemble(
        (f"{files} files", "bold"), "  ", (f"+{additions}", "green"), "  ", (f"−{deletions}", "red")
    )
    if path:
        summary.append(f"  saved to {path}", style="dim")
    console.print(
        Panel(summary, title=f"[bold]Portage diff · {job_id[:8]}[/]", border_style="bright_black")
    )
    if not stat_only and not output:
        console.print(Syntax(diff or "(empty diff)", "diff", theme="ansi_dark", word_wrap=False))
    if open_editor:
        path = path or _write_patch(diff, f".portage/portage-{job_id[:8]}.patch")
        if _open_patch(path):
            console.print(f"[green]Opened[/] {path}")
        else:
            return _die("No editor command found. Set PORTAGE_EDITOR, VISUAL, or EDITOR.")
    if path:
        console.print(f"\n[dim]Safe handoff:[/] git apply --check {shlex.quote(str(path))}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    with _client(args.api) as client:
        report = _report(client, args.job_id)
        if report is None:
            return _die("No report for this job yet")
        return _print_diff(
            report.get("diff") or "",
            args.job_id,
            output=args.output,
            open_editor=args.open,
            stat_only=args.stat,
        )


def cmd_report(args: argparse.Namespace) -> int:
    with _client(args.api) as client:
        report = _report(client, args.job_id)
        if report is None:
            return _die("No report for this job yet")
        if args.diff:
            return _print_diff(report.get("diff") or "", args.job_id)
        if args.json:
            console.print_json(json.dumps(report))
            return 0
        facts = Table.grid(padding=(0, 2))
        facts.add_column(style="dim", width=20)
        facts.add_column()
        facts.add_row("Migration outcome", _status_text(report.get("migration_outcome", "unknown")))
        facts.add_row(
            "Tasks", f"{report.get('tasks_done', 0)}/{report.get('tasks_total', 0)} complete"
        )
        facts.add_row("Full suite", _fmt_tests(report.get("test_summary")))
        facts.add_row("Recovery", f"{report.get('recovery', {}).get('visits', 0)} visits")
        facts.add_row(
            "Oracle integrity",
            f"{report.get('oracle_integrity', {}).get('integrity_rate', 0) * 100:.0f}%",
        )
        facts.add_row("LLM cost", f"${report.get('llm_usage', {}).get('cost_usd', 0):.3f}")
        console.print(
            Panel(facts, title=f"[bold]Report · {args.job_id[:8]}[/]", border_style="bright_black")
        )
        console.print(f"[dim]Machine-readable:[/] portage report {args.job_id} --json")
        patch_name = f"portage-{args.job_id[:8]}.patch"
        console.print(f"[dim]Review patch:[/]    portage diff {args.job_id} --output {patch_name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="portage", description="Portage — autonomous code migration from your terminal."
    )
    parser.add_argument(
        "--api",
        default=_DEFAULT_API,
        help=f"control-plane URL (default: {_DEFAULT_API}; env PORTAGE_API)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="submit a migration and optionally watch it live")
    migrate.add_argument("repo", help="git URL or path visible to the worker")
    migrate.add_argument("--recipe", default="flask_to_fastapi")
    migrate.add_argument("--ref", default="", help="pin a commit SHA for reproducibility")
    migrate.add_argument("--subdir", default="", help="subdirectory containing the app")
    migrate.add_argument(
        "--watch",
        action="store_true",
        help="render live progress and exit 0 only on honest success",
    )
    migrate.add_argument("--poll", type=float, default=2.0, help=argparse.SUPPRESS)
    migrate.set_defaults(fn=cmd_migrate)

    status = sub.add_parser("status", help="show outcome, tests, tasks, recovery, and next steps")
    status.add_argument("job_id")
    status.set_defaults(fn=cmd_status)

    jobs = sub.add_parser("jobs", help="list recent runs")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.set_defaults(fn=cmd_jobs)

    report = sub.add_parser("report", help="show the structured run report")
    report.add_argument("job_id")
    report.add_argument(
        "--json", action="store_true", help="emit the complete machine-readable JSON"
    )
    report.add_argument(
        "--diff", action="store_true", help="show the patch (compatibility alias for diff)"
    )
    report.set_defaults(fn=cmd_report)

    diff = sub.add_parser("diff", help="inspect, export, or open a run's patch")
    diff.add_argument("job_id")
    diff.add_argument("--output", "-o", default="", help="save the patch to this path")
    diff.add_argument(
        "--open", action="store_true", help="open the patch in PORTAGE_EDITOR or an available IDE"
    )
    diff.add_argument("--stat", action="store_true", help="show the change summary only")
    diff.set_defaults(fn=cmd_diff)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return _die(f"Cannot reach {args.api}. Is `docker compose up` running?")
    except RuntimeError as exc:
        return _die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
