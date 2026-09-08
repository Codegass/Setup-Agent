# src/sag/metrics/module_keys.py
"""The one grammar for a module name, on both sides of the CI comparison.

A canonical key is a directory-style path relative to the build root
(`connect/api`), with the root project spelled ``"."``.  Gradle project
paths (`:connect:api`, `:root`) translate into it; a Maven reactor display
name (what the Reactor Summary prints, and what SAG's Maven receipts record)
contains whitespace and passes through unchanged — it is the same string on
both sides already, and rewriting its punctuation would only break that.

This module is pure and depends on nothing in the package, so every other
metrics module may import it.
"""

from __future__ import annotations

from typing import Iterable

_ROOT_SPELLINGS = frozenset({":root", ":", ".", "./"})


def module_key(raw: object) -> str:
    """Return the canonical key for one module identity; refuse an empty one."""

    text = " ".join(str(raw if raw is not None else "").split())
    if not text:
        raise ValueError("module identity is empty")
    if " " in text:
        # A reactor display name.  Whitespace never appears in a Gradle project
        # path or a directory key, so this is the one reliable tell.
        return text
    if text in _ROOT_SPELLINGS:
        return "."
    text = text.lstrip(":").replace(":", "/")
    if text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    return text or "."


def module_keys(values: Iterable[object]) -> tuple[str, ...]:
    """Canonical keys of every identity, sorted and de-duplicated."""

    return tuple(sorted({module_key(value) for value in values}))


__all__ = ["module_key", "module_keys"]
