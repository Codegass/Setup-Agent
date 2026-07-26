# tests/test_no_exclusion_coaching.py
"""Source-level tripwire: no SAG tool may coach test exclusion or skipping
(spec §3.4-3). The strings below were live in the 2026-07-24 bigtop run."""

from pathlib import Path

BANNED = ["-Dtest=!", "-DskipTests=true", "-pl !"]
MODULES = [
    "src/sag/tools/internal/maven_tool.py",
    "src/sag/agent/tool_recovery.py",
]


def test_no_module_coaches_exclusions():
    for module in MODULES:
        source = Path(module).read_text()
        for banned in BANNED:
            assert banned not in source, f"{module} still contains {banned!r}"
