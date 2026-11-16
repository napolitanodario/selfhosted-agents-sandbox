"""Monitoring boundaries, durable agent events, recovery, and artifact access."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.harness import run_task
from agent_harness.loader import load_task
from experiment_dashboard.reader import Reader, numeric_metrics, safe_file
from experiment_orchestrator.common import file_digest, write_json
from experiment_orchestrator.telemetry import DATABASE, Redactor, SessionRecorder
from dashboard_fixture import seed
from support import FakeFactory


HAS_API = importlib.util.find_spec("fastapi") is not None
if HAS_API:
    from fastapi.testclient import TestClient
    from experiment_dashboard.api import COOKIE, create_app

ROOT = Path(__file__).parents[1]
TOKEN = "test-dashboard-access-token-only"


class TelemetryTest(unittest.TestCase):
    def test_parallel_sessions_preserve_identity_cursor_and_split_secret_redaction(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def record(index):
                with SessionRecorder(
                    root,
                    {
                        "campaign_id": "campaign",
                        "role": "proposer",
                        "project_id": f"project-{index}",
                    },
                    {"API_KEY": "private-credential"},
                ) as recorder:
                    recorder.emit(
                        "command.started",
                        {"label": "agent 1", "command": "python agent.py"},
                    )
                    recorder.emit("log", {"stream": "stdout", "text": "private-"})
                    recorder.emit(
                        "log", {"stream": "stdout", "text": "credential\ncompleted\n"}
                    )
                    recorder.emit("log", {"text": "-----BEGIN PRIVATE KEY-----\n"})
                    recorder.emit("log", {"text": "private-key-body\n"})
                    recorder.emit("log", {"text": "-----END PRIVATE KEY-----\n"})
                    recorder.emit(
                        "command.finished",
                        {"label": "agent 1", "exit_code": 0, "seconds": 1},
                    )
                    recorder.finish(response={"summary": "done"})
                return recorder.document["id"]

            with ThreadPoolExecutor(max_workers=4) as pool:
                identifiers = list(pool.map(record, range(4)))
            reader = Reader(root)
            self.assertEqual(len(reader.snapshot()["sessions"]), 4)
            for identifier in identifiers:
                session = reader.session(identifier)
                self.assertEqual(session["status"], "succeeded")
                page = reader.events(identifier, after=0, limit=2)
                next_page = reader.events(identifier, after=page["cursor"])
                self.assertTrue(
                    all(
                        event["sequence"] > page["cursor"]
                        for event in next_page["events"]
                    )
                )
                text = json.dumps(reader.events(identifier))
                self.assertNotIn("private-credential", text)
                self.assertNotIn("private-key-body", text)
                self.assertIn("[redacted]", text)

    def test_observer_failure_does_not_change_harness_result(self):
        def broken(kind, payload):
            raise OSError("unavailable")

        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertLogs("agent_harness.observation", level="WARNING"),
        ):
            report = asyncio.run(
                run_task(
                    load_task(ROOT / "tasks/hello/task.yaml"),
                    factory=FakeFactory(),
                    report_dir=Path(directory),
                    environ={},
                    observer=broken,
                )
            )
        self.assertEqual(report.status, "passed")

    def test_telemetry_storage_failure_does_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "file"
            root.write_text("not a directory")
            with self.assertLogs("experiment_orchestrator.telemetry", level="WARNING"):
                with SessionRecorder(root, {"campaign_id": "test"}) as recorder:
                    recorder.finish(response={"ok": True})
            self.assertTrue(recorder.failed)

    def test_output_limit_is_explicit_and_final_result_survives(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("experiment_orchestrator.telemetry.MAX_LOG_BYTES", 10),
        ):
            root = Path(directory)
            with SessionRecorder(
                root, {"campaign_id": "test", "project_id": "test"}
            ) as recorder:
                recorder.emit(
                    "log", {"stream": "stdout", "text": "a large line of output\n"}
                )
                recorder.finish(response={"result": "complete"})
            session = Reader(root).session(recorder.document["id"])
            self.assertTrue(session["events_truncated"])
            self.assertEqual(session["status"], "succeeded")

    def test_numeric_metrics_preserve_zero_and_exclude_booleans(self):
        self.assertEqual(
            numeric_metrics(
                {
                    "score": 0,
                    "passed": True,
                    "nested": {"latency": 2},
                    "invalid": float("inf"),
                    "overflow": 10**400,
                }
            ),
            {"score": 0, "nested.latency": 2},
        )


@unittest.skipUnless(HAS_API, "Install the dashboard extra for API tests")
class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ids = seed(self.root)
        self.app = create_app(
            self.root, token=TOKEN, allowed_hosts=("testserver",), collect_host=False
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post("/api/auth/login", json={"token": TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])

    def test_authentication_and_read_only_boundary(self):
        self.assertEqual(self.client.get("/api/snapshot").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/snapshot",
                headers={"Cf-Access-Authenticated-User-Email": "owner@example.invalid"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/auth/login", json={"token": "invalid"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/login", json={"token": "non-ascii-\u00e9"}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"token": TOKEN},
                headers={"Origin": "https://evil.invalid"},
            ).status_code,
            403,
        )
        self.login()
        for path in (
            "/api/runs/baseline001/cancel",
            "/api/campaigns/campaign001/resume",
            "/api/snapshot",
        ):
            self.assertEqual(self.client.post(path).status_code, 405)
        self.assertEqual(
            self.client.get(
                "/api/snapshot", headers={"Host": "evil.invalid"}
            ).status_code,
            400,
        )
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/snapshot").status_code, 401)

    def test_snapshot_and_details_never_reconcile_or_expose_operator_secrets(self):
        self.login()
        before = file_digest(self.root / "ledger.sqlite3")
        with patch(
            "experiment_orchestrator.broker.Broker.get_run",
            side_effect=AssertionError("Broker must not be called"),
        ):
            response = self.client.get("/api/snapshot")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                set(response.json()["projects"]), {"open-enricher", "retrieval-lab"}
            )
            self.assertNotIn("fixture-secret-value", response.text)
            self.assertNotIn("agent_tasks", response.text)
            self.assertEqual(len(response.json()["sessions"]), 3)
            self.assertEqual(self.client.get("/api/runs/baseline001").status_code, 200)
            self.assertEqual(
                self.client.get("/api/runs/baseline001/events").status_code, 200
            )
        self.assertEqual(before, file_digest(self.root / "ledger.sqlite3"))

    def test_lost_heartbeat_is_unknown_not_failed_and_remote_observations_age(self):
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with closing(sqlite3.connect(self.root / DATABASE)) as connection, connection:
            row = connection.execute(
                "SELECT document FROM sessions WHERE id='session2'"
            ).fetchone()
            session = json.loads(row[0])
            session["heartbeat_at"] = stale
            connection.execute(
                "UPDATE sessions SET document=? WHERE id='session2'",
                (json.dumps(session),),
            )
        self.assertEqual(
            Reader(self.root).session("session2")["display_status"], "unobserved"
        )
        self.assertEqual(Reader(self.root).session("session2")["status"], "running")
        with (
            closing(sqlite3.connect(self.root / "ledger.sqlite3")) as connection,
            connection,
        ):
            row = connection.execute(
                "SELECT document FROM runs WHERE id='candidate001'"
            ).fetchone()
            run = json.loads(row[0])
            run["updated_at"] = stale
            connection.execute(
                "UPDATE runs SET document=? WHERE id='candidate001'", (json.dumps(run),)
            )
        self.assertTrue(Reader(self.root).run("candidate001")["observation_stale"])

    def test_only_indexed_text_artifacts_are_read_and_symlinks_are_rejected(self):
        self.login()
        base = "/api/runs/baseline001/attempts/attempt0"
        self.assertEqual(
            self.client.get(
                f"{base}/preview", params={"path": "result.json"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"{base}/preview", params={"path": "../../ledger.sqlite3"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/api/runs/baseline001/attempts/attempt3/artifacts"
            ).status_code,
            404,
        )
        directory = self.root / "runs/baseline001/attempt0"
        secret = self.root / "private.txt"
        secret.write_text("not an artifact")
        (directory / "artifacts/link.txt").symlink_to(secret)
        write_json(
            directory / "artifact-index.json",
            [{"path": "link.txt", "bytes": 15, "sha256": "ignored"}],
        )
        response = self.client.get(f"{base}/preview", params={"path": "link.txt"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("not an artifact", response.text)
        (directory / "artifacts/linked").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            safe_file(
                self.root, "runs/baseline001/attempt0/artifacts/linked/private.txt"
            )

    def test_empty_workspace_is_not_initialized_by_reads(self):
        root = self.root / "missing"
        app = create_app(
            root, token=TOKEN, allowed_hosts=("testserver",), collect_host=False
        )
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"token": TOKEN})
            response = client.get("/api/snapshot")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ledger_available"])
        self.assertFalse(root.exists())

    def test_event_cursor_survives_reader_restart(self):
        first = Reader(self.root).events("session2", after=0, limit=1)
        rest = Reader(self.root).events("session2", after=first["cursor"])
        self.assertEqual(len(rest["events"]), 1)
        self.assertGreater(rest["cursor"], first["cursor"])


if __name__ == "__main__":
    unittest.main()
