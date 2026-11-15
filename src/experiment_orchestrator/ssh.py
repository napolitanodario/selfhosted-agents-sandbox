"""Host-side SSH transport sharing an OpenSSH connection across broker requests.

OpenSSH owns the local master process and reconnects when its control socket is
no longer usable. Commands still have separate channels and deadlines. Failed
commands are not replayed here: the broker reconciles ambiguous submissions.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess

from .common import ExperimentError


@dataclass(frozen=True)
class SSHSettings:
    persist_seconds: int = 3600
    connect_timeout_seconds: int = 10
    keepalive_seconds: int = 60
    keepalive_count_max: int = 3
    command_timeout_seconds: int = 120
    transfer_timeout_seconds: int = 1800
    control_directory: str | None = None

    @classmethod
    def from_profile(cls, profile):
        raw = profile.get("ssh", {})
        if not isinstance(raw, dict) or set(raw) - {field.name for field in fields(cls)}:
            raise ExperimentError("ssh must be a mapping with supported connection settings")
        settings = cls(**raw)
        for field in fields(cls):
            value = getattr(settings, field.name)
            if field.name == "control_directory":
                if value is not None and (not isinstance(value, str) or not Path(value).is_absolute()
                                          or "%" in value or "\n" in value or "\x00" in value):
                    raise ExperimentError("ssh.control_directory must be an absolute path without percent signs or control characters")
            elif type(value) is not int or value < 1:
                raise ExperimentError(f"ssh.{field.name} must be a positive integer")
        return settings


class SSHTransport:
    def __init__(self, profile):
        self.host = profile["host"]
        self.settings = SSHSettings.from_profile(profile)

    def _control_path(self):
        # A short, user-private directory avoids AF_UNIX path limits in long WSL
        # checkout paths. Never modify permissions on an existing foreign path.
        directory = Path(self.settings.control_directory or f"/tmp/experiment-ssh-{os.getuid()}")
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ExperimentError("SSH control directory must be owned by this user, private (0700), and not a symlink")
        # Distinct connection policies get distinct masters; command/transfer
        # deadlines do not affect connection identity. %C is expanded by OpenSSH
        # from the effective destination, port, user, local host and jump host.
        policy = [self.settings.persist_seconds, self.settings.connect_timeout_seconds,
                  self.settings.keepalive_seconds, self.settings.keepalive_count_max]
        namespace = hashlib.sha256(json.dumps(policy).encode()).hexdigest()[:8]
        path = str(directory / f"{namespace}-%C.sock")
        if len(os.fsencode(path.replace("%C", "0" * 40))) > 90:
            raise ExperimentError("SSH control socket path is too long; use a shorter ssh.control_directory")
        return path

    def command(self, argv):
        settings = self.settings
        return ["ssh", "-T", "-S", self._control_path(),
                "-o", "BatchMode=yes", "-o", "ControlMaster=auto",
                "-o", f"ControlPersist={settings.persist_seconds}",
                "-o", f"ConnectTimeout={settings.connect_timeout_seconds}",
                "-o", "ConnectionAttempts=1",
                "-o", f"ServerAliveInterval={settings.keepalive_seconds}",
                "-o", f"ServerAliveCountMax={settings.keepalive_count_max}",
                self.host, shlex.join(argv)]

    def run(self, argv, *, data=None, output=None, transfer=False):
        timeout = self.settings.transfer_timeout_seconds if transfer else self.settings.command_timeout_seconds
        try:
            response = subprocess.run(self.command(argv), input=data,
                                      stdin=subprocess.DEVNULL if data is None else None,
                                      stdout=output if output is not None else subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ExperimentError(
                f"SSH request timed out after {timeout}s; its remote outcome may be unknown. "
                "Reconcile the existing run; do not create a replacement submission."
            ) from exc
        if response.returncode:
            detail = response.stderr.decode(errors="replace").strip()
            raise ExperimentError(detail or f"remote command failed (exit {response.returncode})")
        return response.stdout
