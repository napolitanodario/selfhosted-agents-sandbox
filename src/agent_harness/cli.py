"""Interfaccia a riga di comando dell'harness."""

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
from agent_harness.session import OpenSandboxFactory, load_settings


def main(argv: list[str] | None = None) -> int:
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
        print(f"errore: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrotto", file=sys.stderr)
        return 130
    raise HarnessError(f"comando sconosciuto: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-harness",
        description="Esegue task di programmazione in una sandbox OpenSandbox locale.",
    )
    parser.add_argument("--version", action="version", version=f"agent-harness {__version__}")
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=Path("tasks"),
        help="directory dei task (default: ./tasks)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("list", help="elenca i task")

    validate = subcommands.add_parser("validate", help="controlla i file di task")
    validate.add_argument("task_id", nargs="?", help="limita il controllo a un task")

    run = subcommands.add_parser("run", help="esegue un task o tutti i task")
    run.add_argument("task_id", nargs="?", help="id del task")
    run.add_argument("--all", action="store_true", help="esegue ogni task in sequenza")
    run.add_argument("--keep", action="store_true", help="non distrugge la sandbox a fine run")
    run.add_argument(
        "--report-dir",
        type=Path,
        default=Path("runs"),
        help="directory dei report JSON (default: ./runs)",
    )
    return parser


def _list(tasks_dir: Path) -> int:
    tasks = discover_tasks(tasks_dir)
    for task in tasks:
        agent = task.agent.kind
        if task.agent.provider:
            agent = f"{agent}:{task.agent.provider}"
        print(f"{task.id}\t{agent}\t{task.title}")
    return 0


def _validate(tasks_dir: Path, task_id: str | None) -> int:
    tasks = _select(tasks_dir, task_id)
    for task in tasks:
        files = len(task.files)
        print(f"ok\t{task.id}\t{files} file\t{task.image}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    if bool(args.task_id) == bool(args.all):
        raise HarnessError("indica un task_id oppure --all")
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
    if task_id is None:
        return discover_tasks(tasks_dir)
    path = tasks_dir / task_id / "task.yaml"
    return [load_task(path)]


if __name__ == "__main__":
    raise SystemExit(main())
