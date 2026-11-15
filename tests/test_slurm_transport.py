"""Exercise real SSH serialization, zipapp staging and collection with fake executables.

No SSH server, GPU or scheduler is required; the worker itself is a real process.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from experiment_orchestrator.broker import Broker
from experiment_orchestrator.config import Config
from experiment_orchestrator.common import read_json
from test_experiments import manifest


FAKE = r'''import json, os, pathlib, shlex, subprocess, sys
tool = pathlib.Path(sys.argv[0]).name
registry = pathlib.Path(os.environ['FAKE_REGISTRY'])
args = sys.argv[1:]
if tool == 'ssh':
    result = subprocess.run(['/bin/sh', '-c', args[-1]], stdin=sys.stdin.buffer, capture_output=True)
    lost = registry.with_suffix('.lost')
    parsed = shlex.split(args[-1])
    if 'submit' in parsed and any(a.endswith('worker.pyz') for a in parsed) and not lost.exists():
        lost.touch()
        sys.stderr.write('simulated lost SSH response after remote acceptance')
        sys.exit(255)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    sys.exit(result.returncode)
if tool == 'sbatch':
    root = pathlib.Path(args[-1]).parent
    previous = json.loads(registry.read_text()) if registry.exists() else {'count': 0}
    name = args[args.index('--job-name') + 1]
    registry.write_text(json.dumps({'root': str(root), 'name': name, 'count': previous['count'] + 1}))
    with (root / 'slurm-123.log').open('w') as log:
        subprocess.Popen(['/bin/sh', args[-1]], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    print('123')
elif registry.exists():
    job = json.loads(registry.read_text())
    root = pathlib.Path(job['root'])
    if tool == 'scancel':
        (root / 'cancel.request').touch()
    elif '--format=%i|%j' in args or '--format=JobIDRaw,JobName%100' in args:
        print('123|' + job['name'])
    elif tool == 'squeue':
        if not (root / 'worker.finished.json').exists(): print('123|RUNNING')
    elif tool == 'sacct' and (root / 'worker.finished.json').exists():
        state = json.loads((root / 'worker.finished.json').read_text())['state'].upper()
        print('123|' + state + '|0:0')
'''


class SlurmTransportTest(unittest.TestCase):
    def test_lost_ssh_reply_restart_and_collection_submit_only_once(self):
        with tempfile.TemporaryDirectory(prefix="slurm fixture ") as raw:
            root = Path(raw)
            binaries = root / "bin"
            binaries.mkdir()
            for tool in ("ssh", "sbatch", "squeue", "sacct", "scancel"):
                path = binaries / tool
                path.write_text(f"#!{sys.executable}\n" + FAKE)
                path.chmod(0o755)
            config = Config(root / "config.yaml", root / "state", {"cpu": {
                "backend": "slurm-ssh", "host": "fake-host", "root": str(root / "remote jobs"),
                "python": sys.executable, "control_python": sys.executable, "slurm": {"time": "00:01:00"},
                "ssh": {"control_directory": str(root / "ssh")},
            }}, {}, {})
            env = {"PATH": str(binaries) + os.pathsep + os.environ["PATH"], "FAKE_REGISTRY": str(root / "registry.json")}
            with patch.dict(os.environ, env):
                broker = Broker(config)
                run = broker.submit_experiment(manifest(), "drop-response")
                self.assertEqual(run["status"], "unknown", run)
                broker = Broker(config)
                for _ in range(20):
                    run = broker.get_run(run["id"], refresh=True)
                    if run["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(run["status"], "succeeded", run)
                self.assertEqual(read_json(root / "registry.json")["count"], 1)
                self.assertEqual(run["attempts"][-1]["observation"]["job_id"], "123")
                index = broker.get_artifacts(run["id"])
                self.assertTrue((Path(index["root"]) / "output/unique.json").exists())
                self.assertEqual(broker.submit_experiment(manifest(), "drop-response")["id"], run["id"])
