"""Operator-owned configuration. Agents can select profiles, never define them."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml

from .common import ExperimentError, identifier
from .ssh import SSHSettings


@dataclass
class Config:
    path: Path
    state_dir: Path
    profiles: dict
    repositories: dict
    workloads: dict
    max_attempts: int = 2
    max_active_runs: int = 4
    artifact_limit_bytes: int = 1024**3

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = path.resolve()
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ExperimentError("configuration requires version: 1")
        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict) or not profiles:
            raise ExperimentError("configure at least one execution profile")
        for name, profile in profiles.items():
            identifier(name, "profile")
            if profile.get("backend") not in {"local-process", "docker", "slurm-ssh"}:
                raise ExperimentError(f"{name}: unsupported backend")
            if profile["backend"] == "local-process" and not profile.get("trusted_local"):
                raise ExperimentError(f"{name}: local-process requires trusted_local: true; it is not a sandbox")
            if profile["backend"] == "docker" and not profile.get("image"):
                raise ExperimentError(f"{name}: Docker image is required")
            if profile["backend"] == "slurm-ssh":
                SSHSettings.from_profile(profile)
                if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]*", profile.get("host", "")):
                    raise ExperimentError(f"{name}: invalid SSH host or alias")
                if not str(profile.get("root", "")).startswith("/"):
                    raise ExperimentError(f"{name}: remote root must be absolute")
                allowed = {"account", "partition", "qos", "cpus-per-task", "mem", "time", "gres", "constraint"}
                if set(profile.get("slurm", {})) - allowed:
                    raise ExperimentError(f"{name}: unsupported Slurm resource option")
            if type(profile.get("timeout_seconds", 86400)) is not int or profile.get("timeout_seconds", 86400) < 1:
                raise ExperimentError(f"{name}: timeout_seconds must be positive")
        policy = data.get("policy", {})
        limits = [policy.get("max_attempts", 2), policy.get("max_active_runs", 4), policy.get("artifact_limit_bytes", 1024**3)]
        if any(type(v) is not int or v < 1 for v in limits):
            raise ExperimentError("policy limits must be positive integers")
        repositories = data.get("repositories", {})
        repositories = {name: str((path.parent / location).resolve()) for name, location in repositories.items()}
        return cls(path, (path.parent / data.get("state_dir", ".experiments")).resolve(), profiles,
                   repositories, data.get("workloads", {}), *limits)
