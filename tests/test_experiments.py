"""Contract and lifecycle tests, including real detached CPU jobs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from agent_harness.harness import run_task
from agent_harness.loader import load_task
from experiment_orchestrator.adapters import normalize_open_enricher, validate_manifest
from experiment_orchestrator.broker import Broker
from experiment_orchestrator.common import ExperimentError, extract_archive, read_json, write_json
from experiment_orchestrator.config import Config
from experiment_orchestrator.coordinator import Coordinator, ROLES
from experiment_orchestrator import remote
from support import FakeFactory, FakeSession


ROOT = Path(__file__).resolve().parents[1]


def manifest(profile="cpu"):
    return {"version": 1, "experiment_id": "test", "adapter": "cpu-dedupe", "profile": profile,
            "payload": {"texts": ["Alpha", "alpha", "Beta", "Beta"], "case_sensitive": False, "expected_unique": 2}}


class ExperimentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(self.root / "config.yaml", self.root / "state",
                             {"cpu": {"backend": "local-process", "trusted_local": True, "timeout_seconds": 10}}, {}, {})
        self.broker = Broker(self.config)

    def finish(self, run_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.broker.get_run(run_id, refresh=True)
            if run["status"] in {"succeeded", "failed", "cancelled"}:
                return run
            time.sleep(0.03)
        self.fail(f"run did not finish: {run}")

    def test_detached_cpu_restart_idempotency_and_artifacts(self):
        run = self.broker.submit_experiment(manifest(), "request-1")
        self.broker = Broker(self.config)
        repeated = self.broker.submit_experiment(manifest(), "request-1")
        self.assertEqual(run["id"], repeated["id"])
        finished = self.finish(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        self.assertEqual(len(finished["attempts"]), 1)
        result = finished["attempts"][-1]["result"]
        self.assertEqual(result["metrics"]["unique_count"], 2)
        artifacts = self.broker.get_artifacts(run["id"])
        self.assertIn("output/unique.json", [i["path"] for i in artifacts["files"]])
        self.assertTrue(all(len(i["sha256"]) == 64 for i in artifacts["files"]))
        changed = manifest()
        changed["payload"]["expected_unique"] = 3
        with self.assertRaisesRegex(ExperimentError, "different manifest"):
            self.broker.submit_experiment(changed, "request-1")

    def test_pipeline_and_evaluation_are_separate_and_retries_bounded(self):
        spec = manifest()
        spec["payload"]["expected_unique"] = 999
        run = self.finish(self.broker.submit_experiment(spec, "wrong-expectation")["id"])
        attempt = run["attempts"][-1]
        self.assertEqual((run["status"], attempt["scheduler"], attempt["pipeline"], attempt["evaluation"]),
                         ("failed", "completed", "succeeded", "failed"))
        original = self.broker.attempt_dir(run)
        rerun = self.finish(self.broker.retry(run["id"])["id"])
        self.assertNotEqual(original, self.broker.attempt_dir(rerun))
        self.assertTrue((original / "result.json").exists())
        with self.assertRaisesRegex(ExperimentError, "budget"):
            self.broker.retry(run["id"])

    def test_unknown_submission_cannot_be_retried(self):
        with patch("experiment_orchestrator.backends.LocalBackend.submit", side_effect=TimeoutError("lost")):
            run = self.broker.submit_experiment(manifest(), "lost")
        self.assertEqual(run["status"], "unknown")
        with self.assertRaisesRegex(ExperimentError, "unknown submissions"):
            self.broker.retry(run["id"])
        with self.assertRaises(ExperimentError):
            self.broker.resolve_unknown(run["id"], "failed", "")
        resolved = self.broker.resolve_unknown(run["id"], "failed", "Test backend failed before creating a process; no process exists")
        self.assertEqual(resolved["status"], "failed")
        self.assertEqual(self.broker.store.events(run["id"])[-1]["event"], "operator_resolution")

    def test_concurrent_duplicate_requests_create_only_one_run(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(Broker(self.config).submit_experiment, manifest(), "concurrent") for _ in range(2)]
            runs = [future.result() for future in futures]
        self.assertEqual(runs[0]["id"], runs[1]["id"])
        self.assertEqual(len(self.broker.store.list_runs()), 1)
        self.assertEqual(self.finish(runs[0]["id"])["status"], "succeeded")

    def test_invalid_manifest_has_no_submission_side_effects(self):
        spec = manifest()
        spec["payload"]["case_sensitive"] = "false"
        with self.assertRaises(ExperimentError):
            self.broker.submit_experiment(spec, "invalid")
        self.assertEqual(self.broker.store.list_runs(), [])

    def test_active_run_limit_includes_unknown_submissions(self):
        self.config.max_active_runs = 1
        with patch("experiment_orchestrator.backends.LocalBackend.submit", side_effect=TimeoutError("lost")):
            self.broker.submit_experiment(manifest(), "one")
            with self.assertRaisesRegex(ExperimentError, "active run budget"):
                self.broker.submit_experiment(manifest(), "two")

    def test_collection_failure_can_be_recovered_without_resubmitting(self):
        run = self.broker.submit_experiment(manifest(), "collect-retry")
        root = self.broker.attempt_dir(run)
        deadline = time.monotonic() + 5
        while not (root / "worker.finished.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with patch("experiment_orchestrator.backends.LocalBackend.collect", side_effect=OSError("download interrupted")):
            observed = self.broker.get_run(run["id"], refresh=True)
        self.assertEqual(observed["status"], "collecting")
        self.assertEqual(observed["attempts"][-1]["scheduler"], "completed")
        with self.assertRaises(ExperimentError):
            self.broker.retry(run["id"])
        recovered = Broker(self.config).get_run(run["id"], refresh=True)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(len(recovered["attempts"]), 1)

    def test_invalid_agent_proposal_is_not_cached_and_can_be_regenerated(self):
        task = load_task(ROOT / "tasks/hello/task.yaml")
        bad = manifest()["payload"]
        bad["texts"] = ["changed cohort"]
        responses = [{"hypothesis": "bad cohort", "payload": bad},
                     {"hypothesis": "valid cohort", "payload": manifest()["payload"]}]
        class Factory:
            async def open(inner, task, env):
                session = FakeSession()
                session.files["/workspace/response.json"] = json.dumps(responses.pop(0))
                return session
        coordinator = Coordinator(self.broker, Factory())
        document = {"id": "fixture", "iterations": [], "baseline": manifest(), "responses": [],
                    "agent_tasks": {"proposer": asdict(task)}}
        with self.assertRaisesRegex(ExperimentError, "cohort"):
            asyncio.run(coordinator._ask(document, "proposer", {}))
        accepted = self.root / "state/campaigns/fixture/000-proposer/accepted.json"
        self.assertFalse(accepted.exists())
        response = asyncio.run(coordinator._ask(document, "proposer", {}))
        self.assertEqual(response["hypothesis"], "valid cohort")
        self.assertTrue(accepted.exists())

    def test_remote_submit_fence_survives_missing_receipt(self):
        root = self.root / "remote"
        root.mkdir()
        write_json(root / "job.json", {"attempt_id": "abc", "created_at": "2026-11-15T10:00:00", "profile": {"slurm": {}}})
        calls = []
        def command(argv):
            calls.append(argv)
            if argv[0] == "sbatch":
                raise subprocess.TimeoutExpired(argv, 45)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(remote, "command", command):
            with self.assertRaises(subprocess.TimeoutExpired):
                remote.submit(root)
            self.assertEqual(remote.submit(root)["state"], "unknown")
        self.assertEqual(sum(argv[0] == "sbatch" for argv in calls), 1)
        with patch.object(remote, "command", lambda argv: subprocess.CompletedProcess(argv, 0, "123|exp-abc\n", "")):
            self.assertEqual(remote.submit(root)["job_id"], "123")

    def test_slurm_accounting_lag_is_unknown_then_terminal(self):
        root = self.root / "remote"
        root.mkdir()
        write_json(root / "receipt.json", {"job_id": "123"})
        with patch.object(remote, "command", lambda argv: subprocess.CompletedProcess(argv, 0, "", "")):
            self.assertEqual(remote.status(root)["state"], "unknown")
        def command(argv):
            return subprocess.CompletedProcess(argv, 0, "123|TIMEOUT|0:15\n" if argv[0] == "sacct" else "", "")
        with patch.object(remote, "command", command):
            self.assertEqual(remote.status(root)["state"], "failed")

    def test_open_enricher_zero_exit_does_not_mask_missing_or_failed_video(self):
        payload = {"variant": {"experiment_id": "test"}, "videos": ["film.mp4"]}
        archive = self.root / "runs/local/test"
        write_json(archive / "video_results.json", [{"video": "film.mp4", "status": "error", "error": "out of memory"}])
        write_json(self.root / "runs/local/loop_summary.json", [{"experiment_id": "test", "evaluation": []}])
        result = normalize_open_enricher(self.root, payload, 0)
        self.assertEqual(result["pipeline"], "failed")
        self.assertEqual(result["evaluation"], "failed")
        self.assertTrue(any("out of memory" in x for x in result["issues"]))
        write_json(archive / "video_results.json", [{"video": "film.mp4", "status": "success"}])
        for name in ("result.json", "film.mp4_llm.json", "timings.json"):
            write_json(archive / "film.mp4" / name, {})
        result = normalize_open_enricher(self.root, payload, 0)
        self.assertEqual(result["pipeline"], "succeeded")
        self.assertEqual(result["evaluation"], "failed")
        write_json(self.root / "runs/local/loop_summary.json", [{"experiment_id": "test", "evaluation": [{"video": "film.mp4", "status": "success"}]}])
        write_json(archive / "scores.json", {"film.mp4": {"overall": 80}})
        (archive / "film.mp4/score.json").write_text("broken json")
        result = normalize_open_enricher(self.root, payload, 0)
        self.assertEqual(result["pipeline"], "succeeded")
        self.assertEqual(result["evaluation"], "failed")
        write_json(archive / "film.mp4/score.json", {"overall": 80})
        result = normalize_open_enricher(self.root, payload, 0)
        self.assertEqual(result["evaluation"], "succeeded")

    def test_archives_reject_traversal_and_links(self):
        for name, link in (("../escape", False), ("output", True)):
            archive = self.root / "unsafe.tar"
            with tarfile.open(archive, "w") as tar:
                entry = tarfile.TarInfo(name)
                if link:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "/etc/passwd"
                tar.addfile(entry, io.BytesIO())
            with self.assertRaises(ExperimentError):
                extract_archive(archive, self.root / "collected")
        self.assertFalse((self.root / "escape").exists())

    def command_manifest(self, script):
        repository = self.root / "workload"
        repository.mkdir()
        (repository / "experiment.py").write_text(script)
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(["git", "-C", str(repository), "add", "experiment.py"], check=True)
        subprocess.run(["git", "-C", str(repository), "-c", "user.email=test@example.test", "-c", "user.name=Test", "commit", "-qm", "fixture"], check=True)
        revision = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
        self.config.repositories["fixture"] = str(repository)
        self.config.workloads["fixture"] = {"repository": "fixture", "argv": [sys.executable, "experiment.py"]}
        return {"version": 1, "experiment_id": "generic", "adapter": "command", "profile": "cpu",
                "source": {"repository": "fixture", "revision": revision}, "payload": {"workload": "fixture", "parameters": {}}}

    def test_generic_command_uses_pinned_commit_not_dirty_workspace(self):
        script = (ROOT / "examples/command-workload/experiment.py").read_text()
        spec = self.command_manifest(script)
        (self.root / "workload/experiment.py").write_text("raise RuntimeError('dirty tree must not be executed')")
        run = self.finish(self.broker.submit_experiment(spec, "generic")["id"])
        self.assertEqual(run["status"], "succeeded", run)
        self.assertEqual(run["attempts"][-1]["result"]["metrics"], {"sum": 6})

    def test_cancellation_stops_a_running_command_and_preserves_evidence(self):
        spec = self.command_manifest("import time\nprint('started', flush=True)\ntime.sleep(120)\n")
        run = self.broker.submit_experiment(spec, "cancel")
        root = self.broker.attempt_dir(run)
        deadline = time.monotonic() + 5
        while not (root / "stdout.log").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.broker.cancel_run(run["id"])
        run = self.finish(run["id"])
        self.assertEqual(run["status"], "cancelled", run)
        self.assertIn("stdout.log", [x["path"] for x in self.broker.get_artifacts(run["id"])["files"]])

    def test_export_precedes_sandbox_teardown_and_report_contains_path(self):
        factory = FakeFactory()
        factory.session.files["/workspace/response.json"] = '{"approved":true}'
        report = asyncio.run(run_task(load_task(ROOT / "tasks/hello/task.yaml"), factory=factory,
            report_dir=self.root / "reports", exports=("response.json",), artifact_dir=self.root / "export"))
        self.assertTrue(factory.session.destroyed)
        self.assertTrue((self.root / "export/response.json").exists())
        self.assertEqual(read_json(Path(report.report_path))["report_path"], report.report_path)

    def test_campaign_runs_all_roles_and_recovers_without_live_sessions(self):
        definition = {"version": 1, "objective": "compare deduplication", "rubric": "count unique texts",
                      "max_runs": 2, "baseline": manifest(), "agents": {}}
        definition["baseline"]["payload"].update(case_sensitive=True, expected_unique=3)
        responses = {"proposer": {"hypothesis": "case-fold removes a duplicate", "payload": manifest()["payload"]},
                     "reviewer": {"approved": True, "reason": "fixed cohort"},
                     "evaluator": {"summary": "3 to 2", "metrics": {"delta_unique": -1}},
                     "analyst": {"summary": "confirmed", "next_action": "stop"}}
        for role in ROLES:
            directory = self.root / role
            directory.mkdir()
            path = directory / "task.yaml"
            path.write_text(yaml.safe_dump({"id": role, "agent": {"kind": "scripted", "commands": [role]}}))
            definition["agents"][role] = str(path)
        path = self.root / "campaign.yaml"
        path.write_text(yaml.safe_dump(definition))
        contexts = {}
        class Factory:
            async def open(inner, task, env):
                session = FakeSession()
                session.files["/workspace/response.json"] = json.dumps(responses[task.id])
                contexts[task.id] = json.loads(dict(task.files)["context.json"])
                return session
        coordinator = Coordinator(self.broker, Factory())
        campaign = coordinator.create(path)
        for _ in range(100):
            # Reconstruct the coordinator each time to exercise checkpoint recovery.
            coordinator = Coordinator(Broker(self.config), Factory())
            campaign = asyncio.run(coordinator.step(campaign["id"]))
            if campaign["phase"] in {"done", "needs_attention"}:
                break
            time.sleep(0.02)
        self.assertEqual(campaign["phase"], "done", campaign.get("error"))
        self.assertEqual(len(campaign["runs"]), 2)
        self.assertEqual(set(contexts), set(ROLES))
        self.assertNotIn("hypothesis", contexts["evaluator"]["candidate"]["manifest"])
        self.assertNotIn("proposal", contexts["evaluator"])
        self.assertIn("proposal", contexts["analyst"]["iteration"])
        self.assertEqual(len(self.broker.store.list_runs()), 2)


if __name__ == "__main__":
    unittest.main()
