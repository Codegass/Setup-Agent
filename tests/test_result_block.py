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
        "                                → exit 0\n"
    ) in text


def test_a_wrapped_item_hangs_under_its_own_text():
    lines = _plain(_card()).splitlines()
    bullet = next(i for i, line in enumerate(lines) if line.lstrip().startswith("· smoke-"))
    continuation = lines[bullet + 1]
    indent = len(lines[bullet]) - len(lines[bullet].lstrip())
    # The bullet groups what follows it, rather than the continuation starting
    # under the glyph as if it were a new item.
    assert len(continuation) - len(continuation.lstrip()) == indent + 2
    assert continuation.strip() == "→ exit 0"


def test_a_reconstructed_result_says_so_and_a_current_one_does_not():
    assert " Record        reconstructed from an older run record\n" in _plain(
        _card(snapshot=_older_record())
    )
    assert " Record " not in _plain(_card())


def test_evidence_and_next_lines_close_the_block():
    text = _plain(_card())
    assert " Evidence      logs/session_20260914_210609\n" in text
    assert (
        " Next          uv run sag ui · uv run sag inspect sag-commons-cli --phase\n"
        "               build\n"
    ) in text


def _next_value(text: str) -> str | None:
    """The Next line's value, rejoined across the wrap; ``None`` when absent."""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line[1:15].strip() != "Next":
            continue
        parts = [line[15:].strip()]
        for follow in lines[index + 1 :]:
            if not follow.startswith(" " * 15):
                break
            parts.append(follow.strip())
        return " ".join(parts)
    return None


def test_the_next_line_names_a_command_that_takes_the_argument_the_card_carries():
    """`sag inspect` takes a container name; `sag trajectory` takes a session dir."""

    from sag.main import cli

    with_container = _plain(_card())
    only_session = _plain(_card(container=None))
    neither = _plain(_card(container=None, session_dir=None))

    assert _next_value(with_container) == (
        "uv run sag ui · uv run sag inspect sag-commons-cli --phase build"
    )
    assert _next_value(only_session) == (
        "uv run sag ui · uv run sag trajectory logs/session_20260914_210609"
    )
    # Neither argument means no command to print, rather than one missing its
    # argument.
    assert _next_value(neither) is None

    # Every command the block names is a command the CLI actually registers.
    assert {"ui", "inspect", "trajectory"} <= set(cli.commands)
    for text in (with_container, only_session, neither):
        assert "sag result" not in text


def test_a_long_container_name_wraps_under_its_label():
    text = _plain(_card(container="sag-advisor-high20-r2-terra-high-commons-cli-20260914"))
    assert (
        " Next          uv run sag ui · uv run sag inspect\n"
        "               sag-advisor-high20-r2-terra-high-commons-cli-20260914 --phase\n"
        "               build\n"
    ) in text


def test_a_long_container_name_leaves_the_rule_one_line_wide():
    lines = _plain(
        _card(container="sag-advisor-high20-r2-terra-high-commons-cli-20260914")
    ).splitlines()
    assert lines[0] == (
        "── commons-cli · e171117 · sag-advisor-high20-r2-terra-high-commons-cli-202… ─"
    )
    assert len(lines[0]) == 78
    # The rule is one line: the row block starts immediately after it, and the
    # project and the commit survive intact because the container gave way.
    assert lines[1].startswith(" Setup")


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


def test_a_value_longer_than_its_column_stays_inside_the_width_it_was_given():
    """No line the block emits is wider than the width the block was built to.

    `break_long_words=False` let a single unbreakable token — a campaign path,
    a full Maven command — run past the edge, producing an 82-column line on an
    80-column block. The console then re-wrapped that line at its own width
    with no indent, snapping the path in half against the left margin.
    """
    from rich.text import Text

    long_path = "logs/advisor-high20-mini-high-commons-cli-20260914/runs/commons-cli"
    # Measured on what the block RETURNS, not on what a console prints: a
    # console re-wraps an over-long line before anyone can see it was over-long,
    # which is exactly why this went unnoticed.
    block = render_result_block(_card(session_dir=long_path, container=None), width=80)

    over = [line for line in block.splitlines() if Text.from_markup(line).cell_len > 80]
    assert over == []
    assert long_path[:20] in block
