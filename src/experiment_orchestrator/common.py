"""Standard-library primitives shared with the remote worker."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ExperimentError(Exception):
    """An actionable configuration or experiment error."""


TERMINAL = {"completed", "failed", "cancelled"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(value: object, label: str = "id") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", value):
        raise ExperimentError(f"{label} must contain 1–100 letters, digits, dots, underscores or hyphens")
    return value


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    # Reject NaN/Infinity (including numeric overflow), which JSON does not support.
    json.dumps(document, allow_nan=False)
    return document


def write_json(path: Path, value: object) -> None:
    """Atomic, durable replacement; readers never see a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def extract_archive(archive: Path, destination: Path, max_bytes: int = 1024**3) -> None:
    """Reject traversal, links and special files before writing any archive content."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if sum(m.size for m in members) > max_bytes or len(members) > 100000:
            raise ExperimentError("archive exceeds collection limits")
        for member in members:
            target = (root / member.name).resolve()
            if not target.is_relative_to(root) or not (member.isdir() or member.isfile()):
                raise ExperimentError(f"unsafe archive entry: {member.name}")
        tar.extractall(root, members=members, filter="data")
