# tests/test_native_capability_state.py
"""Plan 5 Stage E (P0-D): native capability state is PROBED, never inferred
from a phase outcome.

Ground-truth review 2026-07-26 (§"TVM: native artifacts exist, compiler
capability does not"): five `.so` files existed — `libtvm_compiler.so` among
them, 104 MB, linked against LLVM — while the test steer, the advisor and the
report all asserted "The NATIVE core was not built in the build phase". The
trigger was never an artifact probe; it was "the build phase outcome is not
success", so a `pip check` packaging warning was converted into a claim that
native code does not exist.

Under test here:

A. `_native_smoke_guidance` probes for native shared objects under the
   surveyed native root before it renders anything. Artifacts PRESENT -> the
   honest facts text (artifacts present + the outcome closed for
   packaging-integrity reasons + run the bounded smoke); ABSENT -> the legacy
   "not built" steer, now a true statement; the probe FAILING (or no
   orchestrator at all) -> the unknown text, because an unverified absence is
   not a fact.
B. The bounded smoke's all-skipped verdict projects up to three DISTINCT
   junit skip messages, so the model sees `need llvm` — the fact chain the
   live run never showed it.
C. The advisor evidence digest carries the same probe fact (from the cache,
   never a second probe), the capability-unproven flag and the skip reasons,
   for native projects only.
"""

import json
from types import SimpleNamespace

from test_consult_at_entry import _unit_engine
from test_native_smoke_capability_gate import TVM_ROOT, junit_rules, receipt_rule
from test_python_phase_guidance import _engine_at, _python_env
from test_python_tool import (
    TVM_NATIVE_TEST_MANIFEST,
    Orch,
    fail,
    ok,
    tvm_native_smoke_rules,
)

from sag.agent.evidence_state import EvidenceRole, RunEvidenceState, StateScope
from sag.agent.react_engine import NATIVE_NOT_BUILT_TEST_GUIDANCE
from sag.tools.base import ToolResult
from sag.tools.internal.python_tool import PythonTool

NATIVE_ROOT = "/workspace/tvm/python"
SO_PATHS = "\n".join(
    f"{NATIVE_ROOT}/build/{name}"
    for name in (
        "libtvm_compiler.so",
        "libtvm_runtime.so",
        "libtvm_runtime_extra.so",
        "libtvm_ffi.so",
        "libtvm_ffi_testing.so",
    )
)
LLVM_SKIP = "need llvm"
WHEEL_SKIP = "LLVM enablement only asserted during wheel validation"
CUDA_SKIP = "CUDA runtime not expected in this wheel"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _ProbeOrch:
    """Orchestrator double for the engine's ONE bounded artifact probe.

    Everything else (the build-requirements read the digest's island line
    makes) reports missing, so no other digest part appears.
    """

    def __init__(self, probe):
        self.probe = probe
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if "-name '*.so'" in command:
            return dict(self.probe)
        return fail("No such file")

    def probes(self):
        return [command for command in self.commands if "-name '*.so'" in command]


def _native_env():
    env = _python_env()
    env["build_recommendation"]["has_native_build"] = True
    env["build_recommendation"]["build_root"] = NATIVE_ROOT
    env["build_recommendation"]["test_root"] = NATIVE_ROOT
    return env


def _test_phase_engine(probe, env=None):
    """Engine at the test phase (build closed non-success) with a scripted probe."""
    engine = _engine_at(3, env if env is not None else _native_env())
    engine.physical_validator = SimpleNamespace(docker_orchestrator=probe)
    return engine


def _smoke_state(**metadata_overrides):
    state = RunEvidenceState(run_id="native-state")
    metadata = {
        "operation": "test",
        "command": "/venv/bin/python -m pytest tests/python/all-platform-minimal-test",
        "collection_scope": "filtered",
        "collected": 3,
        "collected_after_deselection": 3,
        "executed": 3,
        "collection_errors": 0,
        **metadata_overrides,
    }
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "test",
        ToolResult.completed_success(output="3 skipped", metadata=metadata),
        provenance="tool:build:1",
        roles=(EvidenceRole.TEST,),
        execution_id="exec-test-1",
        params={"action": "test"},
    )
    return state


def _digest_engine(probe, *, native=True, state=None):
    engine = _unit_engine(phase="test")
    env = _native_env() if native else _python_env()
    engine.context_manager = SimpleNamespace(
        current_task_id=None,
        load_trunk_context=lambda: SimpleNamespace(environment_summary=env),
    )
    # A build phase that closed partial (the live TVM packaging-integrity
    # outcome) is what makes the test-phase steer eligible at all.
    engine.phase_machine = SimpleNamespace(
        current_phase="test",
        current_attempt_id="attempt-1",
        records=(SimpleNamespace(phase="build", validated_outcome="partial", outcome="partial"),),
    )
    engine.physical_validator = SimpleNamespace(docker_orchestrator=probe)
    engine.run_evidence_state = state if state is not None else RunEvidenceState(run_id="empty")
    return engine


def skip_reason_rule(reasons):
    """The in-container junit skip-reason extractor, matched on a signature
    line of its script (house pattern: `junit_rules` does the same)."""
    return ('if name != "skipped"', ok(json.dumps(reasons)))


def all_skipped_orch(*, skip_rules=()):
    return Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=3),
            *skip_rules,
        ],
    )


# ---------------------------------------------------------------------------
# A. the artifact probe replaces the phase-outcome proxy
# ---------------------------------------------------------------------------


def test_probe_that_finds_shared_objects_never_says_not_built():
    """THE live TVM case: five .so files present, build closed non-success for
    packaging integrity. No layer may say the native core is missing."""
    orch = _ProbeOrch(ok(SO_PATHS))

    guidance = _test_phase_engine(orch)._native_smoke_guidance("test")

    assert "Native artifacts are present (5 shared objects)" in guidance
    assert "was not built" not in guidance
    assert "packaging-integrity" in guidance
    assert "does NOT mean the native core is missing" in guidance
    # The bounded-smoke discipline is unchanged: the tool owns the target.
    assert "bare build(action='test')" in guidance
    assert "NO args" in guidance
    assert "Never invent, guess, or substitute a test path" in guidance
    assert "skip reasons name the missing capability" in guidance


def test_the_probe_is_one_bounded_command_under_the_surveyed_native_root():
    orch = _ProbeOrch(ok(SO_PATHS))

    _test_phase_engine(orch)._native_smoke_guidance("test")

    assert len(orch.probes()) == 1
    probe = orch.probes()[0]
    assert NATIVE_ROOT in probe
    assert "-not -path '*/.git/*'" in probe
    assert "head -20" in probe


def test_probe_that_finds_nothing_keeps_the_legacy_not_built_steer():
    """Artifacts ABSENT is the one state where 'not built' is a fact."""
    orch = _ProbeOrch(ok(""))

    guidance = _test_phase_engine(orch)._native_smoke_guidance("test")

    assert guidance == NATIVE_NOT_BUILT_TEST_GUIDANCE


def test_a_failed_probe_reports_unknown_and_never_claims_not_built():
    """An unverified absence is not a fact: no count is claimed either."""
    orch = _ProbeOrch(fail("find: '/workspace/tvm/python': No such file or directory"))

    guidance = _test_phase_engine(orch)._native_smoke_guidance("test")

    assert "could not verify native artifacts" in guidance
    assert "was not built" not in guidance
    assert "shared objects" not in guidance
    assert "bare build(action='test')" in guidance


def test_no_orchestrator_reports_unknown_too():
    engine = _engine_at(3, _native_env())

    guidance = engine._native_smoke_guidance("test")

    assert "could not verify native artifacts" in guidance
    assert "was not built" not in guidance


def test_a_non_native_python_project_probes_nothing_and_gets_no_steer():
    orch = _ProbeOrch(ok(SO_PATHS))

    engine = _test_phase_engine(orch, env=_python_env())

    assert engine._native_smoke_guidance("test") is None
    assert orch.probes() == []


def test_a_succeeded_build_phase_probes_nothing_and_gets_no_steer():
    orch = _ProbeOrch(ok(SO_PATHS))
    engine = _test_phase_engine(orch)
    for record in engine.phase_machine.records:
        if record.phase == "build":
            object.__setattr__(record, "validated_outcome", "success")

    assert engine._native_smoke_guidance("test") is None
    assert orch.probes() == []


def test_the_present_text_reaches_the_test_phase_intro():
    orch = _ProbeOrch(ok(SO_PATHS))

    intro = _test_phase_engine(orch)._phase_intro_step().content

    assert "Native artifacts are present (5 shared objects)" in intro
    assert "was not built" not in intro


# ---------------------------------------------------------------------------
# B. all-skipped smoke projects its junit skip reasons
# ---------------------------------------------------------------------------


def test_all_skipped_smoke_projects_up_to_three_distinct_skip_reasons():
    """The live TVM smoke showed three bare SKIPPED labels. The reasons name
    the missing capability, so they are model-visible facts."""
    orch = all_skipped_orch(skip_rules=[skip_reason_rule([LLVM_SKIP, WHEEL_SKIP, CUDA_SKIP])])

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_capability_unproven"] is True
    assert result.metadata["smoke_skip_reasons"] == [LLVM_SKIP, WHEEL_SKIP, CUDA_SKIP]
    assert f"[test] skip reasons: {LLVM_SKIP}; {WHEEL_SKIP}; {CUDA_SKIP}" in result.output
    # One line, appended after the capability-unproven line.
    lines = result.output.splitlines()
    unproven = next(i for i, line in enumerate(lines) if "capability NOT proven" in line)
    assert lines[unproven + 1].startswith("[test] skip reasons: ")


def test_skip_reasons_are_capped_at_three_deduped_and_truncated():
    long_reason = "x" * 300
    orch = all_skipped_orch(
        skip_rules=[
            skip_reason_rule([LLVM_SKIP, LLVM_SKIP, long_reason, WHEEL_SKIP, CUDA_SKIP]),
        ]
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    reasons = result.metadata["smoke_skip_reasons"]
    assert reasons == [LLVM_SKIP, "x" * 120, WHEEL_SKIP]
    line = next(
        line for line in result.output.splitlines() if line.startswith("[test] skip reasons: ")
    )
    assert line == f"[test] skip reasons: {LLVM_SKIP}; {'x' * 120}; {WHEEL_SKIP}"


def test_no_junit_skip_messages_renders_no_line_and_invents_nothing():
    orch = all_skipped_orch(skip_rules=[skip_reason_rule([])])

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_capability_unproven"] is True
    assert "smoke_skip_reasons" not in result.metadata
    assert "skip reasons" not in result.output


def test_an_unreadable_junit_report_renders_no_skip_reasons_line():
    orch = all_skipped_orch(skip_rules=[('if name != "skipped"', fail("Traceback"))])

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert "smoke_skip_reasons" not in result.metadata
    assert "skip reasons" not in result.output


def test_a_proven_smoke_extracts_no_skip_reasons_at_all():
    """The extraction is bounded to the all-skipped path — one extra command
    only where it is the corrective fact."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=2),
            skip_reason_rule([LLVM_SKIP]),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_receipt_written"] is True
    assert "smoke_skip_reasons" not in result.metadata
    assert [command for command in orch.commands if 'if name != "skipped"' in command] == []


# ---------------------------------------------------------------------------
# C. the advisor digest carries the native state
# ---------------------------------------------------------------------------


def test_the_digest_carries_artifacts_unproven_and_skip_reasons():
    engine = _digest_engine(
        _ProbeOrch(ok(SO_PATHS)),
        state=_smoke_state(
            smoke_capability_unproven=True,
            smoke_skip_reasons=[LLVM_SKIP, CUDA_SKIP],
        ),
    )

    digest = engine._advisor_evidence_digest()

    assert (
        "Native state: artifacts=present (5 shared objects), "
        f"last bounded smoke=capability_unproven, skip reasons: {LLVM_SKIP}; {CUDA_SKIP}"
    ) in digest


def test_the_digest_reports_absent_and_unknown_artifacts_without_a_smoke():
    absent = _digest_engine(_ProbeOrch(ok("")))
    unknown = _digest_engine(_ProbeOrch(fail("No such file or directory")))

    assert "Native state: artifacts=absent" in absent._advisor_evidence_digest()
    assert "capability_unproven" not in absent._advisor_evidence_digest()
    assert "Native state: artifacts=unknown" in unknown._advisor_evidence_digest()


def test_the_digest_has_no_native_line_for_a_non_native_project():
    engine = _digest_engine(_ProbeOrch(ok(SO_PATHS)), native=False)

    digest = engine._advisor_evidence_digest()

    assert "Native state:" not in digest
    assert engine.physical_validator.docker_orchestrator.probes() == []


def test_the_digest_reuses_the_cached_probe_fact_instead_of_probing_again():
    orch = _ProbeOrch(ok(SO_PATHS))
    engine = _digest_engine(orch, state=_smoke_state(smoke_capability_unproven=True))

    engine._native_smoke_guidance("test")
    engine._advisor_evidence_digest()
    engine._advisor_evidence_digest()

    assert len(orch.probes()) == 1
