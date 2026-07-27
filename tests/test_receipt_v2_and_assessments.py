# tests/test_receipt_v2_and_assessments.py
"""Plan 6 Stage 0 Task 0.1 — receipt schema v2 and append-only assessments.

Design §C4: a receipt states what a runner PHYSICALLY did and is finalized
once; what that run MEANS is a separate, append-only record. Plan 5's
`mark_semantic_failure` rewrote a finalized receipt in place — the gradle
NO-SOURCE downgrade re-read its own receipt and overwrote it — so the file's
bytes depended on how many classifiers had run since. This lane deletes that
path: the NO-SOURCE verdict becomes a `ReceiptAssessment` NEXT TO the receipt,
and the receipt file is written exactly once.

Schema v2 adds the binding facts the loop needs before it can bind anything
(target sha, survey/config pins, domain, actual cwd, compliance, toolchain
fingerprint, output content hash, bounded per-testcase outcomes per review
binding note (b)) while every v1 key keeps its exact name and shape — the
Plan 5 consumers read the same receipts unchanged.

Pre-dispatch refusals mint no runner receipt (§C4) but are not silence either:
they write a typed `ControlAssessment` at stage `precondition`.

Scripted-orchestrator style (house pattern, shared with
tests/test_invocation_receipts.py and tests/test_python_tool.py).
"""

import hashlib
import json
import shlex

from test_invocation_receipts import (
    HASH_A,
    HASH_B,
    SUREFIRE,
    FakeExecute,
    receipts_written,
)
from test_python_tool import (
    MANIFEST,
    TVM_NATIVE_TEST_MANIFEST,
    Orch,
    fail,
    ok,
    tvm_native_smoke_rules,
)

from sag.agent import invocation_receipts
from sag.agent.evidence_assessments import (
    ASSESSMENT_DIR,
    ASSESSMENT_HEREDOC,
    ASSESSMENT_SCHEMA_VERSION,
    ControlAssessment,
    ReceiptAssessment,
    assessment_id,
    write_assessment,
)
from sag.agent.invocation_receipts import (
    RECEIPT_DIR,
    RECEIPT_SCHEMA_VERSION,
    TESTCASE_OUTCOME_CAP,
    build_receipt,
    nearest_domain_root,
    output_content_hash,
    read_testcase_outcomes,
    record_invocation,
    survey_pins,
    target_sha,
    toolchain_fingerprint,
)
from sag.runtime.paths import BUILD_REQUIREMENTS_PATH

V1_KEYS = (
    "receipt_id",
    "tool",
    "requested_action",
    "effective_action",
    "argv",
    "working_directory",
    "exit_code",
    "outcome",
    "report_delta",
)

V1_ARGS = {
    "receipt_id": "inv-maven-1-0001",
    "tool": "maven",
    "requested_action": "verify",
    "effective_action": "verify jacoco:report",
    "argv": "mvn --fail-at-end verify jacoco:report",
    "working_directory": "/workspace/proj",
    "exit_code": 0,
    "before": {},
    "after": {SUREFIRE: HASH_A},
}

SHA = "9f1a2b3c4d5e6f708192a3b4c5d6e7f809111213"


def assessments_written(commands):
    """Every assessment body persisted through the recorded commands."""
    payloads = []
    for command in commands:
        if ASSESSMENT_DIR not in command or ASSESSMENT_HEREDOC not in command:
            continue
        _, _, rest = command.partition("\n")
        body, _, _ = rest.partition(f"\n{ASSESSMENT_HEREDOC}")
        payloads.append(json.loads(body))
    return payloads


class ContainerFS:
    """Execute double with a file layer, so atomic writes are observable.

    Only the two shapes the evidence writers use are modelled: a single-path
    `cat` read and the `mkdir -p … && cat > tmp <<HEREDOC && mv -f tmp final`
    write. Everything else answers like an empty success.
    """

    def __init__(self, files=None, writable=True):
        self.files = dict(files or {})
        self.writable = writable
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat ") and "\n" not in command:
            path = shlex.split(command)[-1]
            if path in self.files:
                return ok(self.files[path])
            return fail(f"cat: {path}: No such file or directory")
        if "mv -f " in command and "\n" in command:
            if not self.writable:
                return fail("Read-only file system")
            header, _, rest = command.partition("\n")
            heredoc = header.rsplit("<<'", 1)[1].split("'", 1)[0]
            body, _, _ = rest.partition(f"\n{heredoc}")
            final = header.rsplit("mv -f ", 1)[1].split()[1]
            self.files[final] = body
            return ok("")
        return ok("")

    def writes(self):
        return [command for command in self.commands if "mv -f " in command]


# ---------------------------------------------------------------------------
# receipt v2: shape
# ---------------------------------------------------------------------------


def test_receipt_v2_is_schema_version_2():
    assert RECEIPT_SCHEMA_VERSION == 2
    assert build_receipt(**V1_ARGS)["schema_version"] == 2


def test_receipt_v2_keeps_every_v1_key_byte_identical():
    """Cross-lane contract: v1 keys are never renamed or reshaped, so a v2
    receipt's v1 projection is byte-identical to what Plan 5 wrote."""
    receipt = build_receipt(
        **V1_ARGS,
        target_sha=SHA,
        survey_fingerprint="survey-1",
        config_fingerprint="config-1",
        domain_id="/workspace/proj",
        actual_cwd="/workspace/proj",
        toolchain_fingerprint={"executable": "/usr/bin/mvn", "version": "Apache Maven 3.9.6"},
        output_content_hash=HASH_B,
        testcase_outcomes={"nodes": [{"node_id": "A#b", "status": "passed"}]},
    )

    v1_projection = {key: receipt[key] for key in V1_KEYS}
    assert json.dumps(v1_projection, sort_keys=True) == json.dumps(
        {
            "receipt_id": "inv-maven-1-0001",
            "tool": "maven",
            "requested_action": "verify",
            "effective_action": "verify jacoco:report",
            "argv": "mvn --fail-at-end verify jacoco:report",
            "working_directory": "/workspace/proj",
            "exit_code": 0,
            "outcome": "completed",
            "report_delta": {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []},
        },
        sort_keys=True,
    )


def test_receipt_v2_carries_every_new_key_when_the_fact_is_known():
    receipt = build_receipt(
        **V1_ARGS,
        target_sha=SHA,
        survey_fingerprint="survey-1",
        config_fingerprint="config-1",
        domain_id="/workspace/proj/core",
        actual_cwd="/workspace/proj/core",
        toolchain_fingerprint={"executable": "/usr/bin/mvn", "version": "Apache Maven 3.9.6"},
        output_content_hash=HASH_B,
        testcase_outcomes={"nodes": [{"node_id": "A#b", "status": "passed"}]},
        contract_id="ic-000000000abc",
        contract_hash="e" * 64,
        compliance="exact",
    )

    assert receipt["target_sha"] == SHA
    assert receipt["survey_fingerprint"] == "survey-1"
    assert receipt["config_fingerprint"] == "config-1"
    assert receipt["domain_id"] == "/workspace/proj/core"
    assert receipt["actual_cwd"] == "/workspace/proj/core"
    assert receipt["contract_id"] == "ic-000000000abc"
    assert receipt["contract_hash"] == "e" * 64
    assert receipt["compliance"] == "exact"
    assert receipt["toolchain_fingerprint"] == {
        "executable": "/usr/bin/mvn",
        "version": "Apache Maven 3.9.6",
    }
    assert receipt["output_content_hash"] == HASH_B
    assert receipt["testcase_outcomes"] == {"nodes": [{"node_id": "A#b", "status": "passed"}]}


def test_receipt_v2_omits_every_fact_it_does_not_know():
    """Absent facts are absent keys — never null, never a guessed default."""
    receipt = build_receipt(**V1_ARGS)

    assert set(receipt) == {
        "schema_version",
        *V1_KEYS,
        # The one fact a dispatch always knows: where it ran.
        "actual_cwd",
    }


def test_receipt_v2_states_compliance_only_against_a_frozen_contract():
    """Plan 6 Stage B: `compliance` was the constant "exact" while no contract
    existed to compare a dispatch against. Now one does, so the claim is made
    only when the facade actually froze it — a receipt with no contract states
    no compliance rather than asserting a match with nothing."""
    assert "compliance" not in build_receipt(**V1_ARGS)
    assert "contract_id" not in build_receipt(**V1_ARGS)


def test_receipt_v2_actual_cwd_defaults_to_the_directory_the_dispatch_used():
    assert build_receipt(**V1_ARGS)["actual_cwd"] == "/workspace/proj"


# ---------------------------------------------------------------------------
# receipt v2: the individual probes
# ---------------------------------------------------------------------------


def test_target_sha_is_the_working_trees_own_head():
    execute = FakeExecute(rules=[("rev-parse HEAD", ok(f"{SHA}\n"))])

    assert target_sha(execute, "/workspace/proj") == SHA
    assert execute.commands == ["git -C /workspace/proj rev-parse HEAD"]


def test_target_sha_stays_absent_when_the_probe_answers_with_anything_else():
    """A container that answers every command with build-log text has stated
    no sha at all; recording that text as provenance would be a fabrication."""
    execute = FakeExecute(default=ok("[INFO] BUILD SUCCESS\n"))

    assert target_sha(execute, "/workspace/proj") is None


def test_target_sha_never_raises_when_the_container_is_gone():
    assert target_sha(FakeExecute(raises=True), "/workspace/proj") is None


def test_survey_pins_read_through_the_surveys_own_stamp():
    manifest = {"survey": {"config_fingerprint": "cfg-7", "survey_fingerprint": "srv-7"}}

    assert survey_pins(manifest) == {
        "config_fingerprint": "cfg-7",
        "survey_fingerprint": "srv-7",
    }


def test_survey_pins_omit_a_fingerprint_the_survey_never_recorded():
    """The survey stamp is the only producer; a pin it does not carry is an
    absent fact here (Stage A introduces the survey fingerprint)."""
    assert survey_pins({"survey": {"config_fingerprint": "cfg-7"}}) == {
        "config_fingerprint": "cfg-7"
    }
    assert survey_pins({}) == {}
    assert survey_pins({"survey": {"config_fingerprint": None}}) == {}


def test_domain_id_binds_to_the_nearest_containing_domain_root():
    manifest = {
        "build_domains": [
            {"root": "/workspace/proj", "system": "gradle"},
            {"root": "/workspace/proj/spark", "system": "gradle"},
        ]
    }

    assert nearest_domain_root(manifest, "/workspace/proj/spark/sub") == "/workspace/proj/spark"
    assert nearest_domain_root(manifest, "/workspace/proj/core") == "/workspace/proj"


def test_domain_id_is_absent_when_no_surveyed_domain_contains_the_run():
    manifest = {"build_domains": [{"root": "/workspace/proj/spark"}]}

    assert nearest_domain_root(manifest, "/workspace/other") is None
    assert nearest_domain_root({}, "/workspace/proj") is None


def test_domain_id_also_reads_the_nested_recommendation_shape():
    manifest = {"build_recommendation": {"build_domains": [{"root": "/workspace/proj"}]}}

    assert nearest_domain_root(manifest, "/workspace/proj") == "/workspace/proj"


def test_toolchain_fingerprint_pairs_the_resolved_path_with_one_version_line():
    execute = FakeExecute(
        rules=[
            (
                "command -v",
                ok("/usr/bin/mvn\nSAGTOOLCHAIN\nApache Maven 3.9.6 (bc0240f)\n"),
            )
        ]
    )

    fingerprint = toolchain_fingerprint(
        execute,
        executable="mvn",
        version_flag="-v",
        working_directory="/workspace/proj",
    )

    assert fingerprint == {"executable": "/usr/bin/mvn", "version": "Apache Maven 3.9.6 (bc0240f)"}
    assert len(execute.commands) == 1
    assert "mvn -v" in execute.commands[0]


def test_toolchain_fingerprint_keeps_the_version_slot_empty_when_the_path_is_unknown():
    """The marker keeps an absent path from sliding into the version slot."""
    execute = FakeExecute(rules=[("command -v", ok("SAGTOOLCHAIN\nPython 3.12.4\n"))])

    assert toolchain_fingerprint(
        execute, executable="python", version_flag="-V", working_directory=None
    ) == {"version": "Python 3.12.4"}


def test_toolchain_fingerprint_is_absent_when_the_runner_answers_nothing():
    assert toolchain_fingerprint(FakeExecute(), executable="mvn", version_flag="-v") is None


def test_output_content_hash_is_the_sha256_of_the_output_the_tool_already_has():
    output = "[INFO] BUILD SUCCESS\n"

    assert output_content_hash(output) == hashlib.sha256(output.encode("utf-8")).hexdigest()


def test_output_content_hash_is_absent_when_there_is_no_output_fact():
    assert output_content_hash(None) is None


# ---------------------------------------------------------------------------
# receipt v2: bounded per-testcase outcomes (review binding note (b))
# ---------------------------------------------------------------------------


def junit_tokens(*entries):
    """The container's `grep -oE` token stream for JUnit report XMLs."""
    tokens = []
    for classname, name, child in entries:
        tokens.append(f'<testcase classname="{classname}" name="{name}" time="0.1">')
        if child:
            tokens.append(child)
        tokens.append("</testcase>")
    return "\n".join(tokens) + "\n"


def test_testcase_outcomes_parse_this_invocations_own_report_delta():
    execute = FakeExecute(
        rules=[
            (
                "grep -oE",
                ok(
                    junit_tokens(
                        ("a.Suite", "test_pass", None),
                        ("a.Suite", "test_fail", '<failure message="boom" type="AssertionError">'),
                        ("a.Suite", "test_error", '<error message="kaput" type="IOError">'),
                        (
                            "a.Suite",
                            "test_skip",
                            '<skipped message="needs llvm" type="pytest.skip">',
                        ),
                    )
                ),
            )
        ]
    )
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    outcomes = read_testcase_outcomes(execute, delta)

    assert outcomes == {
        "nodes": [
            {"node_id": "a.Suite#test_error", "reason": "kaput", "status": "error"},
            {"node_id": "a.Suite#test_fail", "reason": "boom", "status": "failed"},
            {"node_id": "a.Suite#test_skip", "status": "skipped", "reason": "needs llvm"},
            {"node_id": "a.Suite#test_pass", "status": "passed"},
        ]
    }
    assert SUREFIRE in execute.commands[0]


def test_testcase_outcomes_read_only_the_reports_the_delta_names():
    execute = FakeExecute(rules=[("grep -oE", ok(junit_tokens(("a.S", "t", None))))])
    other = "/workspace/proj/target/surefire-reports/TEST-other.xml"
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    read_testcase_outcomes(execute, delta)

    assert other not in execute.commands[0]


def test_testcase_outcomes_record_a_self_closing_passed_node():
    execute = FakeExecute(
        rules=[("grep -oE", ok('<testcase classname="a.S" name="t" time="0.1"/>\n'))]
    )
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    assert read_testcase_outcomes(execute, delta) == {
        "nodes": [{"node_id": "a.S#t", "status": "passed"}]
    }


def test_testcase_outcomes_carry_the_failure_message_too():
    """Spec §5 S2 (revised from Stage 0's skips-only rule): the failure's own
    message is the distinct typed evidence the R2 chain starts from."""
    execute = FakeExecute(
        rules=[
            (
                "grep -oE",
                ok(
                    junit_tokens(
                        ("a.S", "f", '<failure message="boom" type="AssertionError">'),
                    )
                ),
            )
        ]
    )
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    (node,) = read_testcase_outcomes(execute, delta)["nodes"]
    assert node == {"node_id": "a.S#f", "status": "failed", "reason": "boom"}


def test_testcase_outcomes_are_capped_and_record_the_truncation():
    """Note (b): bounded list, failures first so a truncated list still
    carries the diagnostic signal."""
    entries = [("a.S", f"test_{index:03d}", None) for index in range(TESTCASE_OUTCOME_CAP + 10)]
    entries.append(("a.S", "test_boom", '<failure message="boom" type="X">'))
    execute = FakeExecute(rules=[("grep -oE", ok(junit_tokens(*entries)))])
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    outcomes = read_testcase_outcomes(execute, delta)

    assert outcomes["truncated"] is True
    assert len(outcomes["nodes"]) == TESTCASE_OUTCOME_CAP
    assert outcomes["nodes"][0] == {
        "node_id": "a.S#test_boom",
        "reason": "boom",
        "status": "failed",
    }


def test_testcase_outcomes_are_not_truncated_at_exactly_the_cap():
    entries = [("a.S", f"test_{index:03d}", None) for index in range(TESTCASE_OUTCOME_CAP)]
    execute = FakeExecute(rules=[("grep -oE", ok(junit_tokens(*entries)))])
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    outcomes = read_testcase_outcomes(execute, delta)

    assert len(outcomes["nodes"]) == TESTCASE_OUTCOME_CAP
    assert "truncated" not in outcomes


def test_testcase_outcomes_do_not_touch_the_container_without_a_report_delta():
    execute = FakeExecute()

    assert read_testcase_outcomes(execute, {"new": [], "changed": []}) is None
    assert execute.commands == []


def test_testcase_outcomes_stay_absent_when_no_node_could_be_read():
    """An unreadable report is UNKNOWN, not 'this invocation ran no tests'."""
    execute = FakeExecute(default=ok(""))
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    assert read_testcase_outcomes(execute, delta) is None


def test_testcase_outcomes_never_raise_when_the_container_is_gone():
    delta = {"new": [{"path": SUREFIRE, "sha256": HASH_A}], "changed": []}

    assert read_testcase_outcomes(FakeExecute(raises=True), delta) is None


# ---------------------------------------------------------------------------
# record_invocation: the probes land in the persisted receipt
# ---------------------------------------------------------------------------


V2_MANIFEST = {
    "survey": {"config_fingerprint": "cfg-7"},
    "build_domains": [
        {"root": "/workspace/proj"},
        {"root": "/workspace/proj/core"},
    ],
}


def v2_execute(**overrides):
    rules = [
        ("rev-parse HEAD", ok(f"{SHA}\n")),
        ("command -v", ok("/usr/bin/mvn\nSAGTOOLCHAIN\nApache Maven 3.9.6\n")),
        ("grep -oE", ok(junit_tokens(("a.S", "t", None)))),
    ]
    return FakeExecute(rules=rules, **overrides)


def test_record_invocation_persists_every_v2_fact_it_could_observe():
    execute = v2_execute()

    metadata = record_invocation(
        execute,
        tool="maven",
        attempt=1,
        requested_action="verify",
        effective_action="verify",
        argv="mvn --fail-at-end verify",
        working_directory="/workspace/proj/core",
        exit_code=0,
        before={},
        after={SUREFIRE: HASH_A},
        output="[INFO] BUILD SUCCESS\n",
        requirements=V2_MANIFEST,
        contract_id="ic-000000000abc",
        contract_hash="e" * 64,
        compliance="exact",
    )

    (receipt,) = receipts_written(execute.commands)
    assert metadata == {"receipt_id": receipt["receipt_id"]}
    assert receipt["schema_version"] == 2
    assert receipt["target_sha"] == SHA
    assert receipt["config_fingerprint"] == "cfg-7"
    assert "survey_fingerprint" not in receipt
    assert receipt["domain_id"] == "/workspace/proj/core"
    assert receipt["actual_cwd"] == "/workspace/proj/core"
    assert receipt["contract_id"] == "ic-000000000abc"
    assert receipt["contract_hash"] == "e" * 64
    assert receipt["compliance"] == "exact"
    assert receipt["toolchain_fingerprint"] == {
        "executable": "/usr/bin/mvn",
        "version": "Apache Maven 3.9.6",
    }
    assert receipt["output_content_hash"] == output_content_hash("[INFO] BUILD SUCCESS\n")
    assert receipt["testcase_outcomes"] == {"nodes": [{"node_id": "a.S#t", "status": "passed"}]}


def test_record_invocation_never_probes_the_survey_manifest_itself():
    """Exactly ONE layer reads the manifest per build (the pre-flight owner);
    a receipt reads the pins its caller already holds and never makes that a
    second probe — the duplicate-probe regression
    tests/test_build_tool_preflight_integration.py guards."""
    execute = v2_execute()

    record_invocation(
        execute,
        tool="maven",
        attempt=1,
        requested_action="verify",
        effective_action="verify",
        argv="mvn verify",
        working_directory="/workspace/proj/core",
        exit_code=0,
        before={},
        after={},
        requirements=V2_MANIFEST,
    )

    assert not any(BUILD_REQUIREMENTS_PATH in command for command in execute.commands)


def test_record_invocation_omits_the_pins_when_the_caller_holds_no_manifest():
    """The facade path reads the manifest one layer up, so the receipt states
    the pins it can see and stays silent about the ones it cannot."""
    execute = v2_execute()

    record_invocation(
        execute,
        tool="maven",
        attempt=1,
        requested_action="verify",
        effective_action="verify",
        argv="mvn verify",
        working_directory="/workspace/proj/core",
        exit_code=0,
        before={},
        after={},
    )

    (receipt,) = receipts_written(execute.commands)
    assert "config_fingerprint" not in receipt
    assert "domain_id" not in receipt
    assert receipt["target_sha"] == SHA


def test_record_invocation_fingerprints_the_runner_named_by_the_argv():
    execute = v2_execute()

    record_invocation(
        execute,
        tool="python",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="/workspace/proj/.venv/bin/python -m pytest --junitxml=/tmp/x.xml",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
    )

    probe = next(command for command in execute.commands if "command -v" in command)
    assert "command -v /workspace/proj/.venv/bin/python" in probe
    assert "/workspace/proj/.venv/bin/python -V" in probe


def test_record_invocation_still_writes_a_receipt_when_every_probe_is_silent():
    execute = FakeExecute()

    metadata = record_invocation(
        execute,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradle test",
        working_directory="/workspace/proj",
        exit_code=1,
        before={},
        after={},
    )

    (receipt,) = receipts_written(execute.commands)
    assert metadata == {"receipt_id": receipt["receipt_id"]}
    assert receipt["outcome"] == "failed"
    assert set(receipt) == {
        "schema_version",
        *V1_KEYS,
        "actual_cwd",
    }


def test_mark_semantic_failure_is_gone():
    """Receipts are finalized once; the rewrite path is deleted, not deprecated."""
    assert not hasattr(invocation_receipts, "mark_semantic_failure")


# ---------------------------------------------------------------------------
# evidence assessments: identity, atomic append, no overwrite
# ---------------------------------------------------------------------------


def test_assessment_id_derives_from_the_subject_and_the_typed_code_only():
    first = ReceiptAssessment(
        receipt_id="inv-gradle-1-0001",
        typed_code="compile_no_source_mismatch",
        detail="every compile task reported NO-SOURCE",
    )
    same = ReceiptAssessment(
        receipt_id="inv-gradle-1-0001",
        typed_code="compile_no_source_mismatch",
        detail="a differently worded detail",
    )
    other_code = ReceiptAssessment(
        receipt_id="inv-gradle-1-0001",
        typed_code="tests_all_skipped",
    )
    other_subject = ReceiptAssessment(
        receipt_id="inv-gradle-1-0002",
        typed_code="compile_no_source_mismatch",
    )

    assert first.assessment_id == same.assessment_id
    assert first.assessment_id == assessment_id("inv-gradle-1-0001", "compile_no_source_mismatch")
    assert first.assessment_id != other_code.assessment_id
    assert first.assessment_id != other_subject.assessment_id


def test_receipt_assessment_payload_states_the_typed_verdict():
    assessment = ReceiptAssessment(
        receipt_id="inv-gradle-1-0001",
        typed_code="compile_no_source_mismatch",
        detail="every executed compile task reported NO-SOURCE",
        fingerprints={"target_sha": SHA},
        created_event="evt-42",
    )

    assert assessment.payload() == {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "assessment_id": assessment.assessment_id,
        "receipt_id": "inv-gradle-1-0001",
        "typed_code": "compile_no_source_mismatch",
        "detail": "every executed compile task reported NO-SOURCE",
        "fingerprints": {"target_sha": SHA},
        "created_event": "evt-42",
    }


def test_receipt_assessment_payload_omits_the_facts_it_was_not_given():
    assessment = ReceiptAssessment(receipt_id="inv-gradle-1-0001", typed_code="x")

    assert assessment.payload() == {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "assessment_id": assessment.assessment_id,
        "receipt_id": "inv-gradle-1-0001",
        "typed_code": "x",
    }


def test_control_assessment_payload_names_the_stage_that_refused():
    assessment = ControlAssessment(
        event_or_intent_id="ctl-python_test-0001",
        stage="precondition",
        typed_code="PYTEST_ARGS_REJECTED",
        detail="'make' is not an existing test path",
    )

    assert assessment.payload() == {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "assessment_id": assessment.assessment_id,
        "event_or_intent_id": "ctl-python_test-0001",
        "stage": "precondition",
        "typed_code": "PYTEST_ARGS_REJECTED",
        "detail": "'make' is not an existing test path",
    }


def test_write_assessment_persists_atomically_under_the_assessment_dir():
    execute = ContainerFS()
    assessment = ReceiptAssessment(
        receipt_id="inv-gradle-1-0001",
        typed_code="compile_no_source_mismatch",
        detail="NO-SOURCE",
    )

    assert write_assessment(execute, assessment) is True

    final = f"{ASSESSMENT_DIR}/{assessment.assessment_id}.json"
    (write,) = execute.writes()
    assert f"mkdir -p {ASSESSMENT_DIR}" in write
    assert f"{final}.tmp" in write
    assert f"mv -f {final}.tmp {final}" in write
    assert json.loads(execute.files[final]) == assessment.payload()


def test_write_assessment_is_idempotent_for_the_same_body():
    execute = ContainerFS()
    assessment = ReceiptAssessment(receipt_id="inv-gradle-1-0001", typed_code="x", detail="d")

    assert write_assessment(execute, assessment) is True
    assert write_assessment(execute, assessment) is True

    assert len(execute.writes()) == 1
    assert assessments_written(execute.commands) == [assessment.payload()]


def test_write_assessment_never_overwrites_a_different_body_under_one_id():
    """Append-only: an id already on disk with different content is an error,
    and the persisted bytes do not move."""
    execute = ContainerFS()
    first = ReceiptAssessment(receipt_id="inv-gradle-1-0001", typed_code="x", detail="first")
    second = ReceiptAssessment(receipt_id="inv-gradle-1-0001", typed_code="x", detail="second")
    assert write_assessment(execute, first) is True
    final = f"{ASSESSMENT_DIR}/{first.assessment_id}.json"
    persisted = execute.files[final]

    assert write_assessment(execute, second) is False

    assert execute.files[final] == persisted
    assert len(execute.writes()) == 1


def test_write_assessment_reports_a_failed_persist_without_raising():
    execute = ContainerFS(writable=False)

    assert write_assessment(execute, ReceiptAssessment(receipt_id="r", typed_code="x")) is False


def test_write_assessment_never_raises_when_the_container_is_gone():
    assert (
        write_assessment(
            FakeExecute(raises=True), ReceiptAssessment(receipt_id="r", typed_code="x")
        )
        is False
    )


def test_write_assessment_refuses_an_assessment_without_a_subject_or_code():
    execute = ContainerFS()

    assert write_assessment(execute, ReceiptAssessment(receipt_id="", typed_code="x")) is False
    assert write_assessment(execute, ReceiptAssessment(receipt_id="r", typed_code="")) is False
    assert execute.commands == []


def test_control_assessment_stage_must_be_one_of_the_typed_stages():
    execute = ContainerFS()

    assert (
        write_assessment(
            execute,
            ControlAssessment(event_or_intent_id="e", stage="whenever", typed_code="X"),
        )
        is False
    )
    assert execute.commands == []


# ---------------------------------------------------------------------------
# gradle NO-SOURCE: an assessment appears, the receipt bytes do not move
# ---------------------------------------------------------------------------


def test_gradle_no_source_appends_an_assessment_and_writes_the_receipt_once():
    from test_semantic_action_conservation import SelfProbingGradleOrchestrator

    orchestrator = SelfProbingGradleOrchestrator(
        "> Task :compileJava NO-SOURCE\nBUILD SUCCESSFUL in 3s\n"
    )
    from sag.tools.internal.gradle_tool import GradleTool

    result = GradleTool(orchestrator).execute(
        tasks="compileJava",
        working_directory="/workspace/p",
        use_wrapper=False,
    )

    assert result.succeeded is False
    (receipt,) = receipts_written(orchestrator.commands)
    (assessment,) = assessments_written(orchestrator.commands)
    # The physical fact is unchanged: gradle exited 0 and the receipt says so.
    assert receipt["outcome"] == "completed"
    assert "semantic_failure" not in receipt
    # The verdict lives next to it, keyed to that receipt.
    assert assessment["receipt_id"] == receipt["receipt_id"]
    assert assessment["typed_code"] == "compile_no_source_mismatch"
    assert "NO-SOURCE" in assessment["detail"]
    # Written once, never re-read for a rewrite.
    receipt_writes = [
        command
        for command in orchestrator.commands
        if RECEIPT_DIR in command and "mv -f" in command
    ]
    assert len(receipt_writes) == 1
    assert not any(
        command.startswith("cat ") and RECEIPT_DIR in command for command in orchestrator.commands
    )


# ---------------------------------------------------------------------------
# python facade: pre-dispatch refusals write a ControlAssessment
# ---------------------------------------------------------------------------


def test_rejected_pytest_args_write_a_precondition_control_assessment():
    from sag.tools.internal.python_tool import PythonTool

    orch = Orch(manifest=dict(MANIFEST))

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj", args="make test")

    assert result.error_code == "PYTEST_ARGS_REJECTED"
    (assessment,) = assessments_written(orch.commands)
    assert assessment["stage"] == "precondition"
    assert assessment["typed_code"] == "PYTEST_ARGS_REJECTED"
    assert assessment["detail"]
    assert "receipt_id" not in assessment
    # A rejection dispatched nothing, so it mints no runner receipt (§C4).
    assert receipts_written(orch.commands) == []


def test_unavailable_native_smoke_writes_a_precondition_control_assessment():
    from sag.tools.internal.python_tool import PythonTool

    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=tvm_native_smoke_rules("3 tests collected in 0.2s", target_exists=False),
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/tvm")

    assert result.error_code == "NATIVE_SMOKE_UNAVAILABLE"
    (assessment,) = assessments_written(orch.commands)
    assert assessment["stage"] == "precondition"
    assert assessment["typed_code"] == "NATIVE_SMOKE_UNAVAILABLE"
    assert receipts_written(orch.commands) == []


def test_two_distinct_refusals_are_two_distinct_control_assessments():
    from sag.tools.internal.python_tool import PythonTool

    orch = Orch(manifest=dict(MANIFEST))
    tool = PythonTool(orch)

    tool.execute("test", working_directory="/workspace/proj", args="make test")
    tool.execute("test", working_directory="/workspace/proj", args="make test")

    ids = [assessment["assessment_id"] for assessment in assessments_written(orch.commands)]
    assert len(ids) == 2
    assert len(set(ids)) == 2
