"""Agent adapters that run inside the sandbox.

``scripted`` repeats commands declared in the task. It is enough for a smoke
test and for replaying a patch without a model. ``coding-cli`` installs a
coding-agent CLI in the image and hands it the task prompt.

Codex uses a ChatGPT login copied from ``secrets/codex/auth.json``. Claude
still uses an API key from the host environment.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_harness.models import AgentPlan, AgentSpec, HarnessError, Task

DEFAULT_CODEX_AUTH = "secrets/codex/auth.json"


@dataclass(frozen=True)
class _Provider:
    """Built-in coding CLI: install line, command, and host env to forward."""

    install: tuple[str, ...]
    command: str
    forward: tuple[str, ...]
    required_any: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()


def _claude_command(prompt_file: str) -> str:
    """Build the headless Claude Code command for a prompt file in the sandbox."""
    quoted = shlex.quote(prompt_file)
    return (
        f'claude -p "$(cat {quoted})" --output-format json '
        "--dangerously-skip-permissions"
    )


def _codex_command(prompt_file: str) -> str:
    """Build the headless Codex command for a prompt file in the sandbox."""
    quoted = shlex.quote(prompt_file)
    return (
        "codex exec --json --sandbox workspace-write --ask-for-approval never "
        f'--skip-git-repo-check "$(cat {quoted})"'
    )


def _providers(prompt_file: str) -> dict[str, _Provider]:
    """Return the built-in coding CLIs keyed by provider name."""
    return {
        "claude": _Provider(
            install=("npm i -g @anthropic-ai/claude-code@latest",),
            command=_claude_command(prompt_file),
            forward=(
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_MODEL",
            ),
            required_any=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        ),
        "codex": _Provider(
            install=("npm i -g @openai/codex@latest",),
            command=_codex_command(prompt_file),
            forward=(),
        ),
    }


def resolve_plan(task: Task, environ: dict[str, str]) -> AgentPlan:
    """Turn a task agent spec into install commands, the run command, and env."""
    prompt_file = f"{task.workdir.rstrip('/')}/.harness/prompt.md"
    spec = task.agent
    if spec.kind == "scripted":
        return AgentPlan(
            kind="scripted",
            provider=None,
            install=(),
            commands=spec.commands,
            env=_forward(spec.env, environ),
            missing=_missing(spec, environ),
            stop_on_failure=True,
        )
    if spec.kind != "coding-cli":
        raise HarnessError(f"unsupported agent: {spec.kind}")

    provider_name = spec.provider or "custom"
    known = _providers(prompt_file)
    provider = known.get(provider_name)
    install = spec.install if spec.install is not None else (provider.install if provider else ())
    command = spec.command or (provider.command if provider else None)
    if not command:
        raise HarnessError(f"{task.id}: agent command is missing")
    command = command.replace("{prompt_file}", shlex.quote(prompt_file))

    forward = spec.env
    required_any = spec.required_any
    required_env = spec.required_env
    if provider is not None:
        forward = tuple(dict.fromkeys((*provider.forward, *spec.env)))
        required_any = spec.required_any or provider.required_any
        required_env = tuple(dict.fromkeys((*provider.required_env, *spec.required_env)))

    merged = AgentSpec(
        kind=spec.kind,
        env=forward,
        required_env=required_env,
        required_any=required_any,
    )
    env = _forward(merged.env, environ)
    env.pop("OPENAI_API_KEY", None)
    env["IS_SANDBOX"] = "1"
    auth_file = None
    if provider_name == "codex":
        if environ.get("OPENAI_API_KEY"):
            raise HarnessError(
                "Unset OPENAI_API_KEY to use the ChatGPT login. "
                "Codex bills the API account when that variable is set."
            )
        auth_file = environ.get("CODEX_AUTH_FILE") or DEFAULT_CODEX_AUTH
    return AgentPlan(
        kind="coding-cli",
        provider=provider_name,
        install=tuple(install),
        commands=(command,),
        env=env,
        missing=_missing(merged, environ),
        stop_on_failure=False,
        auth_file=auth_file,
    )


def ensure_plan(plan: AgentPlan) -> None:
    """Raise when the host is missing credentials the agent needs."""
    if plan.missing:
        raise HarnessError(
            "missing environment variables for the agent: " + ", ".join(plan.missing)
        )
    if plan.auth_file is not None:
        read_chatgpt_auth(Path(plan.auth_file))


def project_root_for(task: Task) -> Path:
    """Find the repository root by walking up from the task file."""
    start = Path(task.source).resolve().parent
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


def sync_host_codex_auth(project_root: Path, source: Path | None = None) -> Path:
    """Copy the host Codex login into the project before a sandbox starts."""
    origin = source or (Path.home() / ".codex" / "auth.json")
    if not origin.is_file():
        raise HarnessError(
            f"Codex login not found at {origin}. Run `codex login` on this machine first."
        )
    destination_dir = project_root / "secrets" / "codex"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "auth.json"
    completed = subprocess.run(
        ["cp", "--", str(origin), str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise HarnessError(f"failed to copy Codex login into {destination}: {detail}")
    destination.chmod(0o600)
    return destination


def read_chatgpt_auth(path: Path) -> str:
    """Load a ChatGPT Codex login and drop any embedded API key.

    The returned JSON is what gets written into the sandbox. Removing
    ``OPENAI_API_KEY`` keeps the container on the ChatGPT plan.
    """
    if not path.is_file():
        raise HarnessError(
            f"Codex ChatGPT login not found: {path}. "
            "Copy ~/.codex/auth.json to secrets/codex/auth.json. Do not commit it."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError(f"{path} is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("auth_mode") != "chatgpt":
        raise HarnessError(f"{path} is not a ChatGPT Codex login")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        raise HarnessError(
            f"{path} has no ChatGPT refresh token. "
            'Sign in again with `codex login` after setting '
            'cli_auth_credentials_store = "file".'
        )
    data.pop("OPENAI_API_KEY", None)
    return json.dumps(data)


def _forward(names: tuple[str, ...], environ: dict[str, str]) -> dict[str, str]:
    """Copy named variables from the host, skipping names that are unset or empty."""
    return {name: environ[name] for name in names if environ.get(name)}


def _missing(spec: AgentSpec, environ: dict[str, str]) -> tuple[str, ...]:
    """List required variables that are absent. ``required_any`` counts as one group."""
    missing = [name for name in spec.required_env if not environ.get(name)]
    if spec.required_any and not any(environ.get(name) for name in spec.required_any):
        missing.append(" or ".join(spec.required_any))
    return tuple(missing)
