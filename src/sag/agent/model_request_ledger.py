"""Durable harness-request accounting; SDK/provider-internal attempts stay unknown.

Only identities, timing, outcome and normalized usage are retained. Prompts,
responses, exception messages, URLs, headers and credentials are never copied.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from loguru import logger

_LIMIT = "SDK/provider-internal attempts and failure billing are not observable; this is an observed subtotal, not a complete billed total."
_ROLES = frozenset({"actor", "advisor", "summary"})


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _usage(response):
    usage = _field(response, "usage")
    if usage is None:
        return {"status": "missing", "reason": "provider_usage_absent"}
    prompt, output = _field(usage, "prompt_tokens"), _field(usage, "completion_tokens")
    total = _field(usage, "total_tokens")
    if (
        any(type(v) is not int or v < 0 for v in (prompt, output))
        or total is not None
        and (type(total) is not int or total != prompt + output)
    ):
        return {"status": "invalid", "reason": "normalized_usage_counts_invalid"}
    result = {
        "status": "observed",
        "input_tokens": prompt,
        "output_tokens": output,
        "source": "litellm_normalized_usage",
    }
    details = _field(usage, "completion_tokens_details")
    reasoning = _field(details, "reasoning_tokens")
    if type(reasoning) is int and 0 <= reasoning <= output:
        result["reasoning_tokens"] = reasoning
    # The reasoning count is a subset of output, never an additional bill.
    return result


def _write_once(path, value):
    raw = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _now():
    return datetime.now(timezone.utc).isoformat()


class ModelRequestLedger:
    """Each LiteLLM call gets a persisted begin before provider dispatch."""

    def __init__(self, session_dir, run_id):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Request ledger requires the host run identity")
        self.base = Path(session_dir).resolve()
        self.run_id = run_id
        self.directory = self.base / "model-requests" / hashlib.sha256(run_id.encode()).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._pending = {}
        self._attempted = self._finished = 0
        self._errors = []
        self._closed = False
        try:
            _write_once(
                self.directory / "open.json",
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "session": self.base.name,
                    "started_at": _now(),
                    "source": "sag_litellm_boundary",
                    "provider_attempt_coverage": "unavailable",
                },
            )
        except (OSError, ValueError, TypeError) as exc:
            self._failure("open", exc)
            raise

    def _failure(self, operation, exc):
        self._errors.append({"operation": operation, "error_type": type(exc).__name__})
        logger.warning("Model request accounting {} unavailable: {}", operation, type(exc).__name__)
        try:
            _write_once(
                self.directory / ("observer-error-" + uuid.uuid4().hex + ".json"),
                {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "operation": operation,
                    "error_type": type(exc).__name__,
                },
            )
        except (OSError, ValueError, TypeError):
            pass

    def begin(self, *, role, model, iteration=None, request_phase=None, messages_sha256=None):
        with self._lock:
            self._attempted += 1
            identity = uuid.uuid4().hex
            try:
                if self._closed or role not in _ROLES or not isinstance(model, str):
                    raise ValueError("Request accounting is closed or identity is invalid")
                record = {
                    "schema_version": 1,
                    "event": "begin",
                    "run_id": self.run_id,
                    "request_id": identity,
                    "role": role,
                    "model": model,
                    "started_at": _now(),
                    "iteration": iteration if type(iteration) is int else None,
                }
                if request_phase in {"provision", "analyze", "build", "test", "report"}:
                    record["request_phase"] = request_phase
                if (isinstance(messages_sha256, str) and len(messages_sha256) == 64
                        and all(c in "0123456789abcdef" for c in messages_sha256)):
                    record["messages_sha256"] = messages_sha256
                _write_once(self.directory / (identity + ".begin.json"), record)
                self._pending[identity] = (record, time.monotonic())
                return identity
            except (OSError, ValueError, TypeError) as exc:
                self._failure("begin", exc)
                return None

    def finish(self, request_id, *, response=None, error=None):
        with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            self._finished += 1
            record, started = pending
            try:
                status = (
                    "returned"
                    if error is None
                    else "error" if isinstance(error, Exception) else "interrupted"
                )
                record = {
                    **record,
                    "event": "end",
                    "finished_at": _now(),
                    "elapsed_seconds": max(0.0, time.monotonic() - started),
                    "status": status,
                    "usage": (
                        _usage(response)
                        if error is None
                        else {"status": "missing", "reason": "provider_error_usage_unavailable"}
                    ),
                }
                if error is not None:
                    record["error_type"] = type(error).__name__
                else:
                    response_id = _field(response, "id")
                    if isinstance(response_id, str):
                        record["response_id"] = response_id
                    returned_model = _field(response, "model")
                    if isinstance(returned_model, str):
                        record["returned_model"] = returned_model
                _write_once(self.directory / (request_id + ".end.json"), record)
            except (OSError, ValueError, TypeError) as exc:
                self._failure("end", exc)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                _write_once(
                    self.directory / "close.json",
                    {
                        "schema_version": 1,
                        "run_id": self.run_id,
                        "finished_at": _now(),
                        "attempted_requests": self._attempted,
                        "finished_requests": self._finished,
                        "in_flight": sorted(self._pending),
                        "observer_errors": self._errors,
                    },
                )
            except (OSError, ValueError, TypeError) as exc:
                self._failure("close", exc)


def read_model_request_ledger(session_dir, run_id):
    """Return known usage once per UUID, plus explicit unknown request coverage."""
    base = Path(session_dir).resolve()
    directory = base / "model-requests" / hashlib.sha256(run_id.encode()).hexdigest()
    refs, errors, calls = [], [], []
    counts = dict(
        started=0, returned=0, error=0, interrupted=0, in_flight=0, usage_known=0, usage_unknown=0
    )
    output = {
        "available": False,
        "model_calls": calls,
        "model_calls_complete": False,
        "harness_requests_complete": False,
        "request_counts": counts,
        "usage_limitations": [_LIMIT],
        "usage_errors": errors,
        "evidence_refs": refs,
        "provider_attempt_coverage": "unavailable",
        "attempted_requests": None,
        "unobserved_requests": None,
        "token_accounting": "normalized prompt + completion; reasoning included once; no cache subtraction",
    }

    def read(path):
        if path.is_symlink() or not path.resolve().is_relative_to(base):
            raise ValueError("Request evidence escapes host session")
        raw = path.read_bytes()
        refs.append(
            {
                "path": str(path.relative_to(base)),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
            or value.get("run_id") != run_id
        ):
            raise ValueError("Request evidence belongs to another run or protocol")
        return value

    try:
        opening = read(directory / "open.json")
        if opening.get("source") != "sag_litellm_boundary" or opening.get("session") != base.name:
            raise ValueError("Request observer session/source mismatch")
        output["available"] = True
        begins = list(sorted(directory.glob("*.begin.json")))
        expected_ends = set()
        for path in begins:
            counts["started"] += 1
            try:
                begin = read(path)
                identity = begin.get("request_id")
                if (
                    begin.get("event") != "begin"
                    or begin.get("role") not in _ROLES
                    or not isinstance(begin.get("model"), str)
                    or not isinstance(identity, str)
                    or path.name != identity + ".begin.json"
                ):
                    raise ValueError("Malformed request begin")
                end_path = directory / (identity + ".end.json")
                expected_ends.add(end_path.name)
                if not end_path.exists():
                    counts["in_flight"] += 1
                    counts["usage_unknown"] += 1
                    continue
                end = read(end_path)
                if (
                    end.get("event") != "end"
                    or any(
                        end.get(k) != begin.get(k)
                        for k in ("request_id", "role", "model", "iteration", "started_at",
                                  "request_phase", "messages_sha256")
                    )
                    or end.get("status") not in {"returned", "error", "interrupted"}
                ):
                    raise ValueError("Request end identity/status mismatch")
                counts[end["status"]] += 1
                usage = end.get("usage") or {}
                if not isinstance(usage, dict):
                    raise ValueError("Malformed request usage observation")
                known = (
                    end["status"] == "returned"
                    and usage.get("status") == "observed"
                    and all(
                        type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("input_tokens", "output_tokens")
                    )
                )
                counts["usage_known" if known else "usage_unknown"] += 1
                if known:
                    calls.append(
                        {
                            "request_id": identity,
                            "role": begin["role"],
                            "model": begin["model"],
                            "iteration": begin.get("iteration"),
                            "input_tokens": usage["input_tokens"],
                            "output_tokens": usage["output_tokens"],
                            "source": str(end_path.relative_to(base)),
                        }
                    )
            except (OSError, ValueError, TypeError) as exc:
                errors.append({"file": path.name, "reason": str(exc)})
                counts["usage_unknown"] += 1
        unexpected = {p.name for p in directory.glob("*.end.json")} - expected_ends
        if unexpected:
            errors.append(
                {
                    "reason": "Request end exists without a matching begin",
                    "files": sorted(unexpected),
                }
            )
        closing = read(directory / "close.json")
        attempted = closing.get("attempted_requests")
        if type(attempted) is int and attempted >= counts["started"]:
            output["attempted_requests"] = attempted
            output["unobserved_requests"] = attempted - counts["started"]
            counts["usage_unknown"] += output["unobserved_requests"]
        for failure_path in sorted(directory.glob("observer-error-*.json")):
            failure = read(failure_path)
            errors.append(
                {"operation": failure.get("operation"), "error_type": failure.get("error_type")}
            )
        if closing.get("observer_errors"):
            errors.extend(closing["observer_errors"])
        output["harness_requests_complete"] = (
            not errors
            and type(closing.get("attempted_requests")) is int
            and type(closing.get("finished_requests")) is int
            and closing.get("attempted_requests") == counts["started"]
            and closing.get("finished_requests")
            == counts["returned"] + counts["error"] + counts["interrupted"]
            and not counts["in_flight"]
            and closing.get("in_flight") == []
        )
        if closing.get("attempted_requests") != counts["started"]:
            errors.append({"reason": "Not every attempted harness request has a retained begin"})
    except (OSError, ValueError, TypeError) as exc:
        errors.append({"reason": str(exc)})
    return output
