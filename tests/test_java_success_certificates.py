import pytest
from pydantic import ValidationError

from sag.agent.java_success_certificates import (
    CertificateBlocker,
    JavaCertificateInput,
    ScopeClaim,
    ScopeSubject,
)
from sag.agent.java_success_certificates import TestCounts as RuntimeTestCounts
from sag.agent.java_success_certificates import (
    TypedObligationSet,
    evaluate_java_success_certificate,
    scope_subject_sha256,
)


def _subject(
    *,
    project_id="healthy-java",
    run_id="run-1",
    target_sha="abcdef1",
    plan_sha256="a" * 64,
):
    return ScopeSubject(
        project_id=project_id,
        run_id=run_id,
        target_sha=target_sha,
        plan_sha256=plan_sha256,
    )


def _scope(
    *,
    kind="repository_default",
    closure="closed",
    project_id="healthy-java",
    run_id="run-1",
    target_sha="abcdef1",
    plan_sha256="a" * 64,
):
    return ScopeClaim(
        scope_id="scope-1",
        revision=1,
        evidence_epoch=run_id,
        subject=_subject(
            project_id=project_id,
            run_id=run_id,
            target_sha=target_sha,
            plan_sha256=plan_sha256,
        ),
        kind=kind,
        closure=closure,
        basis_refs=("plan",),
    )


def _obligations(
    unit,
    required,
    satisfied,
    *,
    failed=(),
    unexpected=(),
    applicability="required",
    subject=None,
):
    subject = subject or _subject()
    return TypedObligationSet(
        unit=unit,
        basis_ref=f"basis:{unit}",
        subject_sha256=scope_subject_sha256(subject),
        scope_id="scope-1",
        scope_revision=1,
        evidence_epoch="run-1",
        applicability=applicability,
        required_ids=required,
        satisfied_ids=satisfied,
        failed_ids=failed,
        unexpected_ids=unexpected,
        evidence_refs=(f"evidence:{unit}",),
    )


def _payload(**overrides):
    scope = overrides.get("scope", _scope())
    values = {
        "assurance_level": "sealed_lineage",
        "scope": scope,
        "build_steps": _obligations(
            "build_plan_step", ("build-1",), ("build-1",), subject=scope.subject
        ),
        "build_units": _obligations(
            "single_maven_project", ("root",), ("root",), subject=scope.subject
        ),
        "test_steps": _obligations(
            "test_plan_step", ("test-1",), ("test-1",), subject=scope.subject
        ),
        "test_targets": _obligations(
            "single_test_project", ("root",), ("root",), subject=scope.subject
        ),
        "evidence_items": _obligations(
            "evidence_binding",
            ("plan", "build-receipt", "test-receipt"),
            ("plan", "build-receipt", "test-receipt"),
            subject=scope.subject,
        ),
        "test_results_authority": "receipt_bound",
        "test_counts": RuntimeTestCounts(executed=12, passed=12, failed=0, errors=0, skipped=0),
    }
    values.update(overrides)
    return JavaCertificateInput(**values)


def test_repository_green_is_exact_typed_closure():
    certificate = evaluate_java_success_certificate(_payload())

    assert certificate.result == "repository_green"
    assert certificate.proof_status == "verified"
    assert certificate.build_units.closure_fraction == "1/1"
    assert certificate.build_units.success_fraction == "1/1"
    assert certificate.test_steps.closure_fraction == "1/1"
    assert certificate.test_targets.closure_fraction == "1/1"
    assert certificate.flags.model_dump() == {
        "build_axis_success": True,
        "test_execution_closed": True,
        "authoritative_test_clean": True,
        "green_proof_verified": True,
    }


def test_parameterized_expansion_is_diagnostic_not_a_completion_denominator():
    certificate = evaluate_java_success_certificate(
        _payload(
            test_counts=RuntimeTestCounts(
                executed=1605,
                passed=1596,
                failed=0,
                errors=0,
                skipped=9,
            ),
            diagnostics=(
                {
                    "code": "parameterized_expansion_observed",
                    "observations": {"runtime_executions": 1605, "static_declarations": 1163},
                    "note": "The two counts are separate grains.",
                },
            ),
        )
    )

    assert certificate.result == "repository_green"
    assert certificate.test_counts.executed == 1605
    assert certificate.diagnostics[0].observations["static_declarations"] == 1163


def test_satisfied_ids_cannot_escape_the_denominator_identity_universe():
    with pytest.raises(ValidationError, match="subset of required ids"):
        _obligations("single_maven_project", ("root",), ("root", "extra"))


def test_obligation_identity_hash_is_order_independent():
    first = evaluate_java_success_certificate(
        _payload(
            build_units=_obligations("maven_reactor_module", ("root", "core"), ("root", "core"))
        )
    )
    reversed_order = evaluate_java_success_certificate(
        _payload(
            build_units=_obligations("maven_reactor_module", ("core", "root"), ("core", "root"))
        )
    )

    assert first.build_units.required_ids == ("core", "root")
    assert first.build_units.required_set_sha256 == reversed_order.build_units.required_set_sha256


def test_required_set_hash_changes_when_the_identity_universe_changes():
    root_only = evaluate_java_success_certificate(
        _payload(build_units=_obligations("maven_reactor_module", ("root",), ("root",)))
    )
    reactor = evaluate_java_success_certificate(
        _payload(
            build_units=_obligations("maven_reactor_module", ("root", "core"), ("root", "core"))
        )
    )

    assert root_only.build_units.required_set_sha256 != reactor.build_units.required_set_sha256


def test_required_set_hash_binds_the_explicit_subject():
    first = evaluate_java_success_certificate(_payload())
    other_target = evaluate_java_success_certificate(_payload(scope=_scope(target_sha="bcdef12")))

    assert first.build_units.required_set_sha256 != other_target.build_units.required_set_sha256


def test_arbitrary_or_cross_grain_obligation_units_are_rejected():
    with pytest.raises(ValidationError, match="Input should be"):
        _obligations("free_form_unit", ("root",), ("root",))

    with pytest.raises(ValidationError, match="wrong identity unit"):
        _payload(build_units=_obligations("test_module", ("root",), ("root",)))


def test_scope_epoch_must_equal_the_explicit_subject_run():
    with pytest.raises(ValidationError, match="sealed subject run"):
        ScopeClaim(
            scope_id="scope-1",
            revision=1,
            evidence_epoch="run-1",
            subject=ScopeSubject(
                project_id="healthy-java",
                run_id="foreign-run",
                target_sha="abcdef1",
                plan_sha256="a" * 64,
            ),
            kind="repository_default",
            closure="closed",
        )


def test_evaluator_revalidates_models_poisoned_by_model_copy():
    payload = _payload()
    poisoned_units = payload.build_units.model_copy(
        update={"required_ids": (), "satisfied_ids": ("ghost",)}
    )
    poisoned_payload = payload.model_copy(update={"build_units": poisoned_units})

    with pytest.raises(ValidationError, match="required obligation set"):
        evaluate_java_success_certificate(poisoned_payload)


def test_partial_fraction_never_rounds_up_to_false_one_hundred_percent():
    identities = tuple(f"module-{index:05d}" for index in range(10_000))
    certificate = evaluate_java_success_certificate(
        _payload(build_units=_obligations("maven_reactor_module", identities, identities[:-1]))
    )

    assert certificate.build_units.status == "incomplete"
    assert certificate.build_units.closure_fraction == "9999/10000"
    assert certificate.build_units.success_fraction == "9999/10000"


def test_unexpected_zero_product_tests_is_not_success():
    certificate = evaluate_java_success_certificate(
        _payload(test_counts=RuntimeTestCounts(executed=0, passed=0, failed=0, errors=0, skipped=0))
    )

    assert certificate.test_outcome_status == "empty"
    assert certificate.test_execution_status == "incomplete"
    assert certificate.result == "incomplete"


def test_terminal_entrypoint_does_not_close_a_partial_test_target_surface():
    certificate = evaluate_java_success_certificate(
        _payload(
            scope=_scope(closure="partial"),
            test_targets=_obligations("single_test_project", ("root", "other"), ("root",)),
            test_counts=RuntimeTestCounts(executed=1, passed=1, failed=0, errors=0, skipped=0),
        )
    )

    assert certificate.test_steps.status == "complete"
    assert certificate.test_execution_status == "incomplete"
    assert certificate.result == "incomplete"


def test_red_tests_are_a_verified_project_outcome_not_a_harness_failure():
    certificate = evaluate_java_success_certificate(
        _payload(
            test_targets=_obligations("single_test_project", ("root",), (), failed=("root",)),
            test_counts=RuntimeTestCounts(executed=20, passed=18, failed=1, errors=1, skipped=0),
        )
    )

    assert certificate.test_execution_status == "complete"
    assert certificate.test_outcome_status == "red"
    assert certificate.result == "test_red"
    assert certificate.proof_status == "verified"
    assert certificate.test_targets.closure_fraction == "1/1"
    assert certificate.test_targets.success_fraction == "0/1"


def test_clean_counts_cannot_hide_a_failed_test_obligation():
    with pytest.raises(ValidationError, match="clean receipt-bound counts conflict"):
        _payload(test_targets=_obligations("single_test_project", ("root",), (), failed=("root",)))


def test_red_counts_require_a_failed_test_target_identity():
    with pytest.raises(ValidationError, match="require a failed test-target"):
        _payload(
            test_counts=RuntimeTestCounts(executed=20, passed=19, failed=1, errors=0, skipped=0)
        )


def test_unbound_results_are_unverifiable_even_when_diagnostic_counts_are_red():
    certificate = evaluate_java_success_certificate(
        _payload(
            test_results_authority="diagnostic",
            test_counts=RuntimeTestCounts(executed=20, passed=18, failed=1, errors=1, skipped=0),
            blockers=(
                CertificateBlocker(
                    code="test_results_unbound",
                    affects=("test_execution",),
                    owner="harness",
                    reason="Rows lack sealed module-qualified identity.",
                ),
            ),
        )
    )

    assert certificate.test_outcome_status == "red"
    assert certificate.test_execution_status == "unverifiable"
    assert certificate.result == "unverifiable"


def test_diagnostic_clean_counts_do_not_set_authoritative_clean_flag():
    certificate = evaluate_java_success_certificate(_payload(test_results_authority="diagnostic"))

    assert certificate.test_outcome_status == "clean"
    assert certificate.flags.authoritative_test_clean is False
    assert certificate.result == "unverifiable"


def test_test_outcome_blocker_prevents_a_verified_red_claim():
    certificate = evaluate_java_success_certificate(
        _payload(
            test_targets=_obligations("single_test_project", ("root",), (), failed=("root",)),
            test_counts=RuntimeTestCounts(executed=20, passed=19, failed=1, errors=0, skipped=0),
            blockers=(
                CertificateBlocker(
                    code="outcome_identity_unavailable",
                    affects=("test_outcome",),
                    owner="harness",
                    reason="The rows cannot be bound to a durable target identity.",
                ),
            ),
        )
    )

    assert certificate.test_execution_status == "complete"
    assert certificate.result == "unverifiable"
    assert certificate.proof_status == "unverifiable"


def test_authoritative_required_build_failure_is_distinct_from_test_red():
    certificate = evaluate_java_success_certificate(
        _payload(
            build_units=_obligations(
                "maven_reactor_module",
                ("root", "core"),
                ("root",),
                failed=("core",),
            )
        )
    )

    assert certificate.build_status == "failed"
    assert certificate.result == "build_failed"
    assert certificate.build_units.closure_fraction == "2/2"
    assert certificate.build_units.success_fraction == "1/2"


def test_scoped_green_cannot_be_projected_as_repository_green():
    certificate = evaluate_java_success_certificate(_payload(scope=_scope(kind="selected_reactor")))

    assert certificate.result == "scoped_green"
    assert certificate.flags.green_proof_verified is True


def test_scope_identity_mismatch_is_rejected():
    foreign = _obligations("single_maven_project", ("root",), ("root",)).model_copy(
        update={"scope_revision": 2}
    )
    with pytest.raises(ValidationError, match="sealed scope identity"):
        _payload(build_units=foreign)


def test_obligation_subject_digest_mismatch_is_rejected():
    foreign = _obligations("single_maven_project", ("root",), ("root",)).model_copy(
        update={"subject_sha256": "b" * 64}
    )

    with pytest.raises(ValidationError, match="sealed scope subject"):
        _payload(build_units=foreign)


def test_legacy_projection_can_show_expected_green_without_minting_canonical_success():
    certificate = evaluate_java_success_certificate(_payload(assurance_level="legacy_projected"))

    assert certificate.result == "repository_green"
    assert certificate.proof_status == "projected"
    assert certificate.flags.green_proof_verified is False


def test_legacy_documented_no_tests_is_projected_build_only_not_verified_green():
    certificate = evaluate_java_success_certificate(
        _payload(
            assurance_level="legacy_projected",
            documented_no_automated_tests=True,
            test_steps=_obligations("test_plan_step", (), (), applicability="not_applicable"),
            test_targets=_obligations(
                "single_test_project", (), (), applicability="not_applicable"
            ),
            test_results_authority="unavailable",
            test_counts=None,
        )
    )

    assert certificate.result == "build_only"
    assert certificate.proof_status == "projected"
    assert certificate.flags.green_proof_verified is False


def test_build_only_requires_complete_durable_evidence():
    certificate = evaluate_java_success_certificate(
        _payload(
            documented_no_automated_tests=True,
            test_steps=_obligations("test_plan_step", (), (), applicability="not_applicable"),
            test_targets=_obligations(
                "single_test_project", (), (), applicability="not_applicable"
            ),
            test_results_authority="unavailable",
            test_counts=None,
            terminal_receipts_unpersisted=1,
        )
    )

    assert certificate.integrity.status == "degraded"
    assert certificate.result == "incomplete"


def test_repository_product_test_scope_does_not_claim_the_full_default_quality_gate():
    certificate = evaluate_java_success_certificate(
        _payload(scope=_scope(kind="repository_product_tests"))
    )

    assert certificate.result == "repository_product_test_green"
    assert certificate.flags.green_proof_verified is True
