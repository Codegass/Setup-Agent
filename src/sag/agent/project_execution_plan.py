"""Model-authored project execution plans and their engine-owned seal.

The analyze model authors the project-specific build and test strategy.  The
harness contributes a bounded document inventory, verifies exact document-map
bindings, records discovery gaps, and seals the accepted plan for later phases.
Inventory heuristics and deterministic claims are guidance; neither one is an
allowlist for what the model may review.

The authored plan and its sealed artifact are deliberately separate:

* :class:`ProjectExecutionPlan` contains only the model's reviewed evidence and
  proposed execution strategy;
* :class:`SealedProjectExecutionPlan` binds that authored payload to the analyze
  attempt, accepted phase claim, document-map snapshot, and inventory gaps;
* only the engine should call :func:`seal_and_write_project_execution_plan`.

The container mirror is one canonical JSON object at
``/workspace/.setup_agent/project_execution_plan.json``.  Reads use the exact
byte path and revalidate both the authored-plan digest and the artifact digest.
"""

from __future__ import annotations

import math
import posixpath
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sag.agent.control_events import canonical_json, canonical_sha256
from sag.agent.document_map import document_map_fingerprint
from sag.agent.document_map import entry_id as document_entry_id
from sag.runtime.container_io import ContainerFileReadError, read_container_text
from sag.utils.container_io import (
    WRITE_INVALID_ARGUMENTS,
    ContainerWriteResult,
    write_container_text_atomic,
)

PROJECT_EXECUTION_PLAN_SCHEMA_VERSION = 1
PROJECT_EXECUTION_PLAN_PATH = "/workspace/.setup_agent/project_execution_plan.json"

MAX_AUTHORED_PLAN_BYTES = 16 * 1024
MAX_SEALED_PLAN_BYTES = 64 * 1024
MAX_SYSTEM_PROMPT_CHARS = 24 * 1024
MAX_INVENTORY_PROMPT_CHARS = 16 * 1024

MAX_DOCUMENTS_REVIEWED = 24
MAX_EXECUTION_STEPS = 16
MAX_SUCCESS_CRITERIA = 16
MAX_CONTEXT_ITEMS = 16
MAX_EVIDENCE_REFS = 12
MAX_INVENTORY_WARNINGS = 32
MAX_INVENTORY_RECORDS = 5_000
MAX_PARAMS_BYTES = 4_096
MAX_PARAM_DEPTH = 4
MAX_PARAM_ITEMS = 32
MAX_PARAM_STRING_CHARS = 2_048
MAX_VALIDATION_ERROR_CHARS = 4_096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_REF_RE = re.compile(r"^output_[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")


class ProjectExecutionPlanValidationError(ValueError):
    """The authored plan or its evidence binding is not valid."""


class ProjectExecutionPlanPersistenceError(RuntimeError):
    """The engine could not persist a sealed plan artifact."""


class ProjectExecutionPlanReadError(RuntimeError):
    """The sealed plan artifact was unreadable, malformed, or tampered."""


class _FrozenPlanModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


def _normalize_sha256(value: Any, *, field_name: str) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return digest


def _unique_nonempty(values: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    normalized = tuple(str(value or "").strip() for value in values)
    if any(not value for value in normalized):
        raise ValueError(f"{field_name} cannot contain empty values")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} cannot contain duplicate values")
    return normalized


def _normalize_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_PARAM_DEPTH:
        raise ValueError("execution params exceed the nesting bound")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("execution params require finite numbers")
        return value
    if isinstance(value, str):
        if len(value) > MAX_PARAM_STRING_CHARS:
            raise ValueError("execution param string exceeds the character bound")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_PARAM_ITEMS:
            raise ValueError("execution params exceed the mapping item bound")
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("execution param keys must be strings")
            key = raw_key.strip()
            if not key or len(key) > 128:
                raise ValueError("execution param key is empty or too long")
            if key in normalized:
                raise ValueError("execution params contain duplicate normalized keys")
            normalized[key] = _normalize_json_value(child, depth=depth + 1)
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_PARAM_ITEMS:
            raise ValueError("execution params exceed the sequence item bound")
        return [_normalize_json_value(child, depth=depth + 1) for child in value]
    raise ValueError(f"execution params contain unsupported value {type(value).__name__}")


class ReviewedDocument(_FrozenPlanModel):
    """One source the analyze model deliberately reviewed."""

    path: str = Field(min_length=1, max_length=2_048)
    reason: str = Field(min_length=1, max_length=1_000)
    evidence_refs: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_REFS,
    )
    # Optional model hints only.  The Harness derives authoritative values for
    # document-map-backed paths and drops them for map-external paths before
    # sealing, so the model never has to copy inventory metadata exactly.
    entry_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_hash: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("document path contains NUL")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _valid_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _unique_nonempty(value, field_name="evidence_refs")
        if any(len(ref) > 512 for ref in normalized):
            raise ValueError("evidence reference exceeds the character bound")
        return normalized


class ExecutionStep(_FrozenPlanModel):
    """One planned build or test action, still expressed as public tool params."""

    tool: str = Field(min_length=1, max_length=64)
    params: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=1_000)
    evidence_refs: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_EVIDENCE_REFS,
    )

    @field_validator("params", mode="before")
    @classmethod
    def _bounded_params(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("execution params must be a mapping")
        normalized = _normalize_json_value(value)
        if not isinstance(normalized, dict):  # narrowed from the recursive JSON type
            raise ValueError("execution params must normalize to a mapping")
        if len(canonical_json(normalized).encode("utf-8")) > MAX_PARAMS_BYTES:
            raise ValueError("execution params exceed the canonical byte bound")
        return cast(dict[str, Any], normalized)

    @field_validator("evidence_refs")
    @classmethod
    def _valid_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _unique_nonempty(value, field_name="evidence_refs")
        if any(len(ref) > 512 for ref in normalized):
            raise ValueError("evidence reference exceeds the character bound")
        return normalized


class TestDisposition(_FrozenPlanModel):
    """The model's evidence-backed decision about unattended test execution."""

    status: Literal["planned", "blocked"]
    reason: str = Field(min_length=1, max_length=1_000)
    execution_mechanism: str = Field(min_length=1, max_length=1_000)
    verdict_scope: Literal[
        "product_test_cases",
        "test_metadata",
        "quality_only",
        "benchmark_or_manual",
        "unknown",
    ]
    readiness: Literal["ready", "blocked", "unknown"]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_REFS)
    definition_evidence_refs: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_REFS,
    )

    @field_validator("evidence_refs", "definition_evidence_refs")
    @classmethod
    def _valid_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _unique_nonempty(value, field_name="evidence_refs")
        if any(len(ref) > 512 for ref in normalized):
            raise ValueError("evidence reference exceeds the character bound")
        return normalized

    @model_validator(mode="after")
    def _planned_entry_is_ready(self) -> "TestDisposition":
        if self.status == "planned" and self.readiness != "ready":
            raise ValueError("planned test disposition requires readiness=ready")
        if self.status == "planned" and self.verdict_scope != "product_test_cases":
            raise ValueError("planned test disposition requires verdict_scope=product_test_cases")
        return self


class ProjectExecutionPlan(_FrozenPlanModel):
    """The bounded strategy authored by the analyze model."""

    summary: str = Field(min_length=1, max_length=2_000)
    documents_reviewed: tuple[ReviewedDocument, ...] = Field(
        min_length=1,
        max_length=MAX_DOCUMENTS_REVIEWED,
    )
    build_steps: tuple[ExecutionStep, ...] = Field(
        min_length=1,
        max_length=MAX_EXECUTION_STEPS,
    )
    build_success_criteria: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_SUCCESS_CRITERIA,
    )
    # Added after schema-v1 plans had already been persisted.  New Analyze
    # submissions must provide it at the PhaseTool boundary, while ``None``
    # remains readable so archived plan bytes and their digests stay valid.
    test_disposition: TestDisposition | None = None
    test_steps: tuple[ExecutionStep, ...] = Field(
        default=(),
        max_length=MAX_EXECUTION_STEPS,
    )
    test_success_criteria: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_SUCCESS_CRITERIA,
    )
    environment_constraints: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_CONTEXT_ITEMS,
    )
    risks: tuple[str, ...] = Field(default=(), max_length=MAX_CONTEXT_ITEMS)
    unresolved_questions: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_CONTEXT_ITEMS,
    )

    @field_validator(
        "build_success_criteria",
        "test_success_criteria",
        "environment_constraints",
        "risks",
        "unresolved_questions",
    )
    @classmethod
    def _bounded_text_lists(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        normalized = _unique_nonempty(value, field_name=info.field_name)
        if any(len(item) > 1_000 for item in normalized):
            raise ValueError(f"{info.field_name} item exceeds the character bound")
        return normalized

    @model_validator(mode="after")
    def _unique_documents_and_bounded_payload(self) -> "ProjectExecutionPlan":
        paths = [document.path for document in self.documents_reviewed]
        if len(set(paths)) != len(paths):
            raise ValueError("documents_reviewed contains duplicate paths")
        if self.test_disposition is not None:
            if self.test_disposition.status == "planned" and not self.test_steps:
                raise ValueError("planned test disposition requires at least one test step")
            if self.test_disposition.status == "blocked" and self.test_steps:
                raise ValueError("blocked test disposition requires test_steps to be empty")
            document_refs = {
                ref for document in self.documents_reviewed for ref in document.evidence_refs
            }
            if not set(self.test_disposition.evidence_refs).issubset(document_refs):
                raise ValueError(
                    "test disposition evidence must cite a reviewed-document output ref"
                )
            if not set(self.test_disposition.definition_evidence_refs).issubset(document_refs):
                raise ValueError("test entry definition must cite a reviewed-document output ref")
        size = len(canonical_json(self.model_dump(mode="json")).encode("utf-8"))
        if size > MAX_AUTHORED_PLAN_BYTES:
            raise ValueError("authored project execution plan exceeds the canonical byte bound")
        return self

    def to_system_prompt(self) -> str:
        """Render the complete authored plan within a fixed prompt bound."""

        body = canonical_json(self.model_dump(mode="json"))
        prompt = (
            "=== PROJECT EXECUTION PLAN ===\n"
            "This plan was authored during project analysis. Build and test "
            "actions should follow it; unresolved items remain explicit.\n"
            f"{body}"
        )
        if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
            # The model-level canonical byte bound should make this impossible.
            raise ProjectExecutionPlanValidationError("system prompt exceeds its fixed bound")
        return prompt


class InventoryCoverage(_FrozenPlanModel):
    """Exact counts; no percentage implies that every file needed review."""

    indexed_documents: int = Field(ge=0, le=MAX_INVENTORY_RECORDS)
    reviewed_indexed_documents: int = Field(ge=0, le=MAX_INVENTORY_RECORDS)
    unreviewed_indexed_documents: int = Field(ge=0, le=MAX_INVENTORY_RECORDS)
    external_documents_reviewed: int = Field(ge=0, le=MAX_DOCUMENTS_REVIEWED)
    partial_inventory_records: int = Field(ge=0, le=MAX_INVENTORY_RECORDS)
    all_indexed_documents_reviewed: bool

    @model_validator(mode="after")
    def _counts_reconcile(self) -> "InventoryCoverage":
        if (
            self.reviewed_indexed_documents + self.unreviewed_indexed_documents
            != self.indexed_documents
        ):
            raise ValueError("inventory coverage counts do not reconcile")
        if self.all_indexed_documents_reviewed != (self.unreviewed_indexed_documents == 0):
            raise ValueError("inventory completion flag does not match its counts")
        return self


def _authored_plan_payload(plan: ProjectExecutionPlan) -> dict[str, Any]:
    """Return the canonical payload without rewriting archived schema-v1 plans."""

    payload = plan.model_dump(mode="json")
    if plan.test_disposition is None:
        payload.pop("test_disposition", None)
    return payload


def _sealed_plan_payload(
    artifact: Any,
    *,
    include_artifact_sha256: bool,
) -> dict[str, Any]:
    excluded = set() if include_artifact_sha256 else {"artifact_sha256"}
    payload = artifact.model_dump(mode="json", exclude=excluded)
    plan = payload.get("plan")
    if isinstance(plan, dict) and plan.get("test_disposition") is None:
        plan.pop("test_disposition", None)
    return payload


class SealedProjectExecutionPlan(_FrozenPlanModel):
    """Engine-owned binding of an authored plan to one analyze attempt."""

    schema_version: Literal[1] = 1
    source_attempt_id: str = Field(min_length=1, max_length=128)
    claim_sha256: str
    document_map_fingerprint: str | None = None
    authored_plan_sha256: str
    inventory_coverage: InventoryCoverage
    inventory_warnings: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_INVENTORY_WARNINGS,
    )
    plan: ProjectExecutionPlan
    artifact_sha256: str

    @field_validator("claim_sha256", "authored_plan_sha256", "artifact_sha256")
    @classmethod
    def _valid_sha(cls, value: str, info: Any) -> str:
        return _normalize_sha256(value, field_name=info.field_name)

    @field_validator("document_map_fingerprint")
    @classmethod
    def _valid_map_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_sha256(value, field_name="document_map_fingerprint")

    @field_validator("inventory_warnings")
    @classmethod
    def _valid_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _unique_nonempty(value, field_name="inventory_warnings")
        if any(len(item) > 512 for item in normalized):
            raise ValueError("inventory warning exceeds the character bound")
        return normalized

    @model_validator(mode="after")
    def _valid_seals(self) -> "SealedProjectExecutionPlan":
        if canonical_authored_plan_sha256(self.plan) != self.authored_plan_sha256:
            raise ValueError("authored project execution plan hash mismatch")
        payload = _sealed_plan_payload(self, include_artifact_sha256=False)
        if canonical_sha256(payload) != self.artifact_sha256:
            raise ValueError("sealed project execution plan artifact hash mismatch")
        coverage = self.inventory_coverage
        has_gap = bool(
            coverage.unreviewed_indexed_documents
            or coverage.external_documents_reviewed
            or coverage.partial_inventory_records
        )
        if has_gap and not self.inventory_warnings:
            raise ValueError("inventory gaps require a visible warning")
        return self

    def to_system_prompt(self) -> str:
        """Render seal identity, exact inventory counts, warnings, and the plan."""

        coverage = self.inventory_coverage
        lines = [
            "=== SEALED PROJECT EXECUTION PLAN ===",
            f"source_attempt_id: {self.source_attempt_id}",
            f"claim_sha256: {self.claim_sha256}",
            f"authored_plan_sha256: {self.authored_plan_sha256}",
            "inventory_coverage: "
            f"reviewed={coverage.reviewed_indexed_documents}/"
            f"{coverage.indexed_documents}, "
            f"external={coverage.external_documents_reviewed}, "
            f"partial={coverage.partial_inventory_records}",
        ]
        if self.inventory_warnings:
            lines.append("inventory_warnings:")
            lines.extend(f"- {warning}" for warning in self.inventory_warnings)
        lines.append(self.plan.to_system_prompt())
        prompt = "\n".join(lines)
        if len(prompt) <= MAX_SYSTEM_PROMPT_CHARS:
            return prompt

        # Core plan content is never truncated.  Only diagnostic warnings are
        # compacted when they would crowd the fixed system-prompt envelope.
        compact = [line for line in lines if line != "inventory_warnings:"]
        compact = [line for line in compact if not line.startswith("- ")]
        compact.insert(5, f"inventory_warnings: {len(self.inventory_warnings)} recorded")
        prompt = "\n".join(compact)
        if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
            raise ProjectExecutionPlanValidationError("sealed system prompt exceeds its bound")
        return prompt


def validate_authored_plan(
    candidate: Any,
    *,
    require_test_disposition: bool = False,
) -> ProjectExecutionPlan:
    """Validate and normalize a model-authored candidate into the strict model."""

    try:
        if isinstance(candidate, ProjectExecutionPlan):
            plan = ProjectExecutionPlan.model_validate(candidate.model_dump(mode="python"))
        elif isinstance(candidate, str):
            if len(candidate.encode("utf-8")) > MAX_AUTHORED_PLAN_BYTES:
                raise ValueError("authored plan JSON exceeds the raw byte bound")
            plan = ProjectExecutionPlan.model_validate_json(candidate)
        elif isinstance(candidate, Mapping):
            plan = ProjectExecutionPlan.model_validate(dict(candidate))
        else:
            raise TypeError("authored plan candidate must be a mapping or JSON object")
        if require_test_disposition and plan.test_disposition is None:
            raise ValueError("test_disposition is required for a new Analyze plan")
        if require_test_disposition:
            # New submissions must state the executable interpretation. Keep
            # this out of the model validators so archived payloads and seals
            # remain byte-for-byte readable.
            from sag.tools.build.backends import source_command_tokens

            errors = []
            first_errors = {}
            # Check both lanes before reporting: a build error must not hide
            # an independently invalid test action until the next model turn.
            groups = (("build_steps", plan.build_steps), ("test_steps", plan.test_steps))
            for index in range(max(len(steps) for _, steps in groups)):
                for label, steps in groups:
                    if index >= len(steps):
                        continue
                    step = steps[index]
                    try:
                        cwd = step.params.get("working_directory")
                        if (
                            not isinstance(cwd, str)
                            or not (cwd == "/workspace" or cwd.startswith("/workspace/"))
                            or posixpath.normpath(cwd) != cwd
                        ):
                            raise ValueError(
                                "new execution steps require an explicit canonical workspace working_directory"
                            )
                        if step.tool != "build" or step.params.get("action") in {"deps", "native"}:
                            continue
                        system = step.params.get("system")
                        source = step.params.get("source_command")
                        if system not in {"maven", "gradle", "python"} or not source:
                            raise ValueError(
                                "new build execution steps require explicit system and source_command"
                            )
                        source_command_tokens(
                            source, system, step.params.get("action"), step.params.get("args")
                        )
                    except (TypeError, ValueError) as exc:
                        error = f"{label}[{index}]: {exc}"
                        first_errors.setdefault(label, error)
                        errors.append(error)
            if errors:
                # Four located errors, including the first from each lane,
                # stay within the existing diagnostic bound even for long argv.
                prioritized = list(first_errors.values())
                prioritized += [error for error in errors if error not in prioritized]
                detail_bound = (MAX_VALIDATION_ERROR_CHARS - 128) // 4
                lines = [
                    (
                        error
                        if len(error) <= detail_bound
                        else error[: detail_bound - 23] + " ... (detail truncated)"
                    )
                    for error in prioritized[:4]
                ]
                if len(errors) > 4:
                    lines.append(f"{len(errors) - 4} additional step errors omitted")
                raise ValueError("\n".join(lines))
            disposition = plan.test_disposition
            if disposition is not None and disposition.execution_mechanism.lower().startswith(
                "make "
            ):
                if any(
                    step.tool == "build" and step.params.get("system") != "python"
                    for step in plan.test_steps
                ):
                    raise ValueError(
                        "Make test mechanism cannot be encoded as a different build executor; review its recipe or mark it blocked"
                    )
        # The after-validator checks the canonical bound; this copy also makes
        # the return value independent from caller-owned mutable structures.
        return plan.model_copy(deep=True)
    except (TypeError, ValueError, ValidationError) as exc:
        if isinstance(exc, ProjectExecutionPlanValidationError):
            raise
        raise ProjectExecutionPlanValidationError(str(exc)) from exc


def canonical_authored_plan_sha256(candidate: Any) -> str:
    """Canonical digest of the normalized plan that will be sealed."""

    plan = validate_authored_plan(candidate)
    return canonical_sha256(_authored_plan_payload(plan))


def _entry_body(entry: Any) -> dict[str, Any]:
    if isinstance(entry, Mapping):
        return dict(entry)
    payload = getattr(entry, "payload", None)
    if callable(payload):
        value = payload()
        if isinstance(value, Mapping):
            return dict(value)
    raise ProjectExecutionPlanValidationError("document map entry is not a mapping")


def _is_direct_document_evidence(ref: str) -> bool:
    value = str(ref or "").strip()
    return _OUTPUT_REF_RE.fullmatch(value) is not None


def _record_member(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _normalized_document_path(value: Any) -> str:
    path = str(value or "").strip()
    for prefix in ("file:", "path:"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    return posixpath.normpath(path) if path else ""


def _observation_targets_document(observation: Any, document_path: str) -> bool:
    """Whether one tool call explicitly named this document path.

    Tool names and actions are intentionally irrelevant here.  The model may
    use any current or future read/search facade; the harness only checks the
    structural link between the cited durable output and the claimed path.
    """

    params = _record_member(observation, "params", {})
    if not isinstance(params, Mapping):
        return False
    expected = _normalized_document_path(document_path)
    if not expected:
        return False

    pending: list[Any] = [params]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            pending.extend(value)
        elif isinstance(value, str) and _normalized_document_path(value) == expected:
            return True
    return False


def _bounded_ref_list(refs: Sequence[str]) -> str:
    unique = tuple(dict.fromkeys(str(ref) for ref in refs if str(ref).strip()))
    shown = unique[:MAX_EVIDENCE_REFS]
    rendered = ", ".join(shown) if shown else "none"
    omitted = len(unique) - len(shown)
    if omitted:
        rendered += f" (+{omitted} more)"
    return rendered


def _bounded_validation_error(message: str) -> ProjectExecutionPlanValidationError:
    return ProjectExecutionPlanValidationError(str(message)[:MAX_VALIDATION_ERROR_CHARS])


def validate_reviewed_document_evidence(
    candidate: Any,
    *,
    observations: Sequence[Any],
    output_reader: Callable[[str], str | None],
    source_attempt_id: str,
) -> ProjectExecutionPlan:
    """Require each claimed document review to resolve to this Analyze attempt.

    A document-map entry/hash proves inventory identity and currentness; it does
    not prove that the model actually read the source.  This gate therefore
    requires one durable ``output_*`` reference per reviewed document.  The
    referenced output must be readable, belong to a successful tool
    observation in the current Analyze attempt, and have the claimed path in
    its call parameters.  Document-map hashes are inventory bindings, not
    substitutes for a path-bound model read.

    The check is deliberately independent from document-map membership.  A
    model may discover and read a source outside the bounded map through the
    same ordinary tool path.
    """

    plan = validate_authored_plan(candidate)
    attempt_id = str(source_attempt_id or "").strip()
    if not attempt_id:
        raise ProjectExecutionPlanValidationError(
            "document review evidence requires the current Analyze attempt id"
        )
    if not callable(output_reader):
        raise ProjectExecutionPlanValidationError(
            "document review evidence output reader is unavailable"
        )
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise ProjectExecutionPlanValidationError("tool observations must be a sequence")

    current_observations: list[tuple[str, Any]] = []
    for observation in observations:
        if str(_record_member(observation, "source_phase", "") or "").strip() != "analyze":
            continue
        if str(_record_member(observation, "source_attempt_id", "") or "").strip() != attempt_id:
            continue
        result = _record_member(observation, "result")
        if result is None or not bool(_record_member(result, "succeeded", False)):
            continue
        facts = _record_member(result, "facts", {})
        if isinstance(facts, Mapping) and facts.get("matched") is False:
            continue
        ref = str(_record_member(result, "output_ref", "") or "").strip()
        if _OUTPUT_REF_RE.fullmatch(ref) is not None:
            current_observations.append((ref, observation))

    output_cache: dict[str, str | None] = {}
    for reviewed in plan.documents_reviewed:
        valid_refs: list[str] = []
        for ref, observation in current_observations:
            if not _observation_targets_document(observation, reviewed.path):
                continue
            if ref not in output_cache:
                try:
                    resolved = output_reader(ref)
                except Exception:
                    resolved = None
                output_cache[ref] = resolved if isinstance(resolved, str) else None
            output = output_cache[ref]
            if output is None:
                continue
            valid_refs.append(ref)
        valid_refs = list(dict.fromkeys(valid_refs))
        if not set(reviewed.evidence_refs).intersection(valid_refs):
            raise _bounded_validation_error(
                "Document review evidence is not bound to this path in the current "
                f"Analyze attempt: {reviewed.path}. "
                f"Exact valid readable output refs already observed for this path: "
                f"{_bounded_ref_list(valid_refs)}. "
                f"Refs supplied in the plan: {_bounded_ref_list(reviewed.evidence_refs)}."
            )
    return plan


def _document_inventory(
    document_map: Mapping[str, Any] | None,
) -> tuple[dict[str, tuple[str, str]], str | None, list[dict[str, str]]]:
    if document_map is None:
        return {}, None, []
    if not isinstance(document_map, Mapping):
        raise ProjectExecutionPlanValidationError("document_map must be a mapping")
    raw_entries = document_map.get("entries") or ()
    if isinstance(raw_entries, (str, bytes)) or not isinstance(raw_entries, Sequence):
        raise ProjectExecutionPlanValidationError("document_map entries must be a sequence")
    if len(raw_entries) > MAX_INVENTORY_RECORDS:
        raise ProjectExecutionPlanValidationError("document_map exceeds the inventory bound")

    by_path: dict[str, tuple[str, str]] = {}
    entry_ids: set[str] = set()
    entry_bodies: list[dict[str, Any]] = []
    for raw_entry in raw_entries:
        entry = _entry_body(raw_entry)
        path = str(entry.get("path") or "").strip()
        entry_id = str(entry.get("entry_id") or "").strip()
        source_hash = _normalize_sha256(entry.get("source_hash"), field_name="source_hash")
        if not path or not entry_id:
            raise ProjectExecutionPlanValidationError("document map entry lacks path or entry_id")
        if entry_id != document_entry_id(path):
            raise ProjectExecutionPlanValidationError(
                f"document map entry_id does not match its path: {path}"
            )
        if path in by_path or entry_id in entry_ids:
            raise ProjectExecutionPlanValidationError("document map contains duplicate identity")
        by_path[path] = (entry_id, source_hash)
        entry_ids.add(entry_id)
        entry_bodies.append(entry)

    computed_fingerprint = document_map_fingerprint(entry_bodies)
    stated_fingerprint = str(document_map.get("document_map_fingerprint") or "").strip().lower()
    if stated_fingerprint:
        stated_fingerprint = _normalize_sha256(
            stated_fingerprint,
            field_name="document_map_fingerprint",
        )
        if stated_fingerprint != computed_fingerprint:
            raise ProjectExecutionPlanValidationError("document map fingerprint mismatch")
    else:
        stated_fingerprint = computed_fingerprint

    raw_partial = document_map.get("partial_map") or ()
    if isinstance(raw_partial, (str, bytes)) or not isinstance(raw_partial, Sequence):
        raise ProjectExecutionPlanValidationError("document_map partial_map must be a sequence")
    if len(raw_partial) > MAX_INVENTORY_RECORDS:
        raise ProjectExecutionPlanValidationError(
            "document_map partial inventory exceeds its bound"
        )
    partial: list[dict[str, str]] = []
    for raw_gap in raw_partial:
        if not isinstance(raw_gap, Mapping):
            raise ProjectExecutionPlanValidationError("document_map gap is not a mapping")
        path = str(raw_gap.get("path") or "").strip()
        reason = str(raw_gap.get("reason") or "").strip()
        if not path or not reason:
            raise ProjectExecutionPlanValidationError("document_map gap lacks path or reason")
        partial.append({"path": path, "reason": reason})
    return by_path, stated_fingerprint, partial


def _cross_check_inventory(
    plan: ProjectExecutionPlan,
    document_map: Mapping[str, Any] | None,
) -> tuple[ProjectExecutionPlan, str | None, InventoryCoverage, tuple[str, ...]]:
    by_path, fingerprint, partial = _document_inventory(document_map)
    reviewed_indexed: set[str] = set()
    external: list[ReviewedDocument] = []
    bound_documents: list[ReviewedDocument] = []
    binding_warnings: list[str] = []

    for reviewed in plan.documents_reviewed:
        binding = by_path.get(reviewed.path)
        if binding is not None:
            expected_entry_id, expected_hash = binding
            corrected: list[str] = []
            if reviewed.entry_id is not None and reviewed.entry_id != expected_entry_id:
                corrected.append("entry_id")
            if reviewed.source_hash is not None and reviewed.source_hash != expected_hash:
                corrected.append("source_hash")
            if corrected:
                binding_warnings.append(
                    "document_binding_hint_corrected: "
                    f"{reviewed.path}; fields={','.join(corrected)}"
                )
            bound_documents.append(
                reviewed.model_copy(
                    update={"entry_id": expected_entry_id, "source_hash": expected_hash}
                )
            )
            reviewed_indexed.add(reviewed.path)
            continue

        if reviewed.entry_id is not None or reviewed.source_hash is not None:
            binding_warnings.append(f"external_document_binding_hint_ignored: {reviewed.path}")
        if not any(_is_direct_document_evidence(ref) for ref in reviewed.evidence_refs):
            raise ProjectExecutionPlanValidationError(
                f"map-external document lacks output evidence: {reviewed.path}"
            )
        unbound = reviewed.model_copy(update={"entry_id": None, "source_hash": None})
        bound_documents.append(unbound)
        external.append(unbound)

    bound_plan = ProjectExecutionPlan.model_validate(
        {
            **plan.model_dump(mode="python"),
            "documents_reviewed": [
                reviewed.model_dump(mode="python") for reviewed in bound_documents
            ],
        }
    )

    unreviewed = sorted(set(by_path) - reviewed_indexed)
    coverage = InventoryCoverage(
        indexed_documents=len(by_path),
        reviewed_indexed_documents=len(reviewed_indexed),
        unreviewed_indexed_documents=len(unreviewed),
        external_documents_reviewed=len(external),
        partial_inventory_records=len(partial),
        all_indexed_documents_reviewed=not unreviewed,
    )

    warnings: list[str] = list(binding_warnings)
    if document_map is None:
        warnings.append(
            "document_map_unavailable: reviewed documents are bound only to direct output evidence"
        )
    if unreviewed:
        examples = ", ".join(unreviewed[:5])
        warnings.append(
            "inventory_review_gap: "
            f"{len(unreviewed)} of {len(by_path)} indexed documents were not reviewed"
            + (f"; examples={examples}" if examples else "")
        )
    if partial:
        reasons = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(Counter(row["reason"] for row in partial).items())
        )
        warnings.append(
            f"inventory_discovery_gap: {len(partial)} paths were not indexed; reasons={reasons}"
        )
    for reviewed in external:
        direct_refs = [ref for ref in reviewed.evidence_refs if _is_direct_document_evidence(ref)]
        warnings.append(
            f"external_document_reviewed: {reviewed.path}; evidence={','.join(direct_refs[:3])}"
        )
    if len(warnings) > MAX_INVENTORY_WARNINGS:
        warnings = warnings[: MAX_INVENTORY_WARNINGS - 1] + [
            f"inventory_warnings_truncated: {len(warnings) - MAX_INVENTORY_WARNINGS + 1} omitted"
        ]
    return bound_plan, fingerprint, coverage, tuple(warning[:512] for warning in warnings)


def bind_project_execution_plan_inventory(
    candidate: Any,
    document_map: Mapping[str, Any] | None,
) -> ProjectExecutionPlan:
    """Fill Harness-owned document bindings without changing model strategy.

    The returned plan is the normalized object whose canonical digest must be
    placed in the phase claim.  Calling this before claim construction avoids
    changing hash semantics for existing schema-version-1 plan artifacts.
    """

    try:
        plan = validate_authored_plan(candidate)
        bound_plan, _fingerprint, _coverage, _warnings = _cross_check_inventory(plan, document_map)
        return bound_plan
    except ProjectExecutionPlanValidationError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProjectExecutionPlanValidationError(str(exc)) from exc


def seal_project_execution_plan(
    candidate: Any,
    *,
    source_attempt_id: str,
    claim_sha256: str,
    document_map: Mapping[str, Any] | None = None,
) -> SealedProjectExecutionPlan:
    """Cross-check and seal a candidate without performing container I/O."""

    try:
        plan = validate_authored_plan(candidate)
        attempt_id = str(source_attempt_id or "").strip()
        if not attempt_id or len(attempt_id) > 128:
            raise ProjectExecutionPlanValidationError("source_attempt_id is empty or too long")
        claim_digest = _normalize_sha256(claim_sha256, field_name="claim_sha256")
        plan, fingerprint, coverage, warnings = _cross_check_inventory(plan, document_map)
        authored_digest = canonical_authored_plan_sha256(plan)
        body = {
            "schema_version": PROJECT_EXECUTION_PLAN_SCHEMA_VERSION,
            "source_attempt_id": attempt_id,
            "claim_sha256": claim_digest,
            "document_map_fingerprint": fingerprint,
            "authored_plan_sha256": authored_digest,
            "inventory_coverage": coverage.model_dump(mode="json"),
            "inventory_warnings": list(warnings),
            "plan": _authored_plan_payload(plan),
        }
        body["artifact_sha256"] = canonical_sha256(body)
        artifact = SealedProjectExecutionPlan.model_validate(body)
        if (
            len(canonical_json(artifact.model_dump(mode="json")).encode("utf-8"))
            > MAX_SEALED_PLAN_BYTES
        ):
            raise ProjectExecutionPlanValidationError(
                "sealed project execution plan exceeds its bound"
            )
        return artifact
    except ProjectExecutionPlanValidationError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProjectExecutionPlanValidationError(str(exc)) from exc


def write_sealed_project_execution_plan(
    orchestrator: Any,
    artifact: Any,
) -> ContainerWriteResult:
    """Atomically persist one already-sealed artifact at the fixed path."""

    try:
        if isinstance(artifact, SealedProjectExecutionPlan):
            validated = SealedProjectExecutionPlan.model_validate(
                artifact.model_dump(mode="python")
            )
        elif isinstance(artifact, Mapping):
            validated = SealedProjectExecutionPlan.model_validate(dict(artifact))
        else:
            raise TypeError("artifact must be a SealedProjectExecutionPlan or mapping")
        body = canonical_json(_sealed_plan_payload(validated, include_artifact_sha256=True))
        if len(body.encode("utf-8")) > MAX_SEALED_PLAN_BYTES:
            raise ValueError("sealed artifact exceeds the byte bound")
    except (TypeError, ValueError, ValidationError):
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)
    return write_container_text_atomic(
        orchestrator,
        PROJECT_EXECUTION_PLAN_PATH,
        body,
        validate_json=True,
    )


def seal_and_write_project_execution_plan(
    candidate: Any,
    orchestrator: Any,
    *,
    source_attempt_id: str,
    claim_sha256: str,
    document_map: Mapping[str, Any] | None = None,
) -> SealedProjectExecutionPlan:
    """Engine convenience API: validate, seal, and atomically persist the plan."""

    artifact = seal_project_execution_plan(
        candidate,
        source_attempt_id=source_attempt_id,
        claim_sha256=claim_sha256,
        document_map=document_map,
    )
    result = write_sealed_project_execution_plan(orchestrator, artifact)
    if not result.persisted:
        raise ProjectExecutionPlanPersistenceError(
            f"project execution plan persistence failed: {result.code}"
        )
    return artifact


def read_sealed_project_execution_plan(orchestrator: Any) -> SealedProjectExecutionPlan | None:
    """Strictly read and verify the fixed sealed artifact; absent remains None."""

    try:
        raw = read_container_text(
            orchestrator,
            PROJECT_EXECUTION_PLAN_PATH,
            exact_bytes=True,
        )
    except ContainerFileReadError as exc:
        raise ProjectExecutionPlanReadError(str(exc)) from exc
    if raw is None:
        return None
    try:
        if len(raw.encode("utf-8")) > MAX_SEALED_PLAN_BYTES:
            raise ValueError("sealed project execution plan exceeds the read bound")
        return SealedProjectExecutionPlan.model_validate_json(raw, strict=True)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProjectExecutionPlanReadError(str(exc)) from exc


def sealed_test_disposition_status(orchestrator: Any) -> str | None:
    """``planned`` / ``blocked`` from the sealed plan, or ``None``.

    The one statement of record about whether unattended test execution was
    ever due for this run.  Evidence collectors read it to decide what an
    ABSENCE of test reports means — the alternative, a task-name allowlist in
    the harvest, read ``smokeTest`` as "not a test run" at all.

    ``None`` is unknown and is never "no": no plan sealed yet, a plan authored
    before ``test_disposition`` existed, or a read that did not land.  A caller
    that cannot establish the meaning of an absence discloses that it could
    not, and never asserts that tests were expected.
    """

    try:
        sealed = read_sealed_project_execution_plan(orchestrator)
    except Exception:  # evidence collection never breaks the runner
        return None
    disposition = getattr(getattr(sealed, "plan", None), "test_disposition", None)
    status = getattr(disposition, "status", None)
    return status if status in ("planned", "blocked") else None


def render_plan_system_prompt(
    value: ProjectExecutionPlan | SealedProjectExecutionPlan | Any,
) -> str:
    """Public rendering API for either an authored or a sealed plan."""

    if isinstance(value, SealedProjectExecutionPlan):
        return value.to_system_prompt()
    return validate_authored_plan(value).to_system_prompt()


def _bounded_lines(lines: Sequence[str], *, max_chars: int) -> str:
    budget = max(512, min(int(max_chars), MAX_INVENTORY_PROMPT_CHARS))
    kept: list[str] = []
    used = 0
    omitted = 0
    for raw_line in lines:
        line = str(raw_line).replace("\n", " ")[:2_048]
        cost = len(line) + (1 if kept else 0)
        if used + cost <= budget - 64:
            kept.append(line)
            used += cost
        else:
            omitted += 1
    if omitted:
        marker = f"... {omitted} inventory lines omitted by prompt bound"
        while kept and len("\n".join([*kept, marker])) > budget:
            kept.pop()
            omitted += 1
            marker = f"... {omitted} inventory lines omitted by prompt bound"
        kept.append(marker)
    return "\n".join(kept)[:budget]


def render_document_inventory_guidance(
    document_map: Mapping[str, Any] | None,
    *,
    max_chars: int = MAX_INVENTORY_PROMPT_CHARS,
) -> str:
    """Render bounded discovery guidance for the analyze model.

    The rendering intentionally labels the inventory as hints.  It presents
    paths, not Harness-owned ids or hashes, so the model can choose what to read
    without copying metadata or treating an omitted path as irrelevant.
    """

    by_path, fingerprint, partial = _document_inventory(document_map)
    lines = [
        "=== BOUNDED DOCUMENT INVENTORY ===",
        "Harness discovery is guidance, not a document allowlist. Choose and read project-specific sources before authoring the execution plan.",
        f"document_map_fingerprint: {fingerprint or 'unavailable'}",
        f"indexed_documents: {len(by_path)}",
        f"partial_inventory_records: {len(partial)}",
    ]
    for path in sorted(by_path, key=lambda value: (value.count("/"), value)):
        lines.append(f"- path={path}")
    for gap in sorted(partial, key=lambda item: (item["path"], item["reason"])):
        lines.append(f"- inventory_gap path={gap['path']} reason={gap['reason']}")
    return _bounded_lines(lines, max_chars=max_chars)


__all__ = [
    "ExecutionStep",
    "InventoryCoverage",
    "MAX_AUTHORED_PLAN_BYTES",
    "MAX_INVENTORY_PROMPT_CHARS",
    "MAX_SEALED_PLAN_BYTES",
    "MAX_SYSTEM_PROMPT_CHARS",
    "PROJECT_EXECUTION_PLAN_PATH",
    "PROJECT_EXECUTION_PLAN_SCHEMA_VERSION",
    "ProjectExecutionPlan",
    "ProjectExecutionPlanPersistenceError",
    "ProjectExecutionPlanReadError",
    "ProjectExecutionPlanValidationError",
    "ReviewedDocument",
    "SealedProjectExecutionPlan",
    "bind_project_execution_plan_inventory",
    "canonical_authored_plan_sha256",
    "read_sealed_project_execution_plan",
    "render_document_inventory_guidance",
    "render_plan_system_prompt",
    "seal_and_write_project_execution_plan",
    "seal_project_execution_plan",
    "validate_authored_plan",
    "validate_reviewed_document_evidence",
    "write_sealed_project_execution_plan",
]
