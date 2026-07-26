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
"""

import importlib.util
import json
import os

import pytest

from sag.agent.control_events import action_envelope_sha256

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

    assert any(name.endswith("skip_reasons_projected") for name in verifier.failures)


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


# -- regression: the recorded live sessions grade as documented -------------

LIVE_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
)
LIVE_TVM = os.path.join(LIVE_LOGS, "session_20260726_153134_67903")


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
