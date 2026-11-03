"""Task, agent-plan, and run-report models shared by the harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


class HarnessError(Exception):
    """Configuration or execution error shown to the user."""


@dataclass(frozen=True)
class Check:
    """A command that decides whether a task passed after the agent runs."""

    name: str
    command: str
    expect_exit_code: int = 0
    expect_stdout_contains: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class AgentSpec:
    """Agent declaration as written in a task file, before provider defaults."""

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
    """One programming task loaded from disk, including its workspace files."""

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
    """Concrete commands to run in the sandbox, plus env and an optional login file."""

    kind: str
    provider: str | None
    install: tuple[str, ...]
    commands: tuple[str, ...]
    env: dict[str, str]
    missing: tuple[str, ...]
    stop_on_failure: bool
    auth_file: str | None = None


@dataclass
class CommandResult:
    """Stdout, stderr, and exit status of one sandbox command."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    error: str | None = None
    streamed: bool = False


@dataclass
class StepRecord:
    """One install or agent command stored in the run report."""

    phase: str
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    error: str | None = None


@dataclass
class CheckRecord:
    """One verification command and whether it matched the task expectations."""

    name: str
    command: str
    exit_code: int
    passed: bool
    detail: str
    stdout: str
    stderr: str


@dataclass
class RunReport:
    """Outcome of a single task run, including steps, checks, and the report path."""

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
        """Return a JSON-ready copy with timestamps as ISO strings."""
        payload = asdict(self)
        payload["started_at"] = self.started_at.isoformat()
        payload["finished_at"] = self.finished_at.isoformat()
        return payload
