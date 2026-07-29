"""Receipt-proven module structure (Plan 8 spec §3.6).

The survey proposes what a project is made of; it cannot always read it. p7d
polaris (`session_20260729_111737_22356`): `settings.gradle.kts` registers its
26 subprojects imperatively, the survey parsed none of them, and the manifest
said `root_shape: single_module`, `build_islands: []`. Every guard keyed on
that map was disarmed by a guess.

A build that RAN states the same fact and states it terminally — Maven's
reactor summary, the modules whose tasks Gradle ran — and that statement rides
the invocation receipt already. This module is the provenance ladder the
project already has (doc claim < physical fact < receipt-proven), applied to
structure: once a terminal receipt names its modules, the structure is a
receipt-proven fact with the receipt id as its provenance. A newer terminal
receipt may restate it; a survey re-run may never demote it.

It deliberately parses nothing. Kotlin settings and imperative version checks
stay unparsed; pre-flight owns stated-requirement recovery and owns it well.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

# The manifest key the structure fact lives under. Additive: a manifest written
# before this design carries no such key and every reader degrades to the
# survey's proposal, which is exactly today's behaviour.
STRUCTURE_KEY = "module_structure"
STRUCTURE_SCHEMA_VERSION = 1


def module_key(value: str) -> str:
    """Comparable form of a module label: lowercase alphanumerics only.

    Maven prints `<name>` ("Apache Camel :: Core"), an expectation path carries
    the directory ("core"). Equality on the normalized tail is the only match
    this claims. One spelling for the receipt, the manifest and the coverage
    denominator, or they would silently stop matching each other.
    """
    tail = str(value or "").replace("\\", "/").rstrip("/").split("/")[-1]
    tail = tail.split("::")[-1]
    return "".join(ch for ch in tail.lower() if ch.isalnum())


def read_module_structure(requirements: Mapping[str, Any] | None) -> Dict[str, Any]:
    """The receipt-proven structure in a manifest, or {} when none is proven."""
    structure = (requirements or {}).get(STRUCTURE_KEY)
    if not isinstance(structure, Mapping):
        return {}
    provenance = str(structure.get("provenance") or "").strip()
    modules = [str(name) for name in (structure.get("modules") or ()) if str(name).strip()]
    if not provenance or not modules:
        return {}
    return {
        "schema_version": int(structure.get("schema_version") or STRUCTURE_SCHEMA_VERSION),
        "provenance": provenance,
        "modules": modules,
        "keys": [str(key) for key in (structure.get("keys") or ()) if str(key)]
        or [key for key in (module_key(name) for name in modules) if key],
    }
