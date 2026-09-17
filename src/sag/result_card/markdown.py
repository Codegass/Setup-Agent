"""The card as the report's `## Result` section.

Same rows, same words as the terminal block; only the frame differs. Cell text
is escaped so a command containing a pipe cannot split a column.
"""

from __future__ import annotations

from sag.result_card.models import ResultRow, RunResultCard

#: The same sentence the terminal block prints on its ``Record`` line. One
#: fact, one wording, whichever surface a reader happens to be looking at.
_OLDER_RECORD = "reconstructed from an older run record"

#: What a row's items are, said in the row's own terms. A row not named here
#: takes its label, so a new row gets a heading without an edit here.
_ITEM_HEADINGS: dict[str, str] = {
    "task": "Required task steps",
    "ci": "Official CI findings",
}


def _cell(text: str | None) -> str:
    if not text:
        return "—"
    return text.replace("|", r"\|").replace("\n", " ")


def _detail(row: ResultRow) -> str:
    """Everything the row says beyond its status word, each thing said once.

    A headline that only repeats the status says the same thing twice, so the
    cell leads with the explanation instead — the rule the terminal block
    follows, so one card reads the same whichever surface shows it. The reason
    joins like any other part: the row builders already parenthesise the code
    inside it, and a second pair of parentheses only nests them.
    """

    parts = (
        None if row.headline == row.status else row.headline,
        row.detail,
        row.reason,
    )
    return " · ".join(part for part in parts if part)


def _items_heading(row: ResultRow) -> str:
    return _ITEM_HEADINGS.get(row.key, f"{row.label} details")


def render_result_card_markdown(card: RunResultCard) -> list[str]:
    """Return the section's lines, ending with exactly one blank line."""

    lines = ["## Result", ""]

    # A result rebuilt from an older run record is worth less than one read
    # from the run that wrote it, and the report outlives the terminal it was
    # printed beside, so the section says so where the terminal block says so.
    if card.verdict_source == "legacy":
        lines.extend([f"**Record** — {_OLDER_RECORD}", ""])

    lines.extend(["| | Status | Detail |", "|---|---|---|"])
    for row in card.rows:
        lines.append(f"| **{row.label}** | {_cell(row.status)} | {_cell(_detail(row))} |")
    lines.append("")

    # Each row's items under a heading that names the row. Loose in one list
    # they read as findings from nowhere, and the table above is the only thing
    # that could have said which measurement they belong to.
    for row in card.rows:
        if not row.items:
            continue
        lines.extend([f"### {_items_heading(row)}", ""])
        lines.extend(f"- {_cell(item)}" for item in row.items)
        lines.append("")

    if card.attention:
        lines.extend(["### Needs attention", ""])
        for item in card.attention:
            text = item.title if not item.detail else f"{item.title} — {item.detail}"
            lines.append(f"- {_cell(text)}")
        lines.append("")

    if card.notes:
        lines.extend(["### Data notes", ""])
        for note in card.notes:
            lines.append(f"- {_cell(note)}")
        lines.append("")

    return lines


__all__ = ["render_result_card_markdown"]
