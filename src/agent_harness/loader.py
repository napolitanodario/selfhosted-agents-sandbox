"""Load tasks from ``tasks/<id>/task.yaml``."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

from agent_harness.models import AgentSpec, Check, HarnessError, Task

_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")
_SKIP_PARTS = {"__pycache__", ".git"}


def discover_tasks(tasks_dir: Path) -> list[Task]:
    """Load every ``task.yaml`` in an immediate child of ``tasks_dir``."""
    if not tasks_dir.is_dir():
        raise HarnessError(f"task directory not found: {tasks_dir}")
    paths = sorted(tasks_dir.glob("*/task.yaml"))
    if not paths:
        raise HarnessError(f"no tasks in {tasks_dir} (expected tasks/<id>/task.yaml)")
    tasks = [load_task(path) for path in paths]
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise HarnessError(f"duplicate task ids: {', '.join(duplicates)}")
    return tasks


def load_task(path: Path) -> Task:
    """Parse one task file and read its workspace into memory."""
    if not path.is_file():
        raise HarnessError(f"task file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HarnessError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise HarnessError(f"{path}: the document must be a mapping")

    task_id = _require_str(raw, "id", path)
    if task_id != path.parent.name:
        raise HarnessError(
            f"{path}: id '{task_id}' does not match directory '{path.parent.name}'"
        )
    if not task_id or any(char not in _ID_CHARS for char in task_id):
        raise HarnessError(
            f"{path}: id '{task_id}' is invalid (use a-z, 0-9, and hyphen only)"
        )

    workspace = _workspace(raw, path)
    inline_files = _inline_files(raw.get("files", []), path)
    agent = _agent(raw.get("agent"), path)
    checks = _checks(raw.get("checks", []), path)
    resources = _resources(raw.get("resources", {}), path)

    return Task(
        id=task_id,
        title=_optional_str(raw, "title") or task_id,
        description=_optional_str(raw, "description"),
        image=_optional_str(raw, "image") or "python:3.12",
        workdir=_workdir(raw, path),
        timeout_minutes=_positive_int(raw, "timeout_minutes", 15, path),
        ready_timeout_seconds=_positive_int(raw, "ready_timeout_seconds", 180, path),
        command_timeout_seconds=_positive_int(raw, "command_timeout_seconds", 120, path),
        resources=resources,
        agent=agent,
        checks=checks,
        prompt=_optional_str(raw, "prompt"),
        source=str(path),
        workspace=str(workspace) if workspace is not None else None,
        files=tuple(collect_workspace_files(workspace) + inline_files),
    )


def collect_workspace_files(workspace: Path | None) -> list[tuple[str, str]]:
    """Read workspace text files as ``(relative posix path, contents)`` pairs."""
    if workspace is None:
        return []
    files: list[tuple[str, str]] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if _SKIP_PARTS.intersection(path.parts) or path.suffix == ".pyc":
            continue
        relative = path.relative_to(workspace).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise HarnessError(f"{path}: the workspace accepts text files only") from exc
        files.append((relative, text))
    return files


def _workdir(raw: dict, path: Path) -> str:
    """Return the absolute sandbox workdir, defaulting to ``/workspace``."""
    workdir = _optional_str(raw, "workdir") or "/workspace"
    if not workdir.startswith("/") or ".." in PurePosixPath(workdir).parts:
        raise HarnessError(f"{path}: workdir must be an absolute path inside the sandbox")
    return workdir.rstrip("/") or "/"


def _workspace(raw: dict, path: Path) -> Path | None:
    """Resolve the host directory copied into the sandbox, if one exists."""
    if "workspace" in raw and raw["workspace"] is not None:
        workspace = (path.parent / str(raw["workspace"])).resolve()
    else:
        default = path.parent / "workspace"
        if not default.exists():
            return None
        workspace = default.resolve()
    if not workspace.is_dir():
        raise HarnessError(f"{path}: workspace not found: {workspace}")
    return workspace


def _agent(raw: object, path: Path) -> AgentSpec:
    """Parse the ``agent`` mapping into an ``AgentSpec``."""
    if not isinstance(raw, dict):
        raise HarnessError(f"{path}: 'agent' must be a mapping")
    kind = raw.get("kind")
    if kind not in {"scripted", "coding-cli"}:
        raise HarnessError(f"{path}: agent.kind must be 'scripted' or 'coding-cli'")

    commands = _string_list(raw.get("commands", []), path, "agent.commands")
    install = raw.get("install")
    install_commands = None if install is None else tuple(_string_list(install, path, "agent.install"))
    env = tuple(_string_list(raw.get("env", []), path, "agent.env"))
    required_env = tuple(_string_list(raw.get("required_env", []), path, "agent.required_env"))
    required_any = tuple(_string_list(raw.get("required_any", []), path, "agent.required_any"))
    command = raw.get("command")
    if command is not None and not isinstance(command, str):
        raise HarnessError(f"{path}: agent.command must be a string")
    provider = raw.get("provider")
    if provider is not None and not isinstance(provider, str):
        raise HarnessError(f"{path}: agent.provider must be a string")

    if kind == "scripted" and not commands:
        raise HarnessError(f"{path}: a scripted agent requires agent.commands")
    if kind == "coding-cli":
        provider = provider or "custom"
        if provider not in {"claude", "codex", "custom"}:
            raise HarnessError(f"{path}: unknown provider '{provider}'")
        if provider == "custom" and not command:
            raise HarnessError(f"{path}: the custom provider requires agent.command")

    return AgentSpec(
        kind=kind,
        commands=tuple(commands),
        provider=provider,
        command=command,
        install=install_commands,
        env=env,
        required_env=required_env,
        required_any=required_any,
    )


def _checks(raw: object, path: Path) -> tuple[Check, ...]:
    """Parse the ``checks`` list. An omitted list means the task has no checks."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise HarnessError(f"{path}: 'checks' must be a list")
    checks: list[Check] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise HarnessError(f"{path}: checks[{index}] must be a mapping")
        name = item.get("name") or f"check-{index}"
        command = item.get("command")
        if not isinstance(name, str) or not isinstance(command, str) or not command.strip():
            raise HarnessError(f"{path}: checks[{index}] requires name and command")
        expect = item.get("expect_exit_code", 0)
        if not isinstance(expect, int):
            raise HarnessError(f"{path}: checks[{index}].expect_exit_code must be an integer")
        contains = item.get("expect_stdout_contains")
        if contains is not None and not isinstance(contains, str):
            raise HarnessError(
                f"{path}: checks[{index}].expect_stdout_contains must be a string"
            )
        timeout = item.get("timeout_seconds")
        if timeout is not None and (not isinstance(timeout, int) or timeout < 1):
            raise HarnessError(
                f"{path}: checks[{index}].timeout_seconds must be a positive integer"
            )
        checks.append(
            Check(
                name=name,
                command=command,
                expect_exit_code=expect,
                expect_stdout_contains=contains,
                timeout_seconds=timeout,
            )
        )
    return tuple(checks)


def _inline_files(raw: object, path: Path) -> list[tuple[str, str]]:
    """Parse inline ``files`` entries and reject paths that leave the workspace."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HarnessError(f"{path}: 'files' must be a list")
    files: list[tuple[str, str]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise HarnessError(f"{path}: files[{index}] must be a mapping")
        relative = item.get("path")
        content = item.get("content", "")
        if not isinstance(relative, str) or not relative.strip():
            raise HarnessError(f"{path}: files[{index}].path is required")
        if not isinstance(content, str):
            raise HarnessError(f"{path}: files[{index}].content must be text")
        normalized = Path(relative)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise HarnessError(f"{path}: files[{index}].path must stay inside the workspace")
        files.append((normalized.as_posix(), content))
    return files


def _resources(raw: object, path: Path) -> dict[str, str]:
    """Parse resource limits and coerce every value to a string."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HarnessError(f"{path}: 'resources' must be a mapping")
    return {str(key): str(value) for key, value in raw.items()}


def _require_str(raw: dict, key: str, path: Path) -> str:
    """Return a required non-empty string field."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{path}: '{key}' is required")
    return value.strip()


def _optional_str(raw: dict, key: str) -> str:
    """Return a string field, or an empty string when the key is absent."""
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HarnessError(f"'{key}' must be a string")
    return value.strip()


def _positive_int(raw: dict, key: str, default: int, path: Path) -> int:
    """Return a positive integer field, or ``default`` when the key is absent."""
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, int) or value < 1:
        raise HarnessError(f"{path}: '{key}' must be a positive integer")
    return value


def _string_list(raw: object, path: Path, label: str) -> list[str]:
    """Return a list of non-empty strings, or an empty list when ``raw`` is null."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise HarnessError(f"{path}: '{label}' must be a list of strings")
    return [item.strip() for item in raw]
