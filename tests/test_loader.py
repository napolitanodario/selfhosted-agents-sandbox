import tempfile
import unittest
from pathlib import Path

from agent_harness.cli import main
from agent_harness.loader import discover_tasks, load_task
from agent_harness.models import HarnessError

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"


class LoaderTest(unittest.TestCase):
    def test_discover_sample_tasks(self) -> None:
        tasks = {task.id: task for task in discover_tasks(TASKS)}
        self.assertEqual(set(tasks), {"hello", "fix-fizzbuzz", "scripted-fix"})

        hello = tasks["hello"]
        self.assertEqual(hello.image, "python:3.12")
        self.assertEqual(hello.files, (("main.py", 'print("hello")\n'),))
        self.assertEqual(hello.agent.kind, "scripted")
        self.assertEqual(hello.resources["memory"], "512Mi")

        coded = tasks["fix-fizzbuzz"]
        self.assertEqual(coded.agent.provider, "claude")
        self.assertIn("Non modificare i test.", coded.prompt)
        self.assertIn("return str(n)", dict(coded.files)["fizzbuzz.py"])

        scripted = tasks["scripted-fix"]
        self.assertIn("def fizzbuzz", scripted.agent.commands[0])
        self.assertEqual(
            {name for name, _text in scripted.files},
            {"fizzbuzz.py", "test_fizzbuzz.py"},
        )

    def test_rejects_mismatched_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw) / "altro"
            task_dir.mkdir()
            (task_dir / "task.yaml").write_text(
                "id: nome\nagent:\n  kind: scripted\n  commands: ['true']\n",
                encoding="utf-8",
            )
            with self.assertRaises(HarnessError):
                load_task(task_dir / "task.yaml")

    def test_cli_list_and_validate(self) -> None:
        self.assertEqual(main(["--tasks-dir", str(TASKS), "list"]), 0)
        self.assertEqual(main(["--tasks-dir", str(TASKS), "validate"]), 0)
        self.assertEqual(main(["--tasks-dir", str(TASKS), "validate", "hello"]), 0)
