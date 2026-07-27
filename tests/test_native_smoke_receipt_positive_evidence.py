# tests/test_native_smoke_receipt_positive_evidence.py
"""Plan 5 Stage A (P0-E): a capability receipt requires POSITIVE evidence.

Ground-truth review 2026-07-26 (§"Capability-receipt defect"): the live TVM
receipt was minted from a smoke where all three selected tests SKIPPED —

    {"stats": {"executed": 3, "failed": 0, "errors": 0, "skipped": 3}, ...}

— so a run that proved nothing unlocked the full collect on the next bare
call. All-skipped means "capability NOT proven": no receipt is written, and
the next bare test call stays bounded. A receipt is also bound to the target
SHA it was earned on, so a materially different checkout cannot ride an old
one.

Scripted-orchestrator style (house pattern, shared with
tests/test_python_tool.py and tests/test_native_smoke_capability_gate.py).
"""

import json

from test_native_smoke_capability_gate import (
    TVM_ROOT,
    junit_rules,
    receipt_rule,
    receipt_writes,
)
from test_python_tool import (
    TVM_NATIVE_TEST_MANIFEST,
    TVM_SMOKE_PATH,
    Orch,
    fail,
    ok,
    tvm_native_smoke_rules,
)

from sag.tools.internal.python_tool import PythonTool

HEAD_SHA = "1b0e2c9d4f6a8b3c5d7e9f0a1b2c3d4e5f607182"
OTHER_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"


def head_rule(sha=HEAD_SHA):
    """`git -C <root> rev-parse HEAD` for the native project root."""
    return (f"git -C {TVM_ROOT} rev-parse HEAD", ok(f"{sha}\n"))


def positive_receipt(*, passed=1, target_sha=HEAD_SHA):
    payload = {
        "project_root": TVM_ROOT,
        "candidate": TVM_SMOKE_PATH,
        "stats": {
            "executed": 3,
            "selected": 3,
            "passed": passed,
            "failed": 0,
            "errors": 0,
            "skipped": 3 - passed,
        },
        "attempt": 1,
    }
    if target_sha is not None:
        payload["target_sha"] = target_sha
    return payload


def written_receipt(orch):
    writes = receipt_writes(orch)
    assert len(writes) == 1
    return json.loads(writes[0].split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0])


def gate_commands(orch):
    """Everything the capability gate ran — i.e. before any pytest call.

    The gate decides BEFORE pytest is invoked, so this window is exactly its
    own container traffic. Scoping matters since Plan 6 Stage 0: the invocation
    receipt records the run's target sha AFTER the dispatch, so a bare
    "did any git command run" scan no longer isolates the gate's behaviour.
    """
    for index, command in enumerate(orch.commands):
        if "-m pytest" in command:
            return orch.commands[:index]
    return list(orch.commands)


# ---------------------------------------------------------------------------
# (a) all-skipped proves nothing
# ---------------------------------------------------------------------------


def test_all_skipped_bounded_smoke_mints_no_receipt():
    """THE live TVM case: 3 selected, 3 skipped, zero failures — a clean run
    that demonstrated no capability whatsoever."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=3),
            head_rule(),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.succeeded is True
    assert receipt_writes(orch) == []
    assert "smoke_receipt_written" not in result.metadata
    assert result.metadata["smoke_capability_unproven"] is True
    assert (
        "[test] bounded smoke: all 3 selected tests were skipped — capability NOT "
        "proven; no receipt written; the next bare test call remains bounded"
    ) in result.output


def test_a_second_bare_call_after_an_all_skipped_smoke_is_still_bounded():
    """No receipt was minted, so nothing changed for the next bare call: the
    surveyed smoke runs again and the full suite is never collected."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=3),
            head_rule(),
        ],
    )
    tool = PythonTool(orch)

    tool.execute("test", working_directory=TVM_ROOT)
    result = tool.execute("test", working_directory=TVM_ROOT)

    assert result.metadata["collection_scope"] == "filtered"
    assert result.metadata["native_bounded"] is True
    assert result.metadata["smoke_receipt_present"] is False
    assert all(TVM_SMOKE_PATH in c for c in orch.commands if "--collect-only" in c)


# ---------------------------------------------------------------------------
# (b) one real pass IS positive evidence
# ---------------------------------------------------------------------------


def test_one_passed_two_skipped_mints_a_receipt_with_positive_evidence():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=2),
            head_rule(),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.succeeded is True
    assert result.metadata["smoke_receipt_written"] is True
    assert "smoke_capability_unproven" not in result.metadata
    payload = written_receipt(orch)
    assert payload["stats"]["passed"] == 1
    assert payload["stats"]["skipped"] == 2
    assert payload["target_sha"] == HEAD_SHA


def test_receipt_omits_target_sha_when_rev_parse_fails():
    """Absent facts are absent keys — an unavailable SHA is never invented."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=2),
            (f"git -C {TVM_ROOT} rev-parse HEAD", fail("not a git repository", exit_code=128)),
        ],
    )

    PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert "target_sha" not in written_receipt(orch)


# ---------------------------------------------------------------------------
# (c)/(f) receipts on disk without positive evidence are inert
# ---------------------------------------------------------------------------


def test_receipt_with_zero_passed_does_not_unlock_the_full_suite():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(positive_receipt(passed=0)),
            head_rule(),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=3),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_receipt_present"] is False
    assert result.metadata["native_bounded"] is True
    assert result.metadata["collection_scope"] == "filtered"


def test_legacy_receipt_without_a_passed_stat_does_not_unlock_the_full_suite():
    """The receipt the live TVM session actually left on disk."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(
                {
                    "attempt": 1,
                    "candidate": TVM_SMOKE_PATH,
                    "project_root": TVM_ROOT,
                    "stats": {
                        "errors": 0,
                        "executed": 3,
                        "failed": 0,
                        "selected": 3,
                        "skipped": 3,
                    },
                }
            ),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=3),
            head_rule(),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_receipt_present"] is False
    assert result.metadata["collection_scope"] == "filtered"


# ---------------------------------------------------------------------------
# (d)/(e) a receipt is bound to the checkout it was earned on
# ---------------------------------------------------------------------------


def test_receipt_from_another_target_sha_does_not_unlock_the_full_suite():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(positive_receipt(target_sha=OTHER_SHA)),
            head_rule(),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3, skipped=2),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_receipt_present"] is False
    assert result.metadata["native_bounded"] is True
    assert result.metadata["collection_scope"] == "filtered"


def test_valid_positive_receipt_on_the_current_sha_still_unlocks_the_full_suite():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(positive_receipt()),
            head_rule(),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=357),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.succeeded is True
    assert result.metadata["smoke_receipt_present"] is True
    assert result.metadata["collection_scope"] == "full"
    assert result.metadata["collected"] == 357


def test_positive_receipt_without_target_sha_still_unlocks_and_skips_rev_parse():
    """A receipt minted where the SHA was unavailable carries no binding — it
    must not be rejected for a fact it never claimed, and no git command runs."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(positive_receipt(target_sha=None)),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=357),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_receipt_present"] is True
    assert result.metadata["collection_scope"] == "full"
    assert not any("rev-parse HEAD" in command for command in gate_commands(orch))


# ---------------------------------------------------------------------------
# byte-compat: untouched paths gain no keys
# ---------------------------------------------------------------------------


def test_non_native_run_gains_no_capability_keys():
    from test_python_tool import MANIFEST

    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("42 tests collected in 0.2s")),
            *junit_rules(tests=42, skipped=42),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    assert "smoke_capability_unproven" not in result.metadata
    assert "smoke_receipt_written" not in result.metadata
    assert not any("rev-parse HEAD" in command for command in gate_commands(orch))
