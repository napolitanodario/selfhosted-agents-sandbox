"""Sessione di sandbox usata dall'harness.

Il resto del progetto dipende solo da ``SandboxSession``. L'SDK OpenSandbox
viene importato quando si apre una sessione vera, così i test del harness
non hanno bisogno del server.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from agent_harness.models import CommandResult, Task


@dataclass(frozen=True)
class Settings:
    domain: str = "localhost:8080"
    protocol: str = "http"
    api_key: str | None = None
    use_server_proxy: bool = True
    request_timeout_seconds: int = 600


class SandboxSession(Protocol):
    id: str

    async def write_text(self, path: str, data: str, mode: int = 644) -> None: ...

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: timedelta | None,
    ) -> CommandResult: ...

    async def destroy(self) -> None: ...


class SandboxFactory(Protocol):
    async def open(self, task: Task, env: dict[str, str]) -> SandboxSession: ...


def load_settings(environ: dict[str, str]) -> Settings:
    api_key = environ.get("SANDBOX_API_KEY") or environ.get("OPEN_SANDBOX_API_KEY") or None
    if api_key is not None and not api_key.strip():
        api_key = None
    timeout = environ.get("SANDBOX_REQUEST_TIMEOUT_SECONDS", "600")
    try:
        timeout_seconds = int(timeout)
    except ValueError as exc:
        raise ValueError("SANDBOX_REQUEST_TIMEOUT_SECONDS deve essere un intero") from exc
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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def open(self, task: Task, env: dict[str, str]) -> SandboxSession:
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
    def __init__(self, sandbox) -> None:
        self._sandbox = sandbox
        self.id = str(sandbox.id)

    async def write_text(self, path: str, data: str, mode: int = 644) -> None:
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
        from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts

        async def on_stdout(message) -> None:
            sys.stdout.write(message.text)
            sys.stdout.flush()

        async def on_stderr(message) -> None:
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
        await self._sandbox.destroy()


def execution_to_result(command: str, execution) -> CommandResult:
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
    return "".join(getattr(message, "text", str(message)) for message in messages)
