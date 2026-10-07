"""Join runner timing with observed SAG model usage after process termination.

The response-only CSV cannot certify provider failure/retry billing or absent
usage. It supplies a known subtotal, never a complete all-call total.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
from pathlib import Path

from .evaluator import evaluate
from .recorder import no_symlinks, reference, write_json
from .requirements import evaluation_identity, load_json


_ROLES = {"executor": "actor", "thought": "actor", "action": "actor",
          "advisor": "advisor", "advisor_compression": "summary", "summary": "summary",
          "retry": "retry", "executor_retry": "retry", "advisor_retry": "retry"}
_FIELDS = {"iteration", "timestamp", "type", "model", "total_tokens", "prompt_tokens",
           "completion_tokens", "reasoning_tokens", "actual_output_tokens"}
_LIMIT = ("The response CSV has no complete request ledger: provider errors, SDK retries, "
          "missing usage, and interrupted final export cannot be ruled out.")


def read_response_usage(raw: bytes | None) -> dict:
    """Retain every distinct returned-response row, including same-turn retries."""
    calls, errors, seen = [], [], set()
    if raw is None:
        return {"model_calls": None, "model_calls_complete": False,
                "usage_limitations": [_LIMIT, "Token usage CSV is missing."]}
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8")), strict=True)
        if not _FIELDS <= set(reader.fieldnames or []):
            raise ValueError("Token usage CSV header is incomplete")
        for line, row in enumerate(reader, 2):
            try:
                if None in row or any(row.get(k) is None for k in _FIELDS):
                    raise ValueError("Incomplete CSV row")
                key = tuple(sorted(row.items()))
                if key in seen:
                    raise ValueError("Exact duplicate response row; not counted twice")
                seen.add(key)
                role = _ROLES.get(row["type"].strip())
                if role is None:
                    raise ValueError("Unknown model call type")
                names = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "actual_output_tokens")
                values = {}
                for name in names:
                    value = row[name].strip()
                    if not value.isascii() or not value.isdigit():
                        raise ValueError(f"Invalid {name}")
                    values[name] = int(value)
                prompt, output = values["prompt_tokens"], values["completion_tokens"]
                if values["total_tokens"] != prompt + output:
                    raise ValueError("Total disagrees with prompt + completion")
                if values["reasoning_tokens"] > output or values["actual_output_tokens"] != output - values["reasoning_tokens"]:
                    raise ValueError("Reasoning/output partition disagrees with completion")
                if not prompt + output:
                    raise ValueError("All-zero legacy usage may mean provider usage was absent")
                if not row["timestamp"].strip() or not row["model"].strip():
                    raise ValueError("Model response identity is incomplete")
                calls.append({"role": role, "input_tokens": prompt, "output_tokens": output,
                              "model": row["model"], "source_type": row["type"],
                              "iteration": row["iteration"], "timestamp": row["timestamp"],
                              "source_row": line})
            except ValueError as exc:
                errors.append({"row": line, "reason": str(exc)})
    except (ValueError, UnicodeError, csv.Error) as exc:
        errors.append({"reason": str(exc)})
    return {"model_calls": calls or None, "model_calls_complete": False,
            "usage_limitations": [_LIMIT], "usage_errors": errors,
            "token_accounting": "prompt_tokens + completion_tokens; reasoning is included once; no cache subtraction"}


def finalize_campaign_analysis(session_dir, result, *, runner_file, intervention_file):
    """Publish a derived cost-aware sidecar, preserving the closed SAG export."""
    if result.get("authority_ok") is not True or not result.get("run_id"):
        raise ValueError("Cost analysis needs a host-authorized collected run")
    base = no_symlinks(session_dir).resolve()
    out = base / "requirements-evidence" / hashlib.sha256(result["run_id"].encode()).hexdigest()
    no_symlinks(out)
    run_path = out / "run.json"
    run = load_json(run_path)
    task = load_json(out / "task.json")
    spec = load_json(out / "requirements.json")
    identity = evaluation_identity(spec)
    expected = {k: v for k, v in result.get("evaluation_protocol", {}).items() if k != "requirements_file_sha256"}
    if run.get("run_id") != result["run_id"] or run.get("evaluation_identity") != identity or expected != identity:
        raise ValueError("Cost analysis and frozen run identity differ")
    destination = out / "campaign-run.json"
    if destination.exists():
        raise ValueError("Campaign analysis has already been closed")
    token_path = base / "token_usage.csv"
    no_symlinks(token_path)
    raw = token_path.read_bytes() if token_path.exists() else None
    usage = read_response_usage(raw)
    usage["model_calls_source_kind"] = "legacy_response_csv"
    request_dir = base / "model-requests" / hashlib.sha256(result["run_id"].encode()).hexdigest()
    if request_dir.exists() or request_dir.is_symlink():
        from sag.agent.model_request_ledger import read_model_request_ledger

        no_symlinks(request_dir)
        usage = read_model_request_ledger(base, result["run_id"])
        usage["model_calls_source_kind"] = "durable_harness_request_ledger"
        # A closed, empty ledger observes zero requests. Missing usage from
        # actual or unknown requests does not observe zero token consumption.
        if not usage.get("model_calls") and not (usage.get("harness_requests_complete") is True and usage.get("request_counts", {}).get("started") == 0):
            usage["model_calls"] = None
    telemetry = {"schema_version": 2, "source": "trusted_runner", "run_id": result["run_id"],
                 "runner_version": hashlib.sha256(Path(runner_file).read_bytes()).hexdigest(),
                 "started_at": result.get("process_started_at"), "finished_at": result.get("process_finished_at"),
                 "unattended_seconds": None, "human_interventions": None,
                 "external_intervention_coverage": "unavailable", **usage}
    seconds = result.get("process_seconds")
    if telemetry["started_at"] and telemetry["finished_at"] and type(seconds) in {int, float} and math.isfinite(seconds) and seconds >= 0:
        telemetry["unattended_seconds"] = seconds
    if raw is not None:
        copy = out / "campaign-token-usage.csv"
        copy.write_bytes(raw)
        telemetry["legacy_csv_source"] = reference(base, copy)
        if usage["model_calls_source_kind"] == "legacy_response_csv":
            telemetry["model_calls_source"] = telemetry["legacy_csv_source"]
    intervention = no_symlinks(intervention_file)
    intervention_copy = out / "campaign-interventions.json"
    intervention_copy.write_bytes(intervention.read_bytes())
    data = load_json(intervention_copy)
    if data.get("run_id") != result["run_id"]:
        raise ValueError("Intervention record belongs to another run")
    telemetry["intervention_source"] = reference(base, intervention_copy)
    if data.get("intervention_protocol") is not None:
        from .intervention_protocol import copy_evidence

        observed = copy_evidence(intervention.parent / "intervention-ledger",
                                 out / "campaign-intervention-ledger", base=base, result=result, run=run)
        if observed.get("intervention_protocol_sha256") != data.get("intervention_protocol_sha256"):
            raise ValueError("Intervention policy differs from its closed telemetry")
        telemetry.update(observed)
    write_json(out / "campaign-telemetry.json", telemetry)
    derived = {**run, "telemetry": reference(base, out / "campaign-telemetry.json"),
               "source_run": reference(base, run_path)}
    write_json(destination, derived)
    score = evaluate(task, spec, derived, base)
    score["cost_limitations"] = telemetry["usage_limitations"]
    score["usage_errors"] = telemetry.get("usage_errors", [])
    write_json(out / "campaign-requirement-results.json", score)
    return {"run": reference(base, destination),
            "result": reference(base, out / "campaign-requirement-results.json"),
            "telemetry": reference(base, out / "campaign-telemetry.json"),
            "evidence_base": str(base), "status": score["status"],
            "known_tokens": score["known_tokens"], "total_tokens": score["total_tokens"],
            "unattended_seconds": score["unattended_seconds"], "cost_coverage": score["cost_coverage"]}
