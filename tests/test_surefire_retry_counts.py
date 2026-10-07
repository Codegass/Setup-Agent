"""A stale retry header needs independent class and goal evidence, never max()."""
import hashlib
import xml.etree.ElementTree as ET

import pytest

from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import junit_counts, maven_events


XML = '''<testsuite xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xsi:noNamespaceSchemaLocation="https://maven.apache.org/surefire/maven-surefire-plugin/xsd/surefire-test-report-3.0.xsd"
 version="3.0" name="example.RetryTest" tests="1" failures="0" errors="0" skipped="0">
 <testcase classname="example.RetryTest" name="passes"/>
 <testcase classname="example.RetryTest" name="skips"><skipped/></testcase>
 <testcase classname="example.RetryTest" name="recovers"><flakyFailure message="first attempt failed"/></testcase>
</testsuite>'''
LOG = '''[INFO] --- surefire:3.0.0-M5:test (default-test) @ sample ---
[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 1, Time elapsed: 1 s <<< FAILURE! - in example.RetryTest
[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.1 s - in example.RetryTest
[WARNING] Tests run: 3, Failures: 0, Errors: 0, Skipped: 1, Flakes: 1
[INFO] BUILD SUCCESS
'''


def bound(tmp_path, name, text):
    p = tmp_path/name; p.write_text(text)
    return {'path': name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size}


def recorded(tmp_path, xml=XML, log=LOG):
    report = {**bound(tmp_path, 'report.xml', xml), 'fresh': True,
              'module': 'sample', 'test_kind': 'unit',
              'producer_relative_path': 'target/surefire-reports/TEST-example.RetryTest.xml'}
    invocation = {'invocation_id': 'inv', 'runner': 'maven', 'status': 'completed',
        'exit_code': 0, 'log_complete': True, 'log': bound(tmp_path, 'output.log', log),
        'reports': [report], 'reports_collection_complete': True,
        'effective_execution': {'serial': True}}
    row = {'id': 'unit', 'kind': 'test', 'subtype': 'unit', 'module': 'sample',
        'scope': {'status': 'declared', 'module_path': '.'},
        'validation': {'rule': 'junit', 'goals': ['surefire:test'],
                       'report_directories': ['target/surefire-reports']}}
    return row, invocation, report


def assess(tmp_path, xml=XML, log=LOG):
    row, invocation, _ = recorded(tmp_path, xml, log)
    try:
        return native_requirement(row, invocation, tmp_path, log,
                                  maven_events(log, terminal=True, serial=True))
    except ValueError as exc:
        return {'status': 'unavailable', 'reason': str(exc)}


def test_rerun_header_is_normalized_only_with_bound_native_summaries(tmp_path):
    row, invocation, report = recorded(tmp_path)
    with pytest.raises(ValueError, match='counts conflict'):
        junit_counts(tmp_path, [report])
    result = assess(tmp_path)
    assert result['status'] == 'passed'
    assert result['test_counts'] == {'reported': 3, 'passed': 2, 'failed': 0,
                                      'errors': 0, 'skipped': 1, 'assessed': 2}
    proof = result['test_count_reconciliation'][0]
    assert proof['raw_suite_counts']['tests'] == 1
    assert proof['resolved_case_counts']['tests'] == 3
    assert proof['retry_executions'] == 1 and proof['header_normalized']
    assert (tmp_path/'report.xml').read_text() == XML


@pytest.mark.parametrize('mutation', [
    lambda s:s.replace('name="passes"', 'name="recovers"'),
    lambda s:s.replace('<flakyFailure message="first attempt failed"/>', ''),
    lambda s:s.replace('<flakyFailure message="first attempt failed"/>', '<failure/>'),
    lambda s:s.replace('<flakyFailure message="first attempt failed"/>', '<flakyFailure/><flakyFailure/>'),
    lambda s:s.replace('version="3.0"', 'version="4.0"'),
    lambda s:s.replace('tests="1"', 'tests="2"'),
    lambda s:s.replace('name="passes"/>', 'name="passes"><skipped/></testcase>'),
])
def test_partial_or_conflicting_cases_do_not_get_reconstructed_counts(tmp_path, mutation):
    assert assess(tmp_path, mutation(XML))['status'] != 'passed'


@pytest.mark.parametrize('mutation', [
    lambda s:s.replace('Tests run: 3, Failures: 1', 'Tests run: 4, Failures: 1'),
    lambda s:s.replace('Tests run: 1, Failures: 0', 'Tests run: 2, Failures: 0'),
    lambda s:s.replace('Flakes: 1', 'Flakes: 2'),
    lambda s:s.replace('Tests run: 3, Failures: 0', 'Tests run: 2, Failures: 0'),
    lambda s:s.replace('[INFO] BUILD SUCCESS', ''),
    lambda s:s.replace('[INFO] BUILD SUCCESS', '[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.0.0-M5:test (default-test) on project sample: failure\n[INFO] BUILD FAILURE'),
])
def test_native_conflicts_missing_return_and_late_failure_do_not_pass(tmp_path, mutation):
    assert assess(tmp_path, log=mutation(LOG))['status'] != 'passed'


def test_correct_retry_header_has_same_final_counts_without_double_counting(tmp_path):
    xml = XML.replace('tests="1"', 'tests="3"').replace('skipped="0"', 'skipped="1"')
    result = assess(tmp_path, xml)
    assert result['status'] == 'passed' and result['test_counts']['assessed'] == 2
    assert result['test_count_reconciliation'][0]['header_normalized'] is False


def test_final_red_results_are_not_cleared_by_flaky_history(tmp_path):
    root = ET.fromstring(XML)
    root.set('tests', '3');root.set('failures', '1');root.set('skipped', '1')
    case = root.findall('testcase')[2]
    case.remove(case[0]);ET.SubElement(case, 'failure')
    result = assess(tmp_path, ET.tostring(root, encoding='unicode'))
    assert result['status'] == 'failed' and result['test_counts']['failed'] == 1
