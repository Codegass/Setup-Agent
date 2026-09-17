"""Deterministic demo read models for the SAG Workbench skeleton UI."""

from __future__ import annotations

from typing import Any

from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
)
from sag.result_card.build import build_result_card
from sag.result_card.models import RunResultCard
from sag.web.models import (
    BuildSummary,
    ClaimedTestLayers,
    ContextTrace,
    ContextTraceAction,
    ContextTraceIteration,
    ContextTracePhase,
    ContextTraceTask,
    ContextTraceTrunk,
    DashboardResponse,
    DockerSummary,
    EvidenceCountSummary,
    EvidenceGroup,
    EvidenceLayerProjectionSummary,
    EvidenceRecord,
    ExecutionSessionDetail,
    MetricsV2EvidenceSummary,
    ModuleRollup,
    ModuleSummary,
    ObservationCountSummary,
    ReceiptSummary,
    ReportDocument,
    TerminalConnectionState,
    TestEvidenceLayers,
    TestSummary,
    WorkspaceResult,
    WorkspaceSummary,
)

_COMMONS_WORKSPACE_ID = "sag-commons-cli"
_COMMONS_SESSION_ID = "CC-3"
_COMMONS_RUN_ID = "20260606_021408_000000_sag-commons-cli_1-1-demo-commons-cli"


def _commons_test_evidence_layers() -> EvidenceLayerProjectionSummary:
    unavailable_identity = EvidenceCountSummary(
        availability="unavailable",
        reason="demo fixture does not model module-qualified test identities",
        basis="demo fixture",
    )
    no_observations = ObservationCountSummary(
        executed=0,
        passed=0,
        failed=0,
        errors=0,
        skipped=0,
        availability="available",
        basis="demo fixture: no diagnostic observations",
    )
    return EvidenceLayerProjectionSummary(
        projection_status="metrics-v2-artifact-unavailable",
        tests=TestEvidenceLayers(
            claimed=ClaimedTestLayers(
                latest_subjects=unavailable_identity,
                latest_cases=unavailable_identity,
                receipt_executions=EvidenceCountSummary(
                    executed=320,
                    passed=312,
                    failed=8,
                    errors=0,
                    skipped=0,
                    availability="available",
                    basis="demo fixture: one synthetic Maven test receipt",
                ),
            ),
            quarantined_observations=no_observations,
            unattributed_observations=no_observations,
            stale_observations=no_observations,
        ),
        evidence=MetricsV2EvidenceSummary(
            integrity="complete",
            receipts_expected=1,
            receipts_persisted=1,
            terminal_receipts_unpersisted=0,
            conflict_count=0,
        ),
    )


def _commons_test_summary() -> TestSummary:
    return TestSummary(
        state="partial",
        pass_count=312,
        fail_count=8,
        skip_count=0,
        total=320,
        pass_rate=97.5,
        execution_rate=100.0,
        note="312 passing, 8 HelpFormatter line-wrapping failures in the local Maven suite.",
        errors=0,
        report_file_count=42,
        unique_total=180,
        unique_passed=176,
        unique_failed=2,
        unique_errors=0,
        unique_skipped=2,
        declared_total=210,
        method_execution_rate=85.7,
        failing_names=[
            "com.demo.FlakyTest.testRetry",
            "com.demo.NetTest.testTimeout",
        ],
        conflicts=[],
        evidence_refs=["output_demo_tests"],
        evidence_layers=_commons_test_evidence_layers(),
    )


def _commons_build_summary() -> BuildSummary:
    return BuildSummary(
        state="success",
        tool="Maven 3.9.6",
        time="47.2s",
        artifact="target/commons-cli-1.6.0.jar",
        note="Compiled with Maven 3.9.6 and JDK 11 inside the workspace container.",
        system="maven",
        class_count=180,
        jar_count=2,
        module_output_count=3,
        artifact_samples=[
            "target/classes/com/demo/App.class",
            "target/demo-1.0.jar",
        ],
        warnings=[],
        evidence_refs=["output_demo_build"],
    )


def _commons_evidence() -> list[EvidenceGroup]:
    return [
        EvidenceGroup(
            source="Project analyzer",
            status="complete",
            counts="4 refs",
            time="02:16:04",
            summary="Detected commons-cli 1.6.0 Maven project layout and HelpFormatter test entrypoints.",
            records=[
                EvidenceRecord(
                    time="02:14:20",
                    status="ok",
                    title="Project model",
                    detail="Read pom.xml and identified commons-cli 1.6.0 packaging metadata.",
                    ref="pom.xml",
                ),
                EvidenceRecord(
                    time="02:14:27",
                    status="ok",
                    title="Test layout",
                    detail="Mapped HelpFormatter tests in src/test/java as the primary validation target.",
                    ref="src/test/java",
                ),
            ],
        ),
        EvidenceGroup(
            source="Test validator",
            status="partial",
            counts="320 tests",
            time="02:16:41",
            summary="Maven 3.9.6 test run completed with 8 HelpFormatter line-wrapping failures.",
            records=[
                EvidenceRecord(
                    time="02:16:23",
                    status="pass",
                    title="Maven test suite",
                    detail="Executed mvn test with JDK 11 in the workspace container.",
                    ref="target/surefire-reports",
                ),
                EvidenceRecord(
                    time="02:16:39",
                    status="fail",
                    title="HelpFormatter line-wrapping failures",
                    detail="Eight HelpFormatter assertions expected width 74 but observed wrapping at width 80.",
                    ref="target/surefire-reports/TEST-org.apache.commons.cli.HelpFormatterTest.xml",
                ),
            ],
        ),
    ]


def _commons_modules() -> list[ModuleSummary]:
    # A coherent multi-module Maven demo for the commons-cli workspace: all
    # three modules build, while one retains project-owned test failures.
    # Names, FQNs, and evidence paths are all commons-cli + Maven (target/) so
    # the --demo view reads as a single, consistent project.
    return [
        ModuleSummary(
            name="commons-cli-validator",
            path="validator",
            build_status="success",
            build_source="reactor",
            class_count=None,
            jar_count=None,
            build_error_samples=[],
            test_source="none",
            failing_names=[],
            failing_count=None,
            evidence_refs=["/workspace/commons-cli/validator/target"],
        ),
        ModuleSummary(
            name="commons-cli-core",
            path="core",
            build_status="success",
            build_source="reactor",
            class_count=261,
            jar_count=1,
            tests_total=1240,
            tests_passed=1232,
            tests_failed=8,
            tests_errors=0,
            tests_skipped=0,
            test_source="runner_xml",
            failing_names=[
                "org.apache.commons.cli.HelpFormatterTest.testWrappedColumns",
                "org.apache.commons.cli.HelpFormatterTest.testPrintWrapped",
                "org.apache.commons.cli.HelpFormatterTest.testRenderWrappedTextWordCut",
                "org.apache.commons.cli.HelpFormatterTest.testPrintHelpWithEmptySyntax",
                "org.apache.commons.cli.HelpFormatterTest.testIndentedHeaderAndFooter",
                "org.apache.commons.cli.HelpFormatterTest.testDefaultArgName",
                "org.apache.commons.cli.HelpFormatterTest.testRtrim",
                "org.apache.commons.cli.HelpFormatterTest.testAutomaticUsage",
            ],
            failing_count=8,
            line_covered=2040,
            line_total=2480,
            line_rate=82.3,
            branch_covered=520,
            branch_total=720,
            branch_rate=72.2,
            coverage_source="jacoco-injected",
            evidence_refs=["/workspace/commons-cli/core/target/surefire-reports"],
        ),
        ModuleSummary(
            name="commons-cli-help",
            path="help",
            build_status="success",
            build_source="reactor",
            class_count=140,
            jar_count=1,
            tests_total=420,
            tests_passed=420,
            tests_failed=0,
            tests_errors=0,
            tests_skipped=0,
            test_source="runner_xml",
            failing_names=[],
            failing_count=0,
            line_covered=410,
            line_total=520,
            line_rate=78.8,
            branch_covered=90,
            branch_total=140,
            branch_rate=64.3,
            coverage_source="jacoco-injected",
            evidence_refs=["/workspace/commons-cli/help/target/surefire-reports"],
        ),
    ]


def _commons_module_summary() -> ModuleRollup:
    return ModuleRollup(
        modules_total=3,
        modules_built=3,
        modules_failed=0,
        modules_skipped=0,
        modules_with_test_failures=1,
        build_systems=["maven"],
        single_module=False,
        line_covered=2450,
        line_total=3000,
        line_rate=81.7,
        branch_covered=610,
        branch_total=860,
        branch_rate=70.9,
        coverage_source="jacoco-injected",
    )


def _commons_context() -> ContextTrace:
    return ContextTrace(
        trunk=ContextTraceTrunk(
            goal="Prepare commons-cli workspace for reproducible setup and validation.",
            state="in_progress",
            progress={"done": 2, "total": 5},
            summary="Workspace analysis and environment setup are complete; test validation is active.",
        ),
        phases=[
            ContextTracePhase(
                id="phase_analyze",
                name="analyze",
                title="Analyze project and dependency graph",
                status="completed",
                evidence_status="success",
                key_results="Captured commons-cli 1.6.0 coordinates and JDK 11 toolchain requirements.",
                progress={"iterations": 2, "thoughts": 1, "actions": 1},
                tasks=[
                    ContextTraceTask(
                        id="phase_analyze/work",
                        title="Analyze project and dependency graph",
                        status="completed",
                        iterations=[
                            ContextTraceIteration(
                                iteration=1,
                                sequence=1,
                                thoughts=["Read project metadata and dependency graph."],
                            ),
                            ContextTraceIteration(
                                iteration=2,
                                sequence=2,
                                actions=[
                                    ContextTraceAction(
                                        tool_name="project",
                                        success=True,
                                        parameters={"action": "analyze"},
                                        observation="Project analysis completed.",
                                    )
                                ],
                            ),
                        ],
                    )
                ],
            ),
            ContextTracePhase(
                id="phase_test",
                name="test",
                title="Run full test suite and summarize HelpFormatter failures",
                status="in_progress",
                evidence_status="partial",
                conflicts=["HelpFormatter expected width 74 but observed wrapping at width 80."],
                key_results="Maven tests ran with HelpFormatter line wrapping at width 80 instead of expected width 74.",
                progress={"iterations": 1, "thoughts": 1, "actions": 1},
                tasks=[
                    ContextTraceTask(
                        id="phase_test/work",
                        title="Run full test suite and summarize HelpFormatter failures",
                        status="in_progress",
                        iterations=[
                            ContextTraceIteration(
                                iteration=7,
                                sequence=1,
                                thoughts=["Run the test lifecycle and preserve failing evidence."],
                                actions=[
                                    ContextTraceAction(
                                        tool_name="build",
                                        success=True,
                                        parameters={"action": "test"},
                                        observation="320 tests observed with 8 HelpFormatter failures.",
                                    )
                                ],
                            )
                        ],
                    )
                ],
            ),
        ],
        debug={"container": _COMMONS_WORKSPACE_ID, "entry": "CLI"},
    )


def _commons_module_metrics() -> dict[str, Any]:
    """The demo's per-module diagnostics, in the shape the card reads them."""

    return {
        "version": 1,
        "generated_at": "2026-06-06T02:16:40Z",
        "module_summary": _commons_module_summary().model_dump(mode="json"),
        "modules": [module.model_dump(mode="json") for module in _commons_modules()],
    }


def _commons_run_record() -> dict[str, Any]:
    """The verdict record a run like this one would have written.

    The demo detail shows a Maven reactor that compiled all three modules while
    eight HelpFormatter tests kept failing, so this record states exactly those
    numbers: the card is derived from it the same way a real run's card is, and
    a demo card that disagreed with the panels beside it would be worse than no
    demo card at all.
    """

    counts = {"executed": 320, "passed": 312, "failed": 8, "errors": 0, "skipped": 0}
    return {
        "schema_version": 5,
        "run_id": _COMMONS_RUN_ID,
        "finalized_at": "2026-06-06T02:16:44Z",
        "input_refs": [],
        "verdict": "partial",
        "build_evidence": {
            "observed": True,
            "green": True,
            "judgment": "success",
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "refs": ["output_demo_build"],
            "compiled_classes": 180,
            "source_files": 96,
            "reactor_modules_succeeded": 3,
            "reactor_modules_total": 3,
        },
        "test_stats": {
            "discovered": 320,
            "denominator_basis": "complete",
            "unique": dict(counts),
            "raw": dict(counts),
            "flaky_count": 0,
            "judgment": "success",
            "collection_errors": 0,
            "receipt_scoped": True,
        },
        "rates": {
            "build": {
                "modules": {
                    "numerator": 3,
                    "denominator": 3,
                    "rate": 100.0,
                    "band": "fully",
                },
                "classes": {
                    "band": "unavailable",
                    "reason": "demo fixture keeps no class census",
                },
            },
            "test": {
                "cases": {
                    "numerator": 312,
                    "denominator": 320,
                    "rate": 97.5,
                    "band": "mostly",
                },
                "modules": {
                    "numerator": 2,
                    "denominator": 3,
                    "rate": 66.7,
                    "band": "mostly",
                },
            },
            "coverage": {
                "status": "collected",
                "line_rate": 81.7,
                "source": "jacoco-injected",
            },
        },
        "conflicts": [],
        "phase_records": [],
    }


def _commons_receipts() -> list[ReceiptSummary]:
    """The two commands this demo run would have recorded receipts for.

    Kept in step with the demo record beside it: the same three modules the
    build row counts, and the same 320 executions the tests row states.
    """

    return [
        ReceiptSummary(
            receipt_id="inv-maven-1-demo0commonscli-0001",
            tool="maven",
            argv="/opt/apache-maven-3.9.9/bin/mvn -B clean install -DskipTests",
            working_directory="/workspace/commons-cli",
            actual_cwd="/workspace/commons-cli",
            exit_code=0,
            outcome="completed",
            lifecycle_state="finished",
            toolchain={
                "executable": "/opt/apache-maven-3.9.9/bin/mvn",
                "version": "Apache Maven 3.9.9",
            },
            jdk_major="17",
            jdk_version="17.0.20",
        ),
        ReceiptSummary(
            receipt_id="inv-maven-1-demo0commonscli-0002",
            tool="maven",
            argv="/opt/apache-maven-3.9.9/bin/mvn -B test",
            working_directory="/workspace/commons-cli",
            actual_cwd="/workspace/commons-cli",
            exit_code=1,
            outcome="completed",
            lifecycle_state="finished",
            toolchain={
                "executable": "/opt/apache-maven-3.9.9/bin/mvn",
                "version": "Apache Maven 3.9.9",
            },
            jdk_major="17",
            jdk_version="17.0.20",
            reports_new=3,
            tests_reported=320,
        ),
    ]


def _commons_workspace_result() -> WorkspaceResult:
    """The rail's cells, built by the same function the live rail uses."""

    from sag.agent.verdict_finalizer import RunVerdictSnapshot
    from sag.web.session_registry import _workspace_result

    result = _workspace_result(
        _commons_result_card().model_dump(mode="json"),
        RunVerdictSnapshot.model_validate(_commons_run_record()),
    )
    assert result is not None  # the demo record always builds a card
    return result


def _commons_result_card() -> RunResultCard:
    """The one result card — the CLI block and the report table print this shape."""

    report = _commons_report()
    return build_result_card(
        _commons_run_record(),
        module_metrics=_commons_module_metrics(),
        termination=RunTermination(
            termination=RunTerminationStatus.COMPLETED,
            report_delivery_status=ReportDeliveryStatus.DELIVERED,
        ),
        # The demo detail already states the model, the step count and the
        # wall clock; the card states those three rather than reporting the
        # run's counts unavailable beside panels that show them.
        run_pin={"action_model": "claude-sonnet-4.5"},
        trajectory_session={"wall_clock_seconds": 156.0},
        turn_count=6,
        project="apache/commons-cli",
        goal="Run full test suite and summarize HelpFormatter failures",
        container=_COMMONS_WORKSPACE_ID,
        report_path=report.path,
    )


def _commons_report() -> ReportDocument:
    return ReportDocument(
        title="setup-report-2026-06-06T0216.md",
        path=".setup_agent/reports/setup-report-2026-06-06T0216.md",
        generated="2026-06-06T02:16:44Z",
        blocks=[
            {
                "type": "summary",
                "heading": "Validation result",
                "body": "commons-cli 1.6.0 setup is usable, with 8 HelpFormatter line-wrapping tests still failing.",
            },
            {
                "type": "evidence",
                "heading": "Evidence sources",
                "body": "Project analyzer and Test validator captured width 74 vs 80 HelpFormatter evidence.",
            },
        ],
    )


def build_demo_dashboard() -> DashboardResponse:
    docker = DockerSummary(
        status="connected",
        image="setup-agent/workbench:demo",
        version="26.06",
        endpoint="unix:///var/run/docker.sock",
    )
    workspace_docker = DockerSummary(
        status="running",
        image=docker.image,
        version=docker.version,
        endpoint=docker.endpoint,
    )
    workspace = WorkspaceSummary(
        id=_COMMONS_WORKSPACE_ID,
        project="apache/commons-cli",
        container=_COMMONS_WORKSPACE_ID,
        stack="Java · Maven",
        tag="rel/commons-cli-1.6.0",
        release="1.6.0",
        commit="rel/commons-cli-1.6.0",
        docker=workspace_docker,
        task="Run full test suite and summarize HelpFormatter failures",
        evidence_status="partial",
        build=_commons_build_summary(),
        test=_commons_test_summary(),
        report="ready",
        changed=2,
        active_session=_COMMONS_SESSION_ID,
        latest_session=_COMMONS_SESSION_ID,
        result=_commons_workspace_result(),
        updated="2026-06-06 02:16",
    )
    return DashboardResponse(docker=docker, workspaces=[workspace])


def get_demo_session(session_id: str) -> ExecutionSessionDetail:
    if session_id != _COMMONS_SESSION_ID:
        raise KeyError(session_id)

    return ExecutionSessionDetail(
        id=_COMMONS_SESSION_ID,
        workspace=_COMMONS_WORKSPACE_ID,
        title="Run full test suite and summarize HelpFormatter failures",
        status="completed",
        evidence_status="partial",
        entry="CLI",
        start="02:14:08",
        duration="2m 36s",
        outcome="Test suite completed with HelpFormatter line-wrapping failures: expected width 74, observed width 80.",
        build=_commons_build_summary(),
        test=_commons_test_summary(),
        modules=_commons_modules(),
        module_summary=_commons_module_summary(),
        report="ready",
        report_doc=_commons_report(),
        evidence=_commons_evidence(),
        context=_commons_context(),
        logs=[
            "02:14:08 workspace sag-commons-cli attached",
            "02:14:18 project analyzer started",
            "02:16:41 mvn test completed: 312 passed, 8 HelpFormatter width failures",
        ],
        partial=True,
        # This session never ran, so no ledger was ever written for it and no
        # trajectory can be derived. The detail says so rather than letting the
        # timeline offer a panel that could only answer "unavailable".
        demo=True,
        result_card=_commons_result_card(),
        receipts=_commons_receipts(),
        rates={
            "build": {
                "modules": {
                    "numerator": 3,
                    "denominator": 3,
                    "rate": 100.0,
                    "band": "fully",
                }
            }
        },
        model="claude-sonnet-4.5",
        steps=6,
        step_budget=40,
    )


def get_demo_terminal(workspace_id: str) -> TerminalConnectionState:
    return TerminalConnectionState(
        container=workspace_id,
        cwd="/workspace/commons-cli",
        status="connected",
        tty="120 × 32",
        lines=[
            {"time": "02:14:08", "text": f"connected to {workspace_id}"},
            {"time": "02:16:41", "text": "mvn test completed with HelpFormatter width failures"},
        ],
    )
