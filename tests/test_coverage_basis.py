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
from sag.agent.physical_validator import (
    INVOCATION_RECEIPTS_DIRNAME,
    PhysicalValidator,
    _AttemptedModules,
)

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


def _orchestrator(files, receipts, degrade):
    """A plain filesystem fake, or one that also serves receipts when the test
    wants the denominator read by production code."""
    if receipts is None:
        return FakeBuildOrchestrator(files=files)
    return ReceiptFilesystem(files=files, receipts=receipts, **(degrade or {}))


def _polaris_validator(
    *,
    class_count=1706,
    has_artifacts=True,
    scan=None,
    jar_on_disk=False,
    attempted=None,
    structure=None,
    receipts=None,
    degrade=None,
):
    """A gradle build whose expectations are jar-only: nothing class-based can
    be derived, which is exactly what polaris's unparsed settings produced.

    `jar_on_disk` is the Kotlin/Scala/Groovy case: the same jar-only expectation
    list, and the jar IS there — a derived expectation, met.
    """
    files = {"/workspace/polaris/settings.gradle.kts"}
    if jar_on_disk:
        files.add("/workspace/polaris/build/libs/polaris-1.0.jar")
    orch = _orchestrator(files, receipts, degrade)
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
    if receipts is None:
        validator._attempted_module_evidence = lambda: _AttemptedModules(
            tuple(attempted or ()), True, None
        )
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


def _maven_reactor_validator(
    *, attempted=None, structure=None, scan=None, receipts=None, degrade=None
):
    """A 26-module Maven project where only `m0` compiled (10 of 260 classes)."""
    files = {f"/workspace/proj/m0/target/classes/C{index}.class" for index in range(10)}
    files |= {"/workspace/proj/pom.xml", "/workspace/proj/m0/target/app.jar"}
    orch = _orchestrator(files, receipts, degrade)
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
    if receipts is None:
        validator._attempted_module_evidence = lambda: _AttemptedModules(
            tuple(attempted or ()), True, None
        )
    validator._receipt_structure = lambda: structure or {}
    return validator


def _met_expectation_validator(
    *, scan, attempted=None, structure=None, class_count=10, receipts=None, degrade=None
):
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
    orch = _orchestrator(files, receipts, degrade)
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
    if receipts is None:
        validator._attempted_module_evidence = lambda: _AttemptedModules(
            tuple(attempted or ()), True, None
        )
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


class ReceiptFilesystem(FakeBuildOrchestrator):
    """The container filesystem AND this run's invocation receipts.

    Every P4 test below reads the denominator through production code — the
    presence probe, the `cat` of the receipt directory, the terminality
    predicate, the scoping outcome — so a DEGRADED READ is a degraded container
    and not a patched method. `corrupt` truncates a receipt line the way a
    half-written file or a clipped `cat` does: it still starts with `{`, and it
    still does not parse.
    """

    def __init__(
        self, files=(), *, receipts=(), corrupt=(), cat_raises=False, present_raises=False
    ):
        super().__init__(files=files)
        self.receipt_bodies = []
        for index, receipt in enumerate(receipts):
            body = json.dumps(receipt)
            if index in corrupt:
                body = body[: max(len(body) // 2, 1)]
            self.receipt_bodies.append(body)
        self.cat_raises = cat_raises
        self.present_raises = present_raises

    def execute_command(self, command, **_kwargs):
        text = command.strip()
        if INVOCATION_RECEIPTS_DIRNAME in text:
            if text.startswith("test -d"):
                if self.present_raises:
                    raise RuntimeError("container flake on the receipt-directory probe")
                return {
                    "exit_code": 0,
                    "output": "EXISTS" if self.receipt_bodies else "",
                }
            if text.startswith("cat "):
                if self.cat_raises:
                    raise RuntimeError("container flake reading the receipts")
                return {"exit_code": 0, "output": "\n".join(self.receipt_bodies)}
        return super().execute_command(command)


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
    260 read as untried rather than unbuilt. One predicate, one answer.

    What the refusal costs is the LICENCE TO NARROW, and nothing else: the 40
    modules it printed are still 40 modules this run attempted, and they stay in
    the claimant list (spec §2 P4). Deleting them — round three's mechanism — is
    what let a scoped retry beside the crash narrow the denominator instead.
    """
    evidence = _receipt_validator([_killed_receipt()])._attempted_module_evidence()

    assert evidence.narrowing_licensed is False
    assert evidence.cap == "build_receipt_not_terminal"
    assert evidence.modules == tuple(f"m{index}" for index in range(40))


def test_a_dispatch_something_else_stopped_is_not_a_denominator_either():
    """A timeout monitor's kill states its reason and truncates the log the same
    way; the exit code (143) is an int, which is exactly what could not tell them
    apart."""
    killed = dict(_killed_receipt(), lifecycle_state=None, termination_reason="silent_timeout")
    killed.pop("lifecycle_state")

    evidence = _receipt_validator([killed])._attempted_module_evidence()

    assert evidence.narrowing_licensed is False
    assert evidence.cap == "build_receipt_not_terminal"
    assert evidence.modules == tuple(f"m{index}" for index in range(40))


def test_a_terminal_receipt_still_sets_the_denominator():
    """The regression fence for the guard above: a dispatch that recorded its own
    exit (and a synchronous one, which states no lifecycle at all) still narrows
    coverage to what it attempted.

    And a crashed receipt beside them widens the claimant list back out and caps,
    rather than being dropped: the union is what this run attempted, and the
    narrowing waits for evidence the harness will stand behind.
    """
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

    assert _receipt_validator([finished])._attempted_module_evidence() == _AttemptedModules(
        ("core",), True, None
    )
    assert _receipt_validator([synchronous])._attempted_module_evidence() == _AttemptedModules(
        ("jms",), True, None
    )
    beside_a_crash = _receipt_validator(
        [_killed_receipt(), finished, synchronous]
    )._attempted_module_evidence()
    assert beside_a_crash.narrowing_licensed is False
    assert beside_a_crash.cap == "build_receipt_not_terminal"
    assert set(beside_a_crash.modules) == {f"m{index}" for index in range(40)} | {"core", "jms"}


def test_an_unreadable_receipt_line_states_nothing_and_hides_what_it_stated():
    """P4's third verb. A half-written or clipped receipt line was skipped as if
    the run had never written it — so the widest statement in a container could
    vanish and the narrowest one decide, which is an improvement bought by a
    failed read. It is a refusal now, and refusals cap."""
    orchestrator = ReceiptOrchestrator([_killed_receipt()])
    orchestrator.receipts = []
    truncated = json.dumps(_killed_receipt())[:40]
    orchestrator.execute_command = lambda command, **_k: (
        {"success": True, "exit_code": 0, "output": "EXISTS"}
        if command.strip().startswith("test -d")
        else {"success": True, "exit_code": 0, "output": truncated}
    )
    validator = PhysicalValidator(docker_orchestrator=orchestrator, project_path="/workspace")

    evidence = validator._attempted_module_evidence()

    assert truncated.startswith("{")  # it IS a receipt line; it just does not parse
    assert evidence == _AttemptedModules((), False, "build_receipts_unreadable")


def test_a_probe_that_threw_is_not_the_fact_that_no_receipt_exists():
    """Both receipt probes swallowed every exception into "nothing stated", and
    "nothing stated" is the state that licenses the widest reading of every other
    module. One flaky `test -d` or `cat` was therefore enough to change the
    denominator, and only ever in the direction of a better verdict."""

    class Flaky:
        def __init__(self, fail_on):
            self.fail_on = fail_on

        def execute_command(self, command, **_kwargs):
            text = command.strip()
            if text.startswith(self.fail_on):
                raise RuntimeError("container flake")
            return {"success": True, "exit_code": 0, "output": "EXISTS"}

    for fail_on in ("test -d", "cat "):
        validator = PhysicalValidator(
            docker_orchestrator=Flaky(fail_on), project_path="/workspace"
        )

        assert validator._attempted_module_evidence() == _AttemptedModules(
            (), False, "build_receipts_unreadable"
        )

    # And a probe that ANSWERED, saying the directory is not there, is still the
    # honest "nothing stated": no narrowing, and nothing to cap.
    absent = PhysicalValidator(
        docker_orchestrator=ReceiptOrchestrator([], present=False), project_path="/workspace"
    )
    assert absent._attempted_module_evidence() == _AttemptedModules((), True, None)


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


def test_two_labels_for_one_module_are_counted_once():
    """The tally the model reads must be a count of MODULES, not of labels.

    The denominator is keyed on `module_key` — that is the whole reason
    `Apache Camel :: Core` and the directory `core` can match at all — while the
    count was `len(denominator_modules)`. A reactor summary naming a module the
    Gradle task list also named (or a receipt pair that spelled one module two
    ways) therefore told the model more modules were attempted than the
    denominator contains.
    """
    one_module = module_basis(
        None, denominator_modules=("Apache Camel :: Core", "/workspace/camel/core")
    )

    assert one_module.total == 1
    assert one_module.phrase() == (
        "denominator: the receipts' module outcomes (1 module(s) attempted)"
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
    (`mvn -pl m0` -> modules `['m0']`), while this pass's own dispatches stated
    nothing (`_attempted_module_evidence()` reports no modules — a single-module
    build, or a receipt-free phase).

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


# ---------------------------------------------------------------------------
# §2 P4 round four — removing evidence must never improve a verdict
# ---------------------------------------------------------------------------
# The diagnosis, not another case: the #17 narrowing shrinks the coverage
# denominator with the attempted-module set, so ANYTHING that removes a module
# from that set makes the build look more complete. Three rounds each removed
# something for a good reason and each improved a verdict:
#
#   r1  fall back to the persisted structure when the receipt probe returns
#       nothing -> a stale single-module structure narrowed 26 to 1
#   r2  key the authority on "a receipt stated modules" -> the minority-scan cap
#       was disarmed by a receipt's mere existence
#   r3  filter non-terminal receipts out of the attempted set -> a scoped retry
#       beside an OOM-killed reactor narrowed 26 to 1 and graded GREEN
#
# So the rule is structural (spec §2 P4, §3.6): a receipt the harness will not
# trust as a PROVER is still counted as a CLAIMANT. A crashed, non-terminal or
# unreadable dispatch CAPS the verdict — as §3.3 already does for an unsettled
# obligation — and never shrinks the denominator.
_VERDICT_RANK = {"blocked": 0, "partial": 1, "success": 2}


def _reactor_receipts(*, wide_lifecycle="finished"):
    """The two dispatches of the reviewer's reproduction.

    `inv-maven-1-0001` is the full-reactor dispatch that named all 26 modules;
    `wide_lifecycle="vanished"` is the OOM kill (`collect_detached_result`
    synthesized exit 1 and the log stops there), `"finished"` is the same reactor
    having written its own exit status. `inv-maven-1-0002` is the scoped
    `mvn -pl m0` retry that finished, exit 0, naming one module.
    """
    return [
        {
            "receipt_id": "inv-maven-1-0001",
            "exit_code": 1,
            "lifecycle_state": wide_lifecycle,
            "module_outcomes": [{"module": name, "status": "success"} for name in MAVEN_MODULES],
        },
        {
            "receipt_id": "inv-maven-1-0002",
            "exit_code": 0,
            "lifecycle_state": "finished",
            "module_outcomes": [{"module": "m0", "status": "success"}],
        },
    ]


def _build_logic_receipt():
    """polaris's shape: one synchronous dispatch naming a subproject that no
    derived expectation mentions."""
    return [
        {
            "receipt_id": "inv-gradle-1-0001",
            "exit_code": 0,
            "module_outcomes": [{"module": "build-logic", "status": "attempted"}],
        }
    ]


def test_a_crashed_dispatchs_modules_still_count_as_claimants():
    """The round-three blocker, reproduced independently on three branches.

    26-module Maven reactor, expectations for m0..m25, only m0 has classes on
    disk (10 of 260). Two receipts: the full-reactor dispatch that detached and
    was OOM-killed (`lifecycle_state: vanished`, synthesized `exit_code: 1`)
    whose reactor summary named all 26 modules, and a scoped `mvn -pl m0` retry
    that finished. Filtering the crashed receipt out of the attempted set left
    `('m0',)`, which narrowed the denominator to one module, dropped the other 25
    expectations as "untried", and graded the build GREEN — a regression from
    BOTH main and round two, and the exact sentence §3.5 requires to be
    unconstructible.

    The crashed dispatch cannot PROVE the structure (its log stops at the kill,
    so its module list is a prefix). The modules it named are still modules this
    run attempted: they are claimants, they stay in the denominator, and the
    receipt caps the verdict instead of narrowing it.
    """
    result = _maven_reactor_validator(
        receipts=_reactor_receipts(wide_lifecycle="vanished")
    ).validate_build_status("proj")

    assert result["evidence"]["modules_attempted"] == MAVEN_MODULES
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_modules_incomplete" in result["conflicts"]
    assert "build_receipt_not_terminal" in result["conflicts"]
    assert "Built 10 of 260 expected classes" in result["reason"]


def test_two_terminal_dispatches_still_narrow_to_the_union_of_what_they_attempted():
    """The regression fence for the claimant rule: when both dispatches ended on
    their own the narrowing is untouched (#17/d5dc330) — it just measures against
    the union of what they attempted, 26 modules, not the last one."""
    result = _maven_reactor_validator(receipts=_reactor_receipts()).validate_build_status("proj")

    assert result["evidence"]["modules_attempted"] == MAVEN_MODULES
    assert result["build_complete"] is False
    assert "build_receipt_not_terminal" not in result["conflicts"]
    assert "26 module(s) attempted" in result["reason"]


def test_an_unverifiable_scope_caps_the_verdict_even_with_no_module_scan():
    """§3.5's cap must be reachable in EVERY state where the narrowing did not
    take effect — including the state where the module scan is unavailable.

    The receipt names `build-logic`, which cannot be mapped onto the single
    `/build/libs` expectation, so the wide denominator stands and scoping records
    `build_coverage_scope_unverified`. With a scan on hand the minority count
    capped the verdict; with `module_coverage` returning None (a tree it could
    not walk, a validator without the hook) the authority fell back to the survey
    rung, `states_a_shortfall` was False by construction, and the cap was
    silently disarmed — a met one-jar expectation graded a complete success while
    the run had itself recorded that it could not check the denominator. The
    disagreement never reached `result["conflicts"]` either, so nothing
    downstream could cap on it.
    """
    result = _polaris_validator(
        jar_on_disk=True, scan=None, receipts=_build_logic_receipt()
    ).validate_build_status("polaris")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_coverage_scope_unverified" in result["conflicts"]
    assert "Not a complete build" in result["reason"]
    assert "the build named modules the expectation list does not contain" in result["reason"]


def test_the_capped_reason_names_the_check_that_actually_produced_the_clause():
    """Category 3, in one label.

    The capped rewrite subordinates whatever the deciding branch said and
    parenthesises it. On the branch that decided from build FINGERPRINTS no
    coverage check ran at all (`coverage_info` is None — no expectation of any
    kind could be derived), and labelling that sentence `coverage check:` tells
    the model a check produced a finding it never produced.
    """
    validator = _polaris_validator(scan=_polaris_scan())
    validator._get_expected_artifacts = lambda *_a, **_k: []

    result = validator.validate_build_status("polaris")

    assert result["build_complete"] is False
    assert "(build check: Build fingerprints found for gradle project)" in result["reason"]
    assert "coverage check:" not in result["reason"]


def test_no_degraded_read_of_the_same_receipts_can_improve_the_verdict():
    """The P4 fence: a monotonicity property, not a case.

    The evidence on disk is held FIXED and the harness's READ of it is degraded
    in every way the three rounds degraded it — P4 names the verbs exactly:
    discarding a receipt, failing to read one, swallowing an exception into
    `None`. Here a receipt line is truncated (present, unparseable), the `cat` of
    the receipt directory throws, and the directory probe itself throws. Each
    degradation must leave the verdict EQUAL or WORSE than the clean read of the
    same container.

    The sharp corner is the met-expectation shape: one expectation for `m0`,
    fully met at a 100% threshold, so nothing else in `validate_build_status`
    stands between it and a complete success. Clean, the wide reactor receipt
    names 26 modules the expectation list does not contain, the narrowing is
    refused and the verdict capped. Lose that receipt to an unparseable line and
    the scoped `-pl m0` receipt is the only statement left: it narrows cleanly,
    the cap disappears, and a 26-module reactor with one module built grades
    GREEN — better for having read less.

    NOT asserted here, deliberately: that a run which made FEWER DISPATCHES
    grades no better. That is a different claim and it is inconsistent with §3.5
    rung (a) — a run whose only dispatch was `mvn -pl m0` is entitled to be
    measured against m0 (#17), so a two-receipt container is capped where a
    genuine one-receipt container is green. Both cannot hold while the narrowing
    exists. The reader-side property is the one P4's own verbs state, and it is
    the one all three rounds broke.
    """
    states = {
        "met expectation, no scan": (
            "proj",
            lambda **degrade: _met_expectation_validator(
                scan=None, receipts=_reactor_receipts(), degrade=degrade
            ),
        ),
        "met expectation, minority scan": (
            "proj",
            lambda **degrade: _met_expectation_validator(
                scan=_maven_scan(), receipts=_reactor_receipts(), degrade=degrade
            ),
        ),
        "oom reactor": (
            "proj",
            lambda **degrade: _maven_reactor_validator(
                receipts=_reactor_receipts(wide_lifecycle="vanished"), degrade=degrade
            ),
        ),
        "polaris, jar met, minority scan": (
            "polaris",
            lambda **degrade: _polaris_validator(
                jar_on_disk=True,
                scan=_polaris_scan(),
                receipts=_build_logic_receipt(),
                degrade=degrade,
            ),
        ),
    }
    degradations = {
        "the first receipt is a half-written line": {"corrupt": (0,)},
        "the second receipt is a half-written line": {"corrupt": (1,)},
        "every receipt is a half-written line": {"corrupt": (0, 1)},
        "the receipt cat throws": {"cat_raises": True},
        "the directory probe throws": {"present_raises": True},
    }

    for state, (project, build) in states.items():
        clean = build().validate_build_status(project)
        clean_rank = _VERDICT_RANK[clean["evidence_status"]]
        for label, degrade in degradations.items():
            degraded = build(**degrade).validate_build_status(project)
            assert _VERDICT_RANK[degraded["evidence_status"]] <= clean_rank, (
                f"{state}: {label} improved the verdict from "
                f"{clean['evidence_status']} to {degraded['evidence_status']}"
            )
            assert degraded["build_complete"] <= clean["build_complete"], (
                f"{state}: {label} completed a build the clean read did not"
            )
