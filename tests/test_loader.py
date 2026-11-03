"""Loader and CLI tests for the sample tasks."""

import tempfile
import unittest
from pathlib import Path

from agent_harness.cli import main
from agent_harness.loader import discover_tasks, load_task
from agent_harness.models import HarnessError

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"


class LoaderTest(unittest.TestCase):
    """Checks that the sample tasks load and that invalid ids are rejected."""

    def test_discover_sample_tasks(self) -> None:
        """The three sample tasks load with the expected agent and files."""
        tasks = {task.id: task for task in discover_tasks(TASKS)}
        self.assertEqual(set(tasks), {"hello", "fix-fizzbuzz", "scripted-fix"})

        hello = tasks["hello"]
        self.assertEqual(hello.image, "python:3.12")
        self.assertEqual(hello.files[0][0], "main.py")
        self.assertIn('print("hello")', hello.files[0][1])
        self.assertEqual(hello.agent.kind, "scripted")
        self.assertEqual(hello.resources["memory"], "512Mi")

        coded = tasks["fix-fizzbuzz"]
        self.assertEqual(coded.agent.provider, "claude")
        self.assertIn("Do not modify the tests.", coded.prompt)
        self.assertIn("return str(n)", dict(coded.files)["fizzbuzz.py"])

        scripted = tasks["scripted-fix"]
        self.assertIn("def fizzbuzz", scripted.agent.commands[0])
        self.assertEqual(
            {name for name, _text in scripted.files},
            {"fizzbuzz.py", "test_fizzbuzz.py"},
        )

    def test_rejects_mismatched_directory(self) -> None:
        """A task id that does not match its directory is rejected."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw) / "other"
            task_dir.mkdir()
            (task_dir / "task.yaml").write_text(
                "id: name\nagent:\n  kind: scripted\n  commands: ['true']\n",
                encoding="utf-8",
            )
            with self.assertRaises(HarnessError):
                load_task(task_dir / "task.yaml")

    def test_cli_list_and_validate(self) -> None:
        """list and validate exit 0 for the sample tasks."""
        self.assertEqual(main(["--tasks-dir", str(TASKS), "list"]), 0)
        self.assertEqual(main(["--tasks-dir", str(TASKS), "validate"]), 0)
        self.assertEqual(main(["--tasks-dir", str(TASKS), "validate", "hello"]), 0)
