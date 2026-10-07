"""Real archived GJF reference plus evidence-removal and scope ablations."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan
from sag.benchmark.ci_sources import archive_sources, frozen_test_scope, module_aliases, read_source
from sag.benchmark.ci_verification import (
    _command_slices,
    _pool_counts,
    compare_verified,
    verify_reference,
)
from sag.benchmark.requirements import evaluation_identity, normalize_task
from test_gjf_reviewed_metadata import archived_gjf, save


@pytest.fixture
def reviewed(archived_gjf):
    base, task, draft = archived_gjf
    return base, normalize_task(task), apply_reviewed_plan(draft, task, base / "review.json", base)


def summary(spec, counts):
    return {
        "status": "complete",
        "preconditions": [{"id": "runtime", "status": "passed"}],
        "requirements": [
            {
                "id": r["id"],
                "status": "passed",
                **({"test_counts": counts} if r["kind"] == "test" else {}),
            }
            for r in spec["requirements"]
        ],
    }


def test_real_job_reconciles_module_paths_and_counts_without_case_identity(reviewed):
    base, task, spec = reviewed
    reference = verify_reference(task, spec, base)
    assert reference["counts"] == dict(
        reported=1625, skipped=0, assessed=1625, passed=1625, failed=0, errors=0
    )
    assert reference["pools"][0]["module_path"] == "core"
    assert reference["module_aliases"]["Google Java Format HEAD-SNAPSHOT"] == "core"
    assert len(reference["requirement_ids"]) == 21
    assert frozen_test_scope(task, spec, evaluation_identity(spec)) == {
        "core": {"core/target/surefire-reports"}
    }
    # Old task admission cannot silently turn green when a parser is added.
    assert spec["ci_alignment"]["comparison_admitted"] is False
    result = compare_verified(task, spec, summary(spec, reference["counts"]), source_base=base)
    assert result["reference"]["status"] == "verified"
    assert result["scope"]["status"] == "unavailable"
    assert result["case_identity"]["rate"] is None


def test_fresh_admission_keeps_scope_counts_and_identity_separate(reviewed):
    base, task, spec = reviewed
    spec["ci_alignment"][
        "comparison_admitted"
    ] = True  # Synthetic new protocol, never rewrite an old run.
    local = summary(spec, verify_reference(task, spec, base)["counts"])
    result = compare_verified(task, spec, local, source_base=base)
    assert result["scope"]["numerator"] == result["scope"]["denominator"] == 21
    assert result["scope"]["groups"]["build"]["denominator"] == 14
    assert result["test_counts"]["status"] == "equal_counts"
    assert result["case_identity"]["status"] == "unavailable"
    local["requirements"][0]["status"] = "failed"
    local["status"] = "incomplete"
    failed = compare_verified(task, spec, local, source_base=base)
    assert failed["scope"]["status"] == "not_met"
    assert failed["scope"]["numerator"] == 20
    local["requirements"][0]["status"] = "unavailable"
    missing = compare_verified(task, spec, local, source_base=base)
    assert missing["scope"]["rate"] is None


@pytest.mark.parametrize("selected_url", [None, "", 17, []])
def test_missing_ci_selection_does_not_destroy_local_results(reviewed, selected_url):
    from copy import deepcopy

    base, task, spec = reviewed
    local = summary(spec, verify_reference(task, spec, base)["counts"])
    before = deepcopy(local)
    spec["ci_alignment"].update(selected_url=selected_url, comparison_admitted=False)
    result = compare_verified(task, spec, local, source_base=base)
    assert result["reference"]["status"] == "unavailable"
    assert result["scope"]["numerator"] is None
    assert result["test_counts"]["rate"] is None
    assert "Selected official CI job URL is unavailable" in result["reference"]["reason"]
    assert local == before


@pytest.mark.parametrize(
    "mutation", ["commit", "job", "attempt", "pool", "goal", "order", "bytes", "length", "slice"]
)
def test_wrong_or_removed_ci_evidence_never_yields_a_score(reviewed, mutation):
    base, task, spec = reviewed
    local = summary(spec, verify_reference(task, spec, base)["counts"])
    spec["ci_alignment"]["comparison_admitted"] = True
    alignment = spec["ci_alignment"]
    idx_ref = alignment["archived_ci_index"]
    idx = json.loads(read_source(base, idx_ref))
    if mutation in {"commit", "job", "attempt"}:
        if mutation == "commit":
            idx["sha"] = "1" * 40
        if mutation == "job":
            idx["ci_identity"]["job_id"] += 1
        if mutation == "attempt":
            idx["ci_identity"]["run_attempt"] += 1
        path = base / idx_ref["path"]
        changed = save(path, idx)
        idx_ref.update(sha256=changed["sha256"], bytes=changed["bytes"])
    elif mutation == "bytes":
        (base / idx["sources"]["job_log"]["path"]).write_bytes(b"BUILD SUCCESS\n")
    elif mutation == "length":
        idx_ref["bytes"] -= 1
    elif mutation == "order":
        task["steps"].reverse()
    elif mutation == "slice":
        spec["reviewed_execution_plan"]["steps"][0]["ci_source_lineage"]["end_line_exclusive"] -= 1
    else:
        row = next(
            b
            for b in spec["reviewed_execution_plan"]["steps"][1]["ci_bindings"]
            if b["goal"] == "surefire:test"
        )
        if mutation == "pool":
            row["requirement_id"] = "nonexistent-pool"
        else:
            row["module"] = "eclipse_plugin"
    result = compare_verified(task, spec, local, source_base=base)
    assert result["scope"]["status"] == result["test_counts"]["status"] == "unavailable"
    assert result["scope"]["numerator"] is None


def test_source_relocation_preserves_raw_job_and_models(reviewed, tmp_path):
    base, task, spec = reviewed
    destination = tmp_path / "new-controller-archive"
    archive_sources(spec, base, destination)
    assert verify_reference(task, spec, destination) == verify_reference(task, spec, base)
    assert module_aliases(spec, destination) == module_aliases(spec, base)


def test_frozen_test_scope_does_not_warn_about_eclipse_module(reviewed):
    from sag.agent.physical_validator import PhysicalValidator

    _, task, spec = reviewed
    validator = object.__new__(PhysicalValidator)
    validator.docker_orchestrator = SimpleNamespace(
        acceptance_task=task,
        benchmark_requirements=spec,
        benchmark_evaluation_protocol=evaluation_identity(spec),
    )
    validator._execute_command_with_logging = Mock(return_value={"success": True, "output": ""})
    assert (
        validator._check_modules_without_tests(
            "/workspace/gjf", ["/workspace/gjf/core/target/surefire-reports"]
        )
        == []
    )
    validator._execute_command_with_logging.assert_not_called()
    assert validator._check_modules_without_tests("/workspace/gjf", []) == ["core"]
    assert "eclipse_plugin" not in str(validator._execute_command_with_logging.call_args_list)


def test_more_test_occurrences_are_not_claimed_as_better_without_identity(reviewed):
    base, task, spec = reviewed
    spec["ci_alignment"]["comparison_admitted"] = True
    counts = verify_reference(task, spec, base)["counts"]
    counts.update(reported=1626, assessed=1626, passed=1626)
    result = compare_verified(task, spec, summary(spec, counts), source_base=base)
    assert result["test_counts"]["status"] == "scope_conflict"
    assert result["case_identity"]["rate"] is None


@pytest.mark.parametrize(
    "prefix", ["", "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0 - in FooTest\n"]
)
def test_native_totals_do_not_double_count_classes(prefix):
    event = {
        "status": "passed",
        "segment": prefix
        + "[INFO] Results:\n[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n",
    }
    assert _pool_counts(event)["reported"] == 3
    with pytest.raises(ValueError):
        _pool_counts({**event, "segment": event["segment"] * 2})


@pytest.mark.parametrize("total,skipped", [(0, 0), (3, 3), (3, 4)])
def test_empty_skipped_or_contradictory_pool_is_not_a_denominator(total, skipped):
    with pytest.raises(ValueError):
        _pool_counts(
            {
                "status": "passed",
                "segment": f"[INFO] Results:\n[INFO] Tests run: {total}, Failures: 0, Errors: 0, Skipped: {skipped}\n",
            }
        )


def test_bound_command_must_be_complete_and_unambiguous(reviewed):
    base, task, spec = reviewed
    idx = json.loads(read_source(base, spec["ci_alignment"]["archived_ci_index"]))
    raw = read_source(base, idx["sources"]["job_log"])
    with pytest.raises(ValueError):
        _command_slices(task, raw + raw)
    with pytest.raises(ValueError):
        _command_slices(task, raw[: len(raw) // 2])


@pytest.mark.parametrize(
    "mismatch",
    ["ci_java", "ci_maven", "local_runtime", "local_worktree", "missing_pool", "missing_sources"],
)
def test_runtime_worktree_or_missing_sources_do_not_score(reviewed, mismatch):
    base, task, spec = reviewed
    spec["ci_alignment"]["comparison_admitted"] = True
    local = summary(spec, verify_reference(task, spec, base)["counts"])
    if mismatch == "ci_java":
        task["steps"][0]["java_major"] = 17
    elif mismatch == "ci_maven":
        task["steps"][0]["maven_version"] = "3.8.8"
    elif mismatch.startswith("local_"):
        local["preconditions"] = [{"id": mismatch, "status": "failed"}]
    elif mismatch == "missing_pool":
        next(r for r in local["requirements"] if "test_counts" in r).update(
            status="unavailable", test_counts=None
        )
    result = compare_verified(
        task, spec, local, source_base=None if mismatch == "missing_sources" else base
    )
    assert result["scope"]["status"] == "unavailable"
    assert result["test_counts"]["status"] == "unavailable"


def test_display_name_collision_and_wrong_module_path_are_not_guessed(reviewed):
    import xml.etree.ElementTree as ET

    base, _, spec = reviewed
    plans = spec["reviewed_execution_plan"]["steps"]
    ref = plans[0]["sources"]["effective_pom"]
    root = ET.fromstring(read_source(base, ref))
    models = list(root)
    for model in models[:2]:
        name = next(n for n in model if n.tag.rsplit("}", 1)[-1] == "name")
        name.text = "Same display name"
    path = base / ref["path"]
    path.write_bytes(ET.tostring(root))
    from sag.benchmark.requirements import file_digest

    for plan in plans:
        plan["sources"]["effective_pom"].update(sha256=file_digest(path), bytes=path.stat().st_size)
    with pytest.raises(ValueError, match="Ambiguous"):
        module_aliases(spec, base)


def test_requirements_run_withholds_the_old_parallel_ci_score():
    from test_ci_comparison import compare, setup_run

    run = setup_run()
    run.fs.benchmark_requirements = {"schema_version": 2}
    result = compare(run)
    assert result.status == "unavailable" and result.attainment is None
    assert result.reasons == ("REQUIREMENTS_V2_CI_COMPARISON_IN_BENCHMARK_ANALYSIS",)


def test_evaluator_consumes_bound_host_source_archive_and_rejects_mixed_manifest(
    reviewed, tmp_path, monkeypatch
):
    from sag.benchmark.evaluator import evaluate
    from sag.benchmark.recorder import reference, write_json
    from sag.benchmark.requirements import canonical_digest

    base, task, spec = reviewed
    session = tmp_path / "session"
    destination = session / "benchmark-inputs"
    archive_sources(spec, base, destination)
    manifest = {
        "schema_version": 1,
        "evaluation_identity": evaluation_identity(spec),
        "requirements_digest": canonical_digest(spec),
    }
    write_json(destination / "manifest.json", manifest)
    run = {
        "run_id": "controlled-missing-execution",
        "repo": task["repo"],
        "commit": task["sha"],
        "evaluation_identity": evaluation_identity(spec),
        "invocations": [],
        "worktree": [],
        "requirements_sources": reference(session, destination / "manifest.json"),
    }
    result = evaluate(task, spec, run, session)
    assert result["ci_verification"]["reference"]["status"] == "verified"
    assert result["ci_scope_attainment"] is None
    from sag.benchmark.__main__ import main
    import sys

    # A closed SAG sidecar carries sources elsewhere in the host session;
    # the CLI must not mistake the sidecar's own directory for the source root.
    records = session / "requirements-evidence" / "run"
    for name, value in (("task", task), ("requirements", spec), ("run", run)):
        write_json(records / (name + ".json"), value)
    output = records / "replay.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "score",
            "score",
            "--task",
            str(records / "task.json"),
            "--requirements",
            str(records / "requirements.json"),
            "--run",
            str(records / "run.json"),
            "--evidence-root",
            str(session),
            "--output",
            str(output),
        ],
    )
    main()
    assert json.loads(output.read_text())["ci_verification"]["reference"]["status"] == "verified"
    manifest["evaluation_identity"]["task_sha256"] = "0" * 64
    write_json(destination / "manifest.json", manifest)
    run["requirements_sources"] = reference(session, destination / "manifest.json")
    mixed = evaluate(task, spec, run, session)
    assert mixed["ci_verification"]["reference"]["status"] == "unavailable"
    assert mixed["ci_scope_attainment"] is None


def test_ci_runtime_is_not_inherited_across_changed_launcher_environment(reviewed):
    from sag.benchmark.ci_verification import _command_runtime

    base, task, spec = reviewed
    idx = json.loads(read_source(base, spec["ci_alignment"]["archived_ci_index"]))
    slices = _command_slices(task, read_source(base, idx["sources"]["job_log"]))
    first, second = task["steps"]
    text, start, end, _ = slices[first["id"]]
    previous = {
        **_command_runtime(first, text, start=start, previous=None),
        "end_line_exclusive": end,
    }
    text, start, _, _ = slices[second["id"]]
    altered = text.replace("env:\n", "env:\n  MAVEN_HOME: /another/maven\n", 1)
    with pytest.raises(ValueError, match="launcher"):
        _command_runtime(second, altered, start=start, previous=previous)
