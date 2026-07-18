#!/usr/bin/env python3
"""Pinned four-probe collection, surface checking, and campaign summaries."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sag.agent.control_events import (
    ControlEvent,
    ControlEventSink,
    RunPin,
    canonical_json,
    canonical_sha256,
    sanitize_config,
)
from sag.agent.verdict_finalizer import RunVerdictSnapshot


class CollectionError(RuntimeError):
    pass


class RunMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: str
    unique_total: int = 0
    unique_passed: int = 0
    unique_failed: int = 0
    unique_errors: int = 0
    unique_skipped: int = 0
    raw_executions: int = 0
    flaky_count: int = 0
    compiled_classes: int | None = None
    thought_calls: int = 0
    action_calls: int = 0
    gate_interactions: int = 0
    max_failure_gate_interactions: int = 0
    rejected_gate_claims: int = 0
    envelope_count: int = 0
    repair_count: int = 0
    invalid_repair_count: int = 0
    diversity_break_count: int = 0
    second_occurrence_guides: int = 0
    second_occurrence_miss_count: int = 0
    contradicted_gate_survival_count: int = 0
    entry_prerequisite_violations: int = 0
    evidence_contract_violations: int = 0
    duplicate_envelope_count: int = 0
    tool_result_count: int = 0
    duplicate_tool_result_count: int = 0
    unmatched_envelope_count: int = 0
    envelope_drift_count: int = 0
    orphan_tool_result_count: int = 0
    repair_budget_violations: int = 0
    conflicts: tuple[str, ...] = ()


class CollectedRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    session_path: str
    source_schema: str
    pin: RunPin
    metrics: RunMetrics
    surface_ok: bool | None = None
    surface_mismatches: tuple[str, ...] = ()
    structured_inputs: tuple[str, ...]


class SurfaceMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: str
    field: str
    expected: Any
    actual: Any


class SurfaceCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    mismatches: tuple[SurfaceMismatch, ...] = ()
    snapshot_path: str


class MetricSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    probe: str
    stage: str
    name: str
    median: float
    minimum: float
    maximum: float
    count: int

    def render(self) -> str:
        def value(number: float) -> str:
            return str(int(number)) if number.is_integer() else f"{number:g}"

        return f"{value(self.median)} [{value(self.minimum)}-{value(self.maximum)}]"


class CampaignSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metrics: tuple[MetricSummary, ...]
    failures: tuple[str, ...] = ()

    def metric(self, probe: str, name: str, stage: str | None = None) -> MetricSummary:
        matches = [
            metric
            for metric in self.metrics
            if metric.probe == probe
            and metric.name == name
            and (stage is None or metric.stage == stage)
        ]
        if len(matches) != 1:
            raise KeyError((probe, stage, name))
        return matches[0]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"cannot read structured artifact {path}: {exc}") from exc


def _first_existing(*paths: Path) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _load_pin(session: Path) -> tuple[RunPin, Path]:
    path = _first_existing(
        session / ".setup_agent" / "run-pin.json",
        session / "run-pin.json",
    )
    if path is None:
        raise CollectionError("run-pin.json is missing")
    try:
        return RunPin.model_validate(_load_json(path)), path
    except ValidationError as exc:
        missing = [
            str(error["loc"][0])
            for error in exc.errors()
            if error.get("type") == "missing" and error.get("loc")
        ]
        detail = ", ".join(missing) if missing else str(exc)
        raise CollectionError(f"incomplete run pin: {detail}") from exc


def _validate_current_run_pin(
    session: Path,
    *,
    target_repo_sha: str,
    sag_git_sha: str,
    random_seed: int,
    dependency_cache_state: str,
    host_arch: str,
) -> RunPin:
    """Validate runtime-owned host/container pins without rewriting either truth."""
    host_path = session / "run-pin.json"
    container_path = session / ".setup_agent" / "run-pin.json"
    if not host_path.is_file() or not container_path.is_file():
        raise CollectionError("current run requires both host and container run-pin mirrors")
    try:
        pin = RunPin.model_validate(_load_json(host_path))
        container_pin = RunPin.model_validate(_load_json(container_path))
    except ValidationError as exc:
        raise CollectionError(f"current run pin is invalid: {exc}") from exc
    if pin.model_dump(mode="json") != container_pin.model_dump(mode="json"):
        raise CollectionError("host/container run-pin mirrors differ")
    expected = {
        "target_repo_sha": target_repo_sha,
        "sag_git_sha": sag_git_sha,
        "random_seed_or_null": random_seed,
        "dependency_cache_state": dependency_cache_state,
        "host_arch": host_arch,
    }
    actual = pin.model_dump(mode="json")
    drift = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if drift:
        raise CollectionError(
            f"current run pin disagrees with external facts: {canonical_json(drift)}"
        )
    return cast(RunPin, pin)


def _token_call_counts(path: Path | None) -> tuple[int, int]:
    if path is None:
        return 0, 0
    thinking = 0
    action = 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                kind = (
                    str(
                        row.get("model_type")
                        or row.get("model_mode")
                        or row.get("call_type")
                        or row.get("type")
                        or ""
                    )
                    .strip()
                    .lower()
                )
                was_thinking = str(row.get("was_thinking_model") or "").strip().lower()
                if kind in {"thinking", "reasoning", "think", "thought"} or was_thinking == "true":
                    thinking += 1
                elif kind in {"action", "actor", "act"} or was_thinking == "false":
                    action += 1
    except OSError as exc:
        raise CollectionError(f"cannot read token CSV {path}: {exc}") from exc
    return thinking, action


def _read_control_events(path: Path | None) -> tuple[ControlEvent, ...]:
    if path is None:
        return ()
    events: list[ControlEvent] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(ControlEvent.model_validate_json(line))
    except (OSError, ValueError) as exc:
        raise CollectionError(f"invalid control event stream {path}: {exc}") from exc
    for index, event in enumerate(events, 1):
        if event.sequence != index:
            raise CollectionError("control event sequence is not monotonic")
    return tuple(events)


def _event_metrics(events: Sequence[ControlEvent]) -> dict[str, int | None]:
    compiled_classes: int | None = None
    gate_interactions = 0
    gate_interactions_in_attempt = 0
    max_failure_gate_interactions = 0
    rejected_gates = 0
    envelopes = 0
    repairs = 0
    invalid_repairs = 0
    diversity_breaks = 0
    second_guides = 0
    second_misses = 0
    contradicted_gate_survivals = 0
    entry_prerequisite_violations = 0
    evidence_contract_violations = 0
    duplicate_envelopes = 0
    tool_results = 0
    duplicate_tool_results = 0
    envelope_drift = 0
    orphan_results = 0
    repair_budget_violations = 0
    envelopes_by_id: dict[str, Mapping[str, Any]] = {}
    result_envelope_ids: set[str] = set()
    repair_counts: dict[str, int] = {}
    last_gate: Mapping[str, Any] | None = None
    for event in events:
        payload = event.payload
        if event.kind == "action_envelope":
            envelopes += 1
            envelope_id = str(payload.get("envelope_id") or "")
            duplicate_envelopes += int(envelope_id in envelopes_by_id)
            envelopes_by_id[envelope_id] = payload
        elif event.kind == "gate_decision":
            gate_interactions += 1
            gate_interactions_in_attempt += 1
            rejected_gates += int(not payload.get("expected_accepted", True))
            if (
                payload.get("expected_accepted", True)
                and payload.get("expected_outcome") == "failed"
            ):
                max_failure_gate_interactions = max(
                    max_failure_gate_interactions,
                    gate_interactions_in_attempt,
                )
            last_gate = payload
        elif event.kind == "phase_transition":
            if last_gate is not None and not last_gate.get("expected_accepted", True):
                contradicted_gate_survivals += 1
            target = payload.get("expected_target")
            prerequisite = {
                "analyze": "provision.workspace_ready",
                "build": "analysis.build_entry_ready",
                "test": "build.test_entry_ready",
            }.get(str(target))
            if payload.get("expected_kind") == "advance" and prerequisite:
                facts = (last_gate or {}).get("validated_facts") or {}
                entry_prerequisite_violations += int(facts.get(prerequisite) is not True)
            repair = payload.get("repair_request")
            if repair:
                repairs += 1
                edge = (repair.get("from_phase"), repair.get("target_phase"))
                valid = (
                    payload.get("expected_kind") == "repair"
                    and edge in {("test", "build"), ("build", "analyze")}
                    and bool(repair.get("source_attempt_id"))
                    and bool(repair.get("failure_signature"))
                    and bool(repair.get("hypothesis"))
                    and bool(repair.get("evidence_refs"))
                )
                invalid_repairs += int(not valid)
                source_phase = str(repair.get("from_phase") or "")
                repair_counts[source_phase] = repair_counts.get(source_phase, 0) + 1
                repair_budget_violations += int(
                    repair_counts[source_phase] > 1 or sum(repair_counts.values()) > 2
                )
            last_gate = None
            gate_interactions_in_attempt = 0
        elif event.kind == "loop_decision":
            decision = payload.get("expected_decision")
            diversity_breaks += int(
                decision in {"force_break", "close_phase"} and "diversity" in str(payload)
            )
            loop_event = payload.get("event") or {}
            recurrence_count = (
                loop_event.get("recurrence_count") if isinstance(loop_event, Mapping) else None
            )
            if recurrence_count == 2:
                second_guides += int(decision == "guide")
                second_misses += int(decision != "guide")
            elif recurrence_count is None:
                # Schema-0 imported recordings did not carry the explicit count.
                second_guides += int(decision == "guide")
        elif event.kind == "tool_result":
            tool_results += 1
            envelope_id = str(payload.get("envelope_id") or "")
            duplicate_tool_results += int(envelope_id in result_envelope_ids)
            if envelope_id:
                result_envelope_ids.add(envelope_id)
            envelope = envelopes_by_id.get(envelope_id)
            if envelope is None:
                orphan_results += 1
            else:
                envelope_drift += int(
                    envelope.get("tool") != payload.get("tool")
                    or envelope.get("exact_params") != payload.get("params")
                )
            result = payload.get("result") or {}
            terminal = result.get("invocation_status") != "pending"
            has_evidence = any(
                (
                    result.get("output_ref"),
                    result.get("evidence_refs"),
                    result.get("refs"),
                    result.get("facts"),
                    result.get("test_stats"),
                )
            )
            evidence_contract_violations += int(
                terminal
                and result.get("evidence_status") == "verified"
                and payload.get("tool") not in {"phase", "report"}
                and not has_evidence
            )
            candidates: list[Any] = []
            for container in (
                result.get("facts") or {},
                result.get("metadata") or {},
                result.get("raw_data") or {},
            ):
                if isinstance(container, Mapping):
                    candidates.extend(
                        container.get(key)
                        for key in (
                            "compiled_classes",
                            "compiled_class_count",
                            "class_count",
                        )
                    )
            numeric = [value for value in candidates if type(value) is int and value >= 0]
            if numeric:
                compiled_classes = max([compiled_classes or 0, *numeric])
    return {
        "compiled_classes": compiled_classes,
        "gate_interactions": gate_interactions,
        "max_failure_gate_interactions": max_failure_gate_interactions,
        "rejected_gate_claims": rejected_gates,
        "envelope_count": envelopes,
        "repair_count": repairs,
        "invalid_repair_count": invalid_repairs,
        "diversity_break_count": diversity_breaks,
        "second_occurrence_guides": second_guides,
        "second_occurrence_miss_count": second_misses,
        "contradicted_gate_survival_count": contradicted_gate_survivals,
        "entry_prerequisite_violations": entry_prerequisite_violations,
        "evidence_contract_violations": evidence_contract_violations,
        "duplicate_envelope_count": duplicate_envelopes,
        "tool_result_count": tool_results,
        "duplicate_tool_result_count": duplicate_tool_results,
        "unmatched_envelope_count": len(set(envelopes_by_id) - result_envelope_ids),
        "envelope_drift_count": envelope_drift,
        "orphan_tool_result_count": orphan_results,
        "repair_budget_violations": repair_budget_violations,
    }


def _new_snapshot_metrics(
    payload: Mapping[str, Any],
    *,
    thought_calls: int,
    action_calls: int,
    event_metrics: Mapping[str, Any],
) -> tuple[str, RunMetrics]:
    try:
        snapshot = RunVerdictSnapshot.model_validate(payload)
    except ValidationError as exc:
        raise CollectionError(f"invalid verdict.json: {exc}") from exc
    tests = snapshot.test_stats
    metrics = RunMetrics(
        verdict=snapshot.verdict,
        unique_total=tests.unique.executed,
        unique_passed=tests.unique.passed,
        unique_failed=tests.unique.failed,
        unique_errors=tests.unique.errors,
        unique_skipped=tests.unique.skipped,
        raw_executions=tests.raw.executed,
        flaky_count=tests.flaky_count,
        thought_calls=thought_calls,
        action_calls=action_calls,
        conflicts=tuple(snapshot.conflicts),
        **event_metrics,
    )
    return snapshot.run_id, metrics


def _find_report_snapshot(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        direct = value.get("report_snapshot")
        if isinstance(direct, Mapping):
            return direct
        for child in value.values():
            found = _find_report_snapshot(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_report_snapshot(child)
            if found is not None:
                return found
    return None


def _find_structured_report_action(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        parameters = value.get("parameters")
        if (
            value.get("tool_name") == "report"
            and value.get("success") is True
            and isinstance(parameters, Mapping)
            and isinstance(parameters.get("test_stats"), Mapping)
        ):
            return parameters
        for child in reversed(tuple(value.values())):
            found = _find_structured_report_action(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in reversed(value):
            found = _find_structured_report_action(child)
            if found is not None:
                return found
    return None


def _legacy_metrics(
    session: Path,
    *,
    thought_calls: int,
    action_calls: int,
    event_metrics: Mapping[str, Any],
) -> tuple[str, RunMetrics, Path]:
    phase_report = session / ".setup_agent" / "contexts" / "phase_report.json"
    if not phase_report.is_file():
        raise CollectionError("neither verdict.json nor legacy structured phase_report.json exists")
    phase_payload = _load_json(phase_report)
    snapshot = _find_report_snapshot(phase_payload)
    if snapshot is None:
        action = _find_structured_report_action(phase_payload)
        if action is None:
            raise CollectionError(
                "legacy phase_report has neither report_snapshot nor a structured report action"
            )
        snapshot = {
            "verdict": action.get("status") or action.get("evidence_status") or "unknown",
            "test_stats": action.get("test_stats"),
            "conflicts": action.get("conflicts") or (),
        }
    test = snapshot.get("test_stats") or snapshot.get("test") or {}
    if not isinstance(test, Mapping):
        raise CollectionError("legacy structured test_stats is invalid")
    executed = int(test.get("unique_executed", test.get("executed", test.get("total", 0))) or 0)
    passed = int(test.get("unique_passed", test.get("passed", 0)) or 0)
    failed = int(test.get("unique_failed", test.get("failed", 0)) or 0)
    errors = int(test.get("unique_errors", test.get("errors", 0)) or 0)
    skipped = int(test.get("unique_skipped", test.get("skipped", 0)) or 0)
    metrics = RunMetrics(
        verdict=str(snapshot.get("verdict") or snapshot.get("status") or "unknown").lower(),
        unique_total=executed,
        unique_passed=passed,
        unique_failed=failed,
        unique_errors=errors,
        unique_skipped=skipped,
        raw_executions=int(test.get("raw_executions", executed) or executed),
        flaky_count=int(test.get("flaky_count", 0) or 0),
        thought_calls=thought_calls,
        action_calls=action_calls,
        conflicts=tuple(snapshot.get("conflicts") or ()),
        **event_metrics,
    )
    return str(snapshot.get("run_id") or session.name), metrics, phase_report


class ABCollector:
    """Read structured run truth only. Rendered artifacts are never opened."""

    def collect(self, session_path: str | Path) -> CollectedRun:
        session = Path(session_path)
        pin, pin_path = _load_pin(session)
        verdict_path = _first_existing(
            session / ".setup_agent" / "verdict.json",
            session / "verdict.json",
        )
        events_path = _first_existing(
            session / ".setup_agent" / "control_events.jsonl",
            session / "control_events.jsonl",
        )
        token_path = _first_existing(
            session / "token_usage.csv", session / ".setup_agent" / "token_usage.csv"
        )
        events = _read_control_events(events_path)
        thought_calls, action_calls = _token_call_counts(token_path)
        event_metrics = _event_metrics(events)
        structured = [str(pin_path)]
        if events_path is not None:
            structured.append(str(events_path))
        if token_path is not None:
            structured.append(str(token_path))
        if verdict_path is not None:
            run_id, metrics = _new_snapshot_metrics(
                _load_json(verdict_path),
                thought_calls=thought_calls,
                action_calls=action_calls,
                event_metrics=event_metrics,
            )
            source_schema = "verdict_v3"
            structured.append(str(verdict_path))
        else:
            run_id, metrics, phase_report = _legacy_metrics(
                session,
                thought_calls=thought_calls,
                action_calls=action_calls,
                event_metrics=event_metrics,
            )
            source_schema = "legacy_structured"
            structured.append(str(phase_report))
        return CollectedRun(
            run_id=run_id,
            session_path=str(session),
            source_schema=source_schema,
            pin=pin,
            metrics=metrics,
            structured_inputs=tuple(structured),
        )


_TEXT_TESTS_RATIO = re.compile(
    r"\bTests?\s*:?\s*(?P<passed>\d+)\s*/\s*(?P<total>\d+)\s+passed\b",
    re.IGNORECASE,
)
_TEXT_TESTS_CLI = re.compile(
    r"\bTests?\s*:\s*(?P<total>\d+)\s+unique\s*" r"\(\s*(?P<passed>\d+)\s+passed\b",
    re.IGNORECASE,
)
_TEXT_NO_TESTS = re.compile(
    r"\bTests?\s*:\s*(?:no tests executed|0\s+of\s+\d+\s+detected tests executed)",
    re.IGNORECASE,
)
_TEXT_FLAKY = re.compile(r"\b(?P<flaky>\d+)\s+flaky\b", re.IGNORECASE)
_TEXT_VERDICT = re.compile(r"\b(SUCCESS|PARTIAL|FAILED|UNKNOWN)\b", re.IGNORECASE)
_TEXT_PERCENT = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)%")
_TEXT_FAILED = re.compile(r"\b(?P<value>\d+)\s+failed\b", re.IGNORECASE)
_TEXT_ERRORS = re.compile(r"\b(?P<value>\d+)\s+errors?\b", re.IGNORECASE)
_TEXT_SKIPPED = re.compile(r"\b(?P<value>\d+)\s+skipped\b", re.IGNORECASE)
_TEXT_RAW = re.compile(
    r"\bRaw\s+executions?(?:\s*\(diagnostic\))?\s*:\s*(?P<value>\d+)\b",
    re.IGNORECASE,
)


def _text_surface(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    normalized = text.replace("**", "")
    verdict = _TEXT_VERDICT.search(normalized)
    tests = _TEXT_TESTS_RATIO.search(normalized) or _TEXT_TESTS_CLI.search(normalized)
    no_tests = _TEXT_NO_TESTS.search(normalized)
    anchor = tests or no_tests
    if anchor is not None:
        line_start = normalized.rfind("\n", 0, anchor.start()) + 1
        line_end = normalized.find("\n", anchor.end())
        test_line = normalized[line_start : line_end if line_end >= 0 else len(normalized)]
    else:
        test_line = ""
    flaky = _TEXT_FLAKY.search(test_line)
    failed = _TEXT_FAILED.search(test_line)
    errors = _TEXT_ERRORS.search(test_line)
    skipped = _TEXT_SKIPPED.search(test_line)
    raw = _TEXT_RAW.search(normalized)
    ratios = [float(match.group(1)) for match in _TEXT_PERCENT.finditer(normalized)]
    return {
        "verdict": verdict.group(1).lower() if verdict else None,
        "passed": int(tests.group("passed")) if tests else (0 if no_tests else None),
        "total": int(tests.group("total")) if tests else (0 if no_tests else None),
        "flaky_count": int(flaky.group("flaky")) if flaky else 0,
        "failed": int(failed.group("value")) if failed else (0 if no_tests else None),
        "errors": int(errors.group("value")) if errors else (0 if no_tests else None),
        "skipped": int(skipped.group("value")) if skipped else (0 if no_tests else None),
        "raw_executions": int(raw.group("value")) if raw else None,
        "ratio_over_100": next((value for value in ratios if value > 100.0), None),
    }


def _web_surface(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    test = payload.get("test") or {}
    return {
        "verdict": str(payload.get("verdict") or "").lower() or None,
        "passed": test.get("passed", test.get("pass")),
        "total": test.get("total"),
        "flaky_count": test.get("flakyCount", test.get("flaky_count", 0)),
        "failed": test.get("fail", test.get("failed")),
        "errors": test.get("errors"),
        "skipped": test.get("skip", test.get("skipped")),
        "raw_executions": test.get("raw_executions", test.get("rawExecutions")),
        "ratio_over_100": None,
    }


def _named_text_values(value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(child, str):
                yield str(key), child
            else:
                yield from _named_text_values(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _named_text_values(child)


def _recorded_condensed_output(phase_report: Path) -> str:
    candidates: list[tuple[int, int, str]] = []
    for index, (name, value) in enumerate(_named_text_values(_load_json(phase_report))):
        if "Tests:" not in value or _TEXT_VERDICT.search(value) is None:
            continue
        score = 0
        score += 100 if name == "output" else 0
        score += 50 if "SETUP COMPLETED" in value else 0
        score += 25 if "Result:" in value else 0
        score += 10 if "Full report saved to:" in value else 0
        candidates.append((score, index, value))
    if not candidates:
        raise CollectionError("phase_report.json has no recorded condensed report output")
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def prepare_surface_artifacts(
    session_path: str | Path,
    captured_cli_path: str | Path,
) -> Path:
    """Materialize the four production-rendered surfaces for literal checking."""
    from sag.web.session_registry import _snapshot_test_payload

    session = Path(session_path)
    snapshot_path = _first_existing(
        session / ".setup_agent" / "verdict.json",
        session / "verdict.json",
    )
    if snapshot_path is None:
        raise CollectionError("surface preparation requires verdict.json")
    snapshot = RunVerdictSnapshot.model_validate(_load_json(snapshot_path))

    reports = sorted(session.glob("setup-report-*.md"))
    if not reports:
        raise CollectionError("surface preparation requires a setup-report markdown artifact")
    markdown_path = reports[-1]
    phase_report = _first_existing(
        session / ".setup_agent" / "contexts" / "phase_report.json",
        session / "contexts" / "phase_report.json",
    )
    if phase_report is None:
        raise CollectionError("surface preparation requires phase_report.json")

    cli_source = Path(captured_cli_path)
    if not cli_source.is_file():
        raise CollectionError(f"captured CLI output is missing: {cli_source}")
    cli_path = session / "surface-cli.log"
    shutil.copyfile(cli_source, cli_path)

    condensed_path = session / "surface-condensed.log"
    condensed_path.write_text(_recorded_condensed_output(phase_report), encoding="utf-8")

    web_test = _snapshot_test_payload(snapshot)
    web_path = session / "web-read-model.json"
    web_path.write_text(
        canonical_json({"verdict": snapshot.verdict, "test": web_test}),
        encoding="utf-8",
    )

    manifest = {
        "markdown": markdown_path.name,
        "condensed": condensed_path.name,
        "cli": cli_path.name,
        "web": web_path.name,
    }
    manifest_path = session / "surface-artifacts.json"
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    return manifest_path


def check_surfaces(session_path: str | Path) -> SurfaceCheckResult:
    session = Path(session_path)
    snapshot_path = _first_existing(
        session / ".setup_agent" / "verdict.json",
        session / "verdict.json",
    )
    if snapshot_path is None:
        raise CollectionError("surface check requires verdict.json")
    snapshot = RunVerdictSnapshot.model_validate(_load_json(snapshot_path))
    expected = {
        "verdict": snapshot.verdict,
        "passed": snapshot.test_stats.unique.passed,
        "total": snapshot.test_stats.unique.executed,
        "flaky_count": snapshot.test_stats.flaky_count,
        "failed": snapshot.test_stats.unique.failed,
        "errors": snapshot.test_stats.unique.errors,
        "skipped": snapshot.test_stats.unique.skipped,
        "raw_executions": snapshot.test_stats.raw.executed,
    }
    manifest_path = session / "surface-artifacts.json"
    if not manifest_path.is_file():
        raise CollectionError("surface-artifacts.json is missing")
    manifest = _load_json(manifest_path)
    mismatches: list[SurfaceMismatch] = []
    for surface in ("markdown", "condensed", "cli", "web"):
        relative = manifest.get(surface)
        if not relative:
            mismatches.append(
                SurfaceMismatch(surface=surface, field="artifact", expected="present", actual=None)
            )
            continue
        artifact = session / str(relative)
        if not artifact.is_file():
            mismatches.append(
                SurfaceMismatch(
                    surface=surface,
                    field="artifact",
                    expected="present",
                    actual="missing",
                )
            )
            continue
        actual = _web_surface(artifact) if surface == "web" else _text_surface(artifact)
        for field, expected_value in expected.items():
            if (
                field in {"failed", "errors", "skipped", "raw_executions"}
                and actual.get(field) is None
            ):
                continue
            if actual.get(field) != expected_value:
                mismatches.append(
                    SurfaceMismatch(
                        surface=surface,
                        field=field,
                        expected=expected_value,
                        actual=actual.get(field),
                    )
                )
        if actual.get("ratio_over_100") is not None:
            mismatches.append(
                SurfaceMismatch(
                    surface=surface,
                    field="ratio",
                    expected="<=100%",
                    actual=actual["ratio_over_100"],
                )
            )
    return SurfaceCheckResult(
        ok=not mismatches,
        mismatches=tuple(mismatches),
        snapshot_path=str(snapshot_path),
    )


class CampaignStore:
    def __init__(self, campaign_path: str | Path) -> None:
        self.path = Path(campaign_path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._synthetic: dict[tuple[str, str, str], list[float]] = {}

    def _record_path(self, probe: str, stage: str) -> Path:
        return self.path / f"{probe}-{stage}.json"

    def append(self, probe: str, stage: str, record: CollectedRun) -> Path:
        path = self._record_path(probe, stage)
        loaded = (
            _load_json(path) if path.is_file() else {"probe": probe, "stage": stage, "runs": []}
        )
        if not isinstance(loaded, dict):
            raise CollectionError(f"campaign record must be an object: {path}")
        payload: dict[str, Any] = dict(loaded)
        loaded_runs = payload.get("runs") or []
        if not isinstance(loaded_runs, list) or not all(
            isinstance(item, dict) for item in loaded_runs
        ):
            raise CollectionError(f"campaign runs must be a list of objects: {path}")
        runs: list[dict[str, Any]] = [dict(item) for item in loaded_runs]
        if any(item.get("run_id") == record.run_id for item in runs):
            raise CollectionError(f"duplicate run id: {record.run_id}")
        if runs:
            first_pin = runs[0].get("pin")
            if first_pin != record.pin.model_dump(mode="json"):
                raise CollectionError("pin mismatch within probe/stage campaign")
        runs.append(record.model_dump(mode="json"))
        payload["runs"] = runs
        temporary = path.with_suffix(".tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        temporary.replace(path)
        return path

    def add_metric_runs(
        self,
        probe: str,
        stage: str,
        metric: str,
        values: Iterable[int | float],
    ) -> None:
        self._synthetic.setdefault((probe, stage, metric), []).extend(
            float(value) for value in values
        )

    def _all_metric_values(self) -> dict[tuple[str, str, str], list[float]]:
        values = {key: list(items) for key, items in self._synthetic.items()}
        for path in self.path.glob("*-*.json"):
            payload = _load_json(path)
            probe = str(payload.get("probe") or "")
            stage = str(payload.get("stage") or "")
            for run in payload.get("runs") or []:
                metrics = run.get("metrics") or {}
                for name, value in metrics.items():
                    if type(value) in {int, float}:
                        values.setdefault((probe, stage, name), []).append(float(value))
        return values

    def summarize(self) -> CampaignSummary:
        summaries = []
        for (probe, stage, name), values in sorted(self._all_metric_values().items()):
            if not values:
                continue
            summaries.append(
                MetricSummary(
                    probe=probe,
                    stage=stage,
                    name=name,
                    median=float(statistics.median(values)),
                    minimum=min(values),
                    maximum=max(values),
                    count=len(values),
                )
            )
        return CampaignSummary(metrics=tuple(summaries))

    def _records(self, stage: str) -> dict[str, list[CollectedRun]]:
        records: dict[str, list[CollectedRun]] = {}
        for path in self.path.glob("*-*.json"):
            payload = _load_json(path)
            if payload.get("stage") != stage:
                continue
            probe = str(payload.get("probe") or "")
            records[probe] = [
                CollectedRun.model_validate(item) for item in payload.get("runs") or []
            ]
        return records

    def evaluate(self, stage: str, *, min_repeats: int = 3) -> CampaignSummary:
        """Evaluate the six control-layer campaign bars from structured records."""
        required_probes = ("tvm", "bigtop", "paramiko", "cassandra-java-driver")
        selected_metrics = tuple(
            metric for metric in self.summarize().metrics if metric.stage == stage
        )
        current = self._records(stage)
        failures: list[str] = []
        match = re.fullmatch(r"ws(?P<number>[0-9]+)", stage.lower())
        stage_number = int(match.group("number")) if match else 0

        for probe in required_probes:
            runs = current.get(probe, [])
            if len(runs) < min_repeats:
                failures.append(f"{probe} has fewer than {min_repeats} repeats")
            for run in runs:
                metrics = run.metrics
                prefix = f"{probe}/{run.run_id}"
                if stage_number >= 1 and run.surface_ok is not True:
                    failures.append(f"bar1 surface agreement failed for {prefix}")
                if metrics.contradicted_gate_survival_count:
                    failures.append(f"bar2 contradicted gate survived for {prefix}")
                if metrics.entry_prerequisite_violations:
                    failures.append(f"bar2 entry prerequisite violated for {prefix}")
                if metrics.invalid_repair_count or metrics.repair_budget_violations:
                    failures.append(f"bar2 repair contract failed for {prefix}")
                if metrics.repair_count > 2:
                    failures.append(f"bar2 repair budget exceeded for {prefix}")
                if metrics.max_failure_gate_interactions > 2:
                    failures.append(f"bar2 determined failure closed too late for {prefix}")
                if metrics.diversity_break_count:
                    failures.append(f"bar3 diversity caused a hard break for {prefix}")
                if metrics.second_occurrence_miss_count:
                    failures.append(
                        f"bar3 second identical failure was not intercepted for {prefix}"
                    )
                if stage_number >= 4:
                    if metrics.evidence_contract_violations:
                        failures.append(f"bar4 missing post-step evidence for {prefix}")
                    if (
                        metrics.duplicate_envelope_count
                        or metrics.duplicate_tool_result_count
                        or metrics.unmatched_envelope_count
                        or metrics.envelope_drift_count
                        or metrics.orphan_tool_result_count
                    ):
                        failures.append(f"bar4 envelope contract failed for {prefix}")
                if metrics.raw_executions < metrics.unique_total:
                    failures.append(f"bar5 raw execution total is below unique total for {prefix}")
                if (
                    any("retry" in conflict.lower() for conflict in metrics.conflicts)
                    and not metrics.flaky_count
                ):
                    failures.append(f"bar5 retry flakiness is hidden for {prefix}")
                if probe == "cassandra-java-driver":
                    if metrics.verdict != "success":
                        failures.append(f"bar6 Cassandra verdict is not success for {prefix}")
                    if metrics.compiled_classes != 8916:
                        failures.append(
                            f"bar6 Cassandra must report exactly 8,916 compiled classes for {prefix}"
                        )
                    if not 4800 <= metrics.unique_total <= 5100:
                        failures.append(
                            f"bar6 Cassandra unique tests are outside 4,800..5,100 for {prefix}"
                        )

        if stage_number >= 4:
            baseline = self._records("baseline").get("paramiko", [])
            current_paramiko = current.get("paramiko", [])
            if len(baseline) < min_repeats:
                failures.append(f"bar4 Paramiko baseline has fewer than {min_repeats} repeats")
            elif current_paramiko:
                baseline_verdicts = {run.metrics.verdict for run in baseline}
                current_verdicts = {run.metrics.verdict for run in current_paramiko}
                if len(baseline_verdicts) != 1 or current_verdicts != baseline_verdicts:
                    failures.append("bar4 Paramiko verdict drifted from baseline")
                baseline_median = statistics.median(run.metrics.thought_calls for run in baseline)
                current_median = statistics.median(
                    run.metrics.thought_calls for run in current_paramiko
                )
                if current_median > baseline_median * 0.5:
                    failures.append(
                        "bar4 Paramiko thinking-call median is not at least 50% below baseline"
                    )

        return CampaignSummary(
            metrics=selected_metrics,
            failures=tuple(dict.fromkeys(failures)),
        )


def pin_panel(panel_path: str | Path, campaign_path: str | Path) -> Path:
    panel = _load_json(Path(panel_path))
    probes = panel.get("probes") or {}
    if not probes:
        raise CollectionError("panel defines no probes")
    locked: dict[str, Any] = {
        "schema_version": 1,
        "baseline_sag_sha": panel.get("baseline_sag_sha"),
        "probes": {},
    }
    for name, config in probes.items():
        url = config["url"]
        ref = config.get("ref", "HEAD")
        process = subprocess.run(
            ["git", "ls-remote", url, ref],
            check=False,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0 or not process.stdout.strip():
            raise CollectionError(f"cannot resolve {name} {ref}: {process.stderr.strip()}")
        sha = process.stdout.split()[0]
        locked["probes"][name] = {**config, "locked_sha": sha}
    target = Path(campaign_path) / "panel-lock.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(locked), encoding="utf-8")
    return target


def _git_sha(root: Path) -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise CollectionError(f"cannot resolve SAG SHA: {process.stderr.strip()}")
    return process.stdout.strip()


def _assert_clean_tracked_tree(root: Path) -> None:
    process = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise CollectionError(f"cannot inspect SAG worktree: {process.stderr.strip()}")
    if process.stdout.strip():
        raise CollectionError("SAG worktree has tracked changes; its git SHA cannot pin this run")


def _assert_fresh_container_name(run_name: str) -> None:
    container_name = f"sag-{run_name}"
    process = subprocess.run(
        [
            "docker",
            "container",
            "ls",
            "-a",
            "--filter",
            f"name=^{container_name}$",
            "--format",
            "{{.Names}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise CollectionError(f"cannot verify fresh container name: {process.stderr.strip()}")
    if container_name in process.stdout.splitlines():
        raise CollectionError(f"container already exists and will not be reused: {container_name}")


def build_legacy_run_pin(
    *,
    target_repo_sha: str,
    container_image_digest: str,
    sag_git_sha: str,
    runtime_config: Mapping[str, Any],
    prompt_bundle_sha256: str,
    random_seed: int | None,
    dependency_cache_state: str,
    host_arch: str,
) -> RunPin:
    """Build a schema-0 baseline pin solely from externally observed facts."""
    thinking_model = str(runtime_config.get("thinking_model") or "").strip()
    action_model = str(runtime_config.get("action_model") or "").strip()
    if not thinking_model or not action_model:
        raise CollectionError("legacy runtime config did not expose both model names")
    config_without_models = {
        key: value
        for key, value in runtime_config.items()
        if key not in {"thinking_model", "action_model"}
    }
    return RunPin(
        target_repo_sha=target_repo_sha,
        container_image_digest=container_image_digest,
        sag_git_sha=sag_git_sha,
        thinking_model=thinking_model,
        action_model=action_model,
        sanitized_config=sanitize_config(config_without_models),
        prompt_bundle_sha256=prompt_bundle_sha256,
        feature_flags={
            "control_events": False,
            "reasoning_scheduler": False,
            "phase_machine": False,
        },
        random_seed_or_null=random_seed,
        dependency_cache_state=dependency_cache_state,
        host_arch=host_arch,
    )


_SAFE_RUNTIME_CONFIG_FIELDS = (
    "thinking_model",
    "thinking_provider",
    "thinking_temperature",
    "thinking_max_tokens",
    "reasoning_effort",
    "thinking_budget_tokens",
    "verbosity",
    "gpt5_reasoning_effort",
    "action_model",
    "action_provider",
    "action_temperature",
    "action_max_tokens",
    "docker_base_image",
    "workspace_path",
    "max_iterations",
    "context_switch_threshold",
    "max_wall_clock_seconds",
    "test_pass_threshold",
    "build_coverage_threshold",
    "test_execution_threshold",
)


def _legacy_runtime_config(
    sag_root: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    fields = repr(_SAFE_RUNTIME_CONFIG_FIELDS)
    helper = (
        "import json; from sag.config.settings import Config; "
        "config = Config.from_env(); "
        f"fields = {fields}; "
        "print(json.dumps({name: getattr(config, name, None) for name in fields}, "
        "sort_keys=True))"
    )
    process = subprocess.run(
        ["uv", "--directory", str(sag_root), "run", "python", "-c", helper],
        check=False,
        capture_output=True,
        text=True,
        env=dict(environment),
    )
    if process.returncode != 0:
        raise CollectionError(f"cannot read baseline runtime config: {process.stderr.strip()}")
    for line in reversed(process.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise CollectionError("baseline runtime config helper returned no structured payload")


def _prompt_source_digest(sag_root: Path, sag_sha: str) -> str:
    paths = (
        "src/sag/config/prompts",
        "src/sag/config/prompt_loader.py",
        "src/sag/agent/react_prompt_builder.py",
        "src/sag/agent/react_engine.py",
    )
    process = subprocess.run(
        ["git", "ls-tree", "-r", sag_sha, "--", *paths],
        cwd=sag_root,
        check=False,
        capture_output=True,
        text=True,
    )
    entries = process.stdout.splitlines()
    if process.returncode != 0 or not entries:
        raise CollectionError(f"cannot pin baseline prompt sources: {process.stderr.strip()}")
    return canonical_sha256(
        {
            "basis": "git-tree prompt sources schema0",
            "entries": entries,
        }
    )


def _container_image_digest(run_name: str) -> str:
    process = subprocess.run(
        ["docker", "inspect", "--format={{.Image}}", f"sag-{run_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise CollectionError(f"cannot inspect baseline container image: {process.stderr.strip()}")
    return process.stdout.strip()


def _write_external_baseline_pin(
    session: Path,
    *,
    sag_root: Path,
    sag_sha: str,
    target_repo_sha: str,
    run_name: str,
    random_seed: int,
    dependency_cache_state: str,
    environment: Mapping[str, str],
) -> Path:
    setup_dir = session / ".setup_agent"
    target = setup_dir / "run-pin.json" if setup_dir.is_dir() else session / "run-pin.json"
    pin = build_legacy_run_pin(
        target_repo_sha=target_repo_sha,
        container_image_digest=_container_image_digest(run_name),
        sag_git_sha=sag_sha,
        runtime_config=_legacy_runtime_config(sag_root, environment),
        prompt_bundle_sha256=_prompt_source_digest(sag_root, sag_sha),
        random_seed=random_seed,
        dependency_cache_state=dependency_cache_state,
        host_arch=platform.machine() or "unknown",
    )
    return ControlEventSink.write_run_pin(target, pin)


def _probe_environment(env_file: str | None) -> dict[str, str]:
    environment = dict(os.environ)
    if not env_file:
        return environment
    from dotenv import dotenv_values

    path = Path(env_file)
    if not path.is_file():
        raise CollectionError(f"probe env file is missing: {path}")
    for key, value in dotenv_values(path).items():
        if value is not None:
            environment.setdefault(key, value)
    return environment


def run_probe(args: argparse.Namespace) -> CollectedRun:
    campaign = Path(args.campaign)
    lock_path = campaign / "panel-lock.json"
    lock = _load_json(lock_path)
    try:
        probe = lock["probes"][args.probe]
    except KeyError as exc:
        raise CollectionError(f"probe {args.probe!r} is absent from panel lock") from exc
    sag_root = Path(args.sag_root).resolve()
    _assert_clean_tracked_tree(sag_root)
    sag_sha = _git_sha(sag_root)
    baseline_sha = str(lock.get("baseline_sag_sha") or "").strip().lower()
    if args.stage == "baseline" and (
        not baseline_sha or not sag_sha.lower().startswith(baseline_sha)
    ):
        raise CollectionError(
            f"baseline worktree is {sag_sha}, expected {baseline_sha or 'a pinned SHA'}"
        )
    run_name = f"ab-{args.probe}-{args.stage}-r{args.repeat}"
    _assert_fresh_container_name(run_name)
    logs_root = sag_root / "logs"
    before = {path.resolve() for path in logs_root.glob("session_*") if path.is_dir()}
    cli_path = campaign / f"{run_name}-cli.log"
    command = [
        "uv",
        "--directory",
        str(sag_root),
        "run",
        "sag",
        "project",
        probe["url"],
        "--ref",
        probe["locked_sha"],
        "--name",
        run_name,
        "--record",
    ]
    run_environment = _probe_environment(args.env_file)
    run_environment["SAG_RANDOM_SEED"] = str(args.seed)
    run_environment["SAG_DEPENDENCY_CACHE_STATE"] = args.dependency_cache
    process = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=run_environment,
    )
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text(process.stdout + process.stderr, encoding="utf-8")
    after = {path.resolve() for path in logs_root.glob("session_*") if path.is_dir()}
    created = sorted(after - before)
    if len(created) != 1:
        raise CollectionError(
            f"probe must create exactly one session directory, found {len(created)}"
        )
    session = created[0]
    pin_path = _first_existing(session / ".setup_agent" / "run-pin.json", session / "run-pin.json")
    if pin_path is None:
        if args.stage != "baseline":
            raise CollectionError("live probe did not write run-pin.json")
        pin_path = _write_external_baseline_pin(
            session,
            sag_root=sag_root,
            sag_sha=sag_sha,
            target_repo_sha=probe["locked_sha"],
            run_name=run_name,
            random_seed=args.seed,
            dependency_cache_state=args.dependency_cache,
            environment=run_environment,
        )
    if args.stage == "baseline":
        pin = RunPin.model_validate(_load_json(pin_path))
    else:
        pin = _validate_current_run_pin(
            session,
            target_repo_sha=probe["locked_sha"],
            sag_git_sha=sag_sha,
            random_seed=args.seed,
            dependency_cache_state=args.dependency_cache,
            host_arch=platform.machine() or "unknown",
        )
    observed_image = RunPin.model_validate(
        {**pin.model_dump(mode="json"), "container_image_digest": _container_image_digest(run_name)}
    ).container_image_digest
    if observed_image != pin.container_image_digest:
        raise CollectionError("run pin container image digest disagrees with the created container")
    record = ABCollector().collect(session)
    if args.stage != "baseline":
        prepare_surface_artifacts(session, cli_path)
        surface = check_surfaces(session)
        record = record.model_copy(
            update={
                "surface_ok": surface.ok,
                "surface_mismatches": tuple(
                    f"{item.surface}:{item.field}" for item in surface.mismatches
                ),
            }
        )
    CampaignStore(campaign).append(args.probe, args.stage, record)
    if process.returncode != 0:
        print(
            f"probe process exited {process.returncode}; structured evidence was retained",
            file=sys.stderr,
        )
    return record


def _cmd_collect(args: argparse.Namespace) -> int:
    record = ABCollector().collect(args.session)
    text = canonical_json(record)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def _cmd_check_surfaces(args: argparse.Namespace) -> int:
    result = check_surfaces(args.session)
    text = canonical_json(result)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if result.ok else 1


def _cmd_summarize(args: argparse.Namespace) -> int:
    final = CampaignStore(args.campaign).evaluate(
        args.stage,
        min_repeats=args.min_repeats,
    )
    output = Path(args.campaign) / f"summary-{args.stage}.json"
    output.write_text(canonical_json(final), encoding="utf-8")
    print(canonical_json(final))
    return 1 if args.enforce and final.failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    pin = subparsers.add_parser("pin-panel", help="resolve and freeze target repository SHAs")
    pin.add_argument("--panel", required=True)
    pin.add_argument("--campaign", required=True)

    run = subparsers.add_parser("run-probe", help="run one pinned, isolated live probe")
    run.add_argument("--campaign", required=True)
    run.add_argument("--probe", required=True)
    run.add_argument("--stage", required=True)
    run.add_argument("--repeat", required=True, type=int)
    run.add_argument("--sag-root", required=True)
    run.add_argument("--dependency-cache", required=True, choices=("cold", "warm"))
    run.add_argument("--seed", required=True, type=int)
    run.add_argument("--env-file")

    collect = subparsers.add_parser("collect", help="collect structured metrics from one session")
    collect.add_argument("--session", required=True)
    collect.add_argument("--output")

    surfaces = subparsers.add_parser(
        "check-surfaces", help="compare rendered surfaces literally with verdict.json"
    )
    surfaces.add_argument("--session", required=True)
    surfaces.add_argument("--output")

    summarize = subparsers.add_parser("summarize", help="report median [min-max] campaign metrics")
    summarize.add_argument("--campaign", required=True)
    summarize.add_argument("--stage", required=True)
    summarize.add_argument("--min-repeats", type=int, default=3)
    summarize.add_argument("--enforce", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "pin-panel":
            print(pin_panel(args.panel, args.campaign))
            return 0
        if args.command == "run-probe":
            print(canonical_json(run_probe(args)))
            return 0
        if args.command == "collect":
            return _cmd_collect(args)
        if args.command == "check-surfaces":
            return _cmd_check_surfaces(args)
        if args.command == "summarize":
            return _cmd_summarize(args)
    except CollectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
