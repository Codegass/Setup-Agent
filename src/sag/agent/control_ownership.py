"""Typed ownership for blockers observed by the evidence/control layers.

Ownership answers who may act next.  It is deliberately independent from the
project outcome: a failed build can be project-owned, while a missing receipt
for that build is harness-owned and therefore takes precedence.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class BlockerOwner(str, Enum):
    NONE = "none"
    PROJECT = "project"
    HARNESS = "harness"
    UNKNOWN = "unknown"


_NO_BLOCKER_CODES = frozenset({"expectation_met"})
_HARNESS_CODE_PARTS = (
    "contract_binding",
    "contract_persist",
    "evidence_unpersisted",
    "integrity",
    "ledger_",
    "persistence",
    "receipt_persist",
    "receipt_unreadable",
    "replay_",
    "settlement_",
    "transport_",
)
_PROJECT_CODE_PREFIXES = (
    "compile_",
    "dependency_incompatible_",
    "falsifier_",
    "java_version_mismatch",
    "maven_extension_incompatible",
    "semantic_failure",
)
_PROJECT_CODES = frozenset({"expectation_unmet"})


def blocker_owner_for_assessment(code: Any) -> BlockerOwner:
    """Classify a typed assessment code without reading diagnostic prose.

    The mapping is intentionally conservative.  Only explicit internal
    transport/integrity vocabulary grants the controller ownership, and only
    runner/project semantic vocabulary grants project ownership.  A new code
    therefore starts as ``unknown`` instead of silently authorizing either
    side to act.
    """

    normalized = str(code or "").strip().lower()
    if not normalized:
        return BlockerOwner.UNKNOWN
    if normalized in _NO_BLOCKER_CODES:
        return BlockerOwner.NONE
    if any(part in normalized for part in _HARNESS_CODE_PARTS):
        return BlockerOwner.HARNESS
    if normalized in _PROJECT_CODES or normalized.startswith(_PROJECT_CODE_PREFIXES):
        return BlockerOwner.PROJECT
    return BlockerOwner.UNKNOWN


__all__ = ["BlockerOwner", "blocker_owner_for_assessment"]
