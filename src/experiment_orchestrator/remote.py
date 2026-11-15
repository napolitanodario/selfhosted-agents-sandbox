"""Slurm control executed over SSH on the login node, using the staged worker zipapp.

A durable marker precedes sbatch. A missing receipt is reconciled, never resubmitted.
"""
from __future__ import annotations

import fcntl
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile

from .common import ExperimentError, now, read_json, write_json


def command(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=45)


def job_name(root):
    return "exp-" + read_json(root / "job.json")["attempt_id"]


def reconcile(root):
    receipt = root / "receipt.json"
    if receipt.exists():
        return read_json(receipt)
    name = job_name(root)
    # A calendar-day margin covers login-node time zones without excluding a fast job.
    created = datetime.fromisoformat(read_json(root / "job.json")["created_at"])
    since = (created - timedelta(days=1)).date().isoformat()
    queue = command(["squeue", "--noheader", "--name", name, "--format=%i|%j"])
    accounting = command(["sacct", "--noheader", "--parsable2", "--allocations", "--starttime", since,
                          "--name", name, "--format=JobIDRaw,JobName%100"])
    ids = set()
    for output in (queue, accounting):
        if output.returncode == 0:
            for row in output.stdout.splitlines():
                fields = row.strip().split("|")
                if len(fields) == 2 and fields[1] == name and fields[0].isdigit():
                    ids.add(fields[0])
    if len(ids) == 1:
        value = {"job_id": ids.pop(), "time": now(), "reconciled": True}
        write_json(receipt, value)
        return value
    return {"state": "unknown", "detail": "submission receipt unavailable; no resubmission is permitted", "matches": sorted(ids)}


def submit(root):
    with (root / "submission.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (root / "submit.started.json").exists():
            return reconcile(root)
        if (root / "cancel.request").exists():
            return {"state": "cancelled"}
        job = read_json(root / "job.json")
        profile = job["profile"]
        script = root / "job.sh"
        script.write_text("#!/bin/sh\nexec " + shlex.join([profile.get("python", "python3"), str(root / "worker.pyz"), "run", str(root)]) + "\n")
        argv = ["sbatch", "--parsable", "--no-requeue", "--job-name", job_name(root), "--chdir", str(root),
                "--output", str(root / "slurm-%j.log")]
        for key, value in profile.get("slurm", {}).items():
            argv.append(f"--{key}={value}")
        argv.append(str(script))
        write_json(root / "submit.started.json", {"time": now(), "argv": argv})
        response = command(argv)
        if response.returncode != 0:
            # Nonzero or timeout can still be ambiguous with site wrappers. Retain intent.
            return {"state": "unknown", "detail": response.stderr.strip() or "sbatch returned nonzero"}
        job_id = response.stdout.strip().split(";")[0]
        if not re.fullmatch(r"[0-9]+", job_id):
            return {"state": "unknown", "detail": "unrecognized sbatch response"}
        receipt = {"job_id": job_id, "time": now()}
        write_json(root / "receipt.json", receipt)
        return receipt


def status(root):
    if (root / "cancel.request").exists() and not (root / "submit.started.json").exists():
        return {"state": "cancelled"}
    receipt = reconcile(root)
    if "job_id" not in receipt:
        return receipt
    job_id = receipt["job_id"]
    queue = command(["squeue", "--noheader", "--jobs", job_id, "--format=%i|%T"])
    accounting = command(["sacct", "--noheader", "--parsable2", "--allocations", "--jobs", job_id,
                          "--format=JobIDRaw,State,ExitCode"])
    raw = None
    for output in (queue, accounting):
        if output.returncode == 0:
            for line in output.stdout.splitlines():
                parts = line.strip().split("|")
                if len(parts) >= 2 and parts[0] == job_id:
                    raw = parts[1].split()[0].rstrip("+")
                    break
            if raw:
                break
    mapping = {"PENDING": "queued", "CONFIGURING": "queued", "RUNNING": "running", "COMPLETING": "running",
               "SUSPENDED": "running", "COMPLETED": "completed", "CANCELLED": "cancelled",
               "FAILED": "failed", "TIMEOUT": "failed", "OUT_OF_MEMORY": "failed", "NODE_FAIL": "failed",
               "PREEMPTED": "failed", "BOOT_FAIL": "failed", "DEADLINE": "failed", "REVOKED": "failed"}
    return {**receipt, "state": mapping.get(raw, "unknown"), "scheduler_detail": raw}


def cancel(root):
    (root / "cancel.request").touch()
    receipt = reconcile(root)
    if "job_id" in receipt:
        result = command(["scancel", receipt["job_id"]])
        if result.returncode:
            raise ExperimentError(result.stderr.strip())
    return receipt


def collect(root, limit):
    """Preserve partial evidence too; exclude datasets, source code and credentials."""
    candidates = [p for pattern in ("*.json", "*.log") for p in root.glob(pattern) if p.name != "job.json"]
    for folder in (root / "output", root / "source/runs/local"):
        if folder.is_dir():
            candidates.extend(p for p in folder.rglob("*") if p.is_file() and "_cache" not in p.relative_to(folder).parts)
    files, size = [], 0
    for path in sorted(set(candidates)):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            continue
        size += path.stat().st_size
        if size > limit:
            raise ExperimentError("artifacts exceed configured size limit; files retained on execution host")
        files.append(path)
    target = root / "artifacts.tar.gz"
    with tarfile.open(target.with_suffix(".tmp"), "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=path.relative_to(root), recursive=False)
    target.with_suffix(".tmp").replace(target)
    return {"archive": str(target), "files": len(files), "bytes": size}


def main():
    import sys
    from .worker import run
    action, location = sys.argv[1:3]
    root = Path(location).resolve()
    if action == "run":
        return run(root)
    actions = {"submit": submit, "status": status, "cancel": cancel,
               "collect": lambda p: collect(p, int(sys.argv[3]))}
    try:
        print(json.dumps(actions[action](root)))
        return 0
    except Exception as exc:
        print(json.dumps({"state": "unknown", "detail": str(exc)}))
        return 1
