"""Harness tests that use a fake sandbox instead of OpenSandbox."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_harness.agents import resolve_plan, sync_host_codex_auth
from agent_harness.harness import run_task
from agent_harness.loader import load_task
from agent_harness.models import CommandResult, HarnessError
from agent_harness.session import (
    apply_env_file,
    build_create_kwargs,
    execution_to_result,
    load_settings,
)

from support import FakeFactory, FakeSession

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"


def _task(name: str):
    """Load one sample task by directory name."""
    return load_task(TASKS / name / "task.yaml")


class HarnessTest(unittest.TestCase):
    """Covers seeding, checks, env forwarding, and sandbox cleanup."""

    def test_hello_passes_and_destroys_the_sandbox(self) -> None:
        """A passing scripted task seeds the workspace and destroys the sandbox."""
        factory = FakeFactory()
        with tempfile.TemporaryDirectory() as raw:
            report = self._run(_task("hello"), factory, Path(raw))
            self.assertEqual(report.status, "passed")
            self.assertTrue(factory.session.destroyed)
            expected = (TASKS / "hello" / "workspace" / "main.py").read_text(encoding="utf-8")
            self.assertEqual(factory.session.files["/workspace/main.py"], expected)
            self.assertIn("Smoke test", factory.session.files["/workspace/.harness/prompt.md"])
            mkdir = factory.session.commands[0][0]
            self.assertIn("/workspace/.harness", mkdir)
            self.assertIn("/workspace", mkdir)
            self.assertNotIn("/workspaceharness", mkdir)
            saved = json.loads(Path(report.report_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "passed")
        self.assertEqual(saved["checks"][0]["passed"], True)

    def test_failed_check_still_destroys_the_sandbox(self) -> None:
        """A failed check is reported and the sandbox is still destroyed."""
        def handler(command: str) -> CommandResult:
            if command.startswith("mkdir"):
                return CommandResult(command=command, exit_code=0, stdout="", stderr="")
            return CommandResult(command=command, exit_code=1, stdout="FAIL", stderr="boom")

        session = FakeSession(handler)
        factory = FakeFactory(session)
        with tempfile.TemporaryDirectory() as raw:
            report = self._run(_task("scripted-fix"), factory, Path(raw))
        self.assertEqual(report.status, "failed")
        self.assertTrue(session.destroyed)
        self.assertFalse(report.checks[0].passed)

    def test_keep_leaves_the_sandbox(self) -> None:
        """``keep`` skips destroy and records the sandbox id."""
        factory = FakeFactory()
        with tempfile.TemporaryDirectory() as raw:
            report = self._run(_task("hello"), factory, Path(raw), keep=True)
        self.assertEqual(report.status, "passed")
        self.assertFalse(factory.session.destroyed)
        self.assertEqual(report.sandbox_id, "sbx-test")

    def test_coding_cli_requires_a_key_before_creating_a_sandbox(self) -> None:
        """A missing provider key fails before any sandbox is created."""
        factory = FakeFactory()
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(HarnessError):
                self._run(_task("fix-fizzbuzz"), factory, Path(raw), environ={})
        self.assertEqual(factory.opened, 0)

    def test_coding_cli_forwards_only_the_provider_env(self) -> None:
        """Only the provider env is injected, and secrets stay out of the report."""
        factory = FakeFactory()
        environ = {
            "ANTHROPIC_API_KEY": "secret-key",
            "OPENAI_API_KEY": "other-secret",
            "UNRELATED": "nope",
        }
        with tempfile.TemporaryDirectory() as raw:
            report = self._run(_task("fix-fizzbuzz"), factory, Path(raw), environ=environ)
            report_text = Path(report.report_path).read_text(encoding="utf-8")
        self.assertEqual(factory.env["ANTHROPIC_API_KEY"], "secret-key")
        self.assertEqual(factory.env["IS_SANDBOX"], "1")
        self.assertNotIn("OPENAI_API_KEY", factory.env)
        self.assertNotIn("secret-key", report_text)
        plan = resolve_plan(_task("fix-fizzbuzz"), environ)
        self.assertIn("--dangerously-skip-permissions", plan.commands[0])
        self.assertIn("/workspace/.harness/prompt.md", plan.commands[0])
        self.assertEqual(plan.install, ("npm i -g @anthropic-ai/claude-code@latest",))

    def test_codex_copies_the_chatgpt_login_and_drops_the_api_key(self) -> None:
        """A ChatGPT login is written outside the workspace and kept out of the report."""

        def handler(command: str) -> CommandResult:
            if "printf" in command:
                return CommandResult(command=command, exit_code=0, stdout="/root", stderr="")
            return CommandResult(command=command, exit_code=0, stdout="", stderr="")

        factory = FakeFactory(FakeSession(handler))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            auth = root / "auth.json"
            auth.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "OPENAI_API_KEY": "sk-secret",
                        "tokens": {"refresh_token": "refresh-secret"},
                    }
                ),
                encoding="utf-8",
            )
            report = self._run(
                _task("codex-fizzbuzz"),
                factory,
                root / "runs",
                environ={"CODEX_AUTH_FILE": str(auth)},
            )
            report_text = Path(report.report_path).read_text(encoding="utf-8")
        written = json.loads(factory.session.files["/root/.codex/auth.json"])
        self.assertEqual(written["auth_mode"], "chatgpt")
        self.assertEqual(written["tokens"]["refresh_token"], "refresh-secret")
        self.assertNotIn("OPENAI_API_KEY", written)
        self.assertEqual(factory.session.modes["/root/.codex/auth.json"], 0o600)
        self.assertIn('cli_auth_credentials_store = "file"', factory.session.files["/root/.codex/config.toml"])
        self.assertNotIn("refresh-secret", report_text)
        self.assertNotIn("sk-secret", report_text)
        self.assertNotIn("OPENAI_API_KEY", factory.env)

    def test_codex_refuses_to_start_when_the_api_key_is_set(self) -> None:
        """A set API key is rejected before a sandbox exists."""
        factory = FakeFactory()
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(HarnessError) as caught:
                self._run(
                    _task("codex-fizzbuzz"),
                    factory,
                    Path(raw),
                    environ={"OPENAI_API_KEY": "sk-secret"},
                )
        self.assertIn("Unset OPENAI_API_KEY", str(caught.exception))
        self.assertEqual(factory.opened, 0)

    def test_sync_copies_the_host_login_into_the_project(self) -> None:
        """The startup copy lands in secrets/codex and is not world-readable."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "home-auth.json"
            source.write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")
            project = root / "project"
            project.mkdir()
            copied = sync_host_codex_auth(project, source)
            self.assertEqual(copied, project / "secrets" / "codex" / "auth.json")
            self.assertEqual(copied.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)

    def test_codex_refuses_a_missing_login_file(self) -> None:
        """A missing ChatGPT login fails before a sandbox exists."""
        factory = FakeFactory()
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(HarnessError):
                self._run(
                    _task("codex-fizzbuzz"),
                    factory,
                    Path(raw),
                    environ={"CODEX_AUTH_FILE": str(Path(raw) / "missing.json")},
                )
        self.assertEqual(factory.opened, 0)

    def test_create_failure_is_reported(self) -> None:
        """A factory error becomes an error report and no sandbox id."""

        class Boom:
            """Factory that fails before a session exists."""

            async def open(self, task, env):
                """Refuse to create a sandbox."""
                del task, env
                raise RuntimeError("server down")

        with tempfile.TemporaryDirectory() as raw:
            report = self._run(_task("hello"), Boom(), Path(raw))
        self.assertEqual(report.status, "error")
        self.assertIn("server down", report.error or "")
        self.assertIsNone(report.sandbox_id)

    def test_execution_mapping_uses_error_exit_code(self) -> None:
        """An execution error supplies the exit code when the top-level field is empty."""
        execution = SimpleNamespace(
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text="out")],
                stderr=[SimpleNamespace(text="err")],
            ),
            exit_code=None,
            error=SimpleNamespace(name="CommandExecError", value="failed", exit_code=2),
        )
        result = execution_to_result("false", execution)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.stdout, "out")
        self.assertEqual(result.error, "CommandExecError: failed")

    def test_create_kwargs_carry_limits_and_metadata(self) -> None:
        """Create kwargs include resource limits, metadata, and the sandbox TTL."""
        kwargs = build_create_kwargs(_task("hello"), {})
        self.assertEqual(kwargs["resource"], {"cpu": "1", "memory": "512Mi"})
        self.assertEqual(kwargs["metadata"]["task"], "hello")
        self.assertNotIn("env", kwargs)
        self.assertEqual(kwargs["timeout"].total_seconds(), 600)

    def test_env_file_fills_missing_keys_only(self) -> None:
        """A project .env supplies keys the shell did not already set."""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / ".env"
            path.write_text(
                '\n'.join(
                    [
                        "# comment",
                        "SANDBOX_API_KEY=from-file",
                        "SANDBOX_DOMAIN=from-file",
                        "export SANDBOX_PROTOCOL='http'",
                    ]
                ),
                encoding="utf-8",
            )
            environ = {"SANDBOX_DOMAIN": "from-shell"}
            apply_env_file(path, environ)
        self.assertEqual(environ["SANDBOX_API_KEY"], "from-file")
        self.assertEqual(environ["SANDBOX_DOMAIN"], "from-shell")
        self.assertEqual(environ["SANDBOX_PROTOCOL"], "http")

    def test_settings_from_the_environment(self) -> None:
        """An empty API key is dropped and the proxy flag accepts false."""
        settings = load_settings(
            {
                "SANDBOX_DOMAIN": "127.0.0.1:8080",
                "SANDBOX_API_KEY": "",
                "SANDBOX_USE_SERVER_PROXY": "false",
            }
        )
        self.assertEqual(settings.domain, "127.0.0.1:8080")
        self.assertIsNone(settings.api_key)
        self.assertFalse(settings.use_server_proxy)

    def _run(self, task, factory, report_dir: Path, keep: bool = False, environ: dict | None = None):
        """Run one task to completion on a fresh event loop."""
        import asyncio

        return asyncio.run(
            run_task(
                task,
                factory=factory,
                report_dir=report_dir,
                keep=keep,
                environ=os.environ if environ is None else environ,
            )
        )


if __name__ == "__main__":
    unittest.main()
