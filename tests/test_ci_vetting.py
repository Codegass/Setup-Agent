"""Laundering detection and cell matching over CI configs and cell identifiers."""

from pathlib import Path

import pytest

from sag.metrics.ci_vetting import (
    extract_cell_jdk,
    extract_cell_os,
    match_cell,
    vet_workflow_config,
)
from sag.metrics.target_record import CellTarget

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "target_attainment"
LAUNDERED_CI = FIXTURES / "laundered-ci.yml"


def _cell(cell_id):
    return CellTarget(
        cell_id=cell_id,
        build="ok",
        executed_count=1,
        executed_ids=("pkg.C#a",),
        red_count=0,
        grade="A",
    )


class TestVetWorkflowConfig:
    def test_a_clean_workflow_is_not_laundered(self):
        vet = vet_workflow_config("""
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: mvn -V test
            """)
        assert vet.laundered is False
        assert vet.locations == ()

    def test_job_level_continue_on_error_launders_the_job(self):
        vet = vet_workflow_config("""
            jobs:
              build:
                continue-on-error: true
                steps:
                  - run: echo hello
            """)
        assert vet.laundered is True
        assert vet.locations == ("job:build",)

    def test_a_job_level_flag_launders_even_without_a_build_step(self):
        vet = vet_workflow_config("""
            jobs:
              docs:
                continue-on-error: true
                steps:
                  - run: echo docs
            """)
        assert vet.locations == ("job:docs",)

    def test_a_step_is_located_by_its_index(self):
        vet = vet_workflow_config("""
            jobs:
              build:
                steps:
                  - uses: actions/checkout@v4
                  - run: echo setup
                  - run: mvn -V test
                    continue-on-error: true
            """)
        assert vet.locations == ("job:build/step:2",)

    @pytest.mark.parametrize(
        "run_text",
        [
            "mvn -V test",
            "./mvnw verify",
            "gradle build",
            "./gradlew check",
            "make check",
            "python -m pytest",  # the ' test' token does not fire here
        ],
    )
    def test_build_and_test_tokens_are_recognized(self, run_text):
        vet = vet_workflow_config(f"""
            jobs:
              build:
                steps:
                  - run: {run_text}
                    continue-on-error: true
            """)
        expected = run_text != "python -m pytest"
        assert vet.laundered is expected, run_text

    def test_a_leading_space_is_required_before_the_test_token(self):
        # "pytest" must not be read as a test step; "cargo test" must be.
        assert vet_workflow_config("""
            jobs:
              a:
                steps:
                  - run: cargo test
                    continue-on-error: true
            """).laundered is True

    def test_a_swallowed_non_build_step_is_not_laundering(self):
        vet = vet_workflow_config("""
            jobs:
              build:
                steps:
                  - run: rm -rf target
                    continue-on-error: true
                  - run: mvn -V test
            """)
        assert vet.laundered is False
        assert vet.locations == ()

    def test_a_falsey_continue_on_error_is_not_laundering(self):
        for value in ("false", "no", "off", "0"):
            vet = vet_workflow_config(f"""
                jobs:
                  build:
                    steps:
                      - run: mvn -V test
                        continue-on-error: {value}
                """)
            assert vet.laundered is False, value

    def test_an_expression_valued_flag_is_treated_as_truthy(self):
        vet = vet_workflow_config("""
            jobs:
              build:
                steps:
                  - run: mvn -V test
                    continue-on-error: ${{ matrix.experimental }}
            """)
        assert vet.laundered is True

    def test_a_uses_step_without_run_text_is_ignored(self):
        vet = vet_workflow_config("""
            jobs:
              build:
                steps:
                  - uses: some/test-action@v1
                    continue-on-error: true
            """)
        assert vet.laundered is False

    def test_a_workflow_without_jobs_is_clean(self):
        vet = vet_workflow_config("name: CI\non: [push]\n")
        assert vet.laundered is False
        assert vet.locations == ()

    def test_unparseable_yaml_is_disclosed_not_crashed(self):
        vet = vet_workflow_config("jobs:\n  - [unbalanced\n")
        assert vet.laundered is False
        assert vet.locations == ("unparseable",)

    def test_a_non_mapping_document_is_disclosed_as_unparseable(self):
        for text in ("", "just a scalar", "- one\n- two\n"):
            vet = vet_workflow_config(text)
            assert vet.laundered is False
            assert vet.locations == ("unparseable",)

    def test_the_vet_is_frozen(self):
        vet = vet_workflow_config("name: CI\n")
        with pytest.raises(Exception):
            vet.laundered = True


class TestLaunderedFixture:
    def test_the_fixture_flags_both_a_step_and_a_job(self):
        vet = vet_workflow_config(LAUNDERED_CI.read_text(encoding="utf-8"))
        assert vet.laundered is True
        assert vet.locations == ("job:build/step:2", "job:smoke")

    def test_the_fixture_is_labelled_synthetic_and_carries_no_absolute_path(self):
        text = LAUNDERED_CI.read_text(encoding="utf-8")
        assert "synthetic-modeled-on-real" in text
        assert "tomcat-jakartaee-migration" in text
        assert "/Users/" not in text


class TestExtractCellJdk:
    @pytest.mark.parametrize(
        ("cell_id", "expected"),
        [
            ("JDK11 windows-latest", 11),
            ("JDK17 ubuntu-latest", 17),
            ("JDK19-ea ubuntu-latest", 19),
            ("build (17, false)", 17),
            ("JUnit tests Java 25", 25),
            ("(8, ubuntu-latest)", 8),
            ("smoke (8, ubuntu-latest)", 8),
            ("jdk 21 / linux", 21),
            ("temurin-17", 17),
            ("build (ubuntu-22.04, 17)", 17),
            ("ubuntu-latest", None),
            ("build (ubuntu-latest, false)", None),
            ("", None),
            ("   ", None),
        ],
    )
    def test_the_jdk_major_is_read_from_the_cell_id(self, cell_id, expected):
        assert extract_cell_jdk(cell_id) == expected

    def test_a_major_outside_the_plausible_range_is_not_read(self):
        assert extract_cell_jdk("JDK1 ubuntu-latest") is None
        assert extract_cell_jdk("build (1, false)") is None

    def test_the_jdk_word_wins_over_a_bare_matrix_integer(self):
        assert extract_cell_jdk("JDK17 (8, ubuntu-latest)") == 17


class TestExtractCellOs:
    @pytest.mark.parametrize(
        ("cell_id", "expected"),
        [
            ("JDK17 ubuntu-latest", "linux"),
            ("build (17, linux)", "linux"),
            ("JDK17 macos-14", "macos"),
            ("JDK11 windows-latest", "windows"),
            ("build (17, false)", "unknown"),
            ("JDK21", "unknown"),
        ],
    )
    def test_the_platform_is_read_from_the_cell_id(self, cell_id, expected):
        assert extract_cell_os(cell_id) == expected


class TestMatchCell:
    def test_an_exact_linux_cell_matches_with_no_caveat(self):
        cells = (_cell("JDK17 ubuntu-latest"), _cell("JDK21 ubuntu-latest"))
        match = match_cell(cells, 17)
        assert match.cell_id == "JDK17 ubuntu-latest"
        assert match.exact is True
        assert match.caveat is None

    def test_an_exact_linux_cell_is_preferred_over_an_exact_windows_cell(self):
        cells = (_cell("JDK17 windows-latest"), _cell("JDK17 ubuntu-latest"))
        match = match_cell(cells, 17)
        assert match.cell_id == "JDK17 ubuntu-latest"
        assert match.caveat is None

    def test_an_unknown_platform_counts_as_linux_compatible(self):
        cells = (_cell("JDK17 windows-latest"), _cell("build (17, false)"))
        match = match_cell(cells, 17)
        assert match.cell_id == "build (17, false)"
        assert match.exact is True
        assert match.caveat is None

    def test_an_exact_windows_cell_matches_only_with_a_caveat(self):
        cells = (_cell("JDK17 windows-latest"),)
        match = match_cell(cells, 17)
        assert match.cell_id == "JDK17 windows-latest"
        assert match.exact is True
        assert match.caveat == "JDK17 is only proven on windows, not on linux"

    def test_a_linux_cell_is_never_displaced_by_a_windows_cell_of_the_same_jdk(self):
        cells = (
            _cell("JDK17 windows-latest"),
            _cell("JDK17 macos-14"),
            _cell("JDK17 ubuntu-latest"),
        )
        assert match_cell(cells, 17).cell_id == "JDK17 ubuntu-latest"

    def test_the_nearest_jdk_above_on_linux_is_preferred_over_one_below(self):
        cells = (_cell("JDK11 ubuntu-latest"), _cell("JDK21 ubuntu-latest"))
        match = match_cell(cells, 17)
        assert match.cell_id == "JDK21 ubuntu-latest"
        assert match.exact is False
        assert match.caveat == "no cell runs JDK17; matched the nearest above, JDK21"

    def test_the_closest_jdk_above_wins(self):
        cells = (
            _cell("JDK25 ubuntu-latest"),
            _cell("JDK21 ubuntu-latest"),
            _cell("JDK11 ubuntu-latest"),
        )
        assert match_cell(cells, 17).cell_id == "JDK21 ubuntu-latest"

    def test_the_nearest_below_is_used_when_nothing_runs_higher(self):
        cells = (_cell("JDK8 ubuntu-latest"), _cell("JDK11 ubuntu-latest"))
        match = match_cell(cells, 17)
        assert match.cell_id == "JDK11 ubuntu-latest"
        assert match.exact is False
        assert match.caveat == "no cell runs JDK17; matched the nearest below, JDK11"

    def test_an_inexact_windows_cell_is_never_substituted(self):
        cells = (_cell("JDK21 windows-latest"), _cell("JDK11 macos-14"))
        match = match_cell(cells, 17)
        assert match.cell_id is None
        assert match.exact is False
        assert "never substituted" in match.caveat

    def test_no_parseable_jdk_yields_no_match_and_a_caveat(self):
        cells = (_cell("ubuntu-latest"), _cell("windows-latest"))
        match = match_cell(cells, 17)
        assert match.cell_id is None
        assert match.exact is False
        assert match.caveat == "no harvested cell names a parseable JDK major"

    def test_a_tie_between_linux_cells_is_broken_deterministically(self):
        cells = (_cell("z JDK17 ubuntu-latest"), _cell("a JDK17 ubuntu-latest"))
        assert match_cell(cells, 17).cell_id == "a JDK17 ubuntu-latest"
        assert match_cell(tuple(reversed(cells)), 17).cell_id == "a JDK17 ubuntu-latest"

    def test_the_match_is_frozen(self):
        match = match_cell((_cell("JDK17 ubuntu-latest"),), 17)
        with pytest.raises(Exception):
            match.exact = False
