"""The card as the report's `## Result` section.

Same rows, same words as the terminal block; only the frame differs. Cell text
is escaped so a command containing a pipe cannot split a column.
"""

from __future__ import annotations

from sag.result_card.models import RunResultCard

#: The same sentence the terminal block prints on its ``Record`` line. One
#: fact, one wording, whichever surface a reader happens to be looking at.
_OLDER_RECORD = "reconstructed from an older run record"


def _cell(text: str | None) -> str:
    if not text:
        return "—"
    return text.replace("|", r"\|").replace("\n", " ")


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
        detail = " · ".join(part for part in (row.headline, row.detail) if part)
        if row.reason:
            detail = f"{detail} ({row.reason})" if detail else row.reason
        lines.append(f"| **{row.label}** | {_cell(row.status)} | {_cell(detail)} |")
    lines.append("")

    for row in card.rows:
        for item in row.items:
            lines.append(f"- {_cell(item)}")
    if lines[-1] != "":
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
