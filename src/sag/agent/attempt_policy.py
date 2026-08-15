"""Mechanical test-attempt policy for setup phase termination.

The physical validator decides what test evidence means.  This module answers
the narrower control question: when the build is test-ready and the survey
provides a concrete coordinate, has the *current* test attempt reached a real
runner at one of those coordinates?

Keeping this policy outside the validator preserves the role boundary:
execution receipts govern control flow; physical artifacts govern verdicts.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from sag.tools.internal.build_preflight import read_live_build_requirements

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

ReceiptBindingStatus = Literal[
    "available",
    "run_id_missing",
    "project_root_missing",
    "run_pin_unreadable",
    "run_pin_run_mismatch",
    "target_sha_missing",
]

RunReceiptScopeName = Literal["project_root", "clone_root", "workspace_fallback"]

WORKSPACE_ROOT = "/workspace"
# The provision gate seals this fact with the probed clone directory as its
# evidence ref, which `_record_gate_facts` stores as the fact's provenance.
PROVISION_CLONE_ROOT_FACT = "provision.workspace_ready"


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
                raise ValueError("test-candidate snapshot primary requires valid root and system")
            primary = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.root == primary_root and candidate.system == primary_system
                ),
                None,
            )
            if primary is None:
                raise ValueError("test-candidate snapshot primary must be one of its candidates")
        return cls(
            status=status,  # type: ignore[arg-type]
            candidates=tuple(candidates),
            project_root=project_root,
            workspace_root=workspace_root,
            primary=primary,
        )


@dataclass(frozen=True, slots=True)
class RunReceiptScope:
    """The directory a run-wide receipt count was taken over, and its name.

    The name exists so no reader has to infer from a path whether the count was
    bounded to this project or spans every checkout in the container.
    """

    root: str
    name: RunReceiptScopeName

    def to_metadata(self) -> dict[str, Any]:
        return {"name": self.name, "root": self.root}


@dataclass(frozen=True, slots=True)
class CurrentBuildReceiptScope:
    """The immutable pins a build receipt must match to affect this run."""

    status: ReceiptBindingStatus
    run_id: str
    target_sha: str | None = None
    project_root: str | None = None

    @property
    def available(self) -> bool:
        return (
            self.status == "available"
            and self.target_sha is not None
            and self.project_root is not None
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
        live = read_live_build_requirements(orchestrator)
    except Exception:
        return TestCandidateResolution(status="manifest_unreadable")
    if not live.complete or live.conflict is not None or live.payload is None:
        return TestCandidateResolution(status="manifest_unreadable")
    manifest = live.payload

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


def provisioned_clone_root(state: RunEvidenceState | None) -> str | None:
    """The clone root provision recorded for this run, or ``None``.

    ``_inspect_provision`` probes ``/workspace/<project_name>`` and carries that
    directory as the evidence ref of ``provision.workspace_ready``, so the run
    state already holds the one project boundary an unresolvable survey cannot
    supply. Two refs are not boundaries and are rejected here: the
    ``validator:<phase>:<attempt>`` provenance ``_record_gate_facts`` falls back
    to when a gate carries no refs, and a path outside the container workspace.
    A ref that IS the workspace bounds nothing the fallback does not, so it is
    reported as no clone root rather than as a boundary that narrows anything.
    """
    if state is None:
        return None
    try:
        recorded = state.fact_provenance(PROVISION_CLONE_ROOT_FACT)
    except Exception:
        return None
    root = _normalized_root(
        recorded,
        None,
        enforce_project_boundary=False,
        workspace_root=WORKSPACE_ROOT,
    )
    return root if root is not None and root != WORKSPACE_ROOT else None


def run_test_receipt_scope(
    state: RunEvidenceState | None,
    *,
    project_root: str | None = None,
) -> RunReceiptScope:
    """The directory the run-wide consult counts over, under its own name.

    Ordered by how much the boundary proves about THIS project: the survey's
    resolved project root, else the clone root provision recorded, else the
    whole container workspace. The last is a real widening — it admits any
    checkout sharing the container — so it is named rather than assumed, and
    every caller that seals the count seals which of the three it took.
    """
    resolved = _normalized_absolute_path(project_root)
    if resolved is not None:
        return RunReceiptScope(root=resolved, name="project_root")
    clone_root = provisioned_clone_root(state)
    if clone_root is not None:
        return RunReceiptScope(root=clone_root, name="clone_root")
    return RunReceiptScope(root=WORKSPACE_ROOT, name="workspace_fallback")


def run_test_receipts(
    state: RunEvidenceState | None,
    *,
    project_root: str | None = None,
    scope: RunReceiptScope | None = None,
) -> tuple[ToolObservation, ...]:
    """Return terminal test-bearing receipts from ANY phase/attempt of this run.

    Deliberately candidate-free. Every other receipt predicate here binds to a
    survey candidate, which makes them unreachable in the exact state the close
    path fires: ``unsafe_coordinates`` yields ``candidates=()`` and
    `_matches_candidate` is unconditionally False on an empty tuple. A
    ``build(action=test)`` receipt harvested in the build phase is the same
    physical fact as a test-phase one, so the only scoping left is this run and
    the directory boundary.

    That boundary is :func:`run_test_receipt_scope`. It used to be the resolved
    project root or, whenever coordinates were unresolvable, all of
    ``/workspace`` — the exact state the close path fires in. A terminal test
    dispatch in a sibling checkout or a vendored sample then satisfied "did this
    project's test run?", lifting both the forced analyze and the resolution
    cap. Pass ``scope`` to count over the same boundary a caller is about to
    seal, so the count and the name beside it cannot disagree.
    """
    if state is None:
        return ()
    root = (scope or run_test_receipt_scope(state, project_root=project_root)).root
    receipts: list[ToolObservation] = []
    for observation in state.tool_observations:
        if not _is_test_dispatch(observation) or not observation.result.is_terminal:
            continue
        metadata = observation.result.metadata or {}
        # A rendered command is intent; the backend must attest that the runner
        # crossed the dispatch boundary, exactly as `_terminal_runner_receipt`
        # requires of a candidate-bound one.
        if metadata.get("runner_dispatched") is not True:
            continue
        if not str(metadata.get("command") or "").strip():
            continue
        execution_root, _ = test_execution_binding(
            observation.tool_name,
            observation.params,
            observation.result,
        )
        if execution_root is None or not _is_contained(execution_root, root):
            continue
        receipts.append(observation)
    return tuple(receipts)


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


def test_closure_survey(
    state: RunEvidenceState | None,
    orchestrator: Any,
    *,
    phase: str | None,
    resolution: TestCandidateResolution | None = None,
) -> TestCandidateResolution | None:
    """The ONE survey read the test-closure question needs, or ``None``.

    ``None`` means the question is not asked at all — a non-test phase, or a
    build that never opened a test entry — so no manifest read and no realpath
    probe is spent on it. Callers that need both the requirement AND facts about
    it read the survey here once and hand the same resolution to both: two reads
    of one survey can disagree, and a requirement graded against one set of
    coordinates sealed beside a fact graded against another is the same
    fact/word split spec §2.3 fences elsewhere.
    """
    if state is None or phase != "test":
        return None
    if state.fact_value("build.test_entry_ready") is not True:
        return None
    return resolution or resolve_survey_test_candidates(orchestrator)


def required_test_attempt(
    state: RunEvidenceState | None,
    orchestrator: Any,
    *,
    phase: str | None,
    attempt_id: str | None,
    resolution: TestCandidateResolution | None = None,
) -> TestAttemptRequirement | None:
    """Return the exact missing harness action, or ``None`` when closure is legal."""
    resolved = test_closure_survey(
        state,
        orchestrator,
        phase=phase,
        resolution=resolution,
    )
    if resolved is None or state is None:
        return None
    if resolved.status != "available":
        # An unresolvable coordinate cannot un-run a test that already ran.
        # Without candidates there is nothing to bind a receipt to, so the
        # honest question is the run-wide one: did any dispatch of this run
        # reach a terminal runner? If it did, closure is legal and the forced
        # survey refresh has nothing left to establish.
        if run_test_receipts(state, project_root=resolved.project_root):
            return None
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
_DURABLE_RECEIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_TERMINAL_RECEIPT_OUTCOMES = frozenset({"completed", "failed"})
_PRODUCTION_BUILD_ACTION_PREFIXES = (
    "assemble",
    "build",
    "check",
    "compile",
    "install",
    "jar",
    "native",
    "package",
    "test",
    "verify",
    "war",
    "wheel",
)


def local_prerequisite_signature(text: str) -> str | None:
    """Match text against known local, mechanically repairable prerequisites."""
    lowered = (text or "").lower()
    for signature in _LOCAL_PREREQUISITE_SIGNATURES:
        if signature in lowered:
            return signature
    return None


def has_build_attempt_receipt(
    state: RunEvidenceState | None,
    *,
    attempt_id: str | None,
    orchestrator: Any,
    manifest: Mapping[str, Any] | None,
) -> bool:
    """One current-run durable production receipt in this build attempt."""
    if state is None or not attempt_id or orchestrator is None:
        return False
    scope = resolve_current_build_receipt_scope(
        orchestrator,
        run_id=state.run_id,
        manifest=manifest,
    )
    from .evidence_assessments import read_receipt

    execute = getattr(orchestrator, "execute_command", None)
    if not callable(execute):
        return False
    return bool(
        build_attempt_directories(
            state,
            receipt_loader=lambda receipt_id: read_receipt(execute, receipt_id),
            scope=scope,
            attempt_id=attempt_id,
        )
    )


def _read_requirements_manifest(orchestrator: Any) -> Mapping[str, Any] | None:
    """Read the current host-authorized survey manifest, or ``None``."""
    try:
        live = read_live_build_requirements(orchestrator)
    except Exception:
        return None
    if not live.complete or live.conflict is not None or live.payload is None:
        return None
    return live.payload


@dataclass(frozen=True, slots=True)
class BuildAttemptRequirement:
    """Mechanical fact that no real build receipt exists for this attempt."""

    manifest_status: str
    build_system: str | None = None
    build_islands: tuple[tuple[str, str | None], ...] = ()
    receipt_binding_status: ReceiptBindingStatus | None = None

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reason_code": "build_attempt_missing",
            "terminal_build_receipts": 0,
            "manifest_status": self.manifest_status,
        }
        if self.build_system:
            payload["build_system"] = self.build_system
        if self.build_islands:
            payload["build_islands"] = [
                {"root": root, "system": system} for root, system in self.build_islands
            ]
        if self.receipt_binding_status is not None:
            payload["receipt_binding_status"] = self.receipt_binding_status
        return payload


def build_attempt_requirement(
    state: RunEvidenceState | None,
    orchestrator: Any,
    *,
    phase: str | None,
    attempt_id: str | None,
) -> BuildAttemptRequirement | None:
    """Reject build closure without one real build attempt (spec §3.4-7).

    Fail-closed: an unreadable survey manifest never proves a no-target
    project, so it still requires an attempt."""
    if state is None or phase != "build":
        return None
    manifest = _read_requirements_manifest(orchestrator)
    if has_build_attempt_receipt(
        state,
        attempt_id=attempt_id,
        orchestrator=orchestrator,
        manifest=manifest,
    ):
        return None
    if manifest:
        islands = manifest.get("build_islands") or ()
        build_system = manifest.get("build_system") or (
            (manifest.get("build_recommendation") or {}).get("build_system")
        )
        if not islands and not build_system:
            return None  # survey-proven no-target project
    normalized_islands = _manifest_build_islands(manifest) if manifest else ()
    system = None
    if manifest:
        raw_system = manifest.get("build_system") or (
            (manifest.get("build_recommendation") or {}).get("build_system")
        )
        system = str(raw_system or "").strip().lower() or None
    return BuildAttemptRequirement(
        manifest_status="read" if manifest is not None else "unavailable",
        build_system=system,
        build_islands=normalized_islands,
        receipt_binding_status=resolve_current_build_receipt_scope(
            orchestrator,
            run_id=state.run_id,
            manifest=manifest,
        ).status,
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
    receipt_binding_status: ReceiptBindingStatus = "available"

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
        binding_text = (
            f"Current receipt binding is unknown ({self.receipt_binding_status}); "
            if self.receipt_binding_status != "available"
            else ""
        )
        return (
            f"Build phase cannot close while {len(self.roots)} surveyed build "
            f"island{plural} carry no build attempt receipt: "
            f"{', '.join(self.roots)}. "
            f"{binding_text}"
            f"{graph_text}"
            "Closure needs receipts: a failed attempt is a receipt, an "
            "untried island is not."
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "reason_code": "untried_build_islands",
            "receipt_binding_status": self.receipt_binding_status,
            "untried_island_roots": list(self.roots),
            "untried_island_systems": list(self.systems),
            "incompatible_edges": [
                {
                    "consumer": edge.consumer,
                    "producer": edge.producer,
                    "detail": edge.detail,
                }
                for edge in self.edges
            ],
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


def resolve_current_build_receipt_scope(
    orchestrator: Any,
    *,
    run_id: str,
    manifest: Mapping[str, Any] | None = None,
    workspace_root: str = "/workspace",
    project_root: str | None = None,
) -> CurrentBuildReceiptScope:
    """Resolve the run/checkout/root pins shared by gates and judges.

    A same-run receipt from a changed checkout is historical evidence just as
    surely as a receipt from another run.  Therefore a missing or mismatched
    run pin is an explicit unavailable scope, never permission to omit the
    target check.
    """

    expected_run = str(run_id or "").strip()
    if not expected_run:
        return CurrentBuildReceiptScope(status="run_id_missing", run_id="")

    normalized_workspace = _normalized_absolute_path(workspace_root)
    normalized_project = _normalized_absolute_path(project_root)
    if normalized_project is None and isinstance(manifest, Mapping):
        survey = manifest.get("survey")
        if isinstance(survey, Mapping):
            normalized_project = _normalized_absolute_path(survey.get("project_path"))
        if normalized_project is None:
            roots = tuple(root for root, _ in _manifest_build_islands(manifest))
            if roots:
                try:
                    normalized_project = _normalized_absolute_path(posixpath.commonpath(roots))
                except ValueError:
                    normalized_project = None
    if (
        normalized_project is None
        or normalized_workspace is None
        or not _is_contained(normalized_project, normalized_workspace)
    ):
        return CurrentBuildReceiptScope(
            status="project_root_missing",
            run_id=expected_run,
        )

    pin_path = f"{normalized_workspace.rstrip('/')}/.setup_agent/run-pin.json"
    try:
        from sag.agent.control_events import RunPin
        from sag.agent.evidence_publications import RUN_PIN_LOGICAL_ARTIFACT_ID
        from sag.agent.evidence_records import (
            EvidencePublicationBinding,
            read_live_published_mutable_json_object,
        )

        def validate_run_pin(payload: Mapping[str, Any], _expected_id: str) -> Mapping[str, Any]:
            return RunPin.model_validate(payload).model_dump(mode="json")

        def classify_run_pin(
            payload: Mapping[str, Any], current_run_id: str | None
        ) -> Literal["current", "foreign", "forensic"]:
            try:
                candidate = RunPin.model_validate(payload)
            except (TypeError, ValueError):
                return "current"
            if candidate.run_id is None:
                return "forensic"
            return "current" if candidate.run_id == current_run_id else "foreign"

        pin_read = read_live_published_mutable_json_object(
            orchestrator,
            pin_path,
            record_kind="run_pin",
            record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            validator=validate_run_pin,
            publication_binding=lambda payload: EvidencePublicationBinding(
                run_id=str(payload["run_id"]),
                logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            ),
            record_scope=classify_run_pin,
        )
        if not pin_read.complete or pin_read.conflict is not None or pin_read.payload is None:
            raise ValueError(pin_read.conflict or "current run pin is absent")
        pin = pin_read.payload
    except Exception:
        return CurrentBuildReceiptScope(
            status="run_pin_unreadable",
            run_id=expected_run,
            project_root=normalized_project,
        )
    if not isinstance(pin, Mapping):
        return CurrentBuildReceiptScope(
            status="run_pin_unreadable",
            run_id=expected_run,
            project_root=normalized_project,
        )
    if str(pin.get("run_id") or "").strip() != expected_run:
        return CurrentBuildReceiptScope(
            status="run_pin_run_mismatch",
            run_id=expected_run,
            project_root=normalized_project,
        )
    target_sha = str(pin.get("target_repo_sha") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", target_sha):
        return CurrentBuildReceiptScope(
            status="target_sha_missing",
            run_id=expected_run,
            project_root=normalized_project,
        )
    return CurrentBuildReceiptScope(
        status="available",
        run_id=expected_run,
        target_sha=target_sha,
        project_root=normalized_project,
    )


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


def _production_build_receipt(receipt: Mapping[str, Any]) -> bool:
    """Whether one terminal receipt describes artifact/test-producing work.

    Dependency resolution is useful evidence but it does not try an island's
    compile/test surface.  The effective action is what physically ran; the
    requested action is only a fallback for legacy receipts that predate it.
    """

    action = str(receipt.get("effective_action") or receipt.get("requested_action") or "").lower()
    tokens = tuple(token for token in re.split(r"[^a-z0-9_-]+", action) if token)
    return any(
        token.startswith(prefix) for token in tokens for prefix in _PRODUCTION_BUILD_ACTION_PREFIXES
    )


def current_run_durable_receipt(
    receipt: Mapping[str, Any],
    *,
    receipt_id: str,
    run_id: str,
    target_sha: str,
    project_root: str,
) -> bool:
    """Whether a durable receipt matches this run, checkout, and root.

    Target/root constraints are mandatory: omitting a pin cannot weaken the
    binding law. A producer-specific consumer may add its own tool/action
    predicate after this common epoch and containment check.

    ``outcome`` is the terminal ToolResult outcome.  A runner whose lifecycle
    ended abnormally may still carry ``outcome=failed``; consumers that care
    whether it ended on its own must additionally inspect ``dispatch_terminated``.
    """

    expected_id = str(receipt_id or "").strip()
    expected_run = str(run_id or "").strip()
    if not _DURABLE_RECEIPT_ID.fullmatch(expected_id) or not expected_run:
        return False
    if str(receipt.get("receipt_id") or "").strip() != expected_id:
        return False
    if str(receipt.get("run_id") or "").strip() != expected_run:
        return False
    if str(receipt.get("outcome") or "").strip().lower() not in _TERMINAL_RECEIPT_OUTCOMES:
        return False
    expected_target = str(target_sha or "").strip().lower()
    if (
        not expected_target
        or str(receipt.get("target_sha") or "").strip().lower() != expected_target
    ):
        return False

    normalized_project = _normalized_absolute_path(project_root)
    if normalized_project is None:
        return False
    actual_cwd = _normalized_absolute_path(
        receipt.get("actual_cwd") or receipt.get("working_directory")
    )
    if actual_cwd is None or not _is_contained(actual_cwd, normalized_project):
        return False
    domain_id = str(receipt.get("domain_id") or "").strip()
    if domain_id:
        normalized_domain = _normalized_absolute_path(domain_id)
        if normalized_domain is None or not _is_contained(normalized_domain, normalized_project):
            return False
    return True


def current_run_production_build_receipt(
    receipt: Mapping[str, Any],
    *,
    receipt_id: str,
    run_id: str,
    target_sha: str,
    project_root: str,
) -> bool:
    """Whether a current durable receipt tried artifact/test-producing work."""

    return bool(
        current_run_durable_receipt(
            receipt,
            receipt_id=receipt_id,
            run_id=run_id,
            target_sha=target_sha,
            project_root=project_root,
        )
        and str(receipt.get("tool") or "").strip().lower() in _BUILD_RUNNER_TOOLS
        and _production_build_receipt(receipt)
    )


def build_attempt_directories(
    state: RunEvidenceState,
    *,
    receipt_loader: Callable[[str], Mapping[str, Any] | None],
    scope: CurrentBuildReceiptScope,
    attempt_id: str | None = None,
) -> tuple[str, ...]:
    """Actual cwd of current-run durable production-build receipts.

    This is the single binding law shared by the closure gate and the
    model-visible untried-island projection.  Observation metadata supplies
    only the receipt identifier.  The persisted receipt supplies run epoch,
    terminal outcome, effective action, and actual cwd; raw call parameters and
    a self-reported ``runner_dispatched`` bit have zero authority.
    """

    if not scope.available:
        return ()
    directories: list[str] = []
    for observation in state.tool_observations:
        if attempt_id is not None and (
            observation.source_phase != "build" or observation.source_attempt_id != attempt_id
        ):
            continue
        if observation.tool_name not in _BUILD_RUNNER_TOOLS:
            continue
        metadata = getattr(observation.result, "metadata", None) or {}
        receipt_id = str(metadata.get("receipt_id") or "").strip()
        receipt = receipt_loader(receipt_id)
        if not isinstance(receipt, Mapping):
            continue
        if not current_run_production_build_receipt(
            receipt,
            receipt_id=receipt_id,
            run_id=state.run_id,
            target_sha=scope.target_sha or "",
            project_root=scope.project_root or "",
        ):
            continue
        directory = _normalized_absolute_path(
            receipt.get("actual_cwd") or receipt.get("working_directory")
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
    capped_outcome: str | None = None,
) -> UntriedIslandsRequirement | None:
    """Reject giving-up closure while surveyed islands were never attempted.

    Exemptions: no islands surveyed; every island bound to a receipt (success
    or failure — attempted is the bar); and a ``done`` claim of the outcome the
    physical gate below validates, because that gate checks it.  An unreadable
    manifest raises no island requirement: Plan 1's attempt gate owns the
    no-attempt case.

    That last exemption used to read ``done``/``success`` and nothing else, on
    the stated ground that the physical gate checks a success claim for
    completeness — an untried island cannot hide behind ``success`` because the
    oracle looks.  Plan 8 §3.3 then made ``success`` the ONE outcome the gate
    cannot accept while a job obligation is open, and the ground stopped being
    true: with green evidence, an untried island and one open obligation, this
    rule exempted the only claim the gate would refuse and refused every claim
    the gate would accept, so the phase had NO accepted terminal claim at all and
    the two refusals pointed at each other.

    ``capped_outcome`` is what the caller's gate says it validates in place of
    ``success`` on THIS evidence (``phase_gates.settlement_capped_outcome``,
    which is PARTIAL only when the physical oracle was green and the cap fired on
    it).  It is the same claim the old exemption meant, under the name §3.3 gives
    it, so the leniency does not grow: green physical evidence licensed closure
    with an untried island before this parameter existed, and an open obligation
    on its own licenses nothing — a red or physically partial build still owes
    every surveyed island an attempt, which is the bigtop case the rule exists
    for.  Absent (the default) means the caller has no such gate determination,
    and the rule behaves exactly as it did.
    """
    if state is None or phase != "build":
        return None
    exempt_outcomes = {"success"}
    capped = str(capped_outcome or "").strip().lower()
    if capped:
        exempt_outcomes.add(capped)
    if (
        str(signal or "").strip().lower() == "done"
        and str(outcome or "").strip().lower() in exempt_outcomes
    ):
        return None
    manifest = _read_requirements_manifest(orchestrator)
    if manifest is None:
        return None
    islands = _manifest_build_islands(manifest)
    if not islands:
        return None
    scope = resolve_current_build_receipt_scope(
        orchestrator,
        run_id=state.run_id,
        manifest=manifest,
    )
    from .evidence_assessments import read_receipt

    execute = getattr(orchestrator, "execute_command", None)
    attempted: tuple[str, ...]
    if not callable(execute):
        attempted = ()
    else:
        attempted = build_attempt_directories(
            state,
            receipt_loader=lambda receipt_id: read_receipt(execute, receipt_id),
            scope=scope,
        )
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
        receipt_binding_status=scope.status,
    )


__all__ = [
    "CandidateResolutionStatus",
    "CurrentBuildReceiptScope",
    "IncompatibleDomainEdge",
    "ReceiptBindingStatus",
    "RunReceiptScope",
    "RunReceiptScopeName",
    "TestAttemptRequirement",
    "TestCandidateResolution",
    "UntriedIslandsRequirement",
    "build_attempt_directories",
    "build_attempt_requirement",
    "current_run_production_build_receipt",
    "current_run_durable_receipt",
    "has_build_attempt_receipt",
    "local_prerequisite_signature",
    "provisioned_clone_root",
    "required_test_attempt",
    "resolve_current_build_receipt_scope",
    "has_test_candidate_refresh_receipt",
    "forced_test_refusal_receipts",
    "resolve_survey_test_candidates",
    "run_test_receipt_scope",
    "run_test_receipts",
    "survey_test_candidates",
    "test_execution_binding",
    "test_execution_matches_candidate",
    "terminal_test_receipts",
    "untried_islands_requirement",
]
