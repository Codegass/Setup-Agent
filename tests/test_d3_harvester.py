"""The d3 target harvester: a CI run's evidence assembled into a target record.

The offline assembly is the whole of the logic, so the whole of it is tested
here against a synthetic snapshot built in ``tmp_path`` from the committed
kafka retry suite and laundered workflow fixtures.  The network mode is a
courier in front of this same assembly and is never exercised.

One test reaches for the real probe archive under ``logs/`` and skips when the
checkout does not carry it: that archive is evidence, not a fixture.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from sag.metrics.target_record import target_record_sha256
from scripts.d3_harvest_target import (
    HarvestError,
    apply_job_logs,
    assemble_target_record,
    harvest_from_dir,
    main,
    name_signature,
    pool_covers_check,
    pool_jdk_annotation,
)


@pytest.mark.parametrize("ambiguous_first", [True, False])
def test_ambiguous_maven_log_cannot_be_hidden_by_another_log(tmp_path, ambiguous_first):
    from sag.metrics.target_record import CellTarget

    original = CellTarget(
        cell_id="build",
        build="ok",
        executed_count=1,
        red_count=0,
        grade="A",
        modules=("observed-test-module",),
        modules_basis="test_bearing",
    )
    cells = [original]
    good = "[INFO] Reactor Summary:\n[INFO] core ... SUCCESS\n[INFO] BUILD SUCCESS"
    ambiguous = good.replace(
        "[INFO] BUILD SUCCESS", "[INFO] core ... FAILURE\n[INFO] BUILD FAILURE"
    )
    logs = [ambiguous, good] if ambiguous_first else [good, ambiguous]
    for number, text in enumerate(logs):
        with zipfile.ZipFile(tmp_path / f"run-{number}-logs.zip", "w") as archive:
            archive.writestr("0_build.txt", text)

    notes = apply_job_logs(tmp_path, cells, [])

    assert cells == [original]
    assert any("identity ambiguous" in note for note in notes)
    assert any("multiple job logs" in note for note in notes)


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "target_attainment"
RETRY_SUITE = FIXTURES / "kafka-retry-suite.xml"
LAUNDERED_CI = FIXTURES / "laundered-ci.yml"
# Evidence, not a fixture: the raw archive lives on the checkout that harvested it.
KAFKA_RUN = ROOT / "logs" / "ci-ground-truth-probe-20260827" / "kafka-run-27721225836"

REPO = "apache/demo"
SHA = "26b251a451ce941d3d7a55e6487bcb7f16b5ad48"
HARVESTED_AT = "2026-08-28T09:00:00Z"


def _suite(name: str, cases: tuple[tuple[str, bool], ...]) -> bytes:
    rows = "".join(
        f'<testcase classname="{name}" name="{case}">'
        + ('<failure message="boom">boom</failure>' if failed else "")
        + "</testcase>"
        for case, failed in cases
    )
    return f'<?xml version="1.0"?><testsuite name="{name}">{rows}</testsuite>'.encode("utf-8")


def _pool(directory: Path, pool_id: str, members: dict[str, bytes]) -> Path:
    path = directory / f"{pool_id}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for member, payload in members.items():
            archive.writestr(member, payload)
    return path


def build_snapshot(
    directory: Path,
    *,
    pools: dict[str, dict[str, bytes]] | None = None,
    jobs: tuple[tuple[str, str], ...] = (),
    workflow: Path | None = None,
    statuses: dict[str, object] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for pool_id, members in (pools or {}).items():
        _pool(directory, pool_id, members)
    if jobs:
        (directory / "run-jobs.json").write_text(
            json.dumps(
                {
                    "total_count": len(jobs),
                    "jobs": [
                        {"name": name, "status": "completed", "conclusion": conclusion}
                        for name, conclusion in jobs
                    ],
                }
            ),
            encoding="utf-8",
        )
    if statuses is not None:
        (directory / "commit-statuses.json").write_text(json.dumps(statuses), encoding="utf-8")
    if workflow is not None:
        workflows = directory / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / workflow.name).write_bytes(workflow.read_bytes())
    return directory


@pytest.fixture
def snapshot(tmp_path) -> Path:
    """A kafka-shaped snapshot: two JDK17 pools, one JDK25 pool, four checks."""

    return build_snapshot(
        tmp_path / "run",
        pools={
            "junit-xml-17-noflaky-nonew": {
                "core/17-noflaky-nonew/TEST-kafka.server.ReplicationQuotasTest.xml": (
                    RETRY_SUITE.read_bytes()
                )
            },
            "junit-xml-17-flaky-nonew": {
                "core/17-flaky-nonew/TEST-pkg.FlakyTest.xml": _suite(
                    "pkg.FlakyTest", (("onlyOne()", False),)
                )
            },
            "junit-xml-25-noflaky-nonew": {
                "core/25-noflaky-nonew/TEST-pkg.RedTest.xml": _suite(
                    "pkg.RedTest", (("green()", False), ("red()", True))
                )
            },
        },
        jobs=(
            ("build / JUnit tests Java 17", "success"),
            ("build / JUnit tests Java 17 (flaky)", "success"),
            ("build / Compile and Check (Merge Ref)", "success"),
            ("build / Update Test Catalog", "skipped"),
        ),
    )


def _record(snapshot_dir: Path, *, jdk_major: int | None = None):
    return assemble_target_record(
        snapshot_dir,
        repo=REPO,
        sha=SHA,
        harvested_at=HARVESTED_AT,
        jdk_major=jdk_major,
    )


def _cell(record, cell_id: str):
    return next(cell for cell in record.cells if cell.cell_id == cell_id)


def _has_note(record, fragment: str) -> bool:
    return any(fragment in note for note in record.notes)


class TestPoolAndCheckNames:
    def test_a_pool_covers_the_check_whose_axes_it_names(self):
        assert pool_covers_check("junit-xml-17-noflaky-nonew", "build / JUnit tests Java 17")
        assert pool_covers_check("junit-xml-17-flaky-nonew", "build / JUnit tests Java 17 (flaky)")
        assert pool_covers_check("junit-xml-25-noflaky-new", "build / JUnit tests Java 25 (new)")

    def test_a_pool_does_not_cover_a_check_naming_another_axis(self):
        assert not pool_covers_check(
            "junit-xml-17-noflaky-nonew", "build / JUnit tests Java 17 (flaky)"
        )
        assert not pool_covers_check("junit-xml-17-noflaky-nonew", "build / Load Test Catalog")
        assert not pool_covers_check("junit-xml-17-noflaky-nonew", "build / CI checks completed")

    def test_a_check_with_no_distinguishing_token_is_never_covered(self):
        # Its signature is empty, and an empty set is a subset of every pool.
        assert name_signature("build tests") == frozenset()
        assert not pool_covers_check("junit-xml-17-noflaky-nonew", "build tests")

    def test_the_pool_toolchain_is_read_only_when_the_name_states_one(self):
        assert pool_jdk_annotation("junit-xml-17-noflaky-nonew") == 17
        # Already legible to the record layer, so nothing is appended.
        assert pool_jdk_annotation("junit-xml-java-17") is None
        # Ambiguous, so it is disclosed as unreadable rather than guessed.
        assert pool_jdk_annotation("junit-xml-17-and-21") is None
        assert pool_jdk_annotation("junit-xml-main") is None


class TestPoolCells:
    def test_one_grade_a_cell_per_pool_named_for_its_artifact(self, snapshot):
        record = _record(snapshot)
        pool_cells = [cell for cell in record.cells if cell.grade == "A"]

        assert [cell.cell_id for cell in pool_cells] == [
            "junit-xml-17-flaky-nonew (jdk 17)",
            "junit-xml-17-noflaky-nonew (jdk 17)",
            "junit-xml-25-noflaky-nonew (jdk 25)",
        ]
        assert _cell(record, "junit-xml-17-noflaky-nonew (jdk 17)").evidence_refs == (
            "junit-xml-17-noflaky-nonew.zip",
        )
        assert _has_note(record, "the harvester's reading of the pool artifact name")

    def test_the_retried_test_counts_once_and_reads_as_flaky(self, snapshot):
        cell = _cell(_record(snapshot), "junit-xml-17-noflaky-nonew (jdk 17)")

        assert cell.executed_count == 3
        assert cell.red_count == 0
        assert cell.build == "ok"
        assert cell.flaky_ids == (
            "kafka.server.ReplicationQuotasTest#shouldBootstrapTwoBrokersWithFollowerThrottle()",
        )
        assert len(cell.executed_ids) == 3

    def test_a_finally_red_test_makes_the_cell_build_failed(self, snapshot):
        cell = _cell(_record(snapshot), "junit-xml-25-noflaky-nonew (jdk 25)")

        assert cell.build == "failed"
        assert cell.red_count == 1
        assert cell.red_ids == ("pkg.RedTest#red()",)
        assert cell.flaky_ids == ()

    def test_identities_past_the_record_bound_are_counted_not_listed(self, tmp_path):
        long_name = "a" * 600
        snapshot = build_snapshot(
            tmp_path / "run",
            pools={
                "junit-xml-17-noflaky-nonew": {
                    "core/TEST-pkg.LongTest.xml": _suite("pkg.LongTest", ((long_name, False),))
                }
            },
        )
        cell = _cell(_record(snapshot), "junit-xml-17-noflaky-nonew (jdk 17)")

        assert cell.executed_count == 1
        assert cell.executed_ids == ()
        assert _has_note(_record(snapshot), "512-character bound")

    def test_a_flaky_identity_past_the_bound_is_counted_not_erased(self, tmp_path):
        long_name = "b" * 600
        snapshot = build_snapshot(
            tmp_path / "run",
            pools={
                "junit-xml-17-noflaky-nonew": {
                    # One test, failed then passed on the retry: one flaky execution
                    # whose identity the record cannot hold.
                    "core/TEST-pkg.LongFlakyTest.xml": _suite(
                        "pkg.LongFlakyTest", ((long_name, True), (long_name, False))
                    )
                }
            },
        )
        record = _record(snapshot)
        cell = _cell(record, "junit-xml-17-noflaky-nonew (jdk 17)")

        assert cell.executed_count == 1
        assert cell.red_count == 0
        assert cell.build == "ok"
        assert cell.flaky_count == 1
        assert cell.flaky_ids == ()
        assert _has_note(record, "flaky identities are counted, not listed (1 of them)")

    def test_an_unreadable_pool_is_refused_rather_than_reported_empty(self, tmp_path):
        snapshot = tmp_path / "run"
        snapshot.mkdir()
        (snapshot / "junit-xml-17-noflaky-nonew.zip").write_bytes(b"not a zip")
        with pytest.raises(HarvestError) as excinfo:
            _record(snapshot)
        assert "readable pool archive" in str(excinfo.value)


class TestCheckCells:
    def test_a_check_a_pool_measures_gets_no_second_cell(self, snapshot):
        record = _record(snapshot)
        cell_ids = {cell.cell_id for cell in record.cells}

        assert "build / JUnit tests Java 17" not in cell_ids
        assert "build / JUnit tests Java 17 (flaky)" not in cell_ids
        assert _has_note(record, "2 checks are already measured by a JUnit pool")

    def test_an_uncovered_check_becomes_a_conclusion_grade_cell(self, snapshot):
        record = _record(snapshot)
        cell = _cell(record, "build / Compile and Check (Merge Ref)")

        assert cell.grade == "B"
        assert cell.build == "ok"
        assert cell.executed_count == 0
        assert cell.laundered_conclusion is False
        assert cell.evidence_refs == ("run-jobs.json",)

    def test_a_conclusion_that_judged_nothing_leaves_the_build_unknown(self, snapshot):
        assert _cell(_record(snapshot), "build / Update Test Catalog").build == "unknown"

    def test_a_failed_check_over_pool_measured_work_keeps_its_cell(self, tmp_path):
        """A suite that crashed after uploading partial green XML looks like this."""

        snapshot = build_snapshot(
            tmp_path / "run",
            pools={
                "junit-xml-17-noflaky-nonew": {
                    "core/TEST-pkg.GreenTest.xml": _suite(
                        "pkg.GreenTest", (("one()", False), ("two()", False))
                    )
                }
            },
            jobs=(("build / JUnit tests Java 17", "failure"),),
        )
        record = _record(snapshot, jdk_major=17)
        cell = _cell(record, "build / JUnit tests Java 17")

        assert cell.grade == "B"
        assert cell.build == "failed"
        assert not _has_note(record, "already measured by a JUnit pool")
        assert _has_note(record, "defeater of its counts")
        # The pool still sets the goalpost; the defeater stands beside it.
        assert record.matched_cell == "junit-xml-17-noflaky-nonew (jdk 17)"

    def test_a_snapshot_with_neither_pool_nor_check_is_refused(self, tmp_path):
        empty = tmp_path / "run"
        empty.mkdir()
        with pytest.raises(HarvestError) as excinfo:
            _record(empty)
        assert "no JUnit pool and no check conclusion" in str(excinfo.value)


class TestLaunderingVet:
    @pytest.fixture
    def laundered(self, tmp_path):
        return build_snapshot(
            tmp_path / "run",
            pools={
                "junit-xml-17-noflaky-nonew": {
                    "core/TEST-kafka.server.ReplicationQuotasTest.xml": RETRY_SUITE.read_bytes()
                }
            },
            jobs=(("JDK17 ubuntu-latest", "success"), ("smoke (8, ubuntu-latest)", "success")),
            workflow=LAUNDERED_CI,
        )

    def test_a_laundered_conclusion_is_marked_and_its_outcome_withheld(self, laundered):
        record = _record(laundered)
        cell = _cell(record, "JDK17 ubuntu-latest")

        assert cell.grade == "B"
        assert cell.laundered_conclusion is True
        assert cell.build == "unknown"

    def test_counts_survive_laundering_because_continue_on_error_cannot_touch_them(self, laundered):
        cell = _cell(_record(laundered), "junit-xml-17-noflaky-nonew (jdk 17)")

        assert cell.grade == "A"
        assert cell.laundered_conclusion is False
        assert cell.executed_count == 3
        assert cell.build == "ok"

    def test_the_record_names_the_config_it_vetted_and_where_it_launders(self, laundered):
        record = _record(laundered)

        assert _has_note(record, "laundering vet read laundered-ci.yml")
        assert _has_note(record, "laundered-ci.yml:job:build/step:2")
        assert _has_note(record, "conclusion-grade cells are marked laundered")

    def test_an_unvetted_snapshot_says_so_and_launders_nothing(self, snapshot):
        record = _record(snapshot)

        assert _has_note(record, "no workflow config was harvested")
        assert not any(cell.laundered_conclusion for cell in record.cells)

    def test_a_laundered_conclusion_never_becomes_the_goalpost(self, laundered):
        """The laundered check names JDK17 and counted nothing; the pool wins."""

        record = _record(laundered, jdk_major=17)

        assert record.matched_cell == "junit-xml-17-noflaky-nonew (jdk 17)"
        assert _has_note(record, "laundered conclusion-grade cells cannot be the target")

    def test_a_run_whose_only_toolchain_cell_is_laundered_matches_nothing(self, tmp_path):
        only_conclusions = build_snapshot(
            tmp_path / "run",
            jobs=(("JDK17 ubuntu-latest", "success"),),
            workflow=LAUNDERED_CI,
        )
        record = _record(only_conclusions, jdk_major=17)

        assert record.matched_cell is None


class TestMatchedCell:
    def test_the_widest_proven_universe_on_the_toolchain_is_the_target(self, snapshot):
        record = _record(snapshot, jdk_major=17)

        # Alphabetically the 1-test flaky pool would win; it is not the goalpost.
        assert record.matched_cell == "junit-xml-17-noflaky-nonew (jdk 17)"
        assert _has_note(record, "the widest proven universe on the same toolchain")

    def test_each_toolchain_matches_its_own_cell(self, snapshot):
        assert _record(snapshot, jdk_major=25).matched_cell == "junit-xml-25-noflaky-nonew (jdk 25)"

    def test_a_substituted_toolchain_carries_its_caveat_into_the_notes(self, snapshot):
        record = _record(snapshot, jdk_major=11)

        assert record.matched_cell == "junit-xml-17-noflaky-nonew (jdk 17)"
        assert _has_note(record, "no cell runs JDK11; matched the nearest above, JDK17")

    def test_no_toolchain_asked_for_means_no_cell_matched(self, snapshot):
        assert _record(snapshot).matched_cell is None

    def _pool_and_check(self, tmp_path, *, jobs, workflow=None) -> Path:
        return build_snapshot(
            tmp_path / "run",
            pools={
                "junit-xml-17-noflaky-nonew": {
                    "core/TEST-kafka.server.ReplicationQuotasTest.xml": RETRY_SUITE.read_bytes()
                }
            },
            jobs=jobs,
            workflow=workflow,
        )

    def test_a_platform_labelled_conclusion_never_outranks_the_proven_pool(self, tmp_path):
        """The check name says ubuntu; the pool artifact name says no platform at all.

        ``match_cell`` ranks the labelled cell first for that alone, so the
        widest-universe rule is the only thing keeping the counted cell in front
        of a conclusion on the same toolchain.
        """

        record = _record(
            self._pool_and_check(tmp_path, jobs=(("JDK17 ubuntu-latest", "success"),)),
            jdk_major=17,
        )
        matched = _cell(record, record.matched_cell)

        assert record.matched_cell == "junit-xml-17-noflaky-nonew (jdk 17)"
        assert matched.grade == "A"
        assert matched.executed_count == 3
        assert _has_note(record, "whose platform reads unknown where linux was first matched")

    def test_the_laundered_run_is_measured_against_its_pool_not_its_conclusion(self, tmp_path):
        record = _record(
            self._pool_and_check(
                tmp_path,
                jobs=(("JDK17 ubuntu-latest", "success"), ("smoke (8, ubuntu-latest)", "success")),
                workflow=LAUNDERED_CI,
            ),
            jdk_major=17,
        )

        assert record.matched_cell == "junit-xml-17-noflaky-nonew (jdk 17)"
        assert _cell(record, "JDK17 ubuntu-latest").grade == "B"


class TestWrittenRecord:
    def test_the_digest_is_the_same_across_two_harvests(self, snapshot, tmp_path):
        first = harvest_from_dir(
            snapshot,
            repo=REPO,
            sha=SHA,
            harvested_at=HARVESTED_AT,
            jdk_major=17,
            out_path=tmp_path / "first.json",
        )
        second = harvest_from_dir(
            snapshot,
            repo=REPO,
            sha=SHA,
            harvested_at=HARVESTED_AT,
            jdk_major=17,
            out_path=tmp_path / "second.json",
        )

        assert first[1] == second[1]
        assert (tmp_path / "first.json").read_bytes() == (tmp_path / "second.json").read_bytes()

    def test_the_file_on_disk_digests_to_the_records_own_identity(self, snapshot, tmp_path):
        record, digest, destination = harvest_from_dir(
            snapshot,
            repo=REPO,
            sha=SHA,
            harvested_at=HARVESTED_AT,
            jdk_major=17,
            out_path=tmp_path / "target_record.json",
        )

        assert digest == target_record_sha256(record)
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest

    def test_the_record_lands_beside_the_snapshot_by_default(self, snapshot):
        _, _, destination = harvest_from_dir(
            snapshot, repo=REPO, sha=SHA, harvested_at=HARVESTED_AT, jdk_major=17
        )
        assert destination == snapshot / "target_record.json"

    def test_the_revision_is_read_from_the_snapshot_when_not_passed(self, tmp_path):
        directory = build_snapshot(
            tmp_path / "run",
            pools={
                "junit-xml-17-noflaky-nonew": {
                    "core/TEST-kafka.server.ReplicationQuotasTest.xml": RETRY_SUITE.read_bytes()
                }
            },
            statuses={"sha": SHA, "repository": {"full_name": "apache/kafka"}, "statuses": []},
        )
        record, _, _ = harvest_from_dir(
            directory, repo=None, sha=None, harvested_at=HARVESTED_AT, jdk_major=None
        )

        assert record.repo == "apache/kafka"
        assert record.sha == SHA

    def test_a_snapshot_that_names_no_revision_asks_for_one(self, tmp_path):
        directory = build_snapshot(
            tmp_path / "run",
            pools={"junit-xml-17-noflaky-nonew": {"TEST-x.xml": _suite("x", (("a()", False),))}},
        )
        with pytest.raises(HarvestError) as excinfo:
            harvest_from_dir(
                directory, repo=None, sha=None, harvested_at=HARVESTED_AT, jdk_major=None
            )
        assert "--repo" in str(excinfo.value)


class TestCli:
    def test_the_summary_line_states_the_digest_and_the_matched_cell(self, snapshot, capsys):
        exit_code = main(
            [
                "--from-dir",
                str(snapshot),
                "--repo",
                REPO,
                "--sha",
                SHA,
                "--jdk",
                "17",
                "--harvested-at",
                HARVESTED_AT,
            ]
        )
        assert exit_code == 0

        line = capsys.readouterr().out.strip()
        written = (snapshot / "target_record.json").read_bytes()
        assert f"sha256={hashlib.sha256(written).hexdigest()}" in line
        assert "cells=5 grade_a=3 grade_b=2" in line
        assert "matched=junit-xml-17-noflaky-nonew (jdk 17)" in line
        assert "executed=3 red=0 flaky=1" in line
        assert "laundered=false" in line

    def test_a_broken_snapshot_exits_one_with_an_empty_stdout(self, tmp_path, capsys):
        assert main(["--from-dir", str(tmp_path / "missing")]) == 1

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "D3 HARVEST" in captured.err

    def test_network_mode_without_an_output_directory_is_refused(self, capsys):
        assert main(["--repo", REPO, "--sha", SHA]) == 1
        assert "--out-dir" in capsys.readouterr().err


class TestKafkaRunArchive:
    """The real 2026-08-27 probe snapshot, when this checkout carries it."""

    @pytest.fixture(scope="class")
    def record(self):
        if not KAFKA_RUN.is_dir():
            pytest.skip("the ci-ground-truth probe archive is not present under logs/")
        return assemble_target_record(
            KAFKA_RUN,
            repo="apache/kafka",
            sha="26b251a451ce941d3d7a55e6487bcb7f16b5ad48",
            harvested_at=HARVESTED_AT,
            jdk_major=17,
        )

    def test_every_pool_and_every_uncovered_check_is_a_cell(self, record):
        grades = [cell.grade for cell in record.cells]
        assert grades.count("A") == 6
        assert grades.count("B") == 6

    def test_the_jdk17_main_pool_is_the_matched_cell(self, record):
        assert record.matched_cell == "junit-xml-17-noflaky-nonew (jdk 17)"

    def test_the_matched_cell_carries_the_runs_ground_truth(self, record):
        cell = _cell(record, "junit-xml-17-noflaky-nonew (jdk 17)")

        assert cell.executed_count == 36_259
        assert len(cell.flaky_ids) == 5
        assert cell.red_count == 0
        assert cell.build == "ok"
        # 36,259 identities do not fit the record; the counts are the claim.
        assert cell.executed_ids == ()

    def test_the_six_junit_checks_are_measured_by_pools_not_conclusions(self, record):
        cell_ids = {cell.cell_id for cell in record.cells}
        assert "build / JUnit tests Java 17" not in cell_ids
        assert "build / Compile and Check (Merge Ref)" in cell_ids
        assert _has_note(record, "6 checks are already measured by a JUnit pool")


from scripts.d3_harvest_target import pool_member_module, read_pool


@pytest.mark.parametrize(
    "member, module",
    [
        ("clients/17-noflaky-nonew/TEST-a.xml", "clients"),
        ("connect/runtime/17-noflaky-nonew/TEST-a.xml", "connect/runtime"),
        ("streams/integration-tests/17-noflaky-nonew/TEST-a.xml", "streams/integration-tests"),
        ("core/build/test-results/test/TEST-a.xml", "core"),
        ("build/test-results/test/TEST-a.xml", "."),
        ("cli/target/surefire-reports/TEST-a.xml", "cli"),
        ("target/failsafe-reports/TEST-a.xml", "."),
        ("cli/TEST-a.xml", "cli"),
        ("TEST-a.xml", "."),
    ],
)
def test_pool_member_module_reads_every_upload_layout(member, module):
    assert pool_member_module(member) == module


def test_read_pool_states_the_test_bearing_modules(tmp_path):
    pool = _pool(
        tmp_path,
        "junit-xml-17-noflaky-nonew",
        {
            "clients/17-noflaky-nonew/TEST-a.xml": _suite("a.A", (("t1", False),)),
            "connect/runtime/17-noflaky-nonew/TEST-b.xml": _suite("b.B", (("t2", False),)),
            "clients/17-noflaky-nonew/TEST-c.xml": _suite("c.C", (("t3", True),)),
        },
    )

    reading = read_pool(pool)

    assert reading.modules == ("clients", "connect/runtime")
    assert reading.layout == "upload-prefix"


def test_a_pool_cell_carries_its_modules_as_a_lower_bound(tmp_path):
    snapshot = build_snapshot(
        tmp_path / "snap",
        pools={
            "junit-xml-17-noflaky-nonew": {
                "clients/17-noflaky-nonew/TEST-a.xml": _suite("a.A", (("t1", False),)),
                "core/17-noflaky-nonew/TEST-b.xml": _suite("b.B", (("t2", False),)),
            }
        },
    )

    record = _record(snapshot)
    cell = _cell(record, "junit-xml-17-noflaky-nonew (jdk 17)")

    assert cell.modules == ("clients", "core")
    assert cell.modules_basis == "test_bearing"
    assert _has_note(record, "test-bearing lower bound")


def test_a_job_log_outranks_the_pool_lower_bound(tmp_path):
    snapshot = build_snapshot(
        tmp_path / "snap",
        pools={
            "junit-xml-17-noflaky-nonew": {
                "clients/17-noflaky-nonew/TEST-a.xml": _suite("a.A", (("t1", False),)),
            }
        },
        jobs=(("JUnit tests Java 17", "success"),),
    )
    with zipfile.ZipFile(snapshot / "run-1-logs.zip", "w") as archive:
        archive.writestr(
            "0_JUnit tests Java 17.txt",
            "> Task :clients:compileJava\n> Task :core:compileJava\n> Task :generator:compileJava\n",
        )

    record = _record(snapshot)
    cell = _cell(record, "junit-xml-17-noflaky-nonew (jdk 17)")

    assert cell.modules == ("clients", "core", "generator")
    assert cell.modules_basis == "log"
    assert _has_note(record, "job log")


def test_nested_gradle_output_cannot_claim_maven_repository_root(tmp_path):
    snapshot = build_snapshot(tmp_path / "snap", jobs=(("JDK 21, DB derby", "success"),))
    with zipfile.ZipFile(snapshot / "run-1-logs.zip", "w") as archive:
        archive.writestr(
            "0_JDK 21, DB derby.txt",
            "##[group]Run mvn verify -q -DcayenneTestConnection=derby\n"
            "mvn verify -q -DcayenneTestConnection=derby\n##[endgroup]\n"
            "> Task :compileJava\n> Task :classes\n> Task :jar\nBUILD SUCCESSFUL\n",
        )
    record = _record(snapshot)
    cell = _cell(record, "JDK 21, DB derby")
    assert cell.modules == ()
    assert cell.modules_basis is None
    assert _has_note(record, "no proven project root")


def test_ci_command_preserves_separate_build_steps(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    name: Build JDK 17\n    steps:\n"
        "      - run: mvn install -DskipTests\n"
        "      - run: mvn verify -Pcoverage\n"
    )
    snapshot = build_snapshot(
        tmp_path / "snap", jobs=(("Build JDK 17", "success"),), workflow=workflow
    )
    cell = _cell(_record(snapshot), "Build JDK 17")
    assert cell.command == "mvn install -DskipTests\nmvn verify -Pcoverage"
    assert cell.modules == ()
    assert cell.executed_count == 0


def test_literal_matrix_cell_resolves_command_and_cwd_without_cross_cell_merge(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    name: Build ${{ matrix.java }} in ${{ matrix.directory }}\n"
        "    strategy:\n      matrix:\n        java: [17, 21]\n        directory: [core, tools]\n"
        "    steps:\n      - run: mvn verify -Pjdk${{ matrix.java }}\n"
        "        working-directory: ${{ matrix.directory }}\n"
    )
    snapshot = build_snapshot(
        tmp_path / "snap",
        jobs=(("Build 17 in core", "success"), ("Build 21 in tools", "success")),
        workflow=workflow,
    )
    record = _record(snapshot)
    first = _cell(record, "Build 17 in core")
    other = _cell(record, "Build 21 in tools")
    assert "cd core" in first.command and "-Pjdk17" in first.command
    assert "cd tools" in other.command and "-Pjdk21" in other.command
    assert first.executed_count == other.executed_count == 0


def test_controlled_target_scores_then_loses_scope_and_detects_new_red(tmp_path):
    from sag.metrics.attainment import CertificateView, evaluate_attainment

    snapshot = build_snapshot(
        tmp_path / "controlled",
        pools={
            "junit-xml-17-control": {
                "core/17-control/TEST-a.xml": _suite("a.A", (("one", False), ("two", False)))
            }
        },
    )
    record = _record(snapshot)
    cell = record.cells[0]
    record = type(record)(**(record.model_dump() | {"matched_cell": cell.cell_id}))
    assert cell.grade == "A" and cell.modules == ("core",)
    view = CertificateView(
        authority_ok=True,
        counts_receipt_bound=True,
        build_ok=True,
        repo=REPO,
        target_sha=SHA,
        modules=cell.modules,
        executed_count=cell.executed_count,
        executed_ids=cell.executed_ids,
        red_count=0,
    )
    result = evaluate_attainment(view, record)
    assert result.verdict == "met" and result.alpha is not None

    payload = cell.model_dump(mode="json")
    payload.update(modules=(), modules_basis=None)
    without_scope = type(cell)(**payload)
    ablated = evaluate_attainment(
        view, type(record)(**(record.model_dump() | {"cells": (without_scope,)}))
    )
    assert ablated.alpha is None and ablated.verdict not in {"met", "exceeded"}

    red_view = CertificateView(
        **(view.model_dump() | {"red_count": 1, "red_ids": (cell.executed_ids[0],)})
    )
    negative = evaluate_attainment(red_view, record)
    assert negative.verdict == "not_met"
    assert negative.unexpected_red_ids == (cell.executed_ids[0],)


def test_declared_sources_fill_modules_when_no_log_exists(tmp_path, monkeypatch):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    name: build (${{ matrix.java }})\n    steps:\n"
        "      - run: ./gradlew build -x test\n",
        encoding="utf-8",
    )
    snapshot = build_snapshot(
        tmp_path / "snap", jobs=(("build (17)", "success"),), workflow=workflow
    )
    sources = snapshot / "sources"
    sources.mkdir()
    (sources / "settings.gradle").write_text(
        "include 'clients', 'core'\nproject(':core').projectDir = file('kafka-core')\n",
        encoding="utf-8",
    )

    _verify_test_source(monkeypatch, sources / "settings.gradle", "settings.gradle")

    record = _record(snapshot)
    cell = _cell(record, "build (17)")

    assert cell.modules == (".", "clients", "kafka-core")
    assert cell.modules_basis == "declared"
    assert cell.command == "./gradlew build -x test"
    assert _has_note(record, "declared reactor")


def test_parser_accepts_refetch_flags_on_an_existing_snapshot():
    from scripts.d3_harvest_target import build_parser

    args = build_parser().parse_args(
        [
            "--from-dir",
            "x",
            "--repo",
            "apache/kafka",
            "--sha",
            "a" * 40,
            "--fetch-logs",
            "--fetch-sources",
        ]
    )

    assert args.fetch_logs is True
    assert args.fetch_sources is True


def test_multiline_ci_script_survives_harvest_and_parity(tmp_path):
    from sag.metrics.parity import parity_from_texts

    workflow = tmp_path / "ci.yml"
    script = "mvn compile\nmvn verify"
    workflow.write_text(
        "jobs:\n  build:\n    name: build (17)\n    steps:\n      - run: |\n          mvn compile\n          mvn verify\n"
    )
    snapshot = build_snapshot(
        tmp_path / "snap", jobs=(("build (17)", "success"),), workflow=workflow
    )
    cell = _cell(_record(snapshot), "build (17)")
    assert cell.command == script
    assert parity_from_texts(cell.command, ["mvn compile"]).status == "unknown"


def test_unknown_workflow_cwd_cannot_claim_root_scope_or_parity(tmp_path):
    from sag.metrics.parity import parity_from_texts

    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    name: build (17)\n    defaults:\n      run:\n        working-directory: ${{ matrix.directory }}\n    steps:\n      - run: mvn test\n"
    )
    snapshot = build_snapshot(
        tmp_path / "snap", jobs=(("build (17)", "success"),), workflow=workflow
    )
    cell = _cell(_record(snapshot), "build (17)")
    assert "${{ matrix.directory }}" in cell.command
    assert parity_from_texts(cell.command, ["mvn test"]).status == "unknown"
    assert cell.modules_basis is None


def test_fetch_source_refuses_unproven_cache_when_pinned_fetch_fails(tmp_path, monkeypatch):
    import scripts.d3_harvest_target as harvester

    source = tmp_path / "pom.xml"
    source.write_text("foreign bytes")

    def unavailable(path):
        raise HarvestError("offline")

    monkeypatch.setattr(harvester, "fetch", unavailable)
    assert harvester._fetch_source(REPO, SHA, "pom.xml", source) is False


def test_fetch_source_validates_existing_bytes_before_recording_origin(tmp_path, monkeypatch):
    import base64

    import scripts.d3_harvest_target as harvester

    source = tmp_path / "pom.xml"
    source.write_text("foreign bytes")
    monkeypatch.setattr(
        harvester,
        "fetch",
        lambda path: {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(b"pinned bytes").decode(),
        },
    )
    with pytest.raises(HarvestError, match="does not match"):
        harvester._fetch_source(REPO, SHA, "pom.xml", source)
    assert source.read_text() == "foreign bytes"


def _verify_test_source(monkeypatch, destination, relative_path):
    """Exercise the courier's real origin writer with a pinned API fixture."""
    import base64

    import scripts.d3_harvest_target as harvester

    monkeypatch.setattr(
        harvester,
        "fetch",
        lambda path: {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(destination.read_bytes()).decode(),
        },
    )
    assert harvester._fetch_source(REPO, SHA, relative_path, destination)


def test_verified_source_cache_requires_subject_path_and_unchanged_bytes(tmp_path, monkeypatch):
    import scripts.d3_harvest_target as harvester

    source = tmp_path / "pom.xml"
    source.write_text("pinned bytes")
    _verify_test_source(monkeypatch, source, "pom.xml")

    def unavailable(path):
        raise HarvestError("offline")

    monkeypatch.setattr(harvester, "fetch", unavailable)
    assert harvester._fetch_source(REPO, SHA, "pom.xml", source)
    assert not harvester._fetch_source("other/repo", SHA, "pom.xml", source)
    assert not harvester._fetch_source(REPO, "b" * 40, "pom.xml", source)
    assert not harvester._fetch_source(REPO, SHA, "other/pom.xml", source)
    source.write_text("changed after verification")
    assert not harvester._fetch_source(REPO, SHA, "pom.xml", source)


def test_literal_workflow_directory_selects_its_verified_pom(tmp_path, monkeypatch):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    name: build (17)\n    steps:\n      - working-directory: subproject\n        run: mvn test\n"
    )
    snapshot = build_snapshot(
        tmp_path / "snap", jobs=(("build (17)", "success"),), workflow=workflow
    )
    sources = snapshot / "sources"
    (sources / "subproject").mkdir(parents=True)
    root = sources / "pom.xml"
    root.write_text(
        "<project><artifactId>root</artifactId><modules><module>wrong</module></modules></project>"
    )
    selected = sources / "subproject" / "pom.xml"
    selected.write_text("<project><artifactId>selected</artifactId></project>")
    _verify_test_source(monkeypatch, root, "pom.xml")
    _verify_test_source(monkeypatch, selected, "subproject/pom.xml")
    cell = _cell(_record(snapshot), "build (17)")
    assert cell.modules == (".",)
    assert cell.modules_basis == "declared"
    assert cell.command == "cd subproject &&\nmvn test"
    # Losing the pinned source bytes cannot silently reinstate root scope.
    selected.write_text("<project><artifactId>changed</artifactId></project>")
    assert _cell(_record(snapshot), "build (17)").modules_basis is None


def test_unmarked_sources_are_not_admitted_by_offline_assembly(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    name: build (17)\n    steps:\n      - run: mvn test\n"
    )
    snapshot = build_snapshot(
        tmp_path / "snap", jobs=(("build (17)", "success"),), workflow=workflow
    )
    sources = snapshot / "sources"
    sources.mkdir()
    (sources / "pom.xml").write_text("<project><artifactId>unproven</artifactId></project>")
    assert _cell(_record(snapshot), "build (17)").modules_basis is None
