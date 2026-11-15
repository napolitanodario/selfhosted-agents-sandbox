"""Workload contracts. Only this module knows Open-Enricher's file conventions."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .common import ExperimentError, identifier, read_json


def validate_manifest(document: dict, config) -> dict:
    if not isinstance(document, dict):
        raise ExperimentError("manifest must be an object")
    manifest = deepcopy(document)
    allowed = {"version", "experiment_id", "adapter", "profile", "source", "payload", "inputs", "hypothesis"}
    if set(manifest) - allowed or manifest.get("version") != 1:
        raise ExperimentError("unknown manifest fields or unsupported version")
    identifier(manifest.get("experiment_id"), "experiment_id")
    adapter = manifest.get("adapter")
    if adapter not in {"cpu-dedupe", "command", "open-enricher"}:
        raise ExperimentError(f"unknown workload adapter: {adapter}")
    if manifest.get("profile") not in config.profiles:
        raise ExperimentError("unknown execution profile")
    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        raise ExperimentError("payload must be an object")
    if adapter == "cpu-dedupe":
        if "source" in manifest:
            raise ExperimentError("cpu-dedupe is built in and does not accept a source repository")
        if set(payload) - {"texts", "case_sensitive", "expected_unique"}:
            raise ExperimentError("unknown cpu-dedupe payload field")
        texts = payload.get("texts")
        if not isinstance(texts, list) or not texts or len(texts) > 10000 or any(not isinstance(x, str) for x in texts):
            raise ExperimentError("texts must be a nonempty list of at most 10000 strings")
        if type(payload.get("case_sensitive")) is not bool or type(payload.get("expected_unique")) is not int:
            raise ExperimentError("case_sensitive (boolean) and expected_unique (integer) are required")
    else:
        source = manifest.get("source", {})
        if set(source) != {"repository", "revision"} or source.get("repository") not in config.repositories:
            raise ExperimentError("source requires a configured repository and revision")
        if not re.fullmatch(r"[a-f0-9]{40}", source.get("revision", "")):
            raise ExperimentError("source.revision must be a full immutable Git commit SHA")
        if adapter == "command":
            if set(payload) - {"workload", "parameters"} or payload.get("workload") not in config.workloads:
                raise ExperimentError("command payload requires a configured workload and optional parameters")
            if not isinstance(payload.get("parameters", {}), dict):
                raise ExperimentError("parameters must be an object")
            workload = config.workloads[payload["workload"]]
            if workload.get("repository") != source["repository"]:
                raise ExperimentError("workload is not approved for this source repository")
            if not isinstance(workload.get("argv"), list) or not workload["argv"] or any(not isinstance(x, str) for x in workload["argv"]):
                raise ExperimentError("workload configuration requires an argv list")
        else:
            validate_open_enricher(payload)
            profile = config.profiles[manifest["profile"]]
            if not profile.get("video_inputs"):
                raise ExperimentError("Open-Enricher profile requires video_inputs on the execution host")
    inputs = manifest.setdefault("inputs", [])
    if not isinstance(inputs, list):
        raise ExperimentError("inputs must be a list of {ref, sha256} documents")
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {"ref", "sha256"} or not isinstance(item["ref"], str) or not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", "")):
            raise ExperimentError("each input requires ref and a SHA-256 digest")
    if adapter == "open-enricher" and inputs:
        if len(inputs) != len(payload["videos"]) or {i["ref"] for i in inputs} != set(payload["videos"]):
            raise ExperimentError("Open-Enricher input digests must cover exactly the requested videos")
    return manifest


def validate_open_enricher(payload):
    if set(payload) != {"variant", "videos"}:
        raise ExperimentError("Open-Enricher payload requires exactly variant and videos")
    videos = payload["videos"]
    if not isinstance(videos, list) or not videos or any(not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_. -]*", v) for v in videos) or len(set(videos)) != len(videos):
        raise ExperimentError("videos must be unique filenames without directory components")
    variant = payload["variant"]
    fields = {"experiment_id", "title", "interval_seconds", "vlm_memory_scenes", "visual_only", "narrate_from_visual"}
    if not isinstance(variant, dict) or set(variant) != fields:
        raise ExperimentError("variant must contain exactly the six supported Open-Enricher fields")
    identifier(variant["experiment_id"], "variant.experiment_id")
    if not isinstance(variant["title"], str) or not variant["title"].strip():
        raise ExperimentError("variant.title must be nonempty")
    interval = variant["interval_seconds"]
    memory = variant["vlm_memory_scenes"]
    if type(interval) is not int or interval <= 0:
        raise ExperimentError("interval_seconds must be a positive integer")
    if memory is not None and (type(memory) is not int or memory < 0):
        raise ExperimentError("vlm_memory_scenes must be null or a nonnegative integer")
    if any(type(variant[k]) is not bool for k in ("visual_only", "narrate_from_visual")):
        raise ExperimentError("visual_only and narrate_from_visual must be booleans")


def normalize_open_enricher(source: Path, payload: dict, exit_code: int) -> dict:
    """CLI exit zero is insufficient: verify every requested video's native outcome."""
    exp_id = payload["variant"]["experiment_id"]
    archive = source / "runs/local" / exp_id
    pipeline_issues, evaluation_issues = [], []
    try:
        rows = read_json(archive / "video_results.json")
        if not isinstance(rows, list) or len(rows) != len(payload["videos"]):
            raise ValueError("unexpected number of video outcomes")
        by_video = {r["video"]: r for r in rows}
        if set(by_video) != set(payload["videos"]):
            raise ValueError("requested and returned videos differ")
        for video in payload["videos"]:
            row = by_video[video]
            if row.get("status") != "success":
                pipeline_issues.append(f"{video}: {row.get('error') or row.get('status')}")
            for filename in ("result.json", f"{video}_llm.json", "timings.json"):
                if not (archive / video / filename).is_file():
                    pipeline_issues.append(f"missing {video}/{filename}")
                else:
                    read_json(archive / video / filename)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        pipeline_issues.append(f"incomplete native pipeline result: {exc}")
    try:
        summaries = read_json(source / "runs/local/loop_summary.json")
        summary = next(s for s in summaries if s.get("experiment_id") == exp_id)
        evaluations = summary.get("evaluation", [])
        evaluation_ok = (len(evaluations) == len(payload["videos"])
                         and {r.get("video") for r in evaluations} == set(payload["videos"])
                         and all(r.get("status") == "success" for r in evaluations))
        metrics = read_json(archive / "scores.json")
        if not isinstance(metrics, dict) or set(metrics) != set(payload["videos"]):
            raise ValueError("scores do not cover the requested cohort")
        for video in payload["videos"]:
            if not isinstance(read_json(archive / video / "score.json"), dict):
                raise ValueError(f"invalid score document: {video}")
        if not evaluation_ok:
            evaluation_issues.append("one or more native evaluations failed or are missing")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
        evaluation_issues.append(f"incomplete native evaluation: {exc}")
        evaluation_ok, metrics = False, {}
    if exit_code != 0:
        pipeline_issues.append(f"process exit {exit_code}")
    return {"pipeline": "failed" if pipeline_issues else "succeeded",
            "evaluation": "succeeded" if evaluation_ok else "failed",
            "metrics": metrics, "issues": pipeline_issues + evaluation_issues}


def adapter_instructions(adapter: str) -> str:
    return {
        "cpu-dedupe": "Payload: texts (nonempty string list), case_sensitive (boolean), expected_unique (integer). Compare exact and case-insensitive deduplication with the same texts.",
        "open-enricher": "Payload: videos (unique filenames), variant {experiment_id,title,interval_seconds (positive integer),vlm_memory_scenes (nonnegative integer or null),visual_only (boolean),narrate_from_visual (boolean)}. Change one variable at a time; keep the video cohort fixed. Native scores, timings and profiles are evidence; process exit zero does not prove success.",
        "command": "Payload: workload (operator-configured name), parameters (JSON object). The trusted command receives parameters via EXPERIMENT_INPUT and must write EXPERIMENT_RESULT as {pipeline: succeeded|failed, evaluation: succeeded|failed|not_requested, metrics: {...}}.",
    }[adapter]
