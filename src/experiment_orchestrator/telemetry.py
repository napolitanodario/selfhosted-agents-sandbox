"""Bounded, best-effort telemetry for campaign agents.

This module uses only the standard library. A separate database and writer thread
keep dashboard traffic and slow disks out of the broker's execution path.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid

from .common import now


LOGGER = logging.getLogger(__name__)
DATABASE = "monitoring.sqlite3"
MAX_EVENTS = 10_000
MAX_LOG_BYTES = 2 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_campaign ON sessions(campaign_id, started_at);
CREATE TABLE IF NOT EXISTS session_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    time TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON session_events(session_id, sequence);
CREATE INDEX IF NOT EXISTS events_time ON session_events(time);
CREATE TABLE IF NOT EXISTS host_samples (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS host_time ON host_samples(time);
"""


class Redactor:
    """Remove known runtime secrets and common credential representations."""

    def __init__(self, environ: Mapping[str, str] | None = None):
        environ = os.environ if environ is None else environ
        self.secrets = sorted(
            {
                value
                for key, value in environ.items()
                if re.search(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", key, re.I)
                and isinstance(value, str)
                and len(value) >= 6
            },
            key=len,
            reverse=True,
        )

    def text(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        text = re.sub(
            r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[redacted private key]",
            text,
        )
        text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [redacted]", text)
        text = re.sub(
            r"""(?i)((?:[\w-]*(?:api[_-]?key|token|password|secret)[\w-]*)["']?\s*[:=]\s*["']?)[^\s,"'\}\]]+""",
            r"\1[redacted]",
            text,
        )
        # Strip terminal escape sequences before they reach web views or exports.
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)

    def value(self, value, depth: int = 0):
        if depth > 20:
            return "[depth limit]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:1000]:
                sensitive = re.search(
                    r"(?i)(?:^|_)(?:api_key|access_token|refresh_token|password|secret|authorization)$",
                    str(key),
                )
                result[str(key)] = (
                    "[redacted]" if sensitive else self.value(item, depth + 1)
                )
            return result
        if isinstance(value, (list, tuple)):
            return [self.value(item, depth + 1) for item in value[:1000]]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(path, timeout=1)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
    path.chmod(0o600)


class SessionRecorder:
    """Record only sessions explicitly started by a campaign coordinator.

    Queue and output limits protect experiments from verbose agents. The summary
    records truncation and dropped events instead of implying complete coverage.
    """

    def __init__(self, root: Path, metadata: dict, environ=None):
        self.path = root / DATABASE
        self.redactor = Redactor(environ)
        timestamp = now()
        self.document = {
            **metadata,
            "id": uuid.uuid4().hex,
            "status": "running",
            "phase": "starting",
            "started_at": timestamp,
            "finished_at": None,
            "heartbeat_at": timestamp,
            "last_activity_at": timestamp,
            "command_count": 0,
            "command_errors": 0,
            "output_bytes": 0,
            "events_truncated": False,
            "dropped_events": 0,
        }
        self.pending = queue.Queue(maxsize=512)
        self.stopping = threading.Event()
        self.final: dict = {
            "status": "failed",
            "error": "Session ended without a result",
        }
        self.dropped = 0
        self.failed = False
        self.thread = threading.Thread(
            target=self._write, daemon=True, name="agent-telemetry"
        )

    def __enter__(self):
        try:
            self.thread.start()
            self.emit("session.started", {})
        except RuntimeError:
            self.failed = True
            LOGGER.warning(
                "Agent telemetry could not start; campaign execution continues"
            )
        return self

    def emit(self, kind: str, payload: dict) -> None:
        if self.failed or self.stopping.is_set():
            return
        try:
            # Bound chunks before queueing so even a single giant write is bounded.
            if kind == "log" and len(payload.get("text", "")) > 65_536:
                payload = {**payload, "text": "[oversized output chunk omitted]\n"}
                self.dropped += 1
            self.pending.put_nowait((now(), kind, payload))
        except queue.Full:
            self.dropped += 1

    def finish(self, *, response: dict) -> None:
        self.final = {"status": "succeeded", "response": response}

    def __exit__(self, kind, error, traceback):
        if error is not None:
            self.final = {
                "status": "failed" if isinstance(error, Exception) else "interrupted",
                "error": str(error) or type(error).__name__,
            }
        self.stopping.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)

    def _write(self) -> None:
        try:
            initialize_database(self.path)
            with (
                closing(sqlite3.connect(self.path, timeout=0.5)) as connection,
                connection,
            ):
                self._consume(connection)
        except Exception:
            self.failed = True
            # Details might contain file paths or credentials from a failed write.
            LOGGER.warning(
                "Agent telemetry is unavailable; campaign execution continues"
            )

    def _consume(self, connection: sqlite3.Connection) -> None:
        buffers = {"stdout": "", "stderr": ""}
        private_keys = {"stdout": False, "stderr": False}
        event_count = 0
        last_commit = 0.0

        def persist(timestamp, kind, payload):
            nonlocal event_count
            if event_count >= MAX_EVENTS and kind != "session.finished":
                self.document["events_truncated"] = True
                return
            connection.execute(
                "INSERT INTO session_events(session_id,time,kind,payload) VALUES(?,?,?,?)",
                (
                    self.document["id"],
                    timestamp,
                    kind,
                    json.dumps(self.redactor.value(payload)),
                ),
            )
            event_count += 1

        def output(timestamp, stream, content):
            # Key blocks can span many complete lines as well as SDK chunks.
            # Omit each block before persisting any of its contents.
            begins_key = re.search(r"-----BEGIN [^-]*PRIVATE KEY-----", content)
            ends_key = re.search(r"-----END [^-]*PRIVATE KEY-----", content)
            if private_keys[stream] or begins_key:
                private_keys[stream] = not bool(ends_key)
                if not begins_key:
                    return
                content = "[redacted private key]\n"
            safe = self.redactor.text(content)
            size = len(safe.encode("utf-8"))
            if self.document["output_bytes"] + size > MAX_LOG_BYTES:
                self.document["events_truncated"] = True
                return
            self.document["output_bytes"] += size
            persist(timestamp, "log", {"stream": stream, "text": safe[:16_384]})
            if len(safe) > 16_384:
                self.document["events_truncated"] = True

        while True:
            events = []
            try:
                events.append(self.pending.get(timeout=0.2))
                while len(events) < 128:
                    events.append(self.pending.get_nowait())
            except queue.Empty:
                pass
            for timestamp, kind, payload in events:
                self.document["last_activity_at"] = timestamp
                if kind == "log":
                    stream = "stderr" if payload.get("stream") == "stderr" else "stdout"
                    buffers[stream] += payload.get("text", "")
                    lines = buffers[stream].splitlines(keepends=True)
                    buffers[stream] = ""
                    for line in lines:
                        if line.endswith(("\n", "\r")):
                            output(timestamp, stream, line)
                        else:
                            buffers[stream] = line
                    # Never emit a partial credential split across two chunks.
                    if len(buffers[stream]) > 65_536:
                        buffers[stream] = ""
                        self.document["events_truncated"] = True
                else:
                    if kind == "sandbox.created":
                        self.document["sandbox_id"] = payload["sandbox_id"]
                    if kind == "command.started":
                        self.document["command_count"] += 1
                        self.document["phase"] = payload.get("label", "command")
                    if kind == "command.finished" and payload.get("exit_code") != 0:
                        self.document["command_errors"] += 1
                    persist(timestamp, kind, payload)
            finished = self.stopping.is_set() and self.pending.empty()
            if finished:
                for stream, content in buffers.items():
                    if content:
                        output(now(), stream, content)
                self.document.update(
                    self.redactor.value(self.final), finished_at=now(), phase="finished"
                )
                persist(now(), "session.finished", {"status": self.document["status"]})
            if events or finished or time.monotonic() - last_commit >= 5:
                self.document.update(heartbeat_at=now(), dropped_events=self.dropped)
                connection.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                    (
                        self.document["id"],
                        self.document["campaign_id"],
                        self.document["started_at"],
                        json.dumps(self.redactor.value(self.document)),
                    ),
                )
                connection.commit()
                last_commit = time.monotonic()
            if finished:
                break
