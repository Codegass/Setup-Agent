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

import json
import shlex
from typing import Any, Callable, Dict, Mapping, Optional

from loguru import logger

from sag.runtime.paths import BUILD_REQUIREMENTS_PATH

# The manifest key the structure fact lives under. Additive: a manifest written
# before this design carries no such key and every reader degrades to the
# survey's proposal, which is exactly today's behaviour.
STRUCTURE_KEY = "module_structure"
STRUCTURE_SCHEMA_VERSION = 1

_STRUCTURE_HEREDOC = "SAG_STRUCTURE_EOF"


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


def structure_from_receipt(receipt: Mapping[str, Any] | None) -> Optional[Dict[str, Any]]:
    """The structure fact a TERMINAL receipt proves, or None.

    Terminal means the dispatch reached an exit code. A job still in flight has
    stated nothing yet (§3.2 settles it later), and a structure is never
    guessed from a partial log. The exit CODE itself is irrelevant here: a
    reactor that failed still walked its modules and still named them.
    """
    payload = receipt or {}
    receipt_id = str(payload.get("receipt_id") or "").strip()
    exit_code = payload.get("exit_code")
    if not receipt_id or not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    modules: list[str] = []
    for entry in payload.get("module_outcomes") or ():
        name = str((entry or {}).get("module") or "").strip()
        if name and name not in modules:
            modules.append(name)
    if not modules:
        return None
    return {
        "schema_version": STRUCTURE_SCHEMA_VERSION,
        "provenance": receipt_id,
        "modules": modules,
        "keys": [key for key in (module_key(name) for name in modules) if key],
    }


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


def preserve_receipt_structure(
    incoming: Dict[str, Any],
    existing: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """A survey re-run may never demote a receipt-proven structure.

    The analyzer rewrites the whole manifest each time it runs, and a second
    survey has the same blind spot the first one had. Only a receipt may
    replace a receipt.
    """
    proven = read_module_structure(existing)
    if proven and not read_module_structure(incoming):
        incoming[STRUCTURE_KEY] = proven
    return incoming


def promote_structure(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt: Mapping[str, Any] | None,
) -> bool:
    """Persist what a terminal receipt proved. False when there is nothing to do.

    Never raises: the caller is mid-invocation and owes the model a result, and
    a manifest we could not update is a missing improvement, not a failure.
    """
    structure = structure_from_receipt(receipt)
    if not structure:
        return False
    try:
        current = execute(f"cat {shlex.quote(BUILD_REQUIREMENTS_PATH)} 2>/dev/null") or {}
        text = str(current.get("output") or "").strip()
        manifest = json.loads(text) if text.startswith("{") else {}
        if not isinstance(manifest, dict):
            return False
        if read_module_structure(manifest).get("provenance") == structure["provenance"]:
            # Same receipt, same body: the same-body-no-op convention every
            # other evidence writer in this project follows.
            return False
        manifest[STRUCTURE_KEY] = structure
        body = json.dumps(manifest, indent=2, sort_keys=True)
        temp = f"{BUILD_REQUIREMENTS_PATH}.tmp"
        result = (
            execute(
                f"cat > {shlex.quote(temp)} <<'{_STRUCTURE_HEREDOC}' && "
                f"mv -f {shlex.quote(temp)} {shlex.quote(BUILD_REQUIREMENTS_PATH)}\n"
                f"{body}\n{_STRUCTURE_HEREDOC}"
            )
            or {}
        )
        return bool(result.get("success") or result.get("exit_code") == 0)
    except Exception as exc:
        logger.debug(f"receipt-proven structure not persisted: {exc}")
        return False
