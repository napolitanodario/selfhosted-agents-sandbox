"""In-memory sandbox double used by the harness tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from agent_harness.models import CommandResult, Task


class FakeSession:
    """Records files and commands instead of talking to OpenSandbox."""

    def __init__(self, handler: Callable[[str], CommandResult] | None = None) -> None:
        """Store an optional command handler. The default result exits 0."""
        self.id = "sbx-test"
        self.files: dict[str, str] = {}
        self.modes: dict[str, int] = {}
        self.commands: list[tuple[str, str]] = []
        self.destroyed = False
        self.handler = handler

    async def write_text(self, path: str, data: str, mode: int = 644) -> None:
        """Remember a file write."""
        self.files[path] = data
        self.modes[path] = mode

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: timedelta | None,
    ) -> CommandResult:
        """Record a command and return the handler result, or a passing default."""
        del timeout
        self.commands.append((command, cwd))
        if self.handler is not None:
            return self.handler(command)
        return CommandResult(command=command, exit_code=0, stdout="hello\n", stderr="")

    async def destroy(self) -> None:
        """Mark the session destroyed."""
        self.destroyed = True


class FakeFactory:
    """Returns one fake session and records the env passed at open time."""

    def __init__(self, session: FakeSession | None = None) -> None:
        """Use the given session, or a new default session."""
        self.session = session or FakeSession()
        self.env: dict[str, str] | None = None
        self.task: Task | None = None
        self.opened = 0

    async def open(self, task: Task, env: dict[str, str]) -> FakeSession:
        """Count the open call and return the stored session."""
        self.opened += 1
        self.task = task
        self.env = env
        return self.session
