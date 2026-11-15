"""Deterministic stand-in for the four research roles; no model or credentials."""
import json
from pathlib import Path

context = json.loads(Path("context.json").read_text())
role = Path(".harness/prompt.md").read_text().split("You are the ", 1)[1].split()[0]
if role == "proposer":
    payload = dict(context["baseline"]["manifest"]["payload"])
    payload["case_sensitive"] = False
    payload["expected_unique"] = len(set(text.casefold() for text in payload["texts"]))
    response = {"hypothesis": "Case folding merges case variants in the fixed cohort.", "payload": payload}
elif role == "reviewer":
    baseline = context["baseline_manifest"]
    candidate = context["candidate"]
    response = {"approved": baseline["payload"]["texts"] == candidate["payload"]["texts"], "reason": "Compare the same input cohort."}
elif role == "evaluator":
    baseline = context["baseline"]["outcome"]["metrics"]["unique_count"]
    candidate = context["candidate"]["outcome"]["metrics"]["unique_count"]
    response = {"summary": f"Unique count changed from {baseline} to {candidate}.", "metrics": {"delta_unique": candidate - baseline}}
elif role == "analyst":
    response = {"summary": "Case folding merges the case variants. This smoke test has completed its fixed comparison.", "next_action": "stop"}
else:
    raise ValueError(role)
Path("response.json").write_text(json.dumps(response))
