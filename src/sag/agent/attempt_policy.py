"""Mechanical test-attempt policy for setup phase termination.

The physical validator decides what test evidence means.  This module answers
the narrower control question: when the build is test-ready and the survey
provides a concrete coordinate, has the *current* test attempt reached a real
runner at one of those coordinates?

Keeping this policy outside the validator preserves the role boundary:
execution receipts govern control flow; physical artifacts govern verdicts.
"""

from __future__ import annotations

import json
import posixpath
import shlex
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

from .evidence_state import RunEvidenceState, ToolObservation
from .forced_build_graph import (
    probe_forced_build_backend,
    verify_forced_candidate_build_graph,
)

CandidateResolutionStatus = Literal[
    "available",
    "manifest_unreadable",
    "coordinates_missing",
    "unsafe_coordinates",
]


@dataclass(frozen=True, slots=True)
class TestAttemptRequirement:
    """The one exact harness action required before test may terminate."""

    root: str | None
    system: str | None
    required_action: Mapping[str, Any]
    reason_code: str = "test_receipt_missing"
    parent_execution_id: str | None = None

    def action_text(self) -> str:
        tool = self.required_action["tool"]
        params = self.required_action["params"]
        if tool == "search":
            return f"search(target={params['target']!r})"
        if tool == "project":
            return "project(action='analyze')"
        return f"build(action='test', working_directory={params['working_directory']!r})"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "system": self.system,
            "reason_code": self.reason_code,
            "parent_execution_id": self.parent_execution_id,
            "required_action": {
                "tool": self.required_action["tool"],
                "params": dict(self.required_action["params"]),
            },
        }


@dataclass(frozen=True, slots=True)
class TestCandidateResolution:
    """Survey-coordinate read result, including fail-closed failure states."""

    status: CandidateResolutionStatus
    candidates: tuple[TestAttemptRequirement, ...] = ()
    project_root: str | None = None
    workspace_root: str | None = None
    primary: TestAttemptRequirement | None = None

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "workspace_root": self.workspace_root,
            "project_root": self.project_root,
            "candidates": [
                {"root": candidate.root, "system": candidate.system}
                for candidate in self.candidates
            ],
            "primary": (
                {"root": self.primary.root, "system": self.primary.system}
                if self.primary is not None
                else None
            ),
        }

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> "TestCandidateResolution":
        status = str(value.get("status") or "")
        if status not in {
            "available",
            "manifest_unreadable",
            "coordinates_missing",
            "unsafe_coordinates",
        }:
            raise ValueError(f"invalid test-candidate status: {status!r}")
        workspace_root = _normalized_absolute_path(value.get("workspace_root"))
        project_root = _normalized_root(
            value.get("project_root"),
            None,
            enforce_project_boundary=False,
            workspace_root=workspace_root,
        )
        if status == "available" and (workspace_root is None or project_root is None):
            raise ValueError(
                "available test-candidate snapshot requires workspace_root and project_root"
            )
        if project_root is not None and workspace_root is None:
            raise ValueError("test-candidate snapshot project_root requires workspace_root")
        candidates: list[TestAttemptRequirement] = []
        for item in value.get("candidates") or ():
            if not isinstance(item, Mapping):
                raise ValueError("test-candidate snapshot entries must be mappings")
            root = _normalized_root(
                item.get("root"),
                project_root,
                workspace_root=workspace_root,
            )
            system = _normalized_system(item.get("system"))
            if root is None or system is None:
                raise ValueError("test-candidate snapshot requires valid root and system")
            candidates.append(_candidate_requirement(root, system))
        if status == "available" and not candidates:
            raise ValueError("available test-candidate snapshot cannot be empty")
        if status != "available" and candidates:
            raise ValueError("failed test-candidate snapshot cannot contain coordinates")
        # Restore the primary coordinate so replay verification enforces the
        # same discharge policy as the live path (spec §3.4-6). Pre-primary
        # snapshots carry no "primary" key and legally rehydrate to None.
        primary: TestAttemptRequirement | None = None
        primary_item = value.get("primary")
        if primary_item is not None:
            if not isinstance(primary_item, Mapping):
                raise ValueError("test-candidate snapshot primary must be a mapping")
            primary_root = _normalized_root(
                primary_item.get("root"),
                project_root,
                workspace_root=workspace_root,
            )
            primary_system = _normalized_system(primary_item.get("system"))
            if primary_root is None or primary_system is None:
                raise ValueError(
                    "test-candidate snapshot primary requires valid root and system"
                )
            primary = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.root == primary_root and candidate.system == primary_system
                ),
                None,
            )
            if primary is None:
                raise ValueError(
                    "test-candidate snapshot primary must be one of its candidates"
                )
        return cls(
            status=status,  # type: ignore[arg-type]
            candidates=tuple(candidates),
            project_root=project_root,
            workspace_root=workspace_root,
            primary=primary,
        )


def _normalized_absolute_path(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or not raw.startswith("/") or "\x00" in raw or "\n" in raw:
        return None
    return posixpath.normpath(raw)


def _is_contained(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _normalized_root(
    value: Any,
    project_root: str | None,
    *,
    enforce_project_boundary: bool = True,
    workspace_root: str | None = "/workspace",
) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not raw.startswith("/"):
        if not project_root:
            return None
        raw = posixpath.join(project_root, raw)
    normalized = _normalized_absolute_path(raw)
    if normalized is None:
        return None
    if workspace_root is not None and not _is_contained(normalized, workspace_root):
        return None
    if enforce_project_boundary and project_root and not _is_contained(normalized, project_root):
        return None
    return normalized


def _normalized_system(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    aliases = {
        "mvn": "maven",
        "gradlew": "gradle",
        "python": "pytest",
        "py.test": "pytest",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"maven", "gradle", "pytest"} else None


def _candidate_requirement(root: str, system: str) -> TestAttemptRequirement:
    return TestAttemptRequirement(
        root=root,
        system=system,
        required_action={
            "tool": "build",
            "params": {
                "action": "test",
                "working_directory": root,
            },
        },
    )


def _resolved_realpath(orchestrator: Any, path: str) -> str | None:
    """Resolve one container path without permitting manifest shell injection."""
    try:
        result = orchestrator.execute_command(f"realpath -e -- {shlex.quote(path)}")
    except Exception:
        return None
    if not isinstance(result, Mapping) or not result.get("success"):
        return None
    lines = str(result.get("output") or "").splitlines()
    if len(lines) != 1:
        return None
    return _normalized_absolute_path(lines[0])


def resolve_survey_test_candidates(orchestrator: Any) -> TestCandidateResolution:
    """Read test coordinates without collapsing I/O/schema failure into absence."""
    if orchestrator is None:
        return TestCandidateResolution(status="manifest_unreadable")
    workspace_root = _resolved_realpath(orchestrator, "/workspace")
    if workspace_root is None:
        return TestCandidateResolution(status="manifest_unreadable")
    try:
        result = orchestrator.execute_command(f"cat {REQUIREMENTS_PATH}")
        if not isinstance(result, Mapping) or not result.get("success"):
            return TestCandidateResolution(status="manifest_unreadable")
        manifest = json.loads(str(result.get("output") or ""))
    except Exception:
        return TestCandidateResolution(status="manifest_unreadable")
    if not isinstance(manifest, Mapping):
        return TestCandidateResolution(status="manifest_unreadable")

    survey = manifest.get("survey") or {}
    if not isinstance(survey, Mapping):
        survey = {}
    manifest_project_root = _normalized_root(
        survey.get("project_path") or manifest.get("project_path") or manifest.get("root"),
        None,
        enforce_project_boundary=False,
        workspace_root="/workspace",
    )
    if manifest_project_root is None:
        # An absolute test_root is not enough: without the current survey root
        # there is no boundary proving which checkout that coordinate belongs to.
        return TestCandidateResolution(
            status="coordinates_missing",
            workspace_root=workspace_root,
        )
    project_root = _resolved_realpath(orchestrator, manifest_project_root)
    if project_root is None:
        return TestCandidateResolution(
            status="manifest_unreadable",
            workspace_root=workspace_root,
        )
    if not _is_contained(project_root, workspace_root):
        return TestCandidateResolution(
            status="unsafe_coordinates",
            workspace_root=workspace_root,
        )
    raw_candidates: list[tuple[Any, Any, bool]] = []
    islands = manifest.get("test_islands") or ()
    if not isinstance(islands, (list, tuple)):
        return TestCandidateResolution(
            status="coordinates_missing",
            project_root=project_root,
            workspace_root=workspace_root,
        )
    # The manifest test_root/test_system pair is the PRIMARY coordinate and
    # is processed first (spec §3.4-6): auxiliary islands may add evidence
    # but can never substitute for it.
    raw_candidates.append((manifest.get("test_root"), manifest.get("test_system"), True))
    for island in islands:
        if isinstance(island, Mapping):
            raw_candidates.append(
                (island.get("root"), island.get("system") or manifest.get("test_system"), False)
            )

    candidates: list[TestAttemptRequirement] = []
    primary: TestAttemptRequirement | None = None
    seen: set[tuple[str, str]] = set()
    for raw_root, raw_system, is_primary in raw_candidates:
        system = _normalized_system(raw_system)
        if not raw_root or system is None:
            continue
        lexical_root = _normalized_root(
            raw_root,
            manifest_project_root,
            workspace_root="/workspace",
        )
        if lexical_root is None:
            return TestCandidateResolution(
                status="unsafe_coordinates",
                project_root=project_root,
                workspace_root=workspace_root,
            )
        root = _resolved_realpath(orchestrator, lexical_root)
        if root is None:
            return TestCandidateResolution(
                status="manifest_unreadable",
                project_root=project_root,
                workspace_root=workspace_root,
            )
        if not _is_contained(root, project_root):
            return TestCandidateResolution(
                status="unsafe_coordinates",
                project_root=project_root,
                workspace_root=workspace_root,
            )
        if probe_forced_build_backend(orchestrator, root) != system:
            return TestCandidateResolution(
                status="unsafe_coordinates",
                project_root=project_root,
                workspace_root=workspace_root,
            )
        graph_boundary = verify_forced_candidate_build_graph(
            orchestrator,
            project_root=project_root,
            candidate_root=root,
            system=system,
        )
        if not graph_boundary.verified:
            return TestCandidateResolution(
                status="unsafe_coordinates",
                project_root=project_root,
                workspace_root=workspace_root,
            )
        requirement = _candidate_requirement(root, system)
        if (root, system) in seen:
            if is_primary and primary is None:
                primary = next(
                    (c for c in candidates if c.root == root and c.system == system),
                    None,
                )
            continue
        seen.add((root, system))
        candidates.append(requirement)
        if is_primary and primary is None:
            primary = requirement
    if not candidates:
        return TestCandidateResolution(
            status="coordinates_missing",
            project_root=project_root,
            workspace_root=workspace_root,
        )
    return TestCandidateResolution(
        status="available",
        candidates=tuple(candidates),
        project_root=project_root,
        workspace_root=workspace_root,
        primary=primary,
    )


def survey_test_candidates(orchestrator: Any) -> tuple[TestAttemptRequirement, ...]:
    """Compatibility view of available survey-grounded coordinates."""
    return resolve_survey_test_candidates(orchestrator).candidates


def _is_test_dispatch(observation: ToolObservation) -> bool:
    params = observation.params
    if observation.tool_name == "python":
        return str(params.get("operation") or "").strip().lower() == "test"
    if observation.tool_name == "gradle":
        tasks = str(params.get("tasks") or "").strip().lower().split()
        return any(task == "test" or task.endswith(":test") for task in tasks)
    if observation.tool_name == "maven":
        command = str(params.get("command") or "").strip().lower()
        return any(
            token in {"test", "verify"} or token.endswith(":test") or token.endswith(":verify")
            for token in command.split()
        )
    if observation.tool_name == "build":
        return str(params.get("action") or "").strip().lower() == "test"
    return False


def _tool_system(tool_name: str) -> str | None:
    return {
        "maven": "maven",
        "gradle": "gradle",
        "python": "pytest",
    }.get(tool_name)


def test_execution_binding(
    tool_name: str,
    params: Mapping[str, Any],
    result: Any,
) -> tuple[str | None, str | None]:
    """Return the canonical root/backend identity of one physical execution."""
    root = _normalized_root(
        params.get("working_directory"),
        None,
        enforce_project_boundary=False,
        workspace_root=None,
    )
    actual_system = _tool_system(tool_name)
    if tool_name == "build":
        actual_system = _normalized_system((result.facts or {}).get("system"))
    return root, actual_system


def test_execution_matches_candidate(
    tool_name: str,
    params: Mapping[str, Any],
    result: Any,
    candidate: TestAttemptRequirement,
) -> bool:
    root, system = test_execution_binding(tool_name, params, result)
    return root == candidate.root and system == candidate.system


def _matches_candidate(
    observation: ToolObservation,
    candidates: tuple[TestAttemptRequirement, ...],
) -> bool:
    if not candidates:
        return False
    # Backend identity must agree with the survey rather than merely looking
    # test-shaped. A facade receipt is accepted only when it reports the
    # backend in verified facts; missing identity therefore fails closed.
    return any(
        test_execution_matches_candidate(
            observation.tool_name,
            observation.params,
            observation.result,
            candidate,
        )
        for candidate in candidates
    )


def _terminal_runner_receipt(
    observation: ToolObservation,
    candidates: tuple[TestAttemptRequirement, ...],
) -> bool:
    if (
        not _is_test_dispatch(observation)
        or not observation.result.is_terminal
        or not _matches_candidate(observation, candidates)
    ):
        return False
    metadata = observation.result.metadata or {}
    # A rendered command is intent, not physical execution. Docker dispatch can
    # fail after a backend has constructed that command, so the backend must
    # explicitly attest that the runner crossed the dispatch boundary.
    return bool(
        metadata.get("runner_dispatched") is True and str(metadata.get("command") or "").strip()
    )


def _pending_test_dispatch(
    observation: ToolObservation,
    candidates: tuple[TestAttemptRequirement, ...],
) -> bool:
    metadata = observation.result.metadata or {}
    return bool(
        _is_test_dispatch(observation)
        and _matches_candidate(observation, candidates)
        and not observation.result.is_terminal
        and observation.result.poll_ref
        and metadata.get("runner_dispatched") is True
        and str(metadata.get("command") or "").strip()
    )


def _terminal_poll_receipt(
    observation: ToolObservation,
    pending_by_ref: Mapping[str, ToolObservation],
) -> bool:
    if observation.tool_name != "search" or not observation.result.is_terminal:
        return False
    target = str(observation.params.get("target") or "").strip()
    pending = pending_by_ref.get(target)
    if pending is None:
        return False
    metadata = observation.result.metadata or {}
    pending_metadata = pending.result.metadata or {}
    result_poll_ref = str(observation.result.poll_ref or "").strip()
    result_job = str(metadata.get("job_id") or "").strip()
    pending_job = str(pending_metadata.get("job_id") or "").strip()
    return bool(
        result_poll_ref == target
        and metadata.get("dispatch_status") == "completed_detached"
        and (not result_job or not pending_job or result_job == pending_job)
    )


def _attempt_observations(
    state: RunEvidenceState,
    attempt_id: str | None,
) -> tuple[ToolObservation, ...]:
    if not attempt_id:
        return ()
    return tuple(
        observation
        for observation in state.tool_observations
        if observation.source_phase == "test" and observation.source_attempt_id == attempt_id
    )


def has_test_candidate_refresh_receipt(
    state: RunEvidenceState,
    *,
    attempt_id: str | None,
) -> bool:
    """Whether this attempt already spent its one harness-owned survey refresh."""
    return any(
        observation.tool_name == "project"
        and str(observation.params.get("action") or "").strip().lower() == "analyze"
        and observation.result.is_terminal
        for observation in _attempt_observations(state, attempt_id)
    )


def terminal_test_receipts(
    state: RunEvidenceState,
    *,
    attempt_id: str | None,
    candidates: tuple[TestAttemptRequirement, ...],
) -> tuple[ToolObservation, ...]:
    """Return candidate-bound terminal receipts from this concrete attempt."""
    observations = _attempt_observations(state, attempt_id)
    direct = tuple(
        observation
        for observation in observations
        if _terminal_runner_receipt(observation, candidates)
    )
    pending_by_ref = {
        str(observation.result.poll_ref): observation
        for observation in observations
        if _pending_test_dispatch(observation, candidates)
    }
    terminal_polls = tuple(
        observation
        for observation in observations
        if _terminal_poll_receipt(observation, pending_by_ref)
    )
    return (*direct, *terminal_polls)


def forced_test_refusal_receipts(
    state: RunEvidenceState,
    *,
    attempt_id: str | None,
    candidates: tuple[TestAttemptRequirement, ...],
) -> tuple[ToolObservation, ...]:
    """Return bounded harness refusals; these are control receipts, never tests."""
    refusals: list[ToolObservation] = []
    for observation in _attempt_observations(state, attempt_id):
        marker = (observation.result.metadata or {}).get("harness_forced_test_attempt")
        runner_dispatched = (observation.result.metadata or {}).get("runner_dispatched") is True
        command_present = bool(
            str((observation.result.metadata or {}).get("command") or "").strip()
        )
        actual_root, actual_system = test_execution_binding(
            observation.tool_name,
            observation.params,
            observation.result,
        )
        expected_candidate = (
            next(
                (
                    candidate
                    for candidate in candidates
                    if marker.get("root") == candidate.root
                    and marker.get("system") == candidate.system
                ),
                None,
            )
            if isinstance(marker, Mapping)
            else None
        )
        if (
            not isinstance(marker, Mapping)
            or not _is_test_dispatch(observation)
            or not observation.result.is_terminal
            or expected_candidate is None
        ):
            continue
        disposition = marker.get("disposition")
        if (
            marker.get("phase") != "test"
            or marker.get("source_attempt_id") != attempt_id
            or marker.get("actual_root") != actual_root
            or marker.get("actual_system") != actual_system
            or not str(marker.get("reason_code") or "").strip()
        ):
            continue
        if disposition == "no_runner_dispatch":
            if runner_dispatched:
                continue
        elif disposition == "candidate_mismatch":
            if (
                not runner_dispatched
                or not command_present
                or _matches_candidate(observation, candidates)
            ):
                continue
        else:
            continue
        refusals.append(observation)
    return tuple(refusals)


def _pending_test_dispatches(
    state: RunEvidenceState,
    *,
    attempt_id: str | None,
    candidates: tuple[TestAttemptRequirement, ...],
) -> tuple[ToolObservation, ...]:
    observations = _attempt_observations(state, attempt_id)
    terminal_targets = {
        str(observation.params.get("target") or "").strip()
        for observation in observations
        if _terminal_poll_receipt(
            observation,
            {
                str(candidate.result.poll_ref): candidate
                for candidate in observations
                if _pending_test_dispatch(candidate, candidates)
            },
        )
    }
    pending: list[ToolObservation] = []
    seen: set[str] = set()
    for observation in observations:
        poll_ref = str(observation.result.poll_ref or "").strip()
        if (
            _pending_test_dispatch(observation, candidates)
            and poll_ref
            and poll_ref not in terminal_targets
            and poll_ref not in seen
        ):
            seen.add(poll_ref)
            pending.append(observation)
    return tuple(pending)


def required_test_attempt(
    state: RunEvidenceState | None,
    orchestrator: Any,
    *,
    phase: str | None,
    attempt_id: str | None,
    resolution: TestCandidateResolution | None = None,
) -> TestAttemptRequirement | None:
    """Return the exact missing harness action, or ``None`` when closure is legal."""
    if state is None or phase != "test":
        return None
    if state.fact_value("build.test_entry_ready") is not True:
        return None
    resolved = resolution or resolve_survey_test_candidates(orchestrator)
    if resolved.status != "available":
        if has_test_candidate_refresh_receipt(state, attempt_id=attempt_id):
            return None
        return TestAttemptRequirement(
            root=resolved.project_root,
            system=None,
            required_action={
                "tool": "project",
                "params": {"action": "analyze"},
            },
            reason_code=resolved.status,
        )
    candidates = resolved.candidates
    primary = resolved.primary
    gate_candidates = (primary,) if primary is not None else candidates
    if terminal_test_receipts(state, attempt_id=attempt_id, candidates=gate_candidates):
        return None
    if forced_test_refusal_receipts(
        state,
        attempt_id=attempt_id,
        candidates=gate_candidates,
    ):
        return None
    pending = _pending_test_dispatches(
        state,
        attempt_id=attempt_id,
        candidates=gate_candidates,
    )
    if pending:
        observation = pending[0]
        poll_ref = str(observation.result.poll_ref)
        candidate = next(
            candidate for candidate in candidates if _matches_candidate(observation, (candidate,))
        )
        return TestAttemptRequirement(
            root=candidate.root,
            system=candidate.system,
            required_action={
                "tool": "search",
                "params": {"target": poll_ref},
            },
            reason_code="pending_test_poll_required",
            parent_execution_id=observation.execution_id,
        )
    return primary if primary is not None else candidates[0]


_LOCAL_PREREQUISITE_SIGNATURES = (
    "ensurepip is not available",
    "command not found",
    "no module named pip",
    "no module named ensurepip",
)

_BUILD_RUNNER_TOOLS = frozenset({"build", "maven", "gradle", "python"})


def local_prerequisite_signature(text: str) -> str | None:
    """Match text against known local, mechanically repairable prerequisites."""
    lowered = (text or "").lower()
    for signature in _LOCAL_PREREQUISITE_SIGNATURES:
        if signature in lowered:
            return signature
    return None


def has_build_attempt_receipt(
    state: RunEvidenceState | None, *, attempt_id: str | None
) -> bool:
    """One real build-runner dispatch in this build attempt (terminal or not)."""
    if state is None or not attempt_id:
        return False
    for observation in state.tool_observations:
        if observation.source_phase != "build":
            continue
        if observation.source_attempt_id != attempt_id:
            continue
        if observation.tool_name not in _BUILD_RUNNER_TOOLS:
            continue
        metadata = observation.result.metadata or {}
        if metadata.get("runner_dispatched") is True and str(
            metadata.get("command") or ""
        ).strip():
            return True
    return False


def _read_requirements_manifest(orchestrator: Any) -> Mapping[str, Any] | None:
    """Read the survey manifest; ``None`` when absent, unreadable, or malformed."""
    manifest: Any = None
    try:
        result = orchestrator.execute_command(f"cat {REQUIREMENTS_PATH}")
        if isinstance(result, Mapping) and result.get("success"):
            manifest = json.loads(str(result.get("output") or ""))
    except Exception:
        manifest = None
    return manifest if isinstance(manifest, Mapping) else None


def build_attempt_requirement(
    state: RunEvidenceState | None,
    orchestrator: Any,
    *,
    phase: str | None,
    attempt_id: str | None,
) -> str | None:
    """Reject build closure without one real build attempt (spec §3.4-7).

    Fail-closed: an unreadable survey manifest never proves a no-target
    project, so it still requires an attempt."""
    if state is None or phase != "build":
        return None
    if has_build_attempt_receipt(state, attempt_id=attempt_id):
        return None
    manifest = _read_requirements_manifest(orchestrator)
    if manifest:
        islands = manifest.get("build_islands") or ()
        build_system = manifest.get("build_system") or (
            (manifest.get("build_recommendation") or {}).get("build_system")
        )
        if not islands and not build_system:
            return None  # survey-proven no-target project
    return (
        "Build phase cannot terminate before one real build attempt receipt. "
        "NEXT REQUIRED ACTION: build(action='compile') at the surveyed build "
        "root, or build(action='deps') when dependencies are the failure. "
        "Missing OS packages or venv modules are local repairable "
        "prerequisites, not external blockers."
    )


@dataclass(frozen=True, slots=True)
class IncompatibleDomainEdge:
    """One ``version_incompatible`` domain edge (Stage C schema v1) as a fact.

    Independence is a conclusion of the coordinate graph, never a directory
    fact, so the only edges this policy carries are the ones that already
    BLOCK a consumer before any attempt.  ``detail`` is the analyzer's
    rendered mismatch and is reproduced verbatim — the policy never re-derives
    or paraphrases a coordinate.
    """

    consumer: str
    producer: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UntriedIslandsRequirement:
    """Surveyed build islands a closure claim would abandon unattempted.

    Spec §3.4 dropped the mechanical "island attempt queue" and moved its
    guarantee into the gate: closing the build phase while surveyed
    islands are untried is rejected, naming the islands (§3.3).
    """

    roots: tuple[str, ...]
    systems: tuple[str | None, ...] = ()
    # Plan 5 Task C2 (P0-B): incompatible graph edges whose CONSUMER is one of
    # these untried roots. Empty means "the caller supplied no edge info",
    # which is not the same as "the islands are independent" — see message().
    edges: tuple[IncompatibleDomainEdge, ...] = ()

    def action_text(self, index: int = 0) -> str:
        return f"build(action='compile', working_directory={self.roots[index]!r})"

    def suggestions(self) -> list[str]:
        return [self.action_text(index) for index in range(len(self.roots))]

    def blocker_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for edge in self.edges:
            line = f"{edge.consumer} <- {edge.producer}"
            detail = edge.detail.strip()
            lines.append(f"{line}: {detail}" if detail else line)
        return tuple(lines)

    def message(self) -> str:
        """Name the untried islands, and name what already blocks them.

        Ground-truth review 2026-07-26 (§"Unproved independence"): this message
        used to assert "each island builds independently, so one island's
        failure says nothing about the others". Bigtop falsified that — its
        producer builds 3.7.0-SNAPSHOT while two consumers require 3.5/3.6.
        The harness derives independence from the coordinate graph or says
        nothing about it; with edges in hand it names them as blockers instead.
        """
        plural = "" if len(self.roots) == 1 else "s"
        blockers = self.blocker_lines()
        graph_text = (
            "Surveyed dependency edges already name blockers for these "
            f"islands ({'; '.join(blockers)}) — record the mismatch, do not "
            "silently alias. "
            if blockers
            else ""
        )
        return (
            f"Build phase cannot close while {len(self.roots)} surveyed build "
            f"island{plural} carry no build attempt receipt: "
            f"{', '.join(self.roots)}. "
            f"NEXT REQUIRED ACTION: {self.action_text()}. "
            f"{graph_text}"
            "Closure needs receipts: a failed attempt is a receipt, an "
            "untried island is not."
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "reason_code": "untried_build_islands",
            "untried_island_roots": list(self.roots),
            "untried_island_systems": list(self.systems),
            "required_action": {
                "tool": "build",
                "params": {"action": "compile", "working_directory": self.roots[0]},
            },
        }


def _manifest_build_islands(
    manifest: Mapping[str, Any],
) -> tuple[tuple[str, str | None], ...]:
    """Island (root, system) coordinates, deduped, roots normalized absolute."""
    survey = manifest.get("survey")
    project_path = (
        _normalized_absolute_path(survey.get("project_path"))
        if isinstance(survey, Mapping)
        else None
    )
    raw_islands = manifest.get("build_islands")
    if not isinstance(raw_islands, (list, tuple)):
        return ()
    islands: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in raw_islands:
        if not isinstance(item, Mapping):
            continue
        root = _normalized_root(
            item.get("root"),
            project_path,
            enforce_project_boundary=False,
            workspace_root=None,
        )
        if root is None or root in seen:
            continue
        seen.add(root)
        islands.append((root, str(item.get("system") or "").strip().lower() or None))
    return tuple(islands)


def _manifest_incompatible_edges(
    manifest: Mapping[str, Any],
    roots: tuple[str, ...],
) -> tuple[IncompatibleDomainEdge, ...]:
    """``version_incompatible`` edges whose consumer is one of ``roots``.

    Read the way the manifest exposes every other recommendation fact: the
    projected top-level key first, the nested recommendation as the fallback
    (same dual read as ``build_system`` above). Absent graph facts stay absent.
    """
    raw = manifest.get("domain_edges")
    if raw is None:
        recommendation = manifest.get("build_recommendation")
        if isinstance(recommendation, Mapping):
            raw = recommendation.get("domain_edges")
    if not isinstance(raw, (list, tuple)):
        return ()
    wanted = set(roots)
    edges: list[IncompatibleDomainEdge] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("status") or "").strip().lower() != "version_incompatible":
            continue
        consumer = _normalized_absolute_path(item.get("consumer"))
        if consumer is None or consumer not in wanted:
            continue
        producer = _normalized_absolute_path(item.get("producer"))
        edges.append(
            IncompatibleDomainEdge(
                consumer=consumer,
                producer=producer or "",
                detail=str(item.get("detail") or "").strip(),
            )
        )
    return tuple(edges)


def _build_attempt_directories(state: RunEvidenceState) -> tuple[str, ...]:
    """Resolved working directories of every real build-runner dispatch.

    The receipt shape is ``has_build_attempt_receipt``'s (dispatched runner
    plus a rendered command), so a pre-dispatch refusal never counts as an
    attempted island.  The RESOLVED directory is read from result metadata
    first: a bare ``build(action='compile')`` carries no ``working_directory``
    parameter, and only the envelope knows where it actually ran.  Attempts
    are read across phases and attempts because cross-phase repair is legal
    (spec §3.5) — the question is whether the island was ever tried at all.
    """
    directories: list[str] = []
    for observation in state.tool_observations:
        if observation.tool_name not in _BUILD_RUNNER_TOOLS:
            continue
        metadata = getattr(observation.result, "metadata", None) or {}
        if metadata.get("runner_dispatched") is not True:
            continue
        if not str(metadata.get("command") or "").strip():
            continue
        directory = _normalized_absolute_path(
            metadata.get("working_directory")
            or (observation.params or {}).get("working_directory")
        )
        if directory is not None:
            directories.append(directory)
    return tuple(directories)


def untried_islands_requirement(
    state: RunEvidenceState | None,
    orchestrator: Any,
    *,
    phase: str | None,
    signal: str | None,
    outcome: str | None,
) -> UntriedIslandsRequirement | None:
    """Reject giving-up closure while surveyed islands were never attempted.

    Exemptions: no islands surveyed; every island bound to a receipt (success
    or failure — attempted is the bar); and a ``done``/``success`` claim,
    which the physical gate already checks.  An unreadable manifest raises no
    island requirement: Plan 1's attempt gate owns the no-attempt case.
    """
    if state is None or phase != "build":
        return None
    if (
        str(signal or "").strip().lower() == "done"
        and str(outcome or "").strip().lower() == "success"
    ):
        return None
    manifest = _read_requirements_manifest(orchestrator)
    if manifest is None:
        return None
    islands = _manifest_build_islands(manifest)
    if not islands:
        return None
    attempted = _build_attempt_directories(state)
    untried = tuple(
        (root, system)
        for root, system in islands
        if not any(_is_contained(directory, root) for directory in attempted)
    )
    if not untried:
        return None
    roots = tuple(root for root, _ in untried)
    return UntriedIslandsRequirement(
        roots=roots,
        systems=tuple(system for _, system in untried),
        edges=_manifest_incompatible_edges(manifest, roots),
    )


__all__ = [
    "CandidateResolutionStatus",
    "IncompatibleDomainEdge",
    "TestAttemptRequirement",
    "TestCandidateResolution",
    "UntriedIslandsRequirement",
    "build_attempt_requirement",
    "has_build_attempt_receipt",
    "local_prerequisite_signature",
    "required_test_attempt",
    "has_test_candidate_refresh_receipt",
    "forced_test_refusal_receipts",
    "resolve_survey_test_candidates",
    "survey_test_candidates",
    "test_execution_binding",
    "test_execution_matches_candidate",
    "terminal_test_receipts",
    "untried_islands_requirement",
]
