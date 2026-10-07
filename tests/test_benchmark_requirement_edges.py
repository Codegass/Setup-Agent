"""Independent adversarial checks for native boundaries and RAT attribution."""

import copy
import json

import pytest

from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import maven_events
from sag.benchmark.rat_attribution import attribute_rat_failure
from sag.benchmark.requirements import aggregate, evaluation_identity
from test_benchmark_requirement_evaluator import fixture, banner, requirement, write


def test_duplicate_compile_goal_cannot_satisfy_missing_jar_goal(fixture):
    _, _, record, _, base, _ = fixture
    log = (
        banner("compiler:compile") + "[INFO] Compiling 2 source files\n"
    ) * 2 + "[INFO] BUILD SUCCESS\n"
    row = requirement("both", "compile", "compiler:compile")
    row["validation"]["goals"] = ["compiler:compile", "jar:jar"]
    out = native_requirement(row, record, base, log, maven_events(log, terminal=True, serial=True))
    assert out["status"] == "unavailable"


def test_junit_subset_cannot_satisfy_native_execution_total(fixture):
    _, _, record, _, base, score = fixture
    log = (
        banner("compiler:compile")
        + "[INFO] Compiling 2 source files\n"
        + banner("surefire:test")
        + "[INFO] Tests run: 99, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS\n"
    )
    record.update(log=write(base, "log.txt", log), reports_collection_complete=True)
    out = score()
    assert out["status"] != "complete"
    assert out["requirements"][1]["status"] == "unavailable"
    assert out["requirements"][1]["reason"] == "native_and_junit_count_conflict"


def test_all_skipped_reports_do_not_prove_tests_executed(fixture):
    _, _, record, _, base, score = fixture
    record["reports"][0].update(
        write(
            base,
            "TEST.xml",
            '<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase name="a"><skipped/></testcase></testsuite>',
        )
    )
    record["reports_collection_complete"] = True
    out = score()
    assert out["requirements"][1]["status"] == "unavailable"
    assert out["requirements"][1]["test_counts"]["assessed"] == 0


@pytest.mark.parametrize("change", ["truncated", "forked"])
def test_incomplete_native_observation_never_proves_compile_stage(fixture, change):
    _, _, record, _, base, score = fixture
    if change == "truncated":
        record["log_complete"] = False
    else:
        record["log"] = write(
            base, "log.txt", "[INFO] >>> compiler:compile >>>\n" + (base / "log.txt").read_text()
        )
    out = score()
    assert out["groups"]["build"] != "complete"
    assert out["requirements"][0]["status"] == "unavailable"


def test_fail_fast_propagates_through_not_run_chain(fixture):
    _, spec, record, _, base, score = fixture
    package = requirement("package", "package", "jar:jar", depends_on=["tests"])
    package["expectations"] = {
        "artifacts": [{"path": "target/demo.jar", "role": "main", "module": "demo"}]
    }
    package["validation"]["position"] = 2
    spec["requirements"].append(package)
    record.update(
        exit_code=1,
        reports=[],
        log=write(
            base,
            "log.txt",
            banner("compiler:compile")
            + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:1.0:compile (default) on project demo: bad source\n[INFO] BUILD FAILURE\n",
        ),
    )
    out = score()
    assert [r["status"] for r in out["requirements"]] == ["failed", "not_run", "not_run"]
    by_id = {row["id"]: row for row in out["requirements"]}
    blocker = by_id["package"]
    while blocker["status"] == "not_run":
        blocker = by_id[blocker["blocker_id"]]
    assert blocker["id"] == "compile"
    assert out["known_blockers"] == ["compile"]


def test_passed_tests_do_not_block_not_run_quality_after_javadoc_failure(fixture):
    _, spec, record, _, base, score = fixture
    docs = requirement("docs", "documentation", "javadoc:javadoc", depends_on=["tests"])
    check = requirement("check", "quality_check", "checkstyle:check", depends_on=["docs"])
    docs["validation"]["position"] = 2
    check["validation"]["position"] = 3
    spec["requirements"] += [docs, check]
    record["reports_collection_complete"] = True
    log = (
        (base / "log.txt")
        .read_text()
        .replace(
            "[INFO] BUILD SUCCESS\n",
            banner("javadoc:javadoc")
            + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-javadoc-plugin:1.0:javadoc (default) on project demo: bad docs\n[INFO] BUILD FAILURE\n",
        )
    )
    record.update(exit_code=1, log=write(base, "log.txt", log))
    out = score()
    assert [r["status"] for r in out["requirements"]] == ["passed", "passed", "failed", "not_run"]


@pytest.mark.parametrize("serial,expected", [(True, "passed"), (False, "unavailable")])
def test_gradle_continue_retains_only_independently_proven_returns(fixture, serial, expected):
    _, _, record, _, base, _ = fixture
    record.update(runner="gradle", exit_code=1)
    record["effective_execution"].update(serial=serial, argv=["gradle", "build", "--continue"])
    log = "> Task :test\n> Task :javadoc FAILED\nFAILURE: Build failed with an exception.\n"
    tests = requirement("tests", "test", "surefire:test")
    tests["validation"] = {
        "rule": "junit",
        "tasks": [":test"],
        "report_directories": ["build/test-results/test"],
    }
    record["reports"][0]["producer_relative_path"] = "build/test-results/test/TEST.xml"
    docs = requirement("docs", "documentation", "javadoc:javadoc")
    docs["validation"] = {"rule": "native_goal", "tasks": [":javadoc"]}
    record["reports_collection_complete"] = True
    assert native_requirement(tests, record, base, log, [])["status"] == expected
    assert native_requirement(docs, record, base, log, [])["status"] == "failed"


def test_conditional_group_counts_projects_not_module_requirements(fixture):
    _, spec, _, _, _, score = fixture
    first = score()
    many = copy.deepcopy(spec)
    for suffix in ("second", "third"):
        row = copy.deepcopy(many["requirements"][0])
        row["id"] = f"compile-{suffix}"
        many["requirements"].append(row)
        first["requirements"].append({"id": row["id"], "status": "passed"})
    first["evaluation_identity"] = evaluation_identity(many)
    one = copy.deepcopy(spec)
    one["project_id"] = "other"
    second = copy.deepcopy(first)
    second.update(
        project_id="other", evaluation_identity=evaluation_identity(one), status="incomplete"
    )
    second["requirements"] = [
        {"id": "compile", "status": "failed"},
        {"id": "tests", "status": "not_run"},
    ]
    counts = aggregate({"demo": many, "other": one}, [first, second])["conditional_requirements"][
        "compile"
    ]
    assert counts["planned"] == counts["reached"] == 2
    assert counts["conditional_pass_rate"] == 0.5


@pytest.fixture
def rat_fixture(fixture):
    _, _, _, run, base, _ = fixture
    before = next(
        r
        for r in run["worktree"]
        if json.loads((base / r["path"]).read_text())["boundary"] == "task_start"
    )
    after = next(
        r
        for r in run["worktree"]
        if json.loads((base / r["path"]).read_text())["boundary"] == "evidence_close"
    )
    b = json.loads((base / before["path"]).read_text())
    a = json.loads((base / after["path"]).read_text())
    b["observed_at"] = "2026-09-22T00:00:00Z"
    a["observed_at"] = "2026-09-22T00:02:00Z"
    a["probes"]["untracked"]["stdout"] = write(base, "untracked.bin", "agent-note.txt\0")
    before = write(base, before["path"], b)
    after = write(base, after["path"], a)
    content = write(base, "retained-note.txt", "diagnosis without license header")
    writes = {
        "run_id": "run-1",
        "project_root": b["project_root"],
        "source": "trusted_write_observer",
        "coverage": "complete",
        "events": [
            {
                "path": "agent-note.txt",
                "origin": "agent",
                "observed_at": "2026-09-22T00:01:00Z",
                "content": content,
            }
        ],
    }
    report = {
        "run_id": "run-1",
        "invocation_id": "rat-1",
        "project_root": b["project_root"],
        "report": write(
            base,
            "rat.xml",
            '<rat-report><resource name="agent-note.txt"><license-approval name="false"/></resource></rat-report>',
        ),
        "file_contents": [{"project_relative_path": "agent-note.txt", "content": content}],
    }

    def attribute():
        return attribute_rat_failure(
            base,
            "run-1",
            before,
            after,
            write(base, "writes.json", writes),
            write(base, "rat-report.json", report),
        )

    return base, before, after, writes, report, attribute


@pytest.mark.parametrize("origin", ["agent", "harness"])
def test_rat_new_file_requires_write_origin_and_exact_rejected_content(rat_fixture, origin):
    _, _, _, writes, _, attribute = rat_fixture
    writes["events"][0]["origin"] = origin
    out = attribute()
    assert out["origin"] == origin
    assert out["paths"] == [
        {
            "path": "agent-note.txt",
            "origin": origin,
            "reason": "new_file_write_and_rat_content_bound",
        }
    ]


@pytest.mark.parametrize(
    "change",
    [
        "no_event",
        "wrong_run",
        "stale_content",
        "unobserved_channels",
        "outside_interval",
        "no_named_rejection",
        "foreign_report",
    ],
)
def test_rat_does_not_guess_origin_from_new_filename(rat_fixture, change):
    base, _, _, writes, report, attribute = rat_fixture
    if change == "no_event":
        writes["events"] = []
    elif change == "wrong_run":
        writes["run_id"] = "old"
    elif change == "stale_content":
        writes["events"][0]["content"] = write(base, "old-note.txt", "different")
    elif change == "unobserved_channels":
        writes["coverage"] = "partial"
    elif change == "outside_interval":
        writes["events"][0]["observed_at"] = "2026-09-21T00:00:00Z"
    elif change == "no_named_rejection":
        report["report"] = write(base, "rat.xml", "<rat-report/>")
    elif change == "foreign_report":
        report["run_id"] = "old"
    assert attribute()["origin"] == "unknown"


def test_rat_snapshot_clean_boolean_cannot_override_failed_raw_probe(rat_fixture):
    base, _, after, _, _, attribute = rat_fixture
    snap = json.loads((base / after["path"]).read_text())
    snap["untracked_paths"] = ["agent-note.txt"]
    snap["probes"]["untracked"]["exit_code"] = 1
    after.update(write(base, after["path"], snap))
    assert attribute()["origin"] == "unknown"
