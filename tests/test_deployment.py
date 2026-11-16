"""Deployment filesystem/recovery tests; no network or production services."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("deploy_script", Path(__file__).parents[1] / "scripts" / "deploy.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source, self.destination = root / "checkout", root / "installed"
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Deployment test")
        self.git("config", "user.email", "deployment@example.invalid")
        (self.source / ".env.example").write_text("SANDBOX_DOMAIN=localhost:8080\n")
        (self.source / "orchestrator.yaml").write_text("version: 1\nstate_dir: .experiments\n")
        (self.source / "application.txt").write_text("committed code")
        self.commit()
        # Mock only installation and runtime imports. Git archives, links, locks,
        # operator files, activation and failure cleanup use the real filesystem.
        self.installer = patch.object(deployment, "install_release").start()
        self.check_config = patch.object(deployment, "check_config").start()
        self.addCleanup(patch.stopall)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.source), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.revision = self.git("rev-parse", "HEAD")

    def deploy(self, **kwargs):
        return deployment.deploy(self.source, self.destination, "python3", self.revision, **kwargs)

    def test_updates_preserve_state_secrets_and_previous_release(self):
        (self.source / "application.txt").write_text("uncommitted change must not deploy")
        (self.source / ".env").write_text("UNTRACKED_SECRET=never-copy\n")
        first = self.deploy()
        self.assertEqual((first / "application.txt").read_text(), "committed code")
        self.assertNotIn("UNTRACKED_SECRET", (first / ".env").read_text())
        shared = self.destination / "shared"
        (shared / ".env").write_text("PRIVATE_RUNTIME_SETTING=preserve\n")
        (shared / "orchestrator.yaml").write_text("custom operator configuration")
        (shared / ".experiments" / "ledger.db").write_bytes(b"existing ledger")
        (shared / "secrets" / "auth.json").write_text("existing auth")
        second = self.deploy()
        self.assertNotEqual(first, second)
        self.assertEqual((self.destination / "current").resolve(), second)
        self.assertEqual((self.destination / "previous").resolve(), first)
        self.assertTrue(first.is_dir(), "running campaigns still need their original release")
        self.assertEqual((second / ".env").read_text(), "PRIVATE_RUNTIME_SETTING=preserve\n")
        self.assertEqual((second / "orchestrator.yaml").read_text(), "custom operator configuration")
        self.assertEqual((second / ".experiments" / "ledger.db").read_bytes(), b"existing ledger")
        self.assertEqual((second / "secrets" / "auth.json").read_text(), "existing auth")

    def test_failed_install_or_config_check_leaves_current_intact(self):
        first = self.deploy()
        for operation in (self.installer, self.check_config):
            with self.subTest(operation=operation):
                operation.side_effect = RuntimeError("injected deployment failure")
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    self.deploy()
                operation.side_effect = None
                self.assertEqual((self.destination / "current").resolve(), first)
                self.assertEqual(list((self.destination / "releases").iterdir()), [first])

    def test_real_bundle_validation_does_not_create_a_ledger(self):
        project = Path(__file__).parents[1]
        for name in ("pyproject.toml", "orchestrator.yaml"):
            shutil.copyfile(project / name, self.source / name)
        shutil.copytree(project / "tasks", self.source / "tasks")
        (self.source / "examples" / "experiments").mkdir(parents=True)
        shutil.copyfile(project / "examples/experiments/cpu-exact.yaml",
                        self.source / "examples/experiments/cpu-exact.yaml")
        # Exercise the installed CLI and validator, with no installer mocks in
        # this path. A CLI validate used here would silently create .experiments.
        deployment.validate_bundle(self.source, Path(sys.executable).parent)
        self.assertFalse((self.source / ".experiments").exists())

    def test_old_commit_is_skipped_before_install(self):
        with patch.object(deployment, "main_revision", return_value="f" * 40):
            self.assertIsNone(self.deploy(check_main=True))
        self.installer.assert_not_called()
        self.assertFalse((self.destination / "current").exists())

    def test_main_advancing_during_install_does_not_replace_current(self):
        first = self.deploy()
        with patch.object(deployment, "main_revision", side_effect=[self.revision, "f" * 40]):
            self.assertIsNone(self.deploy(check_main=True))
        self.assertEqual((self.destination / "current").resolve(), first)
        self.assertEqual(list((self.destination / "releases").iterdir()), [first])

    def test_wrong_revision_and_overlapping_destinations_are_rejected(self):
        with self.assertRaisesRegex(deployment.DeploymentError, "tested revision"):
            deployment.deploy(self.source, self.destination, "python3", "f" * 40)
        for target in (self.source, self.source / "deploy", self.source.parent):
            with self.subTest(target=target), self.assertRaisesRegex(deployment.DeploymentError, "non-overlapping"):
                deployment.deploy(self.source, target, "python3")
        self.assertFalse(self.destination.exists())

    def test_existing_current_directory_is_never_replaced(self):
        current = self.destination / "current"
        current.mkdir(parents=True)
        (current / "important").write_text("preserve")
        with self.assertRaisesRegex(deployment.DeploymentError, "must be a symlink"):
            self.deploy()
        self.assertEqual((current / "important").read_text(), "preserve")
        self.installer.assert_not_called()
