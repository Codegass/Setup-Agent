"""Unit tests for the Category-3 anchor evaluator.

Every predicate is exercised against SYNTHETIC structured artifacts (never
summary text): sealed verdict.json fields, control-record tool invocations,
and the stamped manifest. Edge cases assert that a MISSING field yields an
anchor FAIL with a named reason and never a crash.
"""

from __future__ import annotations

import pytest

from scripts.panel_category3_evaluator import (
    AnchorResult,
    RunArtifacts,
    ToolInvocation,
    evaluate_probe,
    evaluate_bigtop,
    evaluate_httpcomponents,
    evaluate_pyyaml,
    evaluate_tvm,
    probe_arm_verdict,
)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def artifacts(**kw) -> RunArtifacts:
    base = dict(
        verdict="success",
        build_judgment="success",
        build_source="physical",
        build_green=True,
        compiled_classes=None,
        unique_executed=0,
        unique_failed=0,
        unique_errors=0,
        invocations=(),
        manifest_present=False,
        manifest_python_packages=None,
        manifest_stamped=False,
        project_root=None,
    )
    base.update(kw)
    return RunArtifacts(**base)


def inv(tool="build", action="test", workdir="/workspace/proj", success=True, metadata=None, **extra):
    """Build a ToolInvocation matching the REAL projected schema.

    Real runs surface build/test/pytest through ``tool='build'``,
    ``params={'action', 'working_directory'}``; the pytest command and the
    ``collected*`` counts live in ``result['metadata']`` (react_engine
    projection). ``metadata=`` seeds that map; any leftover kwargs land in
    ``params`` for the odd test that needs a raw param.
    """
    return ToolInvocation(
        tool=tool,
        action=action,
        working_directory=workdir,
        success=success,
        params=dict(extra),
        metadata=dict(metadata or {}),
    )


def pytest_inv(workdir="/workspace/tvm", success=False, command=None, collected_after_deselection=None, collected=None):
    """A build(action='test') pytest invocation as the python backend records it."""
    md: dict = {}
    if command is not None:
        md["command"] = command
    if collected_after_deselection is not None:
        md["collected_after_deselection"] = collected_after_deselection
    if collected is not None:
        md["collected"] = collected
    return inv(tool="build", action="test", workdir=workdir, success=success, metadata=md)


def names_failing(results):
    return {r.name for r in results if not r.passed}


# --------------------------------------------------------------------------
# bigtop
# --------------------------------------------------------------------------
def _bigtop_pass_kwargs():
    return dict(
        verdict="failed",
        compiled_classes=121,
        unique_executed=50,
        unique_failed=0,
        invocations=(
            inv(tool="build", action="install", workdir="/workspace/bigtop/data-generators", success=True),
        ),
    )


def test_bigtop_all_anchors_pass_on_honest_failed_baseline():
    results = evaluate_bigtop(artifacts(**_bigtop_pass_kwargs()))
    assert not names_failing(results), names_failing(results)
    assert all(isinstance(r, AnchorResult) for r in results)


def test_bigtop_verdict_unknown_fails():
    kw = _bigtop_pass_kwargs()
    kw["verdict"] = "unknown"
    fails = names_failing(evaluate_bigtop(artifacts(**kw)))
    assert "verdict_not_unknown" in fails


def test_bigtop_phantom_green_guard_catches_success_with_zero_classes():
    kw = _bigtop_pass_kwargs()
    kw["verdict"] = "success"
    kw["compiled_classes"] = 0
    fails = names_failing(evaluate_bigtop(artifacts(**kw)))
    assert "phantom_green_guard" in fails
    # zero classes also trips the compiled floor
    assert "compiled_classes_floor" in fails


def test_bigtop_compiled_classes_below_floor_fails():
    kw = _bigtop_pass_kwargs()
    kw["compiled_classes"] = 95
    fails = names_failing(evaluate_bigtop(artifacts(**kw)))
    assert "compiled_classes_floor" in fails


def test_bigtop_compiled_classes_missing_fails_with_named_reason():
    kw = _bigtop_pass_kwargs()
    kw["compiled_classes"] = None
    results = evaluate_bigtop(artifacts(**kw))
    floor = next(r for r in results if r.name == "compiled_classes_floor")
    assert not floor.passed
    assert "missing" in floor.reason.lower()


def test_bigtop_requires_a_successful_data_generators_build():
    kw = _bigtop_pass_kwargs()
    kw["invocations"] = (
        inv(tool="build", action="install", workdir="/workspace/bigtop/data-generators", success=False),
    )
    fails = names_failing(evaluate_bigtop(artifacts(**kw)))
    assert "data_generators_build_success" in fails


def test_bigtop_non_data_generators_success_does_not_satisfy_anchor():
    kw = _bigtop_pass_kwargs()
    kw["invocations"] = (inv(tool="build", action="install", workdir="/workspace/bigtop", success=True),)
    fails = names_failing(evaluate_bigtop(artifacts(**kw)))
    assert "data_generators_build_success" in fails


def test_bigtop_tests_below_floor_or_with_failures_fail():
    kw = _bigtop_pass_kwargs()
    kw["unique_executed"] = 49
    kw["unique_failed"] = 1
    fails = names_failing(evaluate_bigtop(artifacts(**kw)))
    assert "unique_executed_floor" in fails
    assert "unique_failed_zero" in fails


# --------------------------------------------------------------------------
# httpcomponents
# --------------------------------------------------------------------------
def _http_pass_kwargs():
    return dict(
        verdict="success",
        build_source="physical",
        unique_executed=1856,
        project_root="/workspace/httpcomponents-client",
        invocations=(
            inv(
                tool="build",
                action="test",
                workdir="/workspace/httpcomponents-client",
                success=True,
                metadata={"command": "/usr/bin/mvn --fail-at-end verify"},
            ),
        ),
    )


def _rejected_attempt(workdir="/workspace"):
    """A REJECTED build(action='test') attempt: empty command, success==False.

    Matches the two failing rerun campaigns' non-root events (operation_outcome
    'unknown', no recorded command) — NOT a test execution (round-review item 3).
    """
    return inv(tool="build", action="test", workdir=workdir, success=False, metadata={})


def _real_test(workdir, command="/usr/bin/mvn --fail-at-end verify", success=True):
    return inv(tool="build", action="test", workdir=workdir, success=success,
               metadata={"command": command})


def test_httpcomponents_all_anchors_pass():
    fails = names_failing(evaluate_httpcomponents(artifacts(**_http_pass_kwargs())))
    assert not fails, fails


def test_httpcomponents_mis_scoped_16_tests_fail_the_floor():
    kw = _http_pass_kwargs()
    kw["unique_executed"] = 16
    fails = names_failing(evaluate_httpcomponents(artifacts(**kw)))
    assert "unique_executed_floor" in fails


def test_httpcomponents_test_workdir_not_root_fails():
    kw = _http_pass_kwargs()
    kw["invocations"] = (
        inv(tool="build", action="test", workdir="/workspace/httpcomponents-client/httpclient5", success=True),
    )
    fails = names_failing(evaluate_httpcomponents(artifacts(**kw), project_root="/workspace/httpcomponents-client"))
    assert "test_phase_workdir_is_root" in fails


def test_httpcomponents_uses_control_record_root_not_the_invocations():
    # Round-review P2-4: a FULLY mis-scoped run — every test in the same
    # submodule — must NOT pass by deriving the root from those invocations.
    # The control-record project_root catches it.
    kw = _http_pass_kwargs()
    kw["project_root"] = "/workspace/httpcomponents-client"
    kw["invocations"] = (
        inv(tool="build", action="test", workdir="/workspace/httpcomponents-client/httpclient5", success=True),
        inv(tool="build", action="test", workdir="/workspace/httpcomponents-client/httpclient5", success=True),
    )
    fails = names_failing(evaluate_httpcomponents(artifacts(**kw)))
    assert "test_phase_workdir_is_root" in fails


def test_httpcomponents_any_mis_scoped_test_fails_not_just_last():
    # An early mis-scoped test must fail even if the LAST test is at root.
    kw = _http_pass_kwargs()
    kw["invocations"] = (
        inv(tool="build", action="test", workdir="/workspace/httpcomponents-client/httpclient5", success=True),
        inv(tool="build", action="test", workdir="/workspace/httpcomponents-client", success=True),
    )
    fails = names_failing(evaluate_httpcomponents(artifacts(**kw)))
    assert "test_phase_workdir_is_root" in fails


def test_httpcomponents_missing_project_root_fails_named():
    kw = _http_pass_kwargs()
    kw["project_root"] = None
    results = evaluate_httpcomponents(artifacts(**kw))
    anchor = next(r for r in results if r.name == "test_phase_workdir_is_root")
    assert not anchor.passed
    assert "project root" in anchor.reason.lower()


def test_httpcomponents_missing_test_invocation_fails_named():
    kw = _http_pass_kwargs()
    kw["invocations"] = (inv(tool="build", action="compile", workdir="/workspace/httpcomponents-client"),)
    results = evaluate_httpcomponents(artifacts(**kw))
    anchor = next(r for r in results if r.name == "test_phase_workdir_is_root")
    assert not anchor.passed
    assert "no execution-bearing" in anchor.reason.lower()


# --- item 3: workdir anchor measures EXECUTION-BEARING invocations ONLY ------
def test_httpcomponents_rejected_non_root_attempt_plus_real_root_passes():
    # The exact failing-rerun shape: N rejected non-root attempts (empty
    # command, success=False) followed by the REAL mvn verify at the reactor
    # root (2255/0). The rejected attempts are NOT test executions, so scoping
    # is clean and the workdir anchor PASSES.
    kw = _http_pass_kwargs()
    kw["unique_executed"] = 2255
    kw["invocations"] = (
        _rejected_attempt("/workspace"),
        _rejected_attempt("/workspace"),
        _rejected_attempt("/workspace"),
        _real_test("/workspace/httpcomponents-client"),
    )
    results = evaluate_httpcomponents(artifacts(**kw))
    anchor = next(r for r in results if r.name == "test_phase_workdir_is_root")
    assert anchor.passed, anchor.reason
    assert not names_failing(results)


def test_httpcomponents_real_non_root_execution_fails():
    # A REAL (execution-bearing) test that actually ran in a submodule is the
    # mis-scope this anchor forbids — it must FAIL even beside a real root test.
    kw = _http_pass_kwargs()
    kw["invocations"] = (
        _real_test("/workspace/httpcomponents-client/httpclient5"),
        _real_test("/workspace/httpcomponents-client"),
    )
    fails = names_failing(evaluate_httpcomponents(artifacts(**kw)))
    assert "test_phase_workdir_is_root" in fails


def test_httpcomponents_zero_real_executions_fails():
    # Only rejected attempts, no real execution anywhere: cannot demonstrate
    # root scoping — the anchor FAILS (round-review item 3).
    kw = _http_pass_kwargs()
    kw["invocations"] = (
        _rejected_attempt("/workspace"),
        _rejected_attempt("/workspace/httpcomponents-client"),
    )
    results = evaluate_httpcomponents(artifacts(**kw))
    anchor = next(r for r in results if r.name == "test_phase_workdir_is_root")
    assert not anchor.passed
    assert "no execution-bearing" in anchor.reason.lower()


def test_httpcomponents_non_physical_source_fails():
    kw = _http_pass_kwargs()
    kw["build_source"] = "observations"
    fails = names_failing(evaluate_httpcomponents(artifacts(**kw)))
    assert "build_evidence_physical" in fails


def test_httpcomponents_partial_verdict_fails():
    kw = _http_pass_kwargs()
    kw["verdict"] = "partial"
    fails = names_failing(evaluate_httpcomponents(artifacts(**kw)))
    assert "verdict_success" in fails


# --------------------------------------------------------------------------
# tvm
# --------------------------------------------------------------------------
def _tvm_pass_kwargs():
    # A native-core-unbuilt run: honest failed/physical build, and a pytest
    # invocation carrying a -k filter with a small post-deselection collection.
    return dict(
        verdict="failed",
        build_judgment="failed",
        build_source="physical",
        build_green=False,
        invocations=(
            pytest_inv(
                workdir="/workspace/tvm",
                command="/workspace/tvm/.venv/bin/python -m pytest -k test_foo --junitxml=x.xml",
                collected_after_deselection=12,
            ),
        ),
    )


def test_tvm_all_anchors_pass_on_failed_physical_with_filter():
    fails = names_failing(evaluate_tvm(artifacts(**_tvm_pass_kwargs())))
    assert not fails, fails


def test_tvm_strictly_better_branch_passes():
    # Build green + full-suite pytest run (no filter, big collection): the
    # never-sweep safety anchor is conditioned on the core being UNBUILT, so a
    # strictly-better run must not fail it (round-review P2-1).
    kw = _tvm_pass_kwargs()
    kw["verdict"] = "partial"
    kw["build_judgment"] = "success"
    kw["build_green"] = True
    kw["invocations"] = (
        pytest_inv(
            workdir="/workspace/tvm",
            command="/workspace/tvm/.venv/bin/python -m pytest",  # no filter
            collected_after_deselection=5000,  # full suite
        ),
    )
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert not fails, fails


def test_tvm_honest_native_partial_physical_green_false_passes_build_anchor():
    # Reviewer-flagged missing regression: the honest native middle state —
    # judgment='partial', source='physical', green=False — is a PASS for the
    # build anchor (the native library is absent while pure-python evidence
    # exists), NOT the failed/physical branch and NOT strictly-better.
    kw = _tvm_pass_kwargs()
    kw["verdict"] = "partial"
    kw["build_judgment"] = "partial"
    kw["build_source"] = "physical"
    kw["build_green"] = False
    results = evaluate_tvm(artifacts(**kw))
    build = next(r for r in results if r.name == "build_failed_physical_or_better")
    assert build.passed, build.reason
    # It is NOT the strictly-better branch, so the never-sweep anchor still
    # applies (the pass_kwargs invocation carries a -k filter, so it passes).
    assert "never_sweep_while_unbuilt" not in names_failing(results)


def test_tvm_partial_physical_but_green_true_is_strictly_better_not_native_partial():
    # partial/physical WITH green is strictly-better, not the honest-native
    # middle state — it takes the strictly-better exemption.
    kw = _tvm_pass_kwargs()
    kw["verdict"] = "partial"
    kw["build_judgment"] = "partial"
    kw["build_source"] = "physical"
    kw["build_green"] = True
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm", command="pytest", collected_after_deselection=5000),
    )
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert not fails, fails


def test_tvm_partial_non_physical_fails_build_anchor():
    # A partial verdict that is NOT physical and NOT green must fail the build
    # anchor — "partial" alone is never enough (guards the generic-partial hole).
    kw = _tvm_pass_kwargs()
    kw["verdict"] = "partial"
    kw["build_judgment"] = "partial"
    kw["build_source"] = "observations"
    kw["build_green"] = False
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "build_failed_physical_or_better" in fails


def test_tvm_success_judgment_without_green_and_not_failed_fails():
    kw = _tvm_pass_kwargs()
    kw["build_judgment"] = "success"  # not "failed"
    kw["build_green"] = False  # and not the better branch
    kw["verdict"] = "failed"
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "build_failed_physical_or_better" in fails


def test_tvm_pytest_without_filter_fails_never_sweep():
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm",
                   command="pytest --maxfail=1", collected_after_deselection=12),
    )
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "never_sweep_while_unbuilt" in fails


def test_tvm_maxfail_alone_does_not_select():
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm",
                   command="pytest --maxfail=1", collected_after_deselection=3),
    )
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "never_sweep_while_unbuilt" in fails


def test_tvm_deselect_does_not_select():
    # --deselect runs everything EXCEPT the named tests; it is NOT a selecting
    # filter (round-review P2-2).
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm",
                   command="pytest --deselect tests/python/test_x.py::test_y",
                   collected_after_deselection=40),
    )
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "never_sweep_while_unbuilt" in fails


def test_tvm_node_id_path_counts_as_selection():
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm",
                   command="pytest tests/python/test_x.py::test_y", collected_after_deselection=1),
    )
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "never_sweep_while_unbuilt" not in fails


def test_tvm_collected_after_deselection_over_50_fails_never_sweep():
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm",
                   command="pytest -k x", collected_after_deselection=51),
    )
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "never_sweep_while_unbuilt" in fails


def test_tvm_filtered_pytest_missing_collected_field_fails_named_not_crash():
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm",
                   command="pytest -k x", collected=8),  # collected present, but no *_after_deselection
    )
    results = evaluate_tvm(artifacts(**kw))
    anchor = next(r for r in results if r.name == "never_sweep_while_unbuilt")
    assert not anchor.passed
    assert "missing" in anchor.reason.lower()


def test_tvm_zero_pytest_invocations_passes_never_sweep():
    # Reviewer split (item 2): ZERO pytest invocations PASSES the never-sweep
    # SAFETY anchor — nothing was swept. The old evaluator wrongly FAILED this
    # ("no pytest invocation to check"), re-punishing an idle-but-safe run and
    # arm-independent 5/6 smoke-compliance noise. This is the TVM P-r2 case.
    kw = _tvm_pass_kwargs()
    kw["invocations"] = ()  # no pytest at all
    results = evaluate_tvm(artifacts(**kw))
    anchor = next(r for r in results if r.name == "never_sweep_while_unbuilt")
    assert anchor.passed, anchor.reason
    assert "never_sweep_while_unbuilt" not in names_failing(results)


def test_tvm_maven_only_test_is_not_a_sweep_and_passes():
    # A run whose only build(action='test') is a maven invocation (no pytest
    # command/collected) has NO execution-bearing PYTEST invocation, so nothing
    # was swept: the safety anchor PASSES (it guards pytest sweeps, not the
    # absence of a python smoke).
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        inv(tool="build", action="test", workdir="/workspace/tvm",
            metadata={"command": "mvn -q test"}),
    )
    results = evaluate_tvm(artifacts(**kw))
    anchor = next(r for r in results if r.name == "never_sweep_while_unbuilt")
    assert anchor.passed, anchor.reason


def test_tvm_verdict_unknown_fails():
    kw = _tvm_pass_kwargs()
    kw["verdict"] = "unknown"
    fails = names_failing(evaluate_tvm(artifacts(**kw)))
    assert "verdict_not_unknown" in fails


# --- smoke_liveness is a REPORTED METRIC, not a per-run anchor (item 2) ------
def test_tvm_smoke_liveness_counts_real_smoke():
    from scripts.panel_category3_evaluator import tvm_smoke_liveness

    kw = _tvm_pass_kwargs()  # one filtered execution-bearing pytest = a real smoke
    assert tvm_smoke_liveness(artifacts(**kw)) == 1


def test_tvm_smoke_liveness_zero_when_no_pytest():
    from scripts.panel_category3_evaluator import tvm_smoke_liveness

    kw = _tvm_pass_kwargs()
    kw["invocations"] = ()
    assert tvm_smoke_liveness(artifacts(**kw)) == 0
    # ... and this run STILL passes every hard anchor (liveness is not a gate).
    assert not names_failing(evaluate_tvm(artifacts(**kw)))


def test_tvm_smoke_liveness_zero_for_unfiltered_sweep():
    from scripts.panel_category3_evaluator import tvm_smoke_liveness

    # An unfiltered full-suite pytest is a sweep, not a smoke: liveness 0 even
    # though a pytest ran (and the hard anchor separately FAILS it).
    kw = _tvm_pass_kwargs()
    kw["invocations"] = (
        pytest_inv(workdir="/workspace/tvm", command="pytest", collected_after_deselection=5000),
    )
    assert tvm_smoke_liveness(artifacts(**kw)) == 0


# --------------------------------------------------------------------------
# pyyaml
# --------------------------------------------------------------------------
def _pyyaml_pass_kwargs():
    return dict(
        verdict="success",
        unique_executed=100,
        unique_failed=0,
        manifest_present=True,
        manifest_stamped=True,
        manifest_python_packages=["yaml"],
    )


def test_pyyaml_all_anchors_pass_with_calibrated_floor():
    fails = names_failing(evaluate_pyyaml(artifacts(**_pyyaml_pass_kwargs()), executed_floor=80))
    assert not fails, fails


def test_pyyaml_missing_manifest_fails_named():
    kw = _pyyaml_pass_kwargs()
    kw["manifest_present"] = False
    results = evaluate_pyyaml(artifacts(**kw), executed_floor=80)
    anchor = next(r for r in results if r.name == "stamped_manifest_exists")
    assert not anchor.passed
    assert "manifest" in anchor.reason.lower()


def test_pyyaml_stampless_manifest_fails_named():
    # Round-review P2-3: a manifest that exists but carries no survey stamp is
    # NOT a stamped manifest.
    kw = _pyyaml_pass_kwargs()
    kw["manifest_present"] = True
    kw["manifest_stamped"] = False
    results = evaluate_pyyaml(artifacts(**kw), executed_floor=80)
    anchor = next(r for r in results if r.name == "stamped_manifest_exists")
    assert not anchor.passed
    assert "stamp" in anchor.reason.lower()


def test_pyyaml_c_extension_sibling_package_passes():
    """Calibration evidence (pyyaml-cal-r1): _yaml (the C-extension package,
    lib/_yaml/__init__.py) is REAL and discovered beside yaml — the anchor
    means 'yaml discovered from the package_dir layout', not exact equality."""
    kw = _pyyaml_pass_kwargs()
    kw["manifest_python_packages"] = ["_yaml", "yaml"]
    fails = names_failing(evaluate_pyyaml(artifacts(**kw), executed_floor=80))
    assert "manifest_python_packages" not in fails


def test_pyyaml_honest_partial_with_zero_failures_is_green():
    """Calibration evidence (pyyaml-cal-r1): 1281/1281 passed while the honest
    ladder verdicts partial (C extension unbuilt) — success-only was
    unachievable-by-construction. Partial+failures stays red."""
    kw = _pyyaml_pass_kwargs()
    kw["verdict"] = "partial"
    kw["unique_failed"] = 0
    fails = names_failing(evaluate_pyyaml(artifacts(**kw), executed_floor=80))
    assert "verdict_green" not in fails
    kw["unique_failed"] = 3
    fails = names_failing(evaluate_pyyaml(artifacts(**kw), executed_floor=80))
    assert "verdict_green" in fails


def test_pyyaml_wrong_packages_fail():
    kw = _pyyaml_pass_kwargs()
    kw["manifest_python_packages"] = ["pyyaml"]
    fails = names_failing(evaluate_pyyaml(artifacts(**kw), executed_floor=80))
    assert "manifest_python_packages" in fails


def test_pyyaml_none_packages_fail_named_not_crash():
    kw = _pyyaml_pass_kwargs()
    kw["manifest_python_packages"] = None
    results = evaluate_pyyaml(artifacts(**kw), executed_floor=80)
    anchor = next(r for r in results if r.name == "manifest_python_packages")
    assert not anchor.passed
    assert "missing" in anchor.reason.lower()


def test_pyyaml_executed_below_calibrated_floor_fails():
    kw = _pyyaml_pass_kwargs()
    kw["unique_executed"] = 79
    fails = names_failing(evaluate_pyyaml(artifacts(**kw), executed_floor=80))
    assert "unique_executed_floor" in fails


def test_pyyaml_requires_a_calibrated_floor():
    # A None floor means calibration never ran — that is an evaluation error,
    # surfaced as an anchor fail, never a silent pass or a crash.
    results = evaluate_pyyaml(artifacts(**_pyyaml_pass_kwargs()), executed_floor=None)
    anchor = next(r for r in results if r.name == "unique_executed_floor")
    assert not anchor.passed
    assert "calibrat" in anchor.reason.lower()


# --------------------------------------------------------------------------
# three-outcome arm verdict
# --------------------------------------------------------------------------
def test_arm_verdict_p_pass_f_pass_votes_delete():
    assert probe_arm_verdict(p_pass=True, f_pass=True) == "delete"


def test_arm_verdict_p_pass_f_fail_needs_stage2():
    assert probe_arm_verdict(p_pass=True, f_pass=False) == "stage-2"


def test_arm_verdict_p_fail_is_invalid_regardless_of_f():
    assert probe_arm_verdict(p_pass=False, f_pass=True) == "invalid"
    assert probe_arm_verdict(p_pass=False, f_pass=False) == "invalid"


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
def test_evaluate_probe_dispatches_by_name():
    results = evaluate_probe("bigtop", artifacts(**_bigtop_pass_kwargs()))
    assert not names_failing(results)


def test_evaluate_probe_unknown_probe_raises():
    with pytest.raises(KeyError):
        evaluate_probe("nope", artifacts())


def test_evaluate_probe_pyyaml_needs_floor_kwarg():
    results = evaluate_probe("pyyaml", artifacts(**_pyyaml_pass_kwargs()), executed_floor=80)
    assert not names_failing(results)


# --------------------------------------------------------------------------
# loader (structured artifacts on disk)
# --------------------------------------------------------------------------
def _write_session(tmp_path, *, verdict, build_evidence, unique, events, manifest):
    import json as _json

    setup = tmp_path / ".setup_agent"
    setup.mkdir(parents=True)
    (setup / "verdict.json").write_text(
        _json.dumps(
            {
                "schema_version": 3,
                "verdict": verdict,
                "build_evidence": build_evidence,
                "test_stats": {"unique": unique},
            }
        ),
        encoding="utf-8",
    )
    lines = []
    for seq, ev in enumerate(events, 1):
        lines.append(_json.dumps({"sequence": seq, "kind": "tool_result", "payload": ev}))
    (setup / "control_events.jsonl").write_text("\n".join(lines), encoding="utf-8")
    if manifest is not None:
        (setup / "build_requirements.json").write_text(_json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_loader_reads_verdict_invocations_and_manifest(tmp_path):
    from scripts.panel_category3_evaluator import load_run_artifacts

    session = _write_session(
        tmp_path / "session_x",
        verdict="failed",
        build_evidence={"judgment": "failed", "source": "physical", "green": False, "compiled_classes": 121},
        unique={"executed": 50, "failed": 0, "errors": 0},
        events=[
            {
                "tool": "project",
                "params": {"action": "clone", "repo_url": "https://x/bigtop.git"},
                "result": {"operation_outcome": "success", "metadata": {"clone_path": "/workspace/bigtop"}},
            },
            {
                "tool": "build",
                "params": {"action": "test", "working_directory": "/workspace/bigtop/data-generators"},
                "result": {"operation_outcome": "success"},
            },
            {
                "tool": "build",
                "params": {"action": "compile", "working_directory": "/workspace/bigtop"},
                "result": {"operation_outcome": "failed"},
            },
        ],
        manifest={
            "survey": {"analyzer_version": 7, "project_path": "/workspace/bigtop", "config_fingerprint": "abc"},
            "python_packages": ["yaml"],
        },
    )
    art = load_run_artifacts(session)
    assert art.verdict == "failed"
    assert art.build_judgment == "failed"
    assert art.build_source == "physical"
    assert art.compiled_classes == 121
    assert art.unique_executed == 50
    assert art.manifest_present is True
    assert art.manifest_stamped is True
    assert art.manifest_python_packages == ["yaml"]
    assert art.project_root == "/workspace/bigtop"
    # project/clone is loaded as an invocation too, so 3 total
    assert len(art.invocations) == 3
    clone, gen, compile_ = art.invocations
    assert gen.success is True
    assert compile_.success is False


def test_loader_reads_pytest_command_and_collected_from_metadata(tmp_path):
    from scripts.panel_category3_evaluator import load_run_artifacts

    session = _write_session(
        tmp_path / "session_pytest",
        verdict="failed",
        build_evidence={"judgment": "failed", "source": "physical", "green": False},
        unique={"executed": 4, "failed": 4, "errors": 0},
        events=[
            {
                "tool": "project",
                "params": {"action": "analyze", "project_path": "/workspace/tvm"},
                "result": {"operation_outcome": "success", "metadata": {"project_path": "/workspace/tvm"}},
            },
            {
                "tool": "build",
                "params": {"action": "test", "working_directory": "/workspace/tvm"},
                "result": {
                    "operation_outcome": "failed",
                    "metadata": {
                        "command": "/workspace/tvm/.venv/bin/python -m pytest -k test_x",
                        "collected": 4,
                        "collected_after_deselection": 2,
                    },
                },
            },
        ],
        manifest=None,
    )
    art = load_run_artifacts(session)
    assert art.project_root == "/workspace/tvm"
    pytests = art.pytest_invocations()
    assert len(pytests) == 1
    assert pytests[0].has_node_or_k_filter() is True
    assert pytests[0].collected_after_deselection() == 2


def test_loader_stampless_manifest_is_not_stamped(tmp_path):
    from scripts.panel_category3_evaluator import load_run_artifacts

    session = _write_session(
        tmp_path / "session_stampless",
        verdict="success",
        build_evidence={"source": "physical"},
        unique={"executed": 100, "failed": 0},
        events=[],
        manifest={"python_packages": ["yaml"]},  # no survey stamp
    )
    art = load_run_artifacts(session)
    assert art.manifest_present is True
    assert art.manifest_stamped is False


def test_loader_missing_verdict_raises_named(tmp_path):
    from scripts.panel_category3_evaluator import EvaluationError, load_run_artifacts

    (tmp_path / "empty").mkdir()
    with pytest.raises(EvaluationError):
        load_run_artifacts(tmp_path / "empty")


def test_loader_absent_manifest_is_not_present(tmp_path):
    from scripts.panel_category3_evaluator import load_run_artifacts

    session = _write_session(
        tmp_path / "session_y",
        verdict="success",
        build_evidence={"source": "physical"},
        unique={"executed": 1856, "failed": 0},
        events=[],
        manifest=None,
    )
    art = load_run_artifacts(session)
    assert art.manifest_present is False
    assert art.manifest_python_packages is None
