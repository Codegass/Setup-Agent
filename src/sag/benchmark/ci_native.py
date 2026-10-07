"""Shared native CI inventory for import and portable verification."""

from collections import Counter
import re
from .native_evidence import ANSI, goal_name

GOAL_BANNER = re.compile(
    r"---\s+(?:[\w.\-]+:)?([\w.\-]+):([^\s:]+):([^\s:]+)\s+\(([^)]+)\)\s+@\s+([^\s]+)\s+---"
)


def ci_native_text(raw: str) -> str:
    """Remove only the fixed GitHub Actions transport timestamp, keeping lines."""
    return re.sub(
        r"^(?:\ufeff)?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ",
        "",
        ANSI.sub("", raw),
        flags=re.M,
    )


def ci_binding_inventory(raw: str) -> tuple[list[dict], list[dict]]:
    """Preserve nested evidence for review; only shared-reader top goals count."""
    from sag.benchmark.native_evidence import FORK, FORK_LINE, maven_events

    text = ci_native_text(raw)
    if re.findall(r"^\[INFO\]\s+BUILD (SUCCESS|FAILURE)\s*$", text, re.M) != ["SUCCESS"]:
        raise ValueError("Review requires one full successful native CI command")
    # Inventory identifies bindings, not successful test outcomes. A runner
    # can log an initial failed test attempt before a successful configured
    # retry. Preserve those bytes; grading still needs native + XML evidence.
    # A successful terminal must not conceal a truncated or mismatched fork.
    stack = []
    for marker in FORK_LINE.finditer(text):
        match = FORK.fullmatch(marker.group())
        if match is None or (match[1], match[6]) not in {(">>>", ">"), ("<<<", "<")}:
            raise ValueError("Forked Maven framing is malformed")
        identity = tuple(match[i] for i in (2, 3, 4, 5, 7, 8))
        if match[1] == ">>>":
            stack.append(identity)
        elif stack and stack[-1] == identity:
            stack.pop()
        else:
            raise ValueError("Forked Maven framing is unbalanced")
    if stack:
        raise ValueError("Forked Maven framing is incomplete")
    raw_rows, seen = [], Counter()
    for index, match in enumerate(GOAL_BANNER.finditer(text)):
        prefix, version, goal, execution, module = match.groups()
        name = goal_name(prefix, goal)
        key = (name, execution, module)
        raw_rows.append(
            {
                "goal": name,
                "version": version,
                "execution": execution,
                "module": module,
                "occurrence": seen[key],
                "position": index,
                "line": text[: match.start()].count("\n") + 1,
            }
        )
        seen[key] += 1
    by_line = {row["line"]: row for row in raw_rows}
    top = []
    for event in maven_events(text, terminal=True, serial=True):
        row = {
            **by_line[event["line"]],
            "position": event["position"],
            "occurrence": event["occurrence"],
        }
        top.append(row)
    if not top:
        raise ValueError("No CI execution bindings")
    top_lines = {row["line"] for row in top}
    return top, [row for row in raw_rows if row["line"] not in top_lines]
