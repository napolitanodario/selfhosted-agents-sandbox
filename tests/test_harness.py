"""Harness tests that use a fake sandbox instead of OpenSandbox."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_harness.agents import resolve_plan
from agent_harness.harness import run_task
from agent_harness.loader import load_task
from agent_harness.models import CommandResult, HarnessError
from agent_harness.session import build_create_kwargs, execution_to_result, load_settings

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
