"""Durable submit, reconcile, collect and cancel operations used by CLI/coordinator."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
import uuid
import zipfile

from .adapters import validate_manifest
from .backends import backend
from .common import TERMINAL, ExperimentError, digest, extract_archive, file_digest, now, read_json, write_json
from .store import Store


def build_worker(target):
    package = Path(__file__).parent
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", "from experiment_orchestrator.remote import main\nraise SystemExit(main())\n")
        for name in ("__init__", "common", "adapters", "worker", "remote"):
            archive.write(package / f"{name}.py", f"experiment_orchestrator/{name}.py")


class Broker:
    def __init__(self, config):
        self.config = config
        self.store = Store(config.state_dir)

    def attempt_dir(self, run):
        return self.config.state_dir / "runs" / run["id"] / run["attempts"][-1]["id"]

    def _capacity(self):
        active = sum(r["status"] not in {"succeeded", "failed", "cancelled"} for r in self.store.list_runs())
        if active >= self.config.max_active_runs:
            raise ExperimentError("active run budget exhausted; reconcile or cancel existing runs")

    def submit_experiment(self, manifest, request_id):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ExperimentError("request_id must contain 1–200 characters")
        manifest = validate_manifest(manifest, self.config)
        fingerprint = digest(manifest)
        with self.store.lock():
            existing = self.store.by_request(request_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ExperimentError("request_id already belongs to a different manifest")
                return existing
            self._capacity()
            run = {"id": uuid.uuid4().hex, "request_id": request_id, "fingerprint": fingerprint,
                   "manifest": manifest, "status": "prepared", "created_at": now(), "attempts": [],
                   "profile": deepcopy(self.config.profiles[manifest["profile"]]),
                   "max_attempts": self.config.max_attempts, "artifact_limit_bytes": self.config.artifact_limit_bytes}
            if manifest["adapter"] == "command":
                run["workload"] = deepcopy(self.config.workloads[manifest["payload"]["workload"]])
            self._prepare(run)
            self._launch(run)
            return run

    def _prepare(self, run):
        attempt = {"id": uuid.uuid4().hex, "number": len(run["attempts"]) + 1, "scheduler": "prepared",
                   "pipeline": "pending", "evaluation": "pending", "artifacts": "pending", "created_at": now()}
        run["attempts"].append(attempt)
        run["status"] = "prepared"
        # Intent is committed before preparing files or contacting an execution host.
        self.store.save(run, "attempt_created", attempt["id"])
        root = self.attempt_dir(run)
        root.mkdir(parents=True, exist_ok=True)
        try:
            job = {"run_id": run["id"], "attempt_id": attempt["id"], "created_at": now(),
                   "manifest": run["manifest"], "profile": run["profile"], "workload": run.get("workload")}
            if len(run["attempts"]) > 1:
                original = root.parent / run["attempts"][0]["id"]
                import shutil
                for name in ("source.tar", "worker.pyz"):
                    if (original / name).exists():
                        shutil.copyfile(original / name, root / name)
            if "source" in run["manifest"] and not (root / "source.tar").exists():
                source = run["manifest"]["source"]
                repository = self.config.repositories[source["repository"]]
                revision = subprocess.run(["git", "-C", repository, "rev-parse", source["revision"] + "^{commit}"], capture_output=True, text=True, check=True).stdout.strip()
                if revision != source["revision"]:
                    raise ExperimentError("revision must identify a commit")
                subprocess.run(["git", "-C", repository, "archive", "--format=tar", "--output", str(root / "source.tar"), revision], check=True, capture_output=True)
            if (root / "source.tar").exists():
                job["source_sha256"] = file_digest(root / "source.tar")
            if not (root / "worker.pyz").exists():
                build_worker(root / "worker.pyz")
            job["worker_sha256"] = file_digest(root / "worker.pyz")
            write_json(root / "job.json", job)
            attempt["bundle_digest"] = digest(job)
            self.store.save(run, "bundle_prepared", attempt["bundle_digest"])
        except Exception as exc:
            attempt.update(scheduler="failed", error=str(exc))
            run["status"] = "failed"
            self.store.save(run, "preparation_failed", str(exc))
            raise ExperimentError(f"preparation failed for run {run['id']}: {exc}") from exc

    def _launch(self, run):
        attempt = run["attempts"][-1]
        root = self.attempt_dir(run)
        attempt["scheduler"] = "submitting"
        run["status"] = "submitting"
        self.store.save(run, "submission_started")
        try:
            execution = backend(run["profile"])
            execution.stage(root)
            observation = execution.submit(root)
            attempt.update(scheduler=observation.get("state", "queued"), observation=observation)
            run["status"] = attempt["scheduler"]
        except Exception as exc:
            attempt.update(scheduler="unknown", error=str(exc))
            run["status"] = "unknown"
        self.store.save(run, "submission_observed", attempt.get("observation", attempt.get("error")))

    def get_run(self, run_id, refresh=False):
        if not refresh:
            return self.store.get(run_id)
        with self.store.lock():
            run = self.store.get(run_id)
            attempt = run["attempts"][-1]
            root = self.attempt_dir(run)
            if attempt["scheduler"] == "prepared":
                if not (root / "job.json").exists():
                    attempt.update(scheduler="failed", error="preparation interrupted before bundle was committed")
                    run["status"] = "failed"
                    self.store.save(run, "preparation_interrupted")
                    return run
                self._launch(run)
            execution = backend(run["profile"])
            try:
                if attempt["scheduler"] not in TERMINAL:
                    # Replaying submit is safe: both local and remote backends fence the launch.
                    if attempt["scheduler"] in {"submitting", "unknown"} and not attempt.get("cancel_requested"):
                        execution.stage(root)
                        execution.submit(root)
                    if attempt.get("cancel_requested"):
                        execution.cancel(root)
                    observation = execution.status(root)
                    attempt.update(scheduler=observation.get("state", "unknown"), observation=observation)
                if attempt["scheduler"] in TERMINAL and attempt["artifacts"] != "ready":
                    archive = execution.collect(root, run["artifact_limit_bytes"])
                    destination = root / "artifacts"
                    extract_archive(archive, destination, run["artifact_limit_bytes"])
                    result_path = destination / "result.json"
                    if result_path.exists():
                        result = read_json(result_path)
                        if result.get("run_id") != run_id or result.get("attempt_id") != attempt["id"]:
                            raise ExperimentError("artifact identity mismatch")
                        if result.get("pipeline") not in {"succeeded", "failed"} or result.get("evaluation") not in {"succeeded", "failed", "not_requested"}:
                            raise ExperimentError("invalid worker result contract")
                        attempt.update(pipeline=result["pipeline"], evaluation=result["evaluation"], result=result, artifacts="ready")
                    else:
                        attempt.update(pipeline="failed", evaluation="not_requested", artifacts="partial")
                    index = [{"path": str(p.relative_to(destination)), "sha256": file_digest(p), "bytes": p.stat().st_size}
                             for p in sorted(destination.rglob("*")) if p.is_file()]
                    write_json(root / "artifact-index.json", index)
                    attempt["artifact_index"] = str(root / "artifact-index.json")
                attempt.pop("error", None)
            except Exception as exc:
                attempt["error"] = str(exc)
                if attempt["scheduler"] not in TERMINAL:
                    attempt["scheduler"] = "unknown"
            self._derive_status(run)
            self.store.save(run, "reconciled", {k: attempt[k] for k in ("scheduler", "pipeline", "evaluation", "artifacts")})
            return run

    @staticmethod
    def _derive_status(run):
        a = run["attempts"][-1]
        if a["scheduler"] == "cancelled":
            run["status"] = "cancelled"
        elif a["scheduler"] in TERMINAL:
            if a["artifacts"] == "pending":
                run["status"] = "collecting"
            else:
                run["status"] = "succeeded" if (a["scheduler"] == "completed" and a["pipeline"] == "succeeded" and a["evaluation"] in {"succeeded", "not_requested"} and a["artifacts"] == "ready") else "failed"
        else:
            run["status"] = a["scheduler"]

    def cancel_run(self, run_id):
        with self.store.lock():
            run = self.store.get(run_id)
            attempt = run["attempts"][-1]
            if attempt["scheduler"] in TERMINAL:
                return run
            attempt["cancel_requested"] = True
            self.store.save(run, "cancellation_requested")
            try:
                backend(run["profile"]).cancel(self.attempt_dir(run))
            except Exception as exc:
                attempt["error"] = str(exc)
            self.store.save(run, "cancellation_sent")
        return self.get_run(run_id, refresh=True)

    def retry(self, run_id):
        with self.store.lock():
            run = self.store.get(run_id)
            if run["status"] not in {"failed", "cancelled"} or run["attempts"][-1]["scheduler"] not in TERMINAL:
                raise ExperimentError("retry requires a confirmed failed/cancelled terminal run; unknown submissions cannot be retried")
            if len(run["attempts"]) >= run["max_attempts"]:
                raise ExperimentError("attempt budget exhausted")
            self._capacity()
            self._prepare(run)
            self._launch(run)
            return run

    def get_artifacts(self, run_id):
        run = self.store.get(run_id)
        root = self.attempt_dir(run)
        path = root / "artifact-index.json"
        return {"root": str(root / "artifacts"), "files": read_json(path) if path.exists() else [],
                "status": run["attempts"][-1]["artifacts"]}

    def resolve_unknown(self, run_id, state, reason):
        """Operator-only recovery after independently establishing no job remains active."""
        if state not in {"failed", "cancelled"} or not isinstance(reason, str) or not reason.strip():
            raise ExperimentError("resolution requires failed/cancelled and a nonempty evidence/reason")
        with self.store.lock():
            run = self.store.get(run_id)
            attempt = run["attempts"][-1]
            if attempt["scheduler"] != "unknown":
                raise ExperimentError("only an unknown attempt can be resolved manually")
            attempt.update(scheduler=state, pipeline="failed", evaluation="not_requested", artifacts="partial",
                           operator_resolution={"state": state, "reason": reason, "time": now()})
            self._derive_status(run)
            self.store.save(run, "operator_resolution", attempt["operator_resolution"])
            return run
