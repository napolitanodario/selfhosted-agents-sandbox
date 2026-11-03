"""Sandbox session used by the harness.

The rest of the project depends only on ``SandboxSession``. The OpenSandbox
SDK is imported when a real session is opened, so harness tests do not need
the server.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from agent_harness.models import CommandResult, Task


@dataclass(frozen=True)
class Settings:
    """Connection settings for the local OpenSandbox server."""

    domain: str = "localhost:8080"
    protocol: str = "http"
    api_key: str | None = None
    use_server_proxy: bool = True
    request_timeout_seconds: int = 600


class SandboxSession(Protocol):
    """Files and commands inside one sandbox, plus a way to destroy it."""

    id: str

    async def write_text(self, path: str, data: str, mode: int = 644) -> None:
        """Write a text file at an absolute path inside the sandbox."""

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: timedelta | None,
    ) -> CommandResult:
        """Run a shell command in ``cwd`` and return its output."""

    async def destroy(self) -> None:
        """Terminate the remote sandbox and release local client resources."""


class SandboxFactory(Protocol):
    """Opens a sandbox session for one task."""

    async def open(self, task: Task, env: dict[str, str]) -> SandboxSession:
        """Create a sandbox for ``task`` with ``env`` injected into the container."""


def load_project_env() -> None:
    """Load the project ``.env`` into the process environment.

    Variables already set in the shell stay as they are. The file is found by
    walking up from the working directory and from this package until a
    ``pyproject.toml`` appears.
    """
    root = _project_root()
    if root is None:
        return
    path = root / ".env"
    if path.is_file():
        apply_env_file(path, os.environ)


def apply_env_file(path: Path, environ: dict[str, str]) -> None:
    """Set keys that are missing from ``environ`` using assignments in ``path``."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in environ:
            continue
        environ[key] = _unquote(value.strip())


def _project_root() -> Path | None:
    """Return the directory that contains ``pyproject.toml``, if one is found."""
    starts = [Path.cwd(), Path(__file__).resolve()]
    seen: set[Path] = set()
    for start in starts:
        for candidate in [start, *start.parents]:
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return None


def _unquote(value: str) -> str:
    """Remove one matching pair of single or double quotes around a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_settings(environ: dict[str, str]) -> Settings:
    """Read server domain, protocol, API key, and proxy flag from the environment."""
    api_key = environ.get("SANDBOX_API_KEY") or environ.get("OPEN_SANDBOX_API_KEY") or None
    if api_key is not None and not api_key.strip():
        api_key = None
    timeout = environ.get("SANDBOX_REQUEST_TIMEOUT_SECONDS", "600")
    try:
        timeout_seconds = int(timeout)
    except ValueError as exc:
        raise ValueError("SANDBOX_REQUEST_TIMEOUT_SECONDS must be an integer") from exc
    proxy = environ.get("SANDBOX_USE_SERVER_PROXY", "true").strip().lower()
    return Settings(
        domain=environ.get("SANDBOX_DOMAIN")
        or environ.get("OPEN_SANDBOX_DOMAIN")
        or "localhost:8080",
        protocol=environ.get("SANDBOX_PROTOCOL", "http"),
        api_key=api_key,
        use_server_proxy=proxy in {"1", "true", "yes", "on"},
        request_timeout_seconds=max(timeout_seconds, 1),
    )


def build_create_kwargs(task: Task, env: dict[str, str]) -> dict:
    """Build the keyword arguments ``Sandbox.create`` accepts for this task."""
    kwargs: dict = {
        "timeout": timedelta(minutes=task.timeout_minutes),
        "ready_timeout": timedelta(seconds=task.ready_timeout_seconds),
        "metadata": {
            "harness": "selfhosted-agents-sandbox",
            "task": task.id,
        },
    }
    if env:
        kwargs["env"] = env
    if task.resources:
        kwargs["resource"] = dict(task.resources)
    return kwargs


class OpenSandboxFactory:
    """Creates real OpenSandbox sessions from harness settings."""

    def __init__(self, settings: Settings) -> None:
        """Store the server connection settings used for every sandbox."""
        self.settings = settings

    async def open(self, task: Task, env: dict[str, str]) -> SandboxSession:
        """Create a sandbox from the task image and wrap it in a session."""
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig

        config_kwargs: dict = {
            "domain": self.settings.domain,
            "protocol": self.settings.protocol,
            "request_timeout": timedelta(seconds=self.settings.request_timeout_seconds),
            "use_server_proxy": self.settings.use_server_proxy,
        }
        if self.settings.api_key:
            config_kwargs["api_key"] = self.settings.api_key
        sandbox = await Sandbox.create(
            task.image,
            connection_config=ConnectionConfig(**config_kwargs),
            **build_create_kwargs(task, env),
        )
        return OpenSandboxSession(sandbox)


class OpenSandboxSession:
    """``SandboxSession`` backed by the OpenSandbox Python SDK."""

    def __init__(self, sandbox) -> None:
        """Keep the SDK sandbox and expose its id."""
        self._sandbox = sandbox
        self.id = str(sandbox.id)

    async def write_text(self, path: str, data: str, mode: int = 644) -> None:
        """Write a text file through the sandbox filesystem API."""
        from opensandbox.models.filesystem import WriteEntry

        await self._sandbox.files.write_files(
            [WriteEntry(path=path, data=data, mode=mode)]
        )

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: timedelta | None,
    ) -> CommandResult:
        """Run a shell command, streaming stdout and stderr to this process."""
        from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts

        async def on_stdout(message) -> None:
            """Write one stdout chunk from the sandbox to this process."""
            sys.stdout.write(message.text)
            sys.stdout.flush()

        async def on_stderr(message) -> None:
            """Write one stderr chunk from the sandbox to this process."""
            sys.stderr.write(message.text)
            sys.stderr.flush()

        execution = await self._sandbox.commands.run(
            command,
            handlers=ExecutionHandlers(on_stdout=on_stdout, on_stderr=on_stderr),
            opts=RunCommandOpts(working_directory=cwd, timeout=timeout),
        )
        result = execution_to_result(command, execution)
        result.streamed = True
        return result

    async def destroy(self) -> None:
        """Terminate the remote sandbox and close the local SDK client."""
        await self._sandbox.destroy()


def execution_to_result(command: str, execution) -> CommandResult:
    """Map an OpenSandbox execution object onto a ``CommandResult``."""
    logs = execution.logs
    stdout = _texts(getattr(logs, "stdout", []) or [])
    stderr = _texts(getattr(logs, "stderr", []) or [])
    error = getattr(execution, "error", None)
    error_text = None
    exit_code = getattr(execution, "exit_code", None)
    if error is not None:
        name = getattr(error, "name", None)
        value = getattr(error, "value", None)
        error_text = f"{name}: {value}" if name else str(error)
        if exit_code is None:
            exit_code = getattr(error, "exit_code", None)
    if exit_code is None:
        exit_code = 1 if error is not None else 0
    return CommandResult(
        command=command,
        exit_code=int(exit_code),
        stdout=stdout,
        stderr=stderr,
        error=error_text,
    )


def _texts(messages) -> str:
    """Join execution log messages into one string."""
    return "".join(getattr(message, "text", str(message)) for message in messages)
