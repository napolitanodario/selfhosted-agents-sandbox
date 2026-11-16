"""Synthetic, isolated data for dashboard tests. Never used by production code."""

from contextlib import closing

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3

from experiment_orchestrator.common import digest, file_digest, write_json
from experiment_orchestrator.store import Store
from experiment_orchestrator.telemetry import DATABASE, initialize_database


def seed(root: Path):
    store = Store(root)
    initialize_database(root / DATABASE)
    clock = datetime.now(timezone.utc)

    def at(seconds):
        return (clock - timedelta(seconds=seconds)).isoformat()

    records = [
        (
            "baseline001",
            "open-enricher",
            "scene-context-baseline",
            "succeeded",
            3600,
            0.76,
        ),
        (
            "candidate001",
            "open-enricher",
            "scene-context-memory-4",
            "running",
            900,
            None,
        ),
        (
            "candidate002",
            "open-enricher",
            "scene-context-window-600",
            "succeeded",
            2400,
            0.82,
        ),
        ("retrieval001", "retrieval-lab", "embedding-baseline", "failed", 1800, None),
    ]
    runs = []
    for index, (run_id, project, name, status, seconds, score) in enumerate(records):
        manifest = {
            "version": 1,
            "experiment_id": name,
            "adapter": "open-enricher" if project == "open-enricher" else "command",
            "profile": "gpu",
            "source": {"repository": project, "revision": "a" * 40},
            "payload": {
                "videos": ["sample.mkv"],
                "window_seconds": 600 if index == 2 else 300,
            },
        }
        attempt = {
            "id": f"attempt{index}",
            "number": 1,
            "scheduler": "completed" if status == "succeeded" else status,
            "pipeline": "succeeded"
            if status == "succeeded"
            else "pending"
            if status == "running"
            else "failed",
            "evaluation": "succeeded"
            if score is not None
            else "pending"
            if status == "running"
            else "not_requested",
            "artifacts": "ready" if status == "succeeded" else "pending",
            "created_at": at(seconds),
            "observation": {"state": status, "job_id": str(83001 + index)},
        }
        if status != "running":
            attempt["result"] = {
                "started_at": at(seconds - 60),
                "finished_at": at(seconds - 600),
                "metrics": {"quality": score, "processing_seconds": 540}
                if score is not None
                else {},
                "issues": []
                if score is not None
                else ["Workload exited before producing a result"],
                "exit_code": 0 if score is not None else 1,
            }
        run = {
            "id": run_id,
            "request_id": f"fixture-{run_id}",
            "fingerprint": digest(manifest),
            "manifest": manifest,
            "status": status,
            "created_at": at(seconds),
            "profile": {
                "backend": "slurm-ssh",
                "env": {"DO_NOT_EXPOSE": "fixture-secret-value"},
            },
            "attempts": [attempt],
        }
        store.save(run, "reconciled", {"scheduler": attempt["scheduler"]})
        runs.append(run)
        if attempt["artifacts"] == "ready":
            directory = root / "runs" / run_id / attempt["id"]
            output = directory / "artifacts" / "result.json"
            write_json(output, attempt["result"])
            write_json(
                directory / "artifact-index.json",
                [
                    {
                        "path": "result.json",
                        "bytes": output.stat().st_size,
                        "sha256": file_digest(output),
                    }
                ],
            )
    campaign = {
        "id": "campaign001",
        "objective": "Improve scene understanding with a wider context window",
        "rubric": "Compare quality on the same video cohort.",
        "phase": "wait",
        "max_runs": 6,
        "baseline": runs[0]["manifest"],
        "baseline_run": "baseline001",
        "runs": ["baseline001", "candidate001", "candidate002"],
        "created_at": at(4000),
        "updated_at": at(10),
        "iterations": [
            {"proposal": {"hypothesis": "Additional context improves continuity"}}
        ],
        "agent_tasks": {"secret": "fixture-secret-value"},
        "responses": [],
    }
    store.save_campaign(campaign)
    with closing(sqlite3.connect(root / DATABASE)) as connection, connection:
        for index, (role, status) in enumerate(
            [
                ("proposer", "succeeded"),
                ("reviewer", "succeeded"),
                ("analyst", "running"),
            ]
        ):
            session = {
                "id": f"session{index}",
                "campaign_id": campaign["id"],
                "project_id": "open-enricher",
                "role": role,
                "iteration": 1,
                "task_id": "research-agent",
                "provider": "scripted",
                "status": status,
                "phase": "agent 1" if status == "running" else "finished",
                "started_at": at(300 + index * 30),
                "finished_at": None if status == "running" else at(60),
                "heartbeat_at": at(0),
                "last_activity_at": at(4),
                "command_count": 3,
                "command_errors": 0,
                "output_bytes": 72,
                "dropped_events": 0,
                "events_truncated": False,
                "sandbox_id": f"sandbox-{index}",
                "prompt": "Inspect the supplied experiment evidence.",
                "response": {"summary": "Synthetic test response"}
                if status != "running"
                else None,
            }
            connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (
                    session["id"],
                    campaign["id"],
                    session["started_at"],
                    json.dumps(session),
                ),
            )
            for kind, payload in [
                ("command.started", {"label": "agent 1", "command": "python agent.py"}),
                (
                    "log",
                    {
                        "stream": "stdout",
                        "text": "Comparing the baseline and candidate evidence.\n",
                    },
                ),
            ]:
                connection.execute(
                    "INSERT INTO session_events(session_id,time,kind,payload) VALUES(?,?,?,?)",
                    (session["id"], at(4), kind, json.dumps(payload)),
                )
        for index in range(120):
            sample = {
                "time": at((119 - index) * 5),
                "hostname": "ubuntu-agent-host",
                "cpu_percent": 24 + 12 * math.sin(index / 10),
                "cpu_count": 16,
                "ram_used_bytes": int((5.8 + 0.2 * math.sin(index / 20)) * 1024**3),
                "ram_total_bytes": 32 * 1024**3,
                "disk_used_bytes": 180 * 1024**3,
                "disk_total_bytes": 1024**4,
                "dashboard_rss_bytes": 65 * 1024**2,
            }
            connection.execute(
                "INSERT INTO host_samples(time,document) VALUES(?,?)",
                (sample["time"], json.dumps(sample)),
            )
    return {
        "run": "baseline001",
        "attempt": "attempt0",
        "session": "session2",
        "campaign": "campaign001",
    }
