"""Example standalone workload: only the environment/result contract is required."""
import json
import os
from pathlib import Path

parameters = json.loads(Path(os.environ["EXPERIMENT_INPUT"]).read_text())
values = parameters.get("values", [1, 2, 3])
Path(os.environ["EXPERIMENT_RESULT"]).write_text(json.dumps({
    "pipeline": "succeeded", "evaluation": "not_requested", "metrics": {"sum": sum(values)},
}))
