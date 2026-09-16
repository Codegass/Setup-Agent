"""The report's Result section is the same seven rows as the terminal block."""

import re

from sag.result_card.build import build_result_card
from sag.result_card.markdown import render_result_card_markdown

from result_card_fakes import RUN_ID, evaluated_ci_comparison, module_metrics, snapshot_dict


def _lines(**kwargs) -> list[str]:
    payload = kwargs.pop("snapshot", snapshot_dict())
    card = build_result_card(payload, module_metrics=module_metrics(), **kwargs)
    return render_result_card_markdown(card)


def test_section_opens_with_a_result_heading_and_a_table_head():
    lines = _lines()
    assert lines[0] == "## Result"
    assert lines[2] == "| | Status | Detail |"
    assert lines[3] == "|---|---|---|"


def test_every_row_is_one_table_row_in_order():
    labels = [line.split("|")[1].strip() for line in _lines() if line.startswith("| **")]
    assert labels == [
        "**Setup**",
        "**Required task**",
        "**Build**",
        "**Tests**",
        "**Coverage**",
        "**Official CI**",
        "**Report**",
    ]


def test_detail_and_reason_are_joined_in_the_third_column():
    # The reason joins like any other part. The row builder already names the
    # code inside it, so a second pair of parentheses would only nest them.
    row = next(line for line in _lines() if "**Official CI**" in line)
    assert row == (
        "| **Official CI** | not compared | no CI job on this commit matches "
        "the run's JDK and OS (official_ci_cell_not_matched) |"
    )


def test_no_row_restates_its_status_word_in_its_detail_cell():
    # What the status column says, the detail column does not say again.
    assert next(line for line in _lines() if "**Coverage**" in line) == (
        "| **Coverage** | not collected | fixture coverage not collected |"
    )
    # A headline saying more than the status word ("complete 1/1 steps") stays;
    # what is barred is a first part that is the status word and nothing else.
    for line in _lines():
        if not line.startswith("| **"):
            continue
        _, _, status, detail, _ = line.split("|")
        assert detail.strip().split(" · ")[0] != status.strip()


def test_pipes_inside_a_command_do_not_break_the_table():
    snapshot = snapshot_dict(
        task_completion={
            "run_id": RUN_ID,
            "task_sha256": "d" * 64,
            "status": "complete",
            "steps": [
                {
                    "id": "s1",
                    "command": "mvn test | tee out.log",
                    "status": "complete",
                    "receipt_id": "inv-1",
                    "exit_code": 0,
                    "reason": None,
                }
            ],
            "reasons": [],
        }
    )
    row = next(line for line in _lines(snapshot=snapshot) if "**Required task**" in line)
    # An escaped pipe is still a `|` character, so count the column separators:
    # every pipe that is not preceded by a backslash.
    assert len(re.findall(r"(?<!\\)\|", row)) == 4
    assert r"\|" in row
    # The same command listed as an item is escaped the same way.
    assert r"- s1: complete — mvn test \| tee out.log → exit 0" in _lines(snapshot=snapshot)


def test_attention_is_listed_under_its_own_heading():
    metrics = module_metrics()
    metrics["modules"][0].update(
        {"tests_failed": 1, "failing_count": 1, "failing_names": ["a.T#one"]}
    )
    lines = render_result_card_markdown(
        build_result_card(snapshot_dict(verdict="partial"), module_metrics=metrics)
    )
    assert "### Needs attention" in lines
    assert any(line.startswith("- commons-cli · 1 failing") for line in lines)


def test_no_attention_heading_when_nothing_needs_attention():
    assert "### Needs attention" not in _lines()


def test_notes_follow_the_table_when_present():
    lines = _lines(snapshot=snapshot_dict(verdict="partial", conflicts=["test_reports_stale"]))
    assert "### Data notes" in lines


def test_a_reconstructed_result_says_so_and_a_current_one_does_not():
    # The report outlives the run it describes, so the section discloses a
    # rebuilt record in the same words the terminal block uses.
    payload = snapshot_dict(schema_version=3)
    payload.pop("rates")
    assert _lines(snapshot=payload)[2] == "**Record** — reconstructed from an older run record"
    assert not any(line.startswith("**Record**") for line in _lines())


def test_a_rows_items_are_listed_under_a_heading_naming_that_row():
    # Loose in one list, an item reads as a finding from nowhere; the table is
    # the only thing that could have said which measurement it belongs to.
    lines = _lines()
    assert lines[lines.index("### Required task steps") + 2] == (
        "- smoke-build-test: complete — mvn clean verify → exit 0"
    )


def test_the_comparisons_findings_are_listed_under_their_own_heading():
    lines = _lines(
        snapshot=snapshot_dict(
            ci_comparison=evaluated_ci_comparison(
                verdict="not_met",
                clean=False,
                red_observed=3,
                unexpected_red_ids=["a.B#c"],
                reason_codes=["NEW_RED_BEYOND_TARGET"],
            )
        )
    )
    assert lines[lines.index("### Official CI findings") + 2] == (
        "- NEW_RED_BEYOND_TARGET: tests failed here that pass in CI"
    )


def test_a_row_with_no_items_gets_no_heading_of_its_own():
    assert "### Coverage details" not in _lines()
