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

#: `sag inspect` takes a container name and `sag trajectory` takes a session
#: directory. One template for both would name a real command with the wrong
#: kind of argument half the time, so each argument gets its own line.
_NEXT_CONTAINER = "uv run sag ui · uv run sag inspect {container} --phase build"
_NEXT_SESSION = "uv run sag ui · uv run sag trajectory {session_dir}"
_ITEM_BULLET = "· "
_OLDER_RECORD = "reconstructed from an older run record"
_ELLIPSIS = "…"

#: What the opening rule spends on itself: ``"── "``, the space closing the
#: title, and the one dash that keeps the rule a rule. The title gets the rest,
#: so the first line is always exactly ``width`` and always one line.
_RULE_CHROME = 5


def _rule(width: int, title: str | None = None) -> str:
    if not title:
        return "─" * width
    head = f"── {title} "
    return head + "─" * max(0, width - len(head))


def _elide(text: str, room: int) -> str:
    """Shorten to ``room``, marking the cut so a reader sees a name was shortened."""

    if room <= 0:
        return ""
    if len(text) <= room:
        return text
    return text[: room - 1] + _ELLIPSIS if room > 1 else _ELLIPSIS


def _identity(card: RunResultCard, room: int) -> str:
    """The run's name, fitted to ``room``.

    The project and the commit are what a reader recognises a run by, so the
    container is the segment that gives way. Nothing is lost by shortening it
    here: the Evidence and Next lines below print it in full.
    """

    named = " · ".join(
        part
        for part in (card.project, card.commit[:_COMMIT_CHARS] if card.commit else None)
        if part
    )
    if card.container:
        separator = " · " if named else ""
        container = _elide(card.container, room - len(named) - len(separator))
        if container:
            named = f"{named}{separator}{container}"
    return _elide(named or card.run_id, room)


def _wrap(text: str, width: int, indent: str, hanging: str | None = None) -> list[str]:
    # ``width`` is the room left for the text itself; ``textwrap`` measures the
    # whole line, indent included, so the indent is added back before wrapping.
    if width <= 0:
        return [indent + text]
    return textwrap.wrap(
        text,
        width=width + len(indent),
        initial_indent=indent,
        subsequent_indent=indent if hanging is None else hanging,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [indent.rstrip()]


def _labelled(label: str, value: str, width: int) -> list[str]:
    """A label in its column and a value that wraps under itself, not off the edge.

    Container names and session paths are as long as the run that made them, so
    these lines are wrapped like every other line rather than trusted to fit.
    """

    indent = " " * (GUTTER + LABEL_WIDTH)
    wrapped = _wrap(value, width - len(indent), indent)
    head = f"{' ' * GUTTER}{label:<{LABEL_WIDTH}}"
    return [head + wrapped[0][len(indent) :], *wrapped[1:]]


def render_result_block(card: RunResultCard, *, width: int = 78) -> str:
    """Render the card as the block a user reads when a run ends."""

    width = max(MIN_WIDTH, width)
    body_indent = " " * (GUTTER + LABEL_WIDTH + STATUS_WIDTH)
    body_width = width - len(body_indent)

    lines: list[str] = [_rule(width, _identity(card, width - _RULE_CHROME))]

    # A result rebuilt from an older run record is worth less than one read from
    # the run that wrote it, and looks identical to one unless the block says so.
    if card.verdict_source == "legacy":
        lines.extend(_labelled("Record", _OLDER_RECORD, width))

    for row in card.rows:
        style = _TONE_STYLE[row.tone]
        status = row.status[: STATUS_WIDTH - 1]
        head = (
            f"{' ' * GUTTER}{row.label:<{LABEL_WIDTH}}"
            f"[{style}]{status}[/{style}]{' ' * (STATUS_WIDTH - len(status))}"
        )

        # A headline that only repeats the status word says the same thing
        # twice, so the body column leads with the explanation instead.
        # Each entry carries the indent its continuation lines take: a bulleted
        # item hangs under its own text, so the bullet groups what follows it.
        item_indent = body_indent + " " * len(_ITEM_BULLET)
        body: list[tuple[str, str]] = [
            (text, body_indent)
            for text in (
                None if row.headline == row.status else row.headline,
                row.detail,
                row.reason,
            )
            if text
        ]
        body.extend((f"{_ITEM_BULLET}{item}", item_indent) for item in row.items[:_MAX_ITEMS])
        if len(row.items) > _MAX_ITEMS:
            body.append((f"{_ITEM_BULLET}+{len(row.items) - _MAX_ITEMS} more", item_indent))

        # One body column: everything the row says starts at the same edge.
        wrapped = [
            line for text, hanging in body for line in _wrap(text, body_width, body_indent, hanging)
        ]
        wrapped = wrapped or [body_indent]
        lines.append(head + wrapped[0][len(body_indent) :])
        lines.extend(wrapped[1:])

    lines.append(_rule(width))

    if card.attention:
        lines.append(f"{' ' * GUTTER}Needs attention")
        for item in card.attention:
            text = item.title if not item.detail else f"{item.title} — {item.detail}"
            lines.extend(_wrap(text, width - 3, "   "))

    if card.session_dir:
        lines.extend(_labelled("Evidence", card.session_dir, width))

    # A run with neither gets no Next line: a command printed without its
    # argument is not a command the reader can run.
    if card.container:
        lines.extend(_labelled("Next", _NEXT_CONTAINER.format(container=card.container), width))
    elif card.session_dir:
        lines.extend(_labelled("Next", _NEXT_SESSION.format(session_dir=card.session_dir), width))

    if card.notes:
        lines.append(f"{' ' * GUTTER}Notes")
        for note in card.notes:
            lines.extend(_wrap(note, width - 3, "   "))

    if card.verdict != "success":
        style = _TONE_STYLE[card.tone]
        lines.append(f"[{style}]Setup verdict: {card.verdict} · exit 1[/{style}]")

    return "\n".join(lines)


__all__ = ["render_result_block"]
