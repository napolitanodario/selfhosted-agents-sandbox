"""Standard-library worker executed in a job, never inside an agent session."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .adapters import normalize_open_enricher
from .common import ExperimentError, extract_archive, file_digest, now, read_json, write_json


def process_token(pid: int):
    """Linux process birth time prevents mistaking a recycled PID for this worker."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if data[0] == "Z" else data[19]
    except (OSError, IndexError):
        return None


def execute(argv, cwd, env, root, timeout):
    deadline = time.monotonic() + timeout
    with (root / "stdout.log").open("a") as stdout, (root / "stderr.log").open("a") as stderr:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            while child.poll() is None:
                if (root / "cancel.request").exists() or time.monotonic() >= deadline:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    return 124 if not (root / "cancel.request").exists() else 130
                time.sleep(0.25)
            return child.returncode
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def run(root: Path) -> int:
    root = root.resolve()
    job = read_json(root / "job.json")
    profile, manifest = job["profile"], job["manifest"]
    write_json(root / "worker.started.json", {"time": now(), "pid": os.getpid(), "token": process_token(os.getpid())})
    signal.signal(signal.SIGTERM, lambda *_: (root / "cancel.request").touch())
    result = {"version": 1, "run_id": job["run_id"], "attempt_id": job["attempt_id"],
              "pipeline": "failed", "evaluation": "not_requested", "metrics": {}, "issues": [], "started_at": now()}
    exit_code = 1
    try:
        if (root / "cancel.request").exists():
            raise ExperimentError("cancelled before execution")
        env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES", "LD_LIBRARY_PATH") if name in os.environ}
        for name in profile.get("env_from", []):
            if name not in os.environ:
                raise ExperimentError(f"runtime environment variable is missing: {name}")
            env[name] = os.environ[name]
        env.update({str(k): str(v) for k, v in profile.get("env", {}).items()})
        source = root / "source"
        if job.get("source_sha256"):
            if file_digest(root / "source.tar") != job["source_sha256"]:
                raise ExperimentError("source archive digest mismatch")
            extract_archive(root / "source.tar", source)
        payload = manifest["payload"]
        adapter = manifest["adapter"]
        if adapter == "cpu-dedupe":
            texts = payload["texts"]
            unique = list(dict.fromkeys(texts if payload["case_sensitive"] else [text.casefold() for text in texts]))
            write_json(root / "output/unique.json", unique)
            result.update(pipeline="succeeded", evaluation="succeeded" if len(unique) == payload["expected_unique"] else "failed",
                          metrics={"input_count": len(texts), "unique_count": len(unique), "duplicate_count": len(texts) - len(unique)})
            exit_code = 0
        elif adapter == "command":
            output = root / "output"
            output.mkdir(exist_ok=True)
            write_json(root / "input.json", payload.get("parameters", {}))
            env.update(EXPERIMENT_INPUT=str(root / "input.json"), EXPERIMENT_RESULT=str(output / "result.json"),
                       EXPERIMENT_OUTPUT=str(output))
            exit_code = execute(job["workload"]["argv"], source, env, root, profile.get("timeout_seconds", 86400))
            native = read_json(output / "result.json")
            if native.get("pipeline") not in {"succeeded", "failed"} or native.get("evaluation") not in {"succeeded", "failed", "not_requested"} or not isinstance(native.get("metrics"), dict):
                raise ExperimentError("invalid command workload result contract")
            result.update({k: native[k] for k in ("pipeline", "evaluation", "metrics")})
            if exit_code:
                result["pipeline"] = "failed"
        elif adapter == "open-enricher":
            videos_root = Path(profile["video_inputs"])
            expected = {i["ref"]: i["sha256"] for i in manifest["inputs"]}
            observed = []
            for video in payload["videos"]:
                path = videos_root / video
                checksum = file_digest(path)
                if video in expected and checksum != expected[video]:
                    raise ExperimentError(f"input digest mismatch: {video}")
                observed.append({"ref": video, "sha256": checksum})
            write_json(root / "observed-inputs.json", observed)
            variant_dir = source / "src/open_enricher/experiments/variants"
            # Only this private checkout is changed; the standalone repository is untouched.
            for existing in variant_dir.glob("*.json"):
                existing.unlink()
            variant = payload["variant"]
            write_json(variant_dir / f"{variant['experiment_id']}.json", variant)
            if profile.get("synopsis_dir"):
                (source / "synopsis").mkdir(exist_ok=True)
                for video in payload["videos"]:
                    name = f"{video}_summary.txt"
                    shutil.copyfile(Path(profile["synopsis_dir"]) / name, source / "synopsis" / name)
            env.update(OPEN_ENRICHER_DATASET_ROOT=str(root / "dataset"), OPEN_ENRICHER_VIDEO_INPUTS=str(videos_root),
                       PYTHONPATH=str(source / "src"))
            argv = [profile.get("python", sys.executable), "-m", "open_enricher", "--only", variant["experiment_id"],
                    "--video", *payload["videos"], "--no-cache", "--force"]
            exit_code = execute(argv, source, env, root, profile.get("timeout_seconds", 86400))
            result.update(normalize_open_enricher(source, payload, exit_code))
        else:
            raise ExperimentError(f"unknown worker adapter: {adapter}")
    except Exception as exc:
        result["pipeline"] = "failed"
        result["issues"].append(str(exc))
    result.update(exit_code=exit_code, finished_at=now())
    write_json(root / "result.json", result)
    scheduler = "cancelled" if (root / "cancel.request").exists() else ("completed" if exit_code == 0 else "failed")
    write_json(root / "worker.finished.json", {"state": scheduler, "time": now(), "exit_code": exit_code})
    return 0 if scheduler == "completed" else 1
