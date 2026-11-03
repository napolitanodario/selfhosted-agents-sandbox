"""Ciclo dell'harness: sandbox, seed, agente, verifiche, report."""

from __future__ import annotations

import json
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from agent_harness.agents import ensure_plan, resolve_plan
from agent_harness.models import (
    Check,
    CheckRecord,
    CommandResult,
    HarnessError,
    RunReport,
    StepRecord,
    Task,
)
from agent_harness.session import SandboxFactory, SandboxSession


async def run_task(
    task: Task,
    *,
    factory: SandboxFactory,
    report_dir: Path,
    keep: bool = False,
    environ: dict[str, str] | None = None,
) -> RunReport:
    plan = resolve_plan(task, environ if environ is not None else {})
    ensure_plan(plan)
    _check_timeouts(task)

    started = datetime.now(timezone.utc)
    session: SandboxSession | None = None
    steps: list[StepRecord] = []
    checks: list[CheckRecord] = []
    error: str | None = None
    status = "error"

    try:
        print(f"==> {task.id}: creo la sandbox ({task.image})", flush=True)
        session = await factory.open(task, plan.env)
        print(f"    sandbox {session.id}", flush=True)
        await _seed(session, task)
        for index, command in enumerate(plan.install, start=1):
            result = await _run_command(
                session,
                command,
                cwd=task.workdir,
                timeout=_timeout(task, None),
                label=f"install {index}",
            )
            steps.append(_step("install", f"install-{index}", result))
            if result.exit_code != 0:
                raise HarnessError(f"installazione dell'agente fallita ({result.exit_code})")
        for index, command in enumerate(plan.commands, start=1):
            result = await _run_command(
                session,
                command,
                cwd=task.workdir,
                timeout=_timeout(task, None),
                label=f"agent {index}",
            )
            steps.append(_step("agent", f"agent-{index}", result))
            if result.exit_code != 0 and plan.stop_on_failure:
                break
        for check in task.checks:
            result = await _run_command(
                session,
                check.command,
                cwd=task.workdir,
                timeout=_timeout(task, check.timeout_seconds),
                label=f"check {check.name}",
            )
            record = _check_record(check, result)
            checks.append(record)
            mark = "pass" if record.passed else "fail"
            print(f"[{mark}] {check.name}: {record.detail}", flush=True)
        status = _status(task, steps, checks)
    except Exception as exc:
        error = str(exc)
        status = "error"
        print(f"errore: {error}", file=sys.stderr, flush=True)
    finally:
        if session is not None and not keep:
            try:
                await session.destroy()
                print(f"    sandbox {session.id} distrutta", flush=True)
            except Exception as exc:
                message = f"distruzione della sandbox fallita: {exc}"
                print(f"avviso: {message}", file=sys.stderr, flush=True)
                if error is None:
                    error = message
                    status = "error"
        elif session is not None and keep:
            print(f"    sandbox {session.id} lasciata attiva (--keep)", flush=True)

    finished = datetime.now(timezone.utc)
    report = RunReport(
        task_id=task.id,
        title=task.title,
        image=task.image,
        agent=plan.kind if plan.provider is None else f"{plan.kind}:{plan.provider}",
        sandbox_id=None if session is None else session.id,
        status=status,
        started_at=started,
        finished_at=finished,
        steps=steps,
        checks=checks,
        error=error,
    )
    report.report_path = str(_write_report(report, report_dir))
    print(f"[{status}] report {report.report_path}", flush=True)
    return report


def _check_timeouts(task: Task) -> None:
    budget = task.timeout_minutes * 60
    if task.command_timeout_seconds > budget:
        raise HarnessError(
            f"{task.id}: command_timeout_seconds supera timeout_minutes della sandbox"
        )


async def _seed(session: SandboxSession, task: Task) -> None:
    prompt = task.prompt or task.description or task.title
    files = list(task.files)
    files.append((".harness/prompt.md", prompt + "\n"))
    files.append(
        (
            ".harness/task.json",
            json.dumps({"id": task.id, "title": task.title}, indent=2) + "\n",
        )
    )

    workdir = task.workdir.rstrip("/") or "/"
    directories = {workdir}
    for relative, _content in files:
        parent = PurePosixPath(relative).parent.as_posix()
        if parent != ".":
            directories.add(f"{workdir}/{parent}")
    quoted = " ".join(shlex.quote(directory) for directory in sorted(directories))
    created = await _run_command(
        session,
        f"mkdir -p {quoted}",
        cwd="/",
        timeout=timedelta(seconds=30),
        label="seed",
    )
    if created.exit_code != 0:
        raise HarnessError(f"creazione delle directory fallita: {created.stderr or created.error}")
    for relative, content in files:
        await session.write_text(f"{workdir}/{relative}", content)


async def _run_command(
    session: SandboxSession,
    command: str,
    *,
    cwd: str,
    timeout: timedelta | None,
    label: str,
) -> CommandResult:
    print(f"\n$ [{label}] {command}", flush=True)
    result = await session.run(command, cwd=cwd, timeout=timeout)
    if not result.streamed:
        if result.stdout:
            print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, flush=True)
    if result.error:
        print(f"error: {result.error}", file=sys.stderr, flush=True)
    print(f"    exit {result.exit_code}", flush=True)
    return result


def _timeout(task: Task, override: int | None) -> timedelta:
    return timedelta(seconds=override or task.command_timeout_seconds)


def _step(phase: str, name: str, result: CommandResult) -> StepRecord:
    return StepRecord(
        phase=phase,
        name=name,
        command=result.command,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        error=result.error,
    )


def _check_record(check: Check, result: CommandResult) -> CheckRecord:
    reasons: list[str] = []
    if result.exit_code != check.expect_exit_code:
        reasons.append(f"exit {result.exit_code}, atteso {check.expect_exit_code}")
    if check.expect_stdout_contains and check.expect_stdout_contains not in result.stdout:
        reasons.append(f"stdout senza {check.expect_stdout_contains!r}")
    passed = not reasons
    detail = "ok" if passed else "; ".join(reasons)
    return CheckRecord(
        name=check.name,
        command=result.command,
        exit_code=result.exit_code,
        passed=passed,
        detail=detail,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _status(task: Task, steps: list[StepRecord], checks: list[CheckRecord]) -> str:
    if task.checks:
        return "passed" if checks and all(item.passed for item in checks) else "failed"
    agent_steps = [step for step in steps if step.phase == "agent"]
    if agent_steps and all(step.exit_code == 0 for step in agent_steps):
        return "passed"
    return "failed"


def _write_report(report: RunReport, report_dir: Path) -> Path:
    stamp = report.started_at.strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"{stamp}-{report.task_id}" / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path
