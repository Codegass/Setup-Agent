from sag.testcases.results import (
    CanonicalTestIdentity,
    TestResultObservation,
    aggregate_test_results,
    canonical_test_identity,
)


def test_canonical_identity_keeps_parameter_id_separate_from_test_name():
    identity = canonical_test_identity(
        classname="tests.test_api.ApiTests",
        name="test_request[https-proxy]",
        file_path="./tests/test_api.py",
    )

    assert identity == CanonicalTestIdentity(
        module_or_file="tests/test_api.py",
        class_name="ApiTests",
        name="test_request",
        param_id="https-proxy",
    )
    assert identity.as_tuple() == (
        "tests/test_api.py",
        "ApiTests",
        "test_request",
        "https-proxy",
    )


def test_canonical_identity_falls_back_to_class_module_without_report_path():
    identity = canonical_test_identity(
        classname="com.example.FooTest",
        name="testWorks",
        file_path=None,
    )

    assert identity.module_or_file == "com.example"
    assert identity.class_name == "FooTest"
    assert identity.name == "testWorks"
    assert identity.param_id == ""


def test_history_orders_by_explicit_attempt_id_not_input_or_source_order():
    identity = canonical_test_identity("tests.test_api", "test_flaky", "tests/test_api.py")
    observations = [
        TestResultObservation(identity=identity, attempt_id=3, status="passed", source="a.xml"),
        TestResultObservation(identity=identity, attempt_id=1, status="failed", source="z.xml"),
        TestResultObservation(identity=identity, attempt_id=2, status="failed", source="m.xml"),
    ]

    result = aggregate_test_results(observations)
    history = result.histories[identity]

    assert history.first == "failed"
    assert history.latest == "passed"
    assert history.worst == "failed"
    assert history.retried_count == 2
    assert history.attempt_ids == (1, 2, 3)
    assert history.flaky is True
    assert result.latest_counts == {
        "executed": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert result.flaky_count == 1


def test_duplicate_observations_in_one_attempt_merge_to_worst_once():
    identity = canonical_test_identity("tests.test_api", "test_case[x]", None)
    result = aggregate_test_results(
        [
            TestResultObservation(identity, 4, "passed", "first.xml"),
            TestResultObservation(identity, 4, "error", "duplicate.xml"),
        ]
    )

    history = result.histories[identity]
    assert history.first == "error"
    assert history.latest == "error"
    assert history.worst == "error"
    assert history.retried_count == 0
    assert result.latest_counts["executed"] == 1
    assert result.latest_counts["errors"] == 1


def test_pass_then_failure_is_not_laundered_or_called_flaky():
    identity = canonical_test_identity("tests.test_api", "test_regressed", None)
    result = aggregate_test_results(
        [
            TestResultObservation(identity, 1, "passed", "one.xml"),
            TestResultObservation(identity, 2, "failed", "two.xml"),
        ]
    )

    history = result.histories[identity]
    assert history.latest == "failed"
    assert history.worst == "failed"
    assert history.flaky is False
    assert result.flaky_count == 0


def test_serialized_histories_are_stably_sorted_by_canonical_identity():
    beta = canonical_test_identity("pkg.Beta", "test_b", None)
    alpha = canonical_test_identity("pkg.Alpha", "test_a", None)
    result = aggregate_test_results(
        [
            TestResultObservation(beta, 1, "passed", "b.xml"),
            TestResultObservation(alpha, 1, "skipped", "a.xml"),
        ]
    )

    serialized = result.to_dict()["histories"]
    assert [item["identity"]["class_name"] for item in serialized] == ["Alpha", "Beta"]
