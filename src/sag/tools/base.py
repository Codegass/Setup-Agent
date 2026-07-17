"""Base classes for agent tools."""

from __future__ import annotations

import hashlib
import inspect
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Union,
    get_args,
    get_origin,
)
from uuid import uuid4

from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    model_validator,
)

from sag.evidence import (
    EvidenceAssessment,
    EvidenceFinding,
    EvidenceStatus,
    InvocationStatus,
    OperationOutcome,
    TestStats,
)


def new_execution_id() -> str:
    return f"execution_{uuid4().hex}"


class ToolError(Exception):
    """Enhanced tool error with actionable guidance and categorization."""

    def __init__(
        self,
        message: str,
        category: str = "execution",  # "validation" | "execution" | "system"
        suggestions: Optional[List[str]] = None,
        documentation_links: Optional[List[str]] = None,
        error_code: Optional[str] = None,
        raw_output: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.suggestions = suggestions or []
        self.documentation_links = documentation_links or []
        self.error_code = error_code
        self.raw_output = raw_output
        self.details = details or {}
        self.retryable = retryable

    def to_result(
        self,
        duration: Optional[float] = None,
        invocation_status: InvocationStatus | str = InvocationStatus.COMPLETED,
    ) -> "ToolResult":
        """Convert ToolError to ToolResult with preserved metadata."""
        metadata = {
            "failure_category": self.category,
            "retryable": self.retryable,
        }

        # Add optional metadata
        if duration is not None:
            metadata["duration_ms"] = duration * 1000  # Convert to milliseconds

        if self.details:
            metadata["error_details"] = self.details

        return ToolResult.terminal_failure(
            invocation_status=invocation_status,
            output="",
            error=self.message,
            error_code=self.error_code,
            suggestions=self.suggestions,
            documentation_links=self.documentation_links,
            raw_output=self.raw_output,
            metadata=metadata,
        )


class OutputPersistenceError(RuntimeError):
    """Raised when neither primary nor emergency output persistence is durable."""

    def __init__(
        self,
        message: str,
        *,
        draft: UnpersistedToolResult | None = None,
        tool_name: str | None = None,
        params: Dict[str, Any] | None = None,
        execution_id: str | None = None,
        actual_executions: Iterable[ActualToolExecution] = (),
    ) -> None:
        super().__init__(message)
        self.draft = draft
        self.tool_name = tool_name
        self.params = dict(params) if params is not None else None
        self.execution_id = execution_id
        self.actual_executions = tuple(actual_executions)

    def attach_draft(self, draft: UnpersistedToolResult) -> OutputPersistenceError:
        if self.draft is None:
            self.draft = (
                draft.model_copy(update={"execution_id": self.execution_id})
                if self.execution_id is not None
                else draft
            )
        return self

    def attach_invocation(
        self,
        tool_name: str,
        params: Dict[str, Any],
        *,
        execution_id: str | None = None,
    ) -> OutputPersistenceError:
        if self.tool_name is None:
            self.tool_name = tool_name
            self.params = dict(params)
        if self.execution_id is None:
            self.execution_id = execution_id or new_execution_id()
        if self.draft is not None and self.draft.execution_id != self.execution_id:
            self.draft = self.draft.model_copy(update={"execution_id": self.execution_id})
        return self

    def attach_actual_executions(
        self,
        executions: Iterable[ActualToolExecution],
    ) -> OutputPersistenceError:
        merged = {actual.execution_id: actual for actual in self.actual_executions}
        for actual in executions:
            existing = merged.get(actual.execution_id)
            if existing is not None and not existing.is_exact_replay_of(actual):
                raise ValueError(f"conflicting execution_id {actual.execution_id}")
            merged.setdefault(actual.execution_id, actual)
        self.actual_executions = tuple(merged.values())
        return self


LEGAL_RESULT_STATES = {
    InvocationStatus.PENDING: {OperationOutcome.UNKNOWN},
    InvocationStatus.COMPLETED: {
        OperationOutcome.UNKNOWN,
        OperationOutcome.SUCCESS,
        OperationOutcome.PARTIAL,
        OperationOutcome.FAILED,
        OperationOutcome.SKIPPED,
    },
    InvocationStatus.TIMEOUT: {
        OperationOutcome.UNKNOWN,
        OperationOutcome.PARTIAL,
        OperationOutcome.FAILED,
    },
    InvocationStatus.CRASHED: {OperationOutcome.UNKNOWN, OperationOutcome.FAILED},
    InvocationStatus.CANCELLED: {OperationOutcome.UNKNOWN, OperationOutcome.SKIPPED},
}

READ_ONLY_RESULT_FIELDS = {
    "invocation_status",
    "operation_outcome",
    "evidence_status",
    "poll_ref",
    "failure_signature",
    "error_tail_preview",
    "output_ref",
    "evidence_assessment",
}


@dataclass(frozen=True)
class _DurableOutputBinding:
    storage: Any
    task_id: str
    tool_name: str


_DURABLE_OUTPUT_BINDING: ContextVar[Optional[_DurableOutputBinding]] = ContextVar(
    "sag_durable_tool_output_binding",
    default=None,
)


def is_output_storage_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"output_[A-Za-z0-9_-]+", value))


def canonical_full_output_source(
    *,
    raw_output: Any = None,
    output: Any = None,
    error: Any = None,
) -> str:
    """Normalize the canonical full-output source without discarding error-only text."""
    source = raw_output or output or error or ""
    if isinstance(source, bytes):
        return source.decode("utf-8", errors="replace")
    return source if isinstance(source, str) else str(source)


@contextmanager
def bind_tool_result_output_storage(
    storage: Any,
    *,
    task_id: str = "tool_result",
    tool_name: str = "tool",
) -> Iterator[None]:
    """Bind the existing OutputStorage owner for results built in this call."""
    binding = (
        _DurableOutputBinding(storage=storage, task_id=task_id, tool_name=tool_name)
        if storage is not None
        else None
    )
    token = _DURABLE_OUTPUT_BINDING.set(binding)
    try:
        yield
    finally:
        _DURABLE_OUTPUT_BINDING.reset(token)


def _store_bound_output(output: str, *, metadata: Dict[str, Any]) -> Optional[str]:
    binding = _DURABLE_OUTPUT_BINDING.get()
    if binding is None:
        return None

    failures: list[str] = []
    persistence_methods = (
        ("primary", getattr(binding.storage, "store_output", None)),
        ("emergency", getattr(binding.storage, "store_emergency_output", None)),
    )
    for label, persist in persistence_methods:
        if not callable(persist):
            failures.append(f"{label} persistence is unavailable")
            continue
        try:
            ref = persist(
                task_id=binding.task_id,
                tool_name=binding.tool_name,
                output=output,
                metadata=metadata,
            )
        except Exception as exc:
            failures.append(f"{label} persistence raised {type(exc).__name__}")
            continue
        if not is_output_storage_ref(ref):
            failures.append(f"{label} persistence returned no durable reference")
            continue
        try:
            retrieved = binding.storage.retrieve_output(ref)
        except Exception as exc:
            failures.append(f"{label} retrieval raised {type(exc).__name__}")
            continue
        if retrieved != output:
            failures.append(f"{label} output did not round-trip")
            continue
        return ref

    raise OutputPersistenceError(
        "primary and emergency output persistence failed: " + "; ".join(failures)
    )


def require_persisted_output_storage_ref(ref: str, *, storage: Any = None) -> None:
    if not is_output_storage_ref(ref):
        raise ValueError("output ref must be a resolvable OutputStorage output_* ref")
    binding = _DURABLE_OUTPUT_BINDING.get()
    resolver = storage if storage is not None else (binding.storage if binding else None)
    index_lookup = getattr(resolver, "has_output_ref", None) if resolver is not None else None
    persisted = (
        bool(index_lookup(ref))
        if callable(index_lookup)
        else resolver is not None and resolver.retrieve_output(ref) is not None
    )
    if not persisted:
        raise ValueError("output ref must be persisted in provided or bound OutputStorage")


def _normalize_temporary_path(match: re.Match[str]) -> str:
    raw_path = match.group(0)
    trailing_period = raw_path.endswith(".")
    path = raw_path[:-1] if trailing_period else raw_path
    basename = PurePosixPath(path.rstrip("/")).name
    parsed_basename = PurePosixPath(basename)
    stable_suffix = "".join(parsed_basename.suffixes)
    stem = basename[: -len(stable_suffix)] if stable_suffix else basename
    entropy_bearing = bool(
        re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
            stem,
            flags=re.IGNORECASE,
        )
        or re.search(r"[0-9a-f]{8,}", stem, flags=re.IGNORECASE)
        or (
            re.match(
                r"^(?:sag|pytest|tmp|temp|run|output|result)(?:[-_.].*)?$",
                stem,
                flags=re.IGNORECASE,
            )
            and any(
                len(token) >= 6 and re.search(r"[A-Za-z]", token) and re.search(r"\d", token)
                for token in re.split(r"[-_.]", stem)
            )
        )
    )
    stable_basename = f"<temp-entry>{stable_suffix}" if entropy_bearing else basename
    normalized = f"<tmp-path>/{stable_basename}" if stable_basename else "<tmp-path>"
    return normalized + ("." if trailing_period else "")


def _stable_failure_signature_source(source: str) -> str:
    """Remove only runtime identifiers that do not identify a root cause."""
    normalized = re.sub(
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
        "<timestamp>",
        source,
    )
    normalized = re.sub(r"\bjob:[A-Za-z0-9_-]+\b", "job:<id>", normalized)
    normalized = re.sub(
        r"\b(?:pid|process(?:\s+id)?)[=:#\s]*\d+\b",
        "pid=<id>",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(?:/tmp|/var/tmp)/[^\s:;,)\]\}>'\"]+",
        _normalize_temporary_path,
        normalized,
    )
    normalized = re.sub(
        r"\b(?:progress|step|completed)\s+\d+\s*/\s*\d+\b",
        "progress <count>",
        normalized,
        flags=re.IGNORECASE,
    )
    return " ".join(normalized.split())


@dataclass(frozen=True, slots=True)
class ActualToolExecution:
    """One real tool invocation hidden behind a facade or recovery result."""

    tool_name: str
    params: Dict[str, Any]
    result: ToolResult
    execution_id: str = field(default_factory=new_execution_id)

    def is_exact_replay_of(self, other: ActualToolExecution) -> bool:
        return bool(
            self.execution_id == other.execution_id
            and self.tool_name == other.tool_name
            and self.params == other.params
            and self.result == other.result
        )


UNPERSISTED_DRAFT_MAX_BYTES = 32 * 1024
_UNPERSISTED_DRAFT_CHAR_BUDGET = 3500
_UNPERSISTED_DRAFT_NODE_BUDGET = 96
_UNPERSISTED_DRAFT_MAX_TEST_COUNT = (1 << 63) - 1


@dataclass
class _DraftBudget:
    """One shared budget for all bounded construction-failure diagnostics."""

    remaining_chars: int = _UNPERSISTED_DRAFT_CHAR_BUDGET
    remaining_nodes: int = _UNPERSISTED_DRAFT_NODE_BUDGET
    truncated: bool = False

    def _claim_node(self) -> bool:
        if self.remaining_nodes <= 0:
            self.truncated = True
            return False
        self.remaining_nodes -= 1
        return True

    def _clip_text(self, value: Any, max_chars: int, *, keep_tail: bool = False) -> str:
        raw = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        available = min(max_chars, self.remaining_chars)
        if available <= 0:
            if raw:
                self.truncated = True
            return ""
        clipped = raw[-available:] if keep_tail else raw[:available]
        self.remaining_chars -= len(clipped)
        if len(clipped) != len(raw):
            self.truncated = True
        return clipped

    def text(
        self,
        value: Any,
        max_chars: int,
        *,
        keep_tail: bool = False,
    ) -> str | None:
        if value is None:
            return None
        if not self._claim_node():
            return ""
        return self._clip_text(value, max_chars, keep_tail=keep_tail)

    def strings(
        self,
        values: Any,
        *,
        max_items: int = 16,
        max_chars: int = 500,
    ) -> List[str]:
        if values is None:
            return []
        iterable = [values] if isinstance(values, (str, bytes)) else values
        bounded: List[str] = []
        for index, value in enumerate(iterable):
            if index >= max_items:
                self.truncated = True
                break
            item = self.text(value, max_chars)
            if item is None:
                continue
            if not item and str(value):
                break
            bounded.append(item)
        return bounded

    def diagnostic(self, value: Any, *, depth: int = 0) -> Any:
        if not self._claim_node():
            return None
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (str, bytes)):
            return self._clip_text(value, 500)
        if depth >= 4:
            self.truncated = True
            return "<truncated>"
        if isinstance(value, dict):
            bounded: Dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 16:
                    self.truncated = True
                    break
                bounded_key = self.text(key, 80)
                if not bounded_key:
                    break
                bounded[bounded_key] = self.diagnostic(item, depth=depth + 1)
            return bounded
        if isinstance(value, (list, tuple, set)):
            bounded_list: List[Any] = []
            for index, item in enumerate(value):
                if index >= 16:
                    self.truncated = True
                    break
                if self.remaining_nodes <= 0 or self.remaining_chars <= 0:
                    self.truncated = True
                    break
                bounded_list.append(self.diagnostic(item, depth=depth + 1))
            return bounded_list
        return self._clip_text(value, 500)

    def findings(self, values: Any) -> List[EvidenceFinding]:
        bounded: List[EvidenceFinding] = []
        for index, value in enumerate(values or []):
            if index >= 8:
                self.truncated = True
                break
            try:
                finding = (
                    value
                    if isinstance(value, EvidenceFinding)
                    else EvidenceFinding.model_validate(value)
                )
            except (TypeError, ValueError):
                self.truncated = True
                continue
            finding_type = self.text(finding.type, 256)
            reason = self.text(finding.reason, 512)
            if not finding_type or not reason:
                self.truncated = True
                break
            refs = self.strings(finding.refs, max_items=8, max_chars=500)
            details = self.diagnostic(finding.details)
            bounded.append(
                EvidenceFinding(
                    type=finding_type,
                    reason=reason,
                    status=finding.status,
                    refs=refs,
                    details=details if isinstance(details, dict) else {},
                )
            )
        return bounded


def _bounded_draft_test_stats(value: Any, budget: _DraftBudget) -> TestStats | None:
    if value is None:
        return None
    try:
        stats = value if isinstance(value, TestStats) else TestStats.model_validate(value)
    except Exception:
        budget.truncated = True
        return None
    counts = (
        stats.discovered,
        stats.executed,
        stats.passed,
        stats.failed,
        stats.skipped,
    )
    if any(
        count is not None and abs(count) > _UNPERSISTED_DRAFT_MAX_TEST_COUNT for count in counts
    ):
        budget.truncated = True
        return None
    return stats.model_copy(deep=True)


def _serialized_draft_size(value: Any) -> int:
    try:
        return len(value.model_dump_json().encode("utf-8"))
    except Exception:
        return UNPERSISTED_DRAFT_MAX_BYTES + 1


class UnpersistedToolResult(BaseModel):
    """Bounded evidence from a real execution whose full output was not durable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: Optional[str] = None
    invocation_status: InvocationStatus
    operation_outcome: OperationOutcome
    evidence_status: EvidenceStatus
    poll_ref: Optional[str] = None
    failure_signature: Optional[str] = None
    error_tail_preview: Optional[str] = None
    output_ref: None = None
    evidence_assessment: EvidenceAssessment = EvidenceAssessment.UNKNOWN
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)
    documentation_links: List[str] = Field(default_factory=list)
    raw_data: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    validator_findings: List[EvidenceFinding] = Field(default_factory=list)
    test_stats: Optional[TestStats] = None
    facts: Dict[str, Any] = Field(default_factory=dict)
    refs: List[str] = Field(default_factory=list)
    truncated: bool = False

    @model_validator(mode="after")
    def _enforce_serialized_size_limit(self) -> UnpersistedToolResult:
        if _serialized_draft_size(self) > UNPERSISTED_DRAFT_MAX_BYTES:
            raise ValueError("unpersisted draft exceeds serialized size limit")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> UnpersistedToolResult:
        copied = super().model_copy(update=update, deep=deep)
        if _serialized_draft_size(copied) > UNPERSISTED_DRAFT_MAX_BYTES:
            raise ValueError("unpersisted draft exceeds serialized size limit")
        return copied

    @classmethod
    def from_failed_construction(
        cls,
        *,
        invocation_status: InvocationStatus,
        operation_outcome: OperationOutcome,
        evidence_status: EvidenceStatus | str,
        payload: Dict[str, Any],
    ) -> UnpersistedToolResult:
        budget = _DraftBudget()
        execution_id = budget.text(payload.get("execution_id"), 128)
        poll_ref = budget.text(payload.get("poll_ref"), 256)
        failure_signature = budget.text(payload.get("failure_signature"), 256)
        error_tail_preview = budget.text(payload.get("error_tail_preview"), 400, keep_tail=True)
        error = budget.text(payload.get("error"), 1000)
        error_code = budget.text(payload.get("error_code"), 200)
        validator_findings = budget.findings(payload.get("validator_findings"))
        conflicts = budget.strings(payload.get("conflicts"))
        evidence_refs = budget.strings(payload.get("evidence_refs"))
        refs = budget.strings(payload.get("refs"))
        facts = budget.diagnostic(payload.get("facts") or {})
        metadata = budget.diagnostic(payload.get("metadata") or {})
        raw_data = (
            budget.diagnostic(payload.get("raw_data"))
            if payload.get("raw_data") is not None
            else None
        )
        suggestions = budget.strings(payload.get("suggestions"))
        documentation_links = budget.strings(payload.get("documentation_links"))
        test_stats = _bounded_draft_test_stats(payload.get("test_stats"), budget)
        draft = cls(
            execution_id=execution_id,
            invocation_status=invocation_status,
            operation_outcome=operation_outcome,
            evidence_status=EvidenceStatus(evidence_status),
            poll_ref=poll_ref,
            failure_signature=failure_signature,
            error_tail_preview=error_tail_preview,
            evidence_assessment=payload.get("evidence_assessment", EvidenceAssessment.UNKNOWN),
            error=error,
            error_code=error_code,
            suggestions=suggestions,
            documentation_links=documentation_links,
            raw_data=raw_data if isinstance(raw_data, dict) else None,
            metadata=metadata if isinstance(metadata, dict) else {},
            evidence_refs=evidence_refs,
            conflicts=conflicts,
            validator_findings=validator_findings,
            test_stats=test_stats,
            facts=facts if isinstance(facts, dict) else {},
            refs=refs,
            truncated=budget.truncated,
        )
        if _serialized_draft_size(draft) <= UNPERSISTED_DRAFT_MAX_BYTES:
            return draft

        minimal_findings = [
            finding.model_copy(update={"refs": finding.refs[:1], "details": {}})
            for finding in draft.validator_findings[:1]
        ]
        fallback = cls(
            execution_id=draft.execution_id,
            invocation_status=draft.invocation_status,
            operation_outcome=draft.operation_outcome,
            evidence_status=draft.evidence_status,
            poll_ref=draft.poll_ref,
            failure_signature=draft.failure_signature,
            error_tail_preview=draft.error_tail_preview,
            evidence_assessment=draft.evidence_assessment,
            error=draft.error,
            error_code=draft.error_code,
            metadata={"draft_truncated": True},
            conflicts=draft.conflicts[:4],
            validator_findings=minimal_findings,
            test_stats=draft.test_stats,
            refs=draft.refs[:4],
            evidence_refs=draft.evidence_refs[:4],
            truncated=True,
        )
        if _serialized_draft_size(fallback) <= UNPERSISTED_DRAFT_MAX_BYTES:
            return fallback

        last_resort = cls(
            invocation_status=draft.invocation_status,
            operation_outcome=draft.operation_outcome,
            evidence_status=draft.evidence_status,
            evidence_assessment=draft.evidence_assessment,
            metadata={"draft_truncated": True},
            truncated=True,
        )
        if _serialized_draft_size(last_resort) > UNPERSISTED_DRAFT_MAX_BYTES:
            raise ValueError("minimal unpersisted draft exceeds serialized size limit")
        return last_resort

    @property
    def is_terminal(self) -> bool:
        return self.invocation_status is not InvocationStatus.PENDING

    @property
    def succeeded(self) -> bool:
        return False


class ToolResult(BaseModel):
    """Canonical, orthogonal tool execution result."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    _output_ref_verified: bool = PrivateAttr(default=False)
    _execution_trace: tuple[ActualToolExecution, ...] = PrivateAttr(default=())

    invocation_status: InvocationStatus
    operation_outcome: OperationOutcome
    evidence_status: EvidenceStatus
    poll_ref: Optional[str] = None
    failure_signature: Optional[str] = None
    error_tail_preview: Optional[str] = None
    output_ref: Optional[str] = None
    output: str
    evidence_assessment: EvidenceAssessment = EvidenceAssessment.UNKNOWN
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)
    documentation_links: List[str] = Field(default_factory=list)
    raw_output: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    validator_findings: List[EvidenceFinding] = Field(default_factory=list)
    test_stats: Optional[TestStats] = None

    facts: Dict[str, Any] = Field(default_factory=dict)
    refs: List[str] = Field(default_factory=list)

    @classmethod
    def completed(
        cls,
        *,
        output: str,
        operation_outcome: OperationOutcome | str,
        evidence_status: EvidenceStatus | str = EvidenceStatus.VERIFIED,
        output_ref_storage: Any = None,
        **payload: Any,
    ) -> "ToolResult":
        """Build a terminal result and fill stable provenance for failures."""
        return cls._terminal(
            invocation_status=InvocationStatus.COMPLETED,
            output=output,
            operation_outcome=operation_outcome,
            evidence_status=evidence_status,
            output_ref_storage=output_ref_storage,
            **payload,
        )

    @classmethod
    def terminal_failure(
        cls,
        *,
        invocation_status: InvocationStatus | str,
        output: str,
        evidence_status: EvidenceStatus | str = EvidenceStatus.VERIFIED,
        output_ref_storage: Any = None,
        **payload: Any,
    ) -> "ToolResult":
        """Build a failed terminal result with its observed lifecycle status."""
        status = InvocationStatus(invocation_status)
        if status is InvocationStatus.PENDING:
            raise ValueError("terminal failures require a non-pending invocation_status")
        return cls._terminal(
            invocation_status=status,
            output=output,
            operation_outcome=OperationOutcome.FAILED,
            evidence_status=evidence_status,
            output_ref_storage=output_ref_storage,
            **payload,
        )

    @classmethod
    def _terminal(
        cls,
        *,
        invocation_status: InvocationStatus,
        output: str,
        operation_outcome: OperationOutcome | str,
        evidence_status: EvidenceStatus | str,
        output_ref_storage: Any,
        **payload: Any,
    ) -> "ToolResult":
        if output_ref_storage is not None:
            with bind_tool_result_output_storage(output_ref_storage):
                return cls._terminal(
                    invocation_status=invocation_status,
                    output=output,
                    operation_outcome=operation_outcome,
                    evidence_status=evidence_status,
                    output_ref_storage=None,
                    **payload,
                )

        outcome = OperationOutcome(operation_outcome)
        source = canonical_full_output_source(
            raw_output=payload.get("raw_output"),
            output=output,
            error=payload.get("error"),
        )
        payload.setdefault(
            "evidence_assessment",
            {
                OperationOutcome.SUCCESS: EvidenceAssessment.SUCCESS,
                OperationOutcome.PARTIAL: EvidenceAssessment.PARTIAL,
                OperationOutcome.FAILED: EvidenceAssessment.BLOCKED,
            }.get(outcome, EvidenceAssessment.UNKNOWN),
        )
        if outcome is OperationOutcome.FAILED:
            signature_source = _stable_failure_signature_source(source)
            digest = hashlib.sha256(signature_source.encode("utf-8", errors="replace")).hexdigest()[
                :16
            ]
            error_code = str(payload.get("error_code") or "TOOL_OPERATION_FAILED")
            payload["error_code"] = error_code
            if not payload.get("failure_signature"):
                payload["failure_signature"] = f"{error_code}:{digest}"
            if not payload.get("error_tail_preview"):
                payload["error_tail_preview"] = source[-400:] or error_code

        if outcome in {OperationOutcome.FAILED, OperationOutcome.PARTIAL}:
            output_ref = payload.get("output_ref")
            if not output_ref:
                output_ref = next(
                    (
                        ref
                        for ref in payload.get("evidence_refs") or []
                        if is_output_storage_ref(ref)
                    ),
                    None,
                )
            if not output_ref:
                try:
                    output_ref = _store_bound_output(
                        source,
                        metadata={
                            "operation_outcome": outcome.value,
                            "error_code": payload.get("error_code"),
                            "failure_signature": payload.get("failure_signature"),
                        },
                    )
                except OutputPersistenceError as exc:
                    exc.attach_draft(
                        UnpersistedToolResult.from_failed_construction(
                            invocation_status=invocation_status,
                            operation_outcome=outcome,
                            evidence_status=evidence_status,
                            payload=payload,
                        )
                    )
                    raise
            if outcome is OperationOutcome.FAILED and not output_ref:
                raise ValueError(
                    "canonical failed results require durable output storage before construction"
                )
            if output_ref:
                payload["output_ref"] = output_ref
        result_values = {
            "invocation_status": invocation_status,
            "operation_outcome": outcome,
            "evidence_status": evidence_status,
            "output": output,
            **payload,
        }
        return cls(**result_values)

    @classmethod
    def completed_success(cls, *, output: str, **payload: Any) -> "ToolResult":
        return cls.completed(
            output=output,
            operation_outcome=OperationOutcome.SUCCESS,
            **payload,
        )

    @classmethod
    def completed_failure(cls, *, output: str, **payload: Any) -> "ToolResult":
        return cls.completed(
            output=output,
            operation_outcome=OperationOutcome.FAILED,
            **payload,
        )

    @model_validator(mode="after")
    def _validate_result_state(self) -> "ToolResult":
        allowed_outcomes = LEGAL_RESULT_STATES[self.invocation_status]
        if self.operation_outcome not in allowed_outcomes:
            raise ValueError(
                f"{self.invocation_status.value} results require operation_outcome to be one of "
                f"{sorted(outcome.value for outcome in allowed_outcomes)}"
            )
        if self.invocation_status is InvocationStatus.PENDING:
            if not self.poll_ref or not self.poll_ref.strip():
                raise ValueError("pending results require a stable poll_ref")
            if self.evidence_status is not EvidenceStatus.UNKNOWN:
                raise ValueError("pending results require evidence_status='unknown'")

        if self.operation_outcome is OperationOutcome.FAILED:
            for field_name in ("failure_signature", "error_tail_preview", "output_ref"):
                value = getattr(self, field_name)
                if not value or not value.strip():
                    raise ValueError(f"canonical failed results require nonblank {field_name}")
            if len(self.error_tail_preview) > 400:
                raise ValueError("error_tail_preview must be at most 400 characters")
            if not self.error_code or not self.error_code.strip():
                raise ValueError("canonical failed results require nonblank error_code")
            if not is_output_storage_ref(self.output_ref):
                raise ValueError(
                    "canonical failed results require a resolvable OutputStorage output_* ref"
                )
            if not self._output_ref_verified:
                require_persisted_output_storage_ref(self.output_ref)
                self._output_ref_verified = True

        return self

    @property
    def is_terminal(self) -> bool:
        return self.invocation_status is not InvocationStatus.PENDING

    @property
    def succeeded(self) -> bool:
        return (
            self.invocation_status is InvocationStatus.COMPLETED
            and self.operation_outcome is OperationOutcome.SUCCESS
        )

    @property
    def execution_trace(self) -> tuple[ActualToolExecution, ...]:
        return self._execution_trace

    def with_execution_trace(
        self,
        executions: Iterable[ActualToolExecution],
    ) -> ToolResult:
        traced = self.model_copy(deep=True)
        traced._execution_trace = tuple(executions)
        return traced

    def __setattr__(self, name: str, value: Any) -> None:
        if name in READ_ONLY_RESULT_FIELDS:
            raise TypeError(f"ToolResult.{name} is read-only after construction")
        super().__setattr__(name, value)

    def __str__(self) -> str:
        if self.succeeded:
            return self.output
        else:
            result = f"Error: {self.error}"
            if self.error_code:
                result += f" (Code: {self.error_code})"
            if self.suggestions:
                result += f"\n\nSuggestions:\n" + "\n".join(f"• {s}" for s in self.suggestions)
            if self.documentation_links:
                result += f"\n\nDocumentation:\n" + "\n".join(
                    f"• {link}" for link in self.documentation_links
                )
            return result


class BaseTool(ABC):
    """Base class for all agent tools with enhanced error handling."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self._parameter_schema: Dict[str, Any] = {}

        # Output truncation settings - increased for build tools
        self.max_output_length = 10000  # Maximum total output length (increased from 3000)
        self.head_length = 4000  # Length of beginning portion (increased from 1200)
        self.tail_length = 3000  # Length of ending portion (increased from 800)

        self._generate_parameter_schema()

    def _generate_parameter_schema(self):
        """Auto-generate parameter schema from execute method signature."""
        sig = inspect.signature(self.execute)
        schema = {"type": "object", "properties": {}, "required": []}

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue

            # Skip **kwargs parameters as they are handled specially
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                continue

            # Skip *args parameters as they are not supported in JSON schema
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                continue

            param_info = {
                "type": "string",  # Default to string
                "description": f"Parameter {param_name}",
            }

            # Check if parameter has a default value
            if param.default == inspect.Parameter.empty:
                schema["required"].append(param_name)
            else:
                param_info["default"] = param.default

            # Try to infer type from annotation
            if param.annotation != inspect.Parameter.empty:
                annotation = param.annotation
                origin = get_origin(annotation)
                if origin is Union:
                    non_none_args = [arg for arg in get_args(annotation) if arg is not type(None)]
                    if len(non_none_args) == 1:
                        annotation = non_none_args[0]
                        origin = get_origin(annotation)

                if annotation == int:
                    param_info["type"] = "integer"
                elif annotation == float:
                    param_info["type"] = "number"
                elif annotation == bool:
                    param_info["type"] = "boolean"
                elif annotation == list or origin is list:
                    param_info["type"] = "array"

            schema["properties"][param_name] = param_info

        self._parameter_schema = schema

    def _truncate_output(self, output: str, tool_name: str = None) -> str:
        """
        Intelligently truncate long output to preserve context window.

        Args:
            output: The raw output to truncate
            tool_name: Name of the tool (used for custom extraction)

        Returns:
            Truncated output with head, tail, and guidance
        """
        if not output or len(output) <= self.max_output_length:
            return output

        # Try tool-specific extraction first
        extracted = self._extract_key_info(output, tool_name or self.name)
        if extracted and extracted != output:
            logger.info(
                f"Applied {tool_name or self.name}-specific extraction, reduced from {len(output)} to {len(extracted)} chars"
            )
            # If extraction is still too long, apply general truncation
            if len(extracted) <= self.max_output_length:
                return extracted
            output = extracted

        # General truncation: head + tail with guidance
        head = output[: self.head_length]
        tail = output[-self.tail_length :]

        truncation_info = (
            f"\n\n... [OUTPUT TRUNCATED: {len(output)} chars total, showing first {self.head_length} "
            f"and last {self.tail_length} chars] ...\n"
            f"💡 TIP: If you need specific information from the full output, use 'bash' tool with 'grep' "
            f"to search for keywords, or 'file_io' to save and search through the complete output.\n\n"
        )

        return head + truncation_info + tail

    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """
        Extract key information from tool output.
        Override in subclasses for tool-specific extraction.

        Args:
            output: Raw tool output
            tool_name: Name of the tool

        Returns:
            Extracted key information or original output
        """
        # Default implementation - can be overridden by specific tools
        return output

    def _extract_maven_key_info(self, output: str) -> str:
        """Extract key information from Maven output."""
        lines = output.split("\n")
        key_lines = []

        # Capture key indicators
        build_status = ""
        test_summary = ""
        compilation_info = ""
        error_summary = []

        for line in lines:
            line_lower = line.lower()

            # Build status
            if "build success" in line_lower:
                build_status = "✅ BUILD SUCCESS"
            elif "build failure" in line_lower:
                build_status = "❌ BUILD FAILURE"

            # Test results
            elif "tests run:" in line_lower:
                test_summary = f"📊 {line.strip()}"

            # Compilation info
            elif "compilation failure" in line_lower:
                compilation_info = "⚠️ Compilation failures detected"
            elif "nothing to compile" in line_lower:
                compilation_info = "✅ All classes up to date"
            elif "building jar:" in line_lower:
                compilation_info = "📦 JAR artifact created"

            # Error patterns - collect specific errors (increased limit)
            elif any(
                error_pattern in line_lower
                for error_pattern in [
                    "error:",
                    "[error]",
                    "exception:",
                    "failed to",
                    "cannot find",
                    "package does not exist",
                    "compilation failure",
                    "cannot resolve",
                    "symbol not found",
                    "method does not exist",
                ]
            ):
                if len(error_summary) < 15:  # Increased limit to capture more errors
                    error_summary.append(f"🚨 {line.strip()}")

        # Build the summary
        summary_parts = []

        if build_status:
            summary_parts.append(build_status)

        if test_summary:
            summary_parts.append(test_summary)

        if compilation_info:
            summary_parts.append(compilation_info)

        if error_summary:
            summary_parts.append("Key Errors:")
            summary_parts.extend(error_summary[:10])  # Show more errors for better debugging
            if len(error_summary) > 10:
                summary_parts.append(f"... and {len(error_summary) - 10} more errors")

        # If we found key info, return it; otherwise return truncated original
        if summary_parts:
            key_info = "\n".join(summary_parts)

            # Add a sample of the raw output for context
            if len(output) > 1000:
                # Add first and last few lines for context
                first_lines = "\n".join(lines[:10])
                last_lines = "\n".join(lines[-10:])

                full_summary = (
                    f"Maven Build Summary:\n{key_info}\n\n"
                    f"Build Output (first 10 lines):\n{first_lines}\n\n"
                    f"... [full output truncated, {len(lines)} total lines] ...\n\n"
                    f"Build Output (last 10 lines):\n{last_lines}\n\n"
                    f"💡 Use 'bash' with 'grep' to search for specific errors or patterns in the full output."
                )
                return full_summary
            else:
                return f"Maven Build Summary:\n{key_info}\n\nFull Output:\n{output}"

        return output

    def _extract_bash_key_info(self, output: str) -> str:
        """Extract key information from bash command output."""
        if not output or len(output) <= self.max_output_length:
            return output

        lines = output.split("\n")

        # For error cases, prioritize error messages
        error_lines = []
        warning_lines = []
        info_lines = []

        for line in lines:
            line_lower = line.lower()
            if any(
                error_word in line_lower
                for error_word in [
                    "error:",
                    "failed:",
                    "cannot",
                    "no such",
                    "permission denied",
                    "not found",
                ]
            ):
                error_lines.append(line)
            elif any(warning_word in line_lower for warning_word in ["warning:", "warn:"]):
                warning_lines.append(line)
            elif line.strip():  # Non-empty lines
                info_lines.append(line)

        # Build summary
        summary_parts = []

        # Add first few lines for context
        summary_parts.append("Command output (first 15 lines):")
        summary_parts.extend(lines[:15])

        if error_lines:
            summary_parts.append(f"\n🚨 Errors found ({len(error_lines)} total):")
            summary_parts.extend(error_lines[:5])  # Show first 5 errors
            if len(error_lines) > 5:
                summary_parts.append(f"... and {len(error_lines) - 5} more errors")

        if warning_lines:
            summary_parts.append(f"\n⚠️ Warnings found ({len(warning_lines)} total):")
            summary_parts.extend(warning_lines[:3])  # Show first 3 warnings

        # Add last few lines
        if len(lines) > 20:
            summary_parts.append(f"\n... [middle content truncated, {len(lines)} total lines] ...")
            summary_parts.append("\nCommand output (last 10 lines):")
            summary_parts.extend(lines[-10:])

        summary_parts.append(
            f"\n💡 Full output has {len(lines)} lines. Use 'grep' to search for specific patterns."
        )

        return "\n".join(summary_parts)

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given parameters."""
        pass

    def _validate_parameters(self, kwargs: Dict[str, Any]) -> None:
        """Validate parameters and raise ToolError if invalid."""
        required_params = self._parameter_schema.get("required", [])
        provided_params = set(kwargs.keys())

        # Check for missing required parameters
        missing_params = [p for p in required_params if p not in provided_params]
        if missing_params:
            raise ToolError(
                message=f"Missing required parameters: {', '.join(missing_params)}",
                category="validation",
                error_code="MISSING_PARAMETERS",
                suggestions=[
                    f"Provide the missing parameters: {', '.join(missing_params)}",
                    f"Use the parameter schema to understand required parameters",
                    f"Example usage: {self.name}({', '.join(f'{p}=<value>' for p in required_params)})",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
                details={"missing_parameters": missing_params},
                retryable=True,
            )

        # Check for unexpected parameters — unless the schema explicitly allows
        # pass-through parameters (facades forward **kwargs to delegates whose
        # full vocabularies are wider than the documented surface).
        if self._parameter_schema.get("additionalProperties"):
            return

        expected_params = set(self._parameter_schema.get("properties", {}).keys())
        unexpected_params = provided_params - expected_params
        if unexpected_params:
            raise ToolError(
                message=f"Unexpected parameters: {', '.join(unexpected_params)}",
                category="validation",
                error_code="UNEXPECTED_PARAMETERS",
                suggestions=[
                    f"Remove unexpected parameters: {', '.join(unexpected_params)}",
                    f"Valid parameters are: {', '.join(expected_params)}",
                    f"Check the parameter schema for correct parameter names",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
                details={"unexpected_parameters": list(unexpected_params)},
                retryable=True,
            )

    def safe_execute(self, **kwargs) -> ToolResult:
        """Execute the tool with enhanced error handling and validation."""
        import time

        start_time = time.time()

        try:
            logger.info(f"Executing tool: {self.name}")

            # Validate parameters - will raise ToolError if invalid
            self._validate_parameters(kwargs)

            result = self.execute(**kwargs)

            # Apply output truncation if needed
            if result.succeeded and result.output:
                original_length = len(result.output)
                result.output = self._truncate_output(result.output, self.name)

                # Update metadata with truncation info
                if len(result.output) < original_length:
                    result.metadata["output_truncated"] = True
                    result.metadata["original_length"] = original_length
                    result.metadata["truncated_length"] = len(result.output)

            # Add execution duration to successful results
            duration = time.time() - start_time
            result.metadata["duration_ms"] = duration * 1000

            self._log_execution(kwargs, result)
            return result

        except OutputPersistenceError as exc:
            raise exc.attach_invocation(self.name, kwargs)

        except ToolError as e:
            # Handle tool errors using to_result() method
            duration = time.time() - start_time
            result = e.to_result(duration=duration)

            # Log to centralized error logger
            try:
                from sag.agent.error_logger import ErrorLogger

                error_logger = ErrorLogger.get_instance()
                error_logger.log_tool_error(
                    tool_name=self.name,
                    error_message=e.message,
                    category=e.category,
                    error_code=e.error_code,
                    suggestions=e.suggestions,
                    retryable=e.retryable,
                    details=e.details,
                    context={"parameters": kwargs},
                )
            except Exception as log_error:
                logger.warning(f"Failed to log error to centralized logger: {log_error}")

            self._log_execution(kwargs, result)
            return result

        except Exception as e:
            # Handle unexpected errors with proper categorization
            duration = time.time() - start_time
            error_msg = f"Tool {self.name} crashed: {str(e)}"
            logger.error(error_msg, exc_info=True)

            # Create a system-level ToolError and convert it
            system_error = ToolError(
                message=error_msg,
                category="system",
                error_code="UNEXPECTED_ERROR",
                suggestions=[
                    "Check the tool parameters for correctness",
                    "Review the tool documentation",
                    "Try a simpler version of the command first",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
                details={"exception_type": type(e).__name__, "exception_str": str(e)},
                retryable=False,
            )

            result = system_error.to_result(
                duration=duration,
                invocation_status=InvocationStatus.CRASHED,
            )

            # Log system error to centralized logger
            try:
                from sag.agent.error_logger import ErrorLogger

                error_logger = ErrorLogger.get_instance()
                error_logger.log_tool_error(
                    tool_name=self.name,
                    error_message=system_error.message,
                    category="system",
                    error_code=system_error.error_code,
                    suggestions=system_error.suggestions,
                    retryable=system_error.retryable,
                    details=system_error.details,
                    context={"parameters": kwargs, "exception_type": type(e).__name__},
                )
            except Exception as log_error:
                logger.warning(f"Failed to log system error to centralized logger: {log_error}")

            self._log_execution(kwargs, result)
            return result

    def _log_execution(self, params: Dict[str, Any], result: ToolResult) -> None:
        """Log tool execution for debugging."""
        logger.debug(f"Tool {self.name} executed with params: {params}")
        if result.invocation_status is InvocationStatus.PENDING:
            logger.info(f"Tool {self.name} is pending: {result.poll_ref}")
        elif result.succeeded:
            output_info = f"{len(result.output)} chars"
            if result.metadata.get("output_truncated"):
                output_info += f" (truncated from {result.metadata.get('original_length', 0)})"
            logger.debug(f"Tool {self.name} succeeded: {output_info}")
        elif result.operation_outcome is OperationOutcome.FAILED:
            logger.warning(f"Tool {self.name} failed: {result.error}")
            if result.suggestions:
                logger.info(f"Suggestions for {self.name}: {result.suggestions}")
        else:
            logger.info(f"Tool {self.name} completed with {result.operation_outcome.value} outcome")

    def get_schema(self) -> Dict[str, Any]:
        """Get the tool schema for the LLM."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._parameter_schema,
            "usage_example": self.get_usage_example(),
        }

    def get_parameter_schema(self) -> Dict[str, Any]:
        """Return this tool's JSON parameter schema for function calling."""
        schema_method = self._get_parameters_schema
        if getattr(schema_method, "__func__", None) is not BaseTool._get_parameters_schema:
            return schema_method()
        return self._parameter_schema

    def get_usage_example(self) -> str:
        """Get a usage example for this tool."""
        required_params = self._parameter_schema.get("required", [])
        optional_params = [
            p
            for p in self._parameter_schema.get("properties", {}).keys()
            if p not in required_params
        ]

        example = f"{self.name}("
        param_examples = []

        for param in required_params:
            param_examples.append(f'{param}="<required_value>"')

        for param in optional_params[:2]:  # Show max 2 optional params
            param_examples.append(f'{param}="<optional_value>"')

        example += ", ".join(param_examples)
        example += ")"

        return example

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return self._parameter_schema
