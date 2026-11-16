#!/usr/bin/env python3
"""Install a Git snapshot in its own venv, then atomically select it as current.

Runs on the Linux deployment host. Uses Python's standard library, Git, pip,
and Node.js to build the dashboard; it does not start experiment jobs.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid


class DeploymentError(RuntimeError):
    pass


def run(*argv, cwd=None, **kwargs):
    # Child build/check commands do not need the repository API credential.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GH_TOKEN", "GITHUB_TOKEN", "PYTHONPATH", "PYTHONHOME"}
    }
    return subprocess.run(
        [str(arg) for arg in argv], cwd=cwd, env=env, check=True, **kwargs
    )


def main_revision():
    """Fail closed if GitHub cannot confirm the latest main revision."""
    repository = os.environ["GITHUB_REPOSITORY"]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(
        f"{api}/repos/{repository}/git/ref/heads/main",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "experiment-orchestrator-deploy",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["object"]["sha"]


def snapshot(source, revision, release):
    with tempfile.TemporaryFile() as archive:
        run("git", "-C", source, "archive", "--format=tar", revision, stdout=archive)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as bundle:
            bundle.extractall(release, filter="data")


def install_release(release, python):
    # Build before pip packages the static assets. Node is a build dependency;
    # the deployed web service itself only runs Python.
    dashboard = release / "dashboard"
    with_dashboard = (dashboard / "package.json").is_file()
    if with_dashboard:
        if not shutil.which("npm"):
            raise DeploymentError(
                "Node.js 24 and npm are required to build the dashboard"
            )
        run("npm", "ci", "--include=dev", cwd=dashboard)
        run("npm", "run", "build", cwd=dashboard)
    # Create at the final path: moving a venv afterwards breaks script shebangs.
    run(python, "-m", "venv", release / ".venv")
    runtime = release / ".venv" / "bin"
    package = ".[dashboard]" if with_dashboard else "."
    run(
        runtime / "python",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        package,
        cwd=release,
    )
    run(runtime / "python", "-m", "pip", "check", cwd=release)
    validate_bundle(release, runtime)
    if with_dashboard:
        run(
            runtime / "experiment-dashboard",
            "--help",
            cwd=release,
            stdout=subprocess.DEVNULL,
        )
        run(
            runtime / "python",
            "-c",
            "from importlib.resources import files; "
            "assert files('experiment_dashboard').joinpath('static/index.html').is_file(), "
            "'Dashboard assets missing from installed package'",
            cwd=release,
        )


def validate_bundle(release, runtime):
    run(runtime / "experimentctl", "--help", cwd=release, stdout=subprocess.DEVNULL)
    run(runtime / "agent-harness", "validate", cwd=release)
    # The CLI's validate command constructs a Broker and creates a ledger. Use
    # the validator directly so the release stays free of disposable state.
    run(
        runtime / "python",
        "-c",
        "from pathlib import Path; import yaml; "
        "from experiment_orchestrator.config import Config; "
        "from experiment_orchestrator.adapters import validate_manifest; "
        "validate_manifest(yaml.safe_load(Path('examples/experiments/cpu-exact.yaml').read_text()), "
        "Config.load(Path('orchestrator.yaml')))",
        cwd=release,
    )


def connect_shared(release, shared):
    """Initialize once; subsequent releases only link operator-owned files."""
    for name, template in (
        (".env", ".env.example"),
        ("orchestrator.yaml", "orchestrator.yaml"),
    ):
        destination = shared / name
        if not destination.exists():
            # Exclusive creation preserves an existing file, including an empty one.
            with destination.open("x") as output:
                output.write((release / template).read_text())
        link = release / name
        if link.exists():
            link.unlink()
        link.symlink_to(destination)
    for name in (".experiments", "runs", "secrets"):
        (shared / name).mkdir(exist_ok=True)
        (release / name).symlink_to(shared / name, target_is_directory=True)


def check_config(release):
    # Config.load resolves the symlink, so relative paths are anchored in shared/.
    # Do not open the production SQLite ledger or submit work during deployment.
    run(
        release / ".venv" / "bin" / "python",
        "-c",
        "from pathlib import Path; from experiment_orchestrator.config import Config; "
        "Config.load(Path('orchestrator.yaml'))",
        cwd=release,
    )


def replace_link(link, target):
    temporary = link.parent / f".{link.name}-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def deploy(source, root, python, expected_revision=None, check_main=False):
    source = Path(source).resolve()
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise DeploymentError("deployment root must be an absolute path")
    root = root.resolve()
    if source == root or source in root.parents or root in source.parents:
        raise DeploymentError(
            "deployment root and checkout must be separate, non-overlapping directories"
        )
    if not hasattr(tarfile, "data_filter"):
        raise DeploymentError(
            "install a current Python 3.11+ patch release with tarfile extraction filters"
        )
    revision = run(
        "git", "-C", source, "rev-parse", "HEAD", capture_output=True, text=True
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise DeploymentError("invalid Git revision")
    if expected_revision and revision != expected_revision:
        raise DeploymentError("checkout differs from the tested revision")

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ("current", "previous"):
            link = root / name
            if link.exists() and not link.is_symlink():
                raise DeploymentError(
                    f"{link} must be a symlink, not an existing file or directory"
                )
        if check_main and main_revision() != revision:
            print("Skipping superseded commit: main has advanced.")
            return None

        releases, shared = root / "releases", root / "shared"
        releases.mkdir(exist_ok=True)
        shared.mkdir(exist_ok=True, mode=0o700)
        release = releases / f"{revision[:12]}-{uuid.uuid4().hex[:12]}"
        release.mkdir()
        activated = False
        try:
            snapshot(source, revision, release)
            install_release(release, python)
            connect_shared(release, shared)
            check_config(release)
            (release / "DEPLOYMENT.json").write_text(
                json.dumps(
                    {
                        "revision": revision,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                )
                + "\n"
            )
            if check_main and main_revision() != revision:
                print("Skipping activation: main advanced during installation.")
                return None
            current = root / "current"
            if current.is_symlink():
                replace_link(root / "previous", current.resolve(strict=True))
            replace_link(current, release)
            activated = True
        finally:
            if not activated:
                shutil.rmtree(release)
        print(f"Activated {revision} at {release}")
        return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--expected-revision")
    parser.add_argument(
        "--check-main",
        action="store_true",
        help="skip commits superseded on GitHub main",
    )
    args = parser.parse_args()
    os.umask(0o077)
    try:
        release = deploy(
            args.source, args.root, args.python, args.expected_revision, args.check_main
        )
    except (
        DeploymentError,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
        tarfile.TarError,
    ) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        return 1
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(
                f"Activated release: `{release}`\n"
                if release
                else "Skipped a superseded main commit.\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
