"""Audit sealed campaigns without overwriting or reclassifying their scores.

Archived native SAG scores are disclosed separately. Baseline transcripts and
post-agent builds cannot substitute for missing physical execution observations.
This script makes no model requests, starts no commands, and copies no scores
into the formal campaign.
"""

import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

from sag.benchmark.requirements import bound_file, evaluation_identity, load_json


def audit_run(directory):
    directory = Path(directory).resolve()
    checksums = load_json(directory / "checksums.json")
    sources = {}

    def read(path):
        path = Path(path)
        relative = str(path.relative_to(directory))
        expected = checksums.get(relative)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected != actual:
            raise ValueError("Sealed source missing or changed: " + relative)
        sources[relative] = actual
        return load_json(path)

    result = read(directory / "result.json")
    common = read(directory / "score.json")
    spec = read(directory / "inputs/requirements.json")
    row = {"run_key": result["run_key"], "arm": common["agent"],
           "project": spec.get("project_id"), "original_status": common["status"],
           "original_execution_origin": "unavailable",
           "native_agent_task_status": "unavailable", "native_agent_evidence": None,
           "native_agent_unavailable_reason": "No archived native physical evaluation",
           "sources": sources}
    run = read(directory / "records/campaign-run.json")
    observed = []
    stop = datetime.fromisoformat(result["inference_finished_at"].replace("Z", "+00:00"))
    if stop.tzinfo is None:
        raise ValueError("Missing timezone on agent boundary")
    for ref in run.get("invocations", []):
        invocation = read(bound_file(directory / "records", ref))
        start = datetime.fromisoformat(invocation["started_at"].replace("Z", "+00:00"))
        if start.tzinfo is None:
            raise ValueError("Missing timezone on invocation")
        observed.append(invocation.get("recorder_version") == "portable-requirements-v2" and start >= stop)
    if observed and all(observed):
        row["original_execution_origin"] = "post_agent_replay"
    if common["agent"] == "sag":
        paths = list((directory / "sag-native/logs").glob("session_*/requirements-evidence/*/run.json"))
        if len(paths) == 1:
            native_path = paths[0]
            base = native_path.parents[2]
            native = read(native_path)
            native_score = read(native_path.with_name("requirement-results.json"))
            if (native.get("producer") != "sag_authorized_receipt_adapter"
                    or native.get("run_id") != result["run_id"]
                    or native_score.get("run_id") != result["run_id"]
                    or native.get("evaluation_identity") != evaluation_identity(spec)
                    or native_score.get("evaluation_identity") != evaluation_identity(spec)):
                raise ValueError("Native evaluation does not bind this frozen task and run")
            if native.get("run_pin"):
                pin = read(bound_file(base, native["run_pin"]))
                if pin.get("run_id") != result["run_id"]:
                    raise ValueError("Native run pin differs")
            for ref in native.get("invocations", []):
                invocation = read(bound_file(base, ref))
                for name in ("source_receipt", "source_contract"):
                    source = read(bound_file(base, invocation[name]))
                    if source.get("run_id") != result["run_id"]:
                        raise ValueError("Native receipt or contract belongs to another run")
            row.update(native_agent_task_status=native_score["status"],
                       native_agent_evidence=str(native_path.relative_to(directory)),
                       native_agent_unavailable_reason=None)
    return row


def audit_campaign(campaign, destination):
    campaign, destination = Path(campaign), Path(destination)
    if destination.resolve().is_relative_to(campaign.resolve()):
        raise ValueError("Historical analysis must remain outside the frozen campaign")
    if destination.exists():
        raise ValueError("Choose a new analysis directory; existing evidence is never replaced")
    state = load_json(campaign / "state.json")
    slots = state.get("completed")
    if not isinstance(slots, (list, dict)) or not slots:
        raise ValueError("Campaign has no recognized completed run inventory")
    if isinstance(slots, dict):
        slots = list(slots.values())
    if len({s.get("run_id") for s in slots}) != len(slots) or any(not s.get("run_id") for s in slots):
        raise ValueError("Completed inventory has missing or duplicate run identities")
    if state.get("all_slots_completed") is True and len(slots) != state.get("planned_attempts"):
        raise ValueError("Completed inventory differs from the frozen denominator")
    rows = []
    for slot in slots:
        directory = campaign / "runs" / slot["run_id"]
        try:
            row = audit_run(directory)
            if row["run_key"] != slot["run_id"] or row["arm"] != slot["arm"]:
                raise ValueError("Archived run differs from campaign slot")
            row["project"] = slot["project"]
        except (KeyError, ValueError, OSError, TypeError) as exc:
            row = {"run_key": slot["run_id"], "arm": slot["arm"], "project": slot["project"],
                   "original_status": "unavailable", "original_execution_origin": "unavailable",
                   "native_agent_task_status": "unavailable", "audit_error": str(exc)}
        rows.append(row)
    destination.mkdir(parents=True)
    data = {"analysis_policy": "historical-execution-boundary-audit-v1", "rows": rows,
            "limitations": [
                "Original frozen scores are unchanged; native statuses are archived evaluations, not upgraded verdicts.",
                "No native physical evaluation means unavailable, never inferred success or failure.",
                "Native SAG coverage does not establish equivalent baseline coverage; do not rank harnesses from this table.",
                "Historical observer and evaluator limitations remain; this is not a rerun under the repaired protocol."]}
    (destination / "audit.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    fields = ["project", "arm", "original_status", "original_execution_origin", "native_agent_task_status",
              "native_agent_unavailable_reason", "audit_error"]
    with (destination / "comparison.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    return {"runs": len(rows), "replay_scored": sum(r["original_execution_origin"] == "post_agent_replay" for r in rows),
            "native_archives": sum(bool(r.get("native_agent_evidence")) for r in rows),
            "audit_errors": sum(bool(r.get("audit_error")) for r in rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_campaign(args.campaign, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result["audit_errors"] else 0)
