"""Bounded match excerpts; complete grep output is archived separately."""

import re
import subprocess


def grep_line_page(
    lines,
    pattern,
    *,
    ignore_case=False,
    offset=0,
    column_offset=0,
    max_chars=20_000,
    regex_dialect="ere",
):
    """Page matching lines, locating long-line hits with grep's ERE dialect.

    The locator operates on returned text only; it never reads host files.
    Offsets within a line are Unicode character offsets, as in file_io.
    Short lines keep the usual grep presentation. Long lines expose each
    match's location and up to 1024 context characters on either side.
    """
    if offset < 0 or column_offset < 0 or max_chars < 1:
        raise ValueError("search offsets must be nonnegative; max_chars must be positive")
    remaining = min(max_chars, 100_000)
    rendered = []
    locations = []
    for line_index, line in enumerate(lines):
        cursor = column_offset if line_index == 0 else 0
        prefix_match = re.match(r"^(?:\d+:|.*?:\d+:)", line)
        prefix = prefix_match.group(0) if prefix_match else ""
        body = line[len(prefix) :]
        if len(line) <= 2048 and not cursor:
            if len(line) > remaining and rendered:
                return (
                    "\n".join(rendered),
                    {"offset": offset + line_index, "column_offset": 0},
                    locations,
                )
            if len(line) <= remaining:
                rendered.append(line)
                remaining -= len(line)
                continue
        if regex_dialect == "python":
            regex = re.compile(pattern, re.MULTILINE | (re.IGNORECASE if ignore_case else 0))
            hits = [(m.start(), m.end()) for m in regex.finditer(body) if m.start() >= cursor]
        else:
            command = ["grep", "-aEbo"] + (["-i"] if ignore_case else []) + ["-e", pattern or "."]
            try:
                found = subprocess.run(
                    command, input=(body + "\n").encode(), capture_output=True, timeout=10
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("matching-line excerpt locator timed out") from exc
            if found.returncode not in (0, 1):
                raise ValueError("matching-line excerpt locator failed")
            body_bytes = body.encode()
            hits = []
            previous_byte = previous_char = 0
            for record in found.stdout.splitlines():
                byte_start, _, value = record.partition(b":")
                if byte_start.isdigit():
                    byte_start = int(byte_start)
                    start = previous_char + len(
                        body_bytes[previous_byte:byte_start].decode("utf-8", errors="replace")
                    )
                    previous_byte, previous_char = byte_start, start
                    end = start + len(value.decode("utf-8", errors="replace"))
                    if start >= cursor:
                        hits.append((start, end))
        if not hits and not cursor:
            # Empty ERE matches (-o emits nothing) still have a matched line.
            hits = [(0, 0)]
        for start, end in hits:
            lo, hi = max(0, start - 1024), min(len(body), max(end, start + 1) + 1024)
            if hi - lo > remaining and rendered:
                return (
                    "\n".join(rendered),
                    {"offset": offset + line_index, "column_offset": start},
                    locations,
                )
            # A match itself can exceed the requested page budget. Clearly
            # label this as an excerpt; the original file/ref remains readable.
            if hi - lo > remaining:
                lo = max(0, start - remaining // 4)
                hi = min(len(body), lo + remaining)
            excerpt = body[lo:hi]
            rendered.append(
                f"{prefix}[match chars {start}:{end}; excerpt {lo}:{hi}/{len(body)}] {excerpt}"
            )
            locations.append(
                {
                    "matching_line_offset": offset + line_index,
                    "column_offset": start,
                    "match_end": end,
                    "excerpt_start": lo,
                    "excerpt_end": hi,
                }
            )
            remaining -= len(excerpt)
    return "\n".join(rendered), None, locations
