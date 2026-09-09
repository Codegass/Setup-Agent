"""Current-run certificate adapter and the sealed official-CI comparison.

The input is host-authorized execution evidence, never a hand-authored
CertificateView. Targets are supplied before the run; this module has no
network client and never chooses a different CI cell at evidence close.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections import Counter, deque
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sag.agent.control_events import canonical_sha256
from sag.agent.java_success_certificates import (
    CertificateBlocker,
    JavaCertificateInput,
    JavaSuccessCertificate,
    ReceiptBoundTestResults,
    ScopeClaim,
    ScopeSubject,
    TestCounts,
    TypedObligationSet,
    evaluate_java_success_certificate,
    receipt_bound_test_counts,
    scope_subject_sha256,
)
from sag.metrics.attainment import AttainmentResult, evaluate_attainment, view_from_certificate
from sag.metrics.module_keys import module_key
from sag.metrics.target_record import TargetRecord


def repository_identity(url: str | None) -> str | None:
    """Keep repository provenance independent of the comparison target."""
    text = str(url or "").strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text.split(":", 1)[1]
    if "://" in text:
        parsed = urlparse(text)
        if parsed.hostname != "github.com":
            return None
        text = parsed.path.strip("/")
    text = text.removesuffix(".git")
    parts = text.split("/")
    return text if len(parts) == 2 and all(parts) else None


class PinnedCITarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record: TargetRecord
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def load_ci_target(path: str | Path) -> PinnedCITarget:
    source = Path(path)
    if source.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("CI target exceeds the 4 MiB input bound")
    raw = source.read_bytes()
    return PinnedCITarget(
        record=TargetRecord.model_validate_json(raw),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


class CIComparisonSnapshot(BaseModel):
    """One additive result inside the host-published verdict snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    status: Literal["evaluated", "no_target", "no_matched_cell", "unavailable"]
    run_id: str = Field(min_length=1, max_length=256)
    repo: str | None = None
    target_sha: str | None = None
    target_record_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    certificate_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    certificate: JavaSuccessCertificate | None = None
    attainment: AttainmentResult | None = None
    receipt_ids: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent_subject(self):
        if self.certificate is not None:
            if (self.certificate.run_id, self.certificate.target_sha) != (
                self.run_id,
                self.target_sha,
            ) or self.certificate_input_sha256 is None:
                raise ValueError("CI certificate does not bind this run and checkout")
        if self.status == "evaluated":
            if self.certificate is None or self.attainment is None or not self.target_record_sha256:
                raise ValueError("evaluated CI comparison requires bound certificate and target")
        elif self.attainment is not None:
            raise ValueError("unavailable CI comparison cannot carry a scored result")
        if self.status == "no_target" and self.target_record_sha256 is not None:
            raise ValueError("no-target comparison cannot carry a target digest")
        return self


def _accepted_plan(orchestrator, state):
    from sag.agent.phase_gates import claim_identity
    from sag.agent.project_execution_plan import read_sealed_project_execution_plan

    for record in reversed(state.phase_records):
        if record.phase != "analyze":
            continue
        claim = record.claim
        if record.transition != "advance" or claim is None or claim.signal != "done":
            return None
        artifact = read_sealed_project_execution_plan(orchestrator)
        if artifact is not None and (
            artifact.source_attempt_id,
            artifact.authored_plan_sha256,
            artifact.claim_sha256,
        ) == (record.attempt_id, claim.execution_plan_sha256, claim_identity(claim)):
            return artifact
        return None
    return None


def _call_key(tool, params):
    return str(tool), json.dumps(
        {key: value for key, value in params.items() if key != "timeout"},
        sort_keys=True,
        separators=(",", ":"),
    )


def _assessed(receipt, assessments):
    from sag.agent.evidence_assessments import ASSESSMENT_BUNDLE_COMPLETE, FINGERPRINT_KEYS

    fingerprints = {
        key: str(receipt[key]).strip()
        for key in FINGERPRINT_KEYS
        if receipt.get(key) is not None and str(receipt[key]).strip()
    }
    return any(
        item.get("receipt_id") == receipt["receipt_id"]
        and item.get("typed_code") == ASSESSMENT_BUNDLE_COMPLETE
        and item.get("scope") == receipt.get("output_content_hash")
        and item.get("fingerprints") == fingerprints
        for item in assessments
    )


def _current_test_counts(receipt, parsed, *, run_id, target_sha):
    """Read an existing totals tier, or complete current Maven XML rows.

    Maven has no Gradle suite-total section. Fresh totals are computed before
    identity caps, over the same complete hash-verified XML read. The bounded
    identity sample itself never becomes a complete count.
    The caller has verified publication, frozen contract, and assessment binding.
    """
    from sag.agent.java_success_certificates import _chain_intact
    from sag.agent.receipt_test_rows import rows_were_bounded

    if not parsed or parsed.get("status") != "complete" or parsed.get("reasons"):
        return None
    bound = receipt_bound_test_counts(receipt, run_id=run_id, target_sha=target_sha)
    if bound is not None:
        return bound if bound.counts_complete else None
    if (
        receipt.get("tool") != "maven"
        or _chain_intact(receipt, run_id=run_id, target_sha=target_sha) is None
    ):
        return None
    fresh_totals = parsed.get("execution_totals")
    if fresh_totals is not None:
        counts = TestCounts.model_validate(fresh_totals)
    else:
        if rows_were_bounded(parsed):
            return None
        outcomes = Counter(row["outcome"] for row in parsed["rows"])
        counts = TestCounts(
            reported=sum(outcomes.values()),
            passed=outcomes["passed"],
            failed=outcomes["failed"],
            errors=outcomes["error"],
            skipped=outcomes["skipped"],
        )
    return ReceiptBoundTestResults(
        counts=counts,
        test_results_authority="receipt_bound",
        receipt_ids=(receipt["receipt_id"],),
        counts_complete=True,
    )


def _single_maven_module(orchestrator, receipt, project_root):
    """A root-only current POM graph can name '.', independently of a CI cell.

    This is used only when a successful single-project Maven invocation has no
    Reactor Summary. Existing receipt module rows do not depend on this probe.
    """
    from sag.agent.forced_build_graph import verify_forced_candidate_build_graph
    from sag.agent.invocation_receipts import _succeeded
    from sag.runtime.container_io import resolve_control_execute

    if (
        receipt.get("tool") != "maven"
        or receipt.get("actual_cwd", receipt.get("working_directory")) != project_root
    ):
        return False
    execute = resolve_control_execute(orchestrator)
    if not callable(execute):
        return False
    unchanged = (
        execute(
            f"git -C {shlex.quote(project_root)} diff --quiet HEAD -- "
            "pom.xml .mvn/maven.config .mvn/extensions.xml"
        )
        or {}
    )
    if not _succeeded(unchanged) or unchanged.get("dispatch_status"):
        return False
    graph = verify_forced_candidate_build_graph(
        orchestrator, project_root=project_root, candidate_root=project_root, system="maven"
    )
    return graph.status == "verified" and graph.visited_roots == (project_root,)


def _certificate_input(orchestrator, state, validator, project_root, repo):
    """Adapt one accepted finite plan and the receipts proving its exact calls."""
    from sag.agent.attempt_policy import resolve_current_build_receipt_scope
    from sag.agent.receipt_structure import dispatch_terminated, maven_module_identity_ambiguous
    from sag.agent.receipt_test_rows import read_delta_testcase_rows
    from sag.runtime.container_io import resolve_control_execute

    scope_pin = resolve_current_build_receipt_scope(
        orchestrator,
        run_id=state.run_id,
        workspace_root=validator.project_path,
        project_root=project_root,
    )
    if not scope_pin.available:
        raise ValueError("current_run_checkout_unavailable")
    from sag.agent.invocation_receipts import target_sha as read_target_sha

    execute = resolve_control_execute(orchestrator)
    if not callable(execute) or read_target_sha(execute, project_root) != scope_pin.target_sha:
        raise ValueError("current_checkout_differs_from_run_pin")
    artifact = _accepted_plan(orchestrator, state)
    if artifact is None:
        raise ValueError("accepted_execution_plan_unavailable")
    receipts = validator._current_scoped_receipts(project_root)
    assessments = validator._read_live_evidence_assessments()
    if receipts is None or assessments is None:
        raise ValueError("current_receipt_or_assessment_publication_unavailable")
    receipts = [
        item
        for item in receipts
        if item.get("tool") in {"maven", "gradle"}
        and item.get("run_id") == state.run_id
        and item.get("target_sha") == scope_pin.target_sha
    ]
    subject = ScopeSubject(
        project_id=repo or project_root,
        run_id=state.run_id,
        target_sha=scope_pin.target_sha,
        plan_sha256=artifact.authored_plan_sha256,
    )
    scope = ScopeClaim(
        scope_id="plan:" + artifact.authored_plan_sha256[:32],
        revision=1,
        evidence_epoch=state.run_id,
        subject=subject,
        kind="declared_entrypoint",
        closure="closed",
        basis_refs=(artifact.artifact_sha256,),
    )

    def obligations(unit, required, satisfied=(), failed=(), *, applicability="required", red=()):
        return TypedObligationSet(
            unit=unit,
            basis_ref=artifact.artifact_sha256,
            subject_sha256=scope_subject_sha256(subject),
            scope_id=scope.scope_id,
            scope_revision=scope.revision,
            evidence_epoch=state.run_id,
            applicability=applicability,
            required_ids=tuple(required),
            satisfied_ids=tuple(satisfied),
            failed_ids=tuple(failed),
            product_red_ids=tuple(red),
            evidence_refs=tuple(sorted(evidence)),
        )

    # Contracts are read through the same live binding predicate used by the
    # execution-completion validator. A later different/narrower call has a
    # different key and cannot discharge the original plan step.
    planned_calls = Counter(
        _call_key(step.tool, step.params)
        for step in (*artifact.plan.build_steps, *artifact.plan.test_steps)
    )
    matched = {}
    for receipt in sorted(receipts, key=validator._receipt_sequence):
        contract = validator._test_execution_contract(receipt)
        if contract is None or not _assessed(receipt, assessments):
            continue
        call = contract["requested_call"]
        key = _call_key(call["tool"], call["params"])
        matched.setdefault(key, deque(maxlen=planned_calls.get(key, 0))).append(receipt)

    evidence = {artifact.artifact_sha256}
    required_evidence = {artifact.artifact_sha256}
    build_required, build_done, build_failed = [], [], []
    test_required, test_done, test_red = [], [], []
    modules, succeeded_modules, failed_modules = set(), set(), set()
    selected_tests, selected_receipts, selected_counts, selected_rows = [], {}, {}, []
    blockers = []

    def block(code, affects, detail):
        blockers.append(
            CertificateBlocker(code=code, affects=affects, owner="harness", reason=detail)
        )

    for lane, steps in (("build", artifact.plan.build_steps), ("test", artifact.plan.test_steps)):
        for index, step in enumerate(steps):
            step_id = f"{lane}:{index + 1}"
            required_evidence.add(step_id)
            required = build_required if lane == "build" else test_required
            required.append(step_id)
            candidates = matched.get(_call_key(step.tool, step.params))
            if not candidates:
                continue
            receipt = candidates.popleft()
            evidence.add(step_id)
            evidence.add(receipt["receipt_id"])
            required_evidence.add(receipt["receipt_id"])
            selected_receipts[receipt["receipt_id"]] = receipt
            codes = {
                a.get("typed_code")
                for a in assessments
                if a.get("receipt_id") == receipt["receipt_id"]
            }
            terminal = (
                dispatch_terminated(receipt) and 0 <= int(receipt.get("exit_code", 255)) < 128
            )
            if lane == "build":
                if terminal and receipt.get("exit_code") == 0 and "expectation_met" in codes:
                    build_done.append(step_id)
                elif terminal and receipt.get("exit_code") != 0:
                    build_failed.append(step_id)
            else:
                parsed = (
                    read_delta_testcase_rows(
                        execute, receipt_id=receipt["receipt_id"], delta=receipt["report_delta"]
                    )
                    if callable(execute)
                    else None
                )
                bound = _current_test_counts(
                    receipt, parsed, run_id=state.run_id, target_sha=scope_pin.target_sha
                )
                if bound is None:
                    block(
                        "CURRENT_TEST_REPORTS_UNAVAILABLE",
                        ("test_execution", "test_outcome"),
                        "Current claimed report hashes, format or complete totals could not be verified.",
                    )
                    continue
                selected_tests.append(receipt)
                selected_counts[receipt["receipt_id"]] = bound.counts
                selected_rows.append(parsed)
                if not bound.counts.reported or bound.counts.reported == bound.counts.skipped:
                    block(
                        "PLAN_TEST_BODIES_NOT_EXECUTED",
                        ("test_execution",),
                        "The selected task has no non-skipped test execution.",
                    )
                if terminal and (
                    (receipt.get("exit_code") == 0 and "expectation_met" in codes)
                    or (
                        bound.counts.red
                        and "expectation_unmet" in codes
                        and "test_failure_exit" in codes
                    )
                ):
                    (test_red if bound.counts.red else test_done).append(step_id)
            if maven_module_identity_ambiguous(receipt) or any(
                item.get("field") == "module_outcomes"
                for item in receipt.get("evidence_omissions", ())
            ):
                block(
                    "BUILD_MODULE_SCOPE_UNAVAILABLE",
                    ("build",),
                    "A selected receipt discloses ambiguous or missing module scope.",
                )
                continue
            outcomes = receipt.get("module_outcomes", ())
            if (
                not outcomes
                and terminal
                and receipt.get("exit_code") == 0
                and "expectation_met" in codes
                and _single_maven_module(orchestrator, receipt, project_root)
            ):
                outcomes = ({"module": ".", "status": "success"},)
            for row in outcomes:
                name = module_key(row["module"])
                modules.add(name)
                if terminal and row.get("status", "").lower() == "success":
                    succeeded_modules.add(name)
                elif row.get("status", "").lower() == "failure":
                    failed_modules.add(name)

    # Observed module rows are the scope of the selected invocations, not a
    # repository census. The separate CI universe determines any wider gap.
    failed_modules -= succeeded_modules
    bound_counts = None
    for counts in selected_counts.values():
        bound_counts = counts if bound_counts is None else bound_counts + counts
    if len(selected_tests) != len(test_required):
        block(
            "PLAN_TEST_EVIDENCE_INCOMPLETE",
            ("test_execution",),
            "Not every selected test step has a complete current report proof.",
        )
    summary = validator._test_execution_receipt_summary(project_root)
    if summary.get("state") != "completed":
        block(
            "TEST_EXECUTION_NOT_COMPLETE",
            ("test_execution",),
            str(summary.get("reason") or "Current test tasks did not complete.")[:2000],
        )
    if not modules:
        block(
            "BUILD_MODULE_SCOPE_UNAVAILABLE",
            ("build",),
            "No selected invocation proves its module universe.",
        )
    # Keep product-red obligation witnesses even if runtime completion is
    # independently blocked; counts state project outcomes, not completion.
    red_targets = tuple(
        f"target:{receipt['receipt_id']}"
        for receipt in selected_tests
        if selected_counts[receipt["receipt_id"]].red
    )
    target_ids = tuple(f"target:{receipt['receipt_id']}" for receipt in selected_tests)
    unique_blockers = {item.code: item for item in blockers}
    payload = JavaCertificateInput(
        assurance_level="sealed_lineage",
        scope=scope,
        build_steps=obligations(
            "build_plan_step",
            build_required,
            build_done,
            build_failed,
            applicability="required" if build_required else "unknown",
        ),
        build_units=obligations(
            "maven_reactor_module",
            sorted(modules),
            sorted(succeeded_modules),
            sorted(failed_modules),
            applicability="required" if modules else "unknown",
        ),
        test_steps=obligations(
            "test_plan_step",
            test_required,
            test_done,
            test_red,
            red=test_red,
            applicability="required" if test_required else "unknown",
        ),
        test_targets=obligations(
            "test_module",
            target_ids,
            [item for item in target_ids if item not in red_targets],
            red_targets,
            applicability="required" if target_ids else "unknown",
            red=red_targets,
        ),
        evidence_items=obligations(
            "evidence_binding", sorted(required_evidence), sorted(evidence & required_evidence)
        ),
        test_results_authority="receipt_bound" if bound_counts else "unavailable",
        test_counts=bound_counts,
        blockers=tuple(unique_blockers.values()),
        diagnostics=(
            (
                {
                    "code": "CI_TEST_SCOPE_OVERLAP_UNRESOLVED",
                    "observations": {"test_invocations": len(selected_tests)},
                    "note": "Counts sum receipt executions; overlap between multiple test invocation pools has not been proven.",
                },
            )
            if len(selected_tests) > 1
            else ()
        ),
    )
    return (
        payload,
        tuple(selected_receipts),
        tuple(str(item["argv"]) for item in selected_receipts.values()),
        selected_rows,
    )


def _same_pool_identities(parsed_reads, counts, cell):
    """Map complete fresh raw JUnit identities only by exact target membership.

    Identity evidence never invents aliases or deduplicates executions. Once a
    target names its universe, an incomplete or different local pool cannot
    fall back to a more flattering count-only comparison.
    """
    from sag.agent.receipt_test_rows import rows_were_bounded

    identity_needed_for_red = (
        counts is not None and counts.red > 0 and (cell.red_count > 0 or cell.flaky_count > 0)
    )
    if not cell.executed_ids:
        return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE" if identity_needed_for_red else None
    if identity_needed_for_red and (
        len(cell.red_ids) != cell.red_count or len(cell.flaky_ids) != cell.flaky_count
    ):
        return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE"
    executed, red = [], []
    for parsed in parsed_reads:
        if parsed.get("status") != "complete" or parsed.get("reasons") or rows_were_bounded(parsed):
            return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE"
        for row in parsed.get("rows", ()):
            classname, name = row.get("classname"), row.get("name")
            if (
                not isinstance(classname, str)
                or not isinstance(name, str)
                or not classname
                or not name
            ):
                return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE"
            identity = classname + "#" + name
            if identity not in cell.executed_ids or identity in executed:
                return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE"
            executed.append(identity)
            if row.get("outcome") in {"failed", "error"}:
                red.append(identity)
    if (
        counts is None
        or len(executed) != counts.reported
        or len(red) != counts.red
        or set(executed) != set(cell.executed_ids)
    ):
        return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE"
    return tuple(executed), tuple(red), None


def build_ci_comparison(orchestrator, state, *, validator, project_root, repository, target=None):
    repo = repository_identity(repository)
    base = {
        "run_id": state.run_id,
        "repo": repo,
        "target_record_sha256": target.raw_sha256 if target else None,
    }
    if validator is None or not project_root:
        return CIComparisonSnapshot(
            **base,
            status="no_target" if target is None else "unavailable",
            reasons=("certificate_adapter_unavailable",),
        )
    try:
        payload, receipt_ids, commands, parsed_reads = _certificate_input(
            orchestrator, state, validator, project_root, repo
        )
        certificate = evaluate_java_success_certificate(payload)
    except Exception as exc:
        return CIComparisonSnapshot(
            **base,
            status="no_target" if target is None else "unavailable",
            reasons=(str(exc)[:512] or type(exc).__name__,),
        )
    base.update(
        target_sha=certificate.target_sha,
        certificate_input_sha256=canonical_sha256(payload.model_dump(mode="json")),
        certificate=certificate,
        receipt_ids=receipt_ids,
        commands=commands,
    )
    if target is None:
        return CIComparisonSnapshot(
            **base, status="no_target", reasons=("official_ci_target_not_supplied",)
        )
    if target.record.matched_cell is None:
        return CIComparisonSnapshot(
            **base, status="no_matched_cell", reasons=("official_ci_cell_not_matched",)
        )
    cell = next(cell for cell in target.record.cells if cell.cell_id == target.record.matched_cell)
    executed_ids, red_ids, identity_reason = _same_pool_identities(
        parsed_reads, certificate.test_counts, cell
    )
    view = view_from_certificate(certificate, repo=repo)
    view_fields = view.model_dump()
    view_fields.update(
        commands=commands,
        executed_ids=executed_ids,
        red_ids=red_ids,
        authority_ok=(
            view.authority_ok
            and identity_reason is None
            and certificate.flags.test_execution_closed
            and certificate.build_units.status not in {"unavailable", "incomplete"}
            and not any(
                item.code == "CI_TEST_SCOPE_OVERLAP_UNRESOLVED" for item in certificate.diagnostics
            )
        ),
    )
    # Revalidate the boundary rather than bypassing it through model_copy.
    view = type(view).model_validate(view_fields)
    result = evaluate_attainment(view, target.record)
    return CIComparisonSnapshot(
        **base,
        status="evaluated",
        attainment=result,
        reasons=tuple(
            dict.fromkeys(
                (
                    *result.reason_codes,
                    *((identity_reason,) if identity_reason else ()),
                    *(item.code for item in certificate.blockers),
                    *(
                        item.code
                        for item in certificate.diagnostics
                        if item.code == "CI_TEST_SCOPE_OVERLAP_UNRESOLVED"
                    ),
                )
            )
        ),
    )


def render_ci_comparison_lines(value: CIComparisonSnapshot | Mapping[str, Any] | None) -> list[str]:
    if value is None:
        return ["Official CI: unavailable (no sealed comparison)"]
    comparison = (
        value
        if isinstance(value, CIComparisonSnapshot)
        else CIComparisonSnapshot.model_validate(value)
    )
    if comparison.attainment is None:
        return [f"Official CI: {comparison.status} ({'; '.join(comparison.reasons)})"]
    result = comparison.attainment
    score = (
        f"{result.alpha.numerator}/{result.alpha.denominator}"
        if result.alpha is not None
        else "unavailable"
    )
    lines = [f"Official CI: {result.verdict}; scope score {score}; cell {result.cell_id}"]
    if comparison.reasons:
        lines.append("CI evidence: " + "; ".join(comparison.reasons))
    if result.lifecycle_parity is not None:
        parity = result.lifecycle_parity
        details = []
        if parity.missing:
            details.append("missing: " + ", ".join(parity.missing))
        if parity.extra:
            details.append("extra: " + ", ".join(parity.extra))
        suffix = " (" + "; ".join(details) + ")" if details else ""
        lines.append("CI lifecycle parity: " + parity.status + suffix)
    return lines
