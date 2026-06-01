"""Test case catalog module for unified test tracking across static analysis and runtime validation."""

from .catalog import (
    TestCaseDescriptor,
    RuntimeTestCaseRecord,
    TestCaseCatalog,
    build_java_test_catalog,
    normalize_testcase_identifier,
    generate_test_case_key,
    normalize_method_name,
    merge_testcase_status,
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
]