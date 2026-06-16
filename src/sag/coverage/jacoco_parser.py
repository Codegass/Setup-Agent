"""Pure parser for JaCoCo jacoco.xml -> line/branch coverage totals.

Reads the REPORT-LEVEL counters (direct children of <report>); nested
package/class counters share the same type and must be ignored.
"""

from typing import Any, Dict
from xml.etree import ElementTree as ET


def _rate(covered: int, total: int):
    return round(100.0 * covered / total, 1) if total > 0 else None


def parse_jacoco_xml(content: str) -> Dict[str, Any]:
    """Parse a jacoco.xml string into line/branch totals. {} on failure."""
    if not content or not content.strip():
        return {}
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}
    if root.tag != "report":
        # Some writers wrap differently; find the first <report>.
        found = root.find(".//report")
        if found is None:
            return {}
        root = found

    totals = {"LINE": (0, 0), "BRANCH": (0, 0)}
    for counter in root.findall("counter"):  # direct children only
        ctype = counter.get("type")
        if ctype in totals:
            missed = int(counter.get("missed", "0"))
            covered = int(counter.get("covered", "0"))
            totals[ctype] = (covered, covered + missed)

    line_c, line_t = totals["LINE"]
    branch_c, branch_t = totals["BRANCH"]
    return {
        "line_covered": line_c, "line_total": line_t, "line_rate": _rate(line_c, line_t),
        "branch_covered": branch_c, "branch_total": branch_t,
        "branch_rate": _rate(branch_c, branch_t),
    }
