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
receipt may widen it; a survey re-run may never demote it.

It deliberately parses nothing. Kotlin settings and imperative version checks
stay unparsed; pre-flight owns stated-requirement recovery and owns it well.

WHAT READS IT, honestly (spec §3.6 names three consumers; this is one).

The structure fact is persisted, protected from survey demotion, and read by
`module_coverage.module_basis` for ONE purpose: naming the receipt that stated
a denominator. It is deliberately NOT an input to the denominator itself. The
denominator's narrowing input (#17) is what THIS pass's receipts say they
ATTEMPTED, and a persisted structure is not that: a structure proved by an
earlier `mvn -pl core` would shrink this pass's expectation list to one module
and refine a partial build upward into a complete success — the exact P0-F
direction Plan 8 exists to forbid. The other two consumers §3.6 names, the
test-bearing module list and the domain graph, still read the survey's
proposal; wiring them is not in this pass, and claiming otherwise here would
be the same kind of untrue sentence the plan is about.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Callable, Dict, Mapping, Optional

from loguru import logger

from sag.runtime.container_io import read_container_text
from sag.runtime.paths import BUILD_REQUIREMENTS_PATH

# The manifest key the structure fact lives under. Additive: a manifest written
# before this design carries no such key and every reader degrades to the
# survey's proposal, which is exactly today's behaviour.
STRUCTURE_KEY = "module_structure"
STRUCTURE_SCHEMA_VERSION = 1

_STRUCTURE_HEREDOC = "SAG_STRUCTURE_EOF"

# How a dispatch ENDED, as the dispatch layer states it on the receipt.
# `finished` means the process wrote its OWN exit status
# (`/tmp/sag_jobs/<id>.log.exit`, atomically, by the launcher). `vanished` means
# the process is gone and `collect_detached_result` SYNTHESIZED exit_code 1 —
# a crashed or OOM-killed job, whose log stops wherever the kill landed. An
# absent lifecycle is the synchronous in-band return, which IS the process's own
# status; that keeps every receipt written before this design reading exactly as
# it did.
RECORDED_TERMINAL_LIFECYCLE = frozenset({"", "finished"})


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


def dispatch_terminated(receipt: Mapping[str, Any] | None) -> bool:
    """Did the dispatch this receipt describes actually END on its own?

    Terminality is a property of how the dispatch ended, NOT of the exit code's
    type. A detached job that vanished has a SYNTHESIZED exit code
    (docker_orch's `collect_detached_result`: `exit_code = 1` when the process
    is gone and no exit file exists) and a log truncated at the kill — Gradle
    prints `> Task :m:compileJava` incrementally, so a 300-module build
    OOM-killed at module 40 names exactly 40. A dispatch a timeout monitor
    stopped states its `termination_reason`, and its log is a prefix too.
    Neither has stated anything about the project (§3.2 settles the first kind
    later); "isinstance(exit_code, int)" could not tell them apart.
    """
    payload = receipt or {}
    exit_code = payload.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return False
    if str(payload.get("termination_reason") or "").strip():
        return False
    lifecycle = str(payload.get("lifecycle_state") or "").strip().lower()
    return lifecycle in RECORDED_TERMINAL_LIFECYCLE


def structure_from_receipt(receipt: Mapping[str, Any] | None) -> Optional[Dict[str, Any]]:
    """The structure fact a TERMINAL receipt proves, or None.

    Terminal means the dispatch ended and recorded its own status
    (`dispatch_terminated`). A job still in flight, one that crashed, and one a
    timeout killed have all stated nothing yet, and a structure is never
    guessed from a partial log. The exit CODE itself is irrelevant here: a
    reactor that failed still walked its modules and still named them.
    """
    payload = receipt or {}
    receipt_id = str(payload.get("receipt_id") or "").strip()
    if not receipt_id or not dispatch_terminated(payload):
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


def structure_updates(
    proven: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> bool:
    """May `incoming` replace the already-proven structure?

    §3.6 says a newer terminal receipt "may update the structure". Update means
    WIDEN or state something new — never narrow. `mvn -pl core` states what that
    dispatch ATTEMPTED; it does not state that the other 25 modules stopped
    existing, and replacing unconditionally let that narrow statement demote a
    full reactor's (a structure fact is read as a statement about the PROJECT).
    So a module list that is a subset of what is already proven — the equal case
    included, which is also the same-body no-op every evidence writer here
    follows — proves nothing new and the wider statement stands.

    A list that is neither subset nor superset is a receipt that walked modules
    the older one did not: it replaces, and its own id stays the provenance.
    Merging the two into a union was rejected deliberately — a union is a
    statement no single receipt ever made, and provenance would then name a
    receipt for a list it did not state.
    """
    new_keys = {key for key in (incoming or {}).get("keys") or () if key}
    if not new_keys:
        return False
    return not new_keys.issubset({key for key in (proven or {}).get("keys") or () if key})


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
        # A read-MODIFY-write of the survey's whole manifest is a machine
        # consumer, so it reads through the lossless path (`container_io`
        # exists because DockerOrchestrator strips and may truncate ordinary
        # output on its way to the model). An absent manifest is created; a
        # manifest whose body does not parse is NOT a manifest we may rewrite —
        # treating it as empty would replace every stated requirement with one
        # structure key.
        content = read_container_text(
            _ExecuteOnly(execute), BUILD_REQUIREMENTS_PATH, exact_bytes=True
        )
        if content is None:
            manifest: Dict[str, Any] = {}
        else:
            manifest = json.loads(content)
        if not isinstance(manifest, dict):
            return False
        if not structure_updates(read_module_structure(manifest), structure):
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


class _ExecuteOnly:
    """Adapter: `container_io` reads through an orchestrator, a receipt writer
    holds only its `execute` callable. Nothing else about the orchestrator is
    needed for one lossless read."""

    def __init__(self, execute: Callable[..., Optional[Mapping[str, Any]]]) -> None:
        self._execute = execute

    def execute_command(self, command: str, **kwargs: Any) -> Optional[Mapping[str, Any]]:
        return self._execute(command, **kwargs)
