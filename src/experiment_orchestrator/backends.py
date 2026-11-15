"""Execution backends, independent of workload semantics."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import threading

from .common import ExperimentError, now, read_json, write_json
from .remote import collect
from .ssh import SSHTransport
from .worker import process_token


class LocalBackend:
    def __init__(self, profile):
        self.profile = profile

    def stage(self, root):
        return None

    def submit(self, root):
        if (root / "submit.started.json").exists():
            return self.status(root)
        if (root / "cancel.request").exists():
            return {"state": "cancelled"}
        write_json(root / "submit.started.json", {"time": now()})
        with (root / "launcher.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, str(root / "worker.pyz"), "run", str(root)],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        receipt = {"job_id": str(child.pid), "pid": child.pid, "token": process_token(child.pid), "state": "running"}
        write_json(root / "receipt.json", receipt)
        threading.Thread(target=child.wait, daemon=True).start()
        # Popen is intentionally detached; worker completion is observed through durable files.
        return receipt

    def status(self, root):
        if (root / "worker.finished.json").exists():
            return read_json(root / "worker.finished.json")
        path = root / "worker.started.json"
        if not path.exists():
            path = root / "receipt.json"
        if path.exists():
            receipt = read_json(path)
            token = receipt.get("token")
            if token and process_token(receipt["pid"]) == token:
                return {**receipt, "state": "running"}
        return {"state": "unknown", "detail": "worker unavailable without a completion record"}

    def cancel(self, root):
        (root / "cancel.request").touch()
        if not (root / "submit.started.json").exists():
            write_json(root / "worker.finished.json", {"state": "cancelled", "time": now()})

    def collect(self, root, limit):
        collect(root, limit)
        return root / "artifacts.tar.gz"


class DockerBackend(LocalBackend):
    def name(self, root):
        return "exp-" + read_json(root / "job.json")["attempt_id"]

    def submit(self, root):
        if (root / "submit.started.json").exists():
            return self.status(root)
        if (root / "cancel.request").exists():
            return {"state": "cancelled"}
        write_json(root / "submit.started.json", {"time": now()})
        argv = ["docker", "run", "--detach", "--name", self.name(root), "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
                "--cpus", str(self.profile.get("cpus", 1)), "--memory", str(self.profile.get("memory", "512m")),
                "--tmpfs", "/tmp", "--user", f"{os.getuid()}:{os.getgid()}",
                "--mount", f"type=bind,source={root},target=/job", self.profile["image"],
                "python3", "/job/worker.pyz", "run", "/job"]
        response = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        if response.returncode:
            raise ExperimentError(response.stderr.strip())
        return {"state": "running", "job_id": self.name(root)}

    def status(self, root):
        if (root / "cancel.request").exists() and not (root / "submit.started.json").exists():
            return {"state": "cancelled"}
        response = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", self.name(root)],
                                  capture_output=True, text=True, timeout=30)
        if response.returncode:
            return {"state": "unknown", "detail": response.stderr.strip()}
        state = json.loads(response.stdout)
        if state["Running"]:
            return {"state": "running", "job_id": self.name(root)}
        if state.get("Status") == "exited":
            if (root / "cancel.request").exists() and state["ExitCode"] != 0:
                outcome = "cancelled"
            else:
                outcome = "completed" if state["ExitCode"] == 0 else "failed"
            return {"state": outcome, "exit_code": state["ExitCode"], "job_id": self.name(root)}
        return {"state": "unknown", "detail": state.get("Status")}

    def cancel(self, root):
        super().cancel(root)
        if not (root / "submit.started.json").exists():
            return
        response = subprocess.run(["docker", "stop", "--time", "15", self.name(root)], capture_output=True, text=True, timeout=30)
        if response.returncode:
            raise ExperimentError(response.stderr.strip())


class SlurmBackend:
    def __init__(self, profile):
        self.profile = profile
        self.transport = SSHTransport(profile)

    def location(self, root):
        return str(Path(self.profile["root"]) / read_json(root / "job.json")["attempt_id"])

    def ssh(self, argv, *, data=None, output=None, transfer=False):
        return self.transport.run(argv, data=data, output=output, transfer=transfer)

    def stage(self, root):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name in ("job.json", "worker.pyz", "source.tar"):
                if (root / name).exists():
                    tar.add(root / name, arcname=name)
        # Upload into a temporary sibling, then publish the complete immutable bundle.
        script = """import io, pathlib, sys, tarfile, tempfile, os, shutil
target = pathlib.Path(sys.argv[1])
data = sys.stdin.buffer.read()
target.parent.mkdir(parents=True, exist_ok=True)
if not target.exists():
    temp = pathlib.Path(tempfile.mkdtemp(prefix='.stage-', dir=target.parent))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            tar.extractall(temp, filter='data')
        os.rename(temp, target)
    finally:
        if temp.exists(): shutil.rmtree(temp)
"""
        self.ssh([self.profile.get("control_python", "python3"), "-c", script, self.location(root)], data=buffer.getvalue(), transfer=True)

    def control(self, action, root, *args):
        location = self.location(root)
        raw = self.ssh([self.profile.get("control_python", "python3"), f"{location}/worker.pyz", action, location, *map(str, args)], transfer=action == "collect")
        return json.loads(raw)

    def submit(self, root):
        return self.control("submit", root)

    def status(self, root):
        return self.control("status", root)

    def cancel(self, root):
        return self.control("cancel", root)

    def collect(self, root, limit):
        self.control("collect", root, limit)
        target = root / "artifacts.tar.gz"
        with target.with_suffix(".tmp").open("wb") as stream:
            self.ssh(["cat", f"{self.location(root)}/artifacts.tar.gz"], output=stream, transfer=True)
        if target.with_suffix(".tmp").stat().st_size > limit + 1024**2:
            raise ExperimentError("download exceeds artifact limit")
        target.with_suffix(".tmp").replace(target)
        return target


def backend(profile):
    return {"local-process": LocalBackend, "docker": DockerBackend, "slurm-ssh": SlurmBackend}[profile["backend"]](profile)
