# tests/test_groovy_tests_survive_oracle.py
"""Bigtop-shaped regression: Groovy testcase failures/errors must survive
canonical XML aggregation (spec §3.4-4 — the 2026-07-24 run sealed 4/4
after filtering the failing Groovy classes out)."""

import inspect

from sag.agent.physical_validator import PhysicalValidator

BIGTOP_SUITE = """<testsuite name="org.apache.bigtop.itest.pmanager.PackageManagerTest" tests="3" failures="1" errors="1" skipped="0">
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="testGetDeps" time="0.1"><failure message="boom"/></testcase>
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="testGetDocs" time="0.1"><error message="crash"/></testcase>
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="installBash" time="0.1"/>
</testsuite>"""


def _validator():
    return PhysicalValidator.__new__(PhysicalValidator)


def test_groovy_named_testcases_are_counted():
    stats = _validator()._parse_single_test_xml(BIGTOP_SUITE, "TEST-PackageManagerTest.xml")
    assert stats["total"] == 3
    assert stats["failed"] == 1
    assert stats["errors"] == 1
    assert stats["passed"] == 1


def test_parser_has_no_language_filter_parameter():
    signature = inspect.signature(PhysicalValidator._parse_single_test_xml)
    assert "groovy_test_classes" not in signature.parameters
