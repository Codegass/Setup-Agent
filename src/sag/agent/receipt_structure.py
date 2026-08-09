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
from typing import Any, Callable, Dict, Mapping, Optional

from loguru import logger

from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    evidence_publication_authority_for,
    latest_publication_raw_sha256,
    publish_evidence_revision,
    verify_evidence_bytes,
    verify_latest_evidence_bytes,
)
from sag.runtime.container_io import read_container_text
from sag.runtime.paths import BUILD_REQUIREMENTS_PATH
from sag.utils.container_io import (
    WRITE_COMPARE_CONFLICT,
    compare_publish_container_text_atomic,
)

# The manifest key the structure fact lives under. Additive: a manifest written
# before this design carries no such key and every reader degrades to the
# survey's proposal, which is exactly today's behaviour.
STRUCTURE_KEY = "module_structure"
STRUCTURE_SCHEMA_VERSION = 2

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

    ONE GAP, KEPT KNOWINGLY, and stated here so the next reader need not
    re-derive it. A job the KERNEL killed (the OOM killer's SIGKILL) whose
    launcher wrapper still wrote the exit file reports `lifecycle_state:
    "finished"` with a signal-shaped code (137), and this predicate calls it
    terminal — so its truncated module list can prove structure and can set the
    coverage denominator. Excluding it would require inferring "was killed" from
    the NUMBER: 128+N is not a reserved range, build tools and shell wrappers
    return codes there for ordinary failures, and every receipt written before
    this design carries no lifecycle at all. Keying on the number's shape would
    therefore demote a large population of genuinely terminal dispatches on a
    guess about an integer, to close a narrow case where the launcher's own
    recorded status is the only physical fact available — and this project's
    provenance ladder says a recorded fact outranks an inference about one. So
    the trade is deliberate: the two cases where the status was SYNTHESIZED
    (`vanished`) or IMPOSED from outside (`termination_reason`) are refused,
    because there the receipt itself states that it was, and nothing is inferred.
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
    structure: Dict[str, Any] = {
        "schema_version": STRUCTURE_SCHEMA_VERSION,
        "provenance": receipt_id,
        "modules": modules,
        "keys": list(dict.fromkeys(key for key in (module_key(name) for name in modules) if key)),
    }
    for key in ("target_sha", "config_fingerprint"):
        value = str(payload.get(key) or "").strip()
        if value:
            structure[key] = value
    return structure


def read_module_structure(requirements: Mapping[str, Any] | None) -> Dict[str, Any]:
    """The receipt-proven structure in a manifest, or {} when none is proven."""
    structure = (requirements or {}).get(STRUCTURE_KEY)
    if not isinstance(structure, Mapping):
        return {}
    raw_version = structure.get("schema_version", 1)
    if (
        isinstance(raw_version, bool)
        or not isinstance(raw_version, int)
        or raw_version not in {1, STRUCTURE_SCHEMA_VERSION}
    ):
        # A future structure may assign different meaning to every field.  It
        # is unavailable evidence, not a best-effort v2 projection; malformed
        # versions likewise never escape through int(...) coercion.
        return {}
    provenance = str(structure.get("provenance") or "").strip()
    modules = [str(name) for name in (structure.get("modules") or ()) if str(name).strip()]
    if not provenance or not modules:
        return {}
    result = {
        "schema_version": raw_version,
        "provenance": provenance,
        "modules": modules,
        "keys": [str(key) for key in (structure.get("keys") or ()) if str(key)]
        or [key for key in (module_key(name) for name in modules) if key],
    }
    for key in ("target_sha", "config_fingerprint"):
        value = str(structure.get(key) or "").strip()
        if value:
            result[key] = value
    # Structure is a fact about one surveyed checkout/config pair.  Legacy
    # unpinned structures remain readable only beside an equally unpinned
    # survey; a partial pin in either direction cannot authorize reuse.
    return result if _matches_survey_identity(result, requirements) else {}


def _matches_survey_identity(
    structure: Mapping[str, Any],
    requirements: Mapping[str, Any] | None,
) -> bool:
    """Whether a receipt structure belongs to this exact surveyed tree.

    Pins are absent on both sides for legacy manifests/receipts, preserving
    their historical behavior.  Once either side states a target SHA or config
    fingerprint, equality is required in both directions: an unavailable pin
    cannot authorize carrying evidence across a survey boundary.
    """

    survey = (requirements or {}).get("survey")
    survey = survey if isinstance(survey, Mapping) else {}
    for key in ("target_sha", "config_fingerprint"):
        observed = str(structure.get(key) or "").strip()
        expected = str(survey.get(key) or "").strip()
        if observed != expected:
            return False
    return True


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
    if (
        proven
        and not read_module_structure(incoming)
        and _matches_survey_identity(proven, incoming)
    ):
        incoming[STRUCTURE_KEY] = proven
    return incoming


def structure_updates(
    proven: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> bool:
    """May `incoming` replace the already-proven structure?

    §3.6 (revised): "Only a terminal receipt whose statement is at least as wide
    may restate the structure." At least as wide means a SUPERSET of the proven
    keys, and nothing else:

    * a superset that adds modules — new knowledge, it replaces;
    * the same set — the same-body no-op every evidence writer here follows;
    * a subset (`mvn -pl core`) — that dispatch states what it ATTEMPTED, not
      that the other 25 modules stopped existing, and a structure fact is read as
      a statement about the PROJECT. The wider statement stands;
    * a DISJOINT or partly-overlapping list (`mvn -pl http` after a full
      reactor, or a second build island) — "not a subset" is not the same test as
      "at least as wide", and the earlier version's `not issubset` let exactly
      this narrow a three-module proven fact down to one. It stands too.

    Merging into a union was rejected deliberately: a union is a statement no
    single receipt ever made, and the provenance would then name a receipt for a
    list it did not state. So a receipt narrower than what is proven leaves the
    proven fact alone — losing a strictly-newer-but-narrower list is a missing
    improvement, while narrowing the persisted denominator is a wrong fact.
    """
    new_keys = {key for key in (incoming or {}).get("keys") or () if key}
    if not new_keys:
        return False
    proven_keys = {key for key in (proven or {}).get("keys") or () if key}
    return proven_keys.issubset(new_keys) and new_keys != proven_keys


def promote_structure(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt: Mapping[str, Any] | None,
) -> bool:
    """Persist what a terminal receipt proved. False when there is nothing to do.

    Never raises: the caller is mid-invocation and owes the model a result, and
    a manifest we could not update is a missing improvement, not a failure.

    §3.9 makes the read fail LOUDLY instead of reporting "absent" (which once
    let a settlement-time read failure replace the whole manifest with one
    structure key — the merged-lanes wipe). The failure lands in the except
    below: no write, promotion lost for THIS receipt. Accepted, and bounded:
    settlement is idempotent so the settled obligation never retries, but
    every LATER terminal receipt runs this same promotion, so the structure
    lands with the next real dispatch. Pinned by
    test_a_failed_promotion_is_recovered_by_the_next_terminal_receipt.
    """
    live_run_id = str((receipt or {}).get("run_id") or "").strip()
    if live_run_id:
        try:
            # Imported lazily: invocation_receipts owns this writer's caller
            # and imports receipt_structure during module initialization.
            from sag.agent.invocation_receipts import validate_receipt_v2

            validated_receipt = validate_receipt_v2(receipt or {})
            receipt_body = json.dumps(validated_receipt, sort_keys=True).encode("utf-8")
            receipt_check = verify_evidence_bytes(
                execute,
                record_kind="invocation_receipt",
                record_id=str(validated_receipt["receipt_id"]),
                raw=receipt_body,
                run_id=live_run_id,
                contract_id=validated_receipt.get("contract_id"),
                contract_hash=validated_receipt.get("contract_hash"),
            )
            if not receipt_check.authorized:
                logger.warning(
                    "receipt-proven structure refused: source receipt lacks "
                    "current host publication authority"
                )
                return False
            receipt = validated_receipt
        except (TypeError, ValueError):
            return False
    structure = structure_from_receipt(receipt)
    if not structure:
        return False
    for _attempt in range(3):
        authority = evidence_publication_authority_for(execute)
        publication_head = authority.latest_head(BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID)
        expected_publication = latest_publication_raw_sha256(
            execute, BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
        )
        try:
            # A read-MODIFY-write of the survey's whole manifest is a machine
            # consumer, so it reads through the lossless path (`container_io`
            # exists because DockerOrchestrator strips and may truncate ordinary
            # output on its way to the model). An absent or non-v1 manifest is
            # NOT a survey identity we may enrich: treating it as empty would
            # manufacture authority beside one structure key.
            content = read_container_text(
                _ExecuteOnly(execute), BUILD_REQUIREMENTS_PATH, exact_bytes=True
            )
            if live_run_id:
                if publication_head is None:
                    if content is not None:
                        logger.warning("receipt structure refused an unpublished base manifest")
                        return False
                elif publication_head.publication_state == "revoked":
                    if content is not None:
                        return False
                elif (
                    content is None
                    or not verify_latest_evidence_bytes(
                        execute,
                        record_kind="build_requirements",
                        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                        raw=content.encode("utf-8"),
                    ).authorized
                ):
                    logger.warning(
                        "receipt structure refused a base manifest that is not "
                        "the current host revision"
                    )
                    return False
            if content is None:
                # A structure is derived evidence about one surveyed manifest;
                # without a complete v1 survey body there is no target/config
                # identity to bind it to.  The receipt remains authoritative,
                # but it cannot manufacture a requirements artifact.
                return False
            else:
                manifest = json.loads(content)
            from sag.tools.internal.build_preflight import validate_build_requirements_v1

            try:
                manifest = validate_build_requirements_v1(manifest)
            except (TypeError, ValueError):
                return False
            if not _matches_survey_identity(structure, manifest):
                return False
            if not structure_updates(read_module_structure(manifest), structure):
                return False
            manifest[STRUCTURE_KEY] = structure
            try:
                # Promotion is a whole-body writer.  Revalidate after the
                # additive update so no already-authorized poison is laundered
                # into a fresh host head beside a valid structure object.
                manifest = validate_build_requirements_v1(manifest)
            except (TypeError, ValueError):
                return False
            body = json.dumps(manifest, indent=2, sort_keys=True)
            result = compare_publish_container_text_atomic(
                execute,
                BUILD_REQUIREMENTS_PATH,
                body,
                expected_content=content,
                validate_json=True,
            )
            if result.persisted:
                raw = body.encode("utf-8")
                publication = publish_evidence_revision(
                    execute,
                    record_kind="receipt_structure",
                    record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                    logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                    raw=raw,
                    expected_previous_raw_sha256=expected_publication,
                )
                if publication.published:
                    return True
                logger.warning(
                    "receipt-proven structure reached the container but host "
                    f"publication failed: {publication.status}"
                )
                return False
            if result.code == WRITE_COMPARE_CONFLICT:
                # Another whole-manifest writer won. Re-read its exact body and
                # re-apply the widening rule; a stale narrow receipt then
                # becomes a no-op instead of overwriting newer wide evidence.
                continue
            logger.warning(
                f"receipt-proven structure not persisted for "
                f"{str((receipt or {}).get('receipt_id') or 'unknown receipt')}: "
                f"{result.code}"
            )
            return False
        except Exception as exc:
            # Warning, not debug: a lost promotion is invisible in every other
            # channel, and the round-four review found the debug line was the only
            # trace a permanently-lost improvement left.
            logger.warning(
                f"receipt-proven structure not persisted for "
                f"{str((receipt or {}).get('receipt_id') or 'unknown receipt')}: {exc}"
            )
            return False
    logger.warning(
        f"receipt-proven structure not persisted for "
        f"{str((receipt or {}).get('receipt_id') or 'unknown receipt')}: compare retry exhausted"
    )
    return False


class _ExecuteOnly:
    """Adapter: `container_io` reads through an orchestrator, a receipt writer
    holds only its `execute` callable. Nothing else about the orchestrator is
    needed for one lossless read."""

    def __init__(self, execute: Callable[..., Optional[Mapping[str, Any]]]) -> None:
        self._execute = execute

    def execute_command(self, command: str, **kwargs: Any) -> Optional[Mapping[str, Any]]:
        return self._execute(command, **kwargs)
