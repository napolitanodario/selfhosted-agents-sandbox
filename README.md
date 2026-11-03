# Self-hosted agents sandbox

Local harness for programming tasks inside [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox). Each task starts in an isolated container: the harness copies the workspace, starts an agent, runs the checks, and destroys the sandbox.

This repository is a skeleton. The `scripted` agent is enough to exercise the loop without API keys. The `coding-cli` agent starts Claude Code or Codex inside the sandbox.

## Run loop

1. Read `tasks/<id>/task.yaml` and the files in `workspace/`.
2. Ask OpenSandbox for a container that uses the task image.
3. Create the workdir, copy the files, and write `.harness/prompt.md`.
4. Run the agent.
5. Run the checks. Their result decides whether the task passed.
6. Save `runs/<timestamp>-<id>/report.json` and destroy the sandbox.

## Requirements

- Docker, used by OpenSandbox for local execution
- Python 3.11+
- An OpenSandbox server, usually listening on `localhost:8080`

## Local server

```bash
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server
```

The health check answers at `http://127.0.0.1:8080/health`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Load the variables before a real run:

```bash
set -a
source .env
set +a
```

`SANDBOX_API_KEY` is needed only when the local server has authentication enabled. `SANDBOX_USE_SERVER_PROXY=true` routes commands and files through the server. That is the OpenSandbox quick start setting for a client that cannot reach container addresses.

## Commands

```bash
agent-harness list
agent-harness validate
agent-harness run hello
agent-harness run scripted-fix
agent-harness run --all
```

`hello` and `scripted-fix` do not call a model. `fix-fizzbuzz` uses Claude Code and needs `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, plus an image that includes Node.js:

```bash
docker pull opensandbox/code-interpreter:v1.1.0
agent-harness run fix-fizzbuzz
```

`--keep` leaves the sandbox running after the run. Reports stay in `runs/`, and that directory is not versioned.

The exit code is `0` when every executed task passes, `1` when a check fails or the run errors, and `2` when the task or the configuration is invalid.

## Tasks

A task is a directory with `task.yaml` and, usually, `workspace/`.

```yaml
id: hello
title: Greeting from the sandbox
image: python:3.12
workdir: /workspace
timeout_minutes: 10
command_timeout_seconds: 60
agent:
  kind: scripted
  commands:
    - python main.py
checks:
  - name: output
    command: python main.py
    expect_stdout_contains: hello
```

`command_timeout_seconds` must fit inside `timeout_minutes`. The first limits a single command. The second is the sandbox TTL. `resources` accepts OpenSandbox limits (`cpu`, `memory`) and is forwarded as given.

`workspace` may point at another directory, as `scripted-fix` does with the `fix-fizzbuzz` workspace. Entries under `files` are inline text and override workspace files with the same path.

Checks accept `expect_exit_code` (default `0`) and `expect_stdout_contains`. With no checks, the task passes when every agent command exits `0`.

## Agents

`scripted` runs `agent.commands` in the workdir. It stops at the first non-zero command and still runs the checks.

`coding-cli` installs a CLI in the container and passes it the prompt.

| `provider` | Install | Required variables |
| --- | --- | --- |
| `claude` | `npm i -g @anthropic-ai/claude-code@latest` | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` |
| `codex` | `npm i -g @openai/codex@latest` | `OPENAI_API_KEY` |
| `custom` | `agent.install`, when set | `agent.required_env` |

The coding-agent task in this repo uses `opensandbox/code-interpreter:v1.1.0` because that image includes Node.js. A `python` image is not enough for Claude or Codex.

Claude is started with `--dangerously-skip-permissions` and Codex with `--ask-for-approval never`, so a run does not wait for a person. Isolation stays with the OpenSandbox sandbox, not with the CLI's own sandbox. Codex uses `--sandbox workspace-write` rather than bypassing its sandbox entirely.

`agent.command` replaces the default command. The `{prompt_file}` placeholder becomes the prompt path inside the sandbox. `agent.install: []` skips the default install. `agent.env` lists extra host variables to copy. Empty values are not forwarded.

Custom provider example:

```yaml
agent:
  kind: coding-cli
  provider: custom
  install:
    - pip install my-agent
  command: my-agent --prompt {prompt_file}
  required_env:
    - MY_AGENT_API_KEY
  env:
    - MY_AGENT_API_KEY
```

To add an agent that is not a CLI, extend `resolve_plan` in `src/agent_harness/agents.py`. The harness talks to the sandbox only through `SandboxSession`: `write_text`, `run`, and `destroy`.

## Layout

```text
src/agent_harness/    loader, agents, OpenSandbox session, harness loop, CLI
tasks/                one task per directory
tests/                loader and harness tests, with a fake sandbox
runs/                 local reports, ignored by git
```

The tests do not start Docker:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

## Operations

Keys stay in the host environment. The harness injects them into the container only for the task provider and does not write them into the report. Do not mount the host home directory into the sandbox. Agent commands are untrusted code, and the container is the boundary.

Stop the OpenSandbox server when it is not needed. `--keep` and a long timeout leave containers running until the TTL or an explicit destroy.
