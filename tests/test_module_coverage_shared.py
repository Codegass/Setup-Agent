"""One island-coverage computation, two consumers (gate mid-run, finalizer at close).

Live evidence driving this module (2026-07-18 probes):
- ws7-final7 bigtop r1: agent built the gradle islands, tried to claim blocked,
  was rejected with "evidence is green" (true at top level, useless as
  guidance), gave up — nothing ever told it which islands remained.
- bigtop5: the agent fixated on the one broken maven island for 7 build calls;
  nothing told it three healthy islands were untouched.

The coverage rollup existed — but only at evidence-close, for the verdict.
This module makes the SAME computation available mid-run so gate responses can
carry the checklist. Same algorithm both places, or the in-run guidance and
the sealed verdict would disagree (the exact split this campaign just fixed).
"""

from sag.agent.module_coverage import (
    coverage_checklist_line,
    coverage_conflicts,
    module_coverage,
)


class FakeValidator:
    project_path = "/workspace"

    def __init__(self, *, primary="maven", by_system=None, tests_by_path=None):
        self._primary = primary
        self._by_system = by_system or {}
        self._tests = tests_by_path or {}

    def _detect_build_system(self, project_dir):
        return self._primary

    def scan_modules(self, project_dir, build_system):
        return [dict(m) for m in self._by_system.get(build_system, [])]

    def parse_module_test_reports(self, module_dir, report_dirs):
        return dict(self._tests.get(module_dir.rsplit("/", 1)[-1], {}))


def _bigtop_validator():
    return FakeValidator(
        primary="maven",
        by_system={
            "maven": [
                {"path": ".", "name": ".", "class_count": 0, "jar_count": 0,
                 "report_dirs": [], "has_test_sources": False},
                {"path": "bigtop-test-framework", "name": "bigtop-test-framework",
                 "class_count": 0, "jar_count": 0, "report_dirs": [],
                 "has_test_sources": True},
            ],
            "gradle": [
                {"path": "bigtop-data-generators/bigtop-samplers",
                 "name": "bigtop-samplers", "class_count": 39, "jar_count": 1,
                 "report_dirs": ["/x/build/test-results/test"],
                 "has_test_sources": True},
                {"path": "bigtop-bigpetstore/bigpetstore-spark", "name": "spark",
                 "class_count": 0, "jar_count": 0, "report_dirs": [],
                 "has_test_sources": True},
            ],
        },
        tests_by_path={
            "bigtop-samplers": {"tests_total": 50, "tests_passed": 50, "failing_count": 0}
        },
    )


def test_coverage_merges_both_jvm_systems_and_rolls_up():
    coverage = module_coverage(_bigtop_validator(), "bigtop")
    assert coverage is not None
    summary = coverage["summary"]
    assert summary["modules_total"] == 4
    assert summary["modules_built"] == 1  # only bigtop-samplers has classes
    built = [m["path"] for m in coverage["modules"] if m["build_status"] == "success"]
    assert built == ["bigtop-data-generators/bigtop-samplers"]


def test_coverage_conflicts_match_the_finalizer_contract():
    coverage = module_coverage(_bigtop_validator(), "bigtop")
    conflicts = coverage_conflicts(coverage)
    assert "build_modules_incomplete" in conflicts
    assert "reactor_scope_narrowed" in conflicts


def test_python_projects_are_exempt():
    assert module_coverage(FakeValidator(primary="python"), "proj") is None
    assert coverage_conflicts(None) == ()
    assert coverage_checklist_line(None) is None


def _httpcomponents_validator():
    """Live httpcomponents-client shape: a Maven packaging=pom reactor root
    (aggregator shell, zero own sources) over 5 real modules that all built and
    tested. scan_modules marks the root aggregator_shell=True; the coverage
    summary must exclude it so the ratio reads 5/5, not 5/6."""
    modules = [
        {"path": ".", "name": ".", "class_count": 0, "jar_count": 0,
         "report_dirs": [], "has_test_sources": False, "aggregator_shell": True},
    ]
    for i in range(1, 6):
        modules.append({
            "path": f"module{i}", "name": f"module{i}",
            "class_count": 100 + i, "jar_count": 1,
            "report_dirs": [f"/x/module{i}/target/surefire-reports"],
            "has_test_sources": True,
        })
    return FakeValidator(
        primary="maven",
        by_system={"maven": modules},
        tests_by_path={
            f"module{i}": {"tests_total": 400 + i, "tests_passed": 400 + i,
                           "failing_count": 0}
            for i in range(1, 6)
        },
    )


def test_aggregator_shell_root_excluded_from_module_ratio():
    """httpcomponents regression: the packaging=pom root must not count as an
    unbuilt denominator entry. 5/5 built, no build_modules_incomplete conflict."""
    coverage = module_coverage(_httpcomponents_validator(), "httpcomponents-client")
    assert coverage is not None
    summary = coverage["summary"]
    assert summary["modules_total"] == 5
    assert summary["modules_built"] == 5
    # the shell row still ships for display/debug — it is not deleted, just uncounted
    paths = {m["path"] for m in coverage["modules"]}
    assert "." in paths
    # the '5/6 built' cap is gone: no coverage conflict on the shell
    assert coverage_conflicts(coverage) == ()


def test_aggregator_shell_verdict_folds_to_success():
    """End-to-end: with the shell uncounted, an otherwise-green run seals SUCCESS
    (the httpcomponents cap folded it to partial)."""
    from sag.agent.evidence_state import EvidenceRole, StateScope
    from sag.agent.evidence_state import RunEvidenceState as _RunEvidenceState
    from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer
    from sag.evidence import EvidenceStatus, OperationOutcome, TestStats
    from sag.tools.base import ToolResult

    class RunEvidenceState(_RunEvidenceState):
        def ingest_tool_result(self, scope, tool_name, result, provenance=None, *, roles=()):
            explicit = list(roles)
            if not explicit:
                if scope is StateScope.ARTIFACTS:
                    explicit.append(EvidenceRole.BUILD)
                if result.test_stats is not None:
                    explicit.append(EvidenceRole.TEST)
            return super().ingest_tool_result(scope, tool_name, result, provenance, roles=explicit)

    inner = _httpcomponents_validator()

    class Orch:
        def __init__(self):
            self.files = {}

        def execute_command(self, command):
            if command.startswith("mkdir -p "):
                return {"success": True, "exit_code": 0, "output": ""}
            if command.startswith("test -f ") and " && cat " in command:
                path = command.split()[2]
                if path not in self.files:
                    return {"success": False, "exit_code": 1, "output": ""}
                return {"success": True, "exit_code": 0, "output": self.files[path]}
            if command.startswith("cat > "):
                path = command.split()[2]
                self.files[path] = command.split("\n", 1)[1].rsplit("\n", 1)[0] + "\n"
                return {"success": True, "exit_code": 0, "output": ""}
            if command.startswith("truncate -s -1 "):
                path = command.split()[-1]
                self.files[path] = self.files[path][:-1]
                return {"success": True, "exit_code": 0, "output": ""}
            if command.startswith("mv "):
                _, src, tgt = command.split()
                self.files[tgt] = self.files.pop(src)
                return {"success": True, "exit_code": 0, "output": ""}
            return {"success": True, "exit_code": 0, "output": ""}

    class V:
        project_path = "/workspace"

        def __init__(self, cov):
            self._cov = cov

        def validate_build_status(self, project_name):
            return {"success": True, "build_complete": True, "reason": "all compiled",
                    "conflicts": [], "evidence_status": "success",
                    "evidence": {"class_count": 515}}

        def _detect_build_system(self, project_dir):
            return self._cov._detect_build_system(project_dir)

        def scan_modules(self, project_dir, build_system):
            return self._cov.scan_modules(project_dir, build_system)

        def parse_module_test_reports(self, module_dir, report_dirs):
            return self._cov.parse_module_test_reports(module_dir, report_dirs)

    state = RunEvidenceState(run_id="session-httpcomponents")
    state.ingest_tool_result(
        StateScope.ARTIFACTS, "build",
        ToolResult.completed_success(output="all modules built", refs=["output_build"]),
        provenance="output_build",
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME, "build",
        ToolResult.completed_success(
            output="green",
            test_stats=TestStats(discovered=2255, executed=2255, passed=2255, failed=0, skipped=0),
            refs=["output_tests"],
        ),
        provenance="output_tests",
    )
    finalizer = VerdictFinalizer(Orch(), validator=V(inner), project_name="httpcomponents-client")
    snapshot = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)
    assert "build_modules_incomplete" not in snapshot.conflicts
    assert snapshot.verdict == "success"


def test_checklist_line_names_built_and_unbuilt_modules():
    line = coverage_checklist_line(module_coverage(_bigtop_validator(), "bigtop"))
    assert line is not None
    # the agent must SEE what remains, not just a ratio
    assert "1/4 built" in line
    assert "bigtop-samplers" in line
    assert "bigtop-test-framework" in line or "no output" in line


def test_validator_failure_degrades_to_none_never_raises():
    class Exploding(FakeValidator):
        def scan_modules(self, project_dir, build_system):
            raise RuntimeError("container gone")

    coverage = module_coverage(Exploding(), "proj")
    assert coverage is None or coverage["summary"]["modules_total"] == 0


# ---- Gate responses carry the checklist (mid-run consumer) ----

from sag.agent.phase_gates import check_phase_claim
from sag.agent.phase_machine import PhaseClaim


class GateFakeValidator(FakeValidator):
    """Coverage fixture + the build-status oracle the gate consults."""

    def __init__(self, build_status, **kwargs):
        super().__init__(**kwargs)
        self._build_status = build_status

    def validate_build_status(self, project_name):
        return dict(self._build_status)


def _gate_validator():
    inner = _bigtop_validator()
    validator = GateFakeValidator(
        {
            "success": True,
            "build_complete": True,
            "reason": "Build fingerprints found for maven project",
            "conflicts": [],
            "evidence_status": "success",
            "evidence": {"class_count": 39},
        },
        primary="maven",
        by_system=inner._by_system,
        tests_by_path=inner._tests,
    )
    return validator


def test_build_gate_response_names_unbuilt_modules():
    """ws7-final7 r1: 'evidence is green' with zero mention of the unattempted
    islands taught the agent to give up. The gate's reason must carry the
    checklist — on ACCEPTANCE too, not only on rejection."""
    claim = PhaseClaim(
        phase="build", signal="done", claimed_outcome="partial",
        key_results="built the samplers island",
    )
    gate = check_phase_claim("build", claim, _gate_validator(), None, "bigtop")
    assert gate.accepted
    text = " ".join([gate.reason or "", *(gate.suggestions or ())])
    assert "1/4 built" in text
    assert "no output yet" in text


def test_blocked_rejection_is_informative_not_gaslighting():
    """The rejection must explain WHY blocked does not fit AND what remains —
    never a bare 'evidence is green' to an agent that just watched a failure."""
    claim = PhaseClaim(
        phase="build", signal="blocked", claimed_outcome="failed",
        reason="maven island will not compile",
    )
    gate = check_phase_claim("build", claim, _gate_validator(), None, "bigtop")
    # blocked against green top-level evidence: still not accepted as blocked…
    text = " ".join([gate.reason or "", *(gate.suggestions or ())])
    # …but the response tells the agent what the evidence actually shows and
    # what it can do next (continue unbuilt modules / claim done honestly).
    assert "1/4 built" in text or "no output yet" in text


# ---- Loop guidance names the untried recommended targets (fix 3) ----

from types import SimpleNamespace

from sag.agent.react_engine import ReActEngine


def _loop_engine(islands, observed_workdirs):
    """The redirect reads islands from the SHARED manifest (panel review: the
    trunk recommendation is projected by treatment dim (b), so sourcing there
    made the allowlisted loop differ across arms)."""
    import json

    from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

    engine = ReActEngine.__new__(ReActEngine)

    class ManifestOrch:
        def execute_command(self, command, **kwargs):
            if command == f"cat {REQUIREMENTS_PATH}":
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": json.dumps({"build_islands": islands}),
                }
            return {"success": True, "exit_code": 0, "output": ""}

    engine.physical_validator = SimpleNamespace(docker_orchestrator=ManifestOrch())
    engine.context_manager = SimpleNamespace(load_trunk_context=lambda: None)
    engine.run_evidence_state = SimpleNamespace(
        tool_observations=tuple(
            SimpleNamespace(params={"working_directory": wd}) for wd in observed_workdirs
        )
    )
    return engine


BIGTOP_ISLANDS = [
    {"root": "/workspace/bigtop/bigtop-test-framework", "system": "maven", "goal": "install"},
    {"root": "/workspace/bigtop/bigtop-data-generators", "system": "gradle",
     "goal": "publishToMavenLocal"},
    {"root": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark", "system": "gradle",
     "goal": "build"},
]


def _guide_decision():
    return SimpleNamespace(
        decision="guide",
        prior_attempt_ids=("build-1",),
        missing_progress_scopes=("artifacts",),
    )


def test_loop_guidance_names_untried_islands():
    """bigtop5: 'change approach' with no destination left the agent hammering
    the one broken island. The guidance must name what it has NOT tried."""
    engine = _loop_engine(
        BIGTOP_ISLANDS, observed_workdirs=["/workspace/bigtop/bigtop-test-framework"]
    )
    text = engine._loop_guidance(_guide_decision())
    assert "bigtop-data-generators" in text
    assert "bigpetstore-spark" in text
    assert "bigtop-test-framework" not in text.split("Untried")[-1]


def test_loop_guidance_stays_clean_when_all_islands_tried_or_no_islands():
    engine = _loop_engine(BIGTOP_ISLANDS, observed_workdirs=[i["root"] for i in BIGTOP_ISLANDS])
    assert "Untried" not in engine._loop_guidance(_guide_decision())
    engine2 = _loop_engine([], observed_workdirs=[])
    assert "Untried" not in engine2._loop_guidance(_guide_decision())


# ---- Test phase reads the native-core state before sweeping (fix 4) ----

from test_python_phase_guidance import _engine_at, _python_env


def _native_env():
    env = _python_env()
    env["build_recommendation"]["has_native_build"] = True
    env["build_recommendation"]["build_root"] = "/workspace/tvm/python"
    return env


def test_test_phase_suggests_smoke_when_native_core_not_built():
    """Live TVM (twice): the agent swept the full suite without libtvm — 356
    identical collection errors. First fix attempt hid the steer on the
    no-brief fallback branch while live runs walk the brief-projection branch
    (the bug-#5/#10 seam lesson again) — so this test goes through the REAL
    intro path WITH a brief projection present."""
    env = _native_env()
    env["project_brief_projection"] = "PROJECT BRIEF: python native project."
    engine = _engine_at(3, env)  # mark_done -> legacy outcome unknown
    intro = engine._phase_intro_step().content
    assert "PROJECT BRIEF" in intro  # the live branch is the one under test
    assert "smoke" in intro.lower()
    assert "collection errors" in intro


def test_test_phase_stays_clean_when_native_built_or_not_native():
    # native repo but build phase succeeded -> no smoke detour
    env = _native_env()
    env["project_brief_projection"] = "PROJECT BRIEF: python native project."
    engine = _engine_at(3, env)
    for record in engine.phase_machine.records:
        if record.phase == "build":
            object.__setattr__(record, "validated_outcome", "success")
    assert "smoke" not in engine._phase_intro_step().content.lower()
    # plain python repo, fallback branch -> guidance byte-identical to before
    engine2 = _engine_at(3, _python_env())
    from sag.agent.react_engine import PYTHON_TEST_PHASE_GUIDANCE

    assert engine2._python_phase_guidance("test") == PYTHON_TEST_PHASE_GUIDANCE
    assert "smoke" not in engine2._phase_intro_step().content.lower()


# ---- Island-keyed checklist: actionable coordinates, not raw module names ----

def test_checklist_prefers_islands_with_full_roots_and_goals():
    """bigtop6 live: the module-scan checklist showed 15 basenames (half noise:
    site, test-artifacts) with no paths or commands — the agent stayed at the
    root for 86 calls. With islands known, the checklist must be keyed to the
    4 actionable islands, each with its FULL root and goal."""
    islands = [
        {"root": "/workspace/bigtop/bigtop-test-framework", "system": "maven",
         "goal": "install"},
        {"root": "/workspace/bigtop/bigtop-data-generators", "system": "gradle",
         "goal": "publishToMavenLocal"},
        {"root": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark",
         "system": "gradle", "goal": "build"},
    ]
    coverage = module_coverage(_bigtop_validator(), "bigtop")
    line = coverage_checklist_line(coverage, islands=islands)
    assert "islands" in line.lower()
    # the built island is recognized through its modules
    assert "1/3 built" in line
    # remaining entries carry full root AND goal — actionable, not just names
    assert "gradle 'build' in /workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark" in line
    assert "maven 'install' in /workspace/bigtop/bigtop-test-framework" in line
    # island keying replaces the noisy 15-module dump
    assert "site" not in line


def test_checklist_falls_back_to_modules_without_islands():
    line = coverage_checklist_line(module_coverage(_bigtop_validator(), "bigtop"), islands=[])
    assert "Module coverage:" in line
