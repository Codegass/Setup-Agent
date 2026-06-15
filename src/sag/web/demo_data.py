"""Deterministic demo read models for the SAG Workbench skeleton UI."""

from __future__ import annotations

from sag.web.models import (
    BuildSummary,
    ContextTrace,
    ContextTraceAction,
    ContextTraceIteration,
    ContextTracePhase,
    ContextTraceTask,
    ContextTraceTrunk,
    DashboardResponse,
    DockerSummary,
    EvidenceGroup,
    EvidenceRecord,
    ExecutionSessionDetail,
    FileChangeCounts,
    FileChangeDigest,
    FileChangeItem,
    FileSnapshotRef,
    ModuleRollup,
    ModuleSummary,
    ReportDocument,
    TerminalConnectionState,
    TestSummary,
    WorkspaceSummary,
)

_COMMONS_WORKSPACE_ID = "sag-commons-cli"
_COMMONS_SESSION_ID = "CC-3"


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
        module_output_count=1,
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
    return [
        ModuleSummary(
            name="connect:runtime",
            path="connect/runtime",
            build_status="failure",
            build_source="reactor",
            class_count=None,
            jar_count=None,
            build_error_samples=[
                "[ERROR] WorkerSinkTask.java:[412,7] cannot find symbol",
            ],
            test_source="none",
            failing_names=[],
            failing_count=None,
            evidence_refs=["/workspace/connect/runtime/target"],
        ),
        ModuleSummary(
            name="streams",
            path="streams",
            build_status="success",
            build_source="reactor",
            class_count=2610,
            jar_count=18,
            tests_total=1240,
            tests_passed=1238,
            tests_failed=2,
            tests_errors=0,
            tests_skipped=0,
            test_source="runner_xml",
            failing_names=[
                "org.apache.kafka.streams.StreamThreadTest.shouldShutdown",
                "org.apache.kafka.streams.state.RocksDBStoreTest.shouldFlush",
            ],
            failing_count=2,
            evidence_refs=["/workspace/streams/build/test-results"],
        ),
        ModuleSummary(
            name="clients",
            path="clients",
            build_status="success",
            build_source="reactor",
            class_count=3140,
            jar_count=22,
            tests_total=1420,
            tests_passed=1420,
            tests_failed=0,
            tests_errors=0,
            tests_skipped=0,
            test_source="runner_xml",
            failing_names=[],
            failing_count=0,
            evidence_refs=["/workspace/clients/build/test-results"],
        ),
    ]


def _commons_module_summary() -> ModuleRollup:
    return ModuleRollup(
        modules_total=3,
        modules_built=2,
        modules_failed=1,
        modules_skipped=0,
        modules_with_test_failures=1,
        build_systems=["maven"],
        single_module=False,
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


def _commons_files() -> FileChangeDigest:
    return FileChangeDigest(
        snapshot=FileSnapshotRef(
            base="HEAD",
            head="workspace-scan-2026-06-06T0216",
            mode="demo",
        ),
        counts=FileChangeCounts(modified=1, added=1, deleted=0, renamed=0),
        items=[
            FileChangeItem(
                path="pom.xml",
                change="modified",
                size="18 KB",
                mtime="02:15:02",
                note="commons-cli 1.6.0 dependency and plugin metadata inspected for setup.",
            ),
            FileChangeItem(
                path=".setup_agent/env_overlay.json",
                change="added",
                size="2 KB",
                mtime="02:15:36",
                note="Container environment overlay generated for reproducible commands.",
            ),
        ],
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
        files=_commons_files(),
        context=_commons_context(),
        logs=[
            "02:14:08 workspace sag-commons-cli attached",
            "02:14:18 project analyzer started",
            "02:16:41 mvn test completed: 312 passed, 8 HelpFormatter width failures",
        ],
        partial=True,
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
