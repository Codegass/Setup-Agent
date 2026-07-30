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

import json

from test_physical_validator import FakeBuildOrchestrator, _coverage_validator

from sag.agent.module_coverage import ModuleBasis, module_basis
from sag.agent.physical_validator import INVOCATION_RECEIPTS_DIRNAME, PhysicalValidator

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


def _polaris_validator(
    *,
    class_count=1706,
    has_artifacts=True,
    scan=None,
    jar_on_disk=False,
    attempted=None,
    structure=None,
):
    """A gradle build whose expectations are jar-only: nothing class-based can
    be derived, which is exactly what polaris's unparsed settings produced.

    `jar_on_disk` is the Kotlin/Scala/Groovy case: the same jar-only expectation
    list, and the jar IS there — a derived expectation, met.
    """
    files = {"/workspace/polaris/settings.gradle.kts"}
    if jar_on_disk:
        files.add("/workspace/polaris/build/libs/polaris-1.0.jar")
    orch = FakeBuildOrchestrator(files=files)
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
    validator._attempted_modules = lambda: attempted
    validator._receipt_structure = lambda: structure or {}
    return validator


# The 26 module expectations a Maven reactor survey derives, 10 of 260 expected
# classes on disk. The reviewer's reproduction for the §3.6 narrowing defect.
MAVEN_MODULES = [f"m{index}" for index in range(26)]


def _maven_scan(built=("m0",)):
    """The module scan's rollup for that same 26-module tree."""
    return {
        "project_dir": "/workspace/proj",
        "summary": {"modules_total": len(MAVEN_MODULES), "modules_built": len(built)},
        "modules": [
            {"path": path, "build_status": "success" if path in built else "no_output"}
            for path in MAVEN_MODULES
        ],
    }


def _maven_reactor_validator(*, attempted=None, structure=None, scan=None):
    """A 26-module Maven project where only `m0` compiled (10 of 260 classes)."""
    files = {f"/workspace/proj/m0/target/classes/C{index}.class" for index in range(10)}
    files |= {"/workspace/proj/pom.xml", "/workspace/proj/m0/target/app.jar"}
    orch = FakeBuildOrchestrator(files=files)
    validator = PhysicalValidator(
        docker_orchestrator=orch, project_path="/workspace", build_coverage_threshold=1.0
    )
    validator._detect_build_system = lambda *_a, **_k: "maven"
    validator._check_build_artifacts_complete = lambda _d: {
        "exist": True,
        "count": 10,
        "class_count": 10,
        "jar_count": 1,
    }
    validator._collect_artifact_samples = lambda *_a, **_k: []
    validator._get_expected_artifacts = lambda *_a, **_k: [
        {
            "path": f"/workspace/proj/{module}/target/classes",
            "type": "classes",
            "artifact": f"{module} classes",
            "min_count": 10,
        }
        for module in MAVEN_MODULES
    ]
    validator._module_scan_result = lambda *_a, **_k: scan
    validator._attempted_modules = lambda: attempted
    validator._receipt_structure = lambda: structure or {}
    return validator


def _met_expectation_validator(*, scan, attempted=None, structure=None, class_count=10):
    """A Maven build whose ONE derived class expectation is fully MET on disk.

    The point of the shape: with `all_present` True and `class_coverage` 1.0 at a
    100% threshold, nothing else in `validate_build_status` stands between this
    state and a complete success — so whether the build is complete is decided by
    the §3.5 denominator rung alone, and deleting the cap changes the verdict.
    `_verify_expected_artifacts` is the real one: the classes are on the fake
    filesystem and are counted there.
    """
    files = {f"/workspace/proj/m0/target/classes/C{index}.class" for index in range(10)}
    files |= {"/workspace/proj/pom.xml", "/workspace/proj/m0/target/app.jar"}
    orch = FakeBuildOrchestrator(files=files)
    validator = PhysicalValidator(
        docker_orchestrator=orch, project_path="/workspace", build_coverage_threshold=1.0
    )
    validator._detect_build_system = lambda *_a, **_k: "maven"
    validator._check_build_artifacts_complete = lambda _d: {
        "exist": True,
        "count": class_count,
        "class_count": class_count,
        "jar_count": 1,
    }
    validator._collect_artifact_samples = lambda *_a, **_k: []
    validator._get_expected_artifacts = lambda *_a, **_k: [
        {
            "path": "/workspace/proj/m0/target/classes",
            "type": "classes",
            "artifact": "m0 classes",
            "min_count": 10,
        }
    ]
    validator._module_scan_result = lambda *_a, **_k: scan
    validator._attempted_modules = lambda: attempted
    validator._receipt_structure = lambda: structure or {}
    return validator


class ReceiptOrchestrator:
    """A container holding invocation receipts, one JSON object per line —
    which is what `cat <receipts dir>/*.json` returns for the schema-v1 files."""

    def __init__(self, receipts, *, present=True):
        self.receipts = list(receipts)
        self.present = present

    def execute_command(self, command, **_kwargs):
        text = command.strip()
        if INVOCATION_RECEIPTS_DIRNAME not in text:
            return {"success": True, "exit_code": 0, "output": ""}
        if text.startswith("test -d"):
            return {
                "success": self.present,
                "exit_code": 0 if self.present else 1,
                "output": "EXISTS" if self.present else "",
            }
        if text.startswith("cat "):
            body = "\n".join(json.dumps(receipt) for receipt in self.receipts)
            return {"success": True, "exit_code": 0, "output": body}
        return {"success": True, "exit_code": 0, "output": ""}


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
    assert "no class-based expectation could be derived" in result["reason"]
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


def test_a_derived_and_met_jar_expectation_is_a_basis_and_stays_a_full_success():
    """"No basis" means NO expectation of any kind could be derived.

    A Kotlin/Scala/Groovy module keeps its sources in `src/main/kotlin`, so the
    parsers emit only the JAR expectation (the `classes` entry is appended
    solely when `test -d <dir>/src/main/java` succeeds). The build ran, the jar
    is on disk, 900 classes compiled: `all_present` is True, `classes_expected`
    is 0. Keying the branch on `classes_expected == 0` preempted the
    `all_present` check, so such a project could no longer reach a full success
    at all — and the sentence it got instead said something untrue.
    """
    validator = _polaris_validator(class_count=900, jar_on_disk=True)

    coverage = validator._verify_expected_artifacts(
        "/workspace/polaris", validator._get_expected_artifacts()
    )
    result = validator.validate_build_status("polaris")

    assert (coverage["all_present"], coverage["classes_expected"]) == (True, 0)
    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["conflicts"] == []
    assert "All expected build artifacts found: main JAR" in result["reason"]
    assert "no basis" not in result["reason"]


def test_an_unmet_expectation_with_no_class_number_names_what_is_missing():
    """The other direction, and the p7d state exactly: a jar expectation WAS
    derived and was NOT met, and no class-weighted number exists to size the
    shortfall with. Partial — and the sentence says which artifact is missing
    rather than implying nothing was expected."""
    result = _polaris_validator().validate_build_status("polaris")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "main JAR" in result["reason"]
    assert "no class-based expectation could be derived" in result["reason"]


def test_a_met_expectation_with_nothing_compiled_is_blocked_not_a_complete_success():
    """The arm round two re-opened.

    `and not coverage_info["all_present"]` routed basis-`none` with ZERO compiled
    classes into the met branch, so a jar on disk (checked in, vendored, or left
    by an earlier build) and nothing compiled graded as a complete success. §3.4
    mandates BLOCKED for the zero-classes arm, and `jvm_no_compiled_evidence` does
    not catch it: that branch also requires `not has_artifacts`, and the jar IS an
    artifact.
    """
    result = _polaris_validator(class_count=0, jar_on_disk=True).validate_build_status("polaris")

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"
    assert "No compiled .class files found for gradle build" in result["reason"]
    # And the sentence states which of the two zero-class shapes this is: the
    # expectations were MET and nothing compiled, which is a different fact from
    # "nothing compiled and the expectations are missing too".
    assert (
        "the expected artifact(s) are present (main JAR) but nothing compiled" in result["reason"]
    )
    assert "All expected build artifacts found" not in result["reason"]


def test_the_four_arms_of_the_no_basis_rule_are_pinned_as_a_set():
    """§3.4's four cases, stated together because fixing one re-opened the other.

    Round one keyed the branch on "no class-based expectation" alone, so a met JAR
    expectation (a Kotlin module: sources under `src/main/kotlin`, no `classes`
    entry derived) could not reach a full success. Round two added
    `and not all_present`, which re-opened the zero-classes direction. The two
    questions are orthogonal — was a derived expectation MET, and did anything
    compile — and only pinning the whole table keeps a later fix to one arm from
    breaking another.
    """

    def _verdict(**kwargs):
        result = _polaris_validator(**kwargs).validate_build_status("polaris")
        return result["evidence_status"], result["success"], result["build_complete"]

    # (expectation unmet, classes compiled) -> PARTIAL: real output, unknown share
    assert _verdict(class_count=1706, jar_on_disk=False) == ("partial", True, False)
    # (expectation unmet, nothing compiled) -> BLOCKED
    assert _verdict(class_count=0, jar_on_disk=False) == ("blocked", False, False)
    # (expectation MET, classes compiled) -> SUCCESS: a met expectation is a basis
    assert _verdict(class_count=900, jar_on_disk=True) == ("success", True, True)
    # (expectation MET, nothing compiled) -> BLOCKED: the hard JVM gate
    assert _verdict(class_count=0, jar_on_disk=True) == ("blocked", False, False)


def test_a_derived_basis_keeps_todays_thresholds_and_messages():
    """Regression fence: nothing about the derived path moves."""
    result = _coverage_validator(0.5, found=["a"], missing=["b", "c", "d"], threshold=0.75)
    result = result.validate_build_status("m")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert "2 of 4 expected classes" in result["reason"]
    assert "2 short" in result["reason"]


# ---------------------------------------------------------------------------
# one predicate, one answer: terminality gates the denominator too
# ---------------------------------------------------------------------------
def _receipt_validator(receipts):
    return PhysicalValidator(
        docker_orchestrator=ReceiptOrchestrator(receipts), project_path="/workspace"
    )


def _killed_receipt():
    """The OOM case: `collect_detached_result` synthesized exit 1 and recorded
    `lifecycle_state: vanished`, and Gradle had printed 40 of 300 modules."""
    return {
        "receipt_id": "inv-gradle-1-0007",
        "exit_code": 1,
        "lifecycle_state": "vanished",
        "module_outcomes": [{"module": f"m{index}", "status": "SUCCESS"} for index in range(40)],
    }


def test_a_crashed_dispatchs_truncated_module_list_is_not_a_denominator():
    """`dispatch_terminated` refuses this receipt as a STRUCTURE prover; the
    denominator ladder asked the same question of the same receipt and answered
    differently, so a truncated list became the `receipt` rung — coverage narrowed
    to the 40 modules the kill happened to reach, and the shortfall in the other
    260 read as untried rather than unbuilt. One predicate, one answer."""
    assert _receipt_validator([_killed_receipt()])._attempted_modules() is None


def test_a_dispatch_something_else_stopped_is_not_a_denominator_either():
    """A timeout monitor's kill states its reason and truncates the log the same
    way; the exit code (143) is an int, which is exactly what could not tell them
    apart."""
    killed = dict(_killed_receipt(), lifecycle_state=None, termination_reason="silent_timeout")
    killed.pop("lifecycle_state")

    assert _receipt_validator([killed])._attempted_modules() is None


def test_a_terminal_receipt_still_sets_the_denominator():
    """The regression fence for the guard above: a dispatch that recorded its own
    exit (and a synchronous one, which states no lifecycle at all) still narrows
    coverage to what it attempted — and a crashed receipt beside it contributes
    nothing rather than poisoning the list."""
    finished = {
        "receipt_id": "inv-maven-1-0001",
        "exit_code": 1,
        "lifecycle_state": "finished",
        "module_outcomes": [{"module": "core", "status": "SUCCESS"}],
    }
    synchronous = {
        "receipt_id": "inv-maven-1-0002",
        "exit_code": 0,
        "module_outcomes": [{"module": "jms", "status": "SUCCESS"}],
    }

    assert _receipt_validator([finished])._attempted_modules() == ("core",)
    assert _receipt_validator([synchronous])._attempted_modules() == ("jms",)
    assert _receipt_validator([_killed_receipt(), finished, synchronous])._attempted_modules() == (
        "core",
        "jms",
    )


# ---------------------------------------------------------------------------
# §3.5 — the decider and the display are one computation
# ---------------------------------------------------------------------------
def test_the_denominator_authority_is_a_ladder_receipt_then_scan_then_survey():
    """Spec §3.5, in order. A receipt outranks a scan; a scan outranks the
    survey's guess. Nothing else may claim the denominator."""
    scan = _polaris_scan()

    from_receipt = module_basis(
        scan,
        denominator_modules=("core", "jms"),
        structure={"provenance": "inv-gradle-1-2", "modules": ["core", "jms"]},
    )
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
    # A run whose manifest predates the structure fact still stands on the
    # receipt rung, and says so rather than naming an id it does not have.
    unpromoted = module_basis(_polaris_scan(), denominator_modules=("core", "jms"))
    assert unpromoted.phrase() == (
        "denominator: the receipts' module outcomes (2 module(s) attempted)"
    )


def test_a_receipt_is_named_only_for_a_denominator_it_actually_stated():
    """The denominator is the UNION of every receipt's module outcomes; the
    promoted structure fact is ONE receipt's statement (§3.6). Naming that id
    over a union it never stated attributes a denominator to a receipt that did
    not claim it — a falsehood in one word."""
    stated_by_one = module_basis(
        None,
        denominator_modules=("core", "jms"),
        structure={"provenance": "inv-maven-1-0001", "modules": ["core", "jms"]},
    )
    union_of_several = module_basis(
        None,
        denominator_modules=("core", "jms", "http"),
        structure={"provenance": "inv-maven-1-0001", "modules": ["core", "jms"]},
    )

    assert "receipt inv-maven-1-0001" in stated_by_one.phrase()
    assert union_of_several.provenance == ""
    assert union_of_several.phrase() == (
        "denominator: the receipts' module outcomes (3 module(s) attempted)"
    )


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


def test_a_persisted_structure_never_narrows_this_passs_denominator():
    """The P0-F direction, on the #17 narrowing input.

    26 module expectations, 10 of 260 expected classes built. The manifest
    carries a receipt-proven structure from an earlier SCOPED dispatch
    (`mvn -pl m0` -> modules `['m0']`), and `_attempted_modules()` returns None
    for this pass — it does so whenever the receipts probe fails, and both
    `_invocation_receipts_present()` and `_attempted_modules()` swallow every
    exception, so one flaky container `test -d` or `cat` is enough.

    Feeding the persisted structure into `attempted_modules` made
    `_scope_expectations_to_attempted` drop the other 25 expectations as
    "untried": coverage 1/1, `basis` receipt-proven, the module-scan shortfall
    cap disarmed, and a partial build refined UPWARD into a complete success.
    Narrowing the denominator is licensed only by what THIS pass's receipts say
    they attempted.
    """
    result = _maven_reactor_validator(
        structure={"provenance": "inv-maven-2-0002", "modules": ["m0"], "keys": ["m0"]}
    ).validate_build_status("proj")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "Built 10 of 260 expected classes" in result["reason"]
    assert "250 short" in result["reason"]
    assert "25 module(s) incomplete" in result["reason"]
    assert "inv-maven-2-0002" not in result["reason"]
    assert result["evidence"].get("modules_attempted") is None


def test_this_passs_own_receipts_still_narrow_exactly_as_before():
    """The regression fence: #17/d5dc330 is untouched. A scoped dispatch THIS
    pass made is measured against the modules it said it attempted."""
    result = _maven_reactor_validator(attempted=("m0",)).validate_build_status("proj")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["evidence"]["modules_attempted"] == ["m0"]


def test_a_receipt_outranks_the_scan_so_the_narrowing_survives():
    """#17/d5dc330 unchanged: a build that stated which modules it attempted is
    measured against those, not against every directory on disk. Capping on the
    scan here would re-break the scoped-build case that narrowing fixed.

    The state is one where the narrowing SUCCEEDS — `mvn -pl m0` against 26
    derived module expectations, so every attempted module maps and the other 25
    leave the denominator as `modules_untried`. The earlier version of this fence
    used an expectation path ('x') that the attempted modules ('a','b') could not
    map onto, which is the state where scoping REFUSES to narrow: it pinned the
    premise the round-three blocker was about.
    """
    result = _maven_reactor_validator(
        attempted=("m0",),
        scan=_maven_scan(),
        structure={"provenance": "inv-maven-1-0001", "modules": ["m0"], "keys": ["m0"]},
    ).validate_build_status("proj")

    assert result["evidence"]["modules_untried"] and result["evidence"]["modules_attempted"] == [
        "m0"
    ]
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert "receipt inv-maven-1-0001" in result["reason"]
    assert "1/26" not in result["reason"]


# ---------------------------------------------------------------------------
# §3.5 round three — the authority follows the denominator the run ACTUALLY used
# ---------------------------------------------------------------------------
def test_a_receipt_that_did_not_set_the_denominator_leaves_the_scan_cap_live():
    """The reviewer's round-three reproduction, which is the p7d polaris state.

    The survey cannot read `settings.gradle.kts`, so the whole derived expectation
    list is the single root `main JAR` — and it IS on disk; 1,706 classes are
    compiled; the scan walked 26 subprojects and found one built; the run's one
    receipt names `build-logic`. `build-logic` cannot be mapped onto a
    `/build/libs` expectation, so `_scope_expectations_to_attempted` returns the
    WIDE list and records `build_coverage_scope_unverified` — the call itself
    states the denominator was NOT narrowed to what the build attempted. The
    receipt therefore did not set the denominator; the survey's wide expectation
    list did. So the authority is not `receipt` in this state and the §3.5
    minority-scan cap is live.

    Before this fix the mere EXISTENCE of a receipt returned authority `receipt`,
    `states_a_shortfall` was False by construction, and this exact sentence graded
    as a complete success: "All expected build artifacts found: main JAR ·
    denominator: the receipts' module outcomes (1 module(s) attempted) · Module
    coverage: 1/26 built [build-logic]".
    """
    result = _polaris_validator(
        jar_on_disk=True, scan=_polaris_scan(), attempted=("build-logic",)
    ).validate_build_status("polaris")

    assert "build_coverage_scope_unverified" in result["evidence"]["conflicts"]
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "the module scan on disk" in result["reason"]
    assert "1/26" in result["reason"]
    assert "module(s) attempted" not in result["reason"]


def test_the_unnarrowed_state_cannot_pair_a_passing_verdict_with_a_minority_scan():
    """Spec §6 acceptance 1, on the state that reached it in p7d.

    The gate's composed sentence is the thing the model was shown, so the pin is
    on that string. The verdict is no longer passing; and the clause that DECIDED
    now leads it, with the coverage check's own finding kept but subordinated — a
    met one-jar expectation is a true fact and stays in the sentence, it just may
    not stand at the head of a sentence whose next clause says 1/26.
    """
    from sag.agent.phase_gates import ValidatorState, _inspect_build

    observation = _inspect_build(
        _polaris_validator(jar_on_disk=True, scan=_polaris_scan(), attempted=("build-logic",)),
        "polaris",
    )

    assert observation.state is not ValidatorState.GREEN
    assert observation.reason.startswith(
        "Not a complete build — the module scan owns the denominator and 1 of 26 modules"
    )
    assert "(coverage check: All expected build artifacts found: main JAR)" in observation.reason
    assert "Module coverage: 1/26 built" in observation.reason
    assert "100%" not in observation.reason


def test_the_authority_is_the_scoping_outcome_not_the_existence_of_a_receipt():
    """P3 applied to the mechanism that was supposed to implement P3.

    `module_basis` used to decide the authority in PARALLEL with the scoping call
    — from "a receipt stated some modules" — while the scoping outcome recorded
    that the denominator had not been narrowed. Two computations answering one
    question; the wrong one decided. The authority now takes the modules the
    scoping outcome says actually SET the denominator, so an unnarrowed pass
    stands on the scan.
    """
    scan = _polaris_scan()
    validator = _polaris_validator(jar_on_disk=True, scan=scan, attempted=("build-logic",))
    wide = validator._get_expected_artifacts()

    scope = validator._scope_expectations_to_attempted(list(wide), ("build-logic",))
    narrowed = validator._scope_expectations_to_attempted(
        [{"path": "/workspace/polaris/build-logic/target/classes", "type": "classes"}],
        ("build-logic",),
    )

    assert (scope.conflict, scope.denominator_modules) == (
        "build_coverage_scope_unverified",
        None,
    )
    assert narrowed.denominator_modules == ("build-logic",)
    assert module_basis(scan, denominator_modules=scope.denominator_modules).authority == "scan"
    assert (
        module_basis(scan, denominator_modules=narrowed.denominator_modules).authority == "receipt"
    )


def test_the_minority_scan_cap_is_what_stops_a_met_coverage_from_completing():
    """The §3.5 cap itself, pinned where deleting it changes the verdict.

    One derived class expectation, fully met, at a 100% threshold: `all_present`
    and `class_coverage == 1.0`, so every other arm of `validate_build_status`
    says complete. What makes this build incomplete is the denominator rung — the
    scan owns it (no receipt narrowed anything) and it counted 1 of 26. Delete
    `ModuleBasis.states_a_shortfall`'s scan clause and this build is a full
    success again, which is the p7d polaris grade.
    """
    validator = _met_expectation_validator(scan=_maven_scan())

    coverage = validator._verify_expected_artifacts(
        "/workspace/proj", validator._get_expected_artifacts()
    )
    result = validator.validate_build_status("proj")

    assert (coverage["all_present"], coverage["class_coverage"]) == (True, 1.0)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_modules_incomplete" in result["conflicts"]
    assert "denominator: the module scan on disk (1/26 modules built)" in result["reason"]


def test_a_full_scan_under_a_met_coverage_is_still_a_complete_success():
    """The cap is a shortfall cap. The same state with every module built stays
    green, so the test above is pinning the shortfall and not the scan rung."""
    result = _met_expectation_validator(
        scan=_maven_scan(built=tuple(MAVEN_MODULES))
    ).validate_build_status("proj")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert "26/26 modules built" in result["reason"]


def test_the_decider_and_the_display_read_one_scan_not_two():
    """Two walks of the same tree is how the two halves drifted apart, and it
    matters in exactly the p7d situation: an in-flight compile writing classes
    between the walks makes the deciding half and the commentary half describe
    different trees. So `PhysicalValidator.module_scan` — the production
    method, caching included — must be what the gate consumes, and the walk
    must happen once per pass.

    Strip the cache lookup from `module_scan` and this fails: the tree is
    walked twice.
    """
    from sag.agent.phase_gates import _inspect_build

    scan = _polaris_scan()
    validator = _polaris_validator(scan=scan)
    walks: list[str] = []
    handed_out: list[object] = []
    walk = validator._module_scan_result
    decide = validator.module_scan  # the real, bound production method

    def counting_walk(project_name):
        walks.append(project_name)
        return walk(project_name)

    def watched_scan(project_name):
        result = decide(project_name)
        handed_out.append(result)
        return result

    validator._module_scan_result = counting_walk
    validator.module_scan = watched_scan

    observation = _inspect_build(validator, "polaris")

    assert "Module coverage: 1/26 built" in observation.reason
    assert walks == ["polaris"]  # ONE walk in the whole gate pass
    assert handed_out and all(result is scan for result in handed_out)
