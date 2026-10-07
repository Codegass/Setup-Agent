"""Prospective measurement of interventions under a declared experiment policy.

This records protocol channels, not every action a host administrator could take.
External task-affecting actions are prohibited or must be reported as deviations.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .recorder import no_symlinks, reference, write_json
from .requirements import canonical_digest, file_digest, load_json

VERSION = "sag-unattended-v1"
CHANNELS = {"stdin": "disabled", "runner_control": "disabled_or_logged",
            "external_actions": "prohibited_or_logged"}
KINDS = {"guidance", "manual_edit", "environment_change", "manual_restore", "manual_cancel", "unknown"}
LIMITATION = "Host or Docker administrator bypass is not technically monitored; results rely on the preregistered operator no-unreported-intervention commitment."


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_protocol(policy):
    if not isinstance(policy, dict) or policy.get("version") != VERSION:
        raise ValueError("Unknown intervention protocol version")
    if policy.get("scope") != "task_affecting_human_actions" or policy.get("channels") != CHANNELS:
        raise ValueError("Intervention protocol must declare every supported channel")
    if policy.get("window") != "process_start_to_termination" or policy.get("initial_task_excluded") is not True:
        raise ValueError("Intervention measurement window is not declared")
    if policy.get("operator_commitment") != "report_any_task_affecting_deviation":
        raise ValueError("External intervention reporting commitment is missing")
    if policy.get("manual_cancellation") != "counted":
        raise ValueError("Manual cancellations must be counted")
    return policy


@contextmanager
def locked(directory):
    import fcntl  # The campaign runner is POSIX; offline verification needs no lock API.

    directory = no_symlinks(directory)
    if not directory.is_dir():
        raise ValueError("Prospective intervention ledger is absent")
    path = no_symlinks(directory / ".lock")
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def initialize(directory, *, policy, run_key, runner_file, manifest=None):
    validate_protocol(policy)
    directory = no_symlinks(directory)
    directory.mkdir(parents=True, exist_ok=False)
    record = {"version": VERSION, "run_key": run_key, "initialized_at": now(),
              "policy": policy, "policy_sha256": canonical_digest(policy),
              "runner_sha256": file_digest(runner_file), "external_host_monitoring": "not_technically_monitored"}
    if manifest is not None:
        if manifest.get("intervention_protocol") != policy or manifest.get("runner_sha256") != record["runner_sha256"]:
            raise ValueError("Prepared manifest differs from protocol or running recorder")
        if len([p for p in manifest.get("projects", []) if p.get("run_key") == run_key]) != 1:
            raise ValueError("Intervention attempt is not uniquely declared in the manifest")
        write_json(directory / "manifest.json", manifest)
        record["manifest"] = reference(directory, directory / "manifest.json")
    write_json(directory / "start.json", record)
    return record


def _event(directory, *, kind, detail, source):
    if kind not in KINDS or source not in {"operator_deviation_report", "campaign_signal_handler"}:
        raise ValueError("Unknown intervention event")
    if not isinstance(detail, str) or not detail.strip() or len(detail) > 2000:
        raise ValueError("A bounded intervention description is required")
    start = load_json(directory / "start.json")
    row = {"id": uuid.uuid4().hex, "kind": kind, "detail": detail,
           "source": source, "recorded_at": now(), "run_key": start["run_key"],
           "policy_sha256": start["policy_sha256"]}
    path = directory / ("event-" + row["id"] + ".json")
    with path.open("x") as stream:
        stream.write(json.dumps(row, sort_keys=True, indent=2) + "\n")
    return row


def record_event(directory, *, kind, detail, source="operator_deviation_report"):
    with locked(directory) as directory:
        if (directory / "close.json").exists():
            raise ValueError("Intervention interval is closed; a late discovery requires invalidating this attempt, not rewriting its closed ledger")
        return _event(directory, kind=kind, detail=detail, source=source)


def close(directory, result, *, process_started, process_finished):
    with locked(directory) as directory:
        destination = directory / "close.json"
        if destination.exists():
            return load_json(destination)
        start = load_json(directory / "start.json")
        validate_protocol(start["policy"])
        if start["run_key"] != result["run_key"]:
            raise ValueError("Intervention ledger belongs to another attempt")
        reported_cancel = any(load_json(path).get("kind") == "manual_cancel" for path in directory.glob("event-*.json"))
        if result.get("cancelled") and not reported_cancel:
            _event(directory, kind="manual_cancel", detail="Campaign received an operator stop signal during execution", source="campaign_signal_handler")
        events = sorted(directory.glob("event-*.json"))
        closed_at = now()
        complete = process_started and process_finished and type(result.get("process_id")) is int
        try:
            points = [datetime.fromisoformat(value) for value in
                      (start["initialized_at"], result["process_started_at"], result["process_finished_at"], closed_at)]
            complete = complete and all(value.tzinfo is not None for value in points) and all(a <= b for a, b in zip(points, points[1:]))
        except (KeyError, TypeError, ValueError):
            complete = False
        record = {"version": VERSION, "run_key": result["run_key"],
                  "policy_sha256": start["policy_sha256"], "closed_at": closed_at,
                  "coverage": "complete" if complete else "unavailable",
                  "process_started_at": result.get("process_started_at"),
                  "process_finished_at": result.get("process_finished_at"), "process_id": result.get("process_id"),
                  "automatic_timeout": result.get("outer_timeout", False),
                  "start": reference(directory, directory / "start.json"),
                  "events": [reference(directory, path) for path in events]}
        write_json(destination, record)
        return record


def summary(directory, result):
    """Read closed protocol observations; no policy is equivalent to no evidence."""
    with locked(directory) as directory:
        from .requirements import bound_file

        closed = load_json(directory / "close.json")
        start = load_json(bound_file(directory, closed["start"]))
        policy = validate_protocol(start["policy"])
        policy_hash = canonical_digest(policy)
        if start["policy_sha256"] != policy_hash or closed["policy_sha256"] != policy_hash or start["run_key"] != result["run_key"] or closed["run_key"] != result["run_key"] or result.get("intervention_protocol_sha256", policy_hash) != policy_hash:
            raise ValueError("Intervention protocol identity differs")
        event_paths = [bound_file(directory, ref) for ref in closed["events"]]
        if set(event_paths) != {p.resolve() for p in directory.glob("event-*.json")}:
            raise ValueError("Closed intervention ledger event set changed")
        events = [load_json(path) for path in event_paths]
        if any(e.get("run_key") != result["run_key"] or e.get("policy_sha256") != policy_hash or e.get("kind") not in KINDS for e in events):
            raise ValueError("Intervention event identity differs")
        complete = closed["coverage"] == "complete" and not any(e["kind"] == "unknown" for e in events)
        return {"source": "trusted_noninteractive_runner", "run_id": result.get("run_id"),
                "run_key": result["run_key"], "coverage": "complete" if complete else "unavailable",
                "intervention_protocol": policy, "intervention_protocol_sha256": policy_hash,
                "human_interventions": len(events) if complete else None, "events": events,
                "known_interventions": sum(e["kind"] != "unknown" for e in events),
                "runner_version": start["runner_sha256"],
                "started_at": closed["process_started_at"], "finished_at": closed["process_finished_at"],
                "input_policy": {"stdin": "DEVNULL", "runner_control": "disabled_or_logged", "external_channels": "prohibited_or_logged"},
                "measurement_scope": "declared_protocol_channels",
                "compliance_basis": "preregistered_operator_commitment",
                "external_host_monitoring": "not_technically_monitored", "external_intervention_limit": LIMITATION,
                "protocol_records": {"start": reference(directory, directory / "start.json"),
                                     "close": reference(directory, directory / "close.json"),
                                     "events": [reference(directory, path) for path in event_paths]}}


def copy_evidence(directory, destination, *, base, result, run=None):
    """Copy the closed source ledger and provide session-relative byte bindings."""
    data = summary(directory, result)
    destination = no_symlinks(destination)
    destination.mkdir(parents=True, exist_ok=False)
    records = data["protocol_records"]
    from .requirements import bound_file

    start = load_json(Path(directory) / "start.json")
    if start.get("manifest") is not None:
        records["manifest"] = start["manifest"]
    if run is not None:
        if result.get("authority_ok") is not True or run.get("run_id") != result.get("run_id"):
            raise ValueError("Intervention run binding requires authorized collection")
        pin_path = bound_file(base, run["run_pin"])
        pin = load_json(pin_path)
        if pin.get("run_id") != run["run_id"] or pin.get("sanitized_config", {}).get("evaluation_protocol") != result.get("evaluation_protocol"):
            raise ValueError("Intervention binding differs from actual run pin")
        archived_pin = Path(directory) / "run-pin.json"
        archived_pin.write_bytes(pin_path.read_bytes())
        pin_ref = reference(directory, archived_pin)
        binding = {"run_id": run["run_id"], "run_key": result["run_key"],
                   "process_id": result["process_id"], "evaluation_protocol": result["evaluation_protocol"],
                   "start": records["start"], "close": records["close"], "run_pin": pin_ref}
        binding_path = Path(directory) / "run-binding.json"
        if binding_path.exists():
            raise ValueError("Intervention run binding has already been published")
        write_json(binding_path, binding)
        records.update(binding=reference(directory, binding_path), run_pin=pin_ref)
    copied = {}
    for kind, refs in records.items():
        single = not isinstance(refs, list)
        refs = [refs] if single else refs
        saved = []
        for ref in refs:
            path = bound_file(directory, ref)
            copy = destination / path.name
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("Intervention bytes changed during archival")
            copy.write_bytes(raw)
            saved.append(reference(base, copy))
        copied[kind] = saved[0] if single else saved
    data["protocol_records"] = copied
    return data


def verified_intervention_count(base, telemetry, run):
    """Verify policy registration, interval, event bytes and actual run identity."""
    from .requirements import bound_file

    try:
        if telemetry.get("source") != "trusted_noninteractive_runner" or telemetry.get("run_id") != run.get("run_id"):
            return None
        refs = telemetry["protocol_records"]
        start, closed, manifest, binding, pin = [load_json(bound_file(base, refs[key])) for key in ("start", "close", "manifest", "binding", "run_pin")]
        policy = validate_protocol(start["policy"])
        policy_hash = canonical_digest(policy)
        if any(value != policy_hash for value in (start["policy_sha256"], closed["policy_sha256"], telemetry["intervention_protocol_sha256"])) or telemetry["intervention_protocol"] != policy or manifest["intervention_protocol"] != policy:
            return None
        if start["manifest"]["sha256"] != refs["manifest"]["sha256"] or start["runner_sha256"] != manifest["runner_sha256"] or telemetry["runner_version"] != start["runner_sha256"]:
            return None
        if closed["start"]["sha256"] != refs["start"]["sha256"] or binding["start"]["sha256"] != refs["start"]["sha256"] or binding["close"]["sha256"] != refs["close"]["sha256"]:
            return None
        run_key = start["run_key"]
        if any(value != run_key for value in (closed["run_key"], binding["run_key"], telemetry["run_key"])):
            return None
        projects = [p for p in manifest["projects"] if p.get("run_key") == run_key]
        if len(projects) != 1 or projects[0].get("repo") != run.get("repo") or projects[0].get("sha") != run.get("commit"):
            return None
        if binding["run_id"] != run.get("run_id") or pin["run_id"] != run.get("run_id") or pin.get("target_repo_sha") != run.get("commit"):
            return None
        if binding["run_pin"]["sha256"] != refs["run_pin"]["sha256"] or run["run_pin"]["sha256"] != refs["run_pin"]["sha256"]:
            return None
        bound_file(base, run["run_pin"])
        if pin.get("sanitized_config", {}).get("evaluation_protocol") != binding["evaluation_protocol"] or {k: v for k, v in binding["evaluation_protocol"].items() if k != "requirements_file_sha256"} != run.get("evaluation_identity"):
            return None
        if projects[0].get("evaluation_protocol") != binding["evaluation_protocol"]:
            return None
        if telemetry.get("input_policy") != {"stdin": "DEVNULL", "runner_control": "disabled_or_logged", "external_channels": "prohibited_or_logged"} or telemetry.get("external_host_monitoring") != "not_technically_monitored":
            return None
        if closed["coverage"] != "complete" or telemetry.get("coverage") != "complete" or binding["process_id"] != closed["process_id"] or type(closed["process_id"]) is not int:
            return None
        points = [datetime.fromisoformat(value) for value in (start["initialized_at"], closed["process_started_at"], closed["process_finished_at"], closed["closed_at"])]
        if not all(p.tzinfo is not None for p in points) or not all(a <= b for a, b in zip(points, points[1:])):
            return None
        if telemetry.get("started_at") != closed["process_started_at"] or telemetry.get("finished_at") != closed["process_finished_at"]:
            return None
        declared = {r["sha256"] for r in closed["events"]}
        if len(declared) != len(closed["events"]) or declared != {r["sha256"] for r in refs["events"]} or len(refs["events"]) != len(declared):
            return None
        events = [load_json(bound_file(base, ref)) for ref in refs["events"]]
        if events != telemetry.get("events") or len({e.get("id") for e in events}) != len(events) or any(e.get("run_key") != run_key or e.get("policy_sha256") != policy_hash or e.get("kind") not in KINDS - {"unknown"} or e.get("source") not in {"operator_deviation_report", "campaign_signal_handler"} for e in events):
            return None
        if type(telemetry.get("human_interventions")) is not int or telemetry["human_interventions"] != len(events):
            return None
        return len(events)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
