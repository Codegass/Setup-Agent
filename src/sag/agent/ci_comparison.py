"""Current-run certificate adapter and the sealed official-CI comparison.

The input is host-authorized execution evidence, never a hand-authored
CertificateView. Targets are supplied before the run; this module has no
network client and never chooses a different CI cell at evidence close.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
import xml.etree.ElementTree as ET
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
from sag.agent.evidence_assessments import receipt_assessment_bundle_complete as _assessed
from sag.agent.receipt_structure import single_maven_module_proven as _single_maven_module
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
    # Host-supplied task, never a model-authored plan or a rewritten CI record.
    execution_command: str | None = Field(default=None, min_length=1, max_length=2048)


def load_ci_target(path: str | Path, *, execution_command: str | None = None) -> PinnedCITarget:
    source = Path(path)
    if source.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("CI target exceeds the 4 MiB input bound")
    raw = source.read_bytes()
    return PinnedCITarget(
        record=TargetRecord.model_validate_json(raw),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        execution_command=execution_command,
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
    acceptance_command: str | None = None
    test_identity_basis: str | None = None
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


def _certificate_input(
    orchestrator, state, validator, project_root, repo, target=None, task_completion=None
):
    """Bind actual receipts to a fixed host task, or read the archived plan path.

    A fixed command supplies obligations, never observations or test counts.
    Matching its recipe does not discharge those obligations without terminal,
    assessed current-checkout receipts and complete hash-verified reports.
    """
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
    from sag.agent.acceptance_task import pinned_acceptance_task

    task = pinned_acceptance_task(scope_pin)
    fixed_command = target.execution_command if target else None
    task_results = {}
    if task is not None:
        if (
            task.repo != repo
            or task_completion is None
            or (
                task_completion.run_id != state.run_id
                or task_completion.task_sha256 != task.sha256
                or tuple((r.id, r.command) for r in task_completion.steps)
                != tuple((s.id, s.command) for s in task.steps)
            )
        ):
            raise ValueError("fixed_task_completion_unavailable")
        task_results = {step.id: step for step in task_completion.steps}
        plan_digest = basis_ref = task.sha256
        groups = (("build", task.steps), ("test", task.steps))
        fixed_key = None
    elif fixed_command:
        from sag.agent.project_execution_plan import ExecutionStep
        from sag.tools.build.backends import parse_runner_command

        if target.record.repo != repo or target.record.sha != scope_pin.target_sha:
            raise ValueError("fixed_task_repository_or_revision_mismatch")
        executor, _, argv = parse_runner_command(fixed_command)
        if executor not in {"maven", "gradle"}:
            raise ValueError("fixed_task_certificate_requires_jvm_runner")
        plan_digest = canonical_sha256(
            {
                "target_sha256": target.raw_sha256,
                "command": fixed_command,
                "project_root": project_root,
            }
        )
        basis_ref = plan_digest
        step = ExecutionStep(
            tool="build",
            params={
                "command": fixed_command,
                "working_directory": project_root,
            },
            purpose="Complete the host-pinned build and test task",
        )
        groups = (("build", (step,)), ("test", (step,)))
        fixed_key = (executor, project_root, tuple(argv))
    else:
        artifact = _accepted_plan(orchestrator, state)
        if artifact is None:
            raise ValueError("accepted_execution_plan_unavailable")
        plan_digest, basis_ref = artifact.authored_plan_sha256, artifact.artifact_sha256
        groups = (("build", artifact.plan.build_steps), ("test", artifact.plan.test_steps))
        fixed_key = None

    receipts = validator._current_scoped_receipts(project_root)
    assessments = validator._read_live_evidence_assessments()
    if receipts is None or assessments is None:
        raise ValueError("current_receipt_or_assessment_publication_unavailable")
    receipts = [
        item
        for item in receipts
        if (task is not None or item.get("tool") in {"maven", "gradle"})
        and item.get("run_id") == state.run_id
        and item.get("target_sha") == scope_pin.target_sha
    ]
    subject = ScopeSubject(
        project_id=repo or project_root,
        run_id=state.run_id,
        target_sha=scope_pin.target_sha,
        plan_sha256=plan_digest,
    )
    scope = ScopeClaim(
        scope_id=("task:" if task is not None or fixed_command else "plan:") + plan_digest[:32],
        revision=1,
        evidence_epoch=state.run_id,
        subject=subject,
        kind="declared_entrypoint",
        closure="closed",
        basis_refs=(basis_ref,),
    )

    def obligations(unit, required, satisfied=(), failed=(), *, applicability="required", red=()):
        return TypedObligationSet(
            unit=unit,
            basis_ref=basis_ref,
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
    planned_calls = (
        Counter()
        if task is not None
        else Counter(
            fixed_key or _call_key(step.tool, step.params) for _, steps in groups for step in steps
        )
    )
    matched = {}
    receipts_by_id = {receipt["receipt_id"]: receipt for receipt in receipts}
    for receipt in sorted(receipts, key=validator._receipt_sequence):
        if task is not None:
            continue  # The completion reader already selected ordered, exact task receipts.
        contract = validator._test_execution_contract(receipt)
        if contract is None or not _assessed(receipt, assessments):
            continue
        if fixed_command and shlex.split(receipt.get("argv") or "")[1:] != list(fixed_key[2]):
            # A runner-added selection/skip is not equivalent to the fixed task,
            # even when the legacy contract's ordered-subsequence check permits it.
            continue
        call = contract["requested_call"]
        key = (
            (
                contract["effective_tool"],
                contract["expected_cwd"],
                tuple(shlex.split(contract.get("expected_argv") or "")),
            )
            if fixed_command
            else _call_key(call["tool"], call["params"])
        )
        matched.setdefault(key, deque(maxlen=planned_calls.get(key, 0))).append(receipt)

    evidence = {basis_ref}
    required_evidence = {basis_ref}
    build_required, build_done, build_failed = [], [], []
    test_required, test_done, test_red = [], [], []
    modules, succeeded_modules, failed_modules = set(), set(), set()
    selected_tests, selected_receipts, selected_counts, selected_rows = [], {}, {}, []
    blockers = []

    def block(code, affects, detail):
        blockers.append(
            CertificateBlocker(code=code, affects=affects, owner="harness", reason=detail)
        )

    if task is not None:
        # Task execution obligations are fixed by the host. Test evidence is
        # an additional whole-task obligation: compile/native steps need not
        # fabricate JUnit reports. Every required command still has to finish.
        test_required.append("test:task")
        required_evidence.add("test:task")
        if task_completion.status != "complete":
            block(
                "FIXED_TASK_INCOMPLETE",
                ("build", "test_execution"),
                "One or more fixed task steps lack ordered, successful execution evidence.",
            )

    for lane, steps in groups:
        for index, step in enumerate(steps):
            step_id = "test:task" if task is not None and lane == "test" else f"{lane}:{index + 1}"
            if not (task is not None and lane == "test"):
                required_evidence.add(step_id)
                required = build_required if lane == "build" else test_required
                required.append(step_id)
            if task is not None:
                witness = task_results[step.id]
                receipt = receipts_by_id.get(witness.receipt_id)
                if receipt is None:
                    continue
                if receipt.get("tool") in {"maven", "gradle"} and (
                    validator._test_execution_contract(receipt) is None
                    or not _assessed(receipt, assessments)
                ):
                    block(
                        "FIXED_TASK_ASSESSMENT_UNAVAILABLE",
                        ("build", "test_execution"),
                        "A fixed JVM task receipt has no complete current assessment.",
                    )
                    continue
                if lane == "test" and (
                    receipt.get("tool") not in {"maven", "gradle"}
                    or not any(
                        receipt.get("report_delta", {}).get(bucket)
                        for bucket in ("new", "changed", "cached")
                    )
                ):
                    continue
            else:
                candidates = matched.get(fixed_key or _call_key(step.tool, step.params))
                if not candidates:
                    continue
                receipt = candidates[-1] if fixed_command else candidates.popleft()
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
                if task is not None and witness.status == "complete":
                    build_done.append(step_id)
                elif (
                    terminal
                    and receipt.get("exit_code") == 0
                    and "expectation_met" in codes
                    and task is None
                ):
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
                    collection = test_red if bound.counts.red else test_done
                    if step_id not in collection:
                        collection.append(step_id)
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
    test_done = [step for step in test_done if step not in test_red]
    if not selected_tests if task is not None else len(selected_tests) != len(test_required):
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


def _jenkins_report_namespaces(orchestrator, parsed_reads, project_root, cell):
    """Bind each raw report to the literal GAV of its unchanged source POM.

    No namespace stripping or artifact/directory-name guessing. A parent group
    may be inherited literally; property expressions and duplicate coordinates
    remain unavailable. The caller already proved the current target SHA.
    """
    from sag.agent.invocation_receipts import _succeeded
    from sag.agent.receipt_test_rows import _report_module_root
    from sag.runtime.container_io import read_container_text, resolve_control_execute

    qualified = [identity.partition("::") for identity in cell.executed_ids]
    if not qualified or not all(
        sep and prefix.count("$") == 1 and suffix for prefix, sep, suffix in qualified
    ):
        return {}
    expected = {prefix for prefix, _, _ in qualified}
    reports = {}
    for parsed in parsed_reads:
        for row in parsed.get("rows", ()):
            path = str(row.get("report_path") or "")
            root = _report_module_root("maven", path)
            if (
                not root
                or posixpath.normpath(path) != path
                or not (root == project_root or root.startswith(project_root.rstrip("/") + "/"))
            ):
                return {}
            reports[path] = root
    roots = set(reports.values())
    if not roots or len(roots) > 256:
        return {}
    execute = resolve_control_execute(orchestrator)
    if not callable(execute):
        return {}
    namespaces = {}
    try:
        for root in sorted(roots):
            path = posixpath.join(root, "pom.xml")
            relative = posixpath.relpath(path, project_root)
            tracked = (
                execute(
                    f"git -C {shlex.quote(project_root)} ls-files --error-unmatch -- {shlex.quote(relative)}"
                )
                or {}
            )
            clean = (
                execute(
                    f"git -C {shlex.quote(project_root)} diff --quiet HEAD -- {shlex.quote(relative)}"
                )
                or {}
            )
            if (
                not _succeeded(tracked)
                or tracked.get("dispatch_status")
                or str(tracked.get("output") or "").strip() != relative
                or not _succeeded(clean)
                or clean.get("dispatch_status")
            ):
                return {}
            content = read_container_text(orchestrator, path, exact_bytes=True)
            if content is None or len(content.encode("utf-8")) > 1024 * 1024:
                return {}
            pom = ET.fromstring(content)
            for element in pom.iter():
                element.tag = element.tag.rsplit("}", 1)[-1]
            if len(pom.findall("artifactId")) != 1 or len(pom.findall("groupId")) > 1:
                return {}
            group = (pom.findtext("groupId") or pom.findtext("parent/groupId") or "").strip()
            artifact = (pom.findtext("artifactId") or "").strip()
            if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (group, artifact)):
                return {}
            namespace = group + "$" + artifact
            if namespace not in expected or namespace in namespaces.values():
                return {}
            namespaces[root] = namespace
    except Exception:
        return {}
    return {path: namespaces[root] for path, root in reports.items()}


def _same_pool_identities(
    parsed_reads, counts, cell, *, single_module=False, report_namespaces=None
):
    """Map complete raw JUnit identities to the same fixed target universe.

    A sole module or unchanged module POM can prove a Jenkins namespace. Once
    a target names its universe, an incomplete or different local pool cannot
    fall back to a more flattering count-only comparison.
    """
    from sag.agent.receipt_test_rows import rows_were_bounded

    identity_needed_for_red = (
        counts is not None and counts.red > 0 and (cell.red_count > 0 or cell.flaky_count > 0)
    )
    if not cell.executed_ids:
        return (
            (),
            (),
            "CI_TEST_IDENTITIES_NOT_COMPARABLE" if identity_needed_for_red else None,
            None,
        )
    if identity_needed_for_red and (
        len(cell.red_ids) != cell.red_count or len(cell.flaky_ids) != cell.flaky_count
    ):
        return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE", None
    # Jenkins Maven reports qualify raw JUnit IDs as group$artifact::class#name.
    # One independently proven module on BOTH sides makes that sole namespace
    # unambiguous. Never strip namespaces across multiple/unknown modules or
    # collapse duplicate testcase identities into a more flattering set.
    qualified = [identity.partition("::") for identity in cell.executed_ids]
    namespaces = {prefix for prefix, separator, _ in qualified if separator}
    aliases = {}
    namespace = None
    if (
        single_module
        and cell.modules == (".",)
        and len(namespaces) == 1
        and all(
            separator and prefix.count("$") == 1 and suffix
            for prefix, separator, suffix in qualified
        )
    ):
        aliases = {
            suffix: original for original, (_, _, suffix) in zip(cell.executed_ids, qualified)
        }
        if len(aliases) != len(cell.executed_ids):
            return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE", None
        namespace = next(iter(namespaces))
    executed, red = [], []
    mapped = False
    for parsed in parsed_reads:
        if parsed.get("status") != "complete" or parsed.get("reasons") or rows_were_bounded(parsed):
            return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE", None
        for row in parsed.get("rows", ()):
            classname, name = row.get("classname"), row.get("name")
            if (
                not isinstance(classname, str)
                or not isinstance(name, str)
                or not classname
                or not name
            ):
                return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE", None
            identity = classname + "#" + name
            if identity not in cell.executed_ids and identity in aliases:
                identity = aliases[identity]
                mapped = True
            elif identity not in cell.executed_ids and report_namespaces:
                prefix = report_namespaces.get(row.get("report_path"))
                if prefix:
                    identity = prefix + "::" + identity
                    mapped = True
            if identity not in cell.executed_ids or identity in executed:
                return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE", None
            executed.append(identity)
            if row.get("outcome") in {"failed", "error"}:
                red.append(identity)
    if (
        counts is None
        or len(executed) != counts.reported
        or len(red) != counts.red
        or set(executed) != set(cell.executed_ids)
    ):
        return (), (), "CI_TEST_IDENTITIES_NOT_COMPARABLE", None
    return (
        tuple(executed),
        tuple(red),
        None,
        (
            "jenkins_module_pom_coordinates"
            if mapped and report_namespaces
            else "jenkins_single_module:" + namespace if mapped else "exact_raw_junit_ids"
        ),
    )


def build_ci_comparison(
    orchestrator, state, *, validator, project_root, repository, target=None, task_completion=None
):
    repo = repository_identity(repository)
    base = {
        "run_id": state.run_id,
        "repo": repo,
        "target_record_sha256": target.raw_sha256 if target else None,
        "acceptance_command": target.execution_command if target else None,
    }
    if validator is None or not project_root:
        return CIComparisonSnapshot(
            **base,
            status="no_target" if target is None else "unavailable",
            reasons=("certificate_adapter_unavailable",),
        )
    try:
        payload, receipt_ids, commands, parsed_reads = _certificate_input(
            orchestrator, state, validator, project_root, repo, target, task_completion
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
    single_module = certificate.build_units.required_ids == (".",)
    namespaces = (
        _jenkins_report_namespaces(orchestrator, parsed_reads, project_root, cell)
        if not single_module
        else {}
    )
    executed_ids, red_ids, identity_reason, identity_basis = _same_pool_identities(
        parsed_reads,
        certificate.test_counts,
        cell,
        single_module=single_module,
        report_namespaces=namespaces,
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
        test_identity_basis=identity_basis,
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
