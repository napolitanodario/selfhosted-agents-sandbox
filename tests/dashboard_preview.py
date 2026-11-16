"""Serve a disposable synthetic workspace for browser tests or local review."""

import argparse
from pathlib import Path
import tempfile

import uvicorn

from dashboard_fixture import seed
from experiment_dashboard.api import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="observatory-test-") as directory:
        root = Path(directory)
        seed(root)
        print(
            "Synthetic test workspace. No agents or GPU jobs are launched.", flush=True
        )
        app = create_app(
            root, token="test-dashboard-access-token-only", collect_host=False
        )
        uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="warning")


if __name__ == "__main__":
    main()
