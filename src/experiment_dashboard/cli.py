"""Start the dashboard on the Ubuntu machine running the coordinator."""

import argparse
import os
from pathlib import Path

from experiment_orchestrator.config import Config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("orchestrator.yaml"))
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8787"))
    )
    parser.add_argument(
        "--allowed-hosts",
        default=os.environ.get("DASHBOARD_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"),
    )
    parser.add_argument(
        "--secure-cookie",
        action="store_true",
        default=os.environ.get("DASHBOARD_SECURE_COOKIE") == "true",
    )
    parser.add_argument(
        "--local-no-auth",
        action="store_true",
        help="development only; requires a loopback bind",
    )
    args = parser.parse_args(argv)
    if args.local_no_auth and args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("--local-no-auth requires a loopback bind")
    try:
        from .api import create_app
        import uvicorn
    except ImportError:
        parser.error("Install dashboard dependencies with pip install '.[dashboard]'")
    try:
        config = Config.load(args.config)
        application = create_app(
            config.state_dir,
            token=os.environ.get("DASHBOARD_ACCESS_TOKEN"),
            local_no_auth=args.local_no_auth,
            allowed_hosts=tuple(
                value.strip()
                for value in args.allowed_hosts.split(",")
                if value.strip()
            ),
            secure_cookie=args.secure_cookie,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    # The default loopback origin works behind an HTTPS reverse proxy or tunnel.
    # Forwarded headers are deliberately ignored; no proxy header grants access.
    uvicorn.run(application, host=args.host, port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
