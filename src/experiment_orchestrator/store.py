"""SQLite ledger and cross-process locks; no agent session is needed to track jobs."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3

from .common import ExperimentError, now


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    fingerprint TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, time TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, document TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "ledger.sqlite3", timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self, name: str = "broker"):
        with (self.root / f"{name}.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ExperimentError(f"unknown run: {run_id}")
        return json.loads(row[0])

    def by_request(self, request_id: str):
        with self.connect() as db:
            row = db.execute("SELECT document FROM runs WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, run: dict, event: str, detail: object = None):
        run["updated_at"] = now()
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                       (run["id"], run["request_id"], run["fingerprint"], json.dumps(run)))
            db.execute("INSERT INTO events(run_id,time,event,detail) VALUES(?,?,?,?)",
                       (run["id"], now(), event, json.dumps(detail)))

    def list_runs(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT document FROM runs ORDER BY rowid DESC")]

    def events(self, run_id):
        with self.connect() as db:
            return [dict(sequence=r[0], time=r[1], event=r[2], detail=json.loads(r[3]))
                    for r in db.execute("SELECT sequence,time,event,detail FROM events WHERE run_id=? ORDER BY sequence", (run_id,))]

    def campaign(self, campaign_id):
        with self.connect() as db:
            row = db.execute("SELECT document FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            raise ExperimentError(f"unknown campaign: {campaign_id}")
        return json.loads(row[0])

    def save_campaign(self, document):
        with self.connect() as db:
            db.execute("INSERT INTO campaigns VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                       (document["id"], json.dumps(document)))
