"""The end-of-run block: one card, seven rows, one screen.

Rich markup is used for the status word only. Everything else is plain text so
the block reads the same in a pipe, a log file and a terminal.
"""

from __future__ import annotations

import textwrap

from sag.result_card.models import RunResultCard, Tone

LABEL_WIDTH = 14
STATUS_WIDTH = 15
GUTTER = 1
MIN_WIDTH = 60
_COMMIT_CHARS = 7
_MAX_ITEMS = 12

_TONE_STYLE: dict[Tone, str] = {
    "success": "green",
    "attention": "yellow",
    "failed": "red",
    "neutral": "dim",
}

_NEXT_STEPS = "uv run sag ui        uv run sag result {target}"


def _rule(width: int, title: str | None = None) -> str:
    if not title:
        return "─" * width
    head = f"── {title} "
    return head + "─" * max(0, width - len(head))


def _identity(card: RunResultCard) -> str:
    parts = [
        card.project,
        card.commit[:_COMMIT_CHARS] if card.commit else None,
        card.container,
    ]
    return " · ".join(part for part in parts if part) or card.run_id


def _wrap(text: str, width: int, indent: str) -> list[str]:
    # ``width`` is the room left for the text itself; ``textwrap`` measures the
    # whole line, indent included, so the indent is added back before wrapping.
    if width <= 0:
        return [indent + text]
    return textwrap.wrap(
        text,
        width=width + len(indent),
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [indent.rstrip()]


def render_result_block(card: RunResultCard, *, width: int = 78) -> str:
    """Render the card as the block a user reads when a run ends."""

    width = max(MIN_WIDTH, width)
    body_indent = " " * (GUTTER + LABEL_WIDTH + STATUS_WIDTH)
    body_width = width - len(body_indent)
    # A row's prose — why it has nothing to measure, and the steps it lists —
    # is sentences rather than column values, so it hangs under the status
    # column, where a whole line of room is left for it. Keeping it clear of
    # the label column also keeps the labels the only thing written there.
    hang_indent = " " * (GUTTER + LABEL_WIDTH)
    hang_width = width - len(hang_indent)

    lines: list[str] = [_rule(width, _identity(card))]

    for row in card.rows:
        style = _TONE_STYLE[row.tone]
        status = row.status[: STATUS_WIDTH - 1]
        head = (
            f"{' ' * GUTTER}{row.label:<{LABEL_WIDTH}}"
            f"[{style}]{status}[/{style}]{' ' * (STATUS_WIDTH - len(status))}"
        )
        headline_lines = _wrap(row.headline, body_width, body_indent)
        lines.append(head + headline_lines[0][len(body_indent) :])
        lines.extend(headline_lines[1:])
        if row.detail:
            lines.extend(_wrap(row.detail, body_width, body_indent))
        if row.reason:
            lines.extend(_wrap(row.reason, hang_width, hang_indent))
        shown = row.items[:_MAX_ITEMS]
        for item in shown:
            lines.extend(_wrap(item, hang_width, hang_indent))
        if len(row.items) > _MAX_ITEMS:
            lines.append(f"{hang_indent}+{len(row.items) - _MAX_ITEMS} more")

    lines.append(_rule(width))

    if card.attention:
        lines.append(f"{' ' * GUTTER}Needs attention")
        for item in card.attention:
            text = item.title if not item.detail else f"{item.title} — {item.detail}"
            lines.extend(_wrap(text, width - 3, "   "))

    if card.session_dir:
        lines.append(f"{' ' * GUTTER}{'Evidence':<{LABEL_WIDTH}}{card.session_dir}")

    target = card.container or card.session_dir
    if target:
        lines.append(
            f"{' ' * GUTTER}{'Next':<{LABEL_WIDTH}}{_NEXT_STEPS.format(target=target)}"
        )

    if card.notes:
        lines.append(f"{' ' * GUTTER}Notes")
        for note in card.notes:
            lines.extend(_wrap(note, width - 3, "   "))

    if card.verdict != "success":
        style = _TONE_STYLE[card.tone]
        lines.append(f"[{style}]Setup verdict: {card.verdict} · exit 1[/{style}]")

    return "\n".join(lines)


__all__ = ["render_result_block"]
