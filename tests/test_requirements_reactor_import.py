"""Reactor definition imports and repeated-lifecycle evidence cannot drop work."""
from copy import deepcopy
import hashlib
import json

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan
from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import maven_events
from sag.benchmark.requirements import validate_requirements
from test_httpcomponents_reviewed_metadata import reviewed_archive
from test_benchmark_requirement_evaluator import fixture, banner, requirement, write


def import_review(archive):
    base, review, task, files = archive
    path = base / ("review-" + review["project_id"] + ".json")
    path.write_text(json.dumps(review))
    draft = json.loads((base / "requirements" / (review["project_id"] + ".json")).read_text())
    return apply_reviewed_plan(draft, task, path, base)


def test_multi_module_import_preserves_every_required_occurrence_and_test_pool(reviewed_archive):
    _, review, task, _ = reviewed_archive
    spec = import_review(reviewed_archive)
    assert spec["annotation_completeness"]["status"] == "complete"
    validate_requirements(spec, task)
    assert len(spec["requirements"]) == review["review_summary"]["logical_requirements"]
    assert sum(len(r["validation"]["native_bindings"]) for r in spec["requirements"]) == sum(
        b["disposition"] == "requirement" for b in review["ci_bindings"])
    for row in spec["requirements"]:
        if row["kind"] == "test":
            assert len(row["validation"]["native_bindings"]) == 1
            assert len(row["validation"]["reused_native_bindings"]) == 1
            assert row["validation"]["report_directories"]


@pytest.mark.parametrize("mutation", ["drop_second_goal", "cross_module_requirement", "invent_reuse",
                                    "support_check", "drop_package", "false_module_path"])
def test_review_cannot_drop_or_relabel_reactor_obligations(reviewed_archive, mutation):
    _, review, _, _ = reviewed_archive
    if mutation == "drop_second_goal":
        review["ci_bindings"].pop(next(i for i, b in enumerate(review["ci_bindings"]) if b["occurrence"] == 1))
    elif mutation == "cross_module_requirement":
        review["requirements"][0]["module"] = review["modules"][-1]["id"]
    elif mutation == "invent_reuse":
        next(b for b in review["ci_bindings"] if b["disposition"] == "same_configuration_reuse")["reuse_of"]["module"] = "another"
    elif mutation == "support_check":
        b = next(b for b in review["ci_bindings"] if b["goal"] == "apache-rat:check")
        b["disposition"] = "support"
        b.pop("requirement_id", None)
    elif mutation == "drop_package":
        next(r for r in review["requirements"] if r["kind"] == "package")["expectations"]["artifacts"].clear()
    else:
        review["modules"][-1]["path"] = "another"
    with pytest.raises(ValueError):
        import_review(reviewed_archive)


def test_preparation_summary_cannot_hide_changed_profile(reviewed_archive):
    _, review, _, files = reviewed_archive
    path = files["preparation_status"]
    status = json.loads(path.read_text())
    status["steps"][0]["commands"][0]["argv"].insert(1, "-DskipTests")
    path.write_text(json.dumps(status))
    ref = review["sources"]["preparation_status"]
    ref.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    with pytest.raises(ValueError, match="metadata command"):
        import_review(reviewed_archive)


def test_disabled_check_requires_actual_effective_skip(reviewed_archive):
    _, review, _, _ = reviewed_archive
    binding = next(b for b in review["ci_bindings"] if b["goal"] == "japicmp:cmp" and b["disposition"] == "requirement")
    binding.update(disposition="ci_disabled", disabled_witness={
        "type": "effective_pom_configuration", "plugin": "japicmp-maven-plugin",
        "path": "configuration/skip", "value": "true", "effective_pom_sha256": review["sources"]["effective_pom"]["sha256"],
        **{k: binding[k] for k in ("module", "goal", "execution")}})
    binding.pop("requirement_id")
    with pytest.raises(ValueError, match="does not disable"):
        import_review(reviewed_archive)


def repeated(row):
    row["validation"].update(native_bindings=[
        {"goal": row["validation"]["goals"][0], "execution": "default", "occurrence": i, "position": i * 2}
        for i in (0, 1)], position=0)
    return row


@pytest.mark.parametrize("second,expected", [("passed", "passed"), ("absent", "unavailable"), ("failed", "failed")])
def test_repeated_lifecycle_requires_both_real_occurrences(fixture, second, expected):
    _, _, invocation, _, base, _ = fixture
    text = banner("compiler:compile") + "[INFO] Compiling 2 source files\n"
    if second != "absent":
        text += banner("compiler:compile")
        text += ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:1.0:compile (default) on project demo: broken\n"
                 if second == "failed" else "[INFO] Nothing to compile - all classes are up to date\n")
    text += "[INFO] BUILD FAILURE\n" if second == "failed" else "[INFO] BUILD SUCCESS\n"
    invocation["exit_code"] = 1 if second == "failed" else 0
    row = repeated(requirement("compile", "compile", "compiler:compile"))
    assert native_requirement(row, invocation, base, text, maven_events(text, terminal=True, serial=True))["status"] == expected


@pytest.mark.parametrize("reuse", ["correct", "absent", "rerun", "wrong_execution", "extra_rerun"])
def test_one_report_pool_passes_only_with_explicit_same_configuration_reuse(fixture, reuse):
    _, spec, invocation, _, base, _ = fixture
    row = deepcopy(spec["requirements"][1])
    row["validation"].update(native_bindings=[{"goal": "surefire:test", "execution": "default", "occurrence": 0, "position": 1}],
                             reused_native_bindings=[{"goal": "surefire:test", "execution": "default", "occurrence": 1, "position": 3}])
    text = banner("surefire:test") + "[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n"
    if reuse != "absent":
        text += banner("surefire:test").replace("(default)", "(other)" if reuse == "wrong_execution" else "(default)")
        text += ("[INFO] Skipping execution of surefire because it has already been run for this configuration\n"
                 if reuse != "rerun" else "[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n")
    text += "[INFO] BUILD SUCCESS\n"
    if reuse == "extra_rerun":
        text = text.replace("[INFO] BUILD SUCCESS\n", banner("surefire:test")
                            + "[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS\n")
    invocation["reports_collection_complete"] = True
    result = native_requirement(row, invocation, base, text, maven_events(text, terminal=True, serial=True))
    assert result["status"] == ("passed" if reuse == "correct" else "unavailable")


def test_late_repeated_failure_cannot_backdate_fail_fast_inference(fixture):
    _, spec, invocation, _, base, score = fixture
    compile_row = repeated(spec["requirements"][0])
    compile_row["validation"]["native_bindings"][1]["position"] = 3
    invocation.update(exit_code=1, reports=[], reports_collection_complete=True,
                      log=write(base, "log.txt", banner("compiler:compile") + "[INFO] Compiling 2 source files\n"
                                + banner("compiler:compile")
                                + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:1.0:compile (default) on project demo: broken\n[INFO] BUILD FAILURE\n"))
    out = score()
    assert out["requirements"][0]["status"] == "failed"
    assert out["requirements"][1]["status"] == "unavailable"
