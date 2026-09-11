"""The Maven observation must not mistake its last module for the whole run."""

import pytest
import test_fixed_command_verification as fixed
from test_ci_comparison import Runner
from test_maven_gradle_tool_contracts import FakeBuildToolOrchestrator, FakeOutputStorage

from sag.agent import receipt_test_rows
from sag.evidence import EvidenceAssessment
from sag.evidence import InvocationStatus
from sag.tools.internal.maven_tool import MavenTool


@pytest.fixture
def report_metadata(tmp_path, monkeypatch, request):
    monkeypatch.setattr(receipt_test_rows, "REPORT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    failed = getattr(request, 'param', 0)
    xml = f'<testsuite tests="5" failures="{failed}" errors="0" skipped="0">' + "".join(
        f'<testcase classname="a.T" name="case{i}">' +
        ('<failure message="red"/>' if i < failed else '') + '</testcase>'
        for i in range(5)
    ) + '</testsuite>'
    monkeypatch.setattr(fixed, "Runner", lambda fs: Runner(fs, xml=xml))
    run = fixed.fixed_run()
    result = fixed.dispatch(run, run.target.execution_command)
    assert result.completed
    assert result.metadata["report_test_counts"] == {
        "reported": 5, "passed": 5 - failed, "failed": failed, "errors": 0, "skipped": 0,
    }
    return result.metadata


def execute_with_metadata(metadata, *, exit_code=0, detached=False, termination=None, output_override=None, requirement=None):
    # Deliberately disagree with the real XML fixture: complete receipt-bound
    # reports remain authoritative even when the console parser gets all modules.
    output = "\n".join([
        '[INFO] --- surefire:3.5.6:test (default-test) @ first ---',
        '[INFO] Results:',
        '[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0',
        '[INFO] --- surefire:3.5.6:test (default-test) @ last ---',
        '[INFO] Results:',
        '[INFO] Tests run: 30, Failures: 0, Errors: 0, Skipped: 0',
        '[INFO] BUILD SUCCESS' if exit_code == 0 else '[ERROR] BUILD FAILURE',
    ])
    if output_override is not None:
        output = output_override
    native = {'output': output, 'exit_code': exit_code, 'runner_dispatched': True}
    if detached:
        native.update(dispatch_status='completed_detached', full_output=output,
                      dispatch={'job_id': '0123456789ab'}, lifecycle_state='finished')
    if termination:
        native.update(termination_reason=termination, execution_time=1200.0)
    tool = MavenTool(FakeBuildToolOrchestrator(native))
    tool.output_storage = FakeOutputStorage('output_feedback_test')
    tool._record_test_summary = lambda *args, **kwargs: None
    # Isolate the consumer from transport; metadata above was produced by the
    # actual hash-checked parser and publication path, not invented counts.
    def attach(**kwargs):
        tool._pending_invocation_receipt = dict(metadata)
    tool._record_invocation_receipt = attach
    return tool.execute(command='verify', working_directory='/workspace/project',
                        **({'maven_version_requirement': requirement} if requirement else {}))


@pytest.mark.usefixtures('facade_contract_authority', 'exact_internal_runner_authority')
@pytest.mark.parametrize('detached', [False, True])
def test_model_stats_and_formatted_feedback_use_verified_whole_run_counts(report_metadata, detached):
    result = execute_with_metadata(report_metadata, detached=detached)
    assert result.succeeded, result
    assert result.test_stats.executed == result.test_stats.passed == 5
    assert result.metadata['analysis']['log_tests_run']['total'] == 32
    assert result.metadata['analysis']['tests_run']['total'] == 5
    assert result.metadata['test_stats_basis'] == 'invocation_report_xml'
    assert 'hash-verified XML' in result.output


@pytest.mark.usefixtures('facade_contract_authority', 'exact_internal_runner_authority')
@pytest.mark.parametrize('metadata', [
    {'receipt_id': 'test-no-current-reports'},
    {'receipt_persisted': False, 'receipt_persistence_code': 'transport_write_failed'},
])
def test_missing_bound_counts_are_explicit_console_diagnostics(metadata):
    result = execute_with_metadata(metadata)
    assert result.test_stats.executed == 32
    assert result.metadata['test_stats_basis'] == 'stdout_summary'
    assert 'complete current XML counts are unavailable' in result.output


@pytest.mark.usefixtures('facade_contract_authority', 'exact_internal_runner_authority')
@pytest.mark.parametrize('detached', [False, True])
def test_green_report_counts_do_not_upgrade_a_failed_runner(report_metadata, detached):
    result = execute_with_metadata(report_metadata, exit_code=1, detached=detached)
    assert not result.succeeded
    assert result.invocation_status is InvocationStatus.COMPLETED
    assert result.test_stats.executed == 5


@pytest.mark.usefixtures('facade_contract_authority', 'exact_internal_runner_authority')
def test_green_report_counts_do_not_upgrade_a_timeout(report_metadata):
    result = execute_with_metadata(report_metadata, termination='silent_timeout')
    assert not result.succeeded
    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert result.test_stats.executed == 5
    assert result.metadata['test_stats_basis'] == 'invocation_report_xml'


@pytest.mark.usefixtures('facade_contract_authority', 'exact_internal_runner_authority')
@pytest.mark.parametrize('report_metadata', [1], indirect=True)
def test_current_red_xml_cannot_be_hidden_by_green_console_summaries(report_metadata):
    result = execute_with_metadata(report_metadata)
    assert result.test_stats.passed == 4
    assert result.test_stats.failed == 1
    assert result.evidence_assessment is EvidenceAssessment.PARTIAL
    assert 'maven_success_vs_test_failures' in result.conflicts


@pytest.mark.usefixtures('facade_contract_authority', 'exact_internal_runner_authority')
def test_repeated_console_executions_do_not_double_the_final_report_pool(report_metadata):
    execution = '\n'.join([
        '[INFO] --- maven-surefire-plugin:3.5.6:test (default-test) @ root ---',
        '[INFO] Results:',
        '[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0',
    ])
    result = execute_with_metadata(report_metadata, output_override=execution+'\n'+execution+'\n[INFO] BUILD SUCCESS')
    assert result.metadata['analysis']['log_tests_run']['total'] == 10
    assert result.test_stats.executed == 5


def test_unparseable_current_xml_does_not_publish_count_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(receipt_test_rows, "REPORT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(fixed, 'Runner', lambda fs: Runner(fs, xml='<broken>'))
    run = fixed.fixed_run()
    result = fixed.dispatch(run, run.target.execution_command)
    assert 'report_test_counts' not in result.metadata
    comparison = fixed.compare(run)
    assert comparison.attainment is None or comparison.attainment.alpha is None


@pytest.mark.usefixtures('facade_contract_authority', 'exact_internal_runner_authority')
@pytest.mark.parametrize('requirement', [None, '[3.9,)'])
@pytest.mark.parametrize('detached', [False, True])
@pytest.mark.parametrize('report_metadata', [1], indirect=True)
def test_passed_enforcer_checks_do_not_mislabel_test_failures_or_invalidate_maven(report_metadata, requirement, detached):
    output = '\n'.join([
        '[INFO] Rule 0: org.apache.maven.enforcer.rules.version.RequireJavaVersion passed',
        '[INFO] Rule 1: org.apache.maven.enforcer.rules.version.RequireMavenVersion passed',
        '[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0',
        '[ERROR] SocketTimeoutTest timed out after 5 seconds',
        '[ERROR] BUILD FAILURE',
    ])
    result = execute_with_metadata(report_metadata, exit_code=1, detached=detached,
                                   output_override=output, requirement=requirement)
    assert result.error_code == 'TEST_FAILURE'
    assert not result.succeeded
    assert result.test_stats.failed == 1
    assert not result.metadata.get('runtime_contract_persisted')
    assert not any('runtime selection changes' in suggestion for suggestion in result.suggestions)
