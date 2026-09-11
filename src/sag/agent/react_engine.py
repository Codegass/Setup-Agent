"""ReAct Engine for Setup-Agent (SAG)."""

import hashlib
import json
import re
import shlex
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.config import create_agent_logger, create_verbose_logger, get_config
from sag.config.prompt_loader import load_react_engine_prompts
from sag.config.settings import effective_phase_floor
from sag.evidence import InvocationStatus, OperationOutcome
from sag.project_fact_sheet import project_fact_sheet_identity
from sag.tools.base import (
    BaseTool,
    OutputPersistenceError,
    ToolResult,
    UnpersistedToolResult,
    is_output_storage_ref,
    new_execution_id,
)
from sag.ui.events import EventType, UIEvent, UIEventEmitter

from .action_intents import (
    ActionIntent,
    EngineActionIntentFactory,
    bounded_exact_params,
    canonical_params,
    validate_repair_action_affordance,
)
from .attempt_ledger import compact_steps
from .attempt_policy import (
    TestAttemptRequirement,
    TestCandidateResolution,
    build_attempt_directories,
    forced_test_refusal_receipts,
    has_test_candidate_refresh_receipt,
    required_test_attempt,
    resolve_current_build_receipt_scope,
    resolve_survey_test_candidates,
    run_test_receipts,
    test_execution_binding,
    test_execution_matches_candidate,
)
from .context_manager import ContextManager, TaskStatus
from .control_events import (
    CANCELLED_CALL_REFUSAL_CODE,
    WINDOW_DIGEST_MAX_COMPONENTS,
    WINDOW_TRUNCATION_REF,
    ControlEventSink,
    RefusalRecordPayload,
    TurnRecordPayload,
    WindowDigestPayload,
    action_envelope_sha256,
    canonical_json,
    canonical_sha256,
    compact_control_value,
    forced_action_sha256,
    job_stall_transition,
)
from .control_ownership import BlockerOwner
from .evidence_assessments import (
    ControlAssessment,
    ensure_receipt_assessed,
    write_assessment,
)
from .evidence_state import EvidenceRole, RunEvidenceState, StateScope
from .history_state import history_entry_kind_for_tool
from .invocation_contracts import (
    CONTRACT_AUTHORITY_MISSING,
    CONTRACT_PERSIST_FAILED,
    action_context,
    clear_action_context,
    set_action_context,
)
from .job_obligations import (
    DETACHED_TERMINAL_AUTHORITY,
    OBLIGATION_DIR,
    ObligationReconciliation,
    blocks_model,
    observe_detached_terminal,
    process_is_live,
    read_obligations,
    reconcile_job_obligations,
    settlement_from_ledger,
)
from .loop_memory import (
    CompletionClaimDecision,
    CompletionClaimEvent,
    LoopDecision,
    LoopEvent,
    LoopMemory,
)
from .native_messages import render_messages
from .output_storage import (
    OBSERVABILITY_TASK_ID,
    OutputStorageManager,
    attach_durable_output_ref,
)
from .phase_gates import (
    GATE_ASSESSMENT_SUBJECT_PREFIX,
    JOB_BARRIER_FACT,
    JOB_INTEGRITY_FACT,
    OPEN_OBLIGATIONS_FACT,
    TERMINAL_UNPERSISTED_FACT,
    ClaimDisposition,
    GateControlDisposition,
    GateResult,
    ValidatorState,
    check_phase_claim,
    claim_identity,
    claimable_outcome,
    disclosed_live_job_ids,
    gate_observation_text,
    validate_phase_claim,
)
from .phase_handoff import PhaseHandoff
from .phase_machine import (
    PHASE_NAMES,
    PhaseClaim,
    PhaseMachine,
    PhaseOutcome,
    PhaseTermination,
)
from .phase_transitions import (
    PhaseTransitionPolicy,
    RepairBudgets,
    RepairRequest,
    TransitionDecision,
)
from .physical_validator import PhysicalValidator
from .react_llm import ReactLLMClient
from .react_prompt_builder import ReActPromptBuilder
from .react_types import ReactModelMode, ReActStep, StepType
from .repair_contexts import (
    ConstraintSet,
    RepairConstraint,
    RepairContext,
    RepairFingerprintSet,
    ToolSemanticAffordance,
    build_repair_context,
    repair_context_reference_set,
    repair_context_sha256,
    write_repair_context,
)
from .replay import recover_active_repair_context_from_path
from .stall_diagnostics import (
    STALL_CONFIRMATION_TRIGGER,
    WALL_GUARD_TRIGGER,
    CleanupResult,
    JobProgressSnapshot,
    StallControlResult,
    cancel_registered_process_group,
    control_stalled_job,
    probe_job_progress,
)
from .token_tracker import TokenTracker
from .tool_orchestration import (
    ActualToolExecution,
    PreDispatchControlError,
    ToolCall,
    ToolExecution,
    ToolExecutionRecord,
    ToolLifecycleEvent,
    ToolOrchestrator,
    format_tool_result,
)
from .verdict_finalizer import (
    EvidenceCloseReason,
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    VerdictFinalizer,
)

_STRICT_LINEAGE_CONTROL_KINDS = frozenset(
    {
        "action_envelope",
        "forced_action",
        "tool_result",
        "gate_decision",
        "gate_outcome_revised",
        "repair_context_opened",
        "phase_transition",
        "evidence_close",
        # A turn record carries the ORDERED refs of the window it sealed.
        # `compact_control_value` keeps the first 128 items of a list, so a
        # long window would come back short — a digest that reconstructs a
        # DIFFERENT array than the model saw, silently.
        "turn_record",
        # For the same reason, one field further in: a refusal record carries
        # the repair intent the model SUBMITTED, and the submission schema
        # allows 4,096 characters a field. The compactor clips strings at 512
        # and appends an ellipsis, so a stated hypothesis came back as a
        # paraphrase of itself with nothing marking the record lossy. The
        # payload bounds itself through `bounded_exact_params`; that is the
        # bound, and it is the only one that may speak.
        "refusal_record",
    }
)
_REPAIR_GUIDANCE_MAX_BYTES = 32 * 1024
_RUN_TASK_COMPLETE_PREFIX = "TASK COMPLETE:"
_REPAIR_PREDISPATCH_REFUSAL_CODES = frozenset(
    {
        "ACTION_INTENT_INVALID",
        "ACTION_INTENT_BINDING_MISMATCH",
        "ACTION_ENVELOPE_IDENTITY_MISSING",
        "ACTION_ENVELOPE_PERSIST_FAILED",
        "ACTION_PARAMS_UNRECORDABLE",
        CONTRACT_AUTHORITY_MISSING,
        CONTRACT_PERSIST_FAILED,
        "REPAIR_ACTION_AFFORDANCE_MISMATCH",
        "REPAIR_CONTEXT_NOT_ACTIVE",
        "REPAIR_INTENT_DOMAIN_MISMATCH",
        "REPAIR_INTENT_INVALID_OBSERVATION",
        "REPAIR_INTENT_INVALID_REFS",
        "REPAIR_INTENT_LINEAGE_MISMATCH",
        "REPAIR_INTENT_REQUIRED",
        "REPAIR_LINEAGE_RECORDING_REQUIRED",
        "REPAIR_TOOL_NOT_ALLOWED",
    }
)


@dataclass(frozen=True)
class _PreparedRejectedCompletion:
    """Backward-compatible four-item projection plus unactivated context."""

    claim: PhaseClaim
    gate: GateResult
    event: CompletionClaimEvent
    decision: CompletionClaimDecision
    context: RepairContext | None = None

    def _legacy(
        self,
    ) -> tuple[PhaseClaim, GateResult, CompletionClaimEvent, CompletionClaimDecision]:
        return (self.claim, self.gate, self.event, self.decision)

    def __iter__(self):
        return iter(self._legacy())

    def __getitem__(self, index):
        return self._legacy()[index]


# Per-phase outcome contracts for the setup state machine.  They describe the
# state and evidence required to close a phase; they never select a public tool
# call or an order of project actions.  Neutral tool syntax is supplied once by
# the generated schemas, while reactive corrective loops remain evidence-gated.
PHASE_OBJECTIVES = {
    "provision": (
        "Establish a checkout of the requested repository and ref plus a measured, "
        "compatible toolchain. Completion evidence must name the resolved commit and "
        "the active runtime identities. A checkout or runtime that cannot be established "
        "remains an explicit blocker or unknown fact."
    ),
    "analyze": (
        "Inspect relevant project documentation, configuration and CI; treat the survey as "
        "evidence hints. Record a short strategy and unresolved prerequisites in key_results. "
        "A structured execution_plan is optional. The user task and pinned CI scope remain "
        "the acceptance requirements. Unknown runner semantics may be explored through "
        "bounded execution; neither a plan nor a command name establishes success."
    ),
    "build": (
        "Execute the task's project command with its original lifecycle, profiles and scope. "
        "Revise strategy from observed evidence, explaining changes. Establish terminal build evidence "
        "for every required surveyed build coordinate. "
        "An aggregator root with no sources is not compile evidence for source-bearing "
        "islands; each required island needs a current receipt and artifact/coverage evidence, "
        "or a typed evidence-backed blocker. A packaging or meta-project with no compile "
        "target is not a failed compile by itself. Build evidence must use the registered "
        "toolchain. While a controller-owned job barrier is active, waiting and settlement "
        "are automatic and no unrelated work may start."
    ),
    "test": (
        "Inspect existing current-run build receipts and test reports first: a completed "
        "build lifecycle may already satisfy the required tests. Reuse that evidence without "
        "repeating a command solely because the phase changed. Execute missing task scope "
        "and explain evidence-driven changes to strategy. Establish terminal "
        "runner evidence for the required surveyed test coordinates. "
        "Test coordinates can live in a different module or build system from build "
        "coordinates. Persist executed, passed, failed, error, and skipped counts with their "
        "receipt references. Report what executed against what was discovered; red tests are "
        "project facts to report, not a repair duty. Claim the outcome the receipts support; "
        "absence of a runner receipt cannot support test success."
    ),
    "report": (
        "Persist the final setup report grounded in fixed task requirements, the sealed "
        "verdict, current receipts, "
        "artifacts, test counts, and unresolved conflicts. Completion requires a durable report "
        "artifact reference; report delivery status does not rewrite the verdict."
    ),
}

# Python keeps an ecosystem-specific evidence contract without prescribing an
# install/build/test sequence.  In particular, absence of a Java target is a
# not-applicable fact, never a Python build failure.
PYTHON_PHASE_OBJECTIVES = {
    "build": (
        "Establish the Python environment, dependency readiness, and byte/native build "
        "evidence required by the surveyed project. A Python project has no Java compile "
        "target; that absence is not grounds for a failed build. Any blocker must cite the "
        "environment, dependency, or build receipt that establishes it. Evidence must use "
        "the registered interpreter. While a controller-owned job barrier is active, waiting "
        "and settlement are automatic and no unrelated work may start."
    ),
    "test": (
        "Establish terminal Python runner evidence at the surveyed test coordinates, with "
        "executed, passed, failed, error, and skipped counts bound to receipt references. "
        "When native readiness is absent or unknown, the surveyed bounded-smoke constraint "
        "limits collection until capability evidence changes. Report what executed against "
        "what was discovered; red tests are project facts to report, not a repair duty. Claim "
        "the outcome the receipts support; no runner receipt cannot support success."
    ),
}

# Kickoff tasks are authored before the ecosystem is known.  The base contracts
# are deliberately ecosystem-neutral, so kickoff needs no prescriptive rewrite.
KICKOFF_PHASE_OBJECTIVES = dict(PHASE_OBJECTIVES)

# Back-compat aliases: dim (d) collapsed the FACTS_* variants INTO the base
# maps above, so these names now point at the same facts-wording objects. Kept
# so existing importers (tests, callers) resolve without a rename churn.
FACTS_PHASE_OBJECTIVES = PHASE_OBJECTIVES
FACTS_PYTHON_PHASE_OBJECTIVES = PYTHON_PHASE_OBJECTIVES
FACTS_KICKOFF_PHASE_OBJECTIVES = KICKOFF_PHASE_OBJECTIVES


def kickoff_phase_objectives() -> dict:
    """Return outcome/evidence contracts for the engine-owned phase tasks."""
    return KICKOFF_PHASE_OBJECTIVES


# dim (e) deleted: the PRE-HOC python/native-first guidance block
# (PYTHON_BUILD_PHASE_GUIDANCE, PYTHON_TEST_PHASE_GUIDANCE, and the
# NATIVE_FIRST_BUILD_GUIDANCE prepend) is gone — pre-hoc advice is a
# prescription. The REACTIVE native smoke steer below stays: it is
# evidence-triggered (the artifact probe decides which state it reports) and is
# part of the corrective-loop allowlist, not a dimension.

# Rendered before the test guidance when the native artifact PROBE found no
# shared objects (live TVM 2026-07-18: the agent swept the full suite without
# libtvm — 356 identical collection errors, pure waste of the phase budget).
# P0-D: the phase outcome is NOT the trigger — see _native_smoke_guidance.
NATIVE_NOT_BUILT_TEST_GUIDANCE = (
    "The NATIVE core was not built in the build phase. Do NOT sweep the full "
    "suite — without the native library it only repeats hundreds of identical "
    "collection errors. Call bare build(action='test') with NO args first. "
    "The build tool will use a survey-verified bounded smoke target, or refuse "
    "safely when no verified target exists. Never invent, guess, or substitute "
    "a test path. Report the bounded smoke result honestly. While the native "
    "core remains unready, do not broaden the test scope merely because that "
    "smoke passed."
)

# The same bounded-smoke discipline, minus the "not built" claim: the shared
# tail of the two honest states the artifact probe can report (P0-D).
_NATIVE_BOUNDED_SMOKE_TAIL = (
    "Do NOT sweep the full suite. Call bare build(action='test') with NO args "
    "first. The build tool will use a survey-verified bounded smoke target, or "
    "refuse safely when no verified target exists. Never invent, guess, or "
    "substitute a test path. If that smoke skips, the skip reasons name the "
    "missing capability — report them as the honest result."
)

# Rendered when the probe SAW native shared objects while the build phase closed
# non-success (live TVM 2026-07-26: five .so files existed — libtvm_compiler.so
# among them — yet every layer asserted the native core was never built, because
# a `pip check` packaging warning had capped the phase outcome).
NATIVE_ARTIFACTS_PRESENT_TEST_GUIDANCE = (
    "Native artifacts are present ({artifacts}). The build phase closed "
    "'{outcome}' for packaging-integrity reasons — that does NOT mean the "
    "native core is missing. " + _NATIVE_BOUNDED_SMOKE_TAIL
)

# Rendered when the probe itself could not run: an unverified absence is not a
# fact, so no layer may downgrade it to "not built" — and with no artifact
# evidence the phase outcome gets no cause attributed to it either.
NATIVE_ARTIFACTS_UNKNOWN_TEST_GUIDANCE = (
    "The native artifact probe could not verify native artifacts under "
    "{root} — the native state is UNKNOWN, not missing. The build phase closed "
    "'{outcome}'. " + _NATIVE_BOUNDED_SMOKE_TAIL
)

# Build-system labels the analyzer emits for Python projects: structure
# detection records "pip/poetry"; the physical validator and manifest say
# "python"; installer variants may surface too.
_PYTHON_BUILD_SYSTEM_LABELS = frozenset(
    {"python", "pip", "poetry", "pip/poetry", "pipenv", "uv", "setuptools", "hatch", "pdm", "conda"}
)


def is_python_build_system(build_system: Optional[str]) -> bool:
    if not build_system:
        return False
    label = str(build_system).strip().lower()
    return label in _PYTHON_BUILD_SYSTEM_LABELS or "python" in label


def phase_objective(phase: str, build_system: Optional[str] = None) -> str:
    """Project-aware phase outcome/evidence contract (spec §3.1).

    When the analyzer detected a Python project, the build/test phases get the
    PYTHON_PHASE_OBJECTIVES overrides; every other project (and an unknown
    build system) gets the PHASE_OBJECTIVES defaults.

    Both maps are non-prescriptive: coordinates are projected separately from
    the survey, and neutral call syntax comes from the tool schemas."""
    if is_python_build_system(build_system):
        override = PYTHON_PHASE_OBJECTIVES.get(phase)
        if override:
            return override
    return PHASE_OBJECTIVES.get(phase, "")


def wall_clock_exceeded(
    start_time: float, cap_seconds: Optional[float], now: Optional[float] = None
) -> bool:
    """Whether a run's global wall-clock cap has been exceeded.

    A cap of None/0/negative disables the check. This bounds total run time
    regardless of per-command behavior — long builds are no longer hard-killed
    per command (dispatch-and-poll), so this is the run's only hard time limit.
    """
    if not cap_seconds or cap_seconds <= 0:
        return False
    current = now if now is not None else time.time()
    return (current - start_time) > cap_seconds


class NoProgressGuard:
    """Trips only when a run has completed `threshold` tasks without EVER
    producing a build artifact. Once any artifact (.class/JAR) appears, the run
    has made physical progress and the guard never trips again — so it cannot
    halt a normal build during its test/report phase, only a run that is stuck
    never building anything (e.g. an analyzer that keeps emitting explore tasks
    that compile nothing).

    The artifact signal is Java-only (.class/JAR files), so the guard is armed
    ONLY when an artifact-bearing build is expected (Java/Maven/Gradle). For
    project types whose build produces no such artifacts (Node.js/Python/Rust/
    Go), `artifacts_expected` is False and the guard is a no-op — otherwise a
    perfectly healthy run that simply completes more than `threshold` tasks
    would be force-stopped because its artifact signal is structurally 0."""

    def __init__(self, threshold: int = 6):
        self.threshold = threshold
        self._ever_built = False
        self._stagnant = 0

    def update(self, artifact_signal: int, artifacts_expected: bool = True) -> bool:
        # Never arm the guard for project types that cannot produce an
        # observable build artifact: there is no signal it could ever see, so
        # tripping would only halt healthy runs.
        if not artifacts_expected:
            return False
        if artifact_signal > 0:
            self._ever_built = True
            self._stagnant = 0
            return False
        if self._ever_built:
            return False
        self._stagnant += 1
        return self._stagnant >= self.threshold


# Transport-persist failures the run can outlive by closing the phase
# honestly (spec 2026-08-13 transient containment). Integrity failures —
# gate_decision_persist_failed, repair_context_projection_invalid, the
# barrier and dispatch families — are deliberately NOT here.
_CONTAINED_CONTROL_PERSIST_CODES = frozenset(
    {"repair_assessment_persist_failed", "repair_context_transport_unavailable"}
)


class ReActEngine(UIEventEmitter):
    """Core ReAct (Reasoning and Acting) engine with dual model support."""

    def __init__(
        self,
        context_manager: ContextManager,
        tools: List[BaseTool],
        repository_url: str = None,
        repository_ref: str = None,
        phase_machine: Optional[PhaseMachine] = None,
        context_journal=None,
        run_evidence_state: Optional[RunEvidenceState] = None,
        verdict_finalizer: Optional[VerdictFinalizer] = None,
        transition_policy: Optional[PhaseTransitionPolicy] = None,
        control_event_sink: Optional[ControlEventSink] = None,
        target_repo_sha_callback=None,
        orchestrator=None,
        llm_client: Any | None = None,
        pre_finalize_evidence_callback: Callable[[], Mapping[str, Any] | None] | None = None,
        physical_validator: PhysicalValidator | None = None,
    ):
        super().__init__()  # Initialize UIEventEmitter
        self.context_manager = context_manager
        self.tools = {tool.name: tool for tool in tools}
        self.config = get_config()
        self.control_event_sink = control_event_sink
        self.orchestrator = orchestrator or getattr(context_manager, "orchestrator", None)
        self._target_repo_sha_callback = target_repo_sha_callback
        self.pre_finalize_evidence_callback = pre_finalize_evidence_callback
        self._pre_finalize_evidence_attempted = False
        self._active_control_envelope_id: str | None = None

        # Engine-owned phase machine for setup runs (spec §3.1). None keeps the
        # legacy free-form behavior (`sag run --task` passes neither).
        self.phase_machine = phase_machine
        self.context_journal = context_journal
        self.run_evidence_state = run_evidence_state
        self.verdict_finalizer = verdict_finalizer
        self.loop_memory = LoopMemory()
        # At most one evidence-triggered model repair is active.  It is
        # replaced only by a new judge assessment and cleared only after a
        # differently fingerprinted, frozen-and-dispatched model action.
        self._pending_repair_context: RepairContext | None = None
        self._last_invocation_contract_id: str | None = None
        if transition_policy is None:
            self.transition_policy = PhaseTransitionPolicy(repair_guard=self.loop_memory)
        else:
            self.transition_policy = transition_policy
            if self.transition_policy.repair_guard is None:
                self.transition_policy.repair_guard = self.loop_memory
        self._repair_global_remaining = 2
        self._repair_phase_remaining = {"test": 1, "build": 1}
        self._report_attempted = False
        self._report_delivered = False
        self._report_failed = False
        self._phase_iterations = 0
        # Window-reset marker: the first journal record after a reset carries
        # the new phase intro text (spec §7 reconstruction).
        self._journal_intro_dirty = False
        # Last ledger text journaled for the current window: compact_steps
        # returns the FULL cumulative ledger on every post-compaction
        # iteration, so records must dedupe on text change (round-6 review:
        # ~6KB re-recorded per iteration once compaction was active).
        self._journal_last_ledger = None
        self.prompts = load_react_engine_prompts()
        self.repository_url = repository_url
        self.repository_ref = repository_ref
        self.prompt_builder = ReActPromptBuilder(
            prompts=self.prompts,
            context_manager=self.context_manager,
            tools=self.tools,
        )

        # ReAct state
        self.steps: List[ReActStep] = []
        self.current_iteration = 0
        self.max_iterations = self.config.max_iterations

        # Context switching guidance
        self.steps_since_context_switch = 0
        self.context_switch_threshold = self.config.context_switch_threshold

        # Tool execution tracking to avoid repetitive calls
        self.recent_tool_executions = []
        self.max_recent_executions = 10

        # Native tool-call identity for harness-authored calls (forced test
        # attempts); the model's own calls come with provider ids.
        self._forced_call_counter = 0
        self._active_native_tool_call_id = None

        # Advisor consult accounting (spec §3.2).
        self._reset_advisor_run_state()

        # State memory for successful operations
        self.successful_states = {
            "working_directory": None,  # Last successful working directory
            "cloned_repos": set(),  # Set of successfully cloned repo URLs
            "project_type": None,  # Detected project type
            "maven_success": False,  # Whether maven operations succeeded
            "gradle_success": False,  # Whether gradle operations succeeded
            "python_success": False,  # Whether python build operations succeeded
            "build_success": False,  # Whether any consolidated build succeeded
            "excluded_modules": set(),
            "excluded_tests": set(),
            "report_snapshot": None,
        }

        # Agent logger for detailed traces
        self.agent_logger = create_agent_logger("react_engine")

        # Initialize output storage manager
        contexts_dir = (
            Path(self.context_manager.contexts_dir)
            if hasattr(self.context_manager, "contexts_dir")
            else Path("/workspace/.setup_agent/contexts")
        )
        # Pass orchestrator to OutputStorageManager for container file operations
        orchestrator = (
            self.context_manager.orchestrator
            if hasattr(self.context_manager, "orchestrator")
            else None
        )
        self.output_storage = OutputStorageManager(contexts_dir, orchestrator=orchestrator)
        self.phase_handoff = None
        if self.phase_machine is not None:
            if self.run_evidence_state is None:
                run_id = str(
                    getattr(self.context_manager, "session_id", None)
                    or f"react-{self._get_timestamp()}"
                )
                self.run_evidence_state = RunEvidenceState(run_id=run_id)
            if self.verdict_finalizer is None and orchestrator is not None:
                self.verdict_finalizer = VerdictFinalizer(orchestrator)
            self.phase_handoff = PhaseHandoff(
                self.run_evidence_state,
                orchestrator=orchestrator,
            )

        # Initialize physical validator for fact-based validation
        self.physical_validator = (
            physical_validator
            if physical_validator is not None
            else PhysicalValidator(
                docker_orchestrator=orchestrator,
                project_path="/workspace",
                build_coverage_threshold=self.config.build_coverage_threshold,
                receipt_run_id=(
                    self.run_evidence_state.run_id if self.run_evidence_state is not None else None
                ),
            )
        )
        self._analysis_facts_recovery_attempted = False
        phase_tool = self.tools.get("phase")
        bind_plan_evidence = getattr(phase_tool, "bind_execution_plan_evidence", None)
        if callable(bind_plan_evidence):
            bind_plan_evidence(self.output_storage)
        bind_recovery = getattr(phase_tool, "bind_analysis_facts_recovery", None)
        if callable(bind_recovery):
            bind_recovery(self._recover_analysis_facts_once)
        # Share the validator with the context manager so ContextTool's
        # completion-evidence gate reuses it (probe cache + threshold) instead
        # of constructing a fresh one per completion attempt.
        if getattr(self.context_manager, "physical_validator", None) is None:
            self.context_manager.physical_validator = self.physical_validator

        # Late-bind the finalizer's build oracle (same contract as
        # SetupAgent._initialize_tools): gates and finalizer must read the SAME
        # physical validator, or the sealed snapshot can carry a gate-validated
        # SUCCESS next to observation-derived FAILED (live ws7-final7 bigtop).
        finalizer = getattr(self, "verdict_finalizer", None)
        if finalizer is not None:
            finalizer.output_storage = self.output_storage
        if finalizer is not None and getattr(finalizer, "validator", None) is None:
            finalizer.validator = self.physical_validator
            if getattr(finalizer, "project_name", None) is None:
                finalizer.project_name = self._project_name_for_gate()

        # No-physical-progress guard: halt a run that completes tasks without
        # ever producing build artifacts (anti-thrash). Only armed for
        # artifact-bearing builds (Java/Maven/Gradle); see _expects_build_artifacts.
        self.progress_guard = NoProgressGuard(
            threshold=getattr(self.config, "no_progress_task_limit", 6)
        )
        # Cache of the workspace build-file probe (None = not yet probed).
        self._artifact_build_probe: Optional[bool] = None
        # Artifact count at the first sample; only growth beyond it counts as
        # progress, so vendored/pre-existing build output can't disarm the guard.
        self._artifact_baseline: Optional[int] = None

        # Initialize token tracker and LLM client for monitoring model usage
        self.token_tracker = TokenTracker()
        if llm_client is None:
            self.llm_client = ReactLLMClient(
                config=self.config,
                tools=self.tools,
                token_tracker=self.token_tracker,
                trace_context=lambda: {
                    "iteration": self.current_iteration,
                    "timestamp": self._get_timestamp(),
                    "agent_logger": self.agent_logger,
                },
                repair_context_provider=lambda: self._pending_repair_context,
            )
            self.llm_client.setup()
        else:
            # Controller-only callers can install a tripwire client without
            # constructing provider capabilities.  The normal production path
            # remains unchanged and still owns ReactLLMClient setup above.
            self.llm_client = llm_client

        # The append-only control stream is the authority after a process
        # restart.  Rebuild a still-open judge context before any new model
        # turn; malformed, unanswered or forged lineage raises and prevents
        # the engine from continuing with guessed repair state.
        self._restore_active_repair_context()

        logger.info(
            "ReAct Engine initialized with the native executor loop, physical validation, "
            "and token tracking"
        )
        logger.info(f"Thinking model: {self.config.get_litellm_model_name('thinking')}")
        logger.info(f"Action model: {self.config.get_litellm_model_name('action')}")
        if repository_url:
            logger.info(f"Repository URL: {repository_url}")
        if repository_ref:
            logger.info(f"Repository ref: {repository_ref}")

    @classmethod
    def for_controller_loop(
        cls,
        *,
        orchestrator: Any,
        llm_client: Any,
        control_event_sink: ControlEventSink,
        run_id: str,
        loop_memory: LoopMemory | None = None,
        repository_url: str | None = None,
        max_wall_clock_seconds: int = 180,
        dispatch_stall_seconds: int = 2,
        max_iterations: int = 1,
        obligation_poll_seconds: float = 1.0,
        report_reserve_seconds: int = 0,
    ) -> "ReActEngine":
        """Construct the real no-phase controller loop with explicit bounds.

        This is the public construction path for callers that exercise the
        obligation barrier without provisioning a model provider or a phase
        machine.  It deliberately goes through ``__init__`` so the controller,
        validator, context manager, and output storage are production objects;
        only the external model dependency is injected.
        """

        if max_wall_clock_seconds <= 0:
            raise ValueError("controller-loop wall clock must be positive")
        if dispatch_stall_seconds < 0:
            raise ValueError("controller-loop stall threshold cannot be negative")
        if max_iterations <= 0:
            raise ValueError("controller-loop max iterations must be positive")
        if obligation_poll_seconds <= 0:
            raise ValueError("controller-loop poll interval must be positive")
        if report_reserve_seconds < 0:
            raise ValueError("controller-loop report reserve cannot be negative")

        context_manager = ContextManager(workspace_path="/workspace", orchestrator=orchestrator)
        engine = cls(
            context_manager,
            [],
            repository_url=repository_url,
            phase_machine=None,
            run_evidence_state=RunEvidenceState(run_id=run_id),
            control_event_sink=control_event_sink,
            orchestrator=orchestrator,
            llm_client=llm_client,
        )
        engine.config = engine.config.model_copy(
            update={
                "max_wall_clock_seconds": max_wall_clock_seconds,
                "dispatch_stall_seconds": dispatch_stall_seconds,
                "advisor_mode": "off",
            }
        )
        engine.max_iterations = max_iterations
        engine.loop_memory = loop_memory or engine.loop_memory
        engine.transition_policy.repair_guard = engine.loop_memory
        engine._OBLIGATION_POLL_SECONDS = obligation_poll_seconds
        engine._REPORT_RESERVE_SECONDS = report_reserve_seconds
        return engine

    def set_repository_url(self, repository_url: str, repository_ref: str | None = None):
        """Set the repository target for the current project."""
        self.repository_url = repository_url
        self.repository_ref = repository_ref
        logger.info(f"Repository URL set: {repository_url}")
        if repository_ref:
            logger.info(f"Repository ref set: {repository_ref}")

    def _artifact_signal(self) -> int:
        """New build artifacts (.class/JAR) produced since the run started.

        A baseline is captured on the first sample so a repo that *vendors*
        committed build output (or one cloned with stale artifacts) does not
        pre-disarm the no-progress guard: only artifacts created during this run
        count as physical progress."""
        raw = self._raw_artifact_count()
        if self._artifact_baseline is None:
            self._artifact_baseline = raw
        return max(0, raw - self._artifact_baseline)

    def _raw_artifact_count(self) -> int:
        """Total class + JAR files currently in the workspace."""
        # `Config` has no project_name; derive it from the context manager
        # (same source used by _validate_physical_state), falling back to None
        # which makes the validator scan the whole workspace recursively.
        project_name = None
        if hasattr(self.context_manager, "project_name"):
            project_name = self.context_manager.project_name
        try:
            result = self.physical_validator.validate_build_artifacts(project_name)
            return int(result.get("class_files", 0)) + int(result.get("jar_files", 0))
        except Exception as exc:
            # Don't let a probe failure silently degrade the guard into an
            # unconditional "stop after N tasks": surface it.
            self.agent_logger.warning(f"Artifact-signal probe failed: {exc}")
            return 0

    def _expects_build_artifacts(self) -> bool:
        """Whether this project is expected to produce observable build
        artifacts (.class/JAR files) that `_artifact_signal` can count.

        Only Java/Maven/Gradle projects qualify. For Node.js/Python/Rust/Go the
        artifact signal is structurally always 0, so the no-progress guard must
        NOT be armed for them — otherwise a healthy run that simply completes
        more than `threshold` tasks would be force-stopped. We arm the guard
        only when we POSITIVELY detect an artifact-bearing build."""
        # Project type discovered during execution (set as Maven/Gradle builds
        # run) always wins and can flip on at any point in the run.
        project_type = (self.successful_states.get("project_type") or "").lower()
        if project_type in ("maven", "gradle", "java"):
            return True

        # Otherwise probe the workspace once for Java/Maven/Gradle build files.
        if self._artifact_build_probe is not None:
            return self._artifact_build_probe

        expects = False
        try:
            cmd = (
                "find /workspace -maxdepth 3 "
                "\\( -name pom.xml -o -name build.gradle -o -name build.gradle.kts \\) "
                "-type f 2>/dev/null | head -1"
            )
            result = self.physical_validator._execute_command_with_logging(
                cmd, "build-artifact expectation probe"
            )
            expects = bool((result.get("output") or "").strip())
        except Exception as exc:
            self.agent_logger.warning(f"Build-artifact expectation probe failed: {exc}")
            expects = False

        self._artifact_build_probe = expects
        return expects

    def _check_progress_after_task(self) -> bool:
        """Return True if the run should stop because no build progress is
        being made across consecutive completed tasks."""
        tripped = self.progress_guard.update(
            self._artifact_signal(),
            artifacts_expected=self._expects_build_artifacts(),
        )
        if tripped:
            self.agent_logger.warning(
                "Stopping: multiple tasks completed with no new build artifacts (no physical progress)."
            )
        return tripped

    # ------------------------------------------------------------------
    # Evidence ownership and run closure (setup mode only)
    # ------------------------------------------------------------------

    _NON_EVIDENCE_TOOLS = frozenset({"phase", "manage_context", "report"})
    _BUILD_EVIDENCE_TOOLS = frozenset({"build", "maven", "gradle", "python"})

    @staticmethod
    def _backend_operation_tokens(params: Dict[str, Any]) -> List[str]:
        values = [
            params[key]
            for key in ("command", "tasks", "task", "operation")
            if params.get(key) not in (None, "", [])
        ]
        if not values:
            values = [params.get("action")]

        tokens: List[str] = []
        pending = list(values)
        while pending:
            value = pending.pop(0)
            if isinstance(value, (list, tuple, set)):
                pending[0:0] = list(value)
                continue
            text = str(value or "").strip()
            if not text:
                continue
            try:
                tokens.extend(shlex.split(text))
            except ValueError:
                tokens.extend(text.split())
        return tokens

    @staticmethod
    def _is_test_operation(token: str) -> bool:
        if token.startswith("-"):
            return False
        leaf = token.rsplit(":", 1)[-1]
        normalized = leaf.lower()
        if normalized in {"test", "tests", "verify", "verify_tests", "check"}:
            return True
        if re.search(r"(?:^|[-_])tests?(?:$|[-_])", normalized):
            return True
        return bool(re.search(r"^test(?=$|[A-Z])", leaf) or re.search(r"Test(?=$|[A-Z])", leaf))

    @staticmethod
    def _is_dependency_operation(token: str) -> bool:
        if token.startswith("-"):
            return False
        normalized = token.lower()
        leaf = normalized.rsplit(":", 1)[-1]
        return normalized.startswith("dependency:") or leaf in {
            "deps",
            "dependencies",
            "dependency",
            "dependencyinsight",
            "resolve",
            "install_dependencies",
            "setup_env",
        }

    def _tool_evidence_action(self, params: Dict[str, Any]) -> str:
        action = str((params or {}).get("action") or "").strip().lower()
        if action:
            return action
        operations = self._backend_operation_tokens(params or {})
        return " ".join(operations).lower() or "execute"

    def _tool_evidence_scope(self, tool_name: str, params: Dict[str, Any]) -> StateScope:
        action = str((params or {}).get("action") or "").strip().lower()
        if tool_name in {"project", "project_analyzer"} and action == "analyze":
            return StateScope.PROJECT_ANALYSIS
        if tool_name in {"project", "project_setup", "system", "env"}:
            return StateScope.ENVIRONMENT
        if tool_name in {"build", "maven", "gradle", "python"}:
            operations = self._backend_operation_tokens(params or {})
            if any(self._is_test_operation(operation) for operation in operations):
                return StateScope.TEST_RUNTIME
            if any(self._is_dependency_operation(operation) for operation in operations):
                return StateScope.DEPENDENCIES
            return StateScope.ARTIFACTS

        phase = getattr(getattr(self, "phase_machine", None), "current_phase", None)
        return {
            "provision": StateScope.ENVIRONMENT,
            "analyze": StateScope.PROJECT_ANALYSIS,
            "build": StateScope.ARTIFACTS,
            "test": StateScope.TEST_RUNTIME,
        }.get(phase, StateScope.PROJECT_ANALYSIS)

    def _tool_evidence_roles(
        self,
        tool_name: str,
        params: Dict[str, Any],
        result: ToolResult | UnpersistedToolResult,
    ) -> tuple[EvidenceRole, ...]:
        roles: list[EvidenceRole] = []
        if tool_name in self._BUILD_EVIDENCE_TOOLS:
            operations = [
                operation
                for operation in self._backend_operation_tokens(params or {})
                if not operation.startswith("-")
            ]
            test_only = bool(operations) and all(
                self._is_test_operation(operation)
                and operation.rsplit(":", 1)[-1].lower() not in {"verify", "check"}
                for operation in operations
            )
            dependency_only = bool(operations) and all(
                self._is_dependency_operation(operation) for operation in operations
            )
            if not test_only and not dependency_only:
                roles.append(EvidenceRole.BUILD)
        if result.test_stats is not None:
            roles.append(EvidenceRole.TEST)
        return tuple(roles)

    def _record_current_phase_evidence(
        self,
        state: RunEvidenceState,
        evidence_refs: List[str],
    ) -> None:
        machine = getattr(self, "phase_machine", None)
        attempt_id = getattr(machine, "current_attempt_id", None)
        refs = self._dedupe_strings(evidence_refs)
        if attempt_id and refs:
            state.record_phase_evidence(attempt_id, refs)

    def _record_tool_execution(
        self,
        tool_name: str,
        params: Dict[str, Any],
        result: ToolResult,
        *,
        attempted_execution: bool = True,
        execution_id: str | None = None,
    ) -> ToolResult:
        """Persist provenance and ingest one evidence-bearing execution once."""
        if tool_name == "report":
            self._report_attempted = True
            if attempted_execution and result.succeeded:
                self._report_delivered = True
            elif result.is_terminal or not attempted_execution:
                self._report_failed = True
            return result

        state = getattr(self, "run_evidence_state", None)
        if state is None or state.sealed or tool_name in self._NON_EVIDENCE_TOOLS:
            return result

        execution_id = execution_id or new_execution_id()
        scope = self._tool_evidence_scope(tool_name, params)
        roles = self._tool_evidence_roles(tool_name, params, result)
        action = self._tool_evidence_action(params)
        machine = getattr(self, "phase_machine", None)
        source_phase = getattr(machine, "current_phase", None)
        source_attempt_id = getattr(machine, "current_attempt_id", None)
        if state.has_execution_id(execution_id):
            state.ingest_tool_result(
                scope,
                tool_name,
                result,
                provenance=result.output_ref or f"tool:{tool_name}:{action}:replay",
                roles=roles,
                execution_id=execution_id,
                params=params,
                source_phase=source_phase,
                source_attempt_id=source_attempt_id,
            )
            return result
        if not attempted_execution:
            state.record_attempt(
                action=f"{tool_name}:{action}",
                relevant_scopes=[scope],
                outcome=result.operation_outcome,
                evidence_refs=self._dedupe_strings(
                    [result.output_ref, *result.evidence_refs, *result.refs]
                ),
            )
            return result

        task_id = str(
            getattr(self.context_manager, "current_task_id", None)
            or f"{tool_name}_{getattr(self, 'current_iteration', 0)}"
        )
        try:
            durable = attach_durable_output_ref(
                result,
                self.output_storage,
                task_id=task_id,
                tool_name=tool_name,
                action=action,
            )
        except OutputPersistenceError as exc:
            logger.error(f"Failed to persist full output for {tool_name}: {exc}")
            state.record_attempt(
                action=f"{tool_name}:{action}",
                relevant_scopes=[scope],
                outcome=result.operation_outcome,
                evidence_refs=self._dedupe_strings(
                    [
                        ref
                        for ref in [*result.evidence_refs, *result.refs]
                        if not is_output_storage_ref(ref)
                    ]
                ),
            )
            state.ingest_tool_result(
                scope,
                tool_name,
                result,
                provenance=f"tool:{tool_name}:{action}:output-persistence-failed",
                roles=roles,
                execution_id=execution_id,
                params=params,
                source_phase=source_phase,
                source_attempt_id=source_attempt_id,
            )
            self._record_current_phase_evidence(
                state,
                self._dedupe_strings([result.output_ref, *result.evidence_refs, *result.refs]),
            )
            state.record_conflict("output_storage_failed")
            raise
        state.record_attempt(
            action=f"{tool_name}:{action}",
            relevant_scopes=[scope],
            outcome=durable.operation_outcome,
            evidence_refs=self._dedupe_strings(
                [durable.output_ref, *durable.evidence_refs, *durable.refs]
            ),
        )
        state.ingest_tool_result(
            scope,
            tool_name,
            durable,
            provenance=durable.output_ref,
            roles=roles,
            execution_id=execution_id,
            params=params,
            source_phase=source_phase,
            source_attempt_id=source_attempt_id,
        )
        self._record_current_phase_evidence(
            state,
            self._dedupe_strings([durable.output_ref, *durable.evidence_refs, *durable.refs]),
        )
        return durable

    def _report_execution_allowed(self) -> bool:
        if getattr(self, "phase_machine", None) is None:
            return True
        state = getattr(self, "run_evidence_state", None)
        finalizer = getattr(self, "verdict_finalizer", None)
        return bool(
            state is not None
            and state.sealed
            and finalizer is not None
            and finalizer.has_current_snapshot(state)
        )

    def _evidence_execution_closed(self, call: ToolCall) -> bool:
        state = getattr(self, "run_evidence_state", None)
        return bool(
            state is not None and state.sealed and call.name not in self._NON_EVIDENCE_TOOLS
        )

    @staticmethod
    def _refused_closed_evidence_execution(call: ToolCall) -> ToolExecution:
        result = ToolResult.completed(
            output="Tool execution refused because setup evidence is already sealed.",
            operation_outcome=OperationOutcome.SKIPPED,
            metadata={"execution_refused": "evidence_closed"},
        )
        return ToolExecution(
            call=call,
            result=result,
            status="skipped",
            raw_params=call.raw_params,
            validated_params=call.validated_params,
            observation_text=format_tool_result(call.name, result),
            attempted_execution=False,
            metadata={"execution_refused": "evidence_closed"},
        )

    @staticmethod
    def _refused_report_execution(call: ToolCall) -> ToolExecution:
        result = ToolResult.completed(
            output="Report execution refused until evidence-close persistence completes.",
            operation_outcome=OperationOutcome.SKIPPED,
        )
        return ToolExecution(
            call=call,
            result=result,
            status="skipped",
            raw_params=call.raw_params,
            validated_params=call.validated_params,
            observation_text=format_tool_result(call.name, result),
            attempted_execution=False,
            metadata={"report_refused": "evidence_not_closed"},
        )

    @staticmethod
    def _failed_report_persistence_execution(
        call: ToolCall, exc: OutputPersistenceError
    ) -> ToolExecution:
        result = ToolResult.completed(
            output="Report delivery failed because output persistence was unavailable.",
            operation_outcome=OperationOutcome.SKIPPED,
            metadata={
                "report_delivery_failure": "output_persistence",
                "persistence_error": type(exc).__name__,
            },
        )
        return ToolExecution(
            call=call,
            result=result,
            status="failure",
            raw_params=call.raw_params,
            validated_params=call.validated_params,
            observation_text=format_tool_result(call.name, result),
            attempted_execution=True,
            metadata={"report_delivery_failure": "output_persistence"},
        )

    def _record_phase_audit(self, record) -> None:
        state = getattr(self, "run_evidence_state", None)
        if state is not None and not state.sealed:
            state.record_phase_record(record)

    # The wait's floor for the report phase. p7d camel spent ~5 minutes on
    # report; the reserve doubles that so the wait can never starve it.
    _REPORT_RESERVE_SECONDS = 600
    _OBLIGATION_POLL_SECONDS = 30
    _JOB_INTEGRITY_RETRIES = 2
    _JOB_MARKER_RECONCILE_ATTEMPTS = 30

    def _report_reserve_seconds(self, cap=None) -> float:
        """Reserve at most ten percent of a bounded run for sealing/reporting."""
        if cap is None:
            cap = getattr(self, "_wall_clock_cap", None) or getattr(
                getattr(self, "config", None), "max_wall_clock_seconds", 7200
            )
        return min(float(self._REPORT_RESERVE_SECONDS), max(0.0, float(cap) * 0.10))

    def _hold_deadline(self) -> Optional[float]:
        """When must any hold stop — ONE computation (P3, spec §4.2).

        Consumed by the evidence-close wait below and, through the provider
        installed at run start, by the orchestrator's dispatch hold. None
        means the margin is unknown — and margin unknown is margin absent:
        every consumer degrades to its bounded behavior, never to an
        unbounded hold.
        """
        started_at = getattr(self, "_run_started_at", None)
        if started_at is None:
            return None
        cap = getattr(self, "_wall_clock_cap", None) or getattr(
            getattr(self, "config", None), "max_wall_clock_seconds", 7200
        )
        return float(started_at) + float(cap) - self._report_reserve_seconds(cap)

    def _install_hold_deadline_provider(self) -> None:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is not None:
            orchestrator.hold_deadline_provider = self._hold_deadline

    def _await_open_obligations(self, reason, *, now=None, sleep=None) -> None:
        """Compatibility wrapper over the controller-owned runtime barrier."""
        self._drain_job_barrier(reason=reason, now=now, sleep=sleep)

    def _ephemeral_job_handles(self) -> Dict[str, Dict[str, Any]]:
        handles = getattr(self, "_job_barrier_ephemeral_handles", None)
        if handles is None:
            handles = {}
            self._job_barrier_ephemeral_handles = handles
        return handles

    def _begin_detached_run_scope(self) -> None:
        """Freeze which in-memory detached handles predate this executor run."""

        orchestrator = getattr(self, "orchestrator", None)
        remembered = getattr(orchestrator, "_detached_handles", None)
        self._detached_handle_baseline = (
            frozenset(str(job_id) for job_id in remembered)
            if isinstance(remembered, Mapping)
            else frozenset()
        )
        self._job_barrier_ephemeral_handles = {}
        self._termination_cleanup_results: Dict[str, CleanupResult] = {}
        self._termination_cleanup_done = False
        self.last_run_cancelled = False

    def _capture_unreturned_detached_handles(self) -> None:
        """Adopt dispatches accepted after run start but interrupted before result return.

        The ordinary barrier registration happens only after a tool result is
        constructed.  DockerOrchestrator remembers the accepted host handle at
        dispatch time, so an operator interrupt during the soft-hold polling
        window can recover that exact identity here without redispatching or
        guessing from container process names.
        """

        orchestrator = getattr(self, "orchestrator", None)
        remembered = getattr(orchestrator, "_detached_handles", None)
        if not isinstance(remembered, Mapping):
            return
        baseline = set(getattr(self, "_detached_handle_baseline", ()) or ())
        ephemeral = self._ephemeral_job_handles()
        for remembered_id, raw_handle in remembered.items():
            job_id = str(remembered_id or "").strip()
            if not job_id or job_id in baseline or not isinstance(raw_handle, Mapping):
                continue
            handle = dict(raw_handle)
            if str(handle.get("job_id") or "").strip() != job_id:
                continue
            ephemeral.setdefault(job_id, handle)

    def _termination_jobs(self) -> Dict[str, Dict[str, Any]]:
        """Return every durable or just-recovered job this run must close."""

        jobs: Dict[str, Dict[str, Any]] = {}
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is not None:
            records = self._obligations_still_owed(orchestrator)
            if records:
                for record in records:
                    job_id = str(record.get("job_id") or "").strip()
                    if job_id:
                        jobs[job_id] = dict(record)
        for job_id, handle in self._ephemeral_job_handles().items():
            if job_id:
                jobs.setdefault(job_id, dict(handle))
        return jobs

    def _terminate_open_jobs(self, reason: EvidenceCloseReason | str) -> Dict[str, CleanupResult]:
        """TERM/KILL exact registered groups on cancellation or abort.

        This is deliberately separate from stall cleanup: cancellation is
        operator termination authority, not a claim that a progressing build
        stalled.  The process helper re-verifies the launcher's PID/PGID/start
        identity before either signal.  A refusal is retained as an explicit
        close result rather than silently abandoning the runner.
        """

        if getattr(self, "_termination_cleanup_done", False):
            return dict(getattr(self, "_termination_cleanup_results", {}) or {})
        self._termination_cleanup_done = True
        self._capture_unreturned_detached_handles()
        jobs = self._termination_jobs()
        results: Dict[str, CleanupResult] = {}
        self._termination_cleanup_results = results
        if not jobs:
            return results

        orchestrator = getattr(self, "orchestrator", None)
        from sag.runtime.container_io import resolve_control_execute

        execute = resolve_control_execute(orchestrator) if orchestrator is not None else None
        reason_text = str(getattr(reason, "value", reason) or "termination")
        grace_seconds = max(
            0,
            int(getattr(getattr(self, "config", None), "cancel_cleanup_grace_seconds", 10)),
        )
        for job_id, job in jobs.items():
            if not callable(execute):
                result = CleanupResult(
                    job_id=job_id,
                    pgid=int(job.get("pgid") or 0),
                    code="cleanup_control_transport_unavailable",
                    group_live=True,
                )
            else:
                result = cancel_registered_process_group(
                    execute,
                    job,
                    grace_seconds=grace_seconds,
                )
            results[job_id] = result
            if result.group_live:
                logger.warning(
                    f"Detached job {job_id} remained live during {reason_text}: {result.code}"
                )
            else:
                logger.info(f"Detached job {job_id} closed during {reason_text}: {result.code}")
        return dict(results)

    @staticmethod
    def _execution_record_state(
        record: ToolExecutionRecord | Mapping[str, Any],
    ) -> tuple[str, Optional[InvocationStatus], Optional[OperationOutcome]]:
        if isinstance(record, ToolExecutionRecord):
            return record.signature, record.invocation_status, record.operation_outcome
        signature = str(record.get("signature") or "").strip()
        try:
            invocation_status = InvocationStatus(record.get("invocation_status"))
        except (TypeError, ValueError):
            invocation_status = None
        try:
            operation_outcome = OperationOutcome(record.get("operation_outcome"))
        except (TypeError, ValueError):
            operation_outcome = None
        return signature, invocation_status, operation_outcome

    def _run_task_completion_conflicts(self) -> Tuple[str, ...]:
        """Structured blockers to a free-form run-task terminal answer."""

        latest: Optional[tuple[str, Optional[InvocationStatus], Optional[OperationOutcome]]] = None
        for record in getattr(self, "recent_tool_executions", ()) or ():
            if not isinstance(record, (ToolExecutionRecord, Mapping)):
                continue
            signature, invocation_status, operation_outcome = self._execution_record_state(record)
            if signature:
                latest = (signature, invocation_status, operation_outcome)

        conflicts: List[str] = []
        if latest is None:
            conflicts.append("no task tool evidence")
        else:
            signature, invocation_status, operation_outcome = latest
            if operation_outcome is OperationOutcome.FAILED:
                conflicts.append(f"unrepaired failure: {signature}")
            elif invocation_status is InvocationStatus.PENDING:
                orchestrator = getattr(self, "orchestrator", None)
                records = None
                if orchestrator is not None:
                    try:
                        records = self._obligations_still_owed(orchestrator)
                    except Exception:
                        records = None
                if records is None or any(blocks_model(record) for record in records):
                    conflicts.append(f"unfinished runner: {signature}")
            elif invocation_status in {
                InvocationStatus.TIMEOUT,
                InvocationStatus.CRASHED,
                InvocationStatus.CANCELLED,
            }:
                conflicts.append(f"terminal tool failure: {signature}")
        for job_id in self._ephemeral_job_handles():
            conflicts.append(f"unfinished runner: {job_id}")
        return tuple(conflicts)

    def _run_task_completion_refusal(self, text: str) -> Optional[str]:
        candidate = str(text or "").strip()
        if not candidate.startswith(_RUN_TASK_COMPLETE_PREFIX):
            return (
                "The task is not closed. Continue with a tool call, or when tool results "
                f"prove completion reply with {_RUN_TASK_COMPLETE_PREFIX} <summary>."
            )
        if not candidate[len(_RUN_TASK_COMPLETE_PREFIX) :].strip():
            return "TASK COMPLETE requires a non-empty evidence-backed summary."
        conflicts = self._run_task_completion_conflicts()
        if conflicts:
            return (
                "TASK COMPLETE was rejected because execution state is unresolved: "
                + "; ".join(conflicts)
                + ". Repair or close that state before claiming completion."
            )
        return None

    def _capture_job_barrier_from_result(self) -> bool:
        """Register a detached handle even when its complete ledger write failed."""
        try:
            result = self._answered_action_result()
        except Exception:
            result = None
        metadata = getattr(result, "metadata", None) or {}
        job_id = str(metadata.get("job_id") or "").strip()
        persistence = metadata.get("job_obligation_persisted")
        persistence_present = "job_obligation_persisted" in metadata
        dispatch_status = str(metadata.get("dispatch_status") or "").strip()
        detached_status = dispatch_status in {
            "running_detached",
            "liveness_unknown_detached",
            "dispatch_unknown",
        }
        invocation_status = getattr(getattr(result, "invocation_status", None), "value", "")
        poll_ref = str(getattr(result, "poll_ref", None) or "").strip()
        source_tool = next(
            (
                str(getattr(step, "tool_name", None) or "").strip()
                for step in reversed(getattr(self, "steps", None) or ())
                if getattr(step, "step_type", None) is StepType.ACTION
            ),
            "",
        )
        detached_shaped = bool(
            detached_status
            or persistence_present
            or (invocation_status == "pending" and (job_id or poll_ref))
        )

        # A running SearchTool result observes an EXISTING dispatch.  Its
        # obligation acknowledgement belongs to the original runner, so the
        # poll quite correctly carries none.  Require the matching durable
        # ledger witness instead of misclassifying it as a new malformed
        # handoff; either way it is an immediate same-turn barrier.
        is_existing_job_poll = bool(
            source_tool == "search"
            and detached_status
            and not persistence_present
            and job_id
            and poll_ref == f"job:{job_id}"
        )
        if is_existing_job_poll:
            # The same reader the rest of this method uses: a job the run has
            # already disclosed as live at close is no longer owed, so it can
            # no longer back a barrier claim either.
            records = self._obligations_still_owed(getattr(self, "orchestrator", None))
            if records is None:
                self._record_job_barrier_integrity_failure(
                    (f"detached_result_poll_ledger_unreadable:{job_id}",)
                )
            elif not any(
                str(record.get("job_id") or "").strip() == job_id and blocks_model(record)
                for record in records
            ):
                self._record_job_barrier_integrity_failure(
                    (f"detached_result_poll_obligation_missing:{job_id}",)
                )
            return True

        if detached_shaped:
            failures: List[str] = []
            if not detached_status:
                failures.append("detached_result_dispatch_status_invalid")
            if not persistence_present or not isinstance(persistence, bool):
                failures.append("detached_result_persistence_invalid")
            if not job_id:
                failures.append("detached_result_job_id_missing")
            if poll_ref and job_id and poll_ref != f"job:{job_id}":
                failures.append("detached_result_poll_ref_mismatch")
            if metadata.get("terminal_authority") != DETACHED_TERMINAL_AUTHORITY:
                failures.append("detached_result_terminal_authority_missing")
            for field in ("docker_exec_id", "container_id"):
                if re.fullmatch(r"[0-9a-f]{64}", str(metadata.get(field) or "")) is None:
                    failures.append(f"detached_result_{field}_missing")
            start_accepted = metadata.get("start_accepted")
            startup_identity_verified = metadata.get("startup_identity_verified")
            started = metadata.get("started")
            runner_dispatch_state = str(metadata.get("runner_dispatch_state") or "")
            runner_dispatched = metadata.get("runner_dispatched")
            if type(start_accepted) is not bool:
                failures.append("detached_result_start_acceptance_missing")
            if (
                type(startup_identity_verified) is not bool
                or type(started) is not bool
                or startup_identity_verified is not started
            ):
                failures.append("detached_result_startup_identity_invalid")
            if persistence is True and startup_identity_verified is not True:
                failures.append("detached_result_persisted_identity_unverified")
            elif dispatch_status == "dispatch_unknown":
                if (
                    start_accepted is not False
                    or startup_identity_verified is not False
                    or started is not False
                    or runner_dispatch_state != "unknown"
                    or runner_dispatched is not None
                    or persistence is not False
                    or str(metadata.get("job_obligation_persistence_code") or "")
                    != "invalid_dispatch_handle"
                ):
                    failures.append("detached_result_unknown_dispatch_inconsistent")
            elif (
                start_accepted is not True
                or runner_dispatch_state != "accepted"
                or runner_dispatched is not True
            ):
                failures.append("detached_result_start_acceptance_unproven")
            if not str(metadata.get("log_path") or "").strip():
                failures.append("detached_result_log_path_missing")
            if failures:
                self._record_job_barrier_integrity_failure(failures)
                return True
        memory = getattr(self, "loop_memory", None)
        if job_id and memory is not None and metadata.get("start_accepted") is True:
            memory.observe_job_transition(
                job_id,
                "running",
                progress_fingerprint=canonical_sha256(
                    {
                        "pid": metadata.get("pid"),
                        "pgid": metadata.get("pgid"),
                        "pid_path": metadata.get("pid_path"),
                        "pgid_path": metadata.get("pgid_path"),
                        "log_path": metadata.get("log_path"),
                    }
                ),
            )
        if persistence is False and job_id:
            self._ephemeral_job_handles()[job_id] = {
                key: metadata.get(key)
                for key in (
                    "job_id",
                    "pid",
                    "pgid",
                    "pid_path",
                    "pgid_path",
                    "identity_path",
                    "process_identity_token",
                    "terminal_authority",
                    "docker_exec_id",
                    "container_id",
                    "start_accepted",
                    "startup_identity_verified",
                    "started",
                    "runner_dispatch_state",
                    "runner_dispatched",
                    "log_path",
                    "exit_code_path",
                    "handoff_reason",
                    "working_directory",
                    "job_obligation_persistence_code",
                )
            }
            return True
        if persistence is False:
            self._ephemeral_job_handles()["unidentified-detached-job"] = {
                "job_id": "unidentified-detached-job",
                "job_obligation_persistence_code": str(
                    metadata.get("job_obligation_persistence_code") or "invalid_dispatch_handle"
                ),
            }
            return True
        if persistence is True and job_id:
            # The dispatch result is the write acknowledgement.  Re-reading
            # the ledger here is both redundant and unsafe: a transient read
            # failure used to turn a successfully persisted detached job into
            # ``False`` and let later calls in the same model turn execute.
            # The next controller drain performs the full typed reconciliation.
            return True
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return False
        records = self._obligations_still_owed(orchestrator)
        if records is None:
            self._record_job_barrier_integrity_failure(("ledger_unreadable_after_tool_result",))
            return True
        return bool(records and any(blocks_model(record) for record in records))

    def _announce_job_reconciliation(self, reconciliation: ObligationReconciliation) -> None:
        """Project typed lifecycle facts before the model can receive a turn."""
        lines: List[str] = []
        state = getattr(self, "run_evidence_state", None)

        terminal_announced = self._assessment_guard("_announced_job_terminals")
        for terminal in reconciliation.terminal_observations:
            if terminal.job_id in terminal_announced:
                continue
            terminal_announced.add(terminal.job_id)
            self._emit_control_event("job_terminal_observed", terminal.event_payload())
            lines.append(
                f"[job terminal] job {terminal.job_id}: exit {terminal.exit_code}; "
                "receipt settlement pending"
            )

        settled_announced = self._assessment_guard("_announced_job_settlements")
        for settlement in reconciliation.settlements:
            if settlement.job_id in settled_announced:
                continue
            settled_announced.add(settlement.job_id)
            self._emit_control_event("job_settled", settlement.event_payload())
            lines.append(settlement.notice())

        unpersisted_announced = self._assessment_guard("_announced_job_terminal_unpersisted")
        for terminal in reconciliation.terminal_unpersisted:
            if terminal.job_id in unpersisted_announced:
                continue
            unpersisted_announced.add(terminal.job_id)
            self._emit_control_event("job_terminal_unpersisted", terminal.event_payload())
            lines.append(terminal.notice())
            if state is not None and not state.sealed:
                state.set_fact(
                    f"job_terminal_unpersisted.{terminal.job_id}",
                    terminal.event_payload(),
                    evidence_ref=terminal.obligation_ref,
                )
                state.record_conflict(f"job_terminal_unpersisted:{terminal.job_id}")

        if lines and hasattr(self, "steps"):
            self.steps.append(
                ReActStep(
                    step_type=StepType.SYSTEM_GUIDANCE,
                    content="\n".join(lines),
                    timestamp=self._get_timestamp(),
                )
            )

    def _reconcile_ephemeral_jobs(self) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        """Wait for runner handles whose complete obligation never landed."""
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return (), ("ephemeral_job_orchestrator_unavailable",)
        running: List[str] = []
        failures: List[str] = []
        state = getattr(self, "run_evidence_state", None)
        announced = self._assessment_guard("_announced_ephemeral_job_terminals")
        handles = self._ephemeral_job_handles()
        records = read_obligations(orchestrator) if handles else ()
        durable = {record["job_id"]: record for record in records or ()}
        for job_id, handle in list(handles.items()):
            # Cancellation also recovers remembered handles whose complete
            # obligation already landed. Their published ledger owns normal
            # reconciliation; the memory copy does not prove a failed write.
            record = durable.get(job_id)
            if record is not None and all(
                handle.get(key) and handle[key] == record.get(key)
                for key in (
                    "terminal_authority",
                    "docker_exec_id",
                    "container_id",
                    "process_identity_token",
                )
            ):
                handles.pop(job_id, None)
                continue
            observation = observe_detached_terminal(orchestrator, handle)
            if observation.state == "running":
                if observation.start_accepted:
                    # Running=true came from the Docker daemon, so an
                    # ambiguous exec_start response is now resolved without
                    # trusting any container-written PID/log marker.
                    handle["start_accepted"] = True
                    handle["runner_dispatch_state"] = "accepted"
                    handle["runner_dispatched"] = True
                    memory = getattr(self, "loop_memory", None)
                    if memory is not None:
                        memory.observe_job_transition(
                            job_id,
                            "running",
                            progress_fingerprint=canonical_sha256(
                                {
                                    "docker_exec_id": handle.get("docker_exec_id"),
                                    "container_id": handle.get("container_id"),
                                    "start_accepted": True,
                                }
                            ),
                        )
                running.append(job_id)
                continue
            if observation.state != "terminal":
                failures.append(f"{job_id}:terminal_authority_{observation.state}")
                continue
            if job_id not in announced:
                announced.add(job_id)
                terminal_payload = {
                    "job_id": job_id,
                    "exit_code": observation.exit_code,
                    "marker_ref": observation.marker_ref,
                    "obligation_ref": "unpersisted",
                    "observed_at": self._get_timestamp(),
                }
                self._emit_control_event("job_terminal_observed", terminal_payload)
                unpersisted_payload = {
                    "job_id": job_id,
                    "exit_code": observation.exit_code,
                    "attempted_receipt_id": "",
                    "persistence_code": str(
                        handle.get("job_obligation_persistence_code")
                        or "obligation_transport_write_failed"
                    ),
                    "attempt_count": 0,
                    "obligation_ref": "unpersisted",
                    "log_ref": str(handle.get("log_path") or ""),
                }
                self._emit_control_event("job_terminal_unpersisted", unpersisted_payload)
                if state is not None and not state.sealed:
                    state.set_fact(
                        f"job_terminal_unpersisted.{job_id}",
                        unpersisted_payload,
                        evidence_ref=str(handle.get("log_path") or "unpersisted"),
                    )
                    state.record_conflict(f"job_terminal_unpersisted:{job_id}")
                if hasattr(self, "steps"):
                    self.steps.append(
                        ReActStep(
                            step_type=StepType.SYSTEM_GUIDANCE,
                            content=(
                                f"[terminal-unpersisted] job {job_id}: exit "
                                f"{observation.exit_code}; detached obligation was not persisted"
                            ),
                            timestamp=self._get_timestamp(),
                        )
                    )
                self._job_barrier_evidence_unpersisted = True
            self._ephemeral_job_handles().pop(job_id, None)
        return tuple(running), tuple(failures)

    def _record_live_jobs_at_close(self, job_ids: Sequence[str], reason: Any) -> None:
        state = getattr(self, "run_evidence_state", None)
        records = read_obligations(getattr(self, "orchestrator", None))
        if records is None:
            self._record_job_barrier_integrity_failure(
                ("ledger_unreadable_while_recording_live_jobs",)
            )
            return
        by_id = {str(record.get("job_id") or ""): record for record in records}
        announced = self._assessment_guard("_announced_jobs_live_at_close")
        reason_text = getattr(reason, "value", None) or str(reason or "deadline")
        for job_id in sorted(set(job_ids)):
            if not job_id or job_id in announced:
                continue
            durable = by_id.get(job_id)
            ephemeral = self._ephemeral_job_handles().get(job_id)
            if durable is not None and not process_is_live(durable):
                # The process crossed terminal settlement after the wall
                # observation but before this projection.  Current ledger
                # truth wins; do not preserve a stale live claim.
                continue
            if durable is None and ephemeral is None:
                self._record_job_barrier_integrity_failure(
                    (f"{job_id}:live_projection_record_missing",)
                )
                continue
            announced.add(job_id)
            record = durable or ephemeral or {}
            payload = compact_control_value(
                {
                    "job_id": job_id,
                    "obligation_ref": (
                        f"{OBLIGATION_DIR}/{job_id}.json" if job_id in by_id else "unpersisted"
                    ),
                    "log_ref": str(record.get("log_path") or ""),
                    "close_reason": reason_text,
                }
            )
            self._emit_control_event("job_live_at_close", payload)
            if state is not None and not state.sealed:
                state.set_fact(
                    f"job_live_at_close.{job_id}",
                    payload,
                    evidence_ref=payload["obligation_ref"],
                )
                state.record_conflict(f"job_live_at_close:{job_id}")

    #: One heartbeat in this many otherwise-identical waits (~5 minutes at the
    #: 30s cadence). A bound on the record, never on the poll.
    _BARRIER_WAIT_EVERY = 10

    def _record_job_barrier_waits(
        self,
        job_ids: Sequence[str],
        *,
        progress_states: Dict[str, Dict[str, Any]],
        job_records: Mapping[str, Mapping[str, Any]],
        remaining: float,
        observed_at: float,
    ) -> None:
        """Give the wait a voice in the authoritative stream (task #53).

        p7d camel's controller polled 160 times over 5,033.9s and wrote nothing:
        every job kind on this path is exceptional, so forensics had to read
        console logs to learn the barrier had run at all — and the shape of the
        hang (a log frozen at 186,675,667 bytes, one 10ms CPU tick every few
        probes) was legible nowhere while it was happening.

        Bounded, so 160 identical probes are not 160 rows: the first wait for a
        job, any wait whose physical observation changed, and one in every
        `_BARRIER_WAIT_EVERY` after that. It concludes nothing and ends nothing
        — `_emit_control_event` swallows a sink failure for this kind, because
        observability never ends a run.
        """
        for job_id in job_ids:
            state = progress_states.setdefault(job_id, {})
            waits = int(state.get("waits") or 0) + 1
            state["waits"] = waits
            state.setdefault("first_wait_at", observed_at)
            snapshot = state.get("snapshot")
            snapshot = snapshot if isinstance(snapshot, JobProgressSnapshot) else None
            digest = canonical_sha256(snapshot.as_dict() if snapshot is not None else {})
            changed = digest != state.get("wait_digest")
            state["wait_digest"] = digest
            if not (changed or waits % self._BARRIER_WAIT_EVERY == 1):
                continue
            record = job_records.get(job_id) or {}
            payload = {
                "job_id": job_id,
                "obligation_ref": (
                    "unpersisted"
                    if record.get("_ephemeral") is True
                    else f"{OBLIGATION_DIR}/{job_id}.json"
                ),
                "waits": waits,
                "waited_seconds": max(int(observed_at - float(state["first_wait_at"])), 0),
                "remaining_seconds": max(int(remaining), 0),
                # An unobserved wait says so rather than borrowing the last
                # snapshot's state: a schema-v1 obligation has no diagnostic
                # identity and this row must not imply one.
                "process_state": snapshot.process_state if snapshot is not None else "unobserved",
                "log_size": snapshot.log_size if snapshot is not None else 0,
                "cpu_ticks_delta": snapshot.cpu_ticks_delta if snapshot is not None else 0,
                "artifact_sha256": snapshot.artifact_sha256 if snapshot is not None else "",
                "report_sha256": snapshot.report_sha256 if snapshot is not None else "",
                # The predicate the controller actually formed this iteration —
                # never re-derived from the stall clock. `last_progress_at` is
                # SEEDED on absence so the stall window can start, and reading
                # that seed back made the first row of every job announce
                # progress it had never observed.  None means no predicate: no
                # prior sample to compare with, or no observation at all.
                "progressing": state.get("progressing"),
            }
            self._emit_control_event("job_barrier_wait", payload)

    def _record_job_barrier_integrity_failure(self, failures: Sequence[str]) -> None:
        """Persist the controller failure in the same shape replay rebuilds."""
        normalized = tuple(
            dict.fromkeys(str(item).strip() for item in failures if str(item).strip())
        )
        if not normalized:
            normalized = ("job_barrier_integrity_failure",)
        # The control record/run fact is the durable witness; this engine-held
        # flag guarantees no further model turn can slip through when the
        # malformed result left no ledger or ephemeral handle to sweep.
        if not str(getattr(self, "_fatal_harness_control_failure", "") or "").strip():
            self._fatal_harness_control_failure = normalized[0]
        announced = self._assessment_guard("_announced_job_barrier_integrity_failures")
        if announced:
            return
        announced.add("recorded")
        payload = {"failures": list(normalized)}
        self._emit_control_event("job_barrier_integrity_failure", payload)
        state = getattr(self, "run_evidence_state", None)
        if state is not None and not state.sealed:
            state.set_fact(
                "job_barrier_integrity_failure",
                payload,
                evidence_ref="control:job_barrier_integrity_failure",
            )
            state.record_conflict("job_barrier_integrity_failure")

    def _disclosed_live_jobs(self) -> set:
        """Jobs this run has already disclosed as live at the report reserve.

        The in-process announcement guard and the durable run-state conflict
        say the same thing; a resumed run has only the second, so both are read.
        """
        disclosed = set(self._assessment_guard("_announced_jobs_live_at_close"))
        disclosed.update(disclosed_live_job_ids(getattr(self, "run_evidence_state", None)))
        return disclosed

    def _obligations_still_owed(self, orchestrator) -> Optional[List[Dict[str, Any]]]:
        """The ledger records the controller is still answerable for.

        A disclosure is the LAST lifecycle word about a job: after
        `job_live_at_close` the controller neither waits on it nor settles it,
        because `job_settled` (or a terminal observation) after that disclosure
        is not a lifecycle any reader — replay included — can walk. The run
        keeps the honest conflict it already recorded instead.

        None still means "the ledger could not be read"; it never means empty.
        """
        records = read_obligations(orchestrator)
        if records is None:
            return None
        disclosed = self._disclosed_live_jobs()
        if not disclosed:
            return records
        return [
            record for record in records if str(record.get("job_id") or "").strip() not in disclosed
        ]

    def _job_barrier_progress_states(self) -> Dict[str, Dict[str, Any]]:
        states = getattr(self, "_job_barrier_progress_by_job", None)
        if states is None:
            states = {}
            self._job_barrier_progress_by_job = states
        return states

    def _record_job_stall_observation(
        self,
        job: Mapping[str, Any],
        result: StallControlResult,
    ) -> None:
        """Mirror one durable physical diagnostic into event and live state."""

        diagnostic = result.diagnostic
        if diagnostic is None or not diagnostic.persisted:
            return
        cleanup = result.cleanup
        progress_signals = tuple(result.progress.signals if result.progress else ())
        payload = {
            "job_id": result.job_id,
            "obligation_ref": (
                "unpersisted"
                if job.get("_ephemeral") is True
                else f"{OBLIGATION_DIR}/{result.job_id}.json"
            ),
            "diagnostic_ref": diagnostic.evidence_ref,
            "diagnostic_fingerprint": diagnostic.diagnostic_fingerprint,
            "observation": diagnostic.observation,
            "progress_signals": list(progress_signals),
            "controller_code": result.code,
            "evidence_sealed": bool(result.seal and result.seal.persisted),
            "term_sent": bool(cleanup and cleanup.term_sent),
            "kill_sent": bool(cleanup and cleanup.kill_sent),
            # A cleanup refusal cannot prove the group dead. Only the cleanup
            # result's explicit terminal predicate may serialize false.
            "group_live": (
                False if cleanup is not None and cleanup.process_group_terminal else True
            ),
        }
        fingerprint = canonical_sha256(payload)
        announced = self._assessment_guard("_announced_job_stall_observations")
        if fingerprint in announced:
            return
        announced.add(fingerprint)
        self._emit_control_event("job_stall_observed", payload)
        state = getattr(self, "run_evidence_state", None)
        if state is not None and not state.sealed:
            state.set_fact(
                f"job_stall_observed.{result.job_id}.{fingerprint[:12]}",
                payload,
                evidence_ref=diagnostic.evidence_ref,
            )
        memory = getattr(self, "loop_memory", None)
        if memory is not None:
            transition = job_stall_transition(payload)
            if transition is not None:
                memory.observe_job_transition(
                    result.job_id,
                    transition,
                    progress_fingerprint=fingerprint,
                )

    @staticmethod
    def _stall_control_failure(result: StallControlResult) -> str:
        nested = ""
        if result.progress is not None and result.progress.code != "observed":
            nested = f":{result.progress.code}"
        elif result.diagnostic is not None and not result.diagnostic.persisted:
            nested = f":{result.diagnostic.code}"
        elif result.seal is not None and not result.seal.persisted:
            nested = f":{result.seal.code}"
        elif result.cleanup is not None:
            nested = f":{result.cleanup.code}"
        return f"{result.job_id}:stall_control_{result.code}{nested}"

    @staticmethod
    def _has_stall_control_identity(job: Mapping[str, Any]) -> bool:
        """Whether WS9 may observe/signal this dispatch without guessing."""

        pid = job.get("pid")
        pgid = job.get("pgid")
        token = str(job.get("process_identity_token") or "")
        docker_exec_id = str(job.get("docker_exec_id") or "")
        container_id = str(job.get("container_id") or "")
        return bool(
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and pgid == pid
            and re.fullmatch(r"[0-9a-f]{64}", token)
            and job.get("terminal_authority") == DETACHED_TERMINAL_AUTHORITY
            and re.fullmatch(r"[0-9a-f]{64}", docker_exec_id)
            and re.fullmatch(r"[0-9a-f]{64}", container_id)
            and job.get("start_accepted") is True
            and job.get("startup_identity_verified") is True
            and job.get("runner_dispatch_state") == "accepted"
        )

    def _await_cleanup_marker(
        self,
        job_id: str,
        *,
        now,
        sleep,
    ) -> str:
        """Let the external supervisor publish exit, then settle normally."""

        deadline = self._hold_deadline()
        for _ in range(self._JOB_MARKER_RECONCILE_ATTEMPTS):
            reconciliation = reconcile_job_obligations(
                self.orchestrator,
                obligations=self._obligations_still_owed(self.orchestrator),
            )
            self._announce_job_reconciliation(reconciliation)
            if reconciliation.terminal_unpersisted or getattr(
                self, "_job_barrier_evidence_unpersisted", False
            ):
                return "evidence_unpersisted"
            if reconciliation.integrity_failures:
                self._record_job_barrier_integrity_failure(reconciliation.integrity_failures)
                return "integrity_failure"
            active = set(reconciliation.running_job_ids) | set(
                reconciliation.settlement_pending_job_ids
            )
            if job_id not in active:
                return "cleared"
            if job_id in reconciliation.settlement_pending_job_ids:
                # Settlement retries are immediate and bounded by WS2.
                continue
            if deadline is not None and now() >= deadline:
                break
            sleep(1.0)
        self._record_job_barrier_integrity_failure(
            (f"{job_id}:terminal_marker_missing_after_cleanup",)
        )
        return "integrity_failure"

    def _drain_job_barrier(self, reason=None, *, now=None, sleep=None) -> str:
        """Poll, diagnose, clean, and settle jobs without model/advisor turns."""
        import time as _time

        now = now or _time.time
        sleep = sleep or _time.sleep
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            if self._ephemeral_job_handles():
                self._record_job_barrier_integrity_failure(
                    ("ephemeral_job_orchestrator_unavailable",)
                )
                return "integrity_failure"
            return "cleared"
        records = self._obligations_still_owed(orchestrator)
        ephemeral = bool(self._ephemeral_job_handles())
        if records == [] and not ephemeral:
            return "cleared"
        from sag.runtime.container_io import resolve_control_execute

        execute = resolve_control_execute(orchestrator)
        if not callable(execute):
            self._record_job_barrier_integrity_failure(("orchestrator_unavailable",))
            return "integrity_failure"
        wait_for_running = reason not in (
            EvidenceCloseReason.ABORTED,
            EvidenceCloseReason.CANCELLED,
        )
        integrity_attempts = 0
        progress_states = self._job_barrier_progress_states()
        stall_seconds = max(
            0,
            int(getattr(getattr(self, "config", None), "dispatch_stall_seconds", 600)),
        )
        while True:
            reconciliation = reconcile_job_obligations(
                orchestrator,
                obligations=self._obligations_still_owed(orchestrator),
            )
            self._announce_job_reconciliation(reconciliation)
            ephemeral_running, ephemeral_failures = self._reconcile_ephemeral_jobs()
            running = tuple(sorted(set(reconciliation.running_job_ids + ephemeral_running)))
            durable_records = self._obligations_still_owed(orchestrator)
            failures = list(reconciliation.integrity_failures) + list(ephemeral_failures)
            if durable_records is None:
                failures.append("ledger_unreadable_after_reconciliation")
                durable_records = []
            durable_by_id = {
                str(record.get("job_id") or ""): dict(record)
                for record in durable_records
                if str(record.get("job_id") or "")
            }
            job_records: Dict[str, Dict[str, Any]] = dict(durable_by_id)
            for job_id, handle in self._ephemeral_job_handles().items():
                job_records.setdefault(job_id, {**handle, "_ephemeral": True})
            if reconciliation.terminal_unpersisted or getattr(
                self, "_job_barrier_evidence_unpersisted", False
            ):
                return "evidence_unpersisted"
            if failures:
                integrity_attempts += 1
                if integrity_attempts >= self._JOB_INTEGRITY_RETRIES:
                    self._record_job_barrier_integrity_failure(failures)
                    return "integrity_failure"
                deadline = self._hold_deadline()
                remaining = max(deadline - now(), 0.0) if deadline is not None else 0.0
                sleep(min(1.0, remaining))
                continue
            integrity_attempts = 0
            if reconciliation.settlement_pending_job_ids:
                # Persistence recovery is immediate and bounded; it is not a
                # process poll and spends no model iteration.
                continue
            if not running:
                progress_states.clear()
                return "cleared"
            deadline = self._hold_deadline()
            if not wait_for_running or deadline is None:
                self._record_live_jobs_at_close(running, reason)
                return "live_at_deadline"

            observed_at = now()
            if observed_at >= deadline:
                terminal_jobs: List[str] = []
                verified_live_jobs: List[str] = []
                wall_guard_failures: List[str] = []
                for job_id in running:
                    job = job_records.get(job_id)
                    if not job:
                        wall_guard_failures.append(
                            f"{job_id}:wall_guard_registered_job_record_missing"
                        )
                        continue
                    if not self._has_stall_control_identity(job):
                        # An exit marker that has not appeared says only that
                        # terminal truth is unavailable.  Without the launcher's
                        # fenced identity, the wall guard cannot make the
                        # stronger physical claim that the job is still live.
                        wall_guard_failures.append(f"{job_id}:wall_guard_identity_unavailable")
                        continue
                    prior = progress_states.get(job_id, {}).get("snapshot")
                    result = control_stalled_job(
                        execute,
                        job,
                        trigger=WALL_GUARD_TRIGGER,
                        previous=prior if isinstance(prior, JobProgressSnapshot) else None,
                        now=now,
                        sleep=sleep,
                    )
                    if result.progress is not None and result.progress.code == "observed":
                        progress_states.setdefault(job_id, {})[
                            "snapshot"
                        ] = result.progress.snapshot
                    if result.code == "terminal_no_wait":
                        terminal_jobs.append(job_id)
                    elif (
                        result.progress is not None
                        and result.progress.code == "observed"
                        and result.progress.snapshot.process_state == "running"
                    ):
                        verified_live_jobs.append(job_id)
                    else:
                        wall_guard_failures.append(self._stall_control_failure(result))
                for job_id in terminal_jobs:
                    marker_status = self._await_cleanup_marker(job_id, now=now, sleep=sleep)
                    if marker_status != "cleared":
                        return marker_status
                if terminal_jobs:
                    continue
                if verified_live_jobs:
                    self._record_live_jobs_at_close(verified_live_jobs, reason)
                if wall_guard_failures:
                    self._record_job_barrier_integrity_failure(wall_guard_failures)
                    return "integrity_failure"
                if not verified_live_jobs:
                    self._record_job_barrier_integrity_failure(
                        ("wall_guard_produced_no_terminal_or_live_observation",)
                    )
                    return "integrity_failure"
                return "live_at_deadline"

            control_failures: List[str] = []
            live_after_cleanup: List[str] = []
            marker_jobs: List[str] = []
            for job_id in running:
                job = job_records.get(job_id)
                if not job:
                    control_failures.append(f"{job_id}:registered_job_record_missing")
                    continue
                if not self._has_stall_control_identity(job):
                    # Schema-v1 obligations predate safe PGID identity. They
                    # keep the controller barrier and ordinary exit-marker
                    # reconciliation, but never gain diagnostic/signal
                    # authority by inference.
                    continue
                progress_state = progress_states.setdefault(job_id, {})
                previous = progress_state.get("snapshot")
                previous_snapshot = previous if isinstance(previous, JobProgressSnapshot) else None
                handoff_stall = str(
                    job.get("handoff_reason") or ""
                ) == "stalled" and not progress_state.get("handoff_stall_consumed")
                due_for_confirmation = handoff_stall

                if not handoff_stall:
                    # A progress predicate is a COMPARISON. Whether one was
                    # available this iteration is decided here, before the
                    # sample that becomes the next one's predecessor, and it is
                    # what the barrier's wait row states — the stall clock
                    # below is seeded on absence and can never answer it.
                    comparable = previous_snapshot is not None
                    progress = probe_job_progress(
                        execute,
                        job,
                        previous=previous_snapshot,
                    )
                    if progress.code != "observed":
                        progress_state["progressing"] = None
                        failures_seen = int(progress_state.get("probe_failures") or 0) + 1
                        progress_state["probe_failures"] = failures_seen
                        if failures_seen >= self._JOB_INTEGRITY_RETRIES:
                            control_failures.append(
                                f"{job_id}:stall_control_progress_unobservable:" f"{progress.code}"
                            )
                        continue
                    progress_state["probe_failures"] = 0
                    progress_state["progressing"] = (
                        bool(progress.progressing) if comparable else None
                    )
                    progress_state["snapshot"] = progress.snapshot
                    previous_snapshot = progress.snapshot
                    if progress.snapshot.process_state == "terminal":
                        marker_jobs.append(job_id)
                        continue
                    if progress.progressing:
                        progress_state["last_progress_at"] = observed_at
                        memory = getattr(self, "loop_memory", None)
                        if memory is not None:
                            memory.observe_job_transition(
                                job_id,
                                "progress",
                                progress_fingerprint=canonical_sha256(
                                    {
                                        "snapshot": progress.snapshot.as_dict(),
                                        "signals": progress.signals,
                                    }
                                ),
                            )
                    elif "last_progress_at" not in progress_state:
                        progress_state["last_progress_at"] = observed_at
                    due_for_confirmation = bool(
                        stall_seconds > 0
                        and observed_at - float(progress_state["last_progress_at"]) >= stall_seconds
                    )

                if not due_for_confirmation:
                    continue
                progress_state["handoff_stall_consumed"] = True
                result = control_stalled_job(
                    execute,
                    job,
                    trigger=STALL_CONFIRMATION_TRIGGER,
                    previous=previous_snapshot,
                    now=now,
                    sleep=sleep,
                )
                self._record_job_stall_observation(job, result)
                if result.progress is not None and result.progress.code == "observed":
                    progress_state["snapshot"] = result.progress.snapshot
                if result.code == "progress_observed":
                    # The confirmation compared two samples of its own and saw
                    # the job move; that IS this iteration's predicate.
                    progress_state["progressing"] = True
                    progress_state["last_progress_at"] = now()
                    continue
                if result.code == "terminal_no_wait":
                    marker_jobs.append(job_id)
                    continue
                if result.confirmed_stall and result.cleanup is not None:
                    if result.marker_reconciliation_required:
                        marker_jobs.append(job_id)
                        continue
                    # No supervisor marker and no physically terminal cleanup
                    # means the ledger still truthfully carries a potentially
                    # live job, even if identity verification itself failed.
                    live_after_cleanup.append(job_id)
                    control_failures.append(self._stall_control_failure(result))
                    continue
                failures_seen = int(progress_state.get("control_failures") or 0) + 1
                progress_state["control_failures"] = failures_seen
                if failures_seen >= self._JOB_INTEGRITY_RETRIES:
                    control_failures.append(self._stall_control_failure(result))

            if control_failures:
                if live_after_cleanup:
                    self._record_live_jobs_at_close(live_after_cleanup, "cleanup_failed")
                self._record_job_barrier_integrity_failure(control_failures)
                return "integrity_failure"
            if marker_jobs:
                for job_id in sorted(set(marker_jobs)):
                    marker_status = self._await_cleanup_marker(job_id, now=now, sleep=sleep)
                    if marker_status != "cleared":
                        return marker_status
                continue

            remaining = deadline - now()
            logger.info(
                f"controller job barrier waiting for {', '.join(running)}: "
                f"{remaining:.0f}s remain before the report reserve"
            )
            self._record_job_barrier_waits(
                running,
                progress_states=progress_states,
                job_records=job_records,
                remaining=remaining,
                observed_at=observed_at,
            )
            sleep(min(self._OBLIGATION_POLL_SECONDS, remaining))

    def _finalize_evidence(self, reason: EvidenceCloseReason):
        state = getattr(self, "run_evidence_state", None)
        finalizer = getattr(self, "verdict_finalizer", None)
        if state is None or finalizer is None:
            raise RuntimeError("setup evidence finalization is not configured")
        was_sealed = state.sealed
        if not was_sealed:
            # Plan 8 §3.2 trigger 3, in two steps: WAIT for a still-running
            # job while allocated budget remains, then the closing sweep. A
            # job that terminated while the run was finishing still owes a
            # receipt, and a job that never terminated owes the verdict an
            # honest conflict.
            self._await_open_obligations(reason)
            self._sweep_job_obligations()
            self._record_unsettled_job_conflicts(reason)
            callback = getattr(self, "pre_finalize_evidence_callback", None)
            if (
                reason is EvidenceCloseReason.TEST_TERMINATED
                and callback is not None
                and not getattr(self, "_pre_finalize_evidence_attempted", False)
            ):
                self._pre_finalize_evidence_attempted = True
                try:
                    summary = callback()
                except Exception as exc:
                    logger.warning(f"Pre-finalize coverage callback failed: {exc}")
                    summary = {
                        "status": "unavailable",
                        "reason": "coverage pass failed before verdict close",
                    }
                if not isinstance(summary, Mapping):
                    summary = {
                        "status": "unavailable",
                        "reason": "coverage pass returned no summary",
                    }
                state.set_fact(
                    "coverage.summary",
                    dict(summary),
                    evidence_ref="coverage://module-metrics",
                )
        snapshot = finalizer.finalize(state, reason)
        if not was_sealed:
            self._emit_control_event("evidence_close", {"reason": reason.value})
        return snapshot

    def _report_delivery_status(self) -> ReportDeliveryStatus:
        if getattr(self, "_report_delivered", False):
            return ReportDeliveryStatus.DELIVERED
        if getattr(self, "_report_attempted", False) or getattr(self, "_report_failed", False):
            return ReportDeliveryStatus.FAILED
        return ReportDeliveryStatus.SKIPPED

    def _close_flow(self, termination: RunTerminationStatus) -> RunTermination:
        state = getattr(self, "run_evidence_state", None)
        if state is None:
            raise RuntimeError("setup flow closure requires run evidence state")
        sealed_reason = None
        if state.sealed:
            try:
                sealed_reason = EvidenceCloseReason(state.close_reason)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("sealed setup evidence has no typed close reason") from exc

        if sealed_reason in {
            EvidenceCloseReason.TEST_TERMINATED,
            EvidenceCloseReason.DEPENDENTS_SKIPPED,
        }:
            # Evidence-close is immutable. A later report-phase abort or
            # cancellation changes flow/delivery status, never snapshot inputs.
            reason = sealed_reason
        elif termination is RunTerminationStatus.CANCELLED:
            reason = EvidenceCloseReason.CANCELLED
        elif termination is RunTerminationStatus.ABORTED:
            reason = EvidenceCloseReason.ABORTED
        else:
            reason = EvidenceCloseReason.DEPENDENTS_SKIPPED
        # This is a cache hit after a successful evidence-close. If persistence
        # failed after sealing, the same reason safely retries the atomic write.
        self._finalize_evidence(reason)
        return RunTermination(
            termination=termination,
            report_delivery_status=self._report_delivery_status(),
        )

    # Provider errors that a retry can plausibly outlive. Anything not
    # listed — BadRequestError reproduces byte-for-byte (the 2026-08-09
    # wire-schema incident), auth and context-window errors likewise — stays
    # deterministic, fail-closed (spec 2026-08-13 transient containment).
    _TRANSIENT_PROVIDER_ERRORS = (
        "InternalServerError",
        "ServiceUnavailableError",
        "RateLimitError",
        "APIConnectionError",
        "Timeout",
    )
    _NATIVE_TURN_BACKOFF_SECONDS = (5.0, 15.0, 45.0)

    @staticmethod
    def _is_transient_provider_error(exc: BaseException) -> bool:
        import litellm

        for name in ReActEngine._TRANSIENT_PROVIDER_ERRORS:
            cls = getattr(litellm, name, None)
            if isinstance(cls, type) and isinstance(exc, cls):
                return True
        return False

    def _native_turn_with_retry(self, messages, *, sleep=None):
        """One native turn, riding out transient provider failures.

        Bounded at three retries with 5/15/45s backoff (~65s worst case
        against a 7,200s wall clock; the wall guard still runs first every
        iteration, so retrying can never extend a run past its cap). A
        deterministic error propagates immediately; exhaustion propagates the
        last error with the attempt count logged, so the abort that follows
        stays honest — it just stops being trigger-happy.
        """
        import time as _time

        wait = sleep if callable(sleep) else _time.sleep
        attempts = 1 + len(self._NATIVE_TURN_BACKOFF_SECONDS)
        for attempt in range(1, attempts + 1):
            try:
                return self.llm_client.get_native_turn(messages)
            except Exception as exc:
                if not self._is_transient_provider_error(exc) or attempt >= attempts:
                    if attempt > 1:
                        getattr(self, "agent_logger", logger).error(
                            f"Native turn failed after {attempt} attempts: {exc}"
                        )
                    raise
                delay = self._NATIVE_TURN_BACKOFF_SECONDS[attempt - 1]
                getattr(self, "agent_logger", logger).warning(
                    f"Transient provider error (attempt {attempt}/{attempts}), "
                    f"retrying in {delay:.0f}s: {exc}"
                )
                wait(delay)
        raise RuntimeError("unreachable: retry loop returns or raises")

    def abort(self, *, reason: str) -> RunTermination:
        machine = getattr(self, "phase_machine", None)
        if machine is None:
            raise RuntimeError("abort termination is available only for setup runs")
        self._terminate_open_jobs(EvidenceCloseReason.ABORTED)
        if not machine.is_complete:
            record = machine.record_abort(reason, evidence=[], outcome=PhaseOutcome.FAILED)
            self._record_phase_audit(record)
        state = getattr(self, "run_evidence_state", None)
        if state is not None and not state.sealed:
            self._finalize_evidence(EvidenceCloseReason.ABORTED)
        return self._close_flow(RunTerminationStatus.ABORTED)

    def cancel(self, *, reason: str = "explicit cancellation") -> RunTermination:
        machine = getattr(self, "phase_machine", None)
        if machine is None:
            raise RuntimeError("cancel termination is available only for setup runs")
        self._terminate_open_jobs(EvidenceCloseReason.CANCELLED)
        if not machine.is_complete:
            record = machine.record_abort(reason, evidence=[])
            self._record_phase_audit(record)
        state = getattr(self, "run_evidence_state", None)
        if state is not None and not state.sealed:
            self._finalize_evidence(EvidenceCloseReason.CANCELLED)
        return self._close_flow(RunTerminationStatus.CANCELLED)

    # ------------------------------------------------------------------
    # Phase-machine wiring (setup mode only; spec §3.1/§3.2/§7)
    # ------------------------------------------------------------------

    def _phase_budget_numbers(self, phase: str) -> tuple[int, int, int]:
        """(max_iter, reserved_for_later_phases, remaining_iterations)."""
        max_iter = getattr(self, "_run_max_iterations", None) or getattr(
            self.config, "max_iterations", 150
        )
        later = PHASE_NAMES[PHASE_NAMES.index(phase) + 1 :]
        floors = getattr(self.config, "phase_min_floors", {}) or {}
        reserved = sum(effective_phase_floor(floors.get(q, 4), max_iter) for q in later)
        remaining = max_iter - getattr(self, "current_iteration", 0)
        return max_iter, reserved, remaining

    def _test_candidate_survey(self) -> Callable[[], TestCandidateResolution]:
        """The ONE survey read a single close is allowed to spend.

        One close asks the test-coordinate question up to three times — the
        missing-attempt requirement, the unresolved-coordinate cap and the
        forced refusals — and each asked it for itself: three manifest reads
        and three sets of realpath probes for one grading. Two reads of one
        survey can disagree (a manifest rewritten between them, a symlink that
        resolves differently), and then the requirement, the cap and the
        refusals answer different coordinates while the record shows one close.

        The reader stays LAZY, so a close that never asks the question still
        spends no probe (P3: one question, one computation).
        """
        resolved: list[TestCandidateResolution] = []

        def survey() -> TestCandidateResolution:
            if not resolved:
                resolved.append(resolve_survey_test_candidates(getattr(self, "orchestrator", None)))
            return resolved[0]

        return survey

    def _missing_required_test_attempt(
        self,
        survey: Callable[[], TestCandidateResolution] | None = None,
    ) -> TestAttemptRequirement | None:
        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.is_complete:
            return None
        resolution = (survey or self._test_candidate_survey())()
        self._last_test_candidate_resolution = resolution
        return required_test_attempt(
            getattr(self, "run_evidence_state", None),
            getattr(self, "orchestrator", None),
            phase=machine.current_phase,
            attempt_id=machine.current_attempt_id,
            resolution=resolution,
            validator=getattr(self, "physical_validator", None),
        )

    def _unresolved_test_coordinates_after_refresh(
        self,
        survey: Callable[[], TestCandidateResolution] | None = None,
    ) -> TestCandidateResolution | None:
        machine = getattr(self, "phase_machine", None)
        state = getattr(self, "run_evidence_state", None)
        if (
            machine is None
            or state is None
            or machine.current_phase != "test"
            or not has_test_candidate_refresh_receipt(
                state,
                attempt_id=machine.current_attempt_id,
            )
        ):
            return None
        resolution = (survey or self._test_candidate_survey())()
        if resolution.status == "available":
            return None
        # Spec §1 item 2: `test_candidate_resolution_unavailable` may only be
        # emitted once the run-wide receipt set is empty of test-bearing
        # receipts. Both close sites read this predicate, so they inherit the
        # consult from here rather than each growing their own.
        if run_test_receipts(state, project_root=resolution.project_root):
            return None
        return resolution

    def _forced_test_refusals(
        self,
        survey: Callable[[], TestCandidateResolution] | None = None,
    ):
        machine = getattr(self, "phase_machine", None)
        state = getattr(self, "run_evidence_state", None)
        if machine is None or state is None or machine.current_phase != "test":
            return ()
        resolution = (survey or self._test_candidate_survey())()
        if resolution.status != "available":
            return ()
        return forced_test_refusal_receipts(
            state,
            attempt_id=machine.current_attempt_id,
            candidates=resolution.candidates,
        )

    def _cap_unresolved_test_gate(
        self,
        claim: PhaseClaim,
        gate: GateResult,
        *,
        survey: Callable[[], TestCandidateResolution] | None = None,
    ) -> GateResult:
        """An unresolved coordinate may close honestly, but can never be green."""
        survey = survey or self._test_candidate_survey()
        resolution = self._unresolved_test_coordinates_after_refresh(survey)
        refusals = self._forced_test_refusals(survey)
        if resolution is None and not refusals:
            return gate
        capped_state = (
            ValidatorState.RED
            if refusals and gate.validator_state is ValidatorState.RED
            else ValidatorState.UNAVAILABLE
        )
        reason = (
            "test coordinates remained unavailable after the one bounded "
            f"survey refresh ({resolution.status})"
            if resolution is not None
            else "the harness-owned test action produced no candidate-bound runner receipt"
        )
        return validate_phase_claim(
            claim,
            capped_state,
            reason=reason,
            evidence_refs=gate.evidence_refs,
            suggestions=("close as unknown/partial/failed with the survey refresh evidence",),
            code=(
                "test_candidate_resolution_unavailable"
                if resolution is not None
                else "forced_test_attempt_nonreceipt"
            ),
            validated_facts=gate.validated_facts,
        )

    @staticmethod
    def _mark_forced_test_refusal(
        result: ToolResult,
        requirement: TestAttemptRequirement,
        *,
        attempt_id: str,
        tool_name: str,
        params: Mapping[str, Any],
        disposition: str,
        add_conflict: bool,
    ) -> ToolResult:
        metadata = dict(result.metadata or {})
        if not result.is_terminal:
            return result
        reason_code = str(
            result.error_code
            or (
                "FORCED_TEST_CANDIDATE_MISMATCH"
                if disposition == "candidate_mismatch"
                else "FORCED_TEST_NO_RUNNER_DISPATCH"
            )
        )
        actual_root, actual_system = test_execution_binding(
            tool_name,
            params,
            result,
        )
        metadata["harness_forced_test_attempt"] = {
            "phase": "test",
            "source_attempt_id": attempt_id,
            "root": requirement.root,
            "system": requirement.system,
            "actual_root": actual_root,
            "actual_system": actual_system,
            "disposition": disposition,
            "reason_code": reason_code,
        }
        conflicts = list(result.conflicts)
        if add_conflict:
            conflicts.append(
                "forced_test_attempt_nonreceipt:"
                f"{attempt_id}:{requirement.root}:{requirement.system}:"
                f"{disposition}:{reason_code}"
            )
        return result.model_copy(
            update={
                "metadata": metadata,
                "conflicts": list(dict.fromkeys(conflicts)),
            }
        )

    def _mark_forced_test_refusals(
        self,
        execution: ToolExecution,
        requirement: TestAttemptRequirement,
    ) -> None:
        """Stamp deterministic pre-run refusals without promoting them to tests."""
        attempt_id = str(self.phase_machine.current_attempt_id)
        if execution.actual_executions:
            if any(
                test_execution_matches_candidate(
                    actual.tool_name,
                    actual.params,
                    actual.result,
                    requirement,
                )
                and (actual.result.metadata or {}).get("runner_dispatched") is True
                and str((actual.result.metadata or {}).get("command") or "").strip()
                and (
                    actual.result.is_terminal
                    or (actual.result.poll_ref and not actual.result.is_terminal)
                )
                for actual in execution.actual_executions
            ):
                return
            marked_conflict = False
            updated: list[ActualToolExecution] = []
            for actual in execution.actual_executions:
                original = actual.result
                runner_dispatched = (original.metadata or {}).get("runner_dispatched") is True
                command_present = bool(str((original.metadata or {}).get("command") or "").strip())
                marked = self._mark_forced_test_refusal(
                    original,
                    requirement,
                    attempt_id=attempt_id,
                    tool_name=actual.tool_name,
                    params=actual.params,
                    disposition=(
                        "candidate_mismatch"
                        if runner_dispatched and command_present
                        else "no_runner_dispatch"
                    ),
                    add_conflict=not marked_conflict,
                )
                if marked is not original:
                    marked_conflict = True
                updated.append(
                    ActualToolExecution(
                        tool_name=actual.tool_name,
                        params=actual.params,
                        result=marked,
                        execution_id=actual.execution_id,
                    )
                )
                if original is execution.result:
                    execution.result = marked
            execution.actual_executions = updated
            return
        execution.result = self._mark_forced_test_refusal(
            execution.result,
            requirement,
            attempt_id=attempt_id,
            tool_name=execution.call.name,
            params=execution.validated_params or execution.call.raw_params,
            disposition=(
                "candidate_mismatch"
                if (
                    (execution.result.metadata or {}).get("runner_dispatched") is True
                    and str((execution.result.metadata or {}).get("command") or "").strip()
                )
                else "no_runner_dispatch"
            ),
            add_conflict=True,
        )

    _FORCED_ATTEMPT_NATIVE_TEXT = "[harness] executing the mandatory test attempt"

    def _force_required_test_attempt(
        self,
        requirement: TestAttemptRequirement,
        *,
        trigger: str,
    ) -> bool:
        """Execute a must-attempt action in the harness, never through a model plan."""
        if getattr(self, "_forcing_required_test_attempt", False):
            return False
        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.current_phase != "test":
            return False
        turn_started = self._turn_stamp()
        branch_task_id = getattr(getattr(self, "context_manager", None), "current_task_id", None)
        resolution = getattr(self, "_last_test_candidate_resolution", None)
        if not isinstance(resolution, TestCandidateResolution):
            resolution = resolve_survey_test_candidates(getattr(self, "orchestrator", None))

        action = requirement.required_action
        tool, exact_params = self._canonicalize_tool_action(
            str(action["tool"]),
            dict(action["params"]),
        )
        controller_intent = EngineActionIntentFactory.for_controller().from_submission(
            {
                "domain_id": self._action_domain_id(exact_params),
                "tool": tool,
                "params": exact_params,
                "next_action_kind": str(exact_params.get("action") or tool),
            },
            tool_call_id=f"controller:{machine.current_attempt_id}:{trigger}",
            predecessor_contract_id=getattr(self, "_last_invocation_contract_id", None),
        )
        sink = getattr(self, "control_event_sink", None)
        envelope_id = f"forced-{sink.sequence + 1:06d}" if sink is not None else None
        resolution_snapshot = resolution.to_snapshot()
        forced_payload = {
            "envelope_id": envelope_id or "forced-unrecorded",
            "policy": "test_attempt_required",
            "trigger": trigger,
            "phase": "test",
            "source_attempt_id": str(machine.current_attempt_id),
            "reason_code": requirement.reason_code,
            "tool": tool,
            "exact_params": bounded_exact_params(exact_params),
            "candidate_root": requirement.root,
            "candidate_system": requirement.system,
            "parent_execution_id": requirement.parent_execution_id,
            "candidate_resolution": resolution_snapshot,
            "intent_id": controller_intent.intent_id,
            "intent_source": controller_intent.source,
            "action_fingerprint": controller_intent.action_fingerprint,
        }
        forced_payload["action_sha256"] = forced_action_sha256(
            policy="test_attempt_required",
            trigger=trigger,
            phase="test",
            source_attempt_id=str(machine.current_attempt_id),
            reason_code=requirement.reason_code,
            tool=tool,
            exact_params=forced_payload["exact_params"],
            candidate_root=requirement.root,
            candidate_system=requirement.system,
            parent_execution_id=requirement.parent_execution_id,
            candidate_resolution=resolution_snapshot,
            intent_id=controller_intent.intent_id,
            intent_source=controller_intent.source,
            action_fingerprint=controller_intent.action_fingerprint,
        )

        self._forcing_required_test_attempt = True
        self._suppress_control_action_envelope = True
        try:
            if envelope_id is not None:
                self._emit_control_event("forced_action", forced_payload)
            call = ToolCall(
                name=tool,
                raw_params=dict(exact_params),
                raw_action_text=f"HARNESS FORCED: {requirement.action_text()}",
                source_step_index=getattr(self, "current_iteration", 0),
                model_used="harness",
                action_intent=controller_intent,
            )
            # The harness authored this intent, so the contract the facade
            # freezes for it must say `controller` — a forced attempt is never
            # the model's call (Plan 6 Stage B, spec §C3).
            with action_context(
                envelope_id=envelope_id,
                intent_source="controller",
                intent_id=controller_intent.intent_id,
                intent_domain_id=controller_intent.domain_id,
                intent_exact_params=bounded_exact_params(exact_params),
                action_fingerprint=controller_intent.action_fingerprint,
                predecessor_contract_id=controller_intent.predecessor_contract_id,
                control_recording_active=sink is not None,
            ):
                execution = self._execute_tool_call(call)
            if tool == "build":
                self._mark_forced_test_refusals(execution, requirement)
            if tool == "project":
                refreshed = resolve_survey_test_candidates(getattr(self, "orchestrator", None))
                self._last_test_candidate_resolution = refreshed
                if refreshed.status != "available":
                    conflict = (
                        "test_candidate_resolution_unresolved:"
                        f"{machine.current_attempt_id}:{refreshed.status}"
                    )
                    execution.result = execution.result.model_copy(
                        update={
                            "metadata": {
                                **dict(execution.result.metadata or {}),
                                "test_candidate_refresh_status": refreshed.status,
                            },
                            "conflicts": list(
                                dict.fromkeys([*execution.result.conflicts, conflict])
                            ),
                        }
                    )
                    execution.observation_text = format_tool_result(
                        call.name,
                        execution.result,
                    )
            result, execution_id, actual_executions = self._record_execution_bundle(
                execution,
                call,
            )
            # The harness authors this call, so the harness mints its id: the
            # synthetic ACTION step and the observation below must render as a
            # matched tool_use/tool_result pair like any model-issued call.
            self._forced_call_counter = getattr(self, "_forced_call_counter", 0) + 1
            forced_call_id = f"forced-{self._forced_call_counter}"
            action_step = ReActStep(
                step_type=StepType.ACTION,
                content=f"HARNESS FORCED: {requirement.action_text()}",
                tool_name=tool,
                tool_params=dict(exact_params),
                tool_result=result,
                timestamp=self._get_timestamp(),
                model_used="harness",
                tool_call_id=forced_call_id,
                native_text=self._FORCED_ATTEMPT_NATIVE_TEXT,
            )
            self.steps.append(action_step)
            self._emit_control_tool_result(
                envelope_id=envelope_id,
                execution_id=execution_id,
                tool=tool,
                params=dict(execution.validated_params or call.validated_params or exact_params),
                result=result,
                actual_executions=actual_executions,
            )
            self._observe_action_intent_progress(execution)
            self._apply_tool_execution_loop_effects(execution)
            observation_step = self._append_native_observation(
                forced_call_id,
                execution.observation_text,
                source_tool=tool,
            )
            # The harness's evidence goes where every other action's evidence
            # goes. Writing it into the window alone left four projects with a
            # phase history that never mentioned the attempt (spec §2.2 rule 2).
            self._persist_action_to_branch_history(
                branch_task_id,
                tool_name=tool,
                tool_params=dict(exact_params),
                result=result,
                observation_text=execution.observation_text,
            )
            self._seal_turn_record(
                actor="controller",
                t0=turn_started,
                t1=self._turn_stamp(),
                envelope_ref=envelope_id,
                observation_ref=self._delivered_observation_ref(observation_step),
                iteration=getattr(self, "current_iteration", None),
            )
            return True
        finally:
            self._suppress_control_action_envelope = False
            self._forcing_required_test_attempt = False

    def _phase_intro_step(self) -> ReActStep:
        """Open a clean phase window with state, evidence contract, and budget."""
        machine = self.phase_machine
        phase = machine.current_phase
        _, reserved, remaining = self._phase_budget_numbers(phase)
        budget = max(5, remaining - reserved)
        # Framework survey guarantee (analyzer diet, Category 1) runs BEFORE
        # the objective is selected: the objective depends on the detected
        # build system, and with analyze skipped the stale env would pick the
        # Java objective for a Python repo in the SAME intro that later
        # renders Python guidance (review 2026-07-19 — the pyyaml false-block
        # shape reopened). Idempotent and token-free.
        survey_state = ""
        if phase in ("analyze", "build", "test"):
            survey_state = self._ensure_project_facts()
        # Project-aware objective: by build/test time the analyzer has recorded
        # the detected build system on the trunk, so a Python project gets the
        # Python evidence contract instead of the Java one.
        objective = phase_objective(phase, self._detected_build_system())
        lines = [
            f"=== PHASE: {phase.upper()} ===",
            "Run picture so far:",
            *machine.digest_lines(),
            "",
            f"Objective: {objective}",
            f"Budget: flexible — up to ~{budget} iterations available (a small reserve is "
            f"kept for later phases). When finished, call phase(action='done', "
            f"outcome='success|partial|failed|unknown', key_results=..., evidence=[refs]). "
            f"For an external impediment, call phase(action='blocked', "
            f"outcome='failed|partial|unknown', reason=..., evidence=[refs]).",
        ]
        # Surface only survey status and coordinates.  The phase contract names
        # required evidence; neither source chooses a public project action.
        if phase == "analyze":
            survey_projection = {
                "created": (
                    "Engine survey status: created — the framework survey computed and "
                    "persisted the project fact sheet before the model's first analyze-phase action."
                ),
                "present": (
                    "Engine survey status: present — the current project fact sheet is already "
                    "persisted; no analyze call is required to create it."
                ),
                "failed": (
                    "Engine survey status: failed — the harness could not establish the current "
                    "fact sheet; this is a harness evidence failure, not a request to guess a "
                    "replacement project action."
                ),
            }.get(survey_state)
            if survey_projection:
                lines.insert(lines.index(f"Objective: {objective}") + 1, survey_projection)
            fact_guidance = self._analyze_fact_sheet_guidance()
            if fact_guidance:
                lines.extend(["", fact_guidance])
            inventory_guidance = self._document_inventory_guidance()
            if inventory_guidance:
                lines.extend(["", inventory_guidance])
        if phase in ("build", "test"):
            if survey_state == "created":
                lines.append(
                    "(framework survey ran — project facts were computed and "
                    "persisted; the agent had not called project analyze)"
                )
            # dim (b) deleted: the recommendation is coordinates-only; dim (c)
            # and dim (e) deleted: no project_brief projection, no pre-hoc
            # python guidance block. The agent gets the coordinate line and the
            # reactive smoke steer only.
            rec_line = self._recommended_build_line(phase)
            if rec_line:
                lines.insert(lines.index(f"Objective: {objective}") + 1, rec_line)
            # Evidence-reactive steer: the analyzer runs at analyze time and
            # cannot know the build outcome, so this runtime state is injected
            # here on every intro (live TVM 2026-07-18: the smoke steer only
            # rendered on the no-brief fallback, and the live run swept 356
            # collection errors again).
            smoke = self._native_smoke_guidance(phase)
            if smoke:
                lines.insert(lines.index(f"Objective: {objective}") + 1, smoke)
        handoff = getattr(self, "phase_handoff", None)
        projection = None
        if handoff is not None:
            char_budget = int(getattr(self.config, "phase_handoff_char_budget", 6000))
            projection = handoff.project_for(phase, char_budget=char_budget)
        contract = "\n".join(lines)
        builder = getattr(self, "prompt_builder", None)
        render_intro = getattr(builder, "build_phase_intro_guidance", None)
        if callable(render_intro):
            content = render_intro(
                phase_contract=contract,
                handoff_projection=projection,
            )
        else:
            content = (
                f"{contract}\n\n{projection.to_prompt_text()}"
                if projection is not None
                else contract
            )
        return ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=content,
            timestamp=self._get_timestamp(),
        )

    def _analyze_fact_sheet_guidance(self) -> str:
        """Project the small observed coordinate set useful for Analyze."""

        try:
            from sag.tools.internal.build_preflight import read_live_build_requirements

            orchestrator = getattr(self, "orchestrator", None) or getattr(
                getattr(self, "context_manager", None), "orchestrator", None
            )
            observed = read_live_build_requirements(orchestrator)
            if (
                not observed.complete
                or observed.conflict is not None
                or not isinstance(observed.payload, Mapping)
            ):
                return ""
            manifest = observed.payload
            module_structure = manifest.get("module_structure")
            modules = (
                module_structure.get("modules") if isinstance(module_structure, Mapping) else None
            )
            fields = [
                ("build_root", manifest.get("build_root")),
                ("test_root", manifest.get("test_root")),
                ("root_shape", manifest.get("root_shape")),
                ("surveyed_modules", len(modules) if isinstance(modules, Sequence) else None),
            ]
            rendered = " · ".join(
                f"{name}={value}" for name, value in fields if value not in (None, "", [])
            )
            if not rendered:
                return ""
            return (
                "Harness fact-sheet hints (mechanically observed coordinates, not a runner "
                "choice or execution plan): "
                f"{rendered}. Use them to locate and gap-check project docs/CI/config; "
                "project evidence still controls the test runner and strategy."
            )
        except Exception:
            return ""

    def _document_inventory_guidance(self) -> str:
        """Bounded Analyze inventory projection; never a document allowlist."""

        try:
            from .document_map import read_live_document_map
            from .project_execution_plan import render_document_inventory_guidance

            orchestrator = getattr(self, "orchestrator", None) or getattr(
                getattr(self, "context_manager", None), "orchestrator", None
            )
            observed = read_live_document_map(orchestrator)
            if not observed.complete or observed.conflict is not None:
                return (
                    "Document inventory unavailable: the Harness could not project its "
                    "bounded checkout inventory. Continue discovering project-specific "
                    "documents with search; do not infer that an unlisted file is irrelevant."
                )
            return render_document_inventory_guidance(observed.payload, max_chars=12_000)
        except Exception as exc:
            return (
                "Document inventory unavailable: "
                f"{str(exc)[:240]}. Use search to discover project-specific sources; "
                "familiar filenames are not an allowlist."
            )

    def _accepted_analysis_plan_binding(self) -> tuple[str, str, str]:
        """Return the one Analyze claim allowed to feed later phases.

        The newest Analyze record is authoritative even when it closed toward
        Report.  Falling back to an older successful record after an Analyze
        repair/re-entry would silently resurrect the superseded plan.
        """

        machine = getattr(self, "phase_machine", None)
        for record in reversed(tuple(getattr(machine, "records", ()) or ())):
            if getattr(record, "phase", None) != "analyze":
                continue
            claim = getattr(record, "claim", None)
            plan_sha256 = str(getattr(claim, "execution_plan_sha256", "") or "").strip()
            if (
                getattr(record, "transition", None) == "advance"
                and claim is not None
                and getattr(claim, "signal", None) == "done"
                and plan_sha256
            ):
                return record.attempt_id, plan_sha256, claim_identity(claim)
            return "", "", ""
        return "", "", ""

    @staticmethod
    def _execution_plan_matches_binding(
        artifact,
        binding: tuple[str, str, str],
    ) -> bool:
        attempt_id, authored_plan_sha256, claim_sha256 = binding
        return bool(
            artifact is not None
            and attempt_id
            and authored_plan_sha256
            and claim_sha256
            and artifact.source_attempt_id == attempt_id
            and artifact.authored_plan_sha256 == authored_plan_sha256
            and artifact.claim_sha256 == claim_sha256
        )

    def _read_sealed_execution_plan(self):
        binding = self._accepted_analysis_plan_binding()
        cached = getattr(self, "_sealed_execution_plan_cache", None)
        if self._execution_plan_matches_binding(cached, binding):
            return cached
        # A repair/re-entry can leave the previous attempt's plan cached even
        # after the canonical artifact has been replaced. Never return it, but
        # still re-read once so a current replacement can become authoritative.
        self._sealed_execution_plan_cache = None
        if not all(binding):
            return None
        from .project_execution_plan import read_sealed_project_execution_plan

        orchestrator = getattr(self, "orchestrator", None) or getattr(
            getattr(self, "context_manager", None), "orchestrator", None
        )
        artifact = read_sealed_project_execution_plan(orchestrator)
        if self._execution_plan_matches_binding(artifact, binding):
            self._sealed_execution_plan_cache = artifact
            return artifact
        return None

    def _system_prompt_for_current_phase(self, base_system_prompt: str) -> str:
        """Append the sealed model plan to the actual provider system message.

        Phase intro steps are intentionally user-role messages in the native
        renderer.  Rebuilding the effective system prompt per request is what
        guarantees that Build/Test/Report receive the accepted Analyze plan in
        ``messages[0]`` rather than as ordinary conversational prose.
        """

        machine = getattr(self, "phase_machine", None)
        phase = str(getattr(machine, "current_phase", "") or "")
        if phase not in {"build", "test", "report"}:
            return base_system_prompt
        artifact = self._read_sealed_execution_plan()
        if artifact is None:
            return base_system_prompt
        from .project_execution_plan import render_plan_system_prompt

        plan_block = render_plan_system_prompt(artifact)
        authority = (
            "MODEL-AUTHORED IN ANALYZE. The Harness verified bounded structure, "
            "source bindings, and persistence; it did not choose or semantically "
            "approve the project commands. This is revisable strategy, not task acceptance. "
            "If current tool evidence requires a deviation, state the evidence and "
            "reason explicitly."
        )
        return f"{base_system_prompt}\n\n{authority}\n{plan_block}"

    def _detected_build_system(self) -> Optional[str]:
        """The analyzer-detected build system, read best-effort from the trunk's
        environment_summary (the same plumbing as _recommended_build_line).
        Any failure abstains with None -> the ecosystem-neutral objectives."""
        try:
            trunk = self.context_manager.load_trunk_context()
            env = getattr(trunk, "environment_summary", None) or {}
        except Exception:
            return None
        rec = env.get("build_recommendation") or {}
        return rec.get("build_system") or env.get("build_system")

    def _build_recommendation(self) -> Dict:
        """The analyzer's build recommendation off the trunk, best-effort ({} on
        any failure — the same plumbing as _detected_build_system)."""
        try:
            trunk = self.context_manager.load_trunk_context()
            env = getattr(trunk, "environment_summary", None) or {}
        except Exception:
            return {}
        return env.get("build_recommendation") or {}

    def _recommended_build_line(self, phase: str = "build") -> Optional[str]:
        """One-line build/test coordinate fact from the survey.

        The legacy method name remains for callers; the projection contains no
        goal, rationale, selected action, or ordering.
        """
        try:
            trunk = self.context_manager.load_trunk_context()
            rec = (getattr(trunk, "environment_summary", None) or {}).get("build_recommendation")
        except Exception:
            return None
        if not rec:
            return None
        # dim (b) deleted: the intro call-out is coordinate FACTS only —
        # detected system + where, no goal/rationale action wording.
        return self._coordinates_line(rec, phase)

    @staticmethod
    def _coordinates_line(rec, phase: str) -> Optional[str]:
        """The intro call-out without action wording (dim b deleted) —
        coordinate FACTS only (system + where), for build and test alike."""
        # P0-B: "independent" is a graph conclusion, not a directory fact —
        # claim it only when the surveyed coordinate graph has no edges.
        island_label = (
            "coordinate-linked domains" if rec.get("domain_edges") else "independent islands"
        )
        if phase == "test":
            islands = rec.get("test_islands")
            if islands and len(islands) > 1:
                coords = "; ".join(
                    f"{isl.get('system') or 'unknown'} in {isl.get('root')}" for isl in islands
                )
                return f"Test coordinates ({island_label}): {coords}."
            test_root = rec.get("test_root")
            if not test_root or test_root == rec.get("build_root"):
                return None
            return f"Test coordinates: {rec.get('test_system')} at {test_root}."
        islands = rec.get("build_islands")
        if islands and len(islands) > 1:
            coords = "; ".join(
                f"{isl.get('system') or 'unknown'} in {isl.get('root')}" for isl in islands
            )
            return f"Build coordinates ({island_label}): {coords}."
        if rec.get("is_aggregator_only"):
            return "Build coordinates: the survey found no standard compile target at the root."
        return f"Build coordinates: {rec.get('build_system')} at {rec.get('build_root')}."

    def _repair_budgets(self) -> RepairBudgets:
        return RepairBudgets(
            global_remaining=getattr(self, "_repair_global_remaining", 2),
            phase_remaining=dict(getattr(self, "_repair_phase_remaining", {"test": 1, "build": 1})),
        )

    def _consume_repair_budget(self, phase: str) -> None:
        self._repair_global_remaining = max(0, getattr(self, "_repair_global_remaining", 2) - 1)
        phase_remaining = dict(getattr(self, "_repair_phase_remaining", {"test": 1, "build": 1}))
        phase_remaining[phase] = max(0, phase_remaining.get(phase, 0) - 1)
        self._repair_phase_remaining = phase_remaining

    def _record_gate_facts(self, phase: str, gate: GateResult) -> None:
        state = getattr(self, "run_evidence_state", None)
        if state is None or state.sealed:
            return
        provenance = (
            gate.evidence_refs[0]
            if gate.evidence_refs
            else f"validator:{phase}:{getattr(self.phase_machine, 'current_attempt_id', '')}"
        )
        for key, value in gate.validated_facts.items():
            if str(key).startswith("run."):
                # #28 (both round-four reviewers): a `run.`-prefixed control
                # fact is the gate's working memory — the cap's inputs, carried
                # on the gate result and the control event for replay. As a run
                # fact it would land in StateScope.PROJECT_ANALYSIS, the epoch
                # vector the build→analyze repair recurrence guard reads, so a
                # DIAGNOSTIC write counted as material progress; and the phase
                # handoff printed it into the model's prompt. No consumer reads
                # these keys back from run state.
                continue
            state.set_fact(
                key,
                value,
                evidence_ref=provenance,
                source_phase=phase,
                source_attempt_id=self.phase_machine.current_attempt_id,
            )
        if gate.evidence_refs:
            state.record_phase_evidence(
                self.phase_machine.current_attempt_id,
                gate.evidence_refs,
            )

    @staticmethod
    def _phase_record_status(record) -> str:
        if (
            record.termination in {PhaseTermination.BLOCKED, PhaseTermination.SKIPPED}
            or record.outcome is PhaseOutcome.FAILED
        ):
            return "failed"
        return "completed"

    def _apply_phase_decision(
        self,
        record,
        decision: TransitionDecision,
        *,
        repair_request: RepairRequest | None = None,
    ) -> None:
        machine = self.phase_machine
        self._pending_repair_context = None
        # Claimed before `machine.apply` moves the attempt on: an observation
        # queued under a closed attempt is a statement about a phase the model
        # is no longer in.
        pending_observation = self._pending_window_observation()
        self._emit_control_phase_transition(decision, repair_request=repair_request)
        appended = machine.apply(decision)
        for applied in appended:
            self._record_phase_audit(applied)
            text = applied.key_results or applied.reason or applied.outcome.value
            self._persist_phase_record(
                applied.phase,
                self._phase_record_status(applied),
                f"[{applied.outcome.value}] {text}",
            )

        if decision.route.kind == "evidence_close":
            reason = (
                EvidenceCloseReason.DEPENDENTS_SKIPPED
                if decision.skips or record.phase != "test"
                else EvidenceCloseReason.TEST_TERMINATED
            )
            self._finalize_evidence(reason)

        self._phase_iterations = 0
        self.steps_since_context_switch = 0
        self._reset_advisor_phase_state()
        if not machine.is_complete:
            self._archive_window_steps()
            self.steps = [self._phase_intro_step()]
            self._journal_intro_dirty = True
            self._journal_last_ledger = None
            self._start_phase_branch()
            # Guarantee 1 (spec §3.2): the advice lands in the fresh window
            # BEFORE the model plans this phase — including on a repair
            # re-entry.
            self._maybe_consult_advisor_at_phase_entry()
            if pending_observation is not None:
                self._add_system_guidance(pending_observation[0], priority=pending_observation[1])

    def _project_name_for_gate(self) -> str | None:
        try:
            trunk = self.context_manager.load_trunk_context()
            return getattr(trunk, "project_name", None)
        except Exception:
            return getattr(self.context_manager, "project_name", None)

    def _finalize_analyze_execution_plan(
        self,
        claim: PhaseClaim,
        delivered: GateResult,
        metadata: Mapping[str, Any],
    ) -> GateResult:
        """Seal an accepted Analyze plan before any phase state can move.

        PhaseTool validates a bounded candidate but does not publish it.  The
        engine owns the transition boundary, so this is the only place that
        may turn that candidate into the authoritative plan read by later
        system prompts.  A persistence/currentness failure replaces the
        delivered acceptance with a harness-owned rejection; the caller's
        existing supersede path states that revision and keeps Analyze open.
        """

        if claim.phase != "analyze" or not claim.execution_plan_sha256:
            return delivered
        candidate = metadata.get("execution_plan_candidate")
        try:
            if not isinstance(candidate, Mapping):
                raise ValueError("accepted Analyze claim lost its execution plan candidate")
            authored = candidate.get("authored_plan")
            if not isinstance(authored, Mapping):
                raise ValueError("execution plan candidate lacks the authored plan")
            source_attempt_id = str(
                getattr(self.phase_machine, "current_attempt_id", "") or ""
            ).strip()
            if not source_attempt_id or self.phase_machine.current_phase != "analyze":
                raise ValueError("execution plan is not bound to an open Analyze attempt")
            phase_tool = getattr(self, "tools", {}).get("phase")
            validate_plan_evidence = getattr(
                phase_tool,
                "validate_execution_plan_evidence",
                None,
            )
            if not callable(validate_plan_evidence):
                raise ValueError("execution plan evidence validator is unavailable")
            authored = validate_plan_evidence(
                authored,
                source_attempt_id=source_attempt_id,
            )

            from .document_map import read_live_document_map
            from .project_execution_plan import (
                PROJECT_EXECUTION_PLAN_PATH,
                read_sealed_project_execution_plan,
                seal_and_write_project_execution_plan,
            )

            orchestrator = getattr(self, "orchestrator", None) or getattr(
                getattr(self, "context_manager", None), "orchestrator", None
            )
            observed_map = read_live_document_map(orchestrator)
            # The inventory is a gap-checking input, never semantic authority.
            # If its publication is unavailable, the plan can still be sealed
            # from direct output/file evidence and the artifact records that
            # inventory gap explicitly. A model claim that relies on map-only
            # entry/hash bindings will still fail currentness validation when
            # the map itself cannot be verified.
            document_map = (
                observed_map.payload
                if observed_map.complete and observed_map.conflict is None
                else None
            )
            artifact = seal_and_write_project_execution_plan(
                authored,
                orchestrator,
                source_attempt_id=source_attempt_id,
                claim_sha256=claim_identity(claim),
                document_map=document_map,
            )
            reread = read_sealed_project_execution_plan(orchestrator)
            if reread is None or reread.artifact_sha256 != artifact.artifact_sha256:
                raise ValueError("sealed execution plan did not round-trip")
            if artifact.authored_plan_sha256 != claim.execution_plan_sha256:
                raise ValueError("sealed execution plan differs from the model claim")
            if artifact.source_attempt_id != source_attempt_id:
                raise ValueError("sealed execution plan belongs to another Analyze attempt")
            if claim.execution_plan_ref != PROJECT_EXECUTION_PLAN_PATH:
                raise ValueError("execution plan claim names a noncanonical artifact path")
            self._sealed_execution_plan_cache = reread
        except Exception as exc:
            reason = f"Analyze execution plan could not be sealed: {exc}"
            return GateResult(
                accepted=False,
                validated_outcome=PhaseOutcome.UNKNOWN,
                claim_disposition=ClaimDisposition.CONTRADICTED,
                validator_state=ValidatorState.UNAVAILABLE,
                control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
                blocker_owner="harness",
                reason=reason,
                evidence_refs=tuple(delivered.evidence_refs),
                suggestions=(),
                code="analysis_execution_plan_seal_failed",
                validated_facts={
                    **dict(delivered.validated_facts),
                    "analysis.execution_plan_sealed": False,
                    "analysis.execution_plan_error": str(exc)[:1000],
                },
                claim=claim,
            )

        return replace(
            delivered,
            evidence_refs=tuple(
                dict.fromkeys((*delivered.evidence_refs, PROJECT_EXECUTION_PLAN_PATH))
            ),
            validated_facts={
                **dict(delivered.validated_facts),
                # The read-only Analyze gate cannot see the artifact before
                # this transition boundary publishes it. Once the accepted
                # survey state and the plan seal coexist, Build entry becomes
                # ready in the one final gate recorded below.
                "analysis.build_entry_ready": delivered.validator_state
                in {ValidatorState.GREEN, ValidatorState.PARTIAL},
                "analysis.execution_plan_candidate_valid": True,
                "analysis.execution_plan_sealed": True,
                "analysis.execution_plan_ref": PROJECT_EXECUTION_PLAN_PATH,
                "analysis.execution_plan_sha256": artifact.authored_plan_sha256,
                "analysis.execution_plan_source_attempt_id": artifact.source_attempt_id,
                "analysis.execution_plan_artifact_sha256": artifact.artifact_sha256,
                "analysis.execution_plan_inventory_coverage": (
                    artifact.inventory_coverage.model_dump(mode="json")
                ),
                "analysis.execution_plan_inventory_warnings": list(artifact.inventory_warnings),
            },
        )

    def _handle_phase_signals(self, executed_steps) -> Optional[str]:
        """Validate terminal claims, then route them through exactly one policy call."""
        if getattr(self, "phase_machine", None) is None:
            return None
        for step in executed_steps:
            result = getattr(step, "tool_result", None)
            metadata = getattr(result, "metadata", None) or {}
            signal = metadata.get("phase_signal")
            if not signal:
                continue
            machine = self.phase_machine
            if signal == "note":
                self._persist_phase_note(machine.current_phase, metadata.get("text", ""))
                return signal
            if signal not in {"done", "blocked"}:
                self.agent_logger.warning(f"Ignoring unknown phase signal: {signal}")
                return None
            if metadata.get("rejected_completion_control_owned") is True:
                # _prepare/_apply_rejected_completion_control owns the durable
                # gate -> optional repair_context_opened sequence for this
                # rejected terminal result. Re-emitting it here creates a bare
                # second gate that neither live restart nor replay can pair.
                return None

            claim_data = metadata.get("phase_claim")
            gate_data = metadata.get("gate_result")
            if not isinstance(claim_data, dict) or not isinstance(gate_data, dict):
                self.agent_logger.warning("Ignoring unvalidated legacy terminal phase signal")
                return None
            try:
                claim = PhaseClaim.from_metadata(claim_data)
                gate = GateResult.from_metadata(gate_data, claim=claim)
            except (TypeError, ValueError, PermissionError) as exc:
                self.agent_logger.warning(f"Ignoring malformed phase validation metadata: {exc}")
                return None
            if claim.phase != machine.current_phase or claim.signal != signal:
                self.agent_logger.warning("Ignoring a stale or mismatched phase claim")
                return None
            # The word in `gate` is the word the model already read and the one
            # the tool result already embedded. Everything below either seals
            # exactly it, or says out loud that it is replacing it (spec §3.1).
            delivered = gate
            self._register_delivered_gate(claim, delivered)
            gate = self._finalize_analyze_execution_plan(claim, delivered, metadata)
            # One close, one survey read: the cap and the requirement below both
            # ask the same question and must get the same answer.
            survey = self._test_candidate_survey()
            gate = self._cap_unresolved_test_gate(claim, gate, survey=survey)
            word_revised = False
            if gate is not delivered:
                # A second grading is legal; it just has to name the first.
                gate = gate.superseding(delivered)
                word_revised = (gate.accepted, PhaseOutcome(gate.validated_outcome)) != (
                    delivered.accepted,
                    PhaseOutcome(delivered.validated_outcome),
                )
            if not gate.accepted:
                revision = (
                    self._emit_gate_outcome_revised(claim, delivered, gate)
                    if word_revised
                    else None
                )
                # The revision names both words; a second rendering of the same
                # seal would only repeat it. This branch routes no phase
                # decision, so whichever word it states is stated HERE and
                # carried nowhere.
                self._seal_engine_gate(
                    claim,
                    gate,
                    deliver=revision is None,
                    carry=False,
                    survey=survey,
                )
                if revision is not None:
                    self._state_gate_observation_now(revision, priority=9)
                self.agent_logger.warning("Ignoring a rejected gate result carrying a phase signal")
                return None
            required_attempt = self._missing_required_test_attempt(survey)
            if required_attempt is not None:
                self._force_required_test_attempt(
                    required_attempt,
                    trigger="terminal_metadata",
                )
                self._add_system_guidance(
                    "TEST_ATTEMPT_REQUIRED: terminal phase metadata cannot bypass "
                    "the missing runner receipt. The harness executed the required "
                    f"action: {required_attempt.action_text()}",
                    priority=9,
                )
                return None
            revision = (
                self._emit_gate_outcome_revised(claim, delivered, gate) if word_revised else None
            )
            if revision is not None:
                self._deliver_gate_observation(revision, priority=9, decision_id=gate.decision_id)
            self._emit_control_gate(claim, gate, survey=survey)
            self._record_gate_facts(claim.phase, gate)
            record = machine.close_attempt(gate)
            state = getattr(self, "run_evidence_state", None)
            if state is None:
                raise RuntimeError("phase routing requires RunEvidenceState")
            policy = getattr(self, "transition_policy", None) or PhaseTransitionPolicy()
            decision = policy.decide(
                record,
                state=state,
                budgets=self._repair_budgets(),
            )
            self._apply_phase_decision(record, decision)
            return signal
        return None

    def _archive_window_steps(self) -> None:
        """Accumulate step counters before a window reset so the end-of-run
        execution summary reflects the WHOLE run, not just the last phase's
        window (round-5: summaries reported 'total_steps: 7' for 141-iteration
        runs)."""
        counts = getattr(self, "_archived_counts", None)
        if counts is None:
            counts = {
                "total_steps": 0,
                "thoughts": 0,
                "actions": 0,
                "observations": 0,
                "successful_actions": 0,
                "failed_actions": 0,
                # Per-tool breakdown must survive window resets too, else the
                # end-of-run report shows only the last phase's tools.
                "tools_used": {},
                "tool_failures": {},
            }
            self._archived_counts = counts
        for s in self.steps:
            counts["total_steps"] += 1
            if s.step_type == StepType.THOUGHT:
                counts["thoughts"] += 1
            elif s.step_type == StepType.ACTION:
                counts["actions"] += 1
                tool_name = getattr(s, "tool_name", None)
                if tool_name:
                    counts["tools_used"][tool_name] = counts["tools_used"].get(tool_name, 0) + 1
                result = getattr(s, "tool_result", None)
                if result is not None:
                    if result.succeeded:
                        counts["successful_actions"] += 1
                    elif result.operation_outcome is OperationOutcome.FAILED:
                        counts["failed_actions"] += 1
                        if tool_name:
                            counts["tool_failures"][tool_name] = (
                                counts["tool_failures"].get(tool_name, 0) + 1
                            )
            elif s.step_type == StepType.OBSERVATION:
                counts["observations"] += 1

    def _record_context_journal(
        self, ledger: Optional[str], n_compacted: int, added: int, total_chars: int
    ) -> None:
        """One in-container journal line for this iteration (spec §7).

        Window texts are deduplicated: the intro only on the first record
        after a window reset, the ledger only when its text CHANGED since the
        last journaled one. compact_steps returns the FULL cumulative ledger
        on every post-compaction iteration, so gating on "a ledger exists"
        re-records ~6KB per line and stamps every `sag inspect` timeline row
        with [LEDGER] (round-6 review). The segment SIZES still describe the
        whole window on every record."""
        if self.context_journal is None:
            return
        intro_len = len(self.steps[0].content) if self.steps else 0
        intro_text = None
        if self._journal_intro_dirty and self.steps:
            intro_text = self.steps[0].content
            self._journal_intro_dirty = False
        ledger_text = None
        if ledger is not None and ledger != self._journal_last_ledger:
            ledger_text = ledger
            self._journal_last_ledger = ledger
        self.context_journal.record(
            phase=self.phase_machine.current_phase,
            iteration=self.current_iteration,
            segments={
                "intro": intro_len,
                "ledger": len(ledger or ""),
                "steps": len(self.steps),
            },
            delta={"added": added, "compacted": n_compacted},
            total_chars=total_chars,
            intro_text=intro_text,
            ledger_text=ledger_text,
            step_span=len(self.steps),
        )

    def _evidence_is_sealed(self) -> bool:
        """Whether this run has closed its evidence and accepts no more of it.

        The one reader of the seal for every settler downstream of it: the batch
        sweep, and the gate — which settles the job ledger before it grades. A
        sealed run must not settle (spec §3.2: the sealed verdict has already
        recorded the job as `job_unsettled`, and evidence-close is immutable)."""
        return bool(getattr(getattr(self, "run_evidence_state", None), "sealed", False))

    def _phase_gate_check(self, phase: str) -> Dict[str, Any]:
        """Run the phase-boundary evidence gate from engine context.

        Fails CLOSED (ok=False) when no validator is wired: the callers
        (floor auto-done, mid-phase nudge) must only act on positive
        evidence, never on inability to check."""
        validator = getattr(self, "physical_validator", None)
        if validator is None:
            return {
                "ok": False,
                "reason": "no validator available",
                "suggestions": [],
                "validator_state": ValidatorState.UNAVAILABLE.value,
                "evidence_refs": [],
                "validated_facts": {},
                "code": "validator_unavailable",
            }
        from .phase_gates import check_phase_done

        project_name = None
        try:
            trunk = self.context_manager.load_trunk_context()
            project_name = getattr(trunk, "project_name", None)
        except Exception:
            pass
        return check_phase_done(
            phase,
            validator=validator,
            orchestrator=getattr(validator, "docker_orchestrator", None),
            project_name=project_name,
            sealed=self._evidence_is_sealed(),
            disclosed_job_ids=sorted(self._disclosed_live_jobs()),
            expected_analysis_attempt_id=(
                str(getattr(self.phase_machine, "current_attempt_id", "") or "")
                if phase == "analyze"
                else None
            ),
        )

    NUDGE_EVERY = 15

    def _maybe_nudge_phase_done(self) -> bool:
        """Mid-phase evidence nudge (round-5 vfs lesson): a model deep in a
        rabbit hole may hold green evidence for dozens of iterations without
        claiming done. Every NUDGE_EVERY phase-iterations, check the gate;
        when it would pass, report the machine-derived state.

        Spec §3.3: the message states the gate result, the validator's own
        reason, the refs it rests on, and the one concrete thing this attempt
        still lacks. It deliberately does NOT spell out the closure call —
        handing the model the exact `phase(...)` string teaches it to type the
        words instead of judging the evidence."""
        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.is_complete:
            return False
        if self._phase_iterations <= 0 or self._phase_iterations % self.NUDGE_EVERY != 0:
            return False
        gate = self._phase_gate_check(machine.current_phase)
        if not gate.get("ok"):
            return False
        refs = [str(ref) for ref in (gate.get("evidence_refs") or [])][:3]
        reason = str(gate.get("reason") or "").strip()
        lines = [
            f"EVIDENCE CHECK — phase '{machine.current_phase}': the completion gate "
            f"passes on physical evidence "
            f"(validator state {gate.get('validator_state') or 'green'}).",
        ]
        if reason:
            lines.append(f"Validator reason: {reason}")
        lines.append(
            f"Evidence it rests on: {', '.join(refs)}"
            if refs
            else "Evidence it rests on: no refs were recorded by the probe."
        )
        lines.append(
            f"Missing for attempt {machine.current_attempt_id}: no outcome has been "
            f"recorded for it, so the phase cannot route."
        )
        self.steps.append(
            ReActStep(
                step_type=StepType.SYSTEM_GUIDANCE,
                content="\n".join(lines),
                timestamp=self._get_timestamp(),
            )
        )
        return True

    def _close_phase_at_barrier_deadline(self) -> bool:
        """Close the open attempt BLOCKED when a job is still live at the reserve.

        The controller waited as long as its own budget allowed, verified the
        job live at `_hold_deadline()`, and disclosed it. What remains is a
        phase that cannot be finished, which is an ordinary blocked close —
        the containment #45 built for a control-persist exhaustion, reused:
        the phase ends, dependents skip, evidence closes and the report
        delivers. Nothing here weakens a claim: the disclosed job is still an
        open obligation, so the §3.3 cap still denies `success`, and the
        verdict still carries `job_live_at_close:<id>` as a conflict.

        Returns False when there is nothing honest to say — no disclosure, a
        sealed run, or a machine that is already complete — and the caller's
        abort stands.
        """
        machine = getattr(self, "phase_machine", None)
        state = getattr(self, "run_evidence_state", None)
        if machine is None or state is None or state.sealed or machine.is_complete:
            return False
        disclosed = sorted(self._disclosed_live_jobs())
        if not disclosed:
            return False
        phase = machine.current_phase
        named = ", ".join(disclosed[:3])
        if len(disclosed) > 3:
            named += f" (+{len(disclosed) - 3} more)"
        probe = self._phase_gate_check(phase)
        validator_state = ValidatorState(
            probe.get("validator_state", ValidatorState.UNAVAILABLE.value)
        )
        validated_facts = dict(probe.get("validated_facts") or {})
        refs = tuple(probe.get("evidence_refs") or ())
        reserve = self._report_reserve_seconds()
        sentence = (
            f"job {named} was still live at the report reserve; the controller "
            f"stopped waiting with {reserve}s reserved for the report and no "
            "terminal runner receipt"
        )
        state.record_blocker(
            failure_signature=f"job_live_at_report_reserve:{machine.current_attempt_id}:{named}",
            category="harness_control",
            error_code="job_live_at_report_reserve",
            evidence_refs=refs,
            source_phase=phase,
            source_attempt_id=machine.current_attempt_id,
        )
        claim = PhaseClaim(
            phase=phase,
            signal="blocked",
            claimed_outcome=claimable_outcome(validator_state, validated_facts),
            key_results=sentence,
            reason=sentence,
            evidence_refs=refs,
        )
        gate = validate_phase_claim(
            claim,
            validator_state,
            reason=" · ".join(
                part for part in (str(probe.get("reason") or "").strip(), sentence) if part
            ),
            evidence_refs=refs,
            suggestions=tuple(probe.get("suggestions") or ()),
            code="job_live_at_report_reserve",
            validated_facts=validated_facts,
            control_disposition=GateControlDisposition.TERMINAL_CLAIMABLE,
            blocker_owner=BlockerOwner.HARNESS,
        )
        if not gate.accepted:
            # UNKNOWN is always claimable; the close must never fail closed
            # into the abort it exists to replace.
            claim = PhaseClaim(
                phase=phase,
                signal="blocked",
                claimed_outcome=PhaseOutcome.UNKNOWN,
                key_results=sentence,
                reason=sentence,
                evidence_refs=refs,
            )
            gate = validate_phase_claim(
                claim,
                validator_state,
                reason=sentence,
                evidence_refs=refs,
                code="job_live_at_report_reserve",
                validated_facts=validated_facts,
                control_disposition=GateControlDisposition.TERMINAL_CLAIMABLE,
                blocker_owner=BlockerOwner.HARNESS,
            )
            if not gate.accepted:
                return False
        self._seal_engine_gate(claim, gate)
        self._record_gate_facts(phase, gate)
        record = machine.close_attempt(gate)
        policy = getattr(self, "transition_policy", None) or PhaseTransitionPolicy()
        decision = policy.decide(record, state=state, budgets=self._repair_budgets())
        self._apply_phase_decision(record, decision)
        getattr(self, "agent_logger", logger).warning(
            f"Closed {phase} blocked at the report reserve: {sentence}"
        )
        return True

    def _enforce_phase_floors(self) -> bool:
        """Close a starved attempt honestly, then let transition policy route it."""
        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.is_complete:
            return False
        phase = machine.current_phase
        _, reserved, remaining = self._phase_budget_numbers(phase)

        # One close, one survey read — the requirement, the unresolved-coordinate
        # cap and the refusals below all answer the same question.
        survey = self._test_candidate_survey()
        required_attempt = self._missing_required_test_attempt(survey)
        # Install the deterministic dispatch/poll while two turns remain
        # outside downstream floors: one to dispatch and one to poll.  Waiting
        # until remaining == reserved would consume the report guarantee.
        if required_attempt is not None and remaining <= reserved + 2:
            self._force_required_test_attempt(
                required_attempt,
                trigger="phase_floor",
            )
            guidance_key = (
                str(machine.current_attempt_id),
                required_attempt.action_text(),
            )
            if getattr(self, "_test_attempt_floor_guidance_key", None) != guidance_key:
                self._test_attempt_floor_guidance_key = guidance_key
                self.steps.append(
                    ReActStep(
                        step_type=StepType.SYSTEM_GUIDANCE,
                        content=(
                            "TEST_ATTEMPT_REQUIRED: the test phase floor cannot close "
                            "without a terminal runner receipt. The harness executed "
                            f"the required action: {required_attempt.action_text()}"
                        ),
                        timestamp=self._get_timestamp(),
                    )
                )
            return False
        if remaining > reserved:
            return False

        # Analyze owns the project strategy.  A generic budget floor may close
        # an evidence phase, but it may not manufacture a model-authored plan
        # or mark Analyze complete without one.  Reclaim the downstream reserve
        # and keep asking for the bounded plan; if the model never supplies it,
        # the outer iteration guard records Analyze as aborted instead of
        # entering Build or pretending analysis completed.
        if self._keep_analyze_open_for_execution_plan("phase_floor"):
            return False

        probe = self._phase_gate_check(phase)
        unresolved = self._unresolved_test_coordinates_after_refresh(survey)
        refusals = self._forced_test_refusals(survey)
        validator_state = (
            (
                ValidatorState.RED
                if refusals
                and ValidatorState(probe.get("validator_state", ValidatorState.UNAVAILABLE.value))
                is ValidatorState.RED
                else ValidatorState.UNAVAILABLE
            )
            if unresolved is not None or refusals
            else ValidatorState(probe.get("validator_state", ValidatorState.UNAVAILABLE.value))
        )
        validated_facts = dict(probe.get("validated_facts") or {})
        # The floor is the safety net for a starved attempt: it derives its own
        # claim from the evidence and then closes UNCONDITIONALLY, because it has
        # no second move. Every other `close_attempt` caller can decline on a
        # rejected gate; this one cannot, so the claim it derives must be the
        # outcome the gate validates — including the §3.3 cap, which is a pure
        # function of (validator state, open obligations) and therefore knowable
        # here. Deriving SUCCESS from a GREEN probe that carries an open
        # obligation made the gate CONTRADICT the harness's own claim, and
        # `close_attempt` then raised into the loop's `except Exception`, which
        # ABORTED a merely starved run with its report phase skipped.
        claimed_outcome = claimable_outcome(validator_state, validated_facts)
        claim = PhaseClaim(
            phase=phase,
            claimed_outcome=claimed_outcome,
            key_results=(
                f"attempt closed at floor exhaustion; {remaining} iterations remain and "
                f"{reserved} are reserved for downstream work"
            ),
            evidence_refs=tuple(probe.get("evidence_refs") or ()),
        )
        gate = validate_phase_claim(
            claim,
            validator_state,
            reason=(
                "test coordinates remained unavailable after the one bounded "
                f"survey refresh ({unresolved.status})"
                if unresolved is not None
                else (
                    "the harness-owned test action produced no candidate-bound runner receipt"
                    if refusals
                    else str(probe.get("reason") or "phase budget exhausted")
                )
            ),
            evidence_refs=tuple(probe.get("evidence_refs") or ()),
            suggestions=tuple(probe.get("suggestions") or ()),
            code=(
                "test_candidate_resolution_unavailable"
                if unresolved is not None
                else (
                    "forced_test_attempt_nonreceipt"
                    if refusals
                    else str(probe.get("code") or "phase_floor_exhausted")
                )
            ),
            validated_facts=validated_facts,
        )
        self._seal_engine_gate(claim, gate, survey=survey)
        self._record_gate_facts(phase, gate)
        record = machine.close_attempt(gate)
        state = getattr(self, "run_evidence_state", None)
        if state is None:
            raise RuntimeError("phase routing requires RunEvidenceState")
        policy = getattr(self, "transition_policy", None) or PhaseTransitionPolicy()
        decision = policy.decide(record, state=state, budgets=self._repair_budgets())
        self._apply_phase_decision(record, decision)
        return True

    def _keep_analyze_open_for_execution_plan(self, trigger: str) -> bool:
        """Return True when an engine-owned close must not bypass the plan.

        Historical replay remains permissive; this seam is called only by the
        live engine's automatic close paths.  The live model-facing PhaseTool
        separately rejects an accepted Analyze terminal claim without a plan.
        """

        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.is_complete or machine.current_phase != "analyze":
            return False
        state = getattr(self, "run_evidence_state", None)
        current_attempt_id = str(machine.current_attempt_id or "")
        sealed = bool(
            state is not None
            and state.fact_value("analysis.execution_plan_sealed") is True
            and str(state.fact_value("analysis.execution_plan_sha256") or "").strip()
            and str(state.fact_value("analysis.execution_plan_source_attempt_id") or "").strip()
            == current_attempt_id
        )
        if sealed:
            return False
        key = (current_attempt_id, str(trigger or ""))
        if getattr(self, "_analysis_plan_guidance_key", None) != key:
            self._analysis_plan_guidance_key = key
            self._add_system_guidance(
                "ANALYSIS_EXECUTION_PLAN_REQUIRED: Analyze remains open. Inspect the broad "
                "document inventory and submit a model-authored execution_plan on "
                "phase(action='done'). The harness will validate and seal it; no automatic "
                "floor may invent the plan or enter Build without it.",
                priority=9,
            )
        return True

    def _persist_phase_record(self, phase_name: str, status: str, text: str) -> None:
        """Mirror a finished phase into the trunk task `phase_<name>` so phase
        history persists exactly like task history (the webui keeps rendering).
        Best-effort: persistence failure must never kill the run."""
        cm = getattr(self, "context_manager", None)
        if cm is None:
            return
        task_id = f"phase_{phase_name}"
        try:
            target = TaskStatus.COMPLETED if status == "completed" else TaskStatus.FAILED
            updater = getattr(cm, "update_task_status", None)
            if callable(updater):
                # Manager-level setter (test fakes / future CM API).
                if updater(task_id, target, text) is False:
                    logger.warning(
                        f"Phase record '{task_id}' not persisted: context manager "
                        f"has no such task (phase history may be missing from the trunk)"
                    )
            else:
                # Real ContextManager: status/key_results live on the trunk.
                trunk = cm.load_trunk_context()
                if trunk is None:
                    return
                existing_notes = ""
                for task in trunk.todo_list:
                    if task.id == task_id:
                        existing_notes = task.notes
                        break
                status_ok = trunk.update_task_status(task_id, target, existing_notes)
                results_ok = trunk.update_task_key_results(task_id, text)
                if not (status_ok and results_ok):
                    # A missing phase_<name> trunk task means phase history is
                    # being dropped — never let that pass silently (the silent
                    # False return hid the analyzer trunk-rewrite defect).
                    logger.warning(
                        f"Phase record '{task_id}' not found in trunk todo list "
                        f"(status_updated={status_ok}, key_results_updated={results_ok}); "
                        f"phase history may be missing from the trunk"
                    )
                cm._save_trunk_context(trunk)
            if getattr(cm, "current_task_id", None) == task_id:
                cm.current_task_id = None
        except Exception as exc:
            logger.warning(f"Failed to persist phase record '{task_id}' ({status}): {exc}")

    def _persist_phase_note(self, phase_name: str, text: str) -> None:
        """Append a model-authored phase note to the trunk task without
        advancing the phase machine. Notes are durable UI/context material;
        action history still records the exact tool call."""
        note = (text or "").strip()
        if not note:
            return
        cm = getattr(self, "context_manager", None)
        if cm is None:
            return
        task_id = f"phase_{phase_name}"
        try:
            trunk = cm.load_trunk_context()
            if trunk is None:
                return
            for task in trunk.todo_list:
                if task.id != task_id:
                    continue
                task.notes = f"{task.notes.rstrip()}\n{note}".strip() if task.notes else note
                trunk.update_timestamp()
                cm._save_trunk_context(trunk)
                return
            logger.warning(
                f"Phase note for '{task_id}' not persisted: context manager "
                f"has no such task (phase notes may be missing from the trunk)"
            )
        except Exception as exc:
            logger.warning(f"Failed to persist phase note '{task_id}': {exc}")

    def _start_phase_branch(self) -> None:
        """Open the branch context for the new current phase (best-effort) so
        per-phase history persists as phase_<name>.json in the container —
        context files live in-container by design (agent self-introspection)."""
        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.is_complete:
            return
        cm = getattr(self, "context_manager", None)
        starter = getattr(cm, "start_new_branch", None)
        if not callable(starter):
            return
        task_id = f"phase_{machine.current_phase}"
        try:
            starter(task_id)
            return
        except Exception as exc:
            # Strict task ordering rejects starting after a FAILED (blocked)
            # phase; the machine owns phase order, so open the branch directly.
            logger.debug(f"start_new_branch rejected {task_id} ({exc}); opening directly")
        try:
            from .context_manager import BranchContextHistory

            description = phase_objective(machine.current_phase, self._detected_build_system())
            trunk = cm.load_trunk_context()
            if trunk is not None:
                for task in trunk.todo_list:
                    if task.id == task_id:
                        description = task.description or description
                        break
            history = BranchContextHistory(task_id=task_id, task_description=description)
            cm._save_branch_history(history, str(cm.contexts_dir / f"{task_id}.json"))
            if trunk is not None and trunk.update_task_status(task_id, TaskStatus.IN_PROGRESS):
                cm._save_trunk_context(trunk)
            cm.current_task_id = task_id
        except Exception as exc:
            logger.warning(f"Could not start phase branch context for {task_id}: {exc}")

    def run_setup_loop(
        self,
        initial_prompt: str,
        max_iterations: Optional[int] = None,
    ) -> RunTermination:
        """Run setup mode with a typed flow-close result."""
        if self.phase_machine is None:
            raise RuntimeError("run_setup_loop requires a phase machine")
        result = self._run_react_loop(
            initial_prompt,
            max_iterations=max_iterations,
            completion_mode="setup",
        )
        if not isinstance(result, RunTermination):
            raise RuntimeError("setup loop exited without typed termination")
        return result

    def run_react_loop(
        self,
        initial_prompt: str,
        max_iterations: Optional[int] = None,
        completion_mode: str = "setup",
    ) -> bool:
        """Preserve the legacy free-form boolean contract."""
        if completion_mode == "setup" and self.phase_machine is not None:
            raise RuntimeError("setup callers must use run_setup_loop")
        result = self._run_react_loop(
            initial_prompt,
            max_iterations=max_iterations,
            completion_mode=completion_mode,
        )
        if isinstance(result, RunTermination):
            raise RuntimeError("legacy loop exited with setup termination")
        return bool(result)

    def _run_react_loop(
        self,
        initial_prompt: str,
        max_iterations: Optional[int] = None,
        completion_mode: str = "setup",
    ):
        """Compatibility alias: the native executor loop is the only protocol.

        Plan 2 Task 8 deleted the THINK/ACTION dual-role loop, the reasoning
        scheduler, the plan lock, and the text response parser; every caller
        now runs `_run_native_loop`."""
        return self._run_native_loop(
            initial_prompt,
            max_iterations=max_iterations,
            completion_mode=completion_mode,
        )

    @staticmethod
    def _native_message_chars(messages: List[Dict[str, Any]]) -> int:
        """Rough size of one rendered request, the native analogue of the flat
        prompt length the context journal used to record."""
        total = 0
        for message in messages:
            total += len(str(message.get("content") or ""))
            for call in message.get("tool_calls") or ():
                total += len(str(call.get("function", {}).get("arguments") or ""))
        return total

    def _run_native_loop(
        self,
        initial_prompt: str,
        max_iterations: Optional[int] = None,
        completion_mode: str = "setup",
    ):
        """Single-executor native tool-calling loop (spec §3.1).

        One LLM call per iteration; the model sees `[system] + render_messages(
        self.steps)`, answers with prose and/or tool calls, and every call is
        dispatched with its id preserved. `self.steps` stays the single source
        of truth, so phase signals, compaction, the archive, the journal, and
        reporting all keep working unchanged. The skeleton (budget, wall clock,
        floors, token export, exit paths) is the one the deleted dual-role loop
        contributed; only the turn body is new."""
        max_iter = max_iterations or self.max_iterations
        self._run_max_iterations = max_iter

        self.agent_logger.info(f"Starting native executor loop with max {max_iter} iterations")

        phase_mode = completion_mode == "setup" and self.phase_machine is not None

        self.current_iteration = 0
        self._phase_iterations = 0
        self._reset_advisor_run_state()
        self._begin_detached_run_scope()
        if phase_mode:
            self.steps = [self._phase_intro_step()]
            self._journal_intro_dirty = True
            self._journal_last_ledger = None
            self._start_phase_branch()
        else:
            self.steps = []
        phase_entry_advisor_pending = phase_mode

        # The system prompt is rebuilt once and re-sent on EVERY request — the
        # audit finding in spec §3.1 was that the old loop rendered it once and
        # then overwrote it with a flat text rebuild.
        base_system_prompt = self.prompt_builder.build_initial_system_prompt(
            repository_url=self.repository_url,
            repository_ref=self.repository_ref,
            workflow_mode=completion_mode,
        )
        if initial_prompt:
            # The kickoff text lives in the system message ONLY. Repeating it as
            # a user turn made a run-task model read its own instructions twice
            # (Stage B carried the duplication deliberately; Task 8 removes it).
            base_system_prompt = base_system_prompt + "\n\n" + initial_prompt

        run_started_at = time.time()
        wall_clock_cap = getattr(self.config, "max_wall_clock_seconds", 7200)
        # The evidence-close wait (§3.2, _await_open_obligations) needs the
        # same clock this loop enforces; a wait that guessed its own margin
        # could overrun the cap the loop is about to apply.
        self._run_started_at = run_started_at
        self._wall_clock_cap = wall_clock_cap
        # The dispatch hold (stall window) must stop at the same line the
        # evidence-close wait stops at; both consume _hold_deadline (P3).
        self._install_hold_deadline_provider()

        try:
            while self.current_iteration < max_iter:
                if wall_clock_exceeded(run_started_at, wall_clock_cap):
                    elapsed = time.time() - run_started_at
                    logger.warning(
                        f"Native loop stopped: global wall-clock cap of {wall_clock_cap}s "
                        f"reached after {elapsed:.0f}s / {self.current_iteration} iterations"
                    )
                    self._export_token_usage_csv()
                    if phase_mode:
                        return self.abort(reason="wall clock cap exceeded")
                    return False

                barrier_status = self._drain_job_barrier()
                if barrier_status != "cleared":
                    # `live_at_deadline` is a BUDGET outcome, not an integrity
                    # failure: the wall guard verified the job live at the
                    # report reserve and said so. Aborting here threw away the
                    # reserve `_hold_deadline` had just spent the whole wait
                    # defending (p7d camel: 84 minutes defended, then discarded
                    # unspent, the phase recorded `aborted` with a null claim
                    # and the report phase never entered). Every other status
                    # is an integrity family and keeps abort semantics.
                    if phase_mode and barrier_status == "live_at_deadline":
                        if self._close_phase_at_barrier_deadline():
                            if self.phase_machine.is_complete:
                                self._export_token_usage_csv()
                                return self._close_flow(RunTerminationStatus.COMPLETED)
                            continue
                    self._export_token_usage_csv()
                    if phase_mode:
                        return self.abort(reason=f"job barrier {barrier_status}")
                    return False

                if phase_entry_advisor_pending:
                    # A resumed build/test run gets its entry consult only
                    # after every inherited job is terminal and settlement is
                    # resolved. Advisor/model work is forbidden inside barrier.
                    self._maybe_consult_advisor_at_phase_entry()
                    phase_entry_advisor_pending = False

                if phase_mode and self._enforce_phase_floors() and self.phase_machine.is_complete:
                    self._export_token_usage_csv()
                    return self._close_flow(RunTerminationStatus.COMPLETED)

                self.current_iteration += 1
                self._phase_iterations += 1
                self.agent_logger.info(f"Native iteration {self.current_iteration}/{max_iter}")
                self.token_tracker.set_iteration(self.current_iteration)

                system_prompt = (
                    self._system_prompt_for_current_phase(base_system_prompt)
                    if phase_mode
                    else base_system_prompt
                )
                messages = render_messages(system_prompt, self.steps)
                # [A], sealed before the request goes out: every turn this
                # iteration opens refers to THIS array, because this is the
                # array the model answered from.
                self._window_digest = self._seal_window_digest(system_prompt, messages)
                turn_started = self._turn_stamp()
                try:
                    turn = self._native_turn_with_retry(messages)
                except Exception as exc:
                    # `get_native_turn` propagates provider errors instead of
                    # swallowing them the way `get_response` did. Transient
                    # classes were already retried with backoff (spec
                    # 2026-08-13: one 5xx aborted D2 tapestry-5 two turns
                    # after the model had self-corrected); what reaches here
                    # is deterministic or exhausted, and the abort is honest.
                    logger.error(f"Native executor request failed: {exc}")
                    self._export_token_usage_csv()
                    if phase_mode:
                        return self.abort(reason=f"LLM response unavailable: {exc}")
                    return False

                steps_before = len(self.steps)

                if not turn.tool_calls:
                    if turn.text.strip():
                        self.steps.append(
                            ReActStep(
                                step_type=StepType.THOUGHT,
                                content=turn.text,
                                timestamp=self._get_timestamp(),
                                model_used=turn.model_used,
                            )
                        )
                    if completion_mode != "setup":
                        refusal = (
                            self._run_task_completion_refusal(turn.text)
                            if completion_mode == "run_task"
                            else None
                        )
                        if refusal is None:
                            # A free-form task ends only on its explicit,
                            # evidence-consistent terminal sentence. Nothing is
                            # delivered back to a model that is finished, so
                            # the record states an answer of None.
                            self._seal_turn_record(
                                actor="model",
                                t0=turn_started,
                                t1=self._turn_stamp(),
                                iteration=getattr(self, "current_iteration", None),
                            )
                            self._export_token_usage_csv()
                            return True
                        cue_text = refusal
                    else:
                        cue_text = (
                            "No tool was called. Continue with a tool call, "
                            "or close the phase honestly via phase(...)."
                        )
                    cue = ReActStep(
                        step_type=StepType.SYSTEM_GUIDANCE,
                        content=cue_text,
                        timestamp=self._get_timestamp(),
                    )
                    self.steps.append(cue)
                    # A response with no tool call is still a turn: the model
                    # read a rendered window, the run was billed for it, and the
                    # continuation cue is the [C] it read next. Only [B] is
                    # missing, and the record says so with `envelope_ref: None`.
                    # Conservation counts CALLS (spec §2.2 rule 5), so a turn
                    # that made none adds nothing to either side of the fence.
                    self._seal_turn_record(
                        actor="model",
                        t0=turn_started,
                        t1=self._turn_stamp(),
                        envelope_ref=None,
                        observation_ref=self._delivered_observation_ref(cue),
                        iteration=getattr(self, "current_iteration", None),
                    )
                    if phase_mode:
                        self._record_context_journal(
                            None,
                            0,
                            len(self.steps) - steps_before,
                            self._native_message_chars(messages),
                        )
                    continue

                executed_steps = self._execute_native_calls(turn)
                added = max(len(self.steps) - steps_before, 0)

                harness_failure = str(
                    getattr(self, "_fatal_harness_control_failure", "") or ""
                ).strip()
                if harness_failure:
                    self._export_token_usage_csv()
                    if phase_mode:
                        return self.abort(
                            reason=("harness control recovery exhausted: " f"{harness_failure}")
                        )
                    return False

                if phase_mode:
                    self._handle_phase_signals(executed_steps)
                    if self.phase_machine.is_complete:
                        termination = self.phase_machine.termination_state()
                        self.agent_logger.info(
                            f"All phases complete; flow termination: {termination}"
                        )
                        self._export_token_usage_csv()
                        return self._close_flow(RunTerminationStatus.COMPLETED)
                    self._maybe_nudge_phase_done()

                # NO-PHYSICAL-PROGRESS GUARD: unchanged from the old loop.
                completed_task_this_iteration = any(
                    step.tool_name == "manage_context"
                    and (step.tool_params or {}).get("action") == "complete_with_results"
                    and step.tool_result is not None
                    and step.tool_result.succeeded
                    for step in executed_steps
                )
                if completed_task_this_iteration and self._check_progress_after_task():
                    logger.warning(
                        "Native loop stopped: no build progress after repeated completed tasks"
                    )
                    self._export_token_usage_csv()
                    if phase_mode:
                        return self.abort(reason="no physical progress")
                    return False

                ledger, n_compacted = self._compact_window_if_needed(phase_mode)

                if phase_mode:
                    self._record_context_journal(
                        ledger,
                        n_compacted,
                        added,
                        self._native_message_chars(messages),
                    )

                if executed_steps:
                    self.steps_since_context_switch += 1

            logger.warning(f"Native loop completed without success after {max_iter} iterations")
            self._export_token_usage_csv()
            if phase_mode:
                return self.abort(reason="iteration budget exhausted")
            return False

        except KeyboardInterrupt:
            logger.warning("Native loop cancelled by keyboard interrupt")
            self._export_token_usage_csv()
            self.last_run_cancelled = True
            if phase_mode:
                return self.cancel(reason="keyboard interrupt")
            self._terminate_open_jobs(EvidenceCloseReason.CANCELLED)
            return False
        except Exception as e:
            logger.error(f"Native loop failed: {e}", exc_info=True)
            self._export_token_usage_csv()
            if phase_mode:
                return self.abort(reason=f"engine exception: {type(e).__name__}")
            return False

    def _compact_window_if_needed(self, phase_mode: bool) -> tuple[Optional[str], int]:
        """ATTEMPT-LEDGER COMPACTION (phase mode): old steps collapse to one
        line each behind the phase intro; exactly one ledger step exists at a
        time (position 1, right after the intro). Shared by both protocols —
        the ledger step renders as a user message in the native loop."""
        if not phase_mode or len(self.steps) <= 1:
            return None, 0
        tail = self.steps[1:]
        ledger, kept = compact_steps(tail, keep_recent=30)
        if ledger is None:
            return None, 0
        ledger_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=ledger,
            timestamp=self._get_timestamp(),
        )
        kept_clean = [s for s in kept if "ATTEMPT LEDGER" not in (getattr(s, "content", "") or "")]
        n_compacted = len(tail) - len(kept_clean)
        self.steps = [self.steps[0], ledger_step] + kept_clean
        return ledger, n_compacted

    def _record_setup_abort(self, phase_mode: bool, reason: str) -> None:
        if phase_mode and not self.phase_machine.is_complete:
            record = self.phase_machine.record_abort(
                reason,
                evidence=[],
                outcome=PhaseOutcome.FAILED,
            )
            self._record_phase_audit(record)

    def _restore_active_repair_context(self) -> None:
        sink = getattr(self, "control_event_sink", None)
        path = getattr(sink, "path", None)
        if path is None:
            return
        run_id = getattr(getattr(self, "run_evidence_state", None), "run_id", None) or getattr(
            getattr(self, "physical_validator", None), "receipt_run_id", None
        )
        state = recover_active_repair_context_from_path(path, run_id=run_id)
        if state.context is None:
            return
        self._pending_repair_context = state.context
        self._add_system_guidance(
            self._repair_context_guidance(state.context),
            priority=9,
        )

    def _emit_control_event(self, kind: str, payload: Dict[str, Any]):
        lifecycle_states = {
            "job_terminal_observed": "terminal",
            "job_terminal_unpersisted": "terminal_unpersisted",
            "job_live_at_close": "live_at_close",
            "job_settled": "settled",
        }
        lifecycle_state = lifecycle_states.get(kind)
        loop_memory = getattr(self, "loop_memory", None)
        if lifecycle_state and loop_memory is not None:
            job_id = str(payload.get("job_id") or "").strip()
            if job_id:
                loop_memory.observe_job_transition(
                    job_id,
                    lifecycle_state,
                    progress_fingerprint=canonical_sha256(compact_control_value(payload)),
                )
        sink = getattr(self, "control_event_sink", None)
        if sink is None:
            return None
        if kind in _STRICT_LINEAGE_CONTROL_KINDS:
            # These payloads carry hashes over exact bounded values. Passing
            # them through compact_control_value would truncate strings while
            # retaining the digest of the original, making the live record
            # unverifiable. Invalid or unwritable lineage is a hard failure.
            return sink.emit(kind, payload)
        try:
            return sink.emit(kind, compact_control_value(payload))
        except Exception as exc:
            logger.warning(f"Control-event emission failed for {kind}: {exc}")
            return None

    def _action_domain_id(self, params: Mapping[str, Any] | None = None) -> str:
        values = dict(params or {})
        working_directory = str(
            values.get("working_directory")
            or values.get("cwd")
            or values.get("workdir")
            or self.successful_states.get("working_directory")
            or "/workspace"
        ).strip()
        phase = str(getattr(getattr(self, "phase_machine", None), "current_phase", "") or "")
        return f"{phase or 'run'}:{working_directory}"

    def _mint_model_action_intent(
        self,
        call: ToolCall,
        params: Mapping[str, Any],
    ) -> ActionIntent:
        """Assign model provenance to one validated public call.

        When a RepairContext is active the model must supply its own
        hypothesis/observation/stop fields.  The engine links those fields to
        the stored context and refuses invented evidence refs or a tool outside
        the neutral affordance set; it never fills in a project command.
        """

        pending = getattr(self, "_pending_repair_context", None)
        repair_submission = call.repair_intent_submission
        tool_name = str(call.name or "").strip().lower()
        submission: dict[str, Any] = {
            "domain_id": self._action_domain_id(params),
            "tool": tool_name,
            "params": dict(params),
        }
        trigger_assessment_id = None
        repair_context_id = None
        repair_context_digest = None

        if pending is not None:
            allowed_tools = {
                str(item.tool or "").strip().lower() for item in pending.allowed_tool_affordances
            }
            if tool_name in allowed_tools:
                if not isinstance(repair_submission, Mapping):
                    raise PreDispatchControlError(
                        "This action responds to a judge RepairContext and requires the "
                        "model-owned repair_intent fields shown in the tool schema.",
                        error_code="REPAIR_INTENT_REQUIRED",
                        metadata={"repair_context_id": pending.repair_context_id},
                    )
                submission.update(dict(repair_submission))
                try:
                    selected_affordance = validate_repair_action_affordance(
                        tool=tool_name,
                        params=params,
                        next_action_kind=submission.get("next_action_kind"),
                        context=pending,
                    )
                except (TypeError, ValueError) as exc:
                    raise PreDispatchControlError(
                        f"repair_intent does not match the active tool affordance: {exc}",
                        error_code="REPAIR_ACTION_AFFORDANCE_MISMATCH",
                        metadata={"repair_context_id": pending.repair_context_id},
                    ) from exc
                allowed_refs = repair_context_reference_set(
                    pending,
                    affordance=selected_affordance,
                )
                submitted_refs = {
                    str(value or "").strip()
                    for value in submission.get("blocking_fact_refs") or ()
                    if str(value or "").strip()
                }
                if not submitted_refs or not submitted_refs.issubset(allowed_refs):
                    raise PreDispatchControlError(
                        "repair_intent.blocking_fact_refs must cite only the active "
                        "RepairContext assessment or evidence refs.",
                        error_code="REPAIR_INTENT_INVALID_REFS",
                        metadata={"repair_context_id": pending.repair_context_id},
                    )
                expected_observations = {
                    str(value or "").strip()
                    for value in submission.get("expected_observation") or ()
                    if str(value or "").strip()
                }
                if not expected_observations or not expected_observations.issubset(
                    set(pending.admissible_observation_types)
                ):
                    raise PreDispatchControlError(
                        "repair_intent.expected_observation must name only active "
                        "RepairContext observation types.",
                        error_code="REPAIR_INTENT_INVALID_OBSERVATION",
                        metadata={"repair_context_id": pending.repair_context_id},
                    )
                trigger_assessment_id = pending.trigger_assessment_id
                repair_context_id = pending.repair_context_id
                repair_context_digest = repair_context_sha256(pending)
            elif (
                tool_name == "phase"
                and str(params.get("action") or "").strip().lower() in {"done", "blocked"}
                and repair_submission is None
            ):
                # A judge context cannot trap the model in an endless repair
                # loop.  It may still make an explicit terminal claim, which
                # the phase gate independently accepts or contradicts.  This
                # is the sole non-affordance exception; note/advisor/report/
                # context calls do not repair the rejected project claim.
                pass
            else:
                raise PreDispatchControlError(
                    "The selected tool is not an affordance in the active RepairContext.",
                    error_code="REPAIR_TOOL_NOT_ALLOWED",
                    metadata={"repair_context_id": pending.repair_context_id},
                )
        elif repair_submission is not None:
            raise PreDispatchControlError(
                "repair_intent was supplied without an active judge RepairContext.",
                error_code="REPAIR_CONTEXT_NOT_ACTIVE",
            )

        try:
            return EngineActionIntentFactory.for_model().from_submission(
                submission,
                tool_call_id=str(getattr(self, "_active_native_tool_call_id", "") or ""),
                trigger_assessment_id=trigger_assessment_id,
                repair_context_id=repair_context_id,
                repair_context_sha256=repair_context_digest,
                predecessor_contract_id=getattr(self, "_last_invocation_contract_id", None),
            )
        except Exception as exc:
            raise PreDispatchControlError(
                f"ActionIntent validation failed: {exc}",
                error_code="ACTION_INTENT_INVALID",
                metadata={
                    "repair_context_id": repair_context_id,
                    "trigger_assessment_id": trigger_assessment_id,
                },
            ) from exc

    def _validate_action_intent_repair_lineage(
        self,
        call: ToolCall,
        params: Mapping[str, Any],
        intent: ActionIntent,
    ) -> None:
        """Re-enter the active judge context for every dispatch boundary.

        A caller may supply an already-minted ActionIntent (forced/controller
        paths and tests do), so validating only the model-submission factory
        would leave a stale or constructed repair intent as an authority seam.
        """

        pending = getattr(self, "_pending_repair_context", None)
        terminal_claim = str(call.name or "").strip().lower() == "phase" and str(
            params.get("action") or ""
        ).strip().lower() in {"done", "blocked"}
        if pending is None:
            if intent.repair_context_id is not None:
                raise PreDispatchControlError(
                    "ActionIntent links a repair context that is not active.",
                    error_code="REPAIR_CONTEXT_NOT_ACTIVE",
                    metadata={"runner_dispatched": False},
                )
            return
        if terminal_claim and intent.repair_context_id is None:
            if (
                intent.source == "model"
                and bool(str(intent.intent_id or "").strip())
                and bool(str(intent.action_fingerprint or "").strip())
            ):
                return
            raise PreDispatchControlError(
                "Only a complete model ActionIntent may make an unlinked terminal "
                "claim while a RepairContext is active.",
                error_code="REPAIR_INTENT_LINEAGE_MISMATCH",
                metadata={
                    "runner_dispatched": False,
                    "repair_context_id": pending.repair_context_id,
                },
            )
        if intent.source == "controller" and getattr(
            self, "_suppress_control_action_envelope", False
        ):
            # A mandatory controller obligation may run while a judge context
            # is open, but it neither claims nor consumes model repair lineage.
            return
        expected_digest = repair_context_sha256(pending)
        expected_tuple = (
            pending.trigger_assessment_id,
            pending.repair_context_id,
            expected_digest,
        )
        actual_tuple = (
            intent.trigger_assessment_id,
            intent.repair_context_id,
            intent.repair_context_sha256,
        )
        if intent.source != "model" or actual_tuple != expected_tuple:
            raise PreDispatchControlError(
                "ActionIntent does not carry the complete active RepairContext lineage.",
                error_code="REPAIR_INTENT_LINEAGE_MISMATCH",
                metadata={
                    "runner_dispatched": False,
                    "repair_context_id": pending.repair_context_id,
                },
            )
        if pending.domain_id is not None and intent.domain_id != pending.domain_id:
            raise PreDispatchControlError(
                "ActionIntent domain differs from the active RepairContext.",
                error_code="REPAIR_INTENT_DOMAIN_MISMATCH",
                metadata={"runner_dispatched": False},
            )
        try:
            affordance = validate_repair_action_affordance(
                tool=call.name,
                params=params,
                next_action_kind=intent.next_action_kind,
                context=pending,
            )
        except (TypeError, ValueError) as exc:
            raise PreDispatchControlError(
                f"ActionIntent does not match the active tool affordance: {exc}",
                error_code="REPAIR_ACTION_AFFORDANCE_MISMATCH",
                metadata={"runner_dispatched": False},
            ) from exc
        if (
            not set(intent.blocking_fact_refs).issubset(
                repair_context_reference_set(pending, affordance=affordance)
            )
            or not intent.blocking_fact_refs
        ):
            raise PreDispatchControlError(
                "ActionIntent cites refs outside the active RepairContext.",
                error_code="REPAIR_INTENT_INVALID_REFS",
                metadata={"runner_dispatched": False},
            )
        if not intent.expected_observation or not set(intent.expected_observation).issubset(
            set(pending.admissible_observation_types)
        ):
            raise PreDispatchControlError(
                "ActionIntent expects observations outside the active RepairContext.",
                error_code="REPAIR_INTENT_INVALID_OBSERVATION",
                metadata={"runner_dispatched": False},
            )

    def _prepare_control_action(
        self,
        call: ToolCall,
        params: Dict[str, Any],
    ) -> str | None:
        """Mint intent, persist its envelope, and open the contract scope."""

        if call.action_intent is None:
            call.action_intent = self._mint_model_action_intent(call, params)
        else:
            # An existing intent is engine-internal data, but it still crosses
            # the same dispatch boundary as an untrusted public call.  Pydantic
            # does not revalidate model instances by default, so round-trip the
            # payload to catch ``model_construct`` and then bind the executable
            # identity to the normalized call the runner will actually see.
            try:
                intent = ActionIntent.model_validate(
                    call.action_intent.model_dump(mode="python", round_trip=True)
                )
            except Exception as exc:
                raise PreDispatchControlError(
                    f"Existing ActionIntent validation failed: {exc}",
                    error_code="ACTION_INTENT_INVALID",
                    metadata={"runner_dispatched": False},
                ) from exc

            expected_tool = str(call.name or "").strip().lower()
            expected_domain = self._action_domain_id(params)
            try:
                expected_params = canonical_params(params)
            except Exception as exc:
                raise PreDispatchControlError(
                    f"Action parameters cannot be bound to the ActionIntent: {exc}",
                    error_code="ACTION_INTENT_INVALID",
                    metadata={"runner_dispatched": False},
                ) from exc
            mismatches = []
            if intent.tool != expected_tool:
                mismatches.append("tool")
            if intent.domain_id != expected_domain:
                mismatches.append("domain_id")
            if intent.canonical_params != expected_params:
                mismatches.append("canonical_params")
            if mismatches:
                raise PreDispatchControlError(
                    "ActionIntent does not describe the normalized public call.",
                    error_code="ACTION_INTENT_BINDING_MISMATCH",
                    metadata={
                        "runner_dispatched": False,
                        "mismatched_fields": mismatches,
                        "intent_id": intent.intent_id,
                    },
                )
            call.action_intent = intent
        intent = call.action_intent
        self._validate_action_intent_repair_lineage(call, params, intent)
        envelope_id = self._emit_control_action_envelope(
            call.name,
            params,
            intent=intent,
        )
        if (
            envelope_id is None
            and intent.repair_context_id is not None
            and not getattr(self, "_suppress_control_action_envelope", False)
        ):
            clear_action_context()
            raise PreDispatchControlError(
                "A repair-linked action requires a durable exact action envelope.",
                error_code="REPAIR_LINEAGE_RECORDING_REQUIRED",
                metadata={
                    "runner_dispatched": False,
                    "repair_context_id": intent.repair_context_id,
                },
            )
        # A run without a control sink still owns its intent. Build dispatches
        # use a unique unrecorded envelope but retain the same intent lineage.
        if envelope_id is None and not getattr(self, "_suppress_control_action_envelope", False):
            set_action_context(
                envelope_id=None,
                intent_source=intent.source,
                intent_id=intent.intent_id,
                intent_domain_id=intent.domain_id,
                intent_exact_params=bounded_exact_params(params),
                action_fingerprint=intent.action_fingerprint,
                trigger_assessment_id=intent.trigger_assessment_id,
                repair_context_id=intent.repair_context_id,
                repair_context_sha256=intent.repair_context_sha256,
                predecessor_contract_id=intent.predecessor_contract_id,
                control_recording_active=False,
            )
        return envelope_id

    def _emit_control_action_envelope(
        self,
        tool: str,
        params: Dict[str, Any],
        *,
        intent: ActionIntent | None = None,
    ) -> str | None:
        """Key the envelope that every downstream `tool_result` hangs off.

        Identity is the native turn's `tool_call_id`:
        `self._active_native_tool_call_id` when a harness-authored call is
        executing out of tail order, else the id of the ACTION step currently
        being executed. `_emit_control_tool_result` drops every event whose
        envelope id is falsy, so losing the identity silently loses every tool
        result — replay, the A/B collector and the webui timeline with it.
        `plan_index` survives only in the hash helper, for transcripts recorded
        before Plan 2.

        Emitting the envelope also OPENS the request scope the build facade
        freezes its invocation contract against (Plan 6 Stage B): the tool
        layer has no other view of this identity, and the scope is cleared
        first so a dispatch can never inherit the previous call's envelope.
        """
        if getattr(self, "_suppress_control_action_envelope", False):
            return None
        clear_action_context()
        sink = getattr(self, "control_event_sink", None)
        if sink is None:
            return None
        tool_call_id = getattr(self, "_active_native_tool_call_id", None)
        if not tool_call_id:
            for step in reversed(getattr(self, "steps", None) or ()):
                if getattr(step, "step_type", None) is StepType.ACTION:
                    tool_call_id = getattr(step, "tool_call_id", None)
                    break
        if not tool_call_id:
            raise PreDispatchControlError(
                "Control action has no active model tool-call identity.",
                error_code="ACTION_ENVELOPE_IDENTITY_MISSING",
                metadata={"runner_dispatched": False},
            )
        try:
            safe_params = bounded_exact_params(params)
        except (TypeError, ValueError) as exc:
            raise PreDispatchControlError(
                f"Exact action parameters cannot be recorded: {exc}",
                error_code="ACTION_PARAMS_UNRECORDABLE",
                metadata={"runner_dispatched": False},
            ) from exc
        envelope_id = f"envelope-{sink.sequence + 1:06d}"
        payload: Dict[str, Any] = {
            "envelope_id": envelope_id,
            "tool": tool,
            "exact_params": safe_params,
            "envelope_sha256": action_envelope_sha256(
                tool_call_id=tool_call_id,
                tool=tool,
                exact_params=safe_params,
            ),
            "tool_call_id": str(tool_call_id),
        }
        if intent is not None:
            payload.update(
                {
                    "intent_id": intent.intent_id,
                    "intent_source": intent.source,
                    "action_fingerprint": intent.action_fingerprint,
                }
            )
            if intent.trigger_assessment_id:
                payload["trigger_assessment_id"] = intent.trigger_assessment_id
            if intent.repair_context_id:
                payload["repair_context_id"] = intent.repair_context_id
                payload["repair_context_sha256"] = intent.repair_context_sha256
                payload["domain_id"] = intent.domain_id
                payload["blocking_fact_refs"] = list(intent.blocking_fact_refs)
                payload["repair_hypothesis"] = intent.repair_hypothesis
                payload["next_action_kind"] = intent.next_action_kind
                payload["expected_observation"] = list(intent.expected_observation)
                payload["stop_condition"] = intent.stop_condition
            payload["envelope_sha256"] = action_envelope_sha256(
                tool_call_id=tool_call_id,
                tool=tool,
                exact_params=safe_params,
                intent_id=intent.intent_id,
                intent_source=intent.source,
                action_fingerprint=intent.action_fingerprint,
                trigger_assessment_id=intent.trigger_assessment_id,
                repair_context_id=intent.repair_context_id,
                repair_context_sha256=intent.repair_context_sha256,
                domain_id=(intent.domain_id if intent.repair_context_id else None),
                blocking_fact_refs=(
                    intent.blocking_fact_refs if intent.repair_context_id else None
                ),
                repair_hypothesis=(intent.repair_hypothesis if intent.repair_context_id else None),
                next_action_kind=(intent.next_action_kind if intent.repair_context_id else None),
                expected_observation=(
                    intent.expected_observation if intent.repair_context_id else None
                ),
                stop_condition=(intent.stop_condition if intent.repair_context_id else None),
            )
        try:
            emitted = self._emit_control_event("action_envelope", payload)
        except Exception as exc:
            clear_action_context()
            raise PreDispatchControlError(
                f"Exact action envelope did not persist: {type(exc).__name__}",
                error_code="ACTION_ENVELOPE_PERSIST_FAILED",
                metadata={"runner_dispatched": False},
            ) from exc
        if emitted is None:
            clear_action_context()
            raise PreDispatchControlError(
                "Exact action envelope did not persist.",
                error_code="ACTION_ENVELOPE_PERSIST_FAILED",
                metadata={"runner_dispatched": False},
            )
        self._active_control_envelope_id = envelope_id
        set_action_context(
            envelope_id=envelope_id,
            intent_source=intent.source if intent is not None else "model",
            intent_id=intent.intent_id if intent is not None else None,
            intent_domain_id=intent.domain_id if intent is not None else None,
            intent_exact_params=safe_params if intent is not None else None,
            action_fingerprint=(intent.action_fingerprint if intent is not None else None),
            trigger_assessment_id=(intent.trigger_assessment_id if intent is not None else None),
            repair_context_id=intent.repair_context_id if intent is not None else None,
            repair_context_sha256=(intent.repair_context_sha256 if intent is not None else None),
            predecessor_contract_id=(
                intent.predecessor_contract_id if intent is not None else None
            ),
            control_recording_active=True,
        )
        return envelope_id

    @staticmethod
    def _delivered_claim_identity(metadata: Mapping[str, Any] | None) -> str:
        """Name the claim a delivered gate belongs to, from the metadata itself.

        Taken before compaction: `phase_claim` is bounded like every other
        recorded string, and a truncated claim would hash to a name the live
        registry never used (spec §3.4).
        """
        claim_data = (metadata or {}).get("phase_claim")
        if not isinstance(claim_data, Mapping):
            return ""
        try:
            return claim_identity(PhaseClaim.from_metadata(claim_data))
        except (TypeError, ValueError, PermissionError):
            return ""

    @staticmethod
    def _control_result_projection(result: ToolResult) -> Dict[str, Any]:
        output_ref = result.output_ref or next(
            (str(ref) for ref in [*result.evidence_refs, *result.refs] if ref),
            None,
        )
        metadata = compact_control_value(result.metadata)
        claim_sha256 = ReActEngine._delivered_claim_identity(result.metadata)
        if claim_sha256:
            metadata["phase_claim_sha256"] = claim_sha256
        projection: Dict[str, Any] = {
            "invocation_status": result.invocation_status.value,
            "operation_outcome": result.operation_outcome.value,
            "evidence_status": result.evidence_status.value,
            "output": (
                f"stored as {output_ref}"
                if output_ref
                else "output body omitted; verify output_sha256"
            ),
            "evidence_assessment": result.evidence_assessment.value,
            "metadata": metadata,
            "evidence_refs": list(result.evidence_refs),
            "conflicts": list(result.conflicts),
            "validator_findings": [
                finding.model_dump(mode="json") for finding in result.validator_findings
            ],
            "facts": compact_control_value(result.facts),
            "refs": list(result.refs),
        }
        for name in (
            "poll_ref",
            "failure_signature",
            "error_tail_preview",
            "output_ref",
            "error",
            "error_code",
        ):
            value = getattr(result, name)
            if value is not None:
                projection[name] = compact_control_value(value)
        if result.test_stats is not None:
            projection["test_stats"] = result.test_stats.model_dump(mode="json")
        return projection

    #: The typed markers a refusal leaves on its result when it carries no
    #: `error_code` of its own. Ordered, so one refusal always names itself the
    #: same way; a second marker on the same result never renames the first.
    _REFUSAL_MARKERS = (
        "execution_refused",
        "report_refused",
        "report_delivery_failure",
        "advisor_redirect",
    )

    @classmethod
    def _refusal_code(cls, execution: ToolExecution) -> str:
        """Why this call was refused, in the refusal's own words.

        Never inferred from what happened afterwards: the code the refusal
        stated is the code the record carries, and a refusal that stated none
        is named by its typed marker rather than given a plausible one.
        """
        result = execution.result
        code = str(getattr(result, "error_code", "") or "").strip()
        if code:
            return code
        metadata = result.metadata or {}
        for marker in cls._REFUSAL_MARKERS:
            value = str(metadata.get(marker) or "").strip()
            if value:
                return f"{marker}:{value}"
        return str(execution.status or "").strip() or "refused"

    def _emit_control_refusal_record(
        self,
        call: ToolCall,
        execution: ToolExecution,
        params: Dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Seal the call nobody accepted (spec §2.2 rule 4).

        A refused call has no envelope, because nothing was dispatched, and no
        `tool_result`, because nothing answered. Until this record it therefore
        appeared in the ledger only as a `loop_decision` describing an execution
        that never happened — the exact shape five projects were measured in.

        The parameters are committed as a digest rather than a copy: the refusal
        of a call and the envelope of its retry then compare directly, which is
        what camel-quarkus seq 124→125 needed and could not do. A submitted
        `repair_intent` is carried whole, because no envelope will (spec §2.2
        rule 3). Never raises — a run does not end because its record of a
        refusal would not validate.
        """
        self._seal_refusal_record(
            call,
            refusal_code=self._refusal_code(execution),
            params=params,
            tool_call_id=tool_call_id,
        )

    def _seal_refusal_record(
        self,
        call: ToolCall,
        *,
        refusal_code: str,
        params: Dict[str, Any] | None,
        tool_call_id: str | None,
    ) -> None:
        """Write one refusal into the ledger, whoever refused the call.

        A tool that does not exist, a repair context that is not open, a batch
        that broke before this call's turn came — the record is the same shape
        because the fact is the same fact: the model asked, and nothing ran.
        """
        sink = getattr(self, "control_event_sink", None)
        if sink is None:
            return
        try:
            payload = RefusalRecordPayload(
                tool=str(call.name or "") or "unknown",
                tool_call_id=str(tool_call_id or "") or None,
                refusal_code=refusal_code,
                exact_params_sha256=self._refused_params_digest(params),
                **self._submitted_repair_intent(call),
            )
            self._emit_control_event("refusal_record", payload.model_dump(mode="json"))
        except Exception as exc:  # observability never ends a run
            logger.warning(f"refusal record was not sealed: {exc}")

    @staticmethod
    def _refused_params_digest(params: Dict[str, Any] | None) -> str:
        """What was asked, in the one form that always fits.

        The exact form is the digest an envelope commits, so a refusal and the
        envelope of its retry compare. A parameter set too large or too exotic
        to commit exactly could never have produced that envelope either, so it
        is digested from its bounded projection instead — which still tells one
        refused call from another, and never claims the call had no parameters.
        """
        try:
            return canonical_sha256(bounded_exact_params(params or {}))
        except (TypeError, ValueError):
            return canonical_sha256(compact_control_value(params or {}))

    @staticmethod
    def _submitted_repair_intent(call: ToolCall) -> Dict[str, Any]:
        """The model's own repair block, kept absent when it submitted none."""
        submission = getattr(call, "repair_intent_submission", None)
        if not isinstance(submission, Mapping):
            return {}
        return {"repair_intent": dict(submission)}

    def _emit_control_tool_result(
        self,
        *,
        envelope_id: str | None,
        execution_id: str,
        tool: str,
        params: Dict[str, Any],
        result: ToolResult,
        actual_executions: List[ActualToolExecution] | None = None,
    ) -> None:
        if not envelope_id:
            clear_action_context()
            return
        machine = getattr(self, "phase_machine", None)
        safe_params = bounded_exact_params(params)
        physical_actuals = [
            actual
            for actual in (actual_executions or ())
            if self._actual_execution_proves_dispatch(actual) is True
        ]
        output_source = result.raw_output if result.raw_output is not None else result.output
        try:
            self._emit_control_event(
                "tool_result",
                {
                    "envelope_id": envelope_id,
                    "execution_id": execution_id,
                    "tool": tool,
                    "params": safe_params,
                    "scope": self._tool_evidence_scope(tool, params).value,
                    "roles": [
                        role.value for role in self._tool_evidence_roles(tool, params, result)
                    ],
                    "result": self._control_result_projection(result),
                    "source_phase": getattr(machine, "current_phase", "") or "",
                    "source_attempt_id": getattr(machine, "current_attempt_id", "") or "",
                    "actual_executions": [
                        {
                            "execution_id": actual.execution_id,
                            "tool": actual.tool_name,
                            "params": bounded_exact_params(actual.params),
                            "scope": self._tool_evidence_scope(
                                actual.tool_name,
                                actual.params,
                            ).value,
                            "roles": [
                                role.value
                                for role in self._tool_evidence_roles(
                                    actual.tool_name,
                                    actual.params,
                                    actual.result,
                                )
                            ],
                            "result": self._control_result_projection(actual.result),
                        }
                        for actual in physical_actuals
                    ],
                    "output_sha256": hashlib.sha256(
                        str(output_source or "").encode("utf-8", errors="replace")
                    ).hexdigest(),
                },
            )
        finally:
            self._active_control_envelope_id = None
            clear_action_context()
        resolved_commit = (result.metadata or {}).get("resolved_commit")
        callback = getattr(self, "_target_repo_sha_callback", None)
        if resolved_commit and callable(callback):
            try:
                callback(str(resolved_commit))
            except Exception as exc:
                logger.warning(f"Could not update target repository run pin: {exc}")

    @staticmethod
    def _actual_execution_proves_dispatch(
        actual: ActualToolExecution,
    ) -> bool | None:
        """Classify a leaf without letting the build facade self-attest.

        A facade-shaped ``build`` record is only a wrapper around zero or more
        backend executions.  Without an explicit runner marker it is unknown,
        not physical evidence.  Other actual-execution tool names are the
        flattened physical leaves themselves.
        """

        result = actual.result
        metadata = dict(result.metadata or {})
        marker = metadata.get("runner_dispatched", None)
        if marker is not None and not isinstance(marker, bool):
            return None
        refused = str(result.error_code or "").strip() in (_REPAIR_PREDISPATCH_REFUSAL_CODES)
        if marker is True and refused:
            return None
        if marker is True:
            return True
        if marker is False or refused:
            return False
        if str(actual.tool_name or "").strip().lower() == "build":
            return None
        return True

    def _repair_action_dispatch_outcome(self, execution: ToolExecution) -> bool | None:
        """Return strict repair consumption truth; None is an integrity fault."""

        result = execution.result
        metadata = dict(result.metadata or {})
        marker = metadata.get("runner_dispatched", None)
        if marker is not None and not isinstance(marker, bool):
            self._record_repair_dispatch_integrity_failure("repair_runner_dispatch_marker_invalid")
            return None
        refusal = (
            marker is False
            or str(result.error_code or "").strip() in _REPAIR_PREDISPATCH_REFUSAL_CODES
        )
        actual_states = tuple(
            self._actual_execution_proves_dispatch(actual) for actual in execution.actual_executions
        )
        if any(state is None for state in actual_states):
            self._record_repair_dispatch_integrity_failure("repair_dispatch_evidence_unknown")
            return None
        actual_dispatch = any(state is True for state in actual_states)
        dispatched = actual_dispatch or marker is True
        if dispatched and refusal:
            self._record_repair_dispatch_integrity_failure("repair_dispatch_evidence_contradiction")
            return None
        if dispatched:
            return True
        if refusal:
            return False
        self._record_repair_dispatch_integrity_failure("repair_dispatch_evidence_unknown")
        return None

    def _record_repair_dispatch_integrity_failure(self, code: str) -> None:
        normalized = str(code or "repair_dispatch_integrity_failure").strip()
        self._fatal_harness_control_failure = normalized
        state = getattr(self, "run_evidence_state", None)
        machine = getattr(self, "phase_machine", None)
        context = getattr(self, "_pending_repair_context", None)
        if state is None or state.sealed:
            return
        refs = tuple(context.observed_fact_refs) if isinstance(context, RepairContext) else ()
        signature = f"harness_control:{getattr(machine, 'current_attempt_id', '')}:{normalized}"
        if any(
            blocker.status == "active" and blocker.failure_signature == signature
            for blocker in state.blockers
        ):
            return
        state.record_blocker(
            failure_signature=signature,
            category="harness_control",
            error_code=normalized,
            evidence_refs=refs,
            source_phase=getattr(machine, "current_phase", None),
            source_attempt_id=getattr(machine, "current_attempt_id", None),
        )

    def _gate_evidence_refs(self, gate: GateResult) -> tuple[str, ...]:
        """Give control-derived facts stable provenance before emit/replay."""

        refs = tuple(gate.evidence_refs)
        if refs or not gate.validated_facts:
            return refs
        machine = getattr(self, "phase_machine", None)
        return (
            "control:gate:"
            f"{getattr(machine, 'current_attempt_id', '')}:"
            f"{gate.code or gate.control_disposition.value}",
        )

    @staticmethod
    def _embedded_gate_body(gate: GateResult) -> Dict[str, Any]:
        """The gate's ONE serialization, bounded exactly as the tool result
        bounds its embedded copy.

        `_control_result_projection` compacts the whole metadata mapping, so the
        gate body is visited one level down. Compacting it at the top level here
        would bound its children at a different depth and the two copies of the
        same object would stop comparing equal.
        """
        return compact_control_value({"gate_result": gate.to_metadata()})["gate_result"]

    def _current_attempt_id(self) -> str:
        return str(getattr(getattr(self, "phase_machine", None), "current_attempt_id", "") or "")

    def _delivered_gates(self) -> Dict[str, GateResult]:
        """Words handed to the model under the OPEN attempt, and only those.

        A gate delivered under a closed attempt can never be the one a later
        decision replaces, so the registry is rebuilt rather than accumulated.
        """
        attempt = self._current_attempt_id()
        if getattr(self, "_delivered_gate_attempt", None) != attempt:
            self._delivered_gate_attempt = attempt
            self._delivered_gate_decisions = {}
        return self._delivered_gate_decisions

    def _register_delivered_gate(self, claim: PhaseClaim, gate: GateResult) -> None:
        """Record the word this result hands the model, whatever it decided.

        A rejection is delivered exactly as an acceptance is: the model reads
        the gate's reason and re-plans on it. Registering only the accepted path
        left a delivered-but-unclosed word standing as the target of the next
        rejection of the same claim text, and the honest second grading was
        refused as if the first had been replaced behind the model's back.
        """
        self._delivered_gates()[claim_identity(claim)] = gate

    def _refuse_undelivered_word(self, claim: PhaseClaim, gate: GateResult) -> None:
        """A word the model acted on may not be replaced behind its back.

        camel delivered `failed` and sealed `unknown`; polaris embedded
        `success`/`green` and sealed `unknown`/`unavailable` two events later.
        Both are one statement re-graded after delivery. A second grading is
        legal — it just has to name the first and be spoken aloud first.
        """
        delivered = self._delivered_gates().get(claim_identity(claim))
        if delivered is None or delivered.decision_id == gate.decision_id:
            return
        if gate.supersedes != delivered.decision_id:
            raise ValueError(
                "a gate decision that replaces a delivered word must supersede it: "
                f"delivered {delivered.decision_id}, sealing {gate.decision_id}"
            )
        if (gate.accepted, PhaseOutcome(gate.validated_outcome)) == (
            delivered.accepted,
            PhaseOutcome(delivered.validated_outcome),
        ):
            return
        revisions = getattr(self, "_revised_gate_decisions", None) or set()
        if (delivered.decision_id, gate.decision_id) not in revisions:
            raise ValueError(
                "a sealed word that differs from the delivered one requires a "
                f"gate_outcome_revised observation: {delivered.decision_id} -> "
                f"{gate.decision_id}"
            )

    def _emit_control_gate(
        self,
        claim: PhaseClaim,
        gate: GateResult,
        *,
        survey: Callable[[], TestCandidateResolution] | None = None,
    ):
        """Seal one graded claim. ``survey`` is the close's shared reader.

        The coordinates this event seals must be the coordinates the close was
        GRADED against: a second read of the survey can answer differently (a
        manifest rewritten between them, a symlink that now resolves
        elsewhere), and the record would then show one close whose cap, whose
        requirement and whose sealed `test_candidate_resolution` disagree. A
        caller that opened no close survey (the report-reserve close) still
        gets exactly one lazy read of its own.
        """
        self._refuse_undelivered_word(claim, gate)
        # The word the record now stands behind. A carried observation about an
        # earlier grading is retired by this line, not by whoever remembers.
        self._last_sealed_decision_id = gate.decision_id
        body = self._embedded_gate_body(gate)
        # The flat fields below are the event's bounded, provenance-resolved
        # projection of `body`; `body` itself goes in verbatim, because it is the
        # copy that must equal the one the tool result already delivered.
        validated_facts = compact_control_value(dict(gate.validated_facts))
        machine = getattr(self, "phase_machine", None)
        control_evidence_refs = list(self._gate_evidence_refs(gate))
        gate_code = body["code"] or (
            "phase_claim_accepted" if body["accepted"] else "phase_claim_contradicted"
        )
        gate_payload: Dict[str, Any] = {
            "phase": claim.phase,
            "signal": claim.signal,
            "claimed_outcome": claim.claimed_outcome.value,
            "decision_id": body["decision_id"],
            "claim_sha256": claim_identity(claim),
            "validator_state": body["validator_state"],
            "expected_accepted": body["accepted"],
            "expected_outcome": body["validated_outcome"],
            "control_disposition": body["control_disposition"],
            "blocker_owner": body["blocker_owner"],
            "code": gate_code,
            "reason": body["reason"],
            "key_results": claim.key_results,
            "evidence_refs": control_evidence_refs,
            # Gate reason/evidence describe the grading. Preserve the exact
            # model claim separately so replay can reconstruct the same shape
            # that `claim_sha256` names, including Analyze plan identity.
            "phase_claim": claim.to_metadata(),
            "validated_facts": validated_facts,
            "gate_result": body,
            "source_attempt_id": getattr(machine, "current_attempt_id", None),
        }
        if claim.execution_plan_sha256:
            gate_payload["execution_plan_sha256"] = claim.execution_plan_sha256
        if claim.execution_plan_ref:
            gate_payload["execution_plan_ref"] = claim.execution_plan_ref
        if body.get("supersedes"):
            gate_payload["supersedes"] = body["supersedes"]
        if claim.phase == "test":
            gate_payload["test_candidate_resolution"] = (
                survey or self._test_candidate_survey()
            )().to_snapshot()
        self._emit_control_event(
            "validator_observation",
            {
                "phase": claim.phase,
                "validator_state": body["validator_state"],
                "control_disposition": body["control_disposition"],
                "blocker_owner": body["blocker_owner"],
                "reason": body["reason"],
                "evidence_refs": control_evidence_refs,
                "validated_facts": validated_facts,
            },
        )
        return self._emit_control_event("gate_decision", gate_payload)

    def _emit_gate_outcome_revised(
        self,
        claim: PhaseClaim,
        delivered: GateResult,
        revised: GateResult,
    ) -> str:
        """One text, two sinks: the model reads the revision the record seals.

        The text is returned rather than delivered here because the caller knows
        which window the model will actually read: a revision appended before a
        phase transition is erased by the window reset that follows it.
        """

        text = gate_observation_text(
            revised,
            phase=claim.phase,
            origin="outcome_revision",
            superseded=delivered,
        )
        machine = getattr(self, "phase_machine", None)
        self._emit_control_event(
            "gate_outcome_revised",
            {
                "phase": claim.phase,
                "delivered_decision_id": delivered.decision_id,
                "revised_decision_id": revised.decision_id,
                "delivered_outcome": PhaseOutcome(delivered.validated_outcome).value,
                "revised_outcome": PhaseOutcome(revised.validated_outcome).value,
                "delivered_accepted": delivered.accepted,
                "revised_accepted": revised.accepted,
                "reason": revised.reason,
                "code": revised.code or "gate_outcome_revised",
                "observation_text": text,
                "source_attempt_id": getattr(machine, "current_attempt_id", None),
            },
        )
        revisions = getattr(self, "_revised_gate_decisions", None)
        if revisions is None:
            revisions = set()
            self._revised_gate_decisions = revisions
        revisions.add((delivered.decision_id, revised.decision_id))
        return text

    def _seal_engine_gate(
        self,
        claim: PhaseClaim,
        gate: GateResult,
        *,
        deliver: bool = True,
        carry: bool = True,
        survey: Callable[[], TestCandidateResolution] | None = None,
    ):
        """The engine's own closes state the word they seal (spec §3.2).

        Four close paths minted a claim, graded it and sealed a `gate_decision`
        while the model saw nothing but a `logger.warning`. Emitting and
        delivering through one sink makes that a property of the sink rather
        than a discipline each caller has to remember.

        `carry` is False for a caller that routes no phase decision after this:
        with no window reset to survive, a carried copy is a word left waiting
        for the next transition to speak it out of turn.

        `survey` is the close's shared coordinate reader, passed through to the
        seal so the sealed coordinates are the graded ones.
        """
        turn_started = self._turn_stamp()
        event = self._emit_control_gate(claim, gate, survey=survey)
        observation_ref = None
        if deliver:
            text = gate_observation_text(gate, phase=claim.phase, origin="engine_close")
            if carry:
                self._deliver_gate_observation(text, priority=8, decision_id=gate.decision_id)
            else:
                self._state_gate_observation_now(text, priority=8)
            observation_ref = self._store_bytes_once(text, label="delivered_observation")
        # The controller graded this phase itself: no model turn carried the
        # word, so the word gets a turn of its own in the same sequence. The
        # id is CLAIMED so a model turn still in flight cannot also report it —
        # one grading belongs to one turn (spec §2.1).
        claims = getattr(self, "_turn_gate_claims", None)
        if claims is None:
            claims = set()
            self._turn_gate_claims = claims
        claims.add(gate.decision_id)
        self._seal_turn_record(
            actor="controller",
            t0=turn_started,
            t1=self._turn_stamp(),
            gate_decision_id=gate.decision_id,
            observation_ref=observation_ref,
            iteration=getattr(self, "current_iteration", None),
        )
        return event

    def _deliver_gate_observation(self, text: str, *, priority: int, decision_id: str) -> None:
        """State the word in this window, and again in the one a reset opens.

        A close observation appended moments before `_apply_phase_decision`
        rebuilds the window is erased by that rebuild — which is how four engine
        closes could look delivered and still reach nobody.

        The carried copy NAMES the decision it describes. A branch that speaks
        and then declines to route (the rejected cap) leaves its word standing
        while the attempt stays open, and the next grading of that attempt
        replaces it: re-stating the superseded sentence in the window the
        transition opens contradicts the record the model just watched seal
        (spec §3.1).
        """
        self._pending_gate_observation = (
            text,
            priority,
            self._current_attempt_id(),
            str(decision_id or ""),
        )
        self._add_system_guidance(text, priority=priority)

    def _state_gate_observation_now(self, text: str, *, priority: int) -> None:
        """State the word in THIS window, and carry nothing forward.

        For a branch that speaks and then declines to route: no window reset
        follows it, so there is nothing for a carried copy to survive — it would
        simply wait under the still-open attempt for the next transition. That
        is how a superseded revision ("the sealed outcome is 'unknown'") was
        re-stated as CRITICAL GUIDANCE inside the phase that followed the sealed
        `success` which replaced it.
        """
        self._pending_gate_observation = None
        self._add_system_guidance(text, priority=priority)

    def _pending_window_observation(self) -> tuple[str, int] | None:
        """The carried word, if it still describes what the record last sealed.

        Claiming it clears it: a word that does not survive this test is spent,
        not deferred to some later transition.
        """
        parked = getattr(self, "_pending_gate_observation", None)
        self._pending_gate_observation = None
        if parked is None:
            return None
        text, priority, attempt_id, decision_id = parked
        if attempt_id != self._current_attempt_id():
            return None
        if decision_id != getattr(self, "_last_sealed_decision_id", None):
            return None
        return (text, priority)

    def _repair_tool_affordances(self) -> tuple[ToolSemanticAffordance, ...]:
        """Project-action capabilities, without params, examples, or ordering."""

        excluded = {"advisor", "manage_context", "phase", "report"}
        affordances: list[ToolSemanticAffordance] = []
        for name, tool in sorted(getattr(self, "tools", {}).items()):
            normalized = str(name or "").strip().lower()
            if not normalized or normalized in excluded:
                continue
            try:
                schema = tool.get_parameter_schema()
            except Exception:
                schema = {}
            properties = dict((schema or {}).get("properties") or {})
            action_schema = properties.get("action") or {}
            action_kinds = tuple(
                str(value).strip()
                for value in action_schema.get("enum") or ()
                if str(value).strip()
            )
            affordances.append(
                ToolSemanticAffordance(
                    tool=normalized,
                    action_kinds=action_kinds,
                    action_parameter=("action" if "action" in properties else None),
                )
            )
        return tuple(affordances)

    @staticmethod
    def _fact_value_recursive(value: Any, key: str) -> Any:
        if isinstance(value, Mapping):
            if key in value:
                return value[key]
            for child in value.values():
                found = ReActEngine._fact_value_recursive(child, key)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for child in value:
                found = ReActEngine._fact_value_recursive(child, key)
                if found is not None:
                    return found
        return None

    def _repair_fingerprints(self, gate: GateResult) -> RepairFingerprintSet:
        facts = dict(gate.validated_facts or {})
        values: dict[str, Any] = {}
        for field_name, fact_key in (
            ("target_sha", "target_sha"),
            ("survey_fingerprint", "survey_fingerprint"),
            ("config_fingerprint", "config_fingerprint"),
            ("document_map_fingerprint", "document_map_fingerprint"),
            ("fact_epoch", "fact_epoch"),
        ):
            value = self._fact_value_recursive(facts, fact_key)
            if value is not None:
                values[field_name] = value
        return RepairFingerprintSet.model_validate(values)

    def _active_blocker_refs(self) -> tuple[str, ...]:
        state = getattr(self, "run_evidence_state", None)
        if state is None:
            return ()
        return tuple(
            sorted(
                {
                    str(blocker.blocker_id)
                    for blocker in state.blockers
                    if str(getattr(blocker, "status", "")) == "active"
                }
            )
        )

    def _gate_control_assessment(
        self,
        claim: PhaseClaim,
        gate: GateResult,
    ) -> ControlAssessment:
        machine = getattr(self, "phase_machine", None)
        subject_material = {
            "decision_id": gate.decision_id,
            "phase_attempt_id": str(getattr(machine, "current_attempt_id", "") or ""),
            "phase": claim.phase,
            "validator_state": gate.validator_state.value,
            "validated_outcome": gate.validated_outcome.value,
            "control_disposition": gate.control_disposition.value,
            "blocker_owner": gate.blocker_owner.value,
            "code": gate.code or "phase_claim_contradicted",
            "validated_facts": compact_control_value(dict(gate.validated_facts or {})),
            "evidence_refs": sorted(set(gate.evidence_refs)),
        }
        subject_id = GATE_ASSESSMENT_SUBJECT_PREFIX + canonical_sha256(subject_material)[:16]
        return ControlAssessment(
            event_or_intent_id=subject_id,
            stage="gate",
            typed_code=gate.code or "phase_claim_contradicted",
            detail=(
                f"phase={claim.phase}; validator={gate.validator_state.value}; "
                f"maximum={gate.validated_outcome.value}; "
                f"disposition={gate.control_disposition.value}"
            ),
            blocker_owner=gate.blocker_owner,
            observed_facts=compact_control_value(dict(gate.validated_facts or {})),
            # The gate event retains the complete report/path set. Refer to
            # that decision instead of copying an unbounded set into a
            # 64-reference assessment and then a 64-reference repair context.
            evidence_refs=(gate.decision_id,),
        )

    def _install_repair_context(
        self,
        claim: PhaseClaim,
        gate: GateResult,
    ) -> tuple[RepairContext | None, str]:
        """Persist judge assessment + non-prescriptive policy context.

        Both writes are evidence transport.  Two bounded attempts make a
        transient write failure controller-owned; the model never receives an
        ungrounded in-memory context.
        """

        orchestrator = getattr(self, "orchestrator", None)
        from sag.runtime.container_io import resolve_control_execute

        execute = resolve_control_execute(orchestrator)
        if not callable(execute):
            return None, "repair_context_transport_unavailable"
        assessment = self._gate_control_assessment(claim, gate)
        assessment_persisted = False
        for _ in range(2):
            if write_assessment(execute, assessment):
                assessment_persisted = True
                break
        if not assessment_persisted:
            return None, "repair_assessment_persist_failed"

        try:
            constraint = RepairConstraint(
                constraint_id=f"constraint-{assessment.assessment_id[-8:]}",
                kind="judge_outcome_ceiling",
                subject=claim.phase,
                relation="maximum_supported_outcome",
                value=gate.validated_outcome.value,
                source_refs=(assessment.assessment_id,),
            )
            context = build_repair_context(
                trigger_assessment_id=assessment.assessment_id,
                typed_blocker=assessment.typed_code,
                blocker_owner=gate.blocker_owner.value,
                domain_id=self._action_domain_id(),
                fingerprints=self._repair_fingerprints(gate),
                observed_fact_refs=(assessment.assessment_id, gate.decision_id),
                constraint_set=ConstraintSet(
                    constraints=(constraint,),
                    source_refs=(assessment.assessment_id,),
                ),
                allowed_tool_affordances=self._repair_tool_affordances(),
                admissible_observation_types=(
                    "tool_result",
                    "receipt_assessment",
                    "artifact_or_report_delta",
                    "job_lifecycle_transition",
                ),
                open_conflict_refs=self._active_blocker_refs(),
            )
        except (TypeError, ValueError):
            return None, "repair_context_projection_invalid"
        write_code = ""
        for _ in range(2):
            write_result = write_repair_context(execute, context)
            write_code = write_result.code
            if write_result.persisted:
                return context, "persisted"
        return None, write_code or "repair_context_persist_failed"

    @staticmethod
    def _repair_context_guidance(context: RepairContext) -> str:
        # This is a neutral, bounded projection of the exact context persisted
        # in repair_context_opened. It exposes semantic capabilities and fact
        # provenance, never a parameter value, argv or harness-selected call.
        affordances = "; ".join(
            (
                f"{item.tool}: selector={item.action_parameter or 'tool'}; "
                f"kinds={','.join(item.action_kinds) or item.tool}; "
                f"constraint_refs={','.join(item.constraint_refs) or 'none'}"
            )
            for item in context.allowed_tool_affordances
        )
        constraints = "; ".join(
            (
                f"{item.constraint_id}: {item.kind} "
                f"{item.subject} {item.relation} {item.value}; "
                f"source_refs={','.join(item.source_refs) or 'none'}"
            )
            for item in context.constraint_set.constraints
        )
        fingerprints = ", ".join(
            f"{key}={value}"
            for key, value in context.fingerprints.model_dump(
                mode="json",
                exclude_none=True,
            ).items()
        )
        guidance = (
            f"REPAIR_CONTEXT {context.repair_context_id}\n"
            f"Judge blocker: {context.typed_blocker}; owner={context.blocker_owner}; "
            f"domain={context.domain_id or 'unknown'}\n"
            f"Observed fact refs: {', '.join(context.observed_fact_refs) or 'none'}\n"
            f"Supporting claim refs: {', '.join(context.supporting_claim_ids) or 'none'}\n"
            f"Open conflict refs: {', '.join(context.open_conflict_refs) or 'none'}\n"
            "Constraint-set source refs: "
            f"{', '.join(context.constraint_set.source_refs) or 'none'}\n"
            f"Fact fingerprints: {fingerprints or 'none'}\n"
            f"Constraints: {constraints or 'none'}\n"
            f"Available public semantic affordances: {affordances or 'none'}\n"
            "Admissible new observations: "
            f"{', '.join(context.admissible_observation_types) or 'none'}\n"
            "Choose an ordinary public tool action yourself. For an admissible tool, "
            "supply repair_intent with context refs, your hypothesis, the semantic "
            "action kind, expected observation, and stop condition. The harness has "
            "not selected parameters or a command. You may instead make an honest "
            "terminal claim at or below the judge-supported outcome."
        )
        if len(guidance.encode("utf-8")) > _REPAIR_GUIDANCE_MAX_BYTES:
            raise ValueError("repair context guidance exceeds its byte limit")
        return guidance

    def _completion_event(
        self,
        claim: PhaseClaim,
        gate: GateResult,
        *,
        assessment_fingerprints: Sequence[str] = (),
        disposition: GateControlDisposition | None = None,
    ) -> CompletionClaimEvent:
        facts = compact_control_value(dict(gate.validated_facts or {}))
        job_facts = {
            key: facts.get(key)
            for key in (
                JOB_BARRIER_FACT,
                OPEN_OBLIGATIONS_FACT,
                TERMINAL_UNPERSISTED_FACT,
                JOB_INTEGRITY_FACT,
            )
            if isinstance(facts, Mapping) and facts.get(key) is not None
        }
        mechanical = {
            "phase": claim.phase,
            "validator_state": gate.validator_state.value,
            "validated_outcome": gate.validated_outcome.value,
            "code": gate.code,
            "facts": facts,
        }
        selected_disposition = disposition or gate.control_disposition
        return CompletionClaimEvent(
            phase_attempt_id=str(
                getattr(getattr(self, "phase_machine", None), "current_attempt_id", "") or ""
            ),
            claim_kind=claim.signal,  # type: ignore[arg-type]
            judge_disposition=selected_disposition.value,  # type: ignore[arg-type]
            blocker_id=gate.code or selected_disposition.value,
            mechanical_evidence_digest=canonical_sha256(mechanical),
            assessment_fingerprints=tuple(assessment_fingerprints),
            open_job_fingerprints=(canonical_sha256(job_facts),) if job_facts else (),
            target_fingerprint=str(ReActEngine._fact_value_recursive(facts, "target_sha") or ""),
            config_fingerprint=str(
                ReActEngine._fact_value_recursive(facts, "config_fingerprint") or ""
            ),
            fact_fingerprint=canonical_sha256(facts),
            evidence_refs=gate.evidence_refs,
            prose=" ".join((claim.key_results, claim.reason)).strip(),
        )

    def _emit_completion_claim_decision(
        self,
        event: CompletionClaimEvent,
        decision: CompletionClaimDecision,
    ) -> None:
        self._emit_control_event(
            "completion_claim_decision",
            {
                "phase_attempt_id": event.phase_attempt_id,
                "claim_kind": event.claim_kind,
                "judge_disposition": event.judge_disposition,
                "blocker_id": event.blocker_id,
                "mechanical_evidence_digest": event.mechanical_evidence_digest,
                "assessment_fingerprints": list(event.assessment_fingerprints),
                "open_job_fingerprints": list(event.open_job_fingerprints),
                "evidence_refs": list(event.evidence_refs),
                "target_fingerprint": event.target_fingerprint,
                "config_fingerprint": event.config_fingerprint,
                "fact_fingerprint": event.fact_fingerprint,
                "expected_decision": decision.decision,
                "expected_recurrence_count": decision.recurrence_count,
                "expected_reason_code": decision.reason_code,
                "expected_close_phase": decision.close_phase,
            },
        )

    def _close_phase_for_control_persist_exhaustion(
        self,
        claim: PhaseClaim,
        gate: GateResult,
        event: CompletionClaimEvent,
    ) -> bool:
        """Contain a transport-persist exhaustion as an honest phase close.

        D2 rocketmq: `repair_assessment_persist_failed` aborted the RUN while
        the model's blocked claim was factually correct. The fail-closed core
        stands — the judge's ceiling could not be made durable, so the phase
        must not continue — but the phase ENDING is the containment: no
        further model turn can exceed an un-persisted ceiling in a phase that
        no longer exists (spec 2026-08-13 §3). The run then routes like any
        blocked phase: dependents skip, evidence closes, the report delivers.
        Integrity families never reach here (fenced by
        `_CONTAINED_CONTROL_PERSIST_CODES`).
        """
        machine = getattr(self, "phase_machine", None)
        state = getattr(self, "run_evidence_state", None)
        if machine is None or state is None or state.sealed or machine.is_complete:
            return False
        refs = tuple(gate.evidence_refs)
        state.record_blocker(
            failure_signature=(
                f"control_persist_exhausted:{machine.current_attempt_id}:{event.blocker_id}"
            ),
            category="harness_control",
            error_code=event.blocker_id,
            evidence_refs=refs,
            source_phase=machine.current_phase,
            source_attempt_id=machine.current_attempt_id,
        )
        honest_claim = PhaseClaim(
            phase=machine.current_phase,
            signal="blocked",
            claimed_outcome=PhaseOutcome.UNKNOWN,
            reason=(
                "controller could not persist the judge-owned repair context; "
                "the phase closes rather than continue without a durable ceiling"
            ),
            evidence_refs=refs,
        )
        honest_gate = validate_phase_claim(
            honest_claim,
            gate.validator_state,
            reason=f"{event.blocker_id}; {gate.reason}",
            evidence_refs=refs,
            code=event.blocker_id,
            validated_facts=gate.validated_facts,
            control_disposition=GateControlDisposition.TERMINAL_CLAIMABLE,
            blocker_owner=BlockerOwner.HARNESS,
        )
        self._seal_engine_gate(honest_claim, honest_gate)
        self._record_gate_facts(honest_claim.phase, honest_gate)
        record = machine.close_attempt(honest_gate)
        route = self.transition_policy.decide(
            record,
            state=state,
            budgets=self._repair_budgets(),
        )
        self._pending_repair_context = None
        self._apply_phase_decision(record, route)
        getattr(self, "agent_logger", logger).warning(
            f"Contained control-persist exhaustion ({event.blocker_id}): "
            f"closed {honest_claim.phase} blocked instead of aborting the run"
        )
        return True

    def _close_phase_for_agent_no_progress(
        self,
        claim: PhaseClaim,
        gate: GateResult,
        decision: CompletionClaimDecision,
    ) -> bool:
        """Finalizer-owned honest close at the judge's supported maximum."""

        machine = getattr(self, "phase_machine", None)
        state = getattr(self, "run_evidence_state", None)
        if machine is None or state is None or state.sealed or machine.is_complete:
            return False
        if self._keep_analyze_open_for_execution_plan("agent_no_progress"):
            return False
        # The test phase's no-op convergence may not skip the harness-owned
        # floor: the TEST_ATTEMPT_REQUIRED gate just told the model "the
        # controller owns and will execute the registered phase-floor action",
        # and live 2026-08-09 lp-commons-dbcp converged to partial one second
        # later with the promise unkept. Force the required action ONCE per
        # attempt: a terminal receipt lets the next claim close on real
        # evidence; a registered refusal makes the recurrence's honest close
        # carry the refusal instead of a broken promise.
        required_attempt = self._missing_required_test_attempt()
        if required_attempt is not None:
            floor_key = (str(machine.current_attempt_id), required_attempt.action_text())
            if getattr(self, "_no_op_convergence_floor_key", None) != floor_key:
                self._no_op_convergence_floor_key = floor_key
                if self._force_required_test_attempt(required_attempt, trigger="no_op_convergence"):
                    self._add_system_guidance(
                        "TEST_ATTEMPT_REQUIRED: no-op completion claims cannot "
                        "close the test phase before the harness executes the "
                        f"required action: {required_attempt.action_text()}",
                        priority=9,
                    )
                    return False
        refs = tuple(gate.evidence_refs)
        blocker = state.record_blocker(
            failure_signature=(
                f"agent_no_progress:{machine.current_attempt_id}:"
                f"{gate.code or gate.control_disposition.value}"
            ),
            category="agent_control",
            error_code="agent_no_progress",
            evidence_refs=refs,
            source_phase=machine.current_phase,
            source_attempt_id=machine.current_attempt_id,
        )
        honest_claim = PhaseClaim(
            phase=machine.current_phase,
            signal="done",
            claimed_outcome=gate.validated_outcome,
            reason=(
                "finalizer close after three no-op completion claims; "
                "outcome is capped at the judge-supported maximum"
            ),
            evidence_refs=refs,
        )
        honest_gate = validate_phase_claim(
            honest_claim,
            gate.validator_state,
            reason=(
                f"agent_no_progress after {decision.recurrence_count} no-op claims; "
                f"{gate.reason}"
            ),
            evidence_refs=refs,
            code="agent_no_progress",
            validated_facts=gate.validated_facts,
            control_disposition=GateControlDisposition.TERMINAL_CLAIMABLE,
            blocker_owner=gate.blocker_owner,
        )
        if not honest_gate.accepted:
            # UNKNOWN is always claimable; this defensive fallback preserves
            # honesty if a future gate matrix cannot accept the prior maximum.
            honest_claim = PhaseClaim(
                phase=machine.current_phase,
                signal="done",
                claimed_outcome=PhaseOutcome.UNKNOWN,
                reason="finalizer could not safely refine beyond unknown",
                evidence_refs=refs,
            )
            honest_gate = validate_phase_claim(
                honest_claim,
                gate.validator_state,
                reason="agent_no_progress; conservative unknown close",
                evidence_refs=refs,
                code="agent_no_progress",
                validated_facts=gate.validated_facts,
                control_disposition=GateControlDisposition.TERMINAL_CLAIMABLE,
                blocker_owner=gate.blocker_owner,
            )
        self._seal_engine_gate(honest_claim, honest_gate)
        self._record_gate_facts(honest_claim.phase, honest_gate)
        record = machine.close_attempt(honest_gate)
        route = self.transition_policy.decide(
            record,
            state=state,
            budgets=self._repair_budgets(),
        )
        self._pending_repair_context = None
        self._apply_phase_decision(record, route)
        getattr(self, "agent_logger", logger).warning(
            f"Closed {claim.phase} at {honest_gate.validated_outcome.value} after "
            f"{decision.recurrence_count} no-op completion claims "
            f"({blocker.blocker_id})"
        )
        return True

    def _prepare_rejected_completion(
        self,
        execution: ToolExecution,
    ) -> _PreparedRejectedCompletion | None:
        """Bind a rejected phase claim to ownership and convergence state."""

        if execution.call.name != "phase":
            return None
        metadata = dict(execution.result.metadata or {})
        claim_data = metadata.get("phase_claim")
        gate_data = metadata.get("gate_result")
        if not isinstance(claim_data, Mapping) or not isinstance(gate_data, Mapping):
            return None
        try:
            claim = PhaseClaim.from_metadata(claim_data)
            gate = GateResult.from_metadata(gate_data, claim=claim)
        except (TypeError, ValueError, PermissionError):
            return None
        if gate.accepted or claim.signal not in {"done", "blocked"}:
            return None
        resolved_refs = self._gate_evidence_refs(gate)
        if resolved_refs != gate.evidence_refs:
            gate = replace(gate, evidence_refs=resolved_refs)

        assessment_fingerprints: tuple[str, ...] = ()
        effective_disposition = gate.control_disposition
        context: RepairContext | None = None
        context_status = "not_required"
        if gate.control_disposition is GateControlDisposition.REPAIR_REQUIRED:
            context, context_status = self._install_repair_context(claim, gate)
            if context is not None:
                assessment_fingerprints = (
                    canonical_sha256(
                        {
                            "assessment_id": context.trigger_assessment_id,
                            "typed_blocker": context.typed_blocker,
                            "owner": context.blocker_owner,
                        }
                    ),
                )
            else:
                effective_disposition = GateControlDisposition.HARNESS_RECOVERY_REQUIRED
                gate = replace(
                    gate,
                    control_disposition=effective_disposition,
                    blocker_owner=BlockerOwner.HARNESS,
                    reason=(
                        f"{gate.reason}; controller could not persist the judge-owned "
                        f"repair context ({context_status})"
                    ),
                    code=context_status,
                    suggestions=(),
                )

        event = self._completion_event(
            claim,
            gate,
            assessment_fingerprints=assessment_fingerprints,
            disposition=effective_disposition,
        )
        decision = self.loop_memory.observe_completion_claim(event)
        result_metadata = {
            **metadata,
            "gate_result": gate.to_metadata(),
            "completion_claim_decision": decision.to_metadata(),
            "effective_control_disposition": effective_disposition.value,
            "rejected_completion_control_owned": True,
        }
        if context is not None:
            result_metadata["repair_context_id"] = context.repair_context_id
            result_metadata["trigger_assessment_id"] = context.trigger_assessment_id
            result_metadata["repair_context"] = context.model_dump(mode="json")
        elif context_status != "not_required":
            result_metadata["repair_context_persistence"] = context_status
            result_metadata["blocker_owner"] = "harness"
        execution.result = execution.result.model_copy(update={"metadata": result_metadata})
        execution.observation_text = format_tool_result(execution.call.name, execution.result)
        # The observation above is the delivery. Everything downstream — the
        # recorded tool_result, the seal in _apply_rejected_completion_control —
        # states the word this line just handed the model.
        self._register_delivered_gate(claim, gate)
        return _PreparedRejectedCompletion(claim, gate, event, decision, context)

    def _mark_harness_control_failure(
        self,
        code: str,
        event: CompletionClaimEvent,
    ) -> None:
        """Make a lineage transport failure durable and stop future model turns."""

        normalized = str(code or "harness_control_failure").strip()
        state = getattr(self, "run_evidence_state", None)
        machine = getattr(self, "phase_machine", None)
        if state is not None and not state.sealed:
            state.record_blocker(
                failure_signature=(f"harness_control:{event.phase_attempt_id}:{normalized}"),
                category="harness_control",
                error_code=normalized,
                evidence_refs=event.evidence_refs,
                source_phase=getattr(machine, "current_phase", None),
                source_attempt_id=getattr(machine, "current_attempt_id", None),
            )
        self._pending_repair_context = None
        self._fatal_harness_control_failure = normalized

    def _apply_rejected_completion_control(
        self,
        prepared: _PreparedRejectedCompletion | None,
    ) -> bool:
        if prepared is None:
            return False
        claim, gate, event, decision = prepared
        context = prepared.context
        try:
            gate_event = self._emit_control_gate(claim, gate)
        except Exception as exc:
            logger.error(f"Gate decision failed closed: {exc}")
            self._mark_harness_control_failure("gate_decision_persist_failed", event)
            return True
        if context is not None:
            if gate_event is None:
                self._mark_harness_control_failure(
                    "repair_context_open_event_unavailable",
                    event,
                )
                return True
            machine = getattr(self, "phase_machine", None)
            attempt_id = str(getattr(machine, "current_attempt_id", "") or "").strip()
            try:
                guidance = self._repair_context_guidance(context)
                opened = self._emit_control_event(
                    "repair_context_opened",
                    {
                        "source_gate_sequence": gate_event.sequence,
                        "source_phase_attempt_id": attempt_id,
                        "context": context.model_dump(mode="json"),
                        "context_sha256": repair_context_sha256(context),
                    },
                )
            except Exception as exc:
                logger.error(f"Repair context open failed closed: {exc}")
                opened = None
            if opened is None:
                self._mark_harness_control_failure(
                    "repair_context_open_event_failed",
                    event,
                )
                return True
            # Activation and model guidance occur only after the exact event
            # is durable. A restart can now reconstruct the same authority.
            self._pending_repair_context = context
            self._add_system_guidance(guidance, priority=9)
        self._emit_completion_claim_decision(event, decision)
        if event.judge_disposition == GateControlDisposition.HARNESS_RECOVERY_REQUIRED.value:
            if event.blocker_id == "TEST_ATTEMPT_REQUIRED":
                # This disposition is the test-floor handoff, not a broken
                # control transport.  `_execute_tool_step` consumes the
                # rejected result immediately below and runs the exact
                # controller-owned build(action='test') attempt.  Treating it
                # as fatal here aborts one branch too early and makes that
                # promised recovery unreachable (live lp-dbcp-rates,
                # 2026-08-10).
                return False
            if event.blocker_id in _CONTAINED_CONTROL_PERSIST_CODES:
                return self._close_phase_for_control_persist_exhaustion(claim, gate, event)
            self._mark_harness_control_failure(event.blocker_id, event)
            return True
        if decision.decision == "agent_no_progress":
            return self._close_phase_for_agent_no_progress(claim, gate, decision)
        return False

    def _emit_control_phase_transition(
        self,
        decision: TransitionDecision,
        *,
        repair_request: RepairRequest | None = None,
    ) -> None:
        self._emit_control_event(
            "phase_transition",
            {
                "expected_kind": decision.route.kind,
                "expected_target": decision.route.target,
                "expected_reason_code": decision.reason_code,
                "repair_request": (
                    repair_request.to_metadata() if repair_request is not None else None
                ),
            },
        )

    def _emit_control_loop_decision(
        self,
        event: LoopEvent,
        decision: LoopDecision,
    ) -> None:
        event_payload = asdict(event)
        event_payload["relevant_state"] = {
            getattr(scope, "value", str(scope)): value
            for scope, value in event.relevant_state.items()
        }
        # LoopMemory's own chain count, verified against a re-run of production
        # LoopMemory by `sag.agent.replay`. Measured across 54 archived ledgers
        # it reads 1 in all 1,032 rows, and honestly so: 840 outcomes were not
        # recurrence candidates at all, 184 opened a chain nothing repeated, 4
        # were poll progress, 4 diversity advisories. Not one decision reached
        # the branch that increments — no chain in any archived run survived to
        # a second link. The wire is here; there is no second counter to build.
        event_payload["recurrence_count"] = decision.recurrence_count
        self._emit_control_event(
            "loop_decision",
            {
                "event": event_payload,
                "expected_decision": decision.decision,
                "expected_reason_code": decision.reason_code,
            },
        )

    # ---- sealed turns (spec §2.1) -------------------------------------

    @staticmethod
    def _turn_stamp() -> str:
        """One clock for turn records: UTC, ISO-8601, the sink's own shape."""
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _turn_byte_store(self) -> Optional[OutputStorageManager]:
        """The HOST store a sealed record's bytes live in — never the container's.

        Window components and delivered observations are bytes this process
        RENDERED. The container never had them, and sending them there costs a
        write, an index read and a full index rewrite per component, on a
        window that re-renders every turn: on a 20-turn synthetic run that was
        150 execs and 55 KB of the container's `full_outputs.jsonl` — 7.5 execs
        and 2.8 KB a turn — for bytes nothing in the container reads. It also
        put them in the one file `output_search` reads, where an observability
        feature has no business.

        They go beside the LEDGER instead, in `contexts/full_outputs.jsonl` of
        the session directory — the same store shape, the same `output_` ref
        namespace, and exactly where the trajectory's full tier already looks
        for the bytes a turn names (spec §3). One namespace, two files: the
        host's for what the engine rendered, the container's for what a tool
        produced, and a reader resolves a ref without knowing which is which.

        No sink means no session directory and no records at all, so there is
        nowhere to put bytes nobody will name.
        """
        store = getattr(self, "_turn_bytes_store", None)
        if store is not None:
            return store
        path = getattr(getattr(self, "control_event_sink", None), "path", None)
        if path is None:
            return None
        try:
            store = OutputStorageManager(Path(path).parent / "contexts")
        except Exception as exc:  # observability never ends a run
            logger.warning(f"turn-record byte store unavailable: {exc}")
            return None
        self._turn_bytes_store = store
        return store

    def _store_bytes_once(self, body: str, *, label: str) -> Optional[str]:
        """Persist one immutable byte string and hand back its ref, once per run.

        Window components repeat: every turn re-sends the system prompt and the
        whole history behind it. Storing them per turn would multiply the store
        by the window length; storing them once and referencing them is what
        makes a component-level digest affordable (spec §2.1). The identity is
        the CONTENT — its sha256 — so the system prompt is written once per run
        and an observation already written when it was delivered is referenced
        by every later window that carries it, never written again. The store
        grows by the run's net-new bytes and by nothing else.

        Returns None when the bytes could not be stored, or when the ref that
        came back is already held for DIFFERENT bytes — an unresolvable or
        ambiguous ref in a window digest is worse than an absent one, because
        it reconstructs a window the model never saw.
        """
        storage = self._turn_byte_store()
        if storage is None:
            return None
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        held = getattr(self, "_stored_byte_refs", None)
        if held is None:
            held = {}
            self._stored_byte_refs = held
        if digest in held:
            return held[digest]
        try:
            ref = storage.store_output(
                task_id=OBSERVABILITY_TASK_ID,
                tool_name=label,
                output=body,
                timestamp=self._turn_stamp(),
                metadata={"sha256": digest, "kind": label},
            )
        except Exception as exc:
            logger.debug(f"turn-record bytes were not stored ({label}): {exc}")
            return None
        if not ref or ref in held.values():
            return None
        held[digest] = ref
        return ref

    def _seal_window_digest(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
    ) -> WindowDigestPayload:
        """State [A] as components: which prompt spoke, and every message after it.

        One ref per rendered message, in render order, so resolving the refs and
        concatenating them in list order reproduces the exact array that went to
        the provider. Identical messages share one ref and still occupy their two
        positions — the order is the record.

        A component that will not store makes the WHOLE list a lie, so the
        digest keeps its prompt hash and drops the refs rather than claiming a
        window with a hole in it.

        A window longer than the record may name is CUT, never refused. The
        bound used to be enforced by raising, and this runs in the loop body
        where the engine's handler turns an exception into an aborted run — an
        observability record ending the run whose window is most worth reading.
        The newest components that fit are kept, in render order, and the first
        slot names how many older ones are missing, so a short list never
        passes for a whole window.
        """
        prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        kept = messages
        marker: List[str] = []
        if len(messages) > WINDOW_DIGEST_MAX_COMPONENTS:
            kept = messages[-(WINDOW_DIGEST_MAX_COMPONENTS - 1) :]
            dropped = len(messages) - len(kept)
            marker = [WINDOW_TRUNCATION_REF.format(dropped=dropped)]
            logger.warning(
                f"window digest truncated: {dropped} of {len(messages)} components are not "
                f"named by this record (limit {WINDOW_DIGEST_MAX_COMPONENTS})"
            )
        refs: List[str] = []
        for message in kept:
            ref = self._store_bytes_once(canonical_json(message), label="window_component")
            if ref is None:
                logger.debug("window digest sealed without components: a component did not store")
                refs = []
                marker = []
                break
            refs.append(ref)
        try:
            return WindowDigestPayload(
                system_prompt_sha256=prompt_sha256,
                component_refs=tuple(marker + refs),
            )
        except Exception as exc:  # observability never ends a run
            logger.warning(f"window digest sealed without components: {exc}")
            return WindowDigestPayload(system_prompt_sha256=prompt_sha256)

    def _window_for(self, actor: str) -> Optional[WindowDigestPayload]:
        """The window a turn answered from — and for the controller, none.

        A forced action and an engine close are answers to POLICY, not to a
        rendered array: the harness read no window, and lending it the model's
        components would state that it did. The controller's row still carries
        the run's prompt identity, so a reader can place it, and the window in
        force is one turn away — it is the model turn beside it.

        Before the first render there is no prompt identity either, and a turn
        sealed there claims NO window. Hashing the empty string was a fact
        nobody observed: sha256("") resolves to nothing, reads like any other
        prompt hash, and is the same 64 hex characters in every run that ever
        sealed one.
        """
        digest = getattr(self, "_window_digest", None)
        if digest is None:
            return None
        if actor == "controller":
            return WindowDigestPayload(system_prompt_sha256=digest.system_prompt_sha256)
        return digest

    def _turn_bill(self, iteration: Optional[int]) -> tuple[Optional[int], Optional[int]]:
        """The response's own row, joined in process — never re-read from the CSV.

        `token_usage.csv` is exported when the loop EXITS, so a record that
        waited for the file would seal blank for the entire run. The row lives
        in the tracker the moment the response lands, keyed by the iteration
        the loop set on it.

        One response, one bill: a response that asks for two tools opens two
        turns, and the row bills the FIRST of them while the siblings ride
        along. Copying it onto both would invent spend — the same rule the
        trajectory builder applies to the exported rows, so both feeds bill the
        same turn.
        """
        if iteration is None:
            return None, None
        claimed = getattr(self, "_turn_bills_claimed", None)
        if claimed is None:
            claimed = set()
            self._turn_bills_claimed = claimed
        if iteration in claimed:
            return None, None
        records = getattr(getattr(self, "token_tracker", None), "token_records", None) or ()
        row = next(
            (
                record
                for record in records
                if record.get("type") == "executor" and record.get("iteration") == iteration
            ),
            None,
        )
        if row is None:
            return None, None
        claimed.add(iteration)
        return row.get("prompt_tokens"), row.get("completion_tokens")

    @staticmethod
    def _billed(
        payload: TurnRecordPayload,
        tokens_in: Optional[int],
        tokens_out: Optional[int],
    ) -> TurnRecordPayload:
        """Join the bill onto the record — through the record's own constraints.

        The bill is known only after the payload is built, and `model_copy`
        does not validate: `update=` writes what it is handed straight past
        every `Field` the class declares, so a negative or non-integer token
        count sealed as fact in the one layer whose whole job is to be
        believable.

        Re-validating is the fix, and what it costs when it fails is the BILL,
        never the turn. A record that does not appear is a hole in the sealed
        sequence (§2.2 rule 5); a record with no tokens on it is a record.
        """
        try:
            return TurnRecordPayload.model_validate(
                {
                    **payload.model_dump(mode="json"),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                }
            )
        except Exception as exc:  # observability never ends a run
            logger.warning(
                f"turn record {payload.turn_id} sealed without its bill "
                f"({tokens_in}/{tokens_out}): {exc}"
            )
            return payload

    def _seal_turn_record(
        self,
        *,
        actor: str,
        t0: str,
        t1: str,
        envelope_ref: Optional[str] = None,
        observation_ref: Optional[str] = None,
        gate_decision_id: Optional[str] = None,
        iteration: Optional[int] = None,
        window_digest: Optional[WindowDigestPayload] = None,
    ) -> None:
        """Seal one turn through the same publication path as every other event.

        Never raises. A turn record is a statement ABOUT a run, and a run that
        died because its observability record would not validate would be the
        one failure this layer must never cause. A record that does not appear
        becomes a hole the trajectory states as a warning (spec §3), which is
        exactly what a hole is supposed to look like.
        """
        sink = getattr(self, "control_event_sink", None)
        if sink is None:
            return
        machine = getattr(self, "phase_machine", None)
        digest = window_digest or self._window_for(actor)
        # The id is spent HERE, on the turn that is being sealed, and never
        # given back. Advancing it after a successful emit made the hole check
        # of spec §2.2 rule 5 unfalsifiable: a record that did not persist
        # handed its id to the next turn, so the sequence closed over the loss
        # and the reducer's `conservation_violation` became dead code for every
        # engine-produced ledger. A run that lost its whole turn stream derived
        # a clean, silent, one-turn trajectory. A lost record is a hole, and a
        # hole is what the fence is written to find.
        turn_id = int(getattr(self, "_sealed_turn_count", 0)) + 1
        self._sealed_turn_count = turn_id
        try:
            payload = TurnRecordPayload(
                turn_id=turn_id,
                phase=(getattr(machine, "current_phase", "") or "") or "unknown",
                iteration=iteration,
                actor=actor,
                window_digest=digest,
                envelope_ref=envelope_ref or None,
                observation_ref=observation_ref or None,
                gate_decision_id=gate_decision_id or None,
                tokens_in=None,
                tokens_out=None,
                t0=t0,
                t1=t1,
            )
            if actor == "model":
                payload = self._billed(payload, *self._turn_bill(iteration))
            self._emit_control_event("turn_record", payload.model_dump(mode="json"))
        except Exception as exc:  # observability never ends a run
            logger.warning(f"turn record {turn_id} was not sealed: {exc}")

    def _delivered_observation_ref(self, step: Any) -> Optional[str]:
        """[C] as the model read it, not as the tool returned it.

        The delivered text is what the observation step carries: the tool's
        own text plus whatever the engine appended to it (a settlement notice,
        a carried gate word). A ref to the tool's raw output would name bytes
        the model was never shown, and the whole point of a sealed turn is that
        [A] and [C] are the model's copies.
        """
        content = getattr(step, "content", None)
        if not isinstance(content, str) or not content:
            return None
        return self._store_bytes_once(content, label="delivered_observation")

    def _claim_turn_gate(self, before: Optional[str]) -> Optional[str]:
        """The gate this turn sealed, if it sealed one and nobody else claimed it.

        `_last_sealed_decision_id` is the word the record now stands behind. A
        turn carried a gate when that word changed while the turn was running —
        unless a controller turn (an engine close) already sealed it, in which
        case the word belongs to the controller's record, not the model's.
        """
        current = getattr(self, "_last_sealed_decision_id", None)
        if not current or current == before:
            return None
        claimed = getattr(self, "_turn_gate_claims", None) or set()
        return None if current in claimed else current

    def _canonicalize_tool_action(
        self,
        tool_name: str,
        params: Mapping[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        """Resolve legacy tool aliases and normalize parameters to the schema.

        The only surviving piece of the deleted plan-lock machinery: harness-
        authored calls (the forced test attempt) must reach the orchestrator on
        exactly the representation a model-authored call would."""
        raw_params = dict(params or {})
        tools = getattr(self, "tools", None)
        if not isinstance(tools, dict) or not tools:
            return tool_name, raw_params

        from .tool_parameters import ToolParameterNormalizer

        normalizer = ToolParameterNormalizer(
            tools=tools,
            successful_states=getattr(self, "successful_states", {}),
            repository_url=getattr(self, "repository_url", None),
            repository_ref=getattr(self, "repository_ref", None),
            logger=logger,
        )
        normalized_tool, aliased_params = normalizer.resolve_legacy_alias(
            tool_name,
            raw_params,
        )
        return normalized_tool, normalizer.validate_and_fix(normalized_tool, aliased_params)

    def _get_tool_orchestrator(self) -> ToolOrchestrator:
        """Build the orchestration adapter for delegated tool execution."""
        return ToolOrchestrator(
            tools=self.tools,
            context_manager=self.context_manager,
            recent_tool_executions=self.recent_tool_executions,
            successful_states=self.successful_states,
            repository_url=self.repository_url,
            repository_ref=self.repository_ref,
            track_tool_execution=self._track_tool_execution,
            update_successful_states=self._update_successful_states,
            add_system_guidance=self._add_system_guidance,
            get_timestamp=self._get_timestamp,
            event_sink=self._handle_tool_lifecycle_event,
            before_tool_execute=self._prepare_control_action,
            output_storage=self.output_storage,
            logger=logger,
        )

    def _handle_tool_lifecycle_event(self, event: ToolLifecycleEvent) -> None:
        """Map orchestration lifecycle events into typed UI events."""
        lifecycle_event_map = {
            "tool_start": EventType.TOOL_START,
            "tool_parameters_fixed": EventType.TOOL_PARAMETERS_FIXED,
            "tool_result": EventType.TOOL_RESULT,
            "tool_error": EventType.TOOL_ERROR,
        }
        event_type = lifecycle_event_map.get(event.event_type)
        if event_type is None:
            return None

        metadata = dict(event.metadata)
        metadata.setdefault("tool_name", event.call.name)
        metadata.setdefault("tool_params", event.call.validated_params or event.call.raw_params)
        metadata.setdefault("tool_message", event.message)

        self.emit_event(
            UIEvent(
                event_type,
                event.message,
                level=event.level,
                metadata=metadata,
            )
        )

    def _build_tool_call_from_step(self, step: ReActStep) -> ToolCall:
        """Translate a parsed ReAct action step into an orchestration tool call."""
        raw_params = dict(step.tool_params or {})
        repair_submission = raw_params.pop("repair_intent", None)
        return ToolCall(
            name=step.tool_name or "",
            raw_params=raw_params,
            raw_action_text=step.content,
            source_step_index=self.current_iteration,
            model_used=step.model_used,
            repair_intent_submission=repair_submission,
        )

    def _execute_tool_call(self, call: ToolCall) -> ToolExecution:
        """Execute one call and audit construction-time persistence failure."""
        try:
            return self._get_tool_orchestrator().execute(call)
        except OutputPersistenceError as exc:
            logger.error(f"Failed to construct durable result for {call.name}: {exc}")
            state = getattr(self, "run_evidence_state", None)
            if (
                call.name == "report"
                and state is not None
                and state.sealed
                and self._report_execution_allowed()
            ):
                return self._failed_report_persistence_execution(call, exc)
            if (
                state is not None
                and not state.sealed
                and call.name not in self._NON_EVIDENCE_TOOLS
                and call.name != "report"
            ):
                for actual in exc.actual_executions:
                    self._record_tool_execution(
                        actual.tool_name,
                        actual.params,
                        actual.result,
                        execution_id=actual.execution_id,
                    )
                tool_name = exc.tool_name or call.name
                params = exc.params or call.validated_params or call.raw_params
                scope = self._tool_evidence_scope(tool_name, params)
                action = self._tool_evidence_action(params)
                state.record_attempt(
                    action=f"{tool_name}:{action}",
                    relevant_scopes=[scope],
                    outcome=(
                        exc.draft.operation_outcome
                        if exc.draft is not None
                        else OperationOutcome.FAILED
                    ),
                    evidence_refs=(
                        self._dedupe_strings([*exc.draft.evidence_refs, *exc.draft.refs])
                        if exc.draft is not None
                        else []
                    ),
                )
                if exc.draft is not None:
                    machine = getattr(self, "phase_machine", None)
                    state.ingest_unpersisted_result(
                        scope,
                        tool_name,
                        exc.draft,
                        provenance=(f"tool:{tool_name}:{action}:output-persistence-failed"),
                        roles=self._tool_evidence_roles(tool_name, params, exc.draft),
                        execution_id=exc.execution_id or exc.draft.execution_id,
                        params=params,
                        source_phase=getattr(machine, "current_phase", None),
                        source_attempt_id=getattr(machine, "current_attempt_id", None),
                    )
                state.record_conflict("output_storage_failed")
            raise

    def _record_execution_bundle(
        self,
        execution: ToolExecution,
        call: ToolCall,
    ) -> tuple[ToolResult, str, list[ActualToolExecution]]:
        """Record backend leaves once and return their control-event projection."""
        if execution.actual_executions:
            result = execution.result
            recorded_executions: list[ActualToolExecution] = []
            control_execution_id: str | None = None
            for actual in execution.actual_executions:
                recorded = self._record_tool_execution(
                    actual.tool_name,
                    actual.params,
                    actual.result,
                    attempted_execution=True,
                    execution_id=actual.execution_id,
                )
                recorded_executions.append(
                    ActualToolExecution(
                        tool_name=actual.tool_name,
                        params=actual.params,
                        result=recorded,
                        execution_id=actual.execution_id,
                    )
                )
                if actual.result is execution.result:
                    result = recorded
                    control_execution_id = actual.execution_id
            execution.actual_executions = recorded_executions
            if result is not execution.result:
                execution.result = result
                execution.observation_text = format_tool_result(call.name, result)
            return (
                result,
                control_execution_id or new_execution_id(),
                recorded_executions,
            )

        execution_id = new_execution_id()
        result = self._record_tool_execution(
            call.name,
            call.validated_params or call.raw_params,
            execution.result,
            attempted_execution=execution.attempted_execution,
            execution_id=execution_id,
        )
        if result is not execution.result:
            execution.result = result
            execution.observation_text = format_tool_result(call.name, result)
        return result, execution_id, []

    def _loop_event_for_execution(self, execution: ToolExecution) -> LoopEvent:
        result = execution.result
        params = execution.executed_params or execution.validated_params or execution.raw_params
        state = getattr(self, "run_evidence_state", None)
        state_vector = (
            state.state_vector(tuple(StateScope))
            if state is not None
            else {scope.value: 0 for scope in StateScope}
        )
        machine = getattr(self, "phase_machine", None)
        metadata = result.metadata or {}
        output_cursor = next(
            (
                str(metadata[key])
                for key in ("output_cursor", "cursor", "bytes_read", "poll_sequence")
                if metadata.get(key) not in (None, "")
            ),
            "",
        )
        evidence_ref = next(
            (str(ref) for ref in [result.output_ref, *result.evidence_refs, *result.refs] if ref),
            "",
        )
        return LoopEvent(
            tool_name=execution.call.name,
            args=params or {},
            operation_outcome=result.operation_outcome.value,
            error_code=result.error_code or "",
            failure_signature=result.failure_signature or "",
            relevant_state=state_vector,
            phase=getattr(machine, "current_phase", "") or "",
            attempt_id=getattr(machine, "current_attempt_id", "") or "",
            iteration=getattr(self, "current_iteration", 0),
            evidence_ref=evidence_ref,
            invocation_status=result.invocation_status.value,
            job_id=str(result.poll_ref or metadata.get("job_id") or ""),
            output_cursor=output_cursor,
            # The harness's phase-entry consult is READ by the ladder like every
            # other call (spec §2.2 rule 1) and COUNTED by none of it: the
            # controller asked a reviewer a question between two of the model's
            # actions, and a question the model never asked may not disarm the
            # break the model's own repetition armed.
            outside_ladder=bool((execution.metadata or {}).get("advisor_entry_consult")),
        )

    def _ensure_project_facts(self) -> str:
        """Run the framework survey guarantee: 'created'|'present'|'failed'."""
        orchestrator = getattr(
            getattr(self, "physical_validator", None), "docker_orchestrator", None
        )
        if orchestrator is None:
            return "failed"
        try:
            from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

            analyzer = ProjectAnalyzerTool(orchestrator, self.context_manager)
            return analyzer.ensure_facts("/workspace")
        except Exception as exc:
            logger.debug(f"framework survey unavailable: {exc}")
            return "failed"

    def _recover_analysis_facts_once(self) -> Optional[str]:
        """Spend the run's sole controller retry of the survey guarantee."""

        if getattr(self, "_analysis_facts_recovery_attempted", False):
            return None
        self._analysis_facts_recovery_attempted = True
        status = str(self._ensure_project_facts() or "failed").strip().lower()
        return status if status in {"created", "present", "failed"} else "failed"

    def _native_smoke_guidance(self, phase: str) -> Optional[str]:
        """The bounded-smoke steer for the test phase, or None.

        Eligible only for python-system repos flagged has_native_build whose
        build phase ended without a success outcome. WHICH text renders is then
        decided by the native artifact PROBE, never by that outcome (P0-D,
        ground-truth review 2026-07-26: five .so files existed while every
        layer said the native core was not built, because a packaging-integrity
        warning had capped the phase at partial):

          absent  -> the "not built" steer, now a true statement;
          present -> the facts text (artifacts + why the phase closed as it
                     did), which never claims the core is missing;
          unknown -> the same honest text WITHOUT a count claim; an unverified
                     absence is not a fact.
        """
        if phase != "test":
            return None
        if not is_python_build_system(self._detected_build_system()):
            return None
        rec = self._build_recommendation()
        if not rec.get("has_native_build") or not self._build_phase_lacked_success():
            return None
        fact = self._native_artifact_fact(rec=rec, phase=phase)
        if fact["status"] == "absent":
            return NATIVE_NOT_BUILT_TEST_GUIDANCE
        outcome = self._build_phase_outcome() or "unknown"
        if fact["status"] == "present":
            return NATIVE_ARTIFACTS_PRESENT_TEST_GUIDANCE.format(
                artifacts=self._native_artifact_count_text(fact),
                outcome=outcome,
            )
        return NATIVE_ARTIFACTS_UNKNOWN_TEST_GUIDANCE.format(
            root=fact["root"] or "the surveyed native root",
            outcome=outcome,
        )

    # One bounded probe per phase: enough paths to state a count, few enough to
    # never flood the container output.
    _NATIVE_ARTIFACT_PROBE_LIMIT = 20

    def _native_artifact_fact(
        self, *, rec: Optional[Dict] = None, phase: Optional[str] = None
    ) -> Dict[str, Any]:
        """Whether project-owned native shared objects exist, as one fact dict:
        ``{"status": "present"|"absent"|"unknown", "count": int|None, "root": str}``.

        Probed ONCE per phase with a single bounded command and cached on the
        engine, so the advisor digest reads the same fact the steer rendered
        instead of paying for a second probe.
        """
        recommendation = self._build_recommendation() if rec is None else rec
        root = str(recommendation.get("build_root") or recommendation.get("test_root") or "")
        phase_key = str(
            phase
            if phase is not None
            else (getattr(getattr(self, "phase_machine", None), "current_phase", "") or "")
        )
        cache = getattr(self, "_native_artifact_probe_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._native_artifact_probe_cache = cache
        cached = cache.get((phase_key, root))
        if cached is None:
            cached = self._probe_native_artifacts(root)
            cache[(phase_key, root)] = cached
        return cached

    def _probe_native_artifacts(self, root: str) -> Dict[str, Any]:
        """The single bounded find behind `_native_artifact_fact`.

        The `test -d` prefix is what makes an "absent" verdict a real scan: a
        pipeline's exit status is head's, so a find over a missing directory
        would otherwise look exactly like a directory with no artifacts.
        """
        unknown = {"status": "unknown", "count": None, "root": root}
        orchestrator = getattr(
            getattr(self, "physical_validator", None), "docker_orchestrator", None
        )
        if not root or orchestrator is None:
            return unknown
        quoted = shlex.quote(root)
        command = (
            f"test -d {quoted} && find {quoted} -name '*.so' -type f "
            f"-not -path '*/.git/*' | head -{self._NATIVE_ARTIFACT_PROBE_LIMIT}"
        )
        try:
            result = orchestrator.execute_command(command) or {}
        except Exception as exc:
            logger.debug(f"native artifact probe unavailable: {exc}")
            return unknown
        if not result.get("success"):
            return unknown
        paths = list(
            dict.fromkeys(
                line.strip()
                for line in str(result.get("output") or "").splitlines()
                if line.strip()
            )
        )
        return {
            "status": "present" if paths else "absent",
            "count": len(paths),
            "root": root,
        }

    @classmethod
    def _native_artifact_count_text(cls, fact: Dict[str, Any]) -> str:
        """The probe's count as a claim it can actually support — the find is
        capped, so a full page of hits is "at least N", never exactly N."""
        count = int(fact.get("count") or 0)
        if count >= cls._NATIVE_ARTIFACT_PROBE_LIMIT:
            return f"at least {count} shared objects"
        return f"{count} shared object{'' if count == 1 else 's'}"

    def _build_phase_outcome(self) -> Optional[str]:
        """The build phase's recorded outcome (validated first, the legacy
        outcome as fallback), or None when there is no build record yet."""
        machine = getattr(self, "phase_machine", None)
        for record in getattr(machine, "records", ()) or ():
            if str(getattr(record, "phase", "")) != "build":
                continue
            validated = getattr(record, "validated_outcome", None)
            outcome = getattr(validated, "value", validated) or getattr(
                getattr(record, "outcome", None), "value", getattr(record, "outcome", None)
            )
            return str(outcome or "unknown")
        return None

    def _build_phase_lacked_success(self) -> bool:
        """True when the build phase's recorded outcome is anything but success.
        No build record yet -> False."""
        outcome = self._build_phase_outcome()
        return outcome is not None and outcome != "success"

    def _untried_island_coordinates(self, *, limit: int = 4) -> str:
        """Surveyed build coordinates with no persisted runner receipt yet.

        This is corrective-loop navigation, not a recommendation: the shared
        manifest supplies only ``system``/``root`` coordinates, and current run
        receipts establish which coordinates were tried.  Survey ``goal`` and
        action ordering never enter the model-visible line.
        """

        try:
            orchestrator = getattr(
                getattr(self, "physical_validator", None), "docker_orchestrator", None
            )
            if orchestrator is None:
                return ""
            from sag.tools.internal.build_preflight import read_live_build_requirements

            live_manifest = read_live_build_requirements(orchestrator)
            if (
                not live_manifest.complete
                or live_manifest.conflict is not None
                or live_manifest.payload is None
            ):
                return ""
            manifest = live_manifest.payload
            islands = manifest.get("build_islands") or []
            if len(islands) < 2:
                return ""

            state = getattr(self, "run_evidence_state", None)
            scope = resolve_current_build_receipt_scope(
                orchestrator,
                run_id=state.run_id if isinstance(state, RunEvidenceState) else "",
                manifest=manifest,
            )
            if not isinstance(state, RunEvidenceState):
                attempted_roots = ()
            else:
                from .evidence_assessments import read_receipt

                attempted_roots = build_attempt_directories(
                    state,
                    receipt_loader=lambda receipt_id: read_receipt(
                        orchestrator.execute_command,
                        receipt_id,
                    ),
                    scope=scope,
                )

            untried = []
            for island in islands:
                if not isinstance(island, Mapping):
                    continue
                root = str(island.get("root") or "").rstrip("/")
                if not root:
                    continue
                if any(
                    attempted == root or attempted.startswith(root + "/")
                    for attempted in attempted_roots
                ):
                    continue
                untried.append((str(island.get("system") or "unknown"), root))
            if not untried:
                return ""

            def one_line(value: str, cap: int = 240) -> str:
                return " ".join(value.split())[:cap]

            items = "; ".join(
                f"{one_line(system)} at {one_line(root)}"
                for system, root in untried[: max(1, int(limit))]
            )
            binding = (
                f" Current receipt binding: unknown ({scope.status})."
                if not scope.available
                else ""
            )
            return f"{binding} Untried surveyed build coordinates: {items}."
        except Exception:
            return ""

    def _loop_guidance(self, decision: LoopDecision) -> str:
        attempts = ", ".join(decision.prior_attempt_ids) or "current run"
        scopes = ", ".join(decision.missing_progress_scopes) or "declared scopes"
        untried = self._untried_island_coordinates()
        outcome_key = getattr(decision, "outcome_key", None)
        error_code = str(getattr(outcome_key, "error_code", "") or "").lower()
        if error_code == "pytest_args_rejected":
            return (
                "PYTEST SELECTOR REJECTED: changing one invented path to another is "
                "the same pre-execution failure. Do not guess another test path. "
                "Call build(action='test') without args; the Python tool will use "
                "surveyed coordinates or refuse an unsafe sweep."
            )
        if decision.decision == "diversity_advisory":
            return (
                "ACTION DIVERSITY ADVISORY: many distinct actions have been tried in this "
                "phase attempt. Consolidate evidence before expanding the search further."
            )
        if decision.decision == "force_break":
            return (
                "LOOP FORCE-BREAK ARMED: the identical action/outcome recurred four times "
                f"with no progress in {scopes}. Prior attempts: {attempts}. Reason once, "
                "then choose a materially different action; an immediate identical repeat "
                f"will close this phase as failed.{untried}"
            )
        return (
            "RECURRENCE WITHOUT PROGRESS: the same action and outcome recurred while "
            f"{scopes} stayed unchanged. Prior attempts: {attempts}. Use the failure "
            f"reference and choose a different hypothesis before retrying.{untried}"
        )

    def _record_loop_blocker(self, decision: LoopDecision) -> None:
        state = getattr(self, "run_evidence_state", None)
        machine = getattr(self, "phase_machine", None)
        if state is None or state.sealed:
            return
        state.record_blocker(
            category="loop",
            error_code="LOOP_WITHOUT_PROGRESS",
            failure_signature=decision.blocker_signature,
            evidence_ref=decision.failure_ref or None,
            source_phase=getattr(machine, "current_phase", None),
            source_attempt_id=getattr(machine, "current_attempt_id", None),
        )

    def _resolve_progressed_loop_blockers(self, decision: LoopDecision) -> None:
        state = getattr(self, "run_evidence_state", None)
        if state is None or state.sealed:
            return
        signatures = set(decision.resolved_blocker_signatures)
        for blocker in state.blockers:
            if blocker.status == "active" and blocker.failure_signature in signatures:
                state.resolve_blocker(
                    blocker.blocker_id,
                    resolution="relevant state epoch progressed",
                    evidence_ref=decision.failure_ref or None,
                )

    def _apply_tool_execution_loop_effects(
        self,
        execution: ToolExecution,
    ) -> LoopDecision | None:
        """Consume orchestrator metadata, then consult engine-owned loop memory."""
        metadata = execution.metadata or {}

        # Ahead of the loop-memory consult and independent of it: a tool that
        # bounded its own recurrence states that fact in typed metadata, and
        # the engine is what writes it into the ledger — including the release
        # no tool can report, because it happens in another tool's runner.
        self._relay_material_recurrence_marker(execution)
        self._release_material_recurrence_on_build(execution)
        # EVERY dispatched call is read here, claim tools included (spec §2.2
        # rule 1): camel-quarkus recorded ten phase calls with no `loop_decision`
        # at all, and their turn numbers had to be recovered by matching
        # parameters. LoopMemory itself decides which tools its recurrence
        # ladder COUNTS (`TOOLS_OUTSIDE_THE_LADDER`); the engine's job is to ask
        # about all of them and record the answer.
        memory = getattr(self, "loop_memory", None)
        if memory is None:
            return None
        loop_event = self._loop_event_for_execution(execution)
        decision = memory.observe(loop_event)
        self._emit_control_loop_decision(loop_event, decision)
        execution.metadata["loop_decision"] = decision.to_metadata()
        self._resolve_progressed_loop_blockers(decision)
        if decision.decision in {"guide", "force_break", "diversity_advisory"}:
            priority = 9 if decision.decision == "force_break" else 7
            if decision.decision == "diversity_advisory":
                priority = 4
            self._add_system_guidance(self._loop_guidance(decision), priority=priority)
        if decision.decision == "force_break":
            self._record_loop_blocker(decision)
        # `decision.request_thinking` is LoopMemory's redirect signal. It has no
        # consumer in the single-executor loop; Plan 3 wires it to the advisor.
        return decision

    # The third rung of the recurrence ladder (#42,
    # docs/superpowers/specs/2026-08-13-material-recurrence-bound-design.md).
    # A tool that has bounded its own recurrence reports the structured fact;
    # only the engine writes run evidence, so only the engine renders and
    # records the blocker — and retires it when the wall is released.
    _MATERIAL_RECURRENCE_MARKER = "material_recurrence_bound"
    _MATERIAL_RECURRENCE_RELEASE = "material_recurrence_released"

    @staticmethod
    def _material_recurrence_tool_prefix(tool: Any) -> str:
        """Every wall stated about one tool, whichever refusal code stated it."""
        return f"material_recurrence_bound:{str(tool or '').strip() or 'unknown'}:"

    @classmethod
    def _material_recurrence_identity(cls, marker: Mapping[str, Any]) -> str:
        """The stable key of one bounded recurrence, inside the signature.

        `(tool, error_code)` exactly as the bound counts it — never the paths
        or the count, which keep growing while the same wall stands.
        """
        error_code = str(marker.get("error_code") or "").strip() or "UNKNOWN"
        return f"{cls._material_recurrence_tool_prefix(marker.get('tool'))}{error_code}"

    @classmethod
    def _material_recurrence_release_prefixes(
        cls,
        released: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Which walls a reported release retires.

        The codes it names, or — when it names none — every wall stated about
        that tool, since a tool that now registers has no standing
        registration wall of any code left to state.
        """
        prefix = cls._material_recurrence_tool_prefix(released.get("tool"))
        codes = [
            str(code).strip()
            for code in (
                *(released.get("error_codes") or ()),
                released.get("error_code") or "",
            )
            if str(code).strip()
        ]
        return tuple(f"{prefix}{code}" for code in dict.fromkeys(codes)) or (prefix,)

    def _resolve_material_recurrence_blockers(
        self,
        prefixes: Sequence[str],
        *,
        reason: str,
        evidence_ref: str | None = None,
    ) -> None:
        """Retire the stated walls the harness has just seen walked around."""
        state = getattr(self, "run_evidence_state", None)
        if state is None or state.sealed or not prefixes:
            return
        for blocker in state.blockers:
            if blocker.status == "active" and any(
                blocker.failure_signature.startswith(prefix) for prefix in prefixes
            ):
                state.resolve_blocker(
                    blocker.blocker_id,
                    resolution=reason,
                    evidence_ref=evidence_ref or None,
                )

    def _material_recurrence_signature(self, marker: Mapping[str, Any]) -> str:
        """Render the model-facing statement from the structured fact.

        `failure_signature` is the only free-text field on a `BlockerRecord`
        and the one the cumulative handoff prints, so the tool, the paths
        already refused and the moves that remain are stated in it.
        """
        paths = [str(path) for path in marker.get("refused_executables") or () if str(path)]
        moves = [str(move) for move in marker.get("remaining_moves") or () if str(move)]
        count = marker.get("refusal_count")
        return (
            f"{self._material_recurrence_identity(marker)}: "
            f"{count if count is not None else len(paths)} refusals for "
            f"{', '.join(paths) or 'no named path'}; "
            f"remaining moves: {' | '.join(moves) or 'none the harness can see'}"
        )

    def _relay_material_recurrence_marker(self, execution: ToolExecution) -> None:
        """Write the tool's bounded-recurrence fact into engine-owned state."""
        metadata = getattr(execution.result, "metadata", None) or {}
        state = getattr(self, "run_evidence_state", None)
        if state is None or state.sealed:
            return
        machine = getattr(self, "phase_machine", None)

        released = metadata.get(self._MATERIAL_RECURRENCE_RELEASE)
        if isinstance(released, Mapping):
            self._resolve_material_recurrence_blockers(
                self._material_recurrence_release_prefixes(released),
                reason=str(released.get("reason") or "").strip()
                or "the bounded registration succeeded",
                evidence_ref=execution.result.output_ref or None,
            )

        marker = metadata.get(self._MATERIAL_RECURRENCE_MARKER)
        if not isinstance(marker, Mapping):
            return
        identity = self._material_recurrence_identity(marker)
        if any(
            blocker.status == "active" and blocker.failure_signature.startswith(identity)
            for blocker in state.blockers
        ):
            # The wall was already stated. A further first look at a new path
            # can refuse again past the bound; it is the same wall.
            return
        refs = [str(ref) for ref in marker.get("evidence_refs") or () if str(ref).strip()]
        if execution.result.output_ref:
            refs.append(execution.result.output_ref)
        state.record_blocker(
            failure_signature=self._material_recurrence_signature(marker),
            category="loop",
            error_code="MATERIAL_RECURRENCE_BOUND",
            evidence_refs=refs,
            source_phase=getattr(machine, "current_phase", None),
            source_attempt_id=getattr(machine, "current_attempt_id", None),
        )

    def _release_material_recurrence_on_build(self, execution: ToolExecution) -> None:
        """Retire a registration wall the model walked around by building.

        The wall is a statement about REGISTERING a runtime, and the refusal's
        own leading move now steers away from registering at all ("the build
        tool already uses the wrapper — dispatch the build instead", c0339ae).
        A model that takes that advice never produces a registration success,
        so the tool never reports a release and the blocker would be restated
        under ACTIVE BLOCKERS at every phase entry for the rest of the run
        (#46 defect 3). The runner that just ran is the other proof, and the
        engine — the only writer of run evidence — is where it is read.
        """
        if execution.call.name not in self._BUILD_EVIDENCE_TOOLS:
            return
        result = execution.result
        if not result.succeeded:
            # `resolution` is a statement in the ledger, and only a build that
            # succeeded supports the one made below.
            return
        metadata = result.metadata or {}
        # `receipt_id` is present exactly when THIS dispatch's invocation
        # receipt was persisted (maven_tool.py:1088, gradle_tool.py:785): a
        # result whose runner never dispatched carries neither key, and a
        # failed receipt write reports `receipt_persisted: false` instead of an
        # id. The persisted receipt is the terminal fact, so it is the signal.
        if not str(metadata.get("receipt_id") or "").strip():
            return
        system = (
            str(
                (result.facts or {}).get("system")
                or metadata.get("system")
                or (execution.call.name if execution.call.name != "build" else "")
            )
            .strip()
            .lower()
        )
        if not system:
            return
        # Scoped to the tool whose runner ran: a Gradle build says nothing
        # about a Maven registration wall.
        self._resolve_material_recurrence_blockers(
            (self._material_recurrence_tool_prefix(system),),
            reason=f"a {system} build succeeded via its own runner",
            evidence_ref=result.output_ref or None,
        )

    def _observe_action_intent_progress(self, execution: ToolExecution) -> bool:
        """Advance completion epochs only for a real public experiment."""

        memory = getattr(self, "loop_memory", None)
        if memory is None:
            return False
        intent = execution.call.action_intent
        if intent is None:
            return False
        result = execution.result
        metadata = dict(result.metadata or {})
        contract_id = str(metadata.get("contract_id") or "").strip()
        if contract_id:
            self._last_invocation_contract_id = contract_id
        excluded = {"advisor", "manage_context", "phase", "report"}
        if execution.call.name in excluded:
            return False
        passed_freeze = result.error_code not in {
            "ACTION_INTENT_INVALID",
            "ACTION_INTENT_BINDING_MISMATCH",
            "ACTION_ENVELOPE_IDENTITY_MISSING",
            "ACTION_ENVELOPE_PERSIST_FAILED",
            "ACTION_PARAMS_UNRECORDABLE",
            CONTRACT_AUTHORITY_MISSING,
            "CONTRACT_PERSIST_FAILED",
            "REPAIR_CONTEXT_NOT_ACTIVE",
            "REPAIR_INTENT_DOMAIN_MISMATCH",
            "REPAIR_INTENT_INVALID_REFS",
            "REPAIR_INTENT_INVALID_OBSERVATION",
            "REPAIR_INTENT_LINEAGE_MISMATCH",
            "REPAIR_INTENT_REQUIRED",
            "REPAIR_LINEAGE_RECORDING_REQUIRED",
            "REPAIR_ACTION_AFFORDANCE_MISMATCH",
            "REPAIR_TOOL_NOT_ALLOWED",
        }
        runner_dispatched = metadata.get("runner_dispatched")
        if intent.repair_context_id:
            repair_dispatch = self._repair_action_dispatch_outcome(execution)
            dispatched = repair_dispatch is True
        else:
            dispatched = bool(execution.attempted_execution and runner_dispatched is not False)
        progressed = memory.observe_material_action(
            intent,
            schema_valid=True,
            passed_freeze=passed_freeze,
            dispatched=dispatched,
        )
        if dispatched and intent.repair_context_id:
            pending = getattr(self, "_pending_repair_context", None)
            if pending is not None and pending.repair_context_id == intent.repair_context_id:
                self._pending_repair_context = None
                self._add_system_guidance(
                    f"REPAIR_CONTEXT {intent.repair_context_id} was consumed by one "
                    "dispatched model action. Do not reuse its repair_intent lineage; "
                    "rely on the resulting evidence and the next judge decision.",
                    priority=7,
                )

        evidence_body = result.raw_output if result.raw_output is not None else result.output
        if dispatched and (
            result.evidence_refs
            or result.refs
            or result.facts
            or result.test_stats is not None
            or execution.actual_executions
        ):
            memory.observe_evidence(
                hashlib.sha256(
                    str(evidence_body or "").encode("utf-8", errors="replace")
                ).hexdigest(),
                kind=f"tool:{execution.call.name}",
            )
        return progressed

    def _close_phase_for_loop(
        self,
        decision: LoopDecision,
        execution: ToolExecution,
    ) -> bool:
        machine = getattr(self, "phase_machine", None)
        state = getattr(self, "run_evidence_state", None)
        if machine is None or state is None or state.sealed or machine.is_complete:
            return False
        if self._keep_analyze_open_for_execution_plan("loop_force_break"):
            return False
        required_attempt = self._missing_required_test_attempt()
        if required_attempt is not None:
            self._force_required_test_attempt(
                required_attempt,
                trigger="loop_close",
            )
            self._add_system_guidance(
                "TEST_ATTEMPT_REQUIRED: repeated pre-execution failures cannot close "
                "a test-ready phase. The harness executed the required action: "
                f"{required_attempt.action_text()}",
                priority=9,
            )
            return False
        refs = tuple(
            self._dedupe_strings(
                [
                    decision.failure_ref,
                    execution.result.output_ref,
                    *execution.result.evidence_refs,
                    *execution.result.refs,
                ]
            )
        )
        claim = PhaseClaim(
            phase=machine.current_phase,
            signal="done",
            claimed_outcome=PhaseOutcome.FAILED,
            reason="loop repeated after an engine force-break",
            evidence_refs=refs,
        )
        gate = GateResult(
            accepted=True,
            validated_outcome=PhaseOutcome.FAILED,
            claim_disposition=ClaimDisposition.CONFIRMED,
            validator_state=ValidatorState.RED,
            reason="identical typed failure recurred without relevant state progress",
            evidence_refs=refs,
            claim=claim,
        )
        self._seal_engine_gate(claim, gate)
        record = machine.close_attempt(gate)
        route = self.transition_policy.decide(
            record,
            state=state,
            budgets=self._repair_budgets(),
        )
        self._apply_phase_decision(record, route)
        return True

    # ------------------------------------------------------------------
    # Advisor consult (spec §3.2)
    # ------------------------------------------------------------------

    _ADVISOR_DISABLED_TEXT = "advisor is disabled for this run — proceed with your best judgment"
    _ADVISOR_CAP_TEXT = "advisor cap reached for this phase — proceed with your best judgment"
    _ADVISOR_UNAVAILABLE_TEXT = "advisor unavailable — proceed with your best judgment"
    _ADVISOR_TRANSCRIPT_HEADER = "PHASE TRANSCRIPT"
    _ADVISOR_DIGEST_HEADER = "EVIDENCE DIGEST"
    # Says both things the jackrabbit/gora advisors got wrong: WHEN this was
    # read, and that it outranks any toolchain failure stated above it.
    _ADVISOR_TOOLCHAIN_PREFIX = (
        "Toolchain state (env overlay, read at consult time; supersedes any "
        "earlier toolchain failure in this digest): "
    )

    def _reset_advisor_run_state(self) -> None:
        """Run-scoped advisor state: telemetry plus the recurrence redirect."""
        self._advisor_calls: List[Dict[str, Any]] = []
        # Run-scoped so `advisor-entry-<n>` ids stay unique across every phase
        # entry and re-entry of the run.
        self._advisor_entry_counter = 0
        self._reset_advisor_phase_state()

    def _reset_advisor_phase_state(self) -> None:
        """Per-phase advisor state, reset with the other per-phase counters.

        The cap is per phase, and the recurrence redirect describes what has
        happened in this phase since the last consult. Carrying it across a
        transition would redirect a phase for a recurrence it never saw.

        `_advisor_entry_consult_done` clears here on purpose: a phase RE-entry
        (a repair loop) is a new entry and gets fresh advice, bounded by the
        cap. Under the deleted before-acting redirect that same reset re-armed
        a trap instead (2026-07-26 audit)."""
        self._advisor_calls_in_phase = 0
        self._advisor_redirect_armed = False
        self._advisor_loop_guidance = ""
        self._advisor_entry_consult_done = False

    @property
    def advisor_mode(self) -> str:
        """Either "off", "same-model", or an explicit litellm model name."""
        return str(getattr(self.config, "advisor_mode", "same-model") or "off").strip() or "off"

    @property
    def advisor_telemetry(self) -> Dict[str, Any]:
        """What the run pin records: the mode and one entry per consult."""
        return {
            "mode": self.advisor_mode,
            "calls": [dict(call) for call in getattr(self, "_advisor_calls", ())],
        }

    def _advisor_enabled(self) -> bool:
        return self.advisor_mode != "off"

    def _advisor_cap_exhausted(self) -> bool:
        cap = int(getattr(self.config, "advisor_phase_cap", 4) or 0)
        return cap <= 0 or int(getattr(self, "_advisor_calls_in_phase", 0)) >= cap

    def _advisor_model(self) -> str:
        mode = self.advisor_mode
        if mode != "same-model":
            return mode
        return str(self.llm_client.capabilities_for(ReactModelMode.ACTION).model)

    @staticmethod
    def _advisor_degraded_result(output: str, reason: str) -> ToolResult:
        """A consult that never reached a provider still answers successfully.

        Both degradations (ablation switch, phase cap) hand back guidance the
        executor can act on. A failure-shaped result here would invite the model
        to treat "no advice" as an impediment worth reporting."""
        return ToolResult.completed_success(output=output, metadata={"advisor": reason})

    def _record_advisor_call(self, *, phase: str, advice_chars: int, outcome: str) -> int:
        """Count one consult and clear the recurrence redirect it satisfies.

        An errored consult counts exactly like an answered one: the executor
        asked, and a provider outage must not pin it behind a redirect."""
        self._advisor_calls_in_phase = int(getattr(self, "_advisor_calls_in_phase", 0)) + 1
        self._advisor_redirect_armed = False
        self._advisor_loop_guidance = ""
        calls = getattr(self, "_advisor_calls", None)
        if calls is None:
            calls = self._advisor_calls = []
        calls.append(
            {
                "iteration": int(getattr(self, "current_iteration", 0)),
                "phase": phase,
                "advice_chars": int(advice_chars),
                "outcome": outcome,
            }
        )
        return len(calls)

    def consult_advisor(self) -> ToolResult:
        """Consult a fresh-context reviewer about the current phase (spec §3.2).

        Never raises and never returns a blocking result: mode "off", an
        exhausted phase cap and a provider outage all degrade to a
        success-shaped "proceed with your best judgment". A broken advisor is
        Plan-2 behavior, not an aborted run."""
        if not self._advisor_enabled():
            return self._advisor_degraded_result(self._ADVISOR_DISABLED_TEXT, "off")
        if self._advisor_cap_exhausted():
            return self._advisor_degraded_result(self._ADVISOR_CAP_TEXT, "cap")

        phase = str(getattr(getattr(self, "phase_machine", None), "current_phase", "") or "")
        model = self._advisor_model()
        try:
            advice = self.llm_client.get_advisor_response(
                self._advisor_messages(),
                model=model,
                max_tokens=int(getattr(self.config, "advisor_max_tokens", 2048)),
            )
        except Exception as exc:
            self.agent_logger.warning(f"Advisor consult unavailable: {exc}")
            advice = ""
        advice_text = str(advice or "").strip()
        outcome = "advice" if advice_text else "error"
        index = self._record_advisor_call(
            phase=phase,
            advice_chars=len(advice_text),
            outcome=outcome,
        )
        return ToolResult.completed_success(
            output=advice_text or self._ADVISOR_UNAVAILABLE_TEXT,
            metadata={
                "advisor": outcome,
                "advisor_call_index": index,
                "advisor_model": model,
            },
        )

    # Guarantee 1 (spec §3.2, amended 2026-07-26): the phases whose entry the
    # harness consults on. provision/analyze actions are dictated by the phase
    # objective, so they buy nothing from a reviewer.
    _ADVISOR_ENTRY_PHASES = frozenset({"build", "test"})
    _ADVISOR_ENTRY_NATIVE_TEXT = "[harness] consulting the advisor at phase entry"
    _ADVISOR_ENTRY_ACTION_TEXT = "HARNESS CONSULT: advisor at phase entry"

    def _maybe_consult_advisor_at_phase_entry(self) -> bool:
        """Consult the advisor mechanically on entering build or test.

        This REPLACES the before-acting redirect (which refused the phase's
        first state-changing call and pointed at `advisor()`). The 2026-07-26
        post-acceptance audit falsified that mechanism: in bigtop r1 it
        cancelled two correctly-planned 4-island batches — 8 wasted calls — and
        every phase re-entry reset the counter and re-armed the trap. A weak
        model cannot be required to re-remember a batch the harness threw away.
        Consulting at entry buys the SAME advice, in the window before the
        model plans, at zero cancelled work.

        The advisor still never blocks a run: mode "off" and an exhausted phase
        cap skip silently, and any failure inside the consult degrades to no
        consult rather than to an exception. Returns whether a consult pair was
        appended (for tests and callers; the caller never acts on False)."""
        machine = getattr(self, "phase_machine", None)
        phase = str(getattr(machine, "current_phase", "") or "")
        if phase not in self._ADVISOR_ENTRY_PHASES:
            return False
        if getattr(self, "_advisor_entry_consult_done", False):
            return False
        if not self._advisor_enabled() or self._advisor_cap_exhausted():
            return False
        # Latched BEFORE the consult: a consult that raises half-way must not
        # be retried on the next call of this seam.
        self._advisor_entry_consult_done = True
        try:
            self._append_entry_consult_pair()
            return True
        except Exception as exc:
            # Module logger, not `agent_logger`: this handler is the last thing
            # standing between an advisor defect and an aborted run, so it may
            # not itself depend on engine state being complete.
            logger.warning(f"Advisor phase-entry consult skipped: {exc}")
            return False

    def _append_entry_consult_pair(self) -> None:
        """The harness authors one advisor call, forced-attempt style.

        Same shape as `_force_required_test_attempt`: a synthetic ACTION step
        whose id the harness mints, answered immediately by its observation, so
        the pairing invariant and the evidence trail hold without a second code
        path. The result is `consult_advisor()`'s — the identical contract the
        model's own `advisor()` call gets, cap accounting included.

        And the identical LEDGER contract too (spec §2.2 rule 5): this call had
        an envelope and a result and no `loop_decision` at all, which is the
        same silence the phase tool was measured in — with the harness as the
        author instead of the model. It is a controller move, so it seals a
        controller turn, in the same sequence as every other turn."""
        turn_started = self._turn_stamp()
        result = self.consult_advisor()
        call = ToolCall(
            name="advisor",
            raw_params={},
            validated_params={},
            raw_action_text=self._ADVISOR_ENTRY_ACTION_TEXT,
            source_step_index=getattr(self, "current_iteration", 0),
            model_used="harness",
        )
        execution = ToolExecution(
            call=call,
            result=result,
            status="success",
            raw_params={},
            validated_params={},
            observation_text=format_tool_result("advisor", result),
            attempted_execution=True,
            metadata={"advisor_entry_consult": True},
        )
        recorded, execution_id, actual_executions = self._record_execution_bundle(execution, call)
        self._advisor_entry_counter = int(getattr(self, "_advisor_entry_counter", 0)) + 1
        entry_call_id = f"advisor-entry-{self._advisor_entry_counter}"
        self.steps.append(
            ReActStep(
                step_type=StepType.ACTION,
                content=self._ADVISOR_ENTRY_ACTION_TEXT,
                tool_name="advisor",
                tool_params={},
                tool_result=recorded,
                timestamp=self._get_timestamp(),
                model_used="harness",
                tool_call_id=entry_call_id,
                native_text=self._ADVISOR_ENTRY_NATIVE_TEXT,
            )
        )
        # The ACTION step is appended first on purpose: the envelope keys off
        # the newest ACTION identity, exactly as a model-issued call does.
        envelope_id = self._emit_control_action_envelope("advisor", {})
        self._emit_control_tool_result(
            envelope_id=envelope_id,
            execution_id=execution_id,
            tool="advisor",
            params={},
            result=recorded,
            actual_executions=actual_executions,
        )
        self._apply_tool_execution_loop_effects(execution)
        observation_step = self._append_native_observation(
            entry_call_id,
            execution.observation_text,
            source_tool="advisor",
        )
        # Closure-contract rule 2: a harness-authored observation persists where
        # every other observation does. In d2r4 jackrabbit and gora the entry
        # consult's advice — which the model then acted on — appeared in no
        # `contexts/phase_build.json` history entry at all, only in
        # `full_outputs.jsonl`, so the phase record showed a model reacting to
        # advice the record never carried.
        self._persist_action_to_branch_history(
            getattr(getattr(self, "context_manager", None), "current_task_id", None),
            tool_name="advisor",
            tool_params={},
            result=recorded,
            observation_text=execution.observation_text,
            # The entry is marked reviewer prose by the write itself, keyed on
            # the tool: the completion gates read history as evidence, and
            # advice to "install openjdk-17" is not a JDK installed — however
            # the consult was asked for.
        )
        self._seal_turn_record(
            actor="controller",
            t0=turn_started,
            t1=self._turn_stamp(),
            envelope_ref=envelope_id,
            observation_ref=self._delivered_observation_ref(observation_step),
            iteration=getattr(self, "current_iteration", None),
        )

    def _advisor_messages(self) -> List[Dict[str, str]]:
        """System reviewer brief + the whole phase transcript and evidence.

        The model chooses WHEN to consult, never WHAT the reviewer sees."""
        # Prompt key: advisor_system
        system_brief = str(self.prompts.get("advisor_system") or "")
        system_brief = self._system_prompt_for_current_phase(system_brief)
        return [
            {"role": "system", "content": system_brief},
            {"role": "user", "content": self._advisor_user_message()},
        ]

    def _advisor_user_message(self) -> str:
        sections = [
            f"{self._ADVISOR_TRANSCRIPT_HEADER}:",
            self._advisor_transcript_text(),
            self._advisor_evidence_digest(),
        ]
        return "\n".join(section for section in sections if section)

    def _advisor_transcript_text(self) -> str:
        """The rendered phase window flattened to `<ROLE>: <content>` lines.

        Reusing `render_messages` is the point: the reviewer reads EXACTLY the
        conversation the executor is working from (same pairing, same clamped
        tool output), not a second, drifting projection of it."""
        lines: List[str] = []
        for message in render_messages("", getattr(self, "steps", None) or []):
            role = str(message.get("role") or "")
            if role == "system":
                continue
            content = str(message.get("content") or "")
            calls = message.get("tool_calls") or ()
            if calls:
                rendered = "; ".join(
                    f"{(call.get('function') or {}).get('name') or 'unnamed'}"
                    f"({(call.get('function') or {}).get('arguments') or '{}'})"
                    for call in calls
                )
                content = f"{content} [tool calls: {rendered}]".strip()
            lines.append(f"{role.upper()}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _advisor_count(value: Any) -> str:
        """Counts render like the model-visible collection line: None is
        `unknown`, never 0 — an unobserved count is not a zero count."""
        return "unknown" if value is None else str(value)

    def _last_test_attempt_line(self) -> str:
        """Render the latest observed JVM reports or pytest collection facts.

        A Build-phase JVM invocation may already have executed tests. Keep its
        reported counts and receipt reference visible after the phase switch;
        this prompt projection does not certify completion or compare CI scope.
        """
        state = getattr(self, "run_evidence_state", None)
        for observation in reversed(tuple(getattr(state, "tool_observations", ()) or ())):
            result = getattr(observation, "result", None)
            metadata = dict(getattr(result, "metadata", None) or {})
            if "collection_scope" in metadata:
                break  # The newest relevant attempt is pytest; use its vocabulary below.
            if metadata.get("system") not in {"maven", "gradle"} or not (
                metadata.get("receipt_id") or metadata.get("runner_dispatched")
            ):
                continue
            job_id = metadata.get("job_id")
            orchestrator = getattr(self, "orchestrator", None)
            if job_id and orchestrator is not None:
                records = read_obligations(orchestrator)
                matching = [
                    record
                    for record in records or ()
                    if record.get("job_id") == job_id
                    and record.get("run_id") == getattr(state, "run_id", None)
                    and record.get("tool") == metadata.get("system")
                    and record.get("working_directory") == metadata.get("working_directory")
                ]
                settlement = (
                    settlement_from_ledger(orchestrator, matching[0])
                    if len(matching) == 1
                    else None
                )
                if settlement is not None:
                    return (
                        f"Last JVM runner observation: {str(metadata.get('command') or 'unknown')[:2048]} — "
                        f"{settlement.notice()}; output=job:{job_id}. "
                        "This host-authorized settlement supersedes the initiating call's "
                        "pending outcome. Report paths are not case counts and an exit code "
                        "is not a completion or CI verdict. Inspect this receipt and log "
                        "before declaring tests missing or rerunning them."
                    )
            counts = metadata.get("report_test_counts")
            if metadata.get("test_stats_basis") != "invocation_report_xml" or not isinstance(
                counts, dict
            ):
                counts = {}
            values = ", ".join(
                f"{key}={self._advisor_count(counts.get(key))}"
                for key in ("reported", "passed", "failed", "errors", "skipped")
            )
            outcome = getattr(result, "operation_outcome", "unknown")
            return (
                f"Last JVM runner observation: {str(metadata.get('command') or 'unknown')[:2048]} — "
                f"cwd={str(metadata.get('working_directory') or 'unknown')[:512]}, "
                f"receipt={str(metadata.get('receipt_id') or 'unknown')[:160]}, "
                f"output={str(metadata.get('output_ref_id') or 'unknown')[:160]}, "
                f"tool_outcome={getattr(outcome, 'value', outcome)}, "
                f"invocation XML counts: {values}. "
                "These are tool observations, not a completion verdict. Inspect the linked "
                "receipt and required scope before declaring tests missing or rerunning them."
            )
        metadata = self._last_pytest_metadata()
        if not metadata:
            return ""
        command = str(metadata.get("command") or metadata.get("collection_command") or "").strip()
        return (
            f"Last test attempt: {command or 'unknown'} — "
            f"scope={metadata.get('collection_scope') or 'unknown'}, "
            f"collected={self._advisor_count(metadata.get('collected'))}, "
            f"selected={self._advisor_count(metadata.get('collected_after_deselection'))}, "
            f"executed={self._advisor_count(metadata.get('executed'))}, "
            f"collection_errors={self._advisor_count(metadata.get('collection_errors'))}"
        )

    def _last_pytest_metadata(self) -> Dict[str, Any]:
        """The newest pytest attempt's metadata, or {} when none ran."""
        state = getattr(self, "run_evidence_state", None)
        for observation in reversed(tuple(getattr(state, "tool_observations", ()) or ())):
            metadata = dict(getattr(getattr(observation, "result", None), "metadata", None) or {})
            if "collection_scope" in metadata:
                return metadata
        return {}

    def _native_state_line(self) -> str:
        """The native project's independent capability facts, for native-build
        projects only: the probed artifact state plus the last bounded smoke's
        verdict and its junit skip reasons.

        P0-D/§P1-E: the advisor repeated the phase outcome's "native core was
        not built" verbatim. It now reads the artifact probe and the skip
        reasons, so it can CORRECT that reading instead of echoing it."""
        rec = self._build_recommendation()
        if not rec.get("has_native_build"):
            return ""
        fact = self._native_artifact_fact(rec=rec)
        artifacts = fact["status"]
        if artifacts == "present":
            artifacts = f"present ({self._native_artifact_count_text(fact)})"
        parts = [f"artifacts={artifacts}"]
        metadata = self._last_pytest_metadata()
        if metadata.get("smoke_capability_unproven"):
            parts.append("last bounded smoke=capability_unproven")
            reasons = [str(reason) for reason in metadata.get("smoke_skip_reasons") or ()]
            if reasons:
                parts.append(f"skip reasons: {'; '.join(reasons)}")
        return f"Native state: {', '.join(parts)}"

    def _toolchain_state_line(self) -> str:
        """What the env overlay says about the toolchain RIGHT NOW.

        d2r4 jackrabbit: maven was installed, registered and activated, and the
        provision gate accepted "Installed and activated Maven 3.8.7" — then the
        build-entry advisor opened with "`/usr/bin/mvn` is missing", the
        pre-provision `ENV_EXECUTABLE_NOT_FOUND` the handoff still carries under
        LAST RELEVANT FAILURES. d2r4 gora is the same defect inverted: the
        advisor sent the model to repair "the broken /usr/bin/mvn env overlay"
        that no registration had ever written.

        A superseded failure is not a current state, so the digest states the
        current one beside it. The env overlay is the only host-published
        toolchain record (``toolchains.json`` has no publication authority and
        is excluded from resolution for exactly that reason), and it is read
        here at consult time, never from a snapshot taken before provisioning.

        The whole overlay entry is read, not its `candidates` half. jackrabbit's
        `/usr/bin/mvn` stayed a registered candidate after the `[3.9,)` failure
        blocked it — `block` records negative evidence and drops `active`, it
        does not un-register — so a digest that reads candidates alone hands the
        reviewer the one Maven that provably cannot answer this build, as a move
        needing no install. A blocked executable is stated AS blocked, with the
        requirement it fails, and the tool's observed requirements are stated
        too: the constraint is what makes a runtime a non-move.

        Silent when there is no container to read and when the overlay cannot
        be read: an unreadable overlay is not an empty one, and the digest may
        not state a state it never read."""
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return ""
        from sag.runtime.env_overlay import EnvOverlayStore

        overlay = EnvOverlayStore(orchestrator).inspect()
        if overlay.get("warnings"):
            return ""
        entries: List[str] = []
        tools = overlay.get("tools") or {}
        for name in sorted(tools):
            entry = tools.get(name) or {}
            candidates = entry.get("candidates") or {}
            active = entry.get("active")
            blocked = self._blocked_executables(entry)
            # Every registered candidate the overlay has NOT blocked, the active
            # one first: a runtime the overlay already holds is a move the model
            # can make without installing anything, and the reviewer can only
            # name it if the digest does.
            ordered = [
                executable
                for executable in [active]
                if executable in candidates and executable not in blocked
            ]
            ordered += [
                executable
                for executable in sorted(candidates)
                if executable != active and executable not in blocked
            ]
            for executable in ordered:
                candidate = candidates.get(executable) or {}
                version = str(candidate.get("version") or "").strip()
                entries.append(
                    f"{name} {version + ' ' if version else ''}"
                    f"{'active' if executable == active else 'registered'} at {executable}"
                    f"{'' if executable == active else ' (not active)'}"
                )
            for executable in sorted(blocked):
                version, detail = blocked[executable]
                entries.append(
                    f"{name} {version + ' ' if version else ''}blocked at {executable}"
                    f"{f' ({detail})' if detail else ''}"
                )
            for requirement in entry.get("requirements") or []:
                raw = str(requirement.get("raw") or "").strip()
                if not raw:
                    continue
                scope = str(requirement.get("working_directory") or "").strip()
                entries.append(
                    f"{name} observed requirement: {raw}{f' (at {scope})' if scope else ''}"
                )
        return self._ADVISOR_TOOLCHAIN_PREFIX + (
            "; ".join(entries) or "no runtime is registered in the env overlay"
        )

    @staticmethod
    def _blocked_executables(entry: Mapping[str, Any]) -> Dict[str, Tuple[str, str]]:
        """Every blocked executable of one overlay entry, with what blocked it.

        Keyed by executable because that is what a block record is about: one
        exact path, blocked by one observed requirement. The newest record for a
        path wins its detail — `block` appends, and the last thing observed is
        the current state.
        """
        blocked: Dict[str, Tuple[str, str]] = {}
        for record in entry.get("blocked") or []:
            if not isinstance(record, Mapping):
                continue
            executable = str(record.get("executable") or "").strip()
            if not executable:
                continue
            requirement = str(record.get("requirement") or "").strip()
            reason = str(record.get("reason") or "").strip()
            detail = f"requirement {requirement}" if requirement else reason
            blocked[executable] = (str(record.get("version") or "").strip(), detail)
        return blocked

    def _advisor_evidence_digest(self) -> str:
        """The deterministic evidence section: handoff projection, the toolchain
        state the overlay holds at consult time, armed recurrence guidance, the
        last test attempt's collection facts, and (native projects only) the
        native state."""
        parts: List[str] = []
        handoff = getattr(self, "phase_handoff", None)
        if handoff is not None:
            phase = str(getattr(getattr(self, "phase_machine", None), "current_phase", "") or "all")
            try:
                budget = int(getattr(self.config, "phase_handoff_char_budget", 6000))
                parts.append(handoff.project_for(phase, char_budget=budget).to_prompt_text())
            except Exception as exc:
                # A digest gap must not cost the run its advice.
                self.agent_logger.warning(f"Advisor evidence digest unavailable: {exc}")
        # After the handoff on purpose: the current state is what supersedes the
        # superseded failure the projection above may still carry.
        try:
            toolchain_state = self._toolchain_state_line()
        except Exception as exc:
            # A digest gap must not cost the run its advice.
            logger.warning(f"Advisor toolchain-state digest unavailable: {exc}")
            toolchain_state = ""
        if toolchain_state:
            parts.append(toolchain_state)
        guidance = str(getattr(self, "_advisor_loop_guidance", "") or "").strip()
        if guidance and getattr(self, "_advisor_redirect_armed", False):
            parts.append(guidance)
        try:
            last_test = self._last_test_attempt_line()
        except Exception as exc:
            # A digest gap must not cost the run its advice.
            logger.warning(f"Advisor test-attempt digest unavailable: {exc}")
            last_test = ""
        if last_test:
            parts.append(last_test)
        try:
            native_state = self._native_state_line()
        except Exception as exc:
            # A digest gap must not cost the run its advice.
            logger.warning(f"Advisor native-state digest unavailable: {exc}")
            native_state = ""
        if native_state:
            parts.append(native_state)
        if not parts:
            return ""
        return "\n".join([f"{self._ADVISOR_DIGEST_HEADER}:", *parts])

    # ------------------------------------------------------------------
    # The three mechanical guarantees (spec §3.2)
    # ------------------------------------------------------------------

    # Tools that can never change project state, so they never need a consult
    # first. `file_io`, `project` and `bash` are decided per action below.
    _ADVISOR_EXEMPT_TOOLS = frozenset({"advisor", "search", "phase", "report"})
    # Reconnaissance the model must stay free to do: making it consult before
    # `ls` would buy ceremony, which is the very cost the advisor exists to cut.
    _READONLY_BASH_PREFIXES = (
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "pwd",
        "wc",
        "which",
        "env",
        "echo",
    )

    def _is_state_changing(self, tool_name: str, params: Dict[str, Any]) -> bool:
        """Whether this exact call can change the project's physical state."""
        name = str(tool_name or "").strip().lower()
        if name in self._ADVISOR_EXEMPT_TOOLS:
            return False
        params = params or {}
        action = str(params.get("action") or "").strip().lower()
        if name == "file_io":
            return action not in {"read", "list"}
        if name == "project":
            return action != "analyze"
        if name == "bash":
            return self._first_command_token(params.get("command")) not in (
                self._READONLY_BASH_PREFIXES
            )
        return True

    @staticmethod
    def _first_command_token(command: Any) -> str:
        text = str(command or "").strip()
        if not text:
            return ""
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        return tokens[0] if tokens else ""

    def _advisor_redirect_for_call(self, call: ToolCall) -> ToolExecution | None:
        """Pre-execution advisor gate. None when the call may proceed.

        Only the materially repeated-action rule remains.  Both older lifecycle
        redirects are gone: ``before-acting`` cancelled correctly planned
        batches, while ``before-giving-up`` prevented a terminal claim from
        reaching the physical judge that now creates the facts-only
        RepairContext.  A judge claim must not be intercepted by an advisor
        with less authoritative evidence.

        ``before-acting`` was deleted on 2026-07-26 after cancelling a phase's first
        state-changing call cancelled correctly-planned batches wholesale
        (bigtop r1), so guarantee 1 is now the harness-authored consult at
        phase entry (`_maybe_consult_advisor_at_phase_entry`), which costs no
        planned work at all.

        The remaining rule is disabled when `advisor_mode == "off"` (the
        ablation switch) or the phase cap is exhausted: a redirect the advisor
        can no longer answer would dead-lock the run, and the advisor must
        NEVER block a run."""
        if not self._advisor_enabled() or self._advisor_cap_exhausted():
            return None
        name = str(call.name or "").strip().lower()
        if name == "advisor":
            return None

        params = call.validated_params or call.raw_params or {}
        state_changing = self._is_state_changing(name, params)

        if getattr(self, "_advisor_redirect_armed", False) and state_changing:
            return self._advisor_redirect_execution(
                call,
                "when-stuck",
                "You are repeating an action that has already failed without progress. "
                "Consult advisor() before retrying. This call was not executed.",
            )

        return None

    @staticmethod
    def _advisor_redirect_execution(call: ToolCall, rule: str, message: str) -> ToolExecution:
        """One redirect, shaped exactly like a Plan-2 refusal.

        Same `ToolExecution` contract, same `attempted_execution=False`, so it
        flows through the identical evidence-recording path: the pairing
        invariant and the audit trail hold without a second code path."""
        result = ToolResult.completed(
            output=message,
            operation_outcome=OperationOutcome.SKIPPED,
            metadata={"advisor_redirect": rule},
        )
        return ToolExecution(
            call=call,
            result=result,
            status="skipped",
            raw_params=call.raw_params,
            validated_params=call.validated_params,
            observation_text=format_tool_result(call.name, result),
            attempted_execution=False,
            metadata={"advisor_redirect": rule},
        )

    def _note_advisor_execution(
        self,
        execution: ToolExecution,
        loop_decision: LoopDecision | None,
    ) -> None:
        """Update the recurrence redirect from one executed tool result.

        Redirects and refusals never reached a tool, so they can neither create
        a failure to review nor evidence of being stuck."""
        if not execution.attempted_execution:
            return
        # `request_thinking` is LoopMemory's own redirect signal (guide /
        # force_break): the identical action and outcome recurred while the
        # relevant state stood still.
        if (
            loop_decision is not None
            and getattr(loop_decision, "request_thinking", False)
            and int(getattr(loop_decision, "recurrence_count", 0)) >= 2
        ):
            self._advisor_redirect_armed = True
            self._advisor_loop_guidance = self._loop_guidance(loop_decision)

    def _refusal_for_call(self, call: ToolCall) -> ToolExecution | None:
        """The harness refusals that stand in for a real execution.

        Shared by both protocols: sealed evidence closes every evidence tool,
        and `report` stays closed until the verdict snapshot exists."""
        if call.name == "advisor":
            # The advisor is NEVER refused. `consult_advisor` already answers
            # every mode; a provider outage must never become a project block.
            return None
        if self._evidence_execution_closed(call):
            return self._refused_closed_evidence_execution(call)
        if call.name == "report" and not self._report_execution_allowed():
            return self._refused_report_execution(call)
        return None

    def _execute_action_step(self, step: ReActStep) -> Optional[str]:
        """Execute one ACTION step that is already appended to `self.steps`.

        Called once per tool call by the native dispatcher. Returns the reason the enclosing batch must stop (a phase
        transition is pending, or the loop breaker closed the phase), else
        None. When the step carries a native `tool_call_id`, its observation is
        stamped with the same id so the two render as a tool_use/tool_result
        pair."""
        self.agent_logger.info(f"🔧 ACTION: {step.content}")
        logger.info(f"🔧 ACTION: {step.content}")

        # Emit UI event for action with parameters
        self.emit(
            EventType.AGENT_ACTION,
            message=f"Using {step.tool_name or 'tool'}",
            step_num=self.current_iteration,
            tool_name=step.tool_name or "unknown",
            tool_params=step.tool_params or {},
        )

        # Update token tracker with actual tool name for the last action token record
        if step.tool_name:
            self.token_tracker.update_last_tool_name(step.tool_name)

        # Detailed logging in verbose mode
        if self.config.verbose:
            self._log_react_step_verbose(step)

        branch_task_id = getattr(self.context_manager, "current_task_id", None)
        # The turn opens when the call starts and closes when its record is
        # sealed — the same span the derived view reads off envelope→result, so
        # both feeds bound a turn the same way.
        turn_started = self._turn_stamp()
        gate_before_turn = getattr(self, "_last_sealed_decision_id", None)
        call = self._build_tool_call_from_step(step)
        # Legacy protocol steps carry no id; native ones do.
        native_call_id = getattr(step, "tool_call_id", None)
        previous_native_call_id = getattr(self, "_active_native_tool_call_id", None)
        if native_call_id:
            self._active_native_tool_call_id = native_call_id
        try:
            # The advisor gate runs FIRST: a redirected call must not be
            # described to the model in closed-evidence refusal wording.
            execution = self._advisor_redirect_for_call(call)
            if execution is None:
                execution = self._refusal_for_call(call)
            if execution is None:
                execution = self._execute_tool_call(call)
            rejected_completion = self._prepare_rejected_completion(execution)
            result, control_execution_id, actual_executions = self._record_execution_bundle(
                execution, call
            )
            step.tool_result = result
            control_envelope_id = str(execution.metadata.get("control_envelope_id") or "") or None
            if execution.validated_params is not None:
                control_params = execution.validated_params
            elif call.validated_params is not None:
                control_params = call.validated_params
            else:
                control_params = call.raw_params
            if control_envelope_id is None and execution.attempted_execution:
                control_envelope_id = self._emit_control_action_envelope(
                    call.name,
                    control_params,
                    intent=call.action_intent,
                )
            if control_envelope_id is None:
                # Nothing was dispatched, so nothing will answer: the refusal is
                # this call's envelope and its result at once (spec §2.2 rule 4).
                # Sealed HERE, before the loop decision below reads the call, so
                # the record opens the turn that decision closes.
                self._emit_control_refusal_record(
                    call,
                    execution,
                    control_params,
                    tool_call_id=native_call_id,
                )
            self._emit_control_tool_result(
                envelope_id=control_envelope_id,
                execution_id=control_execution_id or new_execution_id(),
                tool=call.name,
                params=control_params,
                result=result,
                actual_executions=actual_executions,
            )
        finally:
            self._active_native_tool_call_id = previous_native_call_id
        self._observe_action_intent_progress(execution)
        # Preserve native tool-call pairing: the tool result must immediately
        # answer its ACTION before a newly opened RepairContext can append
        # system guidance for the next model turn.
        observation_step = self._append_native_observation(
            native_call_id,
            execution.observation_text,
            source_tool=call.name,
        )
        completion_closed_phase = self._apply_rejected_completion_control(rejected_completion)
        loop_decision = self._apply_tool_execution_loop_effects(execution)
        self._note_advisor_execution(execution, loop_decision)
        # The call, its answer and the control layer's reading of both are on
        # the record; the turn can now say what it saw, said and heard. Sealed
        # here rather than after the batch so a turn that ends the batch — a
        # closure, a phase signal, a forced attempt below — is still recorded,
        # and so turn ids follow the order the calls actually ran in.
        self._seal_turn_record(
            actor="model",
            t0=turn_started,
            t1=self._turn_stamp(),
            envelope_ref=control_envelope_id,
            observation_ref=self._delivered_observation_ref(observation_step),
            gate_decision_id=self._claim_turn_gate(gate_before_turn),
            iteration=getattr(self, "current_iteration", None),
        )

        # Log tool result in verbose mode
        if self.config.verbose:
            self._log_tool_result_verbose(step.tool_name, result)

        if completion_closed_phase:
            logger.debug("Stopping the action batch after agent_no_progress closure")
            return "the finalizer closed this phase after agent no progress"

        if result.error_code == "TEST_ATTEMPT_REQUIRED":
            required_attempt = self._missing_required_test_attempt()
            if required_attempt is not None:
                self._force_required_test_attempt(
                    required_attempt,
                    trigger="termination_refusal",
                )

        # Log to branch context if we're in one
        self._persist_action_to_branch_history(
            branch_task_id,
            tool_name=step.tool_name,
            tool_params=step.tool_params,
            result=result,
            observation_text=execution.observation_text,
        )

        if (
            loop_decision is not None
            and loop_decision.close_phase
            and self._close_phase_for_loop(loop_decision, execution)
        ):
            logger.debug("Stopping the action batch after loop-driven phase closure")
            return "the loop breaker closed this phase"

        phase_signal = (result.metadata or {}).get("phase_signal")
        if phase_signal in {"done", "blocked"}:
            # The engine must apply the accepted terminal transition before
            # any later action can run under a new or closed prerequisite.
            logger.debug(f"Stopping the action batch at phase signal {phase_signal!r}")
            return f"a {phase_signal} phase transition is being processed"

        return None

    def _persist_action_to_branch_history(
        self,
        branch_task_id: Optional[str],
        *,
        tool_name: Optional[str],
        tool_params: Optional[Dict[str, Any]],
        result,
        observation_text: str,
    ) -> None:
        """Write one executed action into the phase history it belongs to.

        Every action, whoever authored it. A forced attempt used to write its
        ACTION and its observation into the window and nowhere else, so the
        phase history — the record the next context reads, and the one every
        post-hoc reconstruction reads — was missing the harness's own evidence
        (cayenne, ignite, polaris, camel; spec §2.2 rule 2).

        `entry_kind` states what an entry IS when that changes how a reader may
        use it. Only the advisor's prose sets it today, and it is resolved HERE
        from the tool that produced the text rather than passed by one caller:
        `advisor()` is model-callable, so the same reviewer sentences arrive by
        the harness's phase-entry consult and by an ordinary model-issued
        execution, and marking only the first left the completion gates
        text-sniffing every consult the model asked for. The kind says what the
        text is, not who asked. A tool that answered from the container carries
        no kind — it is evidence, and evidence is read.

        Never raises: history is a projection, and a projection that fails must
        not take the run with it.
        """
        if not branch_task_id:
            return
        entry_kind = history_entry_kind_for_tool(tool_name)
        try:
            output_to_store = result.output if result.output else ""
            from datetime import datetime

            timestamp = datetime.now().isoformat()
            fact_sheet_identity = project_fact_sheet_identity(result.metadata)

            # Store full output and get reference if output is large
            stored_output_refs = []
            if (
                len(output_to_store) > 800
                and self.output_storage is not None
                and not result.output_ref
            ):
                # Store the full output
                ref_id = self.output_storage.store_output(
                    task_id=self.context_manager.current_task_id,
                    tool_name=tool_name,
                    output=output_to_store,
                    timestamp=timestamp,
                    metadata={
                        "invocation_status": result.invocation_status.value,
                        "operation_outcome": result.operation_outcome.value,
                        "evidence_status": result.evidence_status.value,
                        "iteration": self.current_iteration,
                        "action": (tool_params or {}).get("action"),
                        **fact_sheet_identity,
                    },
                )
                stored_output_refs.append(ref_id)

                # Get truncated version with reference
                output_to_store = self.output_storage.get_truncation_with_reference(
                    output=output_to_store,
                    ref_id=ref_id,
                    max_length=800,
                    tool_name=tool_name,
                )
            elif fact_sheet_identity and result.output_ref and len(output_to_store) > 800:
                # The durable evidence path may already have persisted this
                # fact sheet. Branch history still gets a bounded preview,
                # never a second multi-kilobyte JSON copy.
                output_to_store = self.output_storage.get_truncation_with_reference(
                    output=output_to_store,
                    ref_id=result.output_ref,
                    max_length=800,
                    tool_name=tool_name,
                )

            observation_to_store = observation_text
            if fact_sheet_identity and len(observation_to_store) > 6_000:
                ref = result.output_ref or "structured fact-sheet metadata"
                suffix = (
                    "\n… [engine project-fact projection truncated in branch history; "
                    f"source: {ref}]"
                )
                observation_to_store = observation_to_store[: 6_000 - len(suffix)] + suffix

            history_entry = {
                "type": "action",
                "iteration": self.current_iteration,
                "tool_name": tool_name,
                "parameters": tool_params or {},
                "succeeded": result.succeeded,
                "invocation_status": result.invocation_status.value,
                "operation_outcome": result.operation_outcome.value,
                "evidence_status": result.evidence_status.value,
                "output": output_to_store,
                "observation": observation_to_store,
                "output_refs": self._dedupe_strings(
                    [
                        *stored_output_refs,
                        result.output_ref,
                        *self._output_refs_from_text(output_to_store),
                    ]
                ),
            }
            if entry_kind:
                history_entry["entry_kind"] = entry_kind
            if fact_sheet_identity:
                history_entry["metadata"] = fact_sheet_identity
            for field_name in ("failure_signature", "error_tail_preview"):
                value = getattr(result, field_name)
                if value:
                    history_entry[field_name] = value
            # A pending dispatch is not build-execution evidence;
            # completion gates must be able to tell.
            dispatch_status = (result.metadata or {}).get("dispatch_status")
            if dispatch_status:
                history_entry["dispatch_status"] = dispatch_status
            self.context_manager.add_to_branch_history(
                branch_task_id,
                history_entry,
            )
        except Exception as e:
            logger.warning(f"Failed to log action to branch history: {e}")

    def _answered_action_result(self):
        """The tool result of the ACTION step this observation answers.

        The published-on-the-engine seam `_observation_source_tool` uses, so no
        caller has to thread a result through the observation path."""
        for step in reversed(getattr(self, "steps", None) or ()):
            if getattr(step, "step_type", None) is StepType.ACTION:
                return getattr(step, "tool_result", None)
        return None

    def _commit_claim_transitions(self, source_tool: Optional[str]) -> None:
        """Never transition claims from authorization citations.

        Contract ``supporting_claim_ids`` explain why dispatch was permitted;
        they do not identify the predicate a receipt tested. Until an explicit
        predicate-subject record exists, this seam deliberately moves nothing.
        """
        del source_tool

    def _assessment_guard(self, name: str) -> set:
        """The in-memory set of assessment ids one reaction has already run for.

        Published lazily on the engine, like `_observation_source_tool`: the
        bound is per RUN, and the seam it guards is reached from constructors
        the tests substitute."""
        guard = getattr(self, name, None)
        if guard is None:
            guard = set()
            setattr(self, name, guard)
        return guard

    def _ensure_observed_receipt_assessed(self, source_tool: Optional[str]) -> None:
        """Idempotent backstop for a build receipt not yet assessed.

        Detached settlement and output persistence can make a valid
        facade-authorized receipt visible after the facade's immediate
        assessment pass. This seam assesses only that existing receipt; it
        never creates dispatch authority or a replacement contract."""
        if source_tool != "build":
            return
        execute = getattr(getattr(self, "orchestrator", None), "execute_command", None)
        if not callable(execute):
            return
        try:
            metadata = getattr(self._answered_action_result(), "metadata", None) or {}
            receipt_id = str(metadata.get("receipt_id") or "").strip()
            if receipt_id:
                result = self._answered_action_result()
                ensure_receipt_assessed(
                    execute,
                    receipt_id,
                    output=getattr(result, "raw_output", None),
                    evidence_ref=receipt_id,
                )
        except Exception as exc:
            logger.debug(f"observed-receipt assessment backstop skipped: {exc}")

    def _pending_settlement_notices(self) -> List[str]:
        """The `[settled]` lines the next observation owes the model.

        Published lazily on the engine, like `_assessment_guard`: the seam is
        reached from constructors the tests substitute."""
        notices = getattr(self, "_settlement_notice_queue", None)
        if notices is None:
            notices = []
            self._settlement_notice_queue = notices
        return notices

    def _sweep_job_obligations(self) -> None:
        """Run one non-blocking lifecycle reconciliation after an action batch.

        Plan 8 §3.2. p7d polaris polled its test job for the rest of the run
        and nothing ever went back to ask whether it had finished; 321 passing
        tests were left unclaimable. This runs after every executed action
        batch REGARDLESS of which tools the batch used, so a poll-heavy model
        cannot starve settlement (spec §7).

        Announcement is keyed on the LEDGER, not on who settled: the phase
        gate settles too (so that a claim is never graded against moving
        books), and its settlements must still produce their event, their one
        bounded notice and the post-receipt hooks. One announcement per job,
        for the life of the run.

        A SEALED run sweeps nothing. The report phase still executes action
        batches after evidence-close, and settling in that window would write a
        receipt, an assessment, a repair context and a `job_settled` event AFTER
        `evidence_close` — for a job the sealed verdict has already recorded as
        `job_unsettled`. A sealed run accepts no further evidence, and the
        obligation staying open is the honest end state, not a gap.

        Never raises: an unswept ledger is an open obligation, which the
        closing sweep and evidence-close already state honestly."""
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None or self._evidence_is_sealed():
            return
        try:
            # ONE ledger read per batch: the common case is a run that never
            # detached anything, and it must not pay two round trips to be
            # told so twice.
            records = self._obligations_still_owed(orchestrator)
            if records is None:
                self._record_job_barrier_integrity_failure(
                    ("ledger_unreadable_after_action_batch",)
                )
                return
            if not records:
                return
            reconciliation = reconcile_job_obligations(orchestrator, obligations=records)
            self._announce_job_reconciliation(reconciliation)
            if reconciliation.integrity_failures:
                self._record_job_barrier_integrity_failure(reconciliation.integrity_failures)
                return
            settled_now = {
                settlement.job_id: settlement for settlement in reconciliation.settlements
            }
            announced = self._assessment_guard("_announced_job_settlements")
            for record in records:
                job_id = str(record.get("job_id") or "").strip()
                if not job_id or job_id in announced:
                    continue
                settlement = settled_now.get(job_id)
                if settlement is None:
                    if not str(record.get("settled_receipt_id") or "").strip():
                        continue
                    settlement = settlement_from_ledger(orchestrator, record)
                if settlement is None:
                    continue
                announced.add(job_id)
                self._emit_control_event("job_settled", settlement.event_payload())
                if hasattr(self, "steps"):
                    self.steps.append(
                        ReActStep(
                            step_type=StepType.SYSTEM_GUIDANCE,
                            content=settlement.notice(),
                            timestamp=self._get_timestamp(),
                        )
                    )
        except Exception as exc:  # settlement never breaks the loop
            logger.debug(f"job obligations were not swept for this batch: {exc}")

    def _record_unsettled_job_conflicts(self, reason="evidence_close") -> None:
        """Compatibility close hook: only physically live jobs remain conflicts.

        New runs emit `job_live_at_close`; `job_unsettled` remains replay-only
        vocabulary for legacy transcripts. A terminal-unpersisted job is
        already recorded by reconciliation and must never be projected live.
        """
        state = getattr(self, "run_evidence_state", None)
        orchestrator = getattr(self, "orchestrator", None)
        if state is None or orchestrator is None or state.sealed:
            return
        try:
            records = read_obligations(orchestrator)
            if records is None:
                self._record_job_barrier_integrity_failure(
                    ("ledger_unreadable_while_recording_unsettled_jobs",)
                )
                return
            live = [
                str(record.get("job_id") or "").strip()
                for record in records
                if process_is_live(record)
            ]
            self._record_live_jobs_at_close(live, reason)
        except Exception as exc:  # the ledger never breaks a close
            logger.debug(f"unsettled job obligations were not recorded: {exc}")

    def _append_native_observation(
        self,
        tool_call_id: Optional[str],
        observation: str,
        source_tool: Optional[str] = None,
    ) -> Optional[ReActStep]:
        """Append an observation through the preserved enrichment path, stamped
        with the tool_call it answers (None for the legacy protocol).

        `source_tool` is the tool whose execution produced this observation —
        the physical-evidence trigger. It is published on the engine rather
        than passed down, because `_add_observation_step` is a one-argument
        seam that callers (and tests) substitute."""
        self._ensure_observed_receipt_assessed(source_tool)
        self._commit_claim_transitions(source_tool)
        # Plan 8 §3.2.7: the settlement the run has not been told about yet.
        # One bounded line per settled job, on the NEXT observation, and never
        # a synthetic tool result — a receipt appearing from nowhere is the
        # surprise §7 names.
        notices = self._pending_settlement_notices()
        if notices:
            observation = "\n".join([str(observation or "").rstrip(), "", *notices]).lstrip()
            notices.clear()
        previous_source_tool = getattr(self, "_observation_source_tool", None)
        self._observation_source_tool = source_tool
        try:
            step = self._add_observation_step(observation)
        finally:
            self._observation_source_tool = previous_source_tool
        if step is not None and tool_call_id:
            step.tool_call_id = tool_call_id
        return step

    def _seal_cancelled_call(self, step: ReActStep, reason: str) -> None:
        """Answer the call the batch broke over — and record that answer.

        A phase transition being applied, a loop-driven close, a live job
        barrier: each stops the batch, and every remaining call of that
        assistant turn is answered "[not executed: ...]" so the provider's
        tool_use/tool_result pairing holds. That answer used to be the call's
        ONLY trace. No envelope, because nothing dispatched; no `tool_result`,
        because nothing ran; no `loop_decision`, because there was no execution
        to read; no refusal record and no turn. A delivered refusal with nothing
        recording it is precisely the silence spec §2.2 rule 4 forbids — and it
        was invisible to the conservation fence, which equates the events that
        EXIST, so a call emitting none of them balanced at zero on every side.

        It is a refusal, so it seals a refusal record, standing as every refusal
        does in both of the places its call never reached. It seals a turn too:
        the model asked from a rendered window and was answered, which is a
        turn whatever came of it, and its observation ref is the refusal the
        model actually read, reason and all.

        There is still no `loop_decision`, and that is the honest part: the
        recurrence ladder reads outcomes, and this call produced none.
        Fabricating one would feed the ladder an execution that never happened.
        """
        started = self._turn_stamp()
        call = self._build_tool_call_from_step(step)
        self._seal_refusal_record(
            call,
            refusal_code=CANCELLED_CALL_REFUSAL_CODE,
            params=call.raw_params,
            tool_call_id=step.tool_call_id,
        )
        observation = self._append_native_observation(
            step.tool_call_id, f"[not executed: {reason}]"
        )
        self._seal_turn_record(
            actor="model",
            t0=started,
            t1=self._turn_stamp(),
            envelope_ref=None,
            observation_ref=self._delivered_observation_ref(observation),
            # A call that ran nothing sealed no gate: the word in force belongs
            # to the turn that actually closed the phase, one row above.
            gate_decision_id=None,
            iteration=getattr(self, "current_iteration", None),
        )

    def _execute_native_calls(self, turn) -> List[ReActStep]:
        """Execute every tool call of one assistant turn, in order.

        EVERY call id gets exactly one observation — a real result, a harness
        refusal, or a cancellation — because Anthropic rejects an assistant
        tool_use that no tool_result answers (anatomy map risk 5). A phase
        signal or a loop force-break stops execution but never stops the
        answering. And every answer is a record: a cancellation the model was
        told about and the ledger was not is the silence spec §2.2 rule 4
        forbids."""
        executed: List[ReActStep] = []
        cancelled_reason: Optional[str] = None

        for call in turn.tool_calls:
            step = ReActStep(
                step_type=StepType.ACTION,
                content=call.name or "(tool call without a name)",
                tool_name=call.name,
                tool_params=dict(call.arguments),
                timestamp=self._get_timestamp(),
                model_used=turn.model_used,
                tool_call_id=call.id,
                native_text=turn.text,
            )
            self.steps.append(step)

            if cancelled_reason is not None:
                self._seal_cancelled_call(step, cancelled_reason)
                continue

            batch_break_reason = self._execute_action_step(step)
            executed.append(step)
            if batch_break_reason is not None:
                cancelled_reason = batch_break_reason
            elif self._capture_job_barrier_from_result():
                # The current call was accepted and gets its real result. Every
                # later call in the same assistant turn is answered but not
                # executed; the controller owns the live job until terminal.
                cancelled_reason = "controller job barrier active"

        # Plan 8 §3.2 trigger 1: after each executed action batch, whatever
        # tools it used. A model that spends its turns polling a log is
        # exactly the model whose job is about to finish.
        self._sweep_job_obligations()
        return executed

    def _output_refs_from_text(self, value: str) -> List[str]:
        return re.findall(r"\boutput_[A-Za-z0-9_-]+\b", value or "")

    def _dedupe_strings(self, values: List[str]) -> List[str]:
        deduped = []
        seen = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def _update_successful_states(self, tool_name: str, params: Dict[str, Any], result: ToolResult):
        """Update successful states based on tool execution results."""
        try:
            # CRITICAL FIX: Reset context switch counter when context actually switches
            # Reset on BOTH successful AND failed attempts to prevent accumulation
            if tool_name == "manage_context":
                action = params.get("action", "")
                # Include all context-changing actions
                context_changing_actions = [
                    "start_task",
                    "complete_with_results",
                    "complete_task",
                    "switch_to_trunk",
                    "create_branch",
                    "switch_to_branch",
                ]
                if action in context_changing_actions:
                    # Reset the counter regardless of success/failure
                    self.steps_since_context_switch = 0
                    if result.succeeded:
                        logger.info(
                            f"✅ Reset steps_since_context_switch counter after successful {action}"
                        )
                    else:
                        logger.info(
                            f"⚠️ Reset steps_since_context_switch counter after failed {action} attempt"
                        )

            if tool_name == "bash":
                # CRITICAL FIX: Get actual working directory from tool result metadata
                # This handles cases where bash tool had to fall back to alternative directories
                actual_working_dir = None

                # First try to get the actual working directory from metadata
                if hasattr(result, "metadata") and result.metadata:
                    actual_working_dir = result.metadata.get("working_directory")

                # Fallback to parameter if metadata not available
                if not actual_working_dir:
                    actual_working_dir = params.get("working_directory")

                if actual_working_dir:
                    # Check if working directory changed (fallback occurred)
                    original_dir = params.get("working_directory", "/workspace")
                    if actual_working_dir != original_dir:
                        # PRIORITY CHECK: Is this a workspace-related fallback?
                        if original_dir.startswith(
                            "/workspace"
                        ) and not actual_working_dir.startswith("/workspace"):
                            logger.error(
                                f"🚨 WORKSPACE FALLBACK: Failed to use {original_dir}, fell back to {actual_working_dir}"
                            )
                            logger.error(
                                f"🚨 This is a MAJOR ISSUE - projects should be in /workspace"
                            )
                            logger.error(
                                f"🚨 Clone operations may not work correctly in {actual_working_dir}"
                            )

                            # Mark this as an abnormal state
                            self.successful_states["workspace_fallback"] = True
                            self.successful_states["fallback_reason"] = (
                                f"Could not establish {original_dir}"
                            )
                        else:
                            logger.warning(
                                f"🔧 Working directory change: {original_dir} → {actual_working_dir}"
                            )

                        # CRITICAL: Update all related tools to use the new working directory
                        self._propagate_working_directory_change(actual_working_dir, original_dir)
                    else:
                        # Normal operation - workspace is working correctly
                        if actual_working_dir.startswith("/workspace"):
                            logger.debug(f"✅ Workspace operation normal: {actual_working_dir}")
                            # Clear any previous fallback flags
                            self.successful_states.pop("workspace_fallback", None)
                            self.successful_states.pop("fallback_reason", None)

                    self.successful_states["working_directory"] = actual_working_dir
                    logger.debug(f"Updated successful working directory: {actual_working_dir}")

            elif tool_name in ("maven", "build"):
                # Remember successful build working directory. The legacy maven
                # tool needed the output marker; the consolidated build tool's
                # success already reflects the backend verdict. Do not label
                # every facade success as Maven: the backend identity is an
                # explicit result fact.
                build_succeeded = (
                    result.succeeded
                    if tool_name == "build"
                    else "BUILD SUCCESS" in (result.output or "")
                )
                if build_succeeded:
                    result_facts = getattr(result, "facts", None) or {}
                    result_metadata = getattr(result, "metadata", None) or {}
                    build_system = (
                        "maven"
                        if tool_name == "maven"
                        else str(
                            result_facts.get("system") or result_metadata.get("system") or "maven"
                        )
                        .strip()
                        .lower()
                    )
                    build_workdir = str(
                        result_metadata.get("working_directory")
                        or result_facts.get("island_root")
                        or params.get("working_directory")
                        or "/workspace"
                    )
                    self.successful_states["working_directory"] = build_workdir
                    self.successful_states["build_success"] = True
                    if build_system in {"maven", "gradle", "python"}:
                        self.successful_states[f"{build_system}_success"] = True

                    # Check if the backend is working outside workspace (concerning)
                    if not build_workdir.startswith("/workspace"):
                        logger.warning(
                            f"⚠️ {build_system.title()} succeeded outside workspace: "
                            f"{build_workdir}"
                        )
                        logger.warning(f"⚠️ This may indicate workspace issues")
                    else:
                        logger.info(
                            f"✅ {build_system.title()} success in workspace: {build_workdir}"
                        )

                    logger.info(
                        f"{build_system.title()} success recorded for directory: {build_workdir}"
                    )

            elif tool_name in ("project_setup", "project"):
                # Remember cloned repositories and project type. The project
                # facade documents repo_url; its delegate uses repository_url.
                repo_url = params.get("repository_url") or params.get("repo_url")
                if repo_url:
                    self.successful_states["cloned_repos"].add(repo_url)
                    logger.debug(f"Recorded cloned repo: {repo_url}")

                    # Set working directory based on cloned repository
                    if params.get("action") == "clone":
                        repo_name = repo_url.split("/")[-1].replace(".git", "")

                        # PRIORITY: Always try to clone in /workspace first
                        if self.successful_states.get("workspace_fallback"):
                            # We're in fallback mode - this is not ideal for cloning
                            current_workdir = self.successful_states.get(
                                "working_directory", "/root"
                            )
                            clone_dir = f"{current_workdir}/{repo_name}"
                            logger.error(f"🚨 CLONING IN FALLBACK LOCATION: {clone_dir}")
                            logger.error(f"🚨 This is SUBOPTIMAL - prefer /workspace for projects")
                        else:
                            # Normal case - clone in workspace
                            clone_dir = f"/workspace/{repo_name}"
                            logger.info(f"✅ Cloning in proper workspace location: {clone_dir}")

                        self.successful_states["working_directory"] = clone_dir
                        logger.info(f"Updated working directory after clone: {clone_dir}")

                # Check for project type detection in output
                output = result.output or ""
                if "maven" in output.lower() or "pom.xml" in output.lower():
                    self.successful_states["project_type"] = "maven"
                    logger.debug("Detected Maven project type")
                elif "gradle" in output.lower() or "build.gradle" in output.lower():
                    self.successful_states["project_type"] = "gradle"
                    logger.debug("Detected Gradle project type")

            elif tool_name == "report":
                snapshot = {}
                if hasattr(result, "metadata") and result.metadata:
                    snapshot = result.metadata.get("report_snapshot") or {}
                if snapshot:
                    self.successful_states["report_snapshot"] = dict(snapshot)
                    logger.debug("Stored report snapshot for completion guidance")

        except Exception as e:
            logger.warning(f"Failed to update successful states: {e}")

    def _propagate_working_directory_change(self, new_workdir: str, old_workdir: str):
        """
        Propagate working directory changes to ensure consistency across all tools.

        When bash tool falls back to a different directory, we need to update
        Agent's understanding of where the project is located.
        """
        try:
            logger.info(f"📁 Propagating working directory change: {old_workdir} → {new_workdir}")

            # Update successful states
            self.successful_states["working_directory"] = new_workdir

            # PRIORITY CHECK: Warn about workspace fallbacks
            if old_workdir.startswith("/workspace") and not new_workdir.startswith("/workspace"):
                logger.error(
                    f"🚨 WORKSPACE LOST: Propagating fallback from {old_workdir} to {new_workdir}"
                )
                logger.error(f"🚨 Future clone operations will be affected")
                logger.error(f"🚨 Consider fixing the underlying workspace issue")

                # Mark this propagation as problematic
                self.successful_states["workspace_fallback"] = True
                self.successful_states["fallback_reason"] = f"Propagated from failed {old_workdir}"
            elif new_workdir.startswith("/workspace"):
                logger.info(f"✅ Workspace propagation successful: {new_workdir}")
                # Clear fallback flags if we're back in workspace
                self.successful_states.pop("workspace_fallback", None)
                self.successful_states.pop("fallback_reason", None)

            # If we have cloned repositories, we might need to adjust their paths
            if self.successful_states.get("cloned_repos"):
                logger.info(
                    f"📁 Note: Cloned repositories may need path adjustment for new working directory"
                )

                # If we're falling back from workspace, this is a major concern
                if self.successful_states.get("workspace_fallback"):
                    logger.error(
                        f"🚨 CRITICAL: Cloned repositories were in workspace, now using {new_workdir}"
                    )
                    logger.error(
                        f"🚨 Project files may be in /workspace but operations will run in {new_workdir}"
                    )

            # Log for debugging
            logger.debug(f"📁 Agent state updated - new working directory: {new_workdir}")
            logger.debug(
                f"📁 All future operations will use this directory unless explicitly overridden"
            )

        except Exception as e:
            logger.error(f"Failed to propagate working directory change: {e}")

    def _track_tool_execution(self, tool_signature: str, result: ToolResult):
        """Track tool execution to detect repetitive patterns."""
        execution_info = ToolExecutionRecord(
            signature=tool_signature,
            invocation_status=result.invocation_status,
            operation_outcome=result.operation_outcome,
            timestamp=self._get_timestamp(),
        )

        self.recent_tool_executions.append(execution_info)

        # Keep only recent executions to prevent memory bloat
        if len(self.recent_tool_executions) > self.max_recent_executions:
            self.recent_tool_executions.pop(0)

    def _add_observation_step(
        self, observation: str, source_tool: Optional[str] = None
    ) -> ReActStep:
        """Add an observation step, enriched with physical validation state.

        Enrichment is triggered by evidence, never by wording: the probe runs
        iff this observation answers a build-family execution. `source_tool`
        names that tool; when it is omitted the engine falls back to the tool
        `_execute_action_step` is currently answering, so the one-argument
        call shape of this seam is preserved.

        Returns the appended step so a native caller can stamp the
        `tool_call_id` it answers."""
        if source_tool is None:
            source_tool = getattr(self, "_observation_source_tool", None)
        if source_tool in self._BUILD_EVIDENCE_TOOLS:
            physical_state = self._get_physical_validation_state(observation)

            # Enrich observation with physical state if available
            if physical_state:
                observation = self._enrich_observation_with_physical_state(
                    observation, physical_state
                )

        obs_step = ReActStep(
            step_type=StepType.OBSERVATION, content=observation, timestamp=self._get_timestamp()
        )
        self.steps.append(obs_step)

        # FIXED: Only log once to prevent duplicate output in logs
        # Use logger.info for main logging, agent_logger for internal tracking only
        logger.info(f"👁️ OBSERVATION: {observation}")

        # Emit UI event for observation
        self.emit(
            EventType.AGENT_OBSERVATION,
            message=observation[:200]
            + ("..." if len(observation) > 200 else ""),  # Truncate for display
            step_num=self.current_iteration,
        )

        return obs_step

    def _get_timestamp(self) -> str:
        """Get current timestamp string."""
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _log_react_step_verbose(self, step: ReActStep):
        """Log detailed ReAct step information in verbose mode."""

        verbose_logger = create_verbose_logger("react_steps")

        step_entry = {
            "event": "react_step",
            "step_type": step.step_type,
            "iteration": self.current_iteration,
            "step_number": len(self.steps),
            "model_used": step.model_used,
            "content_length": len(step.content),
            "content": step.content,
            "tool_name": step.tool_name,
            "tool_params": step.tool_params,
            "timestamp": step.timestamp,
        }

        verbose_logger.info(f"📝 REACT STEP: {json.dumps(step_entry, indent=2, default=str)}")

    def _get_physical_validation_state(self, observation: str) -> Optional[Dict[str, any]]:
        """
        Get physical validation state for a build-family observation.

        Callers gate this probe on the executing tool (`_add_observation_step`),
        so the observation text is never screened for keywords here: a build
        that reports neutrally is still build evidence.

        Args:
            observation: The observation text

        Returns:
            Physical validation state dict or None
        """
        try:
            # Get project name from context or use default
            project_name = None
            if hasattr(self.context_manager, "project_name"):
                project_name = self.context_manager.project_name

            # Run physical validation
            validation_result = self.physical_validator.validate_build_artifacts(project_name)

            return validation_result

        except Exception as e:
            logger.warning(f"Physical validation failed: {e}")
            return None

    def _enrich_observation_with_physical_state(
        self, observation: str, physical_state: Dict[str, any]
    ) -> str:
        """
        Enrich observation with physical validation facts.

        Args:
            observation: Original observation text
            physical_state: Physical validation state dict

        Returns:
            Enriched observation text
        """
        # Build physical evidence summary
        evidence_lines = []

        if physical_state.get("class_files", 0) > 0:
            evidence_lines.append(
                f"[PHYSICAL EVIDENCE: {physical_state['class_files']} .class files exist]"
            )
        else:
            evidence_lines.append(
                "[PHYSICAL EVIDENCE: No .class files found - compilation may have failed]"
            )

        if physical_state.get("jar_files", 0) > 0:
            evidence_lines.append(
                f"[PHYSICAL EVIDENCE: {physical_state['jar_files']} JAR files exist]"
            )

        if physical_state.get("missing_classes"):
            count = len(physical_state["missing_classes"])
            evidence_lines.append(
                f"[PHYSICAL EVIDENCE: {count} Java files have no corresponding .class files]"
            )

        # Add evidence to observation
        if evidence_lines:
            return observation + "\n" + "\n".join(evidence_lines)

        return observation

    def _log_tool_result_verbose(self, tool_name: str, result):
        """Log detailed tool result information in verbose mode."""

        verbose_logger = create_verbose_logger("react_tools")

        result_entry = {
            "event": "tool_execution_result",
            "tool_name": tool_name,
            "iteration": self.current_iteration,
            "succeeded": result.succeeded,
            "invocation_status": result.invocation_status.value,
            "operation_outcome": result.operation_outcome.value,
            "evidence_status": result.evidence_status.value,
            "output_length": len(result.output) if result.output else 0,
            "full_output": result.output,  # Show full output instead of preview
            "error": result.error if hasattr(result, "error") else None,
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.info(f"🔧 TOOL RESULT: {json.dumps(result_entry, indent=2, default=str)}")

        # Save full tool output to container file if we have access
        if (
            hasattr(self.context_manager, "orchestrator")
            and self.context_manager.orchestrator
            and result.output
        ):
            output_file = f"/workspace/.setup_agent/tool_traces/iteration_{self.current_iteration}_{tool_name}_output.txt"
            escaped_output = result.output.replace("'", "'\"'\"'")
            self.context_manager.orchestrator.execute_command(
                f"mkdir -p /workspace/.setup_agent/tool_traces && echo '{escaped_output}' > {output_file}"
            )

    def get_execution_summary(self) -> Dict[str, Any]:
        """Get a summary of the execution.

        Counts the live window PLUS any windows archived at phase resets, so
        phase-mode summaries reflect the whole run, not the last phase only."""
        thinking_actions = len([s for s in self.steps if s.model_used and "o1" in s.model_used])
        action_actions = len(
            [s for s in self.steps if s.model_used and "o1" not in (s.model_used or "")]
        )

        archived = getattr(self, "_archived_counts", None) or {}

        # Runtime metadata for the web read model (graceful when config absent).
        config = getattr(self, "config", None)
        model_name = None
        if config is not None:
            getter = getattr(config, "get_litellm_model_name", None)
            if callable(getter):
                model_name = getter("action")
        max_iterations = getattr(self, "max_iterations", None) or getattr(
            config, "max_iterations", None
        )

        # Cumulative per-tool usage: archived windows + the live window. Without
        # this the report's Tool Usage reflects only the post-compaction window.
        tools_used = dict(archived.get("tools_used", {}))
        tool_failures = dict(archived.get("tool_failures", {}))
        for s in self.steps:
            if s.step_type != StepType.ACTION:
                continue
            tool_name = getattr(s, "tool_name", None)
            if not tool_name:
                continue
            tools_used[tool_name] = tools_used.get(tool_name, 0) + 1
            result = getattr(s, "tool_result", None)
            if result is not None and result.operation_outcome is OperationOutcome.FAILED:
                tool_failures[tool_name] = tool_failures.get(tool_name, 0) + 1

        return {
            "model": model_name,
            "max_iterations": max_iterations,
            "tools_used": tools_used,
            "tool_failures": tool_failures,
            "total_steps": len(self.steps) + archived.get("total_steps", 0),
            "iterations": self.current_iteration,
            "thoughts": len([s for s in self.steps if s.step_type == StepType.THOUGHT])
            + archived.get("thoughts", 0),
            "actions": len([s for s in self.steps if s.step_type == StepType.ACTION])
            + archived.get("actions", 0),
            "observations": len([s for s in self.steps if s.step_type == StepType.OBSERVATION])
            + archived.get("observations", 0),
            "thinking_model_calls": thinking_actions,
            "action_model_calls": action_actions,
            "successful_actions": len(
                [
                    s
                    for s in self.steps
                    if s.step_type == StepType.ACTION and s.tool_result and s.tool_result.succeeded
                ]
            )
            + archived.get("successful_actions", 0),
            "failed_actions": len(
                [
                    s
                    for s in self.steps
                    if s.step_type == StepType.ACTION
                    and s.tool_result
                    and s.tool_result.operation_outcome is OperationOutcome.FAILED
                ]
            )
            + archived.get("failed_actions", 0),
        }

    @staticmethod
    def _normalize_guidance_priority(priority: Any) -> int:
        """Convert guidance priority labels to the numeric scale used for display."""
        if isinstance(priority, str):
            priority_label = priority.strip().lower()
            return {
                "critical": 9,
                "high": 8,
                "important": 8,
                "normal": 5,
                "medium": 5,
                "low": 3,
            }.get(priority_label, 5)

        return priority

    def _add_system_guidance(self, guidance_message: str, priority: int | str = 5):
        """
        Add system guidance with priority handling.
        Higher priority messages are more prominent.
        """
        priority = self._normalize_guidance_priority(priority)

        # Add visual emphasis based on priority
        if priority >= 9:
            prefix = "🚨 CRITICAL GUIDANCE"
        elif priority >= 7:
            prefix = "⚠️ IMPORTANT GUIDANCE"
        else:
            prefix = "💡 SYSTEM GUIDANCE"

        full_message = f"{prefix} (Priority: {priority}):\n{guidance_message}"

        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=full_message,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)

        self.agent_logger.info(f"{prefix}: {guidance_message[:100]}...")
        logger.info(f"{prefix} added with priority {priority}")

    def _export_token_usage_csv(self):
        """Export token usage to CSV file when ReAct loop completes."""
        try:
            # Get session logger for CSV path
            from sag.config.logger import get_session_logger

            session_logger = get_session_logger()

            if session_logger:
                # Save to session directory
                csv_path = session_logger.session_log_dir / "token_usage.csv"
            else:
                # Fallback to logs directory
                logs_dir = Path("logs")
                logs_dir.mkdir(exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_path = logs_dir / f"token_usage_{timestamp}.csv"

            # Export the CSV
            success = self.token_tracker.export_to_csv(str(csv_path))

            if success:
                # Log summary stats
                self.token_tracker.log_summary()
                logger.info(f"📊 Token usage exported to: {csv_path}")
            else:
                logger.warning("Failed to export token usage CSV")

        except Exception as e:
            logger.warning(f"Failed to export token usage CSV: {e}")
