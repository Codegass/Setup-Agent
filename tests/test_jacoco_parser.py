from sag.coverage.jacoco_parser import parse_jacoco_xml

REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<report name="core">
  <package name="org/x">
    <counter type="LINE" missed="1" covered="9"/>
    <counter type="BRANCH" missed="5" covered="5"/>
  </package>
  <counter type="INSTRUCTION" missed="40" covered="160"/>
  <counter type="BRANCH" missed="30" covered="70"/>
  <counter type="LINE" missed="20" covered="80"/>
  <counter type="METHOD" missed="2" covered="18"/>
</report>"""


def test_parses_report_level_line_and_branch():
    cov = parse_jacoco_xml(REPORT)
    # report-level totals, NOT the nested package counters
    assert cov["line_covered"] == 80 and cov["line_total"] == 100
    assert cov["line_rate"] == 80.0
    assert cov["branch_covered"] == 70 and cov["branch_total"] == 100
    assert cov["branch_rate"] == 70.0


def test_zero_total_yields_null_rate():
    xml = '<report name="x"><counter type="LINE" missed="0" covered="0"/></report>'
    cov = parse_jacoco_xml(xml)
    assert cov["line_covered"] == 0 and cov["line_total"] == 0
    assert cov["line_rate"] is None
    assert cov["branch_total"] == 0 and cov["branch_rate"] is None


def test_malformed_xml_returns_empty():
    assert parse_jacoco_xml("not xml <<<") == {}
    assert parse_jacoco_xml("") == {}
