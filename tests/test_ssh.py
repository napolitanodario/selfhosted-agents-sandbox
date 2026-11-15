"""Connection policy, socket isolation and request-failure boundaries."""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiment_orchestrator.common import ExperimentError
from experiment_orchestrator.ssh import SSHSettings, SSHTransport


class SSHTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ssh-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = {"host": "test-cluster", "ssh": {"control_directory": str(self.root / "sockets")}}

    def test_reconstructed_clients_share_connection_for_commands_and_transfers(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b"ok", b"")
        with patch("experiment_orchestrator.ssh.subprocess.run", run):
            self.assertEqual(SSHTransport(self.profile).run(["squeue"]), b"ok")
            SSHTransport(self.profile).run(["sacct"])
            with (self.root / "download").open("wb") as output:
                SSHTransport(self.profile).run(["cat", "/job/artifacts.tar.gz"], output=output, transfer=True)
        sockets = {argv[argv.index("-S") + 1] for argv, _ in calls}
        self.assertEqual(len(sockets), 1)
        self.assertIn("%C", sockets.pop())
        self.assertEqual(calls[0][1]["timeout"], 120)
        self.assertEqual(calls[2][1]["timeout"], 1800)
        for argv, _ in calls:
            self.assertIn("ControlMaster=auto", argv)
            self.assertIn("ControlPersist=3600", argv)
            self.assertIn("ServerAliveInterval=60", argv)
            self.assertIn("ServerAliveCountMax=3", argv)
            self.assertNotIn("StrictHostKeyChecking=no", argv)
        self.assertEqual((self.root / "sockets").stat().st_mode & 0o777, 0o700)

    def test_timeout_does_not_replay_submission(self):
        with patch("experiment_orchestrator.ssh.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 120)) as run:
            with self.assertRaisesRegex(ExperimentError, "remote outcome may be unknown"):
                SSHTransport(self.profile).run(["python3", "worker.pyz", "submit", "/job"])
        self.assertEqual(run.call_count, 1)

    def test_remote_failure_does_not_replay_or_close_shared_connection(self):
        with patch("experiment_orchestrator.ssh.subprocess.run", return_value=subprocess.CompletedProcess("ssh", 1, b"", b"scheduler unavailable")) as run:
            with self.assertRaisesRegex(ExperimentError, "scheduler unavailable"):
                SSHTransport(self.profile).run(["sacct"])
        self.assertEqual(run.call_count, 1)

    def test_rejects_untrusted_control_directory(self):
        directory = self.root / "sockets"
        directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(ExperimentError, "private"):
            SSHTransport(self.profile).command(["true"])
        directory.rmdir()
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ExperimentError, "symlink"):
            SSHTransport(self.profile).command(["true"])

    def test_settings_validate_types_and_unknown_fields(self):
        for raw in ({"persist_seconds": 0}, {"keepalive_seconds": True}, {"command_timeout_seconds": "120"},
                    {"unexpected": 1}, {"control_directory": "relative"}, {"control_directory": "/tmp/%C"}):
            with self.subTest(raw=raw), self.assertRaises(ExperimentError):
                SSHSettings.from_profile({"ssh": raw})

    def test_command_quoting_and_configured_timeouts(self):
        self.profile["ssh"].update(persist_seconds=7200, command_timeout_seconds=45, transfer_timeout_seconds=600)
        command = ["python3", "/directory with spaces/worker.pyz", "status", "/a;echo not-a-command"]
        transport = SSHTransport(self.profile)
        argv = transport.command(command)
        self.assertEqual(shlex.split(argv[-1]), command)
        self.assertIn("ControlPersist=7200", argv)
        with patch("experiment_orchestrator.ssh.subprocess.run", return_value=subprocess.CompletedProcess("ssh", 0, b"", b"")) as run:
            transport.run(command)
            self.assertEqual(run.call_args.kwargs["timeout"], 45)
            transport.run(command, data=b"upload", transfer=True)
            self.assertEqual(run.call_args.kwargs["timeout"], 600)
            self.assertEqual(run.call_args.kwargs["input"], b"upload")
