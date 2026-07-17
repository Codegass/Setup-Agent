"""Test case catalog module for unified test tracking across static analysis and runtime validation."""

from .catalog import (
    RuntimeTestCaseRecord,
    TestCaseCatalog,
    TestCaseDescriptor,
    build_java_test_catalog,
    generate_test_case_key,
    merge_testcase_status,
    normalize_method_name,
    normalize_testcase_identifier,
)
from .results import (
    AggregatedTestResults,
    CanonicalTestIdentity,
    TestResultHistory,
    TestResultObservation,
    aggregate_test_results,
    canonical_test_identity,
)

__all__ = [
    "TestCaseDescriptor",
    "RuntimeTestCaseRecord",
    "TestCaseCatalog",
    "build_java_test_catalog",
    "normalize_testcase_identifier",
    "generate_test_case_key",
    "normalize_method_name",
    "merge_testcase_status",
    "AggregatedTestResults",
    "CanonicalTestIdentity",
    "TestResultHistory",
    "TestResultObservation",
    "aggregate_test_results",
    "canonical_test_identity",
]
