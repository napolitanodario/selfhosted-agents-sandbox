"""Agent adapters that run inside the sandbox.

``scripted`` repeats commands declared in the task. It is enough for a smoke
test and for replaying a patch without a model. ``coding-cli`` installs a
coding-agent CLI in the image and hands it the task prompt.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from agent_harness.models import AgentPlan, AgentSpec, HarnessError, Task


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
            forward=("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"),
            required_env=("OPENAI_API_KEY",),
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
    env["IS_SANDBOX"] = "1"
    return AgentPlan(
        kind="coding-cli",
        provider=provider_name,
        install=tuple(install),
        commands=(command,),
        env=env,
        missing=_missing(merged, environ),
        stop_on_failure=False,
    )


def ensure_plan(plan: AgentPlan) -> None:
    """Raise when the host is missing an environment variable the agent needs."""
    if plan.missing:
        raise HarnessError(
            "missing environment variables for the agent: " + ", ".join(plan.missing)
        )


def _forward(names: tuple[str, ...], environ: dict[str, str]) -> dict[str, str]:
    """Copy named variables from the host, skipping names that are unset or empty."""
    return {name: environ[name] for name in names if environ.get(name)}


def _missing(spec: AgentSpec, environ: dict[str, str]) -> tuple[str, ...]:
    """List required variables that are absent. ``required_any`` counts as one group."""
    missing = [name for name in spec.required_env if not environ.get(name)]
    if spec.required_any and not any(environ.get(name) for name in spec.required_any):
        missing.append(" or ".join(spec.required_any))
    return tuple(missing)
