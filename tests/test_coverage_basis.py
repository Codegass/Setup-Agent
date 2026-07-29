# tests/test_coverage_basis.py
"""Plan 8 stages 3-4 — a check that could derive nothing said everything passed.

Live evidence: p7d polaris (`logs/session_20260729_111737_22356`). The model
claimed `partial` while its compile job was still running, and the build gate
UPGRADED the claim to success on this one sentence:

    Built 100% of expected classes (>= 100% threshold) · Module coverage: 1/26
    built [build-logic] · no output yet: [., aggregated-license-report,
    iceberg-service, management-model, management-service,
    polaris-catalog-service +19 more] · tests ran in 0/8 test-bearing modules

One hundred percent and one-of-twenty-six, in the same sentence, in that order:
the first half decided, the second half decorated.

The chain (walked from the receipt, not guessed — the `d5dc330` narrowing never
ran, because its input is a receipt's `module_outcomes` and the run's only
receipt, the failed Java-17 compile, has no such field):

1. the survey cannot parse polaris's Kotlin settings, so
   `root_shape: single_module`, `build_islands: []` — NO per-module class
   expectation could be derived;
2. with nothing to check, `_verify_expected_artifacts` returned
   `class_coverage: 1.0` (physical_validator.py, the `classes_expected > 0`
   else-branch) and `validate_build_status` read that as ">= threshold". The
   hard JVM gate catches only the zero-classes case, and the in-flight job had
   already compiled `build-logic`, so `class_count` was 1,706;
3. `module_coverage()` — which KNEW 1/26 — reached the sentence through
   `phase_gates.py:1010-1012`, appended to a string nobody consumed.

Spec §2 P2: a check that cannot derive its expectations returns "no basis" —
never "met", never "unmet". Spec §2 P3: the decider and the display consume the
same object. Spec §3.5: the denominator has an authority order, and the message
names which one it used.
"""

from test_physical_validator import FakeBuildOrchestrator, _coverage_validator

from sag.agent.module_coverage import ModuleBasis, module_basis
from sag.agent.physical_validator import PhysicalValidator

# The 26 subprojects polaris's `settings.gradle.kts` registers imperatively and
# the survey never saw. Only `build-logic` had output when the gate graded.
POLARIS_MODULES = [
    ".",
    "aggregated-license-report",
    "build-logic",
    "iceberg-service",
    "management-model",
    "management-service",
    "polaris-catalog-service",
    *[f"plugins/plugin-{n}" for n in range(19)],
]


def _polaris_scan(built=("build-logic",)):
    """`module_coverage`'s rollup shape for the p7d polaris tree."""
    return {
        "project_dir": "/workspace/polaris",
        "summary": {
            "modules_total": len(POLARIS_MODULES),
            "modules_built": len(built),
            "modules_test_bearing": 8,
            "modules_tested": 0,
        },
        "modules": [
            {
                "path": path,
                "build_status": "success" if path in built else "no_output",
            }
            for path in POLARIS_MODULES
        ],
    }


def _polaris_validator(*, class_count=1706, has_artifacts=True, scan=None):
    """A gradle build whose expectations are jar-only: nothing class-based can
    be derived, which is exactly what polaris's unparsed settings produced."""
    orch = FakeBuildOrchestrator(files={"/workspace/polaris/settings.gradle.kts"})
    validator = PhysicalValidator(
        docker_orchestrator=orch, project_path="/workspace", build_coverage_threshold=1.0
    )
    validator._detect_build_system = lambda *_a, **_k: "gradle"
    validator._check_build_artifacts_complete = lambda _d: {
        "exist": has_artifacts,
        "count": class_count,
        "class_count": class_count,
        "jar_count": 1 if has_artifacts else 0,
    }
    validator._validate_gradle_cache = lambda _d: {
        "valid": True,
        "details": {"build_dirs": 1},
        "subprojects": ["build-logic"],
    }
    validator._collect_artifact_samples = lambda *_a, **_k: []
    validator._get_expected_artifacts = lambda *_a, **_k: [
        {"path": "/workspace/polaris/build/libs", "type": "jar", "artifact": "main JAR"}
    ]
    validator._module_scan_result = lambda *_a, **_k: scan
    return validator


# ---------------------------------------------------------------------------
# §3.4 — coverage carries its basis
# ---------------------------------------------------------------------------
def test_no_derivable_class_expectation_states_no_basis_instead_of_full_coverage():
    """`classes_expected == 0` is "nothing to check", not "everything passed".

    The old else-branch returned 1.0 and every caller read a met threshold.
    """
    validator = _polaris_validator()

    result = validator._verify_expected_artifacts(
        "/workspace/polaris",
        [{"path": "/workspace/polaris/build/libs", "type": "jar", "artifact": "main JAR"}],
    )

    assert result["basis"] == "none"
    assert "class_coverage" not in result


def test_a_derived_expectation_still_states_its_fraction():
    """basis `derived` is today's arithmetic, byte for byte."""
    orch = FakeBuildOrchestrator(
        files={f"/workspace/m/target/classes/C{n}.class" for n in range(5)}
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator._verify_expected_artifacts(
        "/workspace/m",
        [
            {
                "path": "/workspace/m/target/classes",
                "type": "classes",
                "artifact": "compiled classes",
                "min_count": 10,
            }
        ],
    )

    assert result["basis"] == "derived"
    assert result["class_coverage"] == 0.5
    assert result["classes_expected"] == 10
    assert result["classes_found"] == 5


def test_the_polaris_snapshot_is_partial_and_says_what_it_actually_knows():
    """1,706 classes is a fact; "100% of expected" was not one."""
    result = _polaris_validator().validate_build_status("polaris")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_modules_incomplete" in result["conflicts"]
    assert "compiled 1,706 classes" in result["reason"]
    assert "no per-module expectation could be derived" in result["reason"]
    assert "coverage has no basis" in result["reason"]
    assert "100%" not in result["reason"]


def test_no_basis_with_nothing_compiled_is_blocked_not_partial():
    """The zero-classes arm of the same rule — the hard JVM gate, stated once.

    Vendored jars are present (so the old `jvm_no_compiled_evidence` branch,
    which also requires `not has_artifacts`, does not fire); nothing compiled,
    and nothing could be derived. That is BLOCKED, never a partial credit.
    """
    result = _polaris_validator(class_count=0).validate_build_status("polaris")

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"
    assert "compiled" in result["reason"].lower()


def test_a_derived_basis_keeps_todays_thresholds_and_messages():
    """Regression fence: nothing about the derived path moves."""
    result = _coverage_validator(0.5, found=["a"], missing=["b", "c", "d"], threshold=0.75)
    result = result.validate_build_status("m")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert "2 of 4 expected classes" in result["reason"]
    assert "2 short" in result["reason"]


# ---------------------------------------------------------------------------
# §3.5 — the decider and the display are one computation
# ---------------------------------------------------------------------------
def test_the_denominator_authority_is_a_ladder_receipt_then_scan_then_survey():
    """Spec §3.5, in order. A receipt outranks a scan; a scan outranks the
    survey's guess. Nothing else may claim the denominator."""
    scan = _polaris_scan()

    from_receipt = module_basis(scan, receipt_modules=("core", "jms"), receipt_id="inv-gradle-1-2")
    from_scan = module_basis(scan)
    from_survey = module_basis(None)

    assert (from_receipt.authority, from_receipt.provenance) == ("receipt", "inv-gradle-1-2")
    assert from_scan.authority == "scan"
    assert (from_scan.built, from_scan.total) == (1, 26)
    assert from_survey.authority == "survey"


def test_a_one_module_scan_is_not_a_structure_and_does_not_claim_the_denominator():
    """The scan earns the denominator by enumerating modules the expectation
    walk did not have. On one module it is the same subject measured with a
    coarser instrument, and letting it decide would be the same split-brain
    pointing the other way."""
    one_module = {
        "project_dir": "/workspace/m",
        "summary": {"modules_total": 1, "modules_built": 0},
        "modules": [{"path": ".", "build_status": "no_output"}],
    }

    basis = module_basis(one_module)

    assert basis.authority == "survey"
    assert basis.states_a_shortfall is False


def test_the_message_names_the_denominator_it_used():
    """Which computation decided this must be answerable from the sentence."""
    assert "receipt inv-gradle-1-2" in ModuleBasis("receipt", "inv-gradle-1-2", 2, 2).phrase()
    assert "the module scan on disk" in module_basis(_polaris_scan()).phrase()
    assert "1/26" in module_basis(_polaris_scan()).phrase()
    assert "the survey's expectations" in module_basis(None).phrase()


def test_the_polaris_sentence_is_unconstructible():
    """The whole point of stage 4: a reason whose decision half says the
    coverage passed while its commentary half says 1/26 cannot be built,
    because both halves now read the same scan object."""
    from sag.agent.phase_gates import ValidatorState, _inspect_build

    scan = _polaris_scan()
    validator = _polaris_validator(scan=scan)

    observation = _inspect_build(validator, "polaris")

    assert observation.state is not ValidatorState.GREEN
    assert "Module coverage: 1/26 built" in observation.reason
    assert "100%" not in observation.reason
    assert "All expected build artifacts found" not in observation.reason


def test_the_two_halves_of_the_sentence_always_state_the_same_ratio():
    """The pin, as a property rather than one example: whatever the tree looks
    like, the ratio the verdict stood on and the ratio the checklist printed
    are read out of one object, so the sentence cannot say 100% and 1/26.

    Both halves are parsed back out of the composed gate reason — the exact
    string p7d's model was shown — and compared.
    """
    import re

    from sag.agent.phase_gates import ValidatorState, _inspect_build

    for built in (POLARIS_MODULES[:1], POLARIS_MODULES[:13], POLARIS_MODULES):
        scan = _polaris_scan(built=tuple(built))

        observation = _inspect_build(_polaris_validator(scan=scan), "polaris")

        decided = re.search(
            r"denominator: the module scan on disk \((\d+)/(\d+)", observation.reason
        )
        displayed = re.search(r"Module coverage: (\d+)/(\d+) built", observation.reason)
        assert decided and displayed
        assert decided.groups() == displayed.groups() == (str(len(built)), "26")
        assert (observation.state is ValidatorState.GREEN) is False  # basis is still none


def test_a_minority_scan_can_never_be_a_complete_build():
    """The scan is the denominator when no receipt outranks it, so the verdict
    it decorated for one release now decides."""
    result = _polaris_validator(scan=_polaris_scan()).validate_build_status("polaris")

    assert result["build_complete"] is False
    assert "the module scan on disk" in result["reason"]
    assert "1/26" in result["reason"]


def test_a_full_scan_leaves_a_complete_build_complete():
    """The cap is a shortfall cap, not a blanket downgrade."""
    scan = _polaris_scan(built=tuple(POLARIS_MODULES))
    validator = _coverage_validator(1.0, found=["a", "b"], missing=[], threshold=1.0)
    validator._module_scan_result = lambda *_a, **_k: scan

    result = validator.validate_build_status("m")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"


def test_a_receipt_outranks_the_scan_so_the_narrowing_survives():
    """#17/d5dc330 unchanged: a build that stated which modules it attempted is
    measured against those, not against every directory on disk. Capping on the
    scan here would re-break the scoped-build case that narrowing fixed."""
    validator = _coverage_validator(1.0, found=["a", "b"], missing=[], threshold=1.0)
    validator._module_scan_result = lambda *_a, **_k: _polaris_scan()
    validator._attempted_modules = lambda: ("a", "b")
    validator._receipt_structure = lambda: {"provenance": "inv-maven-1-0001", "modules": ["a", "b"]}

    result = validator.validate_build_status("m")

    assert result["build_complete"] is True
    assert "receipt inv-maven-1-0001" in result["reason"]


def test_the_decider_and_the_display_read_one_scan_not_two():
    """Two scans of the same tree is how the two halves drifted apart. The gate
    pass performs exactly one, and both halves consume its result."""
    calls: list[str] = []

    class CountingValidator:
        project_path = "/workspace"

        def __init__(self):
            self._scan = _polaris_scan()

        def _detect_build_system(self, _project_dir):
            return "gradle"

        def scan_modules(self, _project_dir, build_system):
            calls.append(build_system)
            return []

        def validate_build_status(self, project_name):
            self._last_module_scan = (project_name, self._scan)
            return {
                "success": True,
                "build_complete": False,
                "evidence_status": "partial",
                "reason": "compiled 1,706 classes; coverage has no basis",
                "evidence": {"class_count": 1706},
                "conflicts": [],
            }

        def module_scan(self, project_name):
            cached = getattr(self, "_last_module_scan", None)
            if cached and cached[0] == project_name:
                return cached[1]
            from sag.agent.module_coverage import module_coverage

            return module_coverage(self, project_name)

    from sag.agent.phase_gates import _inspect_build

    observation = _inspect_build(CountingValidator(), "polaris")

    assert "Module coverage: 1/26 built" in observation.reason
    assert calls == []  # the validator's scan was reused, never re-run
