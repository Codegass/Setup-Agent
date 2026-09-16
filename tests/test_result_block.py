"""The end-of-run block: seven rows, aligned, with nothing said twice."""

from rich.console import Console

from sag.console.result_block import render_result_block
from sag.result_card.build import build_result_card

from result_card_fakes import module_metrics, snapshot_dict


def _plain(card, width: int = 78) -> str:
    """Render, then strip Rich markup the way a terminal would resolve it."""

    console = Console(width=width, force_terminal=False, no_color=True, soft_wrap=False)
    with console.capture() as capture:
        console.print(render_result_block(card, width=width), markup=True, highlight=False)
    return capture.get()


def _card(**kwargs):
    defaults = dict(
        module_metrics=module_metrics(),
        project="commons-cli",
        container="sag-commons-cli",
        commit="e17111798da51037659b3594d9c0b3b525040081",
        session_dir="logs/session_20260914_210609",
        turn_count=12,
        tool_calls=20,
        trajectory_session={"wall_clock_seconds": 390.2},
    )
    payload = kwargs.pop("snapshot", snapshot_dict())
    defaults.update(kwargs)
    return build_result_card(payload, **defaults)


def _older_record() -> dict:
    """A v3 payload, which the card reads as reconstructed rather than current."""

    payload = snapshot_dict(schema_version=3)
    payload.pop("rates")
    return payload


def test_block_opens_with_the_run_identity():
    text = _plain(_card())
    first = text.splitlines()[0]
    assert first.startswith("── commons-cli · e171117 · sag-commons-cli ")
    assert len(first) == 78


def test_every_row_appears_once_in_reading_order():
    lines = [line for line in _plain(_card()).splitlines() if line.startswith(" ")]
    labels = [line[1:15].strip() for line in lines if line[1:15].strip()]
    assert labels[:7] == [
        "Setup",
        "Required task",
        "Build",
        "Tests",
        "Coverage",
        "Official CI",
        "Report",
    ]


def test_headline_and_detail_are_stacked_under_one_label():
    text = _plain(_card())
    assert " Tests         executed       994 executed · 933 passed · 0 failed" in text
    assert "                              100% of non-skipped passed" in text


def test_a_reason_carries_its_code_in_parentheses():
    text = _plain(_card())
    assert (
        " Official CI   not compared   no CI job on this commit matches the run's JDK\n"
        "                              and OS (official_ci_cell_not_matched)\n"
    ) in text


def test_a_row_never_restates_its_status_as_its_headline():
    text = _plain(_card())
    assert " Coverage      not collected  not collected" not in text
    assert " Official CI   not compared   not compared" not in text
    # The third column carries the explanation instead, and carries it once.
    assert " Coverage      not collected  fixture coverage not collected\n" in text
    assert text.count("fixture coverage not collected") == 1


def test_task_steps_are_listed_under_their_row():
    text = _plain(_card())
    assert (
        "                              · smoke-build-test: complete — mvn clean verify\n"
        "                              → exit 0\n"
    ) in text


def test_a_reconstructed_result_says_so_and_a_current_one_does_not():
    assert " Record        reconstructed from an older run record\n" in _plain(
        _card(snapshot=_older_record())
    )
    assert " Record " not in _plain(_card())


def test_evidence_and_next_lines_close_the_block():
    text = _plain(_card())
    assert " Evidence      logs/session_20260914_210609\n" in text
    assert " Next          uv run sag ui · uv run sag result sag-commons-cli\n" in text


def test_a_long_container_name_wraps_under_its_label():
    text = _plain(_card(container="sag-advisor-high20-r2-terra-high-commons-cli-20260914"))
    assert (
        " Next          uv run sag ui · uv run sag result\n"
        "               sag-advisor-high20-r2-terra-high-commons-cli-20260914\n"
    ) in text


def test_a_clean_run_says_nothing_after_the_block():
    text = _plain(_card())
    assert "Setup verdict" not in text


def test_a_non_success_run_states_its_verdict_and_exit_code_once():
    text = _plain(_card(snapshot=snapshot_dict(verdict="partial")))
    assert text.count("Setup verdict: partial · exit 1") == 1


def test_attention_block_appears_only_when_there_is_something_to_do():
    metrics = module_metrics()
    metrics["modules"][0].update(
        {"tests_failed": 2, "failing_count": 2, "failing_names": ["a.T#one", "a.T#two"]}
    )
    text = _plain(_card(snapshot=snapshot_dict(verdict="partial"), module_metrics=metrics))
    assert " Needs attention" in text
    assert "commons-cli · 2 failing" in text
    assert "a.T#one, a.T#two" in text


def test_notes_are_listed_when_the_run_recorded_conflicts():
    text = _plain(
        _card(snapshot=snapshot_dict(verdict="partial", conflicts=["test_reports_stale"]))
    )
    assert " Notes" in text
    assert "some test reports were rewritten after being read and were set aside" in text


def test_no_line_exceeds_the_requested_width():
    for width in (78, 100, 120):
        for line in _plain(_card(), width=width).splitlines():
            assert len(line) <= width, (width, line)


def test_the_block_never_speaks_the_forbidden_vocabulary():
    text = _plain(_card(snapshot=snapshot_dict(verdict="partial"))).lower()
    for word in ("sealed", "canonical", "claimed", "quarantined", "metrics-v2", "promoting"):
        assert word not in text
