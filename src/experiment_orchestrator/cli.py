"""CLI tool surface for humans, automation and local agents via a trusted broker."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

import yaml

from agent_harness.models import HarnessError
from agent_harness.session import load_project_env
from .adapters import validate_manifest
from .broker import Broker
from .common import ExperimentError
from .config import Config
from .coordinator import Coordinator


def parser():
    p = argparse.ArgumentParser(description="Durable, workload-independent experiment orchestration")
    p.add_argument("--config", type=Path, default=Path("orchestrator.yaml"))
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("validate", "submit"):
        sub = commands.add_parser(name)
        sub.add_argument("manifest", type=Path)
        if name == "submit":
            sub.add_argument("--request-id", required=True)
    commands.add_parser("list")
    commands.add_parser("reconcile", help="refresh all nonfinal runs; safe after restart")
    resolution = commands.add_parser("resolve", help="operator recovery ONLY after verifying no original job remains active")
    resolution.add_argument("run_id")
    resolution.add_argument("--state", required=True, choices=["failed", "cancelled"])
    resolution.add_argument("--reason", required=True, help="evidence establishing the original job is no longer active")
    for name in ("status", "cancel", "retry", "artifacts", "events", "watch"):
        sub = commands.add_parser(name)
        sub.add_argument("run_id")
        if name == "status":
            sub.add_argument("--refresh", action="store_true")
        if name == "watch":
            sub.add_argument("--poll-seconds", type=float, default=30,
                             help="seconds between status checks; Slurm checks reuse a persistent SSH connection")
    campaign = commands.add_parser("campaign").add_subparsers(dest="action", required=True)
    campaign.add_parser("create").add_argument("definition", type=Path)
    for name in ("status", "step", "run", "resume"):
        sub = campaign.add_parser(name)
        sub.add_argument("campaign_id")
        if name == "run":
            sub.add_argument("--poll-seconds", type=float, default=30,
                             help="seconds between status checks; Slurm checks reuse a persistent SSH connection")
    return p


def emit(document):
    print(json.dumps(document, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    load_project_env()
    try:
        config = Config.load(args.config)
        broker = Broker(config)
        if args.command in {"validate", "submit"}:
            manifest = yaml.safe_load(args.manifest.read_text())
            emit(validate_manifest(manifest, config) if args.command == "validate" else broker.submit_experiment(manifest, args.request_id))
        elif args.command == "list":
            emit(broker.store.list_runs())
        elif args.command == "reconcile":
            emit([broker.get_run(r["id"], refresh=True) for r in broker.store.list_runs()
                  if r["status"] not in {"succeeded", "failed", "cancelled"} or r["attempts"][-1]["artifacts"] != "ready"])
        elif args.command == "resolve":
            emit(broker.resolve_unknown(args.run_id, args.state, args.reason))
        elif args.command == "campaign":
            coordinator = Coordinator(broker)
            if args.action == "create":
                emit(coordinator.create(args.definition.resolve()))
            elif args.action == "status":
                emit(broker.store.campaign(args.campaign_id))
            elif args.action == "resume":
                emit(coordinator.resume(args.campaign_id))
            else:
                while True:
                    document = asyncio.run(coordinator.step(args.campaign_id))
                    emit(document)
                    if args.action == "step" or document["phase"] in {"done", "needs_attention"}:
                        return 1 if document["phase"] == "needs_attention" else 0
                    if document["phase"] in {"wait", "wait_baseline"}:
                        time.sleep(max(1, args.poll_seconds))
        elif args.command == "watch":
            while True:
                run = broker.get_run(args.run_id, refresh=True)
                emit(run)
                if run["status"] in {"succeeded", "failed", "cancelled"}:
                    return 0 if run["status"] == "succeeded" else 1
                time.sleep(max(1, args.poll_seconds))
        else:
            operations = {"status": lambda: broker.get_run(args.run_id, refresh=args.refresh),
                          "cancel": lambda: broker.cancel_run(args.run_id), "retry": lambda: broker.retry(args.run_id),
                          "artifacts": lambda: broker.get_artifacts(args.run_id), "events": lambda: broker.store.events(args.run_id)}
            emit(operations[args.command]())
        return 0
    except (ExperimentError, HarnessError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped observing. Submitted jobs continue; use cancel to stop a job.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
