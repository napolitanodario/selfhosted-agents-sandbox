"""Read projections of existing state without constructing a broker.

Opening the dashboard never reconciles, submits, retries, or cancels a job. The
coordinator remains responsible for cluster observations and artifact collection.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat

from experiment_orchestrator.telemetry import DATABASE, Redactor


FINAL_RUNS = {"succeeded", "failed", "cancelled"}
HISTORY_LIMIT = 500
MAX_PREVIEW = 256 * 1024


def age_seconds(timestamp: str | None) -> float | None:
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return None
        return max(0, (datetime.now(timezone.utc) - moment).total_seconds())
    except (AttributeError, TypeError, ValueError):
        return None


def project_id(manifest: dict) -> str:
    return manifest.get("source", {}).get("repository") or manifest.get(
        "adapter", "unassigned"
    )


def numeric_metrics(value, prefix="", depth=0) -> dict[str, float]:
    """Keep names intact across arbitrary workload-specific metric dictionaries."""
    if depth > 8:
        return {}
    result = {}
    if isinstance(value, dict):
        for key, item in list(value.items())[:200]:
            name = f"{prefix}.{key}" if prefix else str(key)
            result.update(numeric_metrics(item, name, depth + 1))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                result[prefix] = value
        except OverflowError:
            # A JSON integer may be larger than any browser numeric value.
            pass
    return result


@contextmanager
def read_database(path: Path):
    """mode=ro also prevents accidentally creating an empty production ledger."""
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def safe_file(root: Path, relative: str, limit=MAX_PREVIEW) -> tuple[bytes, int]:
    """Walk with openat/O_NOFOLLOW, including parent directories, to reject links."""
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"..", "."} for part in path.parts)
    ):
        raise FileNotFoundError(relative)
    if any(
        part.startswith(".") or part in {"auth.json", "secrets"} for part in path.parts
    ):
        raise FileNotFoundError(relative)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(path.parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(path.parts) - 1:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FileNotFoundError(relative)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(limit), metadata.st_size
    finally:
        os.close(descriptor)


class Reader:
    def __init__(self, root: Path, redactor: Redactor | None = None):
        self.root = root.resolve()
        self.redactor = redactor or Redactor()

    def _run(self, document):
        manifest = document["manifest"]
        attempts = []
        for attempt in document.get("attempts", []):
            result = attempt.get("result") or {}
            observation = attempt.get("observation") or {}
            attempts.append(
                {
                    **{
                        key: attempt.get(key)
                        for key in (
                            "id",
                            "number",
                            "scheduler",
                            "pipeline",
                            "evaluation",
                            "artifacts",
                            "created_at",
                            "error",
                        )
                    },
                    "job_id": observation.get("job_id"),
                    "exit_code": result.get("exit_code", observation.get("exit_code")),
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                    "metrics": numeric_metrics(result.get("metrics", {})),
                    "issues": result.get("issues", []),
                }
            )
        observed_age = age_seconds(document.get("updated_at"))
        return self.redactor.value(
            {
                "id": document["id"],
                "project_id": project_id(manifest),
                "experiment_id": manifest["experiment_id"],
                "adapter": manifest["adapter"],
                "profile": manifest["profile"],
                "backend": document.get("profile", {}).get("backend", "unknown"),
                "status": document["status"],
                "created_at": document.get("created_at"),
                "updated_at": document.get("updated_at"),
                "observation_stale": document["status"] not in FINAL_RUNS
                and (observed_age is None or observed_age > 120),
                "source_revision": manifest.get("source", {}).get("revision"),
                "parameters": manifest.get("payload", {}),
                "hypothesis": manifest.get("hypothesis"),
                "attempts": attempts,
            }
        )

    def _campaign(self, document):
        return self.redactor.value(
            {
                **{
                    key: document.get(key)
                    for key in (
                        "id",
                        "objective",
                        "rubric",
                        "phase",
                        "error",
                        "created_at",
                        "updated_at",
                        "max_runs",
                        "baseline_run",
                        "runs",
                        "iterations",
                    )
                },
                "project_id": project_id(document["baseline"]),
                "adapter": document["baseline"]["adapter"],
            }
        )

    def _session(self, document, detail=False):
        result = dict(document)
        age = age_seconds(result.get("heartbeat_at"))
        result["display_status"] = (
            "unobserved"
            if result["status"] == "running" and (age is None or age > 20)
            else result["status"]
        )
        if not detail:
            result.pop("prompt", None)
            result.pop("response", None)
        return self.redactor.value(result)

    def snapshot(self):
        runs, campaigns, sessions, samples, notices = [], [], [], [], []
        ledger_available = False
        history_limited = False
        if (self.root / "ledger.sqlite3").exists():
            try:
                with read_database(self.root / "ledger.sqlite3") as connection:
                    # Keep active work visible even when it started before recent history.
                    rows = connection.execute(
                        "SELECT document FROM runs ORDER BY "
                        "json_extract(document, '$.status') IN ('succeeded','failed','cancelled'), "
                        "rowid DESC LIMIT ?",
                        (HISTORY_LIMIT,),
                    )
                    runs = [self._run(json.loads(row[0])) for row in rows]
                    rows = connection.execute(
                        "SELECT document FROM campaigns ORDER BY "
                        "json_extract(document, '$.phase') = 'done', rowid DESC LIMIT ?",
                        (HISTORY_LIMIT,),
                    )
                    campaigns = [self._campaign(json.loads(row[0])) for row in rows]
                    history_limited = any(
                        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[
                            0
                        ]
                        > HISTORY_LIMIT
                        for table in ("runs", "campaigns")
                    )
                    ledger_available = True
            except (sqlite3.Error, ValueError, KeyError, TypeError):
                notices.append(
                    "The experiment ledger is temporarily unavailable. Showing no inferred job state."
                )
        else:
            notices.append(
                "No experiment ledger yet. Campaigns will appear after the orchestrator records work."
            )
        if (self.root / DATABASE).exists():
            try:
                with read_database(self.root / DATABASE) as connection:
                    rows = connection.execute(
                        "SELECT document FROM sessions ORDER BY "
                        "json_extract(document, '$.status') != 'running', started_at DESC LIMIT ?",
                        (HISTORY_LIMIT,),
                    )
                    sessions = [self._session(json.loads(row[0])) for row in rows]
                    history_limited |= (
                        connection.execute("SELECT count(*) FROM sessions").fetchone()[
                            0
                        ]
                        > HISTORY_LIMIT
                    )
                    samples = [
                        json.loads(row[0])
                        for row in connection.execute(
                            "SELECT document FROM host_samples ORDER BY sequence DESC LIMIT 240"
                        )
                    ][::-1]
            except (sqlite3.Error, ValueError, KeyError, TypeError):
                notices.append("Agent telemetry is temporarily unavailable.")
        if history_limited:
            notices.append(
                "Showing up to 500 records per category, with active work first. Summary counts cover these records."
            )
        projects = sorted(
            {item["project_id"] for item in [*runs, *campaigns, *sessions]}
        )
        # Host measurements are local to this service, never attributed to remote GPUs.
        return {
            "runs": runs,
            "campaigns": campaigns,
            "sessions": sessions,
            "projects": projects,
            "host_samples": samples,
            "notices": notices,
            "ledger_available": ledger_available,
            "history_limited": history_limited,
        }

    def run(self, identifier):
        with read_database(self.root / "ledger.sqlite3") as connection:
            row = connection.execute(
                "SELECT document FROM runs WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return self._run(json.loads(row[0]))

    def session(self, identifier):
        with read_database(self.root / DATABASE) as connection:
            row = connection.execute(
                "SELECT document FROM sessions WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return self._session(json.loads(row[0]), detail=True)

    def events(self, identifier, after=None, limit=200):
        self.session(identifier)
        with read_database(self.root / DATABASE) as connection:
            first = connection.execute(
                "SELECT min(sequence) FROM session_events WHERE session_id=?",
                (identifier,),
            ).fetchone()[0]
            if after is None:
                rows = list(
                    connection.execute(
                        "SELECT sequence,time,kind,payload FROM session_events WHERE session_id=? "
                        "ORDER BY sequence DESC LIMIT ?",
                        (identifier, limit),
                    )
                )[::-1]
            else:
                rows = list(
                    connection.execute(
                        "SELECT sequence,time,kind,payload FROM session_events WHERE session_id=? AND sequence>? "
                        "ORDER BY sequence LIMIT ?",
                        (identifier, after, limit),
                    )
                )
        items = [
            {
                "sequence": row[0],
                "time": row[1],
                "kind": row[2],
                "payload": json.loads(row[3]),
            }
            for row in rows
        ]
        return self.redactor.value(
            {
                "events": items,
                "cursor": items[-1]["sequence"] if items else (after or 0),
                "earlier_events": bool(items and first < items[0]["sequence"]),
            }
        )

    def run_events(self, identifier):
        self.run(identifier)
        with read_database(self.root / "ledger.sqlite3") as connection:
            rows = list(
                connection.execute(
                    "SELECT sequence,time,event,detail FROM events WHERE run_id=? ORDER BY sequence DESC LIMIT 200",
                    (identifier,),
                )
            )[::-1]
        return self.redactor.value(
            [
                {
                    "sequence": row[0],
                    "time": row[1],
                    "kind": row[2],
                    "payload": json.loads(row[3]),
                }
                for row in rows
            ]
        )

    def artifacts(self, run_id, attempt_id):
        run = self.run(run_id)
        if attempt_id not in {attempt["id"] for attempt in run["attempts"]}:
            raise KeyError(attempt_id)
        # Only canonical paths derived from recorded IDs are accepted.
        relative = f"runs/{run_id}/{attempt_id}/artifact-index.json"
        try:
            data, size = safe_file(self.root, relative, 4 * 1024 * 1024)
        except FileNotFoundError:
            return []
        if size > len(data):
            raise ValueError("Artifact index exceeds the preview limit")
        entries = json.loads(data)
        return [
            {key: item.get(key) for key in ("path", "bytes", "sha256")}
            for item in entries
        ]

    def preview(self, run_id, attempt_id, path):
        entries = self.artifacts(run_id, attempt_id)
        if path not in {entry["path"] for entry in entries}:
            raise KeyError(path)
        if PurePosixPath(path).suffix.lower() not in {
            ".json",
            ".jsonl",
            ".csv",
            ".txt",
            ".log",
            ".md",
            ".yaml",
        }:
            raise ValueError(
                "Preview is available for text, JSON, CSV, and log artifacts"
            )
        data, size = safe_file(
            self.root, f"runs/{run_id}/{attempt_id}/artifacts/{path}"
        )
        return {
            "path": path,
            "bytes": size,
            "truncated": size > len(data),
            "content": self.redactor.text(data.decode("utf-8", errors="replace")),
        }
