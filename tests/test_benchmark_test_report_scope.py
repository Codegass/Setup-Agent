"""Frozen report roots keep independent Gradle test obligations independent."""

import copy
import sys

import pytest

from sag.benchmark.evaluator import native_requirement
from sag.benchmark.requirements import canonical_digest, normalize_task, validate_requirements
from test_benchmark_requirement_evaluator import fixture, requirement, write
from test_benchmark_recorder import setup as recorder_setup
from test_sag_requirement_observer import setup as observer_setup, contract, receipt


def test_row(name):
    row = requirement(name, "test", "surefire:test")
    row["validation"] = {
        "rule": "junit",
        "tasks": [":" + name],
        "report_directories": ["build/test-results/" + name],
    }
    return row


test_row.__test__ = False


def xml(failed=False, skipped=False):
    return (
        f'<testsuite tests="1" failures="{int(failed)}" errors="0" skipped="{int(skipped)}">'
        '<testcase classname="Tiny" name="one">'
        + ("<failure/>" if failed else "<skipped/>" if skipped else "")
        + "</testcase></testsuite>"
    )


def report(base, name, **kwargs):
    return {
        **write(base, name + ".xml", xml(**kwargs)),
        "producer_relative_path": "build/test-results/" + name + "/TEST-Tiny.xml",
        "module": "demo",
        "test_kind": "unit",
        "fresh": True,
    }


@pytest.mark.parametrize("second_state", ["missing", "failed", "passed", "skipped"])
def test_other_task_reports_cannot_supply_missing_or_failed_test_task(fixture, second_state):
    _, _, record, _, base, _ = fixture
    record.update(runner="gradle", reports=[report(base, "testA")])
    if second_state != "missing":
        record["reports"].append(
            report(
                base, "testB", failed=second_state == "failed", skipped=second_state == "skipped"
            )
        )
    log = "> Task :testA\n> Task :testB\nBUILD SUCCESSFUL\n"
    first = native_requirement(test_row("testA"), record, base, log, [])
    second = native_requirement(test_row("testB"), record, base, log, [])
    assert first["status"] == "passed"
    assert first["test_counts"]["reported"] == 1
    assert (
        second["status"]
        == {
            "missing": "unavailable",
            "failed": "failed",
            "passed": "passed",
            "skipped": "unavailable",
        }[second_state]
    )


@pytest.mark.parametrize(
    "source",
    [None, "build/test-results/testA-other/TEST.xml", "build/test-results/testA/../testB/TEST.xml"],
)
def test_report_source_requires_exact_directory_boundary(fixture, source):
    _, _, record, _, base, _ = fixture
    record.update(runner="gradle", reports=[report(base, "testA")])
    record["reports"][0]["producer_relative_path"] = source
    out = native_requirement(test_row("testA"), record, base, "> Task :testA\n", [])
    assert out["status"] == "unavailable"


def test_each_declared_directory_needs_assessed_reports(fixture):
    _, _, record, _, base, _ = fixture
    record.update(runner="gradle", reports=[report(base, "testA")])
    row = test_row("testA")
    row["validation"]["tasks"].append(":testB")
    row["validation"]["report_directories"].append("build/test-results/testB")
    out = native_requirement(row, record, base, "> Task :testA\n> Task :testB\n", [])
    assert out["status"] == "unavailable"


@pytest.mark.parametrize("problem", ["missing", "overlap", "parent_overlap"])
def test_unpartitioned_test_scope_cannot_be_complete(fixture, problem):
    task, spec, _, _, _, _ = fixture
    first, second = test_row("testA"), test_row("testB")
    if problem == "missing":
        second["validation"].pop("report_directories")
    else:
        second["validation"]["report_directories"] = [
            "build/test-results/testA" if problem == "overlap" else "build/test-results"
        ]
    spec["requirements"] = [first, second]
    with pytest.raises(ValueError, match="report directories"):
        validate_requirements(spec, task)


def test_review_required_repeated_scope_cannot_earn_requirement_passes(fixture):
    _, spec, _, _, _, score = fixture
    second = copy.deepcopy(spec["requirements"][1])
    second["id"] = "same-pool-again"
    spec["requirements"].append(second)
    spec["annotation_completeness"] = {"status": "review_required", "gaps": ["report_scope"]}
    out = score()
    assert [row["status"] for row in out["requirements"]] == [
        "passed",
        "unavailable",
        "unavailable",
    ]


def test_single_maven_test_requirement_keeps_existing_default(fixture):
    assert fixture[-1]()["requirements"][1]["status"] == "passed"


def test_single_gradle_requirement_needs_explicit_scope(fixture):
    task, spec, record, _, base, _ = fixture
    task["steps"][0]["runner"] = "gradle"
    spec["task_sha256"] = canonical_digest(normalize_task(task))
    with pytest.raises(ValueError, match="Gradle test requirements"):
        validate_requirements(spec, task)
    record["runner"] = "gradle"
    row = test_row("testA")
    row["validation"].pop("report_directories")
    assert native_requirement(row, record, base, "> Task :testA\n", [])["status"] == "unavailable"


@pytest.mark.parametrize(
    "directory",
    [
        "../elsewhere",
        "/tmp/reports",
        "build/../reports",
        "build/reports/",
        ".",
        "build/*",
        "build\\reports",
    ],
)
def test_ambiguous_or_escaping_frozen_report_directory_is_rejected(fixture, directory):
    _, spec, _, _, _, _ = fixture
    spec["requirements"][1]["validation"]["report_directories"] = [directory]
    with pytest.raises(ValueError, match="checkout-relative"):
        validate_requirements(spec)


def test_portable_collector_uses_only_frozen_report_roots(recorder_setup):
    root, make = recorder_setup

    def definitions(spec):
        row = spec["requirements"][0]
        row["validation"]["report_directories"] = ["custom-reports/testA"]
        second = copy.deepcopy(row)
        second["id"] = "second"
        second["validation"]["report_directories"] = ["custom-reports/testB"]
        spec["requirements"].append(second)

    def command(task):
        task["steps"][0]["argv"] = [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            + "; ".join(
                f"p=Path({directory!r}); p.mkdir(parents=True); (p/'TEST-Tiny.xml').write_text({xml()!r})"
                for directory in (
                    "custom-reports/testA",
                    "custom-reports/testB",
                    "build/test-results/unrelated",
                )
            ),
        ]

    recorder = make(task_change=command, spec_change=definitions)
    recorder.start()
    observed = recorder.step("test", timeout=5)
    assert observed["reports_collection_complete"] is True
    assert {r["producer_relative_path"] for r in observed["reports"]} == {
        "custom-reports/testA/TEST-Tiny.xml",
        "custom-reports/testB/TEST-Tiny.xml",
    }


def test_live_observer_export_preserves_frozen_report_producer_path(observer_setup):
    root, _, make = observer_setup

    def definitions(task, spec):
        row = spec["requirements"][0]
        row["validation"]["report_directories"] = ["custom-reports/testA"]
        second = copy.deepcopy(row)
        second["id"] = "second"
        second["validation"]["report_directories"] = ["custom-reports/testB"]
        spec["requirements"] = [row, second]

    observer = make(definitions)
    frozen = contract(observer)
    observer.before_contract(frozen)
    for directory in (
        "custom-reports/testA",
        "custom-reports/testB",
        "build/test-results/unrelated",
    ):
        folder = root / directory
        folder.mkdir(parents=True)
        (folder / "TEST-Tiny.xml").write_text(xml())
    observer.after_receipt(receipt(frozen))
    observed = observer.export_invocation(frozen["contract_id"], base=observer.base)
    assert observed["reports_collection_complete"] is True
    assert {r["producer_relative_path"] for r in observed["reports"]} == {
        "custom-reports/testA/TEST-Tiny.xml",
        "custom-reports/testB/TEST-Tiny.xml",
    }
