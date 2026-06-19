"""ReAct Engine for Setup-Agent (SAG)."""

import json
import re
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from sag.config import create_agent_logger, create_verbose_logger, get_config
from sag.config.prompt_loader import load_react_engine_prompts
from sag.config.settings import effective_phase_floor
from sag.evidence import EvidenceStatus, coerce_evidence_status
from sag.reporting import render_condensed_summary
from sag.tools.base import BaseTool, ToolResult
from sag.ui.events import EventType, UIEvent, UIEventEmitter

from .agent_state_evaluator import AgentStateEvaluator
from .attempt_ledger import compact_steps
from .context_manager import ContextManager, TaskStatus
from .output_storage import OutputStorageManager
from .phase_machine import PHASE_NAMES, PhaseMachine
from .physical_validator import PhysicalValidator
from .react_llm import ReactLLMClient
from .react_prompt_builder import ReActPromptBuilder
from .react_response_parser import ReActResponseParser
from .react_types import ReactModelMode, ReActStep, StepType
from .token_tracker import TokenTracker
from .tool_orchestration import (
    ToolCall,
    ToolExecution,
    ToolLifecycleEvent,
    ToolOrchestrator,
)

# Per-phase objectives for the setup phase machine (spec §3.1). These
# prescribe TOOLS, never raw commands — task text outranks prompt guidance
# (round-4 lesson), so the only safe vocabulary here is the tool surface.
PHASE_OBJECTIVES = {
    "provision": (
        "Get the repository cloned and the toolchain installed: "
        "project(action='clone', repo_url=...), then project(action='provision', ...) "
        "for the JDK the project needs. Claim phase(action='done') with what was installed."
    ),
    "analyze": (
        "Understand the project: project(action='analyze'). Record build system, the "
        "analyzer's Recommended Build (target dir + goal), test counts, and special "
        "requirements in key_results. An honest 'unknown' with evidence is acceptable."
    ),
    "build": (
        "Make the project compile: build(action='compile'). Follow the analyzer's "
        "Recommended Build when it differs from a plain root compile — an aggregator "
        "root over Groovy modules needs build(action='package'/'install'), and a "
        "Gradle-primary project needs the Gradle build. If the analyzer reports NO Java "
        "compile target (a packaging/meta-project), phase(action='blocked') with that "
        "evidence instead of forcing a compile. If compilation fails on missing "
        "dependencies, build(action='deps') can resolve them — but do not run deps "
        "first by default (multi-module reactors can fail dependency resolution while "
        "compiling fine). Never run mvn/gradle via bash — build resolves the "
        "registered toolchain. Long builds detach; poll the job ref with search."
    ),
    "test": (
        "Run the test suite: build(action='test'). Run it in the analyzer's "
        "Recommended Tests target (the tests can live in a different module — and "
        "even a different build system — than the build, e.g. Gradle test modules "
        "beside a Maven build); otherwise use the build root. Partial pass above "
        "threshold is a valid outcome — report the numbers honestly in key_results. "
        "If tests genuinely cannot run, phase(action='blocked') with evidence."
    ),
    "report": "Generate the final report with the report tool, then phase(action='done').",
}


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
    ):
        super().__init__()  # Initialize UIEventEmitter
        self.context_manager = context_manager
        self.tools = {tool.name: tool for tool in tools}
        self.config = get_config()

        # Engine-owned phase machine for setup runs (spec §3.1). None keeps the
        # legacy free-form behavior (`sag run --task` passes neither).
        self.phase_machine = phase_machine
        self.context_journal = context_journal
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
        self._force_thinking_next = False

        # CRITICAL: Flag to force thinking after successful tool execution
        self._force_thinking_after_success = False

        # State memory for successful operations
        self.successful_states = {
            "working_directory": None,  # Last successful working directory
            "cloned_repos": set(),  # Set of successfully cloned repo URLs
            "project_type": None,  # Detected project type
            "maven_success": False,  # Whether maven operations succeeded
            "excluded_modules": set(),
            "excluded_tests": set(),
            "report_snapshot": None,
        }

        # Agent logger for detailed traces
        self.agent_logger = create_agent_logger("react_engine")

        # Initialize the centralized state evaluator (will be updated with physical validator after initialization)
        self.state_evaluator = AgentStateEvaluator(self.context_manager)

        # Initialize output storage manager
        from pathlib import Path

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

        # Initialize physical validator for fact-based validation
        self.physical_validator = PhysicalValidator(
            docker_orchestrator=orchestrator,
            project_path="/workspace",
            test_pass_threshold=self.config.test_pass_threshold,
            build_coverage_threshold=self.config.build_coverage_threshold,
            test_execution_threshold=self.config.test_execution_threshold,
        )
        # Share the validator with the context manager so ContextTool's
        # completion-evidence gate reuses it (probe cache + threshold) instead
        # of constructing a fresh one per completion attempt.
        if getattr(self.context_manager, "physical_validator", None) is None:
            self.context_manager.physical_validator = self.physical_validator

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

        # Update state evaluator with physical validator
        self.state_evaluator.physical_validator = self.physical_validator

        # Stage 2: in machine-driven setup runs the evaluator must never end
        # the run from the report tool's completion signal — the report PHASE
        # done does (run_react_loop returns on machine completion with the
        # machine's honest overall outcome).
        self.state_evaluator.phase_machine_active = self.phase_machine is not None

        # Initialize token tracker and LLM client for monitoring model usage
        self.token_tracker = TokenTracker()
        self.llm_client = ReactLLMClient(
            config=self.config,
            tools=self.tools,
            token_tracker=self.token_tracker,
            trace_context=lambda: {
                "iteration": self.current_iteration,
                "timestamp": self._get_timestamp(),
                "agent_logger": self.agent_logger,
            },
        )
        self.llm_client.setup()
        self.response_parser = ReActResponseParser(timestamp_factory=self._get_timestamp)

        logger.info(
            "ReAct Engine initialized with dual model support, physical validation, and token tracking"
        )
        logger.info(f"Thinking model: {self.config.get_litellm_model_name('thinking')}")
        logger.info(f"Action model: {self.config.get_litellm_model_name('action')}")
        if repository_url:
            logger.info(f"Repository URL: {repository_url}")
        if repository_ref:
            logger.info(f"Repository ref: {repository_ref}")

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

    def _phase_intro_step(self) -> ReActStep:
        """The clean-window digest that opens every phase (GTD reset): goal
        picture so far, the new phase's objective (tools, never raw commands),
        and the flexible budget note."""
        machine = self.phase_machine
        phase = machine.current_phase
        _, reserved, remaining = self._phase_budget_numbers(phase)
        budget = max(5, remaining - reserved)
        lines = [
            f"=== PHASE: {phase.upper()} ===",
            "Run picture so far:",
            *machine.digest_lines(),
            "",
            f"Objective: {PHASE_OBJECTIVES[phase]}",
            f"Budget: flexible — up to ~{budget} iterations available (a small reserve is "
            f"kept for later phases). When finished, call phase(action='done', "
            f"key_results=..., evidence=[refs]). If it cannot be finished, "
            f"phase(action='blocked', reason=..., evidence=[refs]).",
        ]
        # Surface the analyzer's build recommendation directly in the build/test
        # intro so the target/goal is present even if the model didn't carry it
        # forward in analyze key_results (Bigtop: compile the right reactor/module,
        # or block honestly on a meta-project — don't compile an empty root).
        if phase in ("build", "test"):
            rec_line = self._recommended_build_line(phase)
            if rec_line:
                lines.insert(lines.index(f"Objective: {PHASE_OBJECTIVES[phase]}") + 1, rec_line)
        return ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content="\n".join(lines),
            timestamp=self._get_timestamp(),
        )

    def _recommended_build_line(self, phase: str = "build") -> Optional[str]:
        """One-line build/test recommendation from the analyzer, read from the
        trunk's environment_summary. Best-effort: any failure yields no line."""
        try:
            trunk = self.context_manager.load_trunk_context()
            rec = (getattr(trunk, "environment_summary", None) or {}).get("build_recommendation")
        except Exception:
            return None
        if not rec:
            return None
        if phase == "test":
            test_root = rec.get("test_root")
            if not test_root:
                return None
            # Only worth calling out when the tests are NOT where we built.
            if test_root == rec.get("build_root") and rec.get("test_system") == rec.get(
                "build_system"
            ):
                return None
            return (
                f"Recommended Tests: run {rec.get('test_system')} 'test' in {test_root} "
                "— the test suite lives here, not in the build module."
            )
        if rec.get("is_aggregator_only"):
            return (
                f"Recommended Build: NONE — {rec.get('rationale', '')} "
                "Use phase(action='blocked') with this evidence rather than forcing a compile."
            )
        return (
            f"Recommended Build: {rec.get('build_system')} '{rec.get('goal')}' in "
            f"{rec.get('build_root')} — {rec.get('rationale', '')}"
        )

    def _handle_phase_signals(self, executed_steps) -> Optional[str]:
        """Advance the machine when the phase tool signalled done/blocked.

        The ENGINE is the single owner of phase state: the tool only emits
        `metadata.phase_signal`; this method mutates the machine, persists the
        phase record to the trunk, and performs the window reset."""
        if getattr(self, "phase_machine", None) is None:
            return None
        for step in executed_steps:
            result = getattr(step, "tool_result", None)
            metadata = getattr(result, "metadata", None) or {}
            signal = metadata.get("phase_signal")
            if not signal:
                continue
            machine = self.phase_machine
            finished = machine.current_phase
            if signal == "note":
                self._persist_phase_note(finished, metadata.get("text", ""))
                return signal
            if signal == "done":
                machine.mark_done(metadata.get("key_results", ""), metadata.get("evidence", []))
                self._persist_phase_record(finished, "completed", metadata.get("key_results", ""))
            else:
                machine.mark_blocked(metadata.get("reason", ""), metadata.get("evidence", []))
                self._persist_phase_record(finished, "failed", metadata.get("reason", ""))
            self._phase_iterations = 0
            # Phase transitions are the context switches of phase mode (no
            # manage_context actions exist to reset the legacy counter).
            self.steps_since_context_switch = 0
            if not machine.is_complete:
                self._archive_window_steps()
                self.steps = [self._phase_intro_step()]
                self._journal_intro_dirty = True
                self._journal_last_ledger = None
                self._start_phase_branch()
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
                    if getattr(result, "success", False):
                        counts["successful_actions"] += 1
                    else:
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

    def _phase_gate_check(self, phase: str) -> Dict[str, Any]:
        """Run the phase-boundary evidence gate from engine context.

        Fails CLOSED (ok=False) when no validator is wired: the callers
        (floor auto-done, mid-phase nudge) must only act on positive
        evidence, never on inability to check."""
        validator = getattr(self, "physical_validator", None)
        if validator is None:
            return {"ok": False, "reason": "no validator available", "suggestions": []}
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
        )

    NUDGE_EVERY = 15

    def _maybe_nudge_phase_done(self) -> bool:
        """Mid-phase evidence nudge (round-5 vfs lesson): a model deep in a
        rabbit hole may hold green evidence for dozens of iterations without
        claiming done. Every NUDGE_EVERY phase-iterations, check the gate;
        when it would pass, say so — break loops with evidence, not limits."""
        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.is_complete:
            return False
        if self._phase_iterations <= 0 or self._phase_iterations % self.NUDGE_EVERY != 0:
            return False
        gate = self._phase_gate_check(machine.current_phase)
        if not gate.get("ok"):
            return False
        self.steps.append(
            ReActStep(
                step_type=StepType.SYSTEM_GUIDANCE,
                content=(
                    f"EVIDENCE CHECK: the completion gate for phase '{machine.current_phase}' "
                    f"already passes on physical evidence. If you agree the objective is met, "
                    f"claim phase(action='done', key_results=..., evidence=[refs]) now and move on. "
                    f"If you are pursuing something beyond this phase's objective, consider "
                    f"whether it belongs to a later phase or a note."
                ),
                timestamp=self._get_timestamp(),
            )
        )
        return True

    def _enforce_phase_floors(self) -> bool:
        """Force-finish the current phase ONLY when continuing would starve the
        minimum floors of later phases (no per-phase quotas: build may consume
        everything the easy phases saved). Round-5 vfs lesson: consult the
        evidence gate first — green evidence auto-completes the phase as done;
        only a failing gate records blocked."""
        machine = getattr(self, "phase_machine", None)
        if machine is None or machine.is_complete:
            return False
        phase = machine.current_phase
        _, reserved, remaining = self._phase_budget_numbers(phase)
        if remaining > reserved:
            return False

        gate = self._phase_gate_check(phase)
        if gate.get("ok"):
            machine.mark_done(
                f"auto-completed at floor exhaustion (remaining {remaining} iterations "
                f"reserved for later phases); evidence gate passed",
                [],
            )
            self._persist_phase_record(
                phase, "completed", "auto-completed at floor exhaustion; evidence gate passed"
            )
        else:
            machine.mark_blocked(
                f"remaining {remaining} iterations are reserved for later phases "
                f"({reserved} needed)",
                [],
            )
            self._persist_phase_record(
                phase, "failed", "remaining iterations reserved for later phases"
            )
        self._phase_iterations = 0
        self.steps_since_context_switch = 0
        if not machine.is_complete:
            self._archive_window_steps()
            self.steps = [self._phase_intro_step()]
            self._journal_intro_dirty = True
            self._journal_last_ledger = None
            self._start_phase_branch()
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
            builder = getattr(self, "prompt_builder", None)
            if builder is not None:
                builder.invalidate_trunk_cache()
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
                builder = getattr(self, "prompt_builder", None)
                if builder is not None:
                    builder.invalidate_trunk_cache()
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

            description = PHASE_OBJECTIVES.get(machine.current_phase, "")
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

    def run_react_loop(
        self,
        initial_prompt: str,
        max_iterations: Optional[int] = None,
        completion_mode: str = "setup",
    ) -> bool:
        """Run the main ReAct loop."""
        max_iter = max_iterations or self.max_iterations
        self._run_max_iterations = max_iter

        self.agent_logger.info(f"Starting ReAct loop with max {max_iter} iterations")

        # Setup-mode phase machine: the engine drives provision→…→report and the
        # model signals with the phase tool. None (run-task mode) = legacy path.
        phase_mode = completion_mode == "setup" and self.phase_machine is not None

        # Initialize with the initial prompt. In phase mode the window opens on
        # the phase intro digest instead of empty.
        self.current_iteration = 0
        self._phase_iterations = 0
        if phase_mode:
            self.steps = [self._phase_intro_step()]
            self._journal_intro_dirty = True
            self._journal_last_ledger = None
            self._start_phase_branch()
        else:
            self.steps = []

        # PERFORMANCE: Initialize trunk context cache at start
        self.prompt_builder.invalidate_trunk_cache()  # Ensure fresh start

        # Start with initial thought using thinking model
        current_prompt = (
            self.prompt_builder.build_initial_system_prompt(
                repository_url=self.repository_url,
                repository_ref=self.repository_ref,
                tool_calling_enabled=self.llm_client.capabilities_for(
                    ReactModelMode.ACTION
                ).supports_function_calling,
                workflow_mode=completion_mode,
            )
            + "\n\n"
            + initial_prompt
        )

        previous_completion_mode = self.state_evaluator.completion_mode
        self.state_evaluator.completion_mode = completion_mode

        run_started_at = time.time()
        wall_clock_cap = getattr(self.config, "max_wall_clock_seconds", 7200)

        try:
            while self.current_iteration < max_iter:
                if wall_clock_exceeded(run_started_at, wall_clock_cap):
                    elapsed = time.time() - run_started_at
                    logger.warning(
                        f"ReAct loop stopped: global wall-clock cap of {wall_clock_cap}s "
                        f"reached after {elapsed:.0f}s / {self.current_iteration} iterations"
                    )
                    self._export_token_usage_csv()
                    return False

                # FLOOR RESERVATIONS (phase mode): force-block the current
                # phase only when continuing would starve later phases' floors,
                # guaranteeing the run always reaches report and ends honestly.
                if phase_mode and self._enforce_phase_floors() and self.phase_machine.is_complete:
                    self._export_token_usage_csv()
                    return self.phase_machine.overall_outcome() == "success"

                self.current_iteration += 1
                self._phase_iterations += 1
                self.agent_logger.info(f"ReAct iteration {self.current_iteration}/{max_iter}")

                # Update token tracker with current iteration
                self.token_tracker.set_iteration(self.current_iteration)

                # Determine if this should be a thinking step or action step
                is_thinking_step = self._should_use_thinking_model()
                mode = ReactModelMode.THINKING if is_thinking_step else ReactModelMode.ACTION

                # Get LLM response
                wrapped_prompt = self.prompt_builder.build_mode_prompt(
                    current_prompt, mode, workflow_mode=completion_mode
                )
                response = self.llm_client.get_response(wrapped_prompt, mode)

                if not response:
                    logger.error("Failed to get LLM response")
                    # Export token usage before early return due to failed LLM response
                    self._export_token_usage_csv()
                    return False

                # Parse the response
                model_used = self.llm_client.capabilities_for(mode).model
                parsed_steps = self.response_parser.parse(
                    response,
                    model_used=model_used,
                    was_thinking_model=is_thinking_step,
                )

                if not parsed_steps:
                    logger.warning("No valid steps parsed from LLM response")
                    logger.warning(f"Raw response was: {repr(response)}")
                    continue

                # Execute the steps
                self._execute_steps(parsed_steps)

                # PHASE SIGNALS (phase mode): a phase tool done/blocked advances
                # the machine, persists the record, and resets the window. When
                # the report phase ends, the machine — not the evaluator — ends
                # the run with its honest overall outcome.
                if phase_mode:
                    self._handle_phase_signals(parsed_steps)
                    if self.phase_machine.is_complete:
                        outcome = self.phase_machine.overall_outcome()
                        self.agent_logger.info(f"All phases complete; overall outcome: {outcome}")
                        self._export_token_usage_csv()
                        return outcome == "success"
                    # Mid-phase evidence nudge: when the gate already passes,
                    # tell the model — break rabbit holes with evidence.
                    self._maybe_nudge_phase_done()

                # CENTRALIZED STATE EVALUATION: Replace all scattered checks
                state_analysis = self.state_evaluator.evaluate(
                    steps=self.steps,
                    current_iteration=self.current_iteration,
                    recent_tool_executions=self.recent_tool_executions,
                    steps_since_context_switch=self.steps_since_context_switch,
                )

                # Handle guidance based on state analysis
                if state_analysis.needs_guidance:
                    self._add_system_guidance(
                        state_analysis.guidance_message, state_analysis.guidance_priority
                    )

                # Check for task completion
                if state_analysis.is_task_complete:
                    self.agent_logger.info("Task completed successfully")
                    # Export token usage before successful completion
                    self._export_token_usage_csv()
                    return True

                # NO-PHYSICAL-PROGRESS GUARD: when a task completed this
                # iteration (a successful complete_with_results) but the run has
                # produced no build artifacts across several tasks, stop instead
                # of thrashing to the iteration cap.
                completed_task_this_iteration = any(
                    step.step_type == StepType.ACTION
                    and step.tool_name == "manage_context"
                    and (step.tool_params or {}).get("action") == "complete_with_results"
                    and step.tool_result is not None
                    and step.tool_result.success
                    for step in parsed_steps
                )
                if completed_task_this_iteration and self._check_progress_after_task():
                    logger.warning(
                        "ReAct loop stopped: no build progress after repeated completed tasks"
                    )
                    # Export token usage before no-progress completion
                    self._export_token_usage_csv()
                    return False

                # DEPRECATED: Legacy checks now handled by state_evaluator
                # Check for context switching guidance
                # self._check_context_switching_guidance()

                # Check if model needs explicit action guidance
                # if self._needs_action_guidance():
                #     self._add_action_guidance()

                # ATTEMPT-LEDGER COMPACTION (phase mode): old steps collapse to
                # one line each behind the phase intro; exactly one ledger step
                # exists at a time (position 1, right after the intro).
                ledger = None
                n_compacted = 0
                if phase_mode and len(self.steps) > 1:
                    tail = self.steps[1:]
                    ledger, kept = compact_steps(tail, keep_recent=30)
                    if ledger is not None:
                        ledger_step = ReActStep(
                            step_type=StepType.SYSTEM_GUIDANCE,
                            content=ledger,
                            timestamp=self._get_timestamp(),
                        )
                        kept_clean = [
                            s
                            for s in kept
                            if "ATTEMPT LEDGER" not in (getattr(s, "content", "") or "")
                        ]
                        n_compacted = len(tail) - len(kept_clean)
                        self.steps = [self.steps[0], ledger_step] + kept_clean

                # Build prompt for next iteration
                current_prompt = self.prompt_builder.build_next_prompt(
                    steps=self.steps,
                    repository_url=self.repository_url,
                    repository_ref=self.repository_ref,
                    tool_calling_enabled=self.llm_client.capabilities_for(
                        ReactModelMode.ACTION
                    ).supports_function_calling,
                    successful_states=self.successful_states,
                    workflow_mode=completion_mode,
                    phase_mode=phase_mode,
                )

                # CONTEXT JOURNAL (phase mode): one in-container line per
                # iteration describing the window composition (spec §7).
                if phase_mode:
                    self._record_context_journal(
                        ledger, n_compacted, len(parsed_steps), len(current_prompt)
                    )

                # Step count is now automatically managed by branch history updates
                # No manual step increment needed in new design

                # FIX: Only increment counter when actual work (ACTION steps) was done
                # Don't count pure thinking steps toward context switch threshold
                if parsed_steps and any(step.step_type == StepType.ACTION for step in parsed_steps):
                    self.steps_since_context_switch += 1
                    logger.debug(
                        f"Incremented steps_since_context_switch to {self.steps_since_context_switch} after ACTION step"
                    )

            logger.warning(f"ReAct loop completed without success after {max_iter} iterations")
            # Export token usage before max iterations completion
            self._export_token_usage_csv()
            return False

        except Exception as e:
            logger.error(f"ReAct loop failed: {e}", exc_info=True)
            # Export token usage before exception completion
            self._export_token_usage_csv()
            return False
        finally:
            self.state_evaluator.completion_mode = previous_completion_mode

    def _should_use_thinking_model(self) -> bool:
        """Determine if we should use the thinking model for this step - ENFORCE REACT ARCHITECTURE."""
        # CRITICAL: Check if thinking model was requested after successful tool execution
        if self._force_thinking_after_success:
            self._force_thinking_after_success = False  # Reset the flag
            logger.info("Using thinking model to analyze successful tool execution results")
            return True

        # Check if thinking model was explicitly requested due to repetitive execution
        if self._force_thinking_next:
            self._force_thinking_next = False  # Reset the flag
            logger.info("Using thinking model due to repetitive execution detection")
            return True

        # CRITICAL: ReAct Architecture Enforcement
        # Thinking model = ANALYSIS and PLANNING (after observations)
        # Action model = EXECUTION (after thinking)

        # Always start with thinking model for initial analysis
        if len(self.steps) == 0:
            logger.info("Using thinking model for initial analysis")
            return True

        # ENFORCE PROPER REACT SEQUENCE: OBSERVATION → THINKING → ACTION → OBSERVATION
        last_step = self.steps[-1] if self.steps else None

        if last_step and last_step.step_type == StepType.OBSERVATION:
            # After observation, always analyze with thinking model
            logger.info("Using thinking model to analyze observation results")
            return True

        if last_step and last_step.step_type == StepType.THOUGHT:
            # After thinking, switch to action model for execution
            logger.info("Switching to action model for tool execution after analysis")
            return False

        # Use thinking model when we encounter errors (need analysis)
        recent_steps = self.steps[-3:] if len(self.steps) >= 3 else self.steps
        recent_errors = [
            s
            for s in recent_steps
            if s.step_type == StepType.ACTION and s.tool_result and not s.tool_result.success
        ]

        if len(recent_errors) >= 2:  # Lower threshold for quicker analysis
            logger.info("Using thinking model due to recent errors requiring analysis")
            return True

        # Default to action model for execution
        return False

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
            logger=logger,
        )

    def _handle_tool_lifecycle_event(self, event: ToolLifecycleEvent) -> None:
        """Map orchestration lifecycle events into typed UI events."""
        lifecycle_event_map = {
            "tool_start": EventType.TOOL_START,
            "tool_parameters_fixed": EventType.TOOL_PARAMETERS_FIXED,
            "tool_result": EventType.TOOL_RESULT,
            "tool_recovery": EventType.TOOL_RECOVERY,
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
        return ToolCall(
            name=step.tool_name or "",
            raw_params=step.tool_params or {},
            raw_action_text=step.content,
            source_step_index=self.current_iteration,
            model_used=step.model_used,
        )

    def _apply_tool_execution_loop_effects(self, execution: ToolExecution) -> None:
        """Apply loop-level side effects requested by orchestration metadata."""
        metadata = execution.metadata or {}

        if metadata.get("force_thinking_next"):
            self._force_thinking_next = True

        if metadata.get("invalidate_trunk_cache"):
            self.prompt_builder.invalidate_trunk_cache()

        if metadata.get("force_next_task") and hasattr(self.context_manager, "force_next_task"):
            self.context_manager.force_next_task()

    def _execute_steps(self, steps: List[ReActStep]) -> bool:
        """Execute a list of ReAct steps."""
        for step in steps:
            self.steps.append(step)

            if step.step_type == StepType.THOUGHT:
                self.agent_logger.info(f"💭 THOUGHT ({step.model_used}): {step.content}")
                logger.info(f"💭 THOUGHT: {step.content}")

                # Emit UI event for thought
                self.emit(
                    EventType.AGENT_THOUGHT,
                    message=step.content[:200]
                    + ("..." if len(step.content) > 200 else ""),  # Truncate for display
                    step_num=self.current_iteration,
                )

                # Detailed logging in verbose mode
                if self.config.verbose:
                    self._log_react_step_verbose(step)

                # Log to branch context if we're in one
                if self.context_manager.current_task_id:
                    # Add thought to branch history using new context management system
                    try:
                        self.context_manager.add_to_branch_history(
                            self.context_manager.current_task_id,
                            {
                                "type": "thought",
                                "iteration": self.current_iteration,
                                "content": step.content,
                            },
                        )
                    except Exception as e:
                        logger.warning(f"Failed to log thought to branch history: {e}")

            elif step.step_type == StepType.ACTION:
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
                call = self._build_tool_call_from_step(step)
                execution = self._get_tool_orchestrator().execute(call)
                result = execution.result
                step.tool_result = result
                self._apply_tool_execution_loop_effects(execution)

                # Log tool result in verbose mode
                if self.config.verbose:
                    self._log_tool_result_verbose(step.tool_name, result)

                # Add observation step with improved formatting
                self._add_observation_step(execution.observation_text)

                # CRITICAL: Force thinking after successful tool execution to prevent cognitive rush
                evidence_status = coerce_evidence_status(result.status)
                should_force_thinking = result.success or evidence_status in {
                    EvidenceStatus.PARTIAL,
                    EvidenceStatus.CONFLICT,
                    EvidenceStatus.UNKNOWN,
                }
                if should_force_thinking:
                    self._force_thinking_after_success = True
                    logger.debug(
                        f"✅ Tool {step.tool_name} requires follow-up thinking on next iteration"
                    )

                # Log to branch context if we're in one
                if branch_task_id:
                    # Add action result to branch history using new context management system
                    try:
                        output_to_store = result.output if result.output else ""
                        from datetime import datetime

                        timestamp = datetime.now().isoformat()

                        # Store full output and get reference if output is large
                        stored_output_refs = []
                        if len(output_to_store) > 800 and self.output_storage is not None:
                            # Store the full output
                            ref_id = self.output_storage.store_output(
                                task_id=self.context_manager.current_task_id,
                                tool_name=step.tool_name,
                                output=output_to_store,
                                timestamp=timestamp,
                                metadata={
                                    "success": result.success,
                                    "iteration": self.current_iteration,
                                },
                            )
                            stored_output_refs.append(ref_id)

                            # Get truncated version with reference
                            output_to_store = self.output_storage.get_truncation_with_reference(
                                output=output_to_store,
                                ref_id=ref_id,
                                max_length=800,
                                tool_name=step.tool_name,
                            )

                        history_entry = {
                            "type": "action",
                            "iteration": self.current_iteration,
                            "tool_name": step.tool_name,
                            "parameters": step.tool_params or {},
                            "success": result.success,
                            "output": output_to_store,
                            "observation": execution.observation_text,
                            "output_refs": self._dedupe_strings(
                                [
                                    *stored_output_refs,
                                    *self._output_refs_from_text(output_to_store),
                                ]
                            ),
                        }
                        # A dispatch-and-poll handoff is success=True but is NOT
                        # build-execution evidence (the command is still
                        # running); completion gates must be able to tell.
                        dispatch_status = (result.metadata or {}).get("dispatch_status")
                        if dispatch_status:
                            history_entry["dispatch_status"] = dispatch_status
                        self.context_manager.add_to_branch_history(
                            branch_task_id,
                            history_entry,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to log action to branch history: {e}")

        return True

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
                    if result.success:
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

            elif tool_name in ("maven", "build") and params.get("working_directory"):
                # Remember successful build working directory. The legacy maven
                # tool needed the output marker; the consolidated build tool's
                # success already reflects the backend verdict.
                build_succeeded = (
                    result.success
                    if tool_name == "build"
                    else "BUILD SUCCESS" in (result.output or "")
                )
                if build_succeeded:
                    # Get working_directory parameter (standardized across all tools)
                    maven_workdir = params.get("working_directory", "/workspace")
                    self.successful_states["working_directory"] = maven_workdir
                    self.successful_states["maven_success"] = True

                    # Check if Maven is working outside workspace (concerning)
                    if not maven_workdir.startswith("/workspace"):
                        logger.warning(f"⚠️ Maven succeeded outside workspace: {maven_workdir}")
                        logger.warning(f"⚠️ This may indicate workspace issues")
                    else:
                        logger.info(f"✅ Maven success in workspace: {maven_workdir}")

                    logger.info(f"Maven success recorded for directory: {maven_workdir}")

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

    def _track_tool_execution(self, tool_signature: str, success: bool):
        """Track tool execution to detect repetitive patterns."""
        execution_info = {
            "signature": tool_signature,
            "success": success,
            "timestamp": self._get_timestamp(),
        }

        self.recent_tool_executions.append(execution_info)

        # Keep only recent executions to prevent memory bloat
        if len(self.recent_tool_executions) > self.max_recent_executions:
            self.recent_tool_executions.pop(0)

    def _add_observation_step(self, observation: str):
        """Add an observation step, enriched with physical validation state."""
        # Get physical validation state if relevant
        physical_state = self._get_physical_validation_state(observation)

        # Enrich observation with physical state if available
        if physical_state:
            observation = self._enrich_observation_with_physical_state(observation, physical_state)

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

        # DEPRECATED: Task completion detection now handled by state_evaluator
        # self._check_task_completion_opportunity(observation)

    def _add_completion_guidance(self, reason: str):
        """Add guidance to help agent recognize task completion."""
        guidance_segments: List[str] = []

        snapshot = self.successful_states.get("report_snapshot")
        if snapshot:
            try:
                guidance_segments.append(render_condensed_summary(snapshot))
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.debug(f"Failed to render report snapshot for completion guidance: {exc}")

        guidance_segments.append(
            f"SYSTEM GUIDANCE: Task completion detected! {reason}. "
            f"You should now generate a completion report using the report tool "
            f"with a summary of what was accomplished, then the system will stop."
        )

        guidance = "\n".join(guidance_segments)

        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=guidance,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)

        self.agent_logger.info(f"🏁 COMPLETION GUIDANCE: {guidance}")
        logger.info(f"🏁 COMPLETION GUIDANCE: Task completion detected - {reason}")

    def _check_completion_suggestion(self) -> str:
        """Check if we should strongly suggest task completion."""
        # Check if Maven build and test succeeded but no report generated yet
        if self.successful_states["maven_success"] and not self._has_report_been_generated():

            # Look for recent Maven test success
            recent_steps = self.steps[-10:] if len(self.steps) >= 10 else self.steps
            for step in recent_steps:
                if (
                    step.step_type == StepType.ACTION
                    and step.tool_name == "maven"
                    and step.tool_result
                    and step.tool_result.success
                ):

                    output = step.tool_result.output or ""
                    if (
                        "test" in step.tool_params.get("command", "").lower()
                        and "BUILD SUCCESS" in output
                        and "Tests run:" in output
                    ):

                        # Parse test results to confirm no failures
                        import re

                        test_match = re.search(
                            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", output
                        )
                        if test_match:
                            total, failures, errors = map(int, test_match.groups())
                            if failures == 0 and errors == 0 and total > 0:
                                return f"Maven build and test completed successfully ({total} tests passed)"

        # Check if we've been running for many iterations without progress
        if self.current_iteration >= 25 and not self._has_report_been_generated():
            # Check if we have any clear successes
            if self.successful_states["cloned_repos"] or self.successful_states["maven_success"]:
                return "Task has been running for many iterations with some successes"

        return None

    def _has_report_been_generated(self) -> bool:
        """Check if a report has already been generated."""
        for step in self.steps:
            if (
                step.step_type == StepType.ACTION
                and step.tool_name == "report"
                and step.tool_result
                and step.tool_result.success
            ):
                return True
        return False

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
        Get physical validation state for build/test related observations.

        Args:
            observation: The observation text

        Returns:
            Physical validation state dict or None
        """
        # Only validate for build/test related observations
        obs_lower = observation.lower()
        if not any(
            keyword in obs_lower
            for keyword in ["build", "compile", "test", "maven", "gradle", "success", "fail"]
        ):
            return None

        try:
            # Get project name from context or use default
            project_name = None
            if hasattr(self.context_manager, "project_name"):
                project_name = self.context_manager.project_name

            # Run physical validation
            validation_result = self.physical_validator.validate_build_artifacts(project_name)

            # Check if we need to replay commands
            if "build success" in obs_lower or "build fail" in obs_lower:
                # Try to get the last build command from command tracker if available
                if hasattr(self, "command_tracker") and self.command_tracker:
                    last_build = self.command_tracker.get_last_build_command()
                    if last_build:
                        replay_result = self.physical_validator.replay_last_build_command(
                            last_build["command"], last_build.get("working_dir")
                        )
                        validation_result["build_replay"] = replay_result

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

        if "build_replay" in physical_state:
            if physical_state["build_replay"]:
                evidence_lines.append("[PHYSICAL EVIDENCE: Build command replay succeeded]")
            else:
                evidence_lines.append("[PHYSICAL EVIDENCE: Build command replay failed]")

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
            "success": result.success,
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
            if result is not None and not getattr(result, "success", False):
                tool_failures[tool_name] = tool_failures.get(tool_name, 0) + 1

        return {
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
                    if s.step_type == StepType.ACTION and s.tool_result and s.tool_result.success
                ]
            )
            + archived.get("successful_actions", 0),
            "failed_actions": len(
                [
                    s
                    for s in self.steps
                    if s.step_type == StepType.ACTION
                    and s.tool_result
                    and not s.tool_result.success
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
                from datetime import datetime
                from pathlib import Path

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
