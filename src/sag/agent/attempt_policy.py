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

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "workspace_root": self.workspace_root,
            "project_root": self.project_root,
            "candidates": [
                {"root": candidate.root, "system": candidate.system}
                for candidate in self.candidates
            ],
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
        return cls(
            status=status,  # type: ignore[arg-type]
            candidates=tuple(candidates),
            project_root=project_root,
            workspace_root=workspace_root,
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
    raw_candidates: list[tuple[Any, Any]] = []
    islands = manifest.get("test_islands") or ()
    if not isinstance(islands, (list, tuple)):
        return TestCandidateResolution(
            status="coordinates_missing",
            project_root=project_root,
            workspace_root=workspace_root,
        )
    for island in islands:
        if isinstance(island, Mapping):
            raw_candidates.append(
                (island.get("root"), island.get("system") or manifest.get("test_system"))
            )
    raw_candidates.append((manifest.get("test_root"), manifest.get("test_system")))

    candidates: list[TestAttemptRequirement] = []
    seen: set[tuple[str, str]] = set()
    for raw_root, raw_system in raw_candidates:
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
        if (root, system) in seen:
            continue
        seen.add((root, system))
        candidates.append(_candidate_requirement(root, system))
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
    if terminal_test_receipts(state, attempt_id=attempt_id, candidates=candidates):
        return None
    if forced_test_refusal_receipts(
        state,
        attempt_id=attempt_id,
        candidates=candidates,
    ):
        return None
    pending = _pending_test_dispatches(
        state,
        attempt_id=attempt_id,
        candidates=candidates,
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
    return candidates[0]


__all__ = [
    "CandidateResolutionStatus",
    "TestAttemptRequirement",
    "TestCandidateResolution",
    "required_test_attempt",
    "has_test_candidate_refresh_receipt",
    "forced_test_refusal_receipts",
    "resolve_survey_test_candidates",
    "survey_test_candidates",
    "test_execution_binding",
    "test_execution_matches_candidate",
    "terminal_test_receipts",
]
