# tests/test_verify_native_test_policy_negative.py
"""Plan 5 Task A2: the acceptance verifier rejects VACUOUS capability receipts.

Ground-truth review 2026-07-26 ("Capability-receipt defect"): the live TVM
session minted a native smoke receipt off three SKIPPED tests, and the Plan 4
verifier graded that session 7/7 because it only ever read
``smoke_receipt_written`` as an oracle. All-skipped proves nothing, so the tvm
profile now asserts the negatives: no receipt on an all-skipped attempt, a
persisted receipt carries positive evidence, and receipt-minted bookkeeping
trusts the flag only when the junit counts back it.

Sessions here are synthesized (not recorded) so each assertion has exactly one
reason to fail.

Plan 6 Stage F2 item 3 arms three more assertions, all gated on EVIDENCE rather
than on a version field: a session that persisted invocation contracts is a
Plan 6 run, and only then must every receipt carry a `contract_id`
(`contracts.chain` graduates from silent-when-absent to required), must every
failure-outcome receipt have a typed assessment (`evidence.assessments_present`)
and must the survey's `document_map.json` exist and parse
(`survey.document_map`). Pre-Plan-6 recordings persist no contracts, so all
three state NOTHING there and the four locked Plan 5 profiles keep their exact
6/10/10/10 assertion sets — proven both by the synthesized negatives below and
by the recorded-session regressions at the bottom of this file.
"""

import glob
import importlib.util
import json
import os

import pytest

from sag.agent.control_events import action_envelope_sha256
from sag.agent.evidence_assessments import ReceiptAssessment
from sag.agent.invocation_contracts import build_contract

SMOKE_PATH = "tests/python/all-platform-minimal-test"
SMOKE_COMMAND = f"/workspace/tvm/.venv/bin/python -m pytest {SMOKE_PATH} --maxfail=1"
FULL_COMMAND = "/workspace/tvm/.venv/bin/python -m pytest"


def load_verifier_module():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "verify_native_test_policy.py",
    )
    spec = importlib.util.spec_from_file_location("verify_native_test_policy", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attempt_meta(
    *,
    index=1,
    scope="filtered",
    command=SMOKE_COMMAND,
    tests=3,
    failed=0,
    errors=0,
    skipped=0,
    receipt_written=None,
    skip_reasons=None,
):
    """One pytest attempt's ToolResult metadata, in the recorded field shape."""
    meta = {
        "attempt_id": index,
        "collection_errors": 0,
        "collection_scope": scope,
        "collected_after_deselection": tests,
        "command": command,
        "error_tests": errors,
        "executed": tests,
        "failed_tests": failed,
        "skipped_tests": skipped,
        "system": "python",
        "tests": tests,
    }
    if receipt_written is not None:
        meta["smoke_receipt_written"] = receipt_written
    if skip_reasons is not None:
        meta["smoke_skip_reasons"] = skip_reasons
    return meta


def write_session(tmp_path, attempts, *, receipt=None, verdict=None):
    """Materialize a session directory the verifier can walk."""
    session = tmp_path / "session"
    control = session / ".setup_agent"
    control.mkdir(parents=True, exist_ok=True)

    lines = []
    for index, meta in enumerate(attempts, 1):
        envelope_id = f"env-{index}"
        exact_params = {"action": "test"}
        lines.append(
            {
                "kind": "action_envelope",
                "payload": {
                    "envelope_id": envelope_id,
                    "tool_call_id": f"call-{index}",
                    "tool": "build",
                    "exact_params": exact_params,
                    "envelope_sha256": action_envelope_sha256(
                        tool_call_id=f"call-{index}",
                        tool="build",
                        exact_params=exact_params,
                    ),
                },
            }
        )
        lines.append(
            {
                "kind": "tool_result",
                "payload": {"envelope_id": envelope_id, "result": {"metadata": meta}},
            }
        )
    (control / "control_events.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    (control / "verdict.json").write_text(
        json.dumps(verdict if verdict is not None else {"test_stats": {}}), encoding="utf-8"
    )
    if receipt is not None:
        (control / "native_smoke_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return session


def run_tvm(session):
    module = load_verifier_module()
    verifier = module.Verifier(str(session))
    verifier.assert_tvm()
    return verifier


def named_failure(verifier, name):
    return [failure for failure in verifier.failures if failure.split(":")[0] == name]


# -- 1. no receipt on an all-skipped attempt --------------------------------


def test_all_skipped_attempt_that_minted_a_receipt_fails(tmp_path):
    session = write_session(
        tmp_path,
        [attempt_meta(tests=3, skipped=3, receipt_written=True)],
    )

    verifier = run_tvm(session)

    assert named_failure(verifier, "tvm.attempt1.no_receipt_on_all_skipped")


def test_all_skipped_attempt_without_a_receipt_passes(tmp_path):
    session = write_session(
        tmp_path,
        [attempt_meta(tests=3, skipped=3, skip_reasons=["need llvm"])],
    )

    verifier = run_tvm(session)

    assert "tvm.attempt1.no_receipt_on_all_skipped" in verifier.passes
    assert "tvm.attempt1.skip_reasons_projected" in verifier.passes
    assert verifier.failures == []


def test_all_skipped_attempt_without_projected_skip_reasons_fails(tmp_path):
    """Plan 5 Stage E anchor: silent all-skipped labels are a verifier FAIL."""
    session = write_session(tmp_path, [attempt_meta(tests=3, skipped=3)])

    verifier = run_tvm(session)

    assert any("skip_reasons_projected" in name for name in verifier.failures)


def test_partially_skipped_attempt_is_not_subject_to_the_negative(tmp_path):
    session = write_session(
        tmp_path,
        [attempt_meta(tests=3, skipped=2, receipt_written=True)],
    )

    verifier = run_tvm(session)

    assert not any(name.endswith("no_receipt_on_all_skipped") for name in verifier.passes)
    assert verifier.failures == []


def test_all_skipped_with_failures_is_not_subject_to_the_negative(tmp_path):
    session = write_session(
        tmp_path,
        [attempt_meta(tests=3, skipped=3, failed=1, receipt_written=True)],
    )

    verifier = run_tvm(session)

    assert not any(name.endswith("no_receipt_on_all_skipped") for name in verifier.passes)
    assert not named_failure(verifier, "tvm.attempt1.no_receipt_on_all_skipped")


# -- 2. session-level receipt positive evidence -----------------------------


def test_persisted_receipt_without_passed_stat_fails(tmp_path):
    """The live TVM receipt shape: executed 3, skipped 3, no `passed` key."""
    session = write_session(
        tmp_path,
        [attempt_meta(tests=3, skipped=3)],
        receipt={
            "attempt": 1,
            "candidate": SMOKE_PATH,
            "project_root": "/workspace/tvm",
            "stats": {"errors": 0, "executed": 3, "failed": 0, "selected": 3, "skipped": 3},
        },
    )

    verifier = run_tvm(session)

    assert named_failure(verifier, "tvm.receipt.positive_evidence")


def test_persisted_receipt_with_zero_passed_fails(tmp_path):
    session = write_session(
        tmp_path,
        [attempt_meta(tests=3, skipped=3)],
        receipt={"project_root": "/workspace/tvm", "stats": {"passed": 0}},
    )

    verifier = run_tvm(session)

    assert named_failure(verifier, "tvm.receipt.positive_evidence")


def test_persisted_receipt_with_positive_passed_passes(tmp_path):
    session = write_session(
        tmp_path,
        [attempt_meta(tests=3, skipped=2, receipt_written=True)],
        receipt={"project_root": "/workspace/tvm", "stats": {"passed": 1}},
    )

    verifier = run_tvm(session)

    assert "tvm.receipt.positive_evidence" in verifier.passes
    assert verifier.failures == []


def test_absent_receipt_file_asserts_nothing(tmp_path):
    session = write_session(tmp_path, [attempt_meta(tests=3, skipped=3)])

    verifier = run_tvm(session)

    assert not any(name == "tvm.receipt.positive_evidence" for name in verifier.passes)
    assert not named_failure(verifier, "tvm.receipt.positive_evidence")


# -- 3. receipt_minted tracking requires positive junit evidence ------------


def test_full_collect_after_a_vacuous_mint_still_requires_filtered_scope(tmp_path):
    """An all-skipped attempt that claims a receipt must not unlock attempt 2."""
    session = write_session(
        tmp_path,
        [
            attempt_meta(index=1, tests=3, skipped=3, receipt_written=True),
            attempt_meta(index=2, scope="full", command=FULL_COMMAND, tests=900),
        ],
    )

    verifier = run_tvm(session)

    assert named_failure(verifier, "tvm.attempt2.scope.filtered")


def test_full_collect_after_a_valid_mint_is_allowed(tmp_path):
    session = write_session(
        tmp_path,
        [
            attempt_meta(index=1, tests=3, skipped=2, receipt_written=True),
            attempt_meta(index=2, scope="full", command=FULL_COMMAND, tests=900),
        ],
    )

    verifier = run_tvm(session)

    assert not named_failure(verifier, "tvm.attempt2.scope.filtered")
    assert "tvm.attempt1.scope.filtered" in verifier.passes


def test_mint_flag_without_junit_counts_is_not_trusted(tmp_path):
    """No `tests` key at all — nothing backs the flag, so it cannot unlock."""
    meta = attempt_meta(index=1, receipt_written=True)
    for key in ("tests", "executed", "failed_tests", "error_tests", "skipped_tests"):
        meta.pop(key, None)
    session = write_session(
        tmp_path,
        [meta, attempt_meta(index=2, scope="full", command=FULL_COMMAND, tests=900)],
    )

    verifier = run_tvm(session)

    assert named_failure(verifier, "tvm.attempt2.scope.filtered")


# -- 4. Plan 6 arming: contracts, assessments and the document map ----------
#
# A Plan 6 session is one that FROZE contracts. The fixtures below build that
# shape out of the production writers (`build_contract`, `ReceiptAssessment`)
# so a fixture cannot encode a hash formula or a payload shape the runtime does
# not actually use.

ENVELOPE = "env-1"
PROJECT = "/workspace/proj"
NO_DOCUMENT_MAP = object()


def make_contract(*, envelope_id=ENVELOPE, argv="--fail-at-end verify", **overrides):
    """One frozen contract, hashed by the module that owns the formula."""
    contract = build_contract(
        envelope_id=envelope_id,
        tool="build",
        params={"action": "test"},
        effective_action="verify",
        expected_cwd=PROJECT,
        expected_argv=argv,
    )
    contract.update(overrides)
    return contract


def make_receipt(receipt_id, *, contract=None, outcome="completed", **overrides):
    """One finalized receipt in the recorded v2 field shape."""
    receipt = {
        "schema_version": 2,
        "receipt_id": receipt_id,
        "tool": "build",
        "requested_action": "test",
        "effective_action": "verify",
        "argv": "mvn --fail-at-end verify",
        "working_directory": PROJECT,
        "actual_cwd": PROJECT,
        "exit_code": 0 if outcome == "completed" else 1,
        "outcome": outcome,
        "report_delta": [],
    }
    if contract is not None:
        receipt["contract_id"] = contract["contract_id"]
        receipt["contract_hash"] = contract["contract_hash"]
        receipt["compliance"] = "exact"
    receipt.update(overrides)
    return receipt


def make_assessment(receipt_id, typed_code="expectation_unmet"):
    return ReceiptAssessment(receipt_id=receipt_id, typed_code=typed_code).payload()


def write_plan6_session(
    tmp_path,
    *,
    receipts=(),
    contracts=(),
    assessments=(),
    document_map=NO_DOCUMENT_MAP,
    envelopes=(ENVELOPE,),
):
    """Materialize a Plan 6 shaped session directory."""
    session = tmp_path / "session"
    control = session / ".setup_agent"
    control.mkdir(parents=True, exist_ok=True)

    lines = []
    for index, envelope_id in enumerate(envelopes, 1):
        exact_params = {"action": "test"}
        lines.append(
            {
                "kind": "action_envelope",
                "payload": {
                    "envelope_id": envelope_id,
                    "tool_call_id": f"call-{index}",
                    "tool": "build",
                    "exact_params": exact_params,
                    "envelope_sha256": action_envelope_sha256(
                        tool_call_id=f"call-{index}",
                        tool="build",
                        exact_params=exact_params,
                    ),
                },
            }
        )
    (control / "control_events.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    (control / "verdict.json").write_text(json.dumps({"test_stats": {}}), encoding="utf-8")

    for directory, payloads, key in (
        ("invocation_receipts", receipts, "receipt_id"),
        ("invocation_contracts", contracts, "contract_id"),
        ("evidence_assessments", assessments, "assessment_id"),
    ):
        if not payloads:
            continue
        target = control / directory
        target.mkdir(parents=True, exist_ok=True)
        for payload in payloads:
            (target / f"{payload[key]}.json").write_text(json.dumps(payload), encoding="utf-8")

    if document_map is not NO_DOCUMENT_MAP:
        (control / "document_map.json").write_text(json.dumps(document_map), encoding="utf-8")
    return session


DOCUMENT_MAP = {"document_map_fingerprint": "a" * 64, "entries": [], "partial_map": []}


def run_assertions(session, *names):
    module = load_verifier_module()
    verifier = module.Verifier(str(session))
    for name in names:
        getattr(verifier, name)()
    return verifier


def stated(verifier, name):
    """Did the verifier state anything at all about `name`?"""
    return name in verifier.passes or bool(named_failure(verifier, name))


# -- 4a. contracts.chain becomes REQUIRED once contracts exist --------------


def test_a_plan6_receipt_without_a_contract_id_fails_the_chain(tmp_path):
    """The session froze contracts, so an unbound receipt is a broken chain."""
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract), make_receipt("rcpt-2")],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_contract_chain")

    assert named_failure(verifier, "contracts.chain")
    assert "rcpt-2" in verifier.failures[0]


def test_a_plan6_session_whose_receipts_all_carry_contracts_passes(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract)],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_contract_chain")

    assert "contracts.chain" in verifier.passes
    assert verifier.failures == []


def test_a_contract_free_session_still_states_nothing_about_the_chain(tmp_path):
    """Every Plan 5 recording: no contracts dir, so the assertion is silent."""
    session = write_plan6_session(tmp_path, receipts=[make_receipt("rcpt-1")])

    verifier = run_assertions(session, "assert_contract_chain")

    assert not stated(verifier, "contracts.chain")


def test_a_plan6_receipt_naming_an_unpersisted_contract_fails(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract, contract_id="ic-000000000000")],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_contract_chain")

    assert named_failure(verifier, "contracts.chain")


def test_a_contract_edited_after_the_freeze_fails(tmp_path):
    """The seal is the recomputation, not the presence of a hash field."""
    contract = make_contract()
    receipt = make_receipt("rcpt-1", contract=contract)
    tampered = dict(contract, expected_cwd="/workspace/elsewhere")
    session = write_plan6_session(
        tmp_path, contracts=[tampered], receipts=[receipt], document_map=DOCUMENT_MAP
    )

    verifier = run_assertions(session, "assert_contract_chain")

    assert named_failure(verifier, "contracts.chain")


def test_a_contract_for_an_envelope_the_session_never_emitted_fails(tmp_path):
    contract = make_contract(envelope_id="env-never")
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract)],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_contract_chain")

    assert named_failure(verifier, "contracts.chain")


# -- 4b. evidence.assessments_present ---------------------------------------


def test_a_failed_receipt_without_an_assessment_fails(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract, outcome="failed")],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_evidence_assessments")

    assert named_failure(verifier, "evidence.assessments_present")
    assert "rcpt-1" in verifier.failures[0]


def test_a_failed_receipt_with_its_assessment_passes(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract, outcome="failed")],
        assessments=[make_assessment("rcpt-1")],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_evidence_assessments")

    assert "evidence.assessments_present" in verifier.passes
    assert verifier.failures == []


def test_an_assessment_of_a_different_receipt_does_not_count(tmp_path):
    """One assessment per failing receipt — not one assessment per session."""
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract, outcome="failed")],
        assessments=[make_assessment("rcpt-other")],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_evidence_assessments")

    assert named_failure(verifier, "evidence.assessments_present")


def test_a_deviated_receipt_is_a_failure_outcome_too(tmp_path):
    """Exit 0 that did not run the frozen vector is a semantic downgrade: the
    assessor owes it a `deviated_receipt` verdict."""
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract, compliance="deviated")],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_evidence_assessments")

    assert named_failure(verifier, "evidence.assessments_present")


def test_a_session_whose_receipts_all_succeeded_states_nothing(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract)],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_evidence_assessments")

    assert not stated(verifier, "evidence.assessments_present")


def test_a_contract_free_session_states_nothing_about_assessments(tmp_path):
    """Plan 5 had no assessor, so its failed receipts owe nothing."""
    session = write_plan6_session(tmp_path, receipts=[make_receipt("rcpt-1", outcome="failed")])

    verifier = run_assertions(session, "assert_evidence_assessments")

    assert not stated(verifier, "evidence.assessments_present")


def test_a_receiptless_session_states_nothing_about_assessments(tmp_path):
    session = write_plan6_session(tmp_path, contracts=[make_contract()], document_map=DOCUMENT_MAP)

    verifier = run_assertions(session, "assert_evidence_assessments")

    assert not stated(verifier, "evidence.assessments_present")


# -- 4c. survey.document_map ------------------------------------------------


def test_a_plan6_session_without_a_document_map_fails(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path, contracts=[contract], receipts=[make_receipt("rcpt-1", contract=contract)]
    )

    verifier = run_assertions(session, "assert_document_map")

    assert named_failure(verifier, "survey.document_map")


def test_a_document_map_without_a_fingerprint_fails(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract)],
        document_map={"entries": []},
    )

    verifier = run_assertions(session, "assert_document_map")

    assert named_failure(verifier, "survey.document_map")


def test_an_unparseable_document_map_fails(tmp_path):
    contract = make_contract()
    session = write_plan6_session(tmp_path, contracts=[contract], document_map=DOCUMENT_MAP)
    (session / ".setup_agent" / "document_map.json").write_text("{not json", encoding="utf-8")

    verifier = run_assertions(session, "assert_document_map")

    assert named_failure(verifier, "survey.document_map")


def test_a_plan6_session_with_a_fingerprinted_document_map_passes(tmp_path):
    contract = make_contract()
    session = write_plan6_session(
        tmp_path,
        contracts=[contract],
        receipts=[make_receipt("rcpt-1", contract=contract)],
        document_map=DOCUMENT_MAP,
    )

    verifier = run_assertions(session, "assert_document_map")

    assert "survey.document_map" in verifier.passes
    assert verifier.failures == []


def test_a_contract_free_session_states_nothing_about_the_document_map(tmp_path):
    session = write_plan6_session(tmp_path, receipts=[make_receipt("rcpt-1")])

    verifier = run_assertions(session, "assert_document_map")

    assert not stated(verifier, "survey.document_map")


# -- regression: the recorded live sessions grade as documented -------------

LIVE_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
)
LIVE_TVM = os.path.join(LIVE_LOGS, "session_20260726_153134_67903")

# The four locked Plan 5 profiles (plan §Stage F "STILL 6/10/10/10"). None of
# them froze a contract, so every Plan 6 assertion must stay silent on all four.
PLAN5_PROFILES = (
    ("cli", "session_20260726_192837_88194", 6),
    ("bigtop", "session_20260726_195220_99607", 10),
    ("tvm", "session_20260726_192841_88267", 10),
    ("tvm", "session_20260726_200021_3936", 10),
)

PLAN6_ASSERTIONS = ("contracts.chain", "evidence.assessments_present", "survey.document_map")


@pytest.mark.skipif(not os.path.isdir(LIVE_TVM), reason="recorded TVM session not present")
def test_live_tvm_session_now_fails_on_the_vacuous_receipt():
    module = load_verifier_module()
    verifier = module.Verifier(LIVE_TVM)
    verifier.assert_pairing_and_hashes()
    verifier.assert_tvm()

    assert named_failure(verifier, "tvm.receipt.positive_evidence")
    for name in (
        "pairing.exact",
        "envelope.hashes",
        "no.scheduler.events",
        "tvm.pytest.attempted",
        "tvm.attempt1.scope.filtered",
        "tvm.attempt1.command.smoke_path",
        "tvm.attempt1.selected.bounded",
    ):
        assert name in verifier.passes


@pytest.mark.parametrize("profile, session_name, expected_passes", PLAN5_PROFILES)
def test_plan5_recorded_profiles_keep_their_exact_assertion_sets(
    profile, session_name, expected_passes
):
    """The locked 6/10/10/10, re-graded by the Plan 6 verifier.

    These four recordings predate contracts, so arming the new assertions must
    not add a single pass or failure to any of them: the counts below are the
    Plan 5 numbers, unchanged, and the Plan 6 names appear nowhere.
    """
    session = os.path.join(LIVE_LOGS, session_name)
    if not os.path.isdir(session):
        pytest.skip(f"recorded session {session_name} not present")

    module = load_verifier_module()
    verifier = module.Verifier(session)
    verifier.assert_pairing_and_hashes()
    verifier.assert_receipts_immutable()
    verifier.assert_contract_chain()
    verifier.assert_evidence_assessments()
    verifier.assert_document_map()
    getattr(verifier, f"assert_{profile}")()

    assert verifier.failures == []
    assert len(verifier.passes) == expected_passes
    for name in PLAN6_ASSERTIONS:
        assert not stated(verifier, name), name


@pytest.mark.parametrize("_profile, session_name, _expected", PLAN5_PROFILES)
def test_plan5_recorded_profiles_persisted_no_contracts(_profile, session_name, _expected):
    """Why the silence above is correct, stated as evidence not assumption."""
    session = os.path.join(LIVE_LOGS, session_name)
    if not os.path.isdir(session):
        pytest.skip(f"recorded session {session_name} not present")

    contracts = glob.glob(os.path.join(session, ".setup_agent", "invocation_contracts", "*.json"))

    assert contracts == []
