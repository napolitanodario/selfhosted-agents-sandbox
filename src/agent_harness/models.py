"""Modelli del task, del piano agente e del report di esecuzione."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


class HarnessError(Exception):
    """Errore di configurazione o di esecuzione mostrato all'utente."""


@dataclass(frozen=True)
class Check:
    name: str
    command: str
    expect_exit_code: int = 0
    expect_stdout_contains: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class AgentSpec:
    kind: str
    commands: tuple[str, ...] = ()
    provider: str | None = None
    command: str | None = None
    install: tuple[str, ...] | None = None
    env: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    description: str
    image: str
    workdir: str
    timeout_minutes: int
    ready_timeout_seconds: int
    command_timeout_seconds: int
    resources: dict[str, str]
    agent: AgentSpec
    checks: tuple[Check, ...]
    prompt: str
    source: str
    workspace: str | None
    files: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AgentPlan:
    """Comandi concreti da eseguire dentro la sandbox, più le env da iniettare."""

    kind: str
    provider: str | None
    install: tuple[str, ...]
    commands: tuple[str, ...]
    env: dict[str, str]
    missing: tuple[str, ...]
    stop_on_failure: bool


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    error: str | None = None
    streamed: bool = False


@dataclass
class StepRecord:
    phase: str
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    error: str | None = None


@dataclass
class CheckRecord:
    name: str
    command: str
    exit_code: int
    passed: bool
    detail: str
    stdout: str
    stderr: str


@dataclass
class RunReport:
    task_id: str
    title: str
    image: str
    agent: str
    sandbox_id: str | None
    status: str
    started_at: datetime
    finished_at: datetime
    steps: list[StepRecord] = field(default_factory=list)
    checks: list[CheckRecord] = field(default_factory=list)
    error: str | None = None
    report_path: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["started_at"] = self.started_at.isoformat()
        payload["finished_at"] = self.finished_at.isoformat()
        return payload
