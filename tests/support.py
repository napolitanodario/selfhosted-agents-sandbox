"""Doppio della sandbox per i test dell'harness."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from agent_harness.models import CommandResult, Task


class FakeSession:
    def __init__(self, handler: Callable[[str], CommandResult] | None = None) -> None:
        self.id = "sbx-test"
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, str]] = []
        self.destroyed = False
        self.handler = handler

    async def write_text(self, path: str, data: str, mode: int = 644) -> None:
        del mode
        self.files[path] = data

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: timedelta | None,
    ) -> CommandResult:
        del timeout
        self.commands.append((command, cwd))
        if self.handler is not None:
            return self.handler(command)
        return CommandResult(command=command, exit_code=0, stdout="hello\n", stderr="")

    async def destroy(self) -> None:
        self.destroyed = True


class FakeFactory:
    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()
        self.env: dict[str, str] | None = None
        self.task: Task | None = None
        self.opened = 0

    async def open(self, task: Task, env: dict[str, str]) -> FakeSession:
        self.opened += 1
        self.task = task
        self.env = env
        return self.session
