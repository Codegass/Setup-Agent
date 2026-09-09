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

from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import add_published_mutable_json, strict_published_evidence

from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.agent.module_coverage import (
    coverage_checklist_line,
    coverage_conflicts,
    module_coverage,
)
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH


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
                {
                    "path": ".",
                    "name": ".",
                    "class_count": 0,
                    "jar_count": 0,
                    "report_dirs": [],
                    "has_test_sources": False,
                },
                {
                    "path": "bigtop-test-framework",
                    "name": "bigtop-test-framework",
                    "class_count": 0,
                    "jar_count": 0,
                    "report_dirs": [],
                    "has_test_sources": True,
                },
            ],
            "gradle": [
                {
                    "path": "bigtop-data-generators/bigtop-samplers",
                    "name": "bigtop-samplers",
                    "class_count": 39,
                    "jar_count": 1,
                    "report_dirs": ["/x/build/test-results/test"],
                    "has_test_sources": True,
                },
                {
                    "path": "bigtop-bigpetstore/bigpetstore-spark",
                    "name": "spark",
                    "class_count": 0,
                    "jar_count": 0,
                    "report_dirs": [],
                    "has_test_sources": True,
                },
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
        {
            "path": ".",
            "name": ".",
            "class_count": 0,
            "jar_count": 0,
            "report_dirs": [],
            "has_test_sources": False,
            "aggregator_shell": True,
        },
    ]
    for i in range(1, 6):
        modules.append(
            {
                "path": f"module{i}",
                "name": f"module{i}",
                "class_count": 100 + i,
                "jar_count": 1,
                "report_dirs": [f"/x/module{i}/target/surefire-reports"],
                "has_test_sources": True,
            }
        )
    return FakeValidator(
        primary="maven",
        by_system={"maven": modules},
        tests_by_path={
            f"module{i}": {"tests_total": 400 + i, "tests_passed": 400 + i, "failing_count": 0}
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
    from container_evidence_fakes import ContainerFS

    from sag.agent.evidence_publications import (
        EvidencePublicationAuthority,
        install_evidence_publication_authority,
        reset_evidence_publication_authority,
    )
    from sag.agent.evidence_state import EvidenceRole
    from sag.agent.evidence_state import RunEvidenceState as _RunEvidenceState
    from sag.agent.evidence_state import StateScope
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
            self.filesystem = ContainerFS()
            self.files = self.filesystem.files

        def execute_command(self, command, **kwargs):
            return self.filesystem(command, **kwargs)

    class Sink:
        path = "/host/module-coverage-control-events.jsonl"

        def emit(self, kind, payload, *, source=None):
            del kind, payload, source

    class V:
        project_path = "/workspace"

        def __init__(self, cov):
            self._cov = cov

        def validate_build_status(self, project_name):
            return {
                "success": True,
                "build_complete": True,
                "reason": "all compiled",
                "conflicts": [],
                "evidence_status": "success",
                "evidence": {"class_count": 515},
            }

        def _detect_build_system(self, project_dir):
            return self._cov._detect_build_system(project_dir)

        def scan_modules(self, project_dir, build_system):
            return self._cov.scan_modules(project_dir, build_system)

        def parse_module_test_reports(self, module_dir, report_dirs):
            return self._cov.parse_module_test_reports(module_dir, report_dirs)

    state = RunEvidenceState(run_id="session-httpcomponents")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="all modules built", refs=["output_build"]),
        provenance="output_build",
    )
    # Rebased 2026-08-14 (spec amendment item 9) onto the provenance a live
    # dispatch states: a receipt-scoped rollup. This fixture's subject is the
    # module-coverage shell, and a headline count without a claim partition is
    # exactly what the finalizer now declines to publish.
    state.register_fact(
        StateScope.TEST_RUNTIME,
        "test.stats",
        {
            "discovered": 2255,
            "unique": {"executed": 2255, "passed": 2255, "failed": 0, "errors": 0, "skipped": 0},
            "raw": {"executed": 2255, "passed": 2255, "failed": 0, "errors": 0, "skipped": 0},
            "receipt_scoped": True,
            # Isolate module coverage from the independently satisfied test gate.
            "execution_state": "completed",
        },
        "output_tests",
    )
    orchestrator = Orch()
    authority = EvidencePublicationAuthority(
        run_id=state.run_id,
        sink=Sink(),
    )
    token = install_evidence_publication_authority(authority, orchestrator=orchestrator)
    reset_evidence_publication_authority(token)
    finalizer = VerdictFinalizer(
        orchestrator,
        validator=V(inner),
        project_name="httpcomponents-client",
    )
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
        phase="build",
        signal="done",
        claimed_outcome="partial",
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
        phase="build",
        signal="blocked",
        claimed_outcome="failed",
        reason="maven island will not compile",
    )
    gate = check_phase_claim("build", claim, _gate_validator(), None, "bigtop")
    # blocked against green top-level evidence: still not accepted as blocked…
    text = " ".join([gate.reason or "", *(gate.suggestions or ())])
    # …but the response tells the agent what the evidence actually shows and
    # what it can do next (continue unbuilt modules / claim done honestly).
    assert "1/4 built" in text or "no output yet" in text


# ---- Loop guidance names factual untried coordinates (fix 3) ----

from types import SimpleNamespace

from sag.agent.react_engine import ReActEngine


def _loop_engine(islands, observed_workdirs, *, receipt=True, tool_name="build"):
    """The redirect reads islands from the SHARED manifest (panel review: the
    trunk recommendation is projected by treatment dim (b), so sourcing there
    made the allowlisted loop differ across arms)."""
    from sag.agent.evidence_state import RunEvidenceState, StateScope
    from sag.tools.base import ToolResult

    engine = ReActEngine.__new__(ReActEngine)
    run_id = "module-loop"
    target_sha = "c" * 40
    durable_receipts = {}
    state = RunEvidenceState(run_id=run_id)
    for index, workdir in enumerate(observed_workdirs):
        receipt_id = f"receipt-{index}"
        metadata = {"receipt_id": receipt_id} if receipt else {}
        state.ingest_tool_result(
            StateScope.ARTIFACTS,
            tool_name,
            ToolResult.completed_failure(
                output="compile attempted",
                error="fixture failure",
                metadata=metadata,
            ),
            params={"action": "compile", "working_directory": workdir},
            source_phase="build",
            source_attempt_id="build-1",
            execution_id=f"exec-{index}",
        )
        if receipt:
            durable_receipts[receipt_id] = {
                "schema_version": 3,
                "receipt_id": receipt_id,
                "run_id": run_id,
                "tool": next(
                    (
                        str(island["system"])
                        for island in islands
                        if workdir == island["root"]
                        or workdir.startswith(str(island["root"]).rstrip("/") + "/")
                    ),
                    "maven",
                ),
                "requested_action": "compile",
                "effective_action": "compile",
                "working_directory": workdir,
                "actual_cwd": workdir,
                "target_sha": target_sha,
                "domain_id": workdir,
                "outcome": "failed",
                "exit_code": 1,
            }

    class ManifestOrch:
        def __init__(self):
            self.evidence = strict_published_evidence(
                self,
                run_id=run_id,
                target_sha=target_sha,
                receipts=tuple(durable_receipts.values()),
            )
            # The strict live reader serves only a complete v1 manifest; the
            # islands ride the smallest complete shape (islands[0] is the
            # selected build root, so a multi-island survey is an aggregator).
            add_published_mutable_json(
                self,
                self.evidence,
                path=REQUIREMENTS_PATH,
                record_kind="build_requirements",
                record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                payload=complete_build_requirements_v1(
                    project_root="/workspace/bigtop",
                    **(
                        {
                            "root_shape": "pathological_aggregator",
                            "build_root": str(islands[0]["root"]),
                            "build_islands": islands,
                        }
                        if islands
                        else {}
                    ),
                ),
            )
            self.files = self.evidence.files

        def execute_command(self, command, **kwargs):
            return self.evidence(command)

        # Strict evidence transport refuses a bound project-runtime executor
        # and requires the clean host-control channel.
        execute_control_command = execute_command

    engine.physical_validator = SimpleNamespace(docker_orchestrator=ManifestOrch())
    engine.context_manager = SimpleNamespace(load_trunk_context=lambda: None)
    engine.run_evidence_state = state
    return engine


BIGTOP_ISLANDS = [
    {"root": "/workspace/bigtop/bigtop-test-framework", "system": "maven", "goal": "install"},
    {
        "root": "/workspace/bigtop/bigtop-data-generators",
        "system": "gradle",
        "goal": "publishToMavenLocal",
    },
    {
        "root": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark",
        "system": "gradle",
        "goal": "build",
    },
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
    factual_line = text.split("Untried surveyed build coordinates:", 1)[1]
    assert "bigtop-data-generators" in text
    assert "bigpetstore-spark" in text
    assert "bigtop-test-framework" not in factual_line
    assert "gradle at /workspace/bigtop/bigtop-data-generators" in factual_line
    assert "publishToMavenLocal" not in factual_line
    assert "goal" not in factual_line.lower()
    assert "build(action=" not in factual_line


def test_loop_guidance_does_not_treat_an_unreceipted_observation_as_tried():
    engine = _loop_engine(
        BIGTOP_ISLANDS,
        observed_workdirs=["/workspace/bigtop/bigtop-test-framework"],
        receipt=False,
    )

    text = engine._loop_guidance(_guide_decision())
    assert "maven at /workspace/bigtop/bigtop-test-framework" in text


def test_loop_guidance_does_not_treat_a_non_build_receipt_as_an_island_attempt():
    engine = _loop_engine(
        BIGTOP_ISLANDS,
        observed_workdirs=["/workspace/bigtop/bigtop-test-framework"],
        tool_name="project",
    )

    text = engine._loop_guidance(_guide_decision())
    assert "maven at /workspace/bigtop/bigtop-test-framework" in text


def test_loop_guidance_stays_clean_when_all_islands_tried_or_no_islands():
    engine = _loop_engine(BIGTOP_ISLANDS, observed_workdirs=[i["root"] for i in BIGTOP_ISLANDS])
    assert "Untried" not in engine._loop_guidance(_guide_decision())
    engine2 = _loop_engine([], observed_workdirs=[])
    assert "Untried" not in engine2._loop_guidance(_guide_decision())


def test_loop_guidance_ignores_a_tampered_manifest_mirror():
    engine = _loop_engine(BIGTOP_ISLANDS, observed_workdirs=[])
    orchestrator = engine.physical_validator.docker_orchestrator
    orchestrator.evidence.files[REQUIREMENTS_PATH] += " "

    assert "Untried" not in engine._loop_guidance(_guide_decision())


# ---- Test phase reads the native-core state before sweeping (fix 4) ----

from test_python_phase_guidance import _engine_at, _python_env


def _native_env():
    env = _python_env()
    env["build_recommendation"]["has_native_build"] = True
    env["build_recommendation"]["build_root"] = "/workspace/tvm/python"
    return env


def test_test_phase_suggests_smoke_when_native_core_not_built():
    """Live TVM (twice): the agent swept the full suite without libtvm — 356
    identical collection errors. The REACTIVE smoke steer fires on every intro
    (there is no brief projection to hide behind after the analyzer diet).

    Plan 5 Stage E: the "not built" wording is now gated on the ARTIFACT PROBE,
    so this case scripts a probe that finds no shared objects — the one state
    where the claim is a fact (probe-present/probe-failed states live in
    tests/test_native_capability_state.py)."""
    env = _native_env()
    engine = _engine_at(3, env)  # mark_done -> legacy outcome unknown
    engine.physical_validator = SimpleNamespace(
        docker_orchestrator=SimpleNamespace(
            execute_command=lambda command, **kwargs: {
                "success": True,
                "exit_code": 0,
                "output": "",
            }
        )
    )
    intro = engine._phase_intro_step().content
    assert "smoke" in intro.lower()
    assert "collection errors" in intro


def test_test_phase_stays_clean_when_native_built_or_not_native():
    # native repo but build phase succeeded -> no smoke detour
    env = _native_env()
    engine = _engine_at(3, env)
    for record in engine.phase_machine.records:
        if record.phase == "build":
            object.__setattr__(record, "validated_outcome", "success")
    built_intro = engine._phase_intro_step().content
    assert "The NATIVE core was not built" not in built_intro
    assert "Do NOT sweep the full suite" not in built_intro
    # plain (non-native) python repo -> the pytest FACTS objective, no reactive
    # smoke steer (the steer is native-only and evidence-triggered).
    engine2 = _engine_at(3, _python_env())
    intro2 = engine2._phase_intro_step().content
    assert "terminal Python runner evidence" in intro2
    assert "build(action=" not in intro2
    assert "The NATIVE core was not built" not in intro2
    assert "Do NOT sweep the full suite" not in intro2


# ---- Island-keyed checklist: actionable coordinates, not raw module names ----


def test_checklist_prefers_islands_with_full_roots_and_goals():
    """bigtop6 live: the module-scan checklist showed 15 basenames (half noise:
    site, test-artifacts) with no paths or commands — the agent stayed at the
    root for 86 calls. With islands known, the checklist must be keyed to the
    4 actionable islands, each with its FULL root and goal."""
    islands = [
        {"root": "/workspace/bigtop/bigtop-test-framework", "system": "maven", "goal": "install"},
        {
            "root": "/workspace/bigtop/bigtop-data-generators",
            "system": "gradle",
            "goal": "publishToMavenLocal",
        },
        {
            "root": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark",
            "system": "gradle",
            "goal": "build",
        },
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


def test_build_grain_rates_keep_class_and_source_counts_diagnostic():
    from sag.agent.module_coverage import build_grain_rates

    coverage = {"summary": {"modules_total": 14, "modules_built": 12}}
    grains, conflicts = build_grain_rates(coverage, compiled_classes=3400, source_files=3412)
    assert conflicts == ()

    assert grains["modules"].payload() == {
        "rate": 85.7,
        "band": "most",
        "numerator": 12,
        "denominator": 14,
    }
    assert grains["classes"].payload() == {
        "band": "unavailable",
        "reason": (
            "class files are diagnostic and not comparable to Java source files "
            "(3400 class files observed; 3412 production Java sources observed)"
        ),
    }


def test_build_grain_rates_type_their_absences():
    from sag.agent.module_coverage import build_grain_rates

    grains, conflicts = build_grain_rates(None, compiled_classes=None, source_files=None)
    assert conflicts == ()

    assert grains["modules"].payload() == {
        "band": "unavailable",
        "reason": "no module scan available",
    }
    assert grains["classes"].payload() == {
        "band": "unavailable",
        "reason": "class files are diagnostic and not comparable to Java source files",
    }


# ---------------------------------------------------------------------------
# A DEGENERATE module scan is not a zero — but a measured one is
# ---------------------------------------------------------------------------


def _measured_rows(count, *, class_count=0):
    """Rows a scan actually measured: it looked in each module and counted."""
    return [
        {
            "path": f"m{i}",
            "class_count": class_count,
            "build_status": "unknown",
            "build_source": "none",
        }
        for i in range(count)
    ]


def test_a_module_scan_far_smaller_than_the_declaration_is_a_contradiction():
    """D2 2026-08-12: kafka compiled 11,421 classes and the physical oracle
    judged the build partial, yet the module scan reported 0 built of a
    denominator of 2 — for a Gradle build declaring 41 subprojects centrally.
    The derived word's rule (modules `none` -> failed) then overrode the oracle
    and sealed `failed`.

    Premise updated: the trigger is the SCAN's own degeneracy (2 enumerated of
    41 declared), not "class files exist" — a project-wide .class census also
    counts checked-in test fixtures, so keying on it masked genuine failures.
    A scan that missed most of the build is not a measurement of zero."""
    from sag.agent.module_coverage import (
        MODULE_SCAN_CONTRADICTED_CONFLICT,
        build_grain_rates,
    )

    coverage = {
        "summary": {"modules_total": 2, "modules_built": 0, "modules_declared": 41},
        "modules": _measured_rows(2),
    }
    grains, conflicts = build_grain_rates(coverage, compiled_classes=11421, source_files=1319)

    assert grains["modules"].band == "unavailable"
    reason = grains["modules"].reason or ""
    assert "41" in reason and "2" in reason, "the degeneracy is named, not just asserted"
    assert conflicts == (MODULE_SCAN_CONTRADICTED_CONFLICT,)


def test_a_scan_that_measured_no_module_at_all_is_a_contradiction():
    """The other degeneracy: rows exist but every one is unmeasured (the class
    probe failed, no receipt spoke for it). Nothing there says zero either."""
    from sag.agent.module_coverage import (
        MODULE_SCAN_CONTRADICTED_CONFLICT,
        build_grain_rates,
    )

    coverage = {
        "summary": {"modules_total": 3, "modules_built": 0},
        "modules": _measured_rows(3, class_count=None),
    }
    grains, conflicts = build_grain_rates(coverage, compiled_classes=1418, source_files=900)

    assert grains["modules"].band == "unavailable"
    assert conflicts == (MODULE_SCAN_CONTRADICTED_CONFLICT,)


def test_a_reactor_that_failed_every_module_is_a_measurement_not_a_blind_scan():
    """A reactor row legitimately carries no class count — no disk scan matched
    it — yet Maven itself stated the outcome. That is a measurement, so 0 of 51
    stays `none` even in a repo shipping .class fixtures."""
    from sag.agent.module_coverage import build_grain_rates

    coverage = {
        "summary": {"modules_total": 51, "modules_built": 0},
        "modules": [
            {
                "path": "",
                "class_count": None,
                "build_status": "failure",
                "build_source": "reactor",
            }
            for _ in range(51)
        ],
    }
    grains, conflicts = build_grain_rates(coverage, compiled_classes=37, source_files=4100)

    assert grains["modules"].band == "none"
    assert conflicts == ()


def test_checked_in_class_fixtures_do_not_mask_a_genuinely_failed_build():
    """The mirror image of the kafka misgrade, and the test the earlier fix
    could not write: `compiled_classes` is a project-wide `find -name '*.class'`
    that counts CHECKED-IN .class fixtures. Keyed on that census, a repo that
    ships fixtures and then fails every module banded `unavailable` and sealed
    PARTIAL. The scan here measured all 51 modules and found nothing: that is a
    zero, and the verdict may say failed."""
    from sag.agent.module_coverage import build_grain_rates

    coverage = {
        "summary": {"modules_total": 51, "modules_built": 0, "modules_declared": 51},
        "modules": _measured_rows(51),
    }
    grains, conflicts = build_grain_rates(coverage, compiled_classes=37, source_files=4100)

    assert grains["modules"].band == "none"
    assert conflicts == ()


def test_a_genuinely_empty_build_still_bands_none():
    """camel in the same campaign: 0 classes AND 0 of 51 modules. Nothing
    contradicts anything, so `none` stands and the verdict may say failed."""
    from sag.agent.module_coverage import build_grain_rates

    coverage = {"summary": {"modules_total": 51, "modules_built": 0}}
    grains, conflicts = build_grain_rates(coverage, compiled_classes=0, source_files=None)

    assert grains["modules"].band == "none"
    assert conflicts == ()


def test_a_census_that_counted_zero_makes_even_a_degenerate_scan_a_none():
    """A degenerate scan states nothing — but a census that positively counted
    ZERO class files anywhere in the tree corroborates the zero independently,
    so there is no contradiction left to state."""
    from sag.agent.module_coverage import build_grain_rates

    coverage = {
        "summary": {"modules_total": 2, "modules_built": 0, "modules_declared": 41},
        "modules": _measured_rows(2),
    }
    grains, conflicts = build_grain_rates(coverage, compiled_classes=0, source_files=1319)

    assert grains["modules"].band == "none"
    assert conflicts == ()


def test_an_honest_kafka_fraction_is_never_touched_by_the_guard():
    """With the enumeration taught the settings.gradle include list, the kafka
    shape produces a real fraction (38 of 41 built) and the guard — now a rare
    fallback — must leave it exactly as measured."""
    from sag.agent.module_coverage import build_grain_rates

    coverage = {
        "summary": {"modules_total": 41, "modules_built": 38, "modules_declared": 41},
        "modules": _measured_rows(41, class_count=120),
    }
    grains, conflicts = build_grain_rates(coverage, compiled_classes=11421, source_files=13190)

    assert grains["modules"].payload() == {
        "rate": 92.7,
        "band": "most",
        "numerator": 38,
        "denominator": 41,
    }
    assert conflicts == ()


def test_the_contradicted_grain_cannot_manufacture_a_failed_word():
    """Premise updated with its sibling above: the contradiction is now keyed on
    the degenerate scan (2 enumerated of 41 declared)."""
    from sag.agent.module_coverage import build_grain_rates
    from sag.verdict_rates import GrainRate, derived_verdict_word

    grains, _ = build_grain_rates(
        {
            "summary": {"modules_total": 2, "modules_built": 0, "modules_declared": 41},
            "modules": _measured_rows(2),
        },
        compiled_classes=11421,
        source_files=1319,
    )

    # Kafka's real shape: contradicted modules, no tests driven.
    assert derived_verdict_word("success", GrainRate(0, 20497)) == "partial"


def test_module_coverage_states_how_many_modules_the_build_declared():
    """The scan carries the DECLARED count to the summary, so a reader can tell
    a small project (2 of 2) from a blind scan (2 of 41)."""
    validator = FakeValidator(
        primary="gradle",
        by_system={
            "gradle": [
                {
                    "path": ".",
                    "name": ".",
                    "class_count": 0,
                    "jar_count": 0,
                    "report_dirs": [],
                    "has_test_sources": False,
                    "declared_modules": 41,
                },
                {
                    "path": "clients",
                    "name": "clients",
                    "class_count": 1200,
                    "jar_count": 1,
                    "report_dirs": [],
                    "has_test_sources": True,
                    "declared_modules": 41,
                },
            ]
        },
    )
    coverage = module_coverage(validator, "kafka")
    assert coverage["summary"]["modules_declared"] == 41
