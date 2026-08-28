"""JUnit parsing and retry deconvolution, against synthetic and real evidence."""

import zipfile
from pathlib import Path

import pytest

# Aliased: pytest would otherwise try to collect the model as a test class.
from sag.metrics.junit_deconvolution import TestEntry as JunitEntry
from sag.metrics.junit_deconvolution import (
    deconvolve,
    parse_junit_entries,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "target_attainment"
KAFKA_RETRY_SUITE = FIXTURES / "kafka-retry-suite.xml"
# The raw probe archive is evidence, not a committed fixture: it lives under
# logs/ on the checkout that harvested it and is absent elsewhere.
KAFKA_RUN = ROOT / "logs" / "ci-ground-truth-probe-20260827" / "kafka-run-27721225836"
KAFKA_MAIN_POOL = KAFKA_RUN / "junit-xml-17-noflaky-nonew.zip"


def _entries(*pairs):
    return tuple(JunitEntry(test_id=test_id, outcome=outcome) for test_id, outcome in pairs)


class TestParseJunitEntries:
    def test_a_testsuite_root_is_accepted(self):
        xml = b"""<?xml version="1.0"?>
        <testsuite name="s">
          <testcase classname="pkg.C" name="a"/>
        </testsuite>"""
        assert parse_junit_entries(xml) == _entries(("pkg.C#a", "passed"))

    def test_a_testsuites_wrapper_is_accepted(self):
        xml = b"""<?xml version="1.0"?>
        <testsuites>
          <testsuite name="one"><testcase classname="pkg.C" name="a"/></testsuite>
          <testsuite name="two"><testcase classname="pkg.D" name="b"/></testsuite>
        </testsuites>"""
        assert parse_junit_entries(xml) == _entries(("pkg.C#a", "passed"), ("pkg.D#b", "passed"))

    def test_child_elements_decide_the_outcome(self):
        xml = b"""<?xml version="1.0"?>
        <testsuite name="s">
          <testcase classname="pkg.C" name="green"/>
          <testcase classname="pkg.C" name="bad"><failure message="m"/></testcase>
          <testcase classname="pkg.C" name="broken"><error message="m"/></testcase>
          <testcase classname="pkg.C" name="off"><skipped/></testcase>
        </testsuite>"""
        assert parse_junit_entries(xml) == _entries(
            ("pkg.C#green", "passed"),
            ("pkg.C#bad", "failed"),
            ("pkg.C#broken", "error"),
            ("pkg.C#off", "skipped"),
        )

    def test_failure_wins_over_error_on_one_testcase(self):
        xml = b"""<?xml version="1.0"?>
        <testsuite name="s">
          <testcase classname="pkg.C" name="a"><failure/><error/></testcase>
        </testsuite>"""
        assert parse_junit_entries(xml)[0].outcome == "failed"

    def test_a_system_out_child_does_not_change_a_pass(self):
        xml = b"""<?xml version="1.0"?>
        <testsuite name="s">
          <testcase classname="pkg.C" name="a"><system-out>noise</system-out></testcase>
        </testsuite>"""
        assert parse_junit_entries(xml)[0].outcome == "passed"

    def test_document_order_is_preserved_and_repeats_are_kept(self):
        xml = b"""<?xml version="1.0"?>
        <testsuite name="s">
          <testcase classname="pkg.C" name="z"/>
          <testcase classname="pkg.C" name="a"><failure/></testcase>
          <testcase classname="pkg.C" name="a"/>
        </testsuite>"""
        assert [entry.test_id for entry in parse_junit_entries(xml)] == [
            "pkg.C#z",
            "pkg.C#a",
            "pkg.C#a",
        ]

    def test_a_testcase_without_a_classname_uses_its_bare_name(self):
        xml = b'<testsuite name="s"><testcase name="a"/></testsuite>'
        assert parse_junit_entries(xml)[0].test_id == "a"

    def test_a_testcase_without_a_name_is_rejected(self):
        xml = b'<testsuite name="s"><testcase classname="pkg.C"/></testsuite>'
        with pytest.raises(ValueError, match="missing its name"):
            parse_junit_entries(xml)

    def test_malformed_xml_raises_value_error(self):
        with pytest.raises(ValueError, match="malformed JUnit XML"):
            parse_junit_entries(b"<testsuite><testcase name='a'>")

    def test_an_unsupported_root_raises_value_error(self):
        with pytest.raises(ValueError, match="unsupported JUnit root"):
            parse_junit_entries(b"<report><testcase name='a'/></report>")

    def test_an_empty_suite_yields_no_entries(self):
        assert parse_junit_entries(b'<testsuite name="s"/>') == ()


class TestLastEntryRule:
    def test_a_failure_then_pass_pair_is_flaky_and_not_red(self):
        result = deconvolve(_entries(("pkg.C#a", "failed"), ("pkg.C#a", "passed")))
        assert result.executed_ids == ("pkg.C#a",)
        assert result.final_red_ids == ()
        assert result.flaky_ids == ("pkg.C#a",)
        assert result.raw_entry_count == 2

    def test_an_error_then_pass_pair_is_flaky_and_not_red(self):
        result = deconvolve(_entries(("pkg.C#a", "error"), ("pkg.C#a", "passed")))
        assert result.final_red_ids == ()
        assert result.flaky_ids == ("pkg.C#a",)

    def test_a_pass_then_fail_pair_is_finally_red(self):
        # The last entry is the final attempt whichever way the pair runs.
        result = deconvolve(_entries(("pkg.C#a", "passed"), ("pkg.C#a", "failed")))
        assert result.final_red_ids == ("pkg.C#a [duplicate-name 2]",)
        assert result.flaky_ids == ()

    def test_a_failure_that_is_never_retried_stays_red(self):
        result = deconvolve(_entries(("pkg.C#a", "failed")))
        assert result.final_red_ids == ("pkg.C#a",)
        assert result.flaky_ids == ()

    def test_three_attempts_ending_red_stay_red(self):
        result = deconvolve(
            _entries(("pkg.C#a", "failed"), ("pkg.C#a", "failed"), ("pkg.C#a", "error"))
        )
        assert result.executed_ids == ("pkg.C#a",)
        assert result.final_red_ids == ("pkg.C#a",)
        assert result.flaky_ids == ()
        assert result.raw_entry_count == 3

    def test_three_attempts_ending_green_are_flaky(self):
        result = deconvolve(
            _entries(("pkg.C#a", "failed"), ("pkg.C#a", "error"), ("pkg.C#a", "passed"))
        )
        assert result.flaky_ids == ("pkg.C#a",)
        assert result.final_red_ids == ()


class TestDeconvolvedShape:
    def test_executed_ids_are_unique_and_sorted(self):
        result = deconvolve(
            _entries(("z#one", "passed"), ("a#two", "passed"), ("m#three", "passed"))
        )
        assert result.executed_ids == ("a#two", "m#three", "z#one")

    def test_a_final_skip_is_reported_and_is_neither_red_nor_flaky(self):
        result = deconvolve(_entries(("pkg.C#a", "skipped"), ("pkg.C#b", "passed")))
        assert result.final_skipped_ids == ("pkg.C#a",)
        assert result.final_red_ids == ()
        assert result.flaky_ids == ()
        assert "pkg.C#a" in result.executed_ids

    def test_a_retry_that_ends_skipped_is_skipped_not_flaky(self):
        result = deconvolve(_entries(("pkg.C#a", "failed"), ("pkg.C#a", "skipped")))
        assert result.final_skipped_ids == ("pkg.C#a",)
        assert result.flaky_ids == ()
        assert result.final_red_ids == ()

    def test_no_entries_yield_an_empty_deconvolution(self):
        result = deconvolve(())
        assert result.executed_ids == ()
        assert result.raw_entry_count == 0
        assert result.duplicate_name_ids == ()

    def test_the_result_is_frozen(self):
        result = deconvolve(_entries(("pkg.C#a", "passed")))
        with pytest.raises(Exception):
            result.raw_entry_count = 9


class TestTruncatedDisplayNameCollisions:
    """Repeats that never failed are distinct tests, not retries.

    JUnit truncates long parameterized display names, so different parameter
    sets render as the same string.  Merging them would erase real executions
    and invent flaky tests that never once failed.
    """

    def test_repeated_passes_stay_separate_executions(self):
        result = deconvolve(_entries(("pkg.C#p(...)", "passed"), ("pkg.C#p(...)", "passed")))
        assert result.executed_ids == (
            "pkg.C#p(...)",
            "pkg.C#p(...) [duplicate-name 2]",
        )
        assert result.flaky_ids == ()
        assert result.raw_entry_count == 2

    def test_a_colliding_name_is_disclosed_by_its_bare_id(self):
        result = deconvolve(_entries(("pkg.C#p(...)", "passed"), ("pkg.C#p(...)", "passed")))
        assert result.duplicate_name_ids == ("pkg.C#p(...)",)

    def test_a_genuine_retry_is_not_reported_as_a_colliding_name(self):
        result = deconvolve(_entries(("pkg.C#a", "failed"), ("pkg.C#a", "passed")))
        assert result.duplicate_name_ids == ()

    def test_one_colliding_execution_can_still_be_red(self):
        result = deconvolve(_entries(("pkg.C#p(...)", "passed"), ("pkg.C#p(...)", "failed")))
        assert result.final_red_ids == ("pkg.C#p(...) [duplicate-name 2]",)

    def test_a_retry_after_a_collision_attaches_to_the_open_execution(self):
        result = deconvolve(
            _entries(
                ("pkg.C#p(...)", "passed"),
                ("pkg.C#p(...)", "failed"),
                ("pkg.C#p(...)", "passed"),
            )
        )
        assert result.executed_ids == (
            "pkg.C#p(...)",
            "pkg.C#p(...) [duplicate-name 2]",
        )
        assert result.flaky_ids == ("pkg.C#p(...) [duplicate-name 2]",)
        assert result.final_red_ids == ()
        assert result.raw_entry_count == 3

    def test_a_synthesized_identity_never_collides_with_a_real_one(self):
        result = deconvolve(
            _entries(
                ("pkg.C#a", "passed"),
                ("pkg.C#a [duplicate-name 2]", "passed"),
                ("pkg.C#a", "passed"),
            )
        )
        assert len(set(result.executed_ids)) == 3
        assert "pkg.C#a [duplicate-name 3]" in result.executed_ids


class TestKafkaRetryFixture:
    """The committed suite from apache/kafka run 27721225836."""

    @pytest.fixture(scope="class")
    def result(self):
        return deconvolve(parse_junit_entries(KAFKA_RETRY_SUITE.read_bytes()))

    def test_the_suite_reports_four_rows_for_three_tests(self, result):
        assert result.raw_entry_count == 4
        assert len(result.executed_ids) == 3

    def test_the_retried_test_is_the_only_flaky_one(self, result):
        assert result.flaky_ids == (
            "kafka.server.ReplicationQuotasTest" "#shouldBootstrapTwoBrokersWithFollowerThrottle()",
        )

    def test_nothing_is_finally_red_despite_the_declared_failure(self, result):
        assert b'failures="1"' in KAFKA_RETRY_SUITE.read_bytes()
        assert result.final_red_ids == ()

    def test_the_fixture_carries_no_name_collisions(self, result):
        assert result.duplicate_name_ids == ()

    def test_the_fixture_names_its_source_run_and_no_absolute_path(self):
        text = KAFKA_RETRY_SUITE.read_text(encoding="utf-8")
        assert "27721225836" in text
        assert "/Users/" not in text


class TestKafkaMainPoolArchive:
    """The whole JDK17 main pool, when the raw probe archive is on this checkout."""

    @pytest.fixture(scope="class")
    def result(self):
        if not KAFKA_MAIN_POOL.exists():
            pytest.skip("the ci-ground-truth probe archive is not present under logs/")
        entries: list[JunitEntry] = []
        with zipfile.ZipFile(KAFKA_MAIN_POOL) as archive:
            for name in sorted(n for n in archive.namelist() if n.endswith(".xml")):
                entries.extend(parse_junit_entries(archive.read(name)))
        return deconvolve(tuple(entries))

    def test_the_pool_deconvolves_to_its_ground_truth(self, result):
        assert result.raw_entry_count == 36_264
        assert len(result.executed_ids) == 36_259
        assert len(result.flaky_ids) == 5
        assert result.final_red_ids == ()

    def test_every_declared_failure_is_a_retry_that_passed(self, result):
        # The pool's own failures= attributes sum to 5, and deconvolution
        # accounts for all five as flaky rather than as final reds.
        assert len(result.flaky_ids) + len(result.final_red_ids) == 5

    def test_the_pool_skips_two_tests(self, result):
        assert len(result.final_skipped_ids) == 2

    def test_truncated_display_names_collide_in_the_real_pool(self, result):
        # 540 bare ids render more than once for reasons that are not retries;
        # collapsing them would erase real executions.
        assert len(result.duplicate_name_ids) == 540
