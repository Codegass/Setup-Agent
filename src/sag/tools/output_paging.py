"""Bounded, lossless pages for model-requested text reads."""

import json
from typing import TextIO


def read_text_page(
    source: TextIO,
    start_line: int = 0,
    end_line: int | None = None,
    column_offset: int = 0,
    max_chars: int = 20_000,
) -> dict:
    """Read a zero-based, end-exclusive range, including fragments of long lines.

    Keep this function self-contained: file_io also runs it next to the file
    in the container, so the transport never needs to load the whole file.
    """
    if start_line < 0 or column_offset < 0 or max_chars < 1:
        raise ValueError("start_line/column_offset must be nonnegative and max_chars positive")
    if end_line is not None and end_line < start_line:
        raise ValueError("end_line must be greater than or equal to start_line")
    max_chars = min(max_chars, 100_000)
    line = 0
    while line < start_line:
        chunk = source.readline(max_chars)
        if not chunk:
            break
        if chunk.endswith("\n"):
            line += 1
    column = 0
    while column < column_offset:
        chunk = source.readline(min(max_chars, column_offset - column))
        if not chunk or chunk.endswith("\n"):
            raise ValueError("column_offset is past the end of the starting line")
        column += len(chunk)
    parts = []
    remaining = max_chars
    eof = line < start_line
    while not eof and remaining and (end_line is None or line < end_line):
        chunk = source.readline(remaining)
        if not chunk:
            eof = True
            break
        parts.append(chunk)
        remaining -= len(chunk)
        if chunk.endswith("\n"):
            line += 1
            column = 0
        else:
            column += len(chunk)
    range_complete = end_line is not None and line >= end_line
    if not eof and not range_complete:
        eof = source.read(1) == ""
    complete = eof or range_complete
    return {
        "text": "".join(parts),
        "start_line": start_line,
        "end_line": end_line,
        "column_offset": column_offset,
        "max_chars": max_chars,
        "complete": complete,
        "eof": eof,
        "next": None if complete else {"start_line": line, "column_offset": column},
    }


def render_text_page(page: dict, read_args: dict) -> str:
    """Keep content unchanged and place continuation outside the returned text."""
    status = "Requested range complete." if page["complete"] else "Character limit reached."
    footer = f"{status} max_chars={page['max_chars']}; line offsets are zero-based."
    if page["next"] is not None:
        next_args = {
            **read_args,
            **page["next"],
            "max_chars": page["max_chars"],
        }
        if page["end_line"] is not None:
            next_args["end_line"] = page["end_line"]
        footer += "\nNext read: " + json.dumps(next_args, ensure_ascii=False)
    return page["text"] + "\n\n[" + footer + "]"
