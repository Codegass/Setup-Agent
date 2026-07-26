# tests/test_native_smoke_capability_gate.py
"""Plan 4 Task 1: the bounded native smoke is CAPABILITY-gated, not
readiness-gated.

Audit 2026-07-26 (CORRECTION block of
docs/superpowers/reports/2026-07-26-sagv2-final-acceptance.md): an LLVM-less
TVM build passed the readiness probe (import + PEP 610 + any native lib), so
``native_unready`` went False and a bare ``build(action='test')`` collected the
FULL suite — the exact failure the guard at python_tool.py:1147 was written to
prevent. Readiness is now an INFORMATIONAL fact; the only thing that unlocks a
full collect is a capability RECEIPT: a bounded smoke that actually executed
tests on this project root.

Scripted-orchestrator style (house pattern, shared with tests/test_python_tool.py).
"""

import json

import pytest
from test_python_tool import (
    TVM_NATIVE_TEST_MANIFEST,
    TVM_SMOKE_PATH,
    Orch,
    fail,
    ok,
    tvm_native_smoke_rules,
)

from sag.tools.internal.python_tool import NATIVE_SMOKE_RECEIPT_JSON, PythonTool

TVM_ROOT = "/workspace/tvm"


def junit_rules(*, tests=3, failures=0, errors=0, skipped=0):
    """Script the two in-container JUnit passes: the attempt tagger and the
    bounded counts extractor (both are `python -c <script> <report>` lines —
    matched on a signature line of each script)."""
    return [
        ("SAG_ATTEMPT_TAGGED", ok("SAG_ATTEMPT_TAGGED")),
        (
            "def unavailable(reason)",
            ok(
                json.dumps(
                    {
                        "ok": True,
                        "tests": tests,
                        "failures": failures,
                        "errors": errors,
                        "skipped": skipped,
                    }
                )
            ),
        ),
    ]


def receipt_rule(payload):
    """`cat` of the capability receipt: a JSON payload, or a missing file."""
    return (
        f"cat {NATIVE_SMOKE_RECEIPT_JSON}",
        ok(json.dumps(payload)) if payload is not None else fail("No such file"),
    )


def receipt_writes(orch):
    return [
        command
        for command in orch.commands
        if NATIVE_SMOKE_RECEIPT_JSON in command and command.startswith("cat >")
    ]


# ---------------------------------------------------------------------------
# (a) the falsifying test: a READY native project still runs the bounded smoke
# ---------------------------------------------------------------------------


def test_ready_native_project_without_receipt_still_runs_bounded_smoke():
    """THE audit's falsifying case. Old behavior: readiness True -> bare test
    collects the full suite (357 collected). New behavior: no receipt -> the
    surveyed smoke, filtered scope, no unfiltered collect anywhere."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s", native_ready=True),
            *junit_rules(tests=3),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.succeeded is True
    assert result.metadata["collection_scope"] == "filtered"
    assert result.metadata["selection_mode"] == "survey_candidate"
    assert result.metadata["smoke_candidate"] == TVM_SMOKE_PATH
    assert result.metadata["collected_after_deselection"] == 3
    # Readiness survives as an informational fact — never as a gate.
    assert result.metadata["native_ready_probe"] is True
    assert result.metadata["smoke_receipt_present"] is False
    collects = [command for command in orch.commands if "--collect-only" in command]
    assert len(collects) == 1
    assert TVM_SMOKE_PATH in collects[0]
    runs = [
        command for command in orch.commands if "--junitxml" in command and "-m pytest" in command
    ]
    assert len(runs) == 1
    assert f"{TVM_SMOKE_PATH} --maxfail=1" in runs[0]


# ---------------------------------------------------------------------------
# (b) a clean bounded smoke writes the capability receipt
# ---------------------------------------------------------------------------


def test_clean_bounded_smoke_writes_the_capability_receipt():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.succeeded is True
    writes = receipt_writes(orch)
    assert len(writes) == 1
    payload = json.loads(writes[0].split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0])
    assert payload["project_root"] == TVM_ROOT
    assert payload["candidate"] == TVM_SMOKE_PATH
    assert payload["attempt"] == 1
    assert payload["stats"]["executed"] == 3
    assert payload["stats"]["failed"] == 0
    assert payload["stats"]["errors"] == 0
    assert result.metadata["smoke_receipt_written"] is True


@pytest.mark.parametrize(
    ("junit", "collection_output", "run_result"),
    [
        # tests failed -> the project has NOT demonstrated a clean smoke
        (dict(tests=3, failures=1), "3 tests collected in 0.2s", None),
        # test errors -> same
        (dict(tests=3, errors=1), "3 tests collected in 0.2s", None),
        # a collection failure executed nothing at all
        (
            dict(tests=3),
            "3 tests collected in 0.2s",
            fail("!!!!! Interrupted: 1 error during collection !!!!!", exit_code=2),
        ),
    ],
)
def test_unclean_bounded_smoke_never_writes_a_receipt(junit, collection_output, run_result):
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules(collection_output),
            *junit_rules(**junit),
            *([("--junitxml", run_result)] if run_result is not None else []),
        ],
    )

    PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert receipt_writes(orch) == []


def test_receipt_for_another_project_root_does_not_unlock_this_one():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(
                {
                    "project_root": "/workspace/other",
                    "candidate": TVM_SMOKE_PATH,
                    "stats": {"executed": 3, "passed": 3},
                    "attempt": 1,
                }
            ),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.metadata["smoke_receipt_present"] is False
    assert result.metadata["collection_scope"] == "filtered"


# ---------------------------------------------------------------------------
# (c) the receipt — and only the receipt — unlocks the full suite
# ---------------------------------------------------------------------------


def test_bare_test_with_receipt_runs_the_full_suite():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(
                {
                    "project_root": TVM_ROOT,
                    "candidate": TVM_SMOKE_PATH,
                    "stats": {"executed": 3, "passed": 3},
                    "attempt": 1,
                }
            ),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=357),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.succeeded is True
    assert result.metadata["collection_scope"] == "full"
    assert result.metadata["collected"] == 357
    assert result.metadata["smoke_receipt_present"] is True
    collects = [command for command in orch.commands if "--collect-only" in command]
    assert len(collects) == 1
    assert TVM_SMOKE_PATH not in collects[0]


# ---------------------------------------------------------------------------
# (d) explicit full-suite args without a receipt are refused, naming the smoke
# ---------------------------------------------------------------------------


def test_explicit_full_suite_args_without_receipt_are_refused_naming_the_smoke():
    """Readiness used to relax arg sanitation to the plain allowlist, so a
    ready-looking native project could ask for the whole tree. Without a
    receipt the refusal must name the exact bounded smoke to run first (§3.3:
    a concrete, machine-derived repair action)."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s", native_ready=True),
            ("test -e /workspace/tvm/tests", ok("EXISTS")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT, args="tests")

    assert result.succeeded is False
    assert result.error_code == "PYTEST_ARGS_REJECTED"
    assert result.metadata["smoke_receipt_present"] is False
    assert result.metadata["replacement_args"] == f"{TVM_SMOKE_PATH} --maxfail=1"
    named = f"{result.output}\n" + "\n".join(result.suggestions or [])
    assert TVM_SMOKE_PATH in named
    assert "receipt" in named.lower()
    assert not any("--collect-only" in command for command in orch.commands)
    assert not any("--junitxml" in command for command in orch.commands)


def test_explicit_args_with_receipt_use_the_plain_allowlist():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(
                {
                    "project_root": TVM_ROOT,
                    "candidate": TVM_SMOKE_PATH,
                    "stats": {"executed": 3, "passed": 3},
                    "attempt": 1,
                }
            ),
            *tvm_native_smoke_rules("3 tests collected in 0.2s", native_ready=True),
            *junit_rules(tests=12),
            ("test -e /workspace/tvm/tests", ok("EXISTS")),
            ("--collect-only", ok("12 tests collected in 0.4s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT, args="tests")

    assert result.succeeded is True
    assert result.metadata["collection_scope"] == "filtered"
    assert result.metadata["smoke_receipt_present"] is True
    runs = [
        command for command in orch.commands if "--junitxml" in command and "-m pytest" in command
    ]
    assert len(runs) == 1
    assert "-m pytest tests --junitxml=" in runs[0]


# ---------------------------------------------------------------------------
# (e) the model-visible Collection: line
# ---------------------------------------------------------------------------


def test_output_text_carries_the_collection_line_for_a_filtered_smoke():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=3),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    expected = (
        "Collection: filtered — unknown collected, 3 selected, 3 executed, 0 collection errors"
    )
    assert expected in result.output


def test_output_text_carries_the_collection_line_for_a_full_suite():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(
                {
                    "project_root": TVM_ROOT,
                    "candidate": TVM_SMOKE_PATH,
                    "stats": {"executed": 3, "passed": 3},
                    "attempt": 1,
                }
            ),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=350, failures=2, skipped=5),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert (
        "Collection: full — 357 collected, 357 selected, 350 executed, 0 collection errors"
    ) in result.output


def test_collection_failure_reports_zero_executed_and_unknown_collection_errors():
    """A collection failure executed NOTHING: the line must never launder the
    JUnit `tests` attribute (56 collection nodes in the live TVM artifact) into
    an executed count. The count itself is unknown until Task 2 lands."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            *junit_rules(tests=56, errors=28, skipped=28),
            (
                "--junitxml",
                fail(
                    "ERROR collecting tests/python/all-platform-minimal-test/test_runtime.py\n"
                    "!!!!! Interrupted: 28 errors during collection !!!!!",
                    exit_code=2,
                ),
            ),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.succeeded is False
    assert result.error_code == "PYTEST_COLLECTION_ERROR"
    assert (
        "Collection: filtered — unknown collected, 3 selected, 0 executed, "
        "unknown collection errors"
    ) in result.output
    assert result.metadata["executed"] == 0
    assert result.metadata["collection_errors"] is None


def test_collection_line_is_present_on_a_non_native_project_run():
    from test_python_tool import MANIFEST

    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("42 tests collected in 0.2s")),
            *junit_rules(tests=42),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    assert (
        "Collection: full — 42 collected, 42 selected, 42 executed, 0 collection errors"
    ) in result.output
    assert result.metadata["native_ready_probe"] is False
    assert result.metadata["smoke_receipt_present"] is False
    assert receipt_writes(orch) == []


def test_bounded_smoke_that_never_starts_still_reports_the_collection_line():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            receipt_rule(None),
            *tvm_native_smoke_rules("51 tests collected in 0.2s"),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory=TVM_ROOT)

    assert result.error_code == "NATIVE_SMOKE_TOO_BROAD"
    expected = (
        "Collection: filtered — unknown collected, 51 selected, 0 executed, 0 collection errors"
    )
    assert expected in result.output
