"""Low-frequency measurements of the Ubuntu agent host, not the GPU cluster."""

from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3

import psutil

from experiment_orchestrator.common import now
from experiment_orchestrator.telemetry import DATABASE, initialize_database


class HostSampler:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / DATABASE
        initialize_database(self.path)
        self.previous_cpu = psutil.cpu_times()

    def sample(self):
        # Keep counters on this instance: asyncio's worker pool may use a
        # different thread each time, while cpu_percent's baseline is per thread.
        current_cpu = psutil.cpu_times()
        changes = {
            name: max(0, getattr(current_cpu, name) - getattr(self.previous_cpu, name))
            for name in current_cpu._fields
        }
        self.previous_cpu = current_cpu
        total = (
            sum(changes.values())
            - changes.get("guest", 0)
            - changes.get("guest_nice", 0)
        )
        idle = changes.get("idle", 0) + changes.get("iowait", 0)
        cpu_percent = max(0, min(100, 100 * (total - idle) / total)) if total else 0
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(self.root)
        document = {
            "time": now(),
            "hostname": socket.gethostname(),
            "cpu_percent": cpu_percent,
            "cpu_count": psutil.cpu_count(),
            "ram_used_bytes": memory.used,
            "ram_total_bytes": memory.total,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "dashboard_rss_bytes": psutil.Process().memory_info().rss,
        }
        with closing(sqlite3.connect(self.path, timeout=0.5)) as connection, connection:
            connection.execute(
                "INSERT INTO host_samples(time,document) VALUES(?,?)",
                (document["time"], json.dumps(document)),
            )
            # Summaries persist. Raw host samples and agent logs have bounded retention.
            connection.execute(
                "DELETE FROM host_samples WHERE time < strftime('%Y-%m-%dT%H:%M:%f', 'now', '-1 day')"
            )
            connection.execute(
                "DELETE FROM session_events WHERE time < strftime('%Y-%m-%dT%H:%M:%f', 'now', '-14 days')"
            )
