"""Optional real OpenSSH multiplexing test against an ephemeral loopback server.

Requires asyncssh in the test environment and EXPERIMENT_TEST_REAL_SSH=1.
No cluster credentials or user's SSH configuration are used or modified.
"""
import asyncio
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from experiment_orchestrator.common import ExperimentError
from experiment_orchestrator.ssh import SSHTransport


@unittest.skipUnless(os.environ.get("EXPERIMENT_TEST_REAL_SSH") == "1" and importlib.util.find_spec("asyncssh"),
                     "opt-in loopback test: install asyncssh and set EXPERIMENT_TEST_REAL_SSH=1")
class LiveSSHTest(unittest.TestCase):
    def test_connection_reuse_drop_reconnect_and_idle_expiry(self):
        import asyncssh

        real_ssh = shutil.which("ssh")
        self.assertIsNotNone(real_ssh)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        connections, requests = [], []
        uploaded = ""
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        host_key = asyncssh.generate_private_key("ssh-ed25519")

        class Server(asyncssh.SSHServer):
            def connection_made(self, connection):
                connections.append(connection)

            def begin_auth(self, username):
                return True

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return username == "fixture" and key.export_public_key() == client_key.export_public_key()

        async def process_handler(process):
            nonlocal uploaded
            requests.append(process.command)
            args = shlex.split(process.command)
            if args[0] == "print":
                process.stdout.write(args[1])
            elif args[0] == "upload":
                uploaded = await process.stdin.read()
            elif args[0] == "download":
                process.stdout.write(uploaded)
            elif args[0] == "slow":
                await asyncio.sleep(5)
            elif args[0] == "fail":
                process.stderr.write("remote command rejected")
                process.exit(1)
                return
            process.exit(0)

        async def start():
            return await asyncssh.create_server(Server, "127.0.0.1", 0,
                                               server_host_keys=[host_key], process_factory=process_handler)

        listener = None
        try:
            listener = asyncio.run_coroutine_threadsafe(start(), loop).result(timeout=10)
            port = listener.get_port()
            with tempfile.TemporaryDirectory(prefix="sshr-") as raw:
                root = Path(raw)
                key_path = root / "id"
                key_path.write_bytes(client_key.export_private_key("openssh"))
                key_path.chmod(0o600)
                known = root / "known_hosts"
                known.write_bytes(f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key())
                config = root / "config"
                config.write_text(f"Host fixture-host\n HostName 127.0.0.1\n Port {port}\n User fixture\n"
                                  f" IdentityFile {key_path}\n IdentitiesOnly yes\n"
                                  f" UserKnownHostsFile {known}\n StrictHostKeyChecking yes\n")
                binaries = root / "bin"
                binaries.mkdir()
                wrapper = binaries / "ssh"
                wrapper.write_text("#!/bin/sh\nexec " + shlex.join([real_ssh, "-F", str(config)]) + ' "$@"\n')
                wrapper.chmod(0o755)
                profile = {"host": "fixture-host", "ssh": {"control_directory": str(root / "s"),
                           "persist_seconds": 30, "keepalive_seconds": 1, "keepalive_count_max": 2,
                           "command_timeout_seconds": 5, "transfer_timeout_seconds": 5}}
                transports = []
                with patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}):
                    try:
                        transport = SSHTransport(profile)
                        transports.append(transport)
                        self.assertEqual(transport.run(["print", "first"]), b"first")
                        self.assertEqual(SSHTransport(profile).run(["print", "second"]), b"second")
                        SSHTransport(profile).run(["upload"], data="payload with spaces and λ".encode(), transfer=True)
                        with (root / "download").open("w+b") as output:
                            SSHTransport(profile).run(["download"], output=output, transfer=True)
                            output.seek(0)
                            self.assertEqual(output.read().decode(), "payload with spaces and λ")
                        self.assertEqual(len(connections), 1, "commands and transfers must share one TCP connection")

                        with self.assertRaisesRegex(ExperimentError, "remote command rejected"):
                            transport.run(["fail"])
                        self.assertEqual(requests.count("fail"), 1)
                        slow_profile = deepcopy(profile)
                        slow_profile["ssh"]["command_timeout_seconds"] = 1
                        with self.assertRaisesRegex(ExperimentError, "timed out"):
                            SSHTransport(slow_profile).run(["slow"])
                        self.assertEqual(transport.run(["print", "after command timeout"]), b"after command timeout")
                        self.assertEqual(len(connections), 1, "a command deadline must not tear down a healthy master")

                        loop.call_soon_threadsafe(connections[-1].abort)
                        check = transport.command([])[:-2] + ["-O", "check", transport.host]
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if subprocess.run(check, capture_output=True, timeout=2).returncode:
                                break
                            time.sleep(0.05)
                        self.assertEqual(SSHTransport(profile).run(["print", "reconnected"]), b"reconnected")
                        self.assertEqual(len(connections), 2)

                        idle_profile = deepcopy(profile)
                        idle_profile["ssh"]["persist_seconds"] = 1
                        idle = SSHTransport(idle_profile)
                        transports.append(idle)
                        self.assertEqual(idle.run(["print", "before idle"]), b"before idle")
                        self.assertEqual(len(connections), 3)
                        time.sleep(2)
                        self.assertEqual(SSHTransport(idle_profile).run(["print", "after idle"]), b"after idle")
                        self.assertEqual(len(connections), 4)
                    finally:
                        for transport in transports:
                            command = transport.command([])[:-2] + ["-O", "exit", transport.host]
                            subprocess.run(command, capture_output=True, timeout=5)
        finally:
            async def stop():
                if listener is not None:
                    listener.close()
                    await listener.wait_closed()
                for connection in connections:
                    connection.close()
                    await connection.wait_closed()
                # Allow process handlers to observe channel closure, then cancel
                # any remaining fixture task before stopping its event loop.
                await asyncio.sleep(0)
                pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            asyncio.run_coroutine_threadsafe(stop(), loop).result(timeout=10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()
