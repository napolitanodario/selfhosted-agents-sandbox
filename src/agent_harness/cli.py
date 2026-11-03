"""Command-line interface for the harness."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from agent_harness import __version__
from agent_harness.harness import run_task
from agent_harness.loader import discover_tasks, load_task
from agent_harness.models import HarnessError, Task
from agent_harness.session import OpenSandboxFactory, load_project_env, load_settings


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run list, validate, or run. Return a process exit code."""
    load_project_env()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _list(args.tasks_dir)
        if args.command == "validate":
            return _validate(args.tasks_dir, args.task_id)
        if args.command == "run":
            return asyncio.run(_run(args))
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    raise HarnessError(f"unknown command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser for the three harness subcommands."""
    parser = argparse.ArgumentParser(
        prog="agent-harness",
        description="Run programming tasks in a local OpenSandbox sandbox.",
    )
    parser.add_argument("--version", action="version", version=f"agent-harness {__version__}")
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=Path("tasks"),
        help="task directory (default: ./tasks)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("list", help="list tasks")

    validate = subcommands.add_parser("validate", help="check task files")
    validate.add_argument("task_id", nargs="?", help="limit the check to one task")

    run = subcommands.add_parser("run", help="run one task or every task")
    run.add_argument("task_id", nargs="?", help="task id")
    run.add_argument("--all", action="store_true", help="run every task in order")
    run.add_argument("--keep", action="store_true", help="do not destroy the sandbox at the end")
    run.add_argument(
        "--report-dir",
        type=Path,
        default=Path("runs"),
        help="directory for JSON reports (default: ./runs)",
    )
    return parser


def _list(tasks_dir: Path) -> int:
    """Print id, agent, and title for every task. Return 0."""
    tasks = discover_tasks(tasks_dir)
    for task in tasks:
        agent = task.agent.kind
        if task.agent.provider:
            agent = f"{agent}:{task.agent.provider}"
        print(f"{task.id}\t{agent}\t{task.title}")
    return 0


def _validate(tasks_dir: Path, task_id: str | None) -> int:
    """Load the selected tasks and print a one-line summary for each."""
    tasks = _select(tasks_dir, task_id)
    for task in tasks:
        count = len(task.files)
        noun = "file" if count == 1 else "files"
        print(f"ok\t{task.id}\t{count} {noun}\t{task.image}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    """Run the selected tasks against the configured OpenSandbox server."""
    if bool(args.task_id) == bool(args.all):
        raise HarnessError("pass a task_id or --all")
    tasks = _select(args.tasks_dir, None if args.all else args.task_id)
    settings = load_settings(dict(os.environ))
    factory = OpenSandboxFactory(settings)
    failed = False
    for task in tasks:
        report = await run_task(
            task,
            factory=factory,
            report_dir=args.report_dir,
            keep=args.keep,
            environ=dict(os.environ),
        )
        if report.status != "passed":
            failed = True
    return 1 if failed else 0


def _select(tasks_dir: Path, task_id: str | None) -> list[Task]:
    """Load every task, or the single task whose directory name is ``task_id``."""
    if task_id is None:
        return discover_tasks(tasks_dir)
    path = tasks_dir / task_id / "task.yaml"
    return [load_task(path)]


if __name__ == "__main__":
    raise SystemExit(main())
