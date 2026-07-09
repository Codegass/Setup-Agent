"""Shared Python-environment helpers: requirement parsing, version resolution,
installer-ladder detection. Used by the analyzer, python_tool, and the setup
tool so the ladder exists exactly once (spec Components 1-3)."""

import re
from typing import List, Optional

SUPPORTED_PYTHONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13"]

_PYPROJECT_RP = re.compile(r'requires-python\s*=\s*["\']([^"\']+)["\']')
_SETUP_PY_RP = re.compile(r'python_requires\s*=\s*["\']([^"\']+)["\']')
_SETUP_CFG_RP = re.compile(r'^\s*python_requires\s*=\s*(.+)$', re.MULTILINE)


def requires_python_from_pyproject(content: str) -> Optional[str]:
    m = _PYPROJECT_RP.search(content or "")
    return m.group(1).strip() if m else None


def requires_python_from_setup_py(content: str) -> Optional[str]:
    m = _SETUP_PY_RP.search(content or "")
    return m.group(1).strip() if m else None


def requires_python_from_setup_cfg(content: str) -> Optional[str]:
    m = _SETUP_CFG_RP.search(content or "")
    return m.group(1).strip() if m else None


def _ver(v: str) -> tuple:
    return tuple(int(p) for p in v.split(".")[:2])


def _satisfies(candidate: str, spec: str) -> bool:
    """Minimal PEP-440 subset for major.minor candidates: >=, <=, ==, !=, ~=, <, >.
    Wildcards (3.9.*) compare on the major.minor prefix. Unknown syntax -> False
    (unresolvable is honest; the caller keeps the container default)."""
    spec = spec.strip()
    m = re.match(r"^(>=|<=|==|!=|~=|<|>)\s*(\d+(?:\.\d+)?)(?:\.\d+|\.\*)?$", spec)
    if not m:
        return False
    op, rhs = m.group(1), _ver(m.group(2))
    c = _ver(candidate)
    if op == ">=":
        return c >= rhs
    if op == "<=":
        return c <= rhs
    if op == "<":
        return c < rhs
    if op == ">":
        return c > rhs
    if op == "==":
        return c == rhs
    if op == "!=":
        return c != rhs
    if op == "~=":  # compatible release on major.minor: same as == at this granularity
        return c == rhs
    return False


def resolve_python_version(
    constraint: Optional[str], candidates: List[str] = SUPPORTED_PYTHONS
) -> Optional[str]:
    """Newest candidate satisfying EVERY comma-separated specifier, or None."""
    if not constraint or "${" in constraint:
        return None
    specs = [s for s in (p.strip() for p in constraint.split(",")) if s]
    if not specs:
        return None
    for candidate in reversed(candidates):
        if all(_satisfies(candidate, s) for s in specs):
            return candidate
    return None
