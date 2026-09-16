"""The report's Result section is the same seven rows as the terminal block."""

import re

from sag.result_card.build import build_result_card
from sag.result_card.markdown import render_result_card_markdown

from result_card_fakes import RUN_ID, module_metrics, snapshot_dict


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
    row = next(line for line in _lines() if "**Official CI**" in line)
    assert "not compared" in row
    assert "no CI job on this commit matches the run's JDK and OS" in row
    assert "official_ci_cell_not_matched" in row


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
