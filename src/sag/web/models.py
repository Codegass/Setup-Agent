"""Pydantic read models consumed by the SAG Workbench frontend."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class WebModel(BaseModel):
    __test__: ClassVar[bool] = False

    model_config = ConfigDict(populate_by_name=True)


class DockerSummary(WebModel):
    status: str
    image: str | None = None
    version: str | None = None
    endpoint: str | None = None


class SystemSummary(WebModel):
    """Host + docker resource usage for the nav bar. Any field is None when its
    source is unavailable (docker down, non-Linux host)."""

    docker_disk_used: int | None = Field(default=None, serialization_alias="dockerDiskUsed")
    docker_reclaimable: int | None = Field(default=None, serialization_alias="dockerReclaimable")
    mem_used: int | None = Field(default=None, serialization_alias="memUsed")
    mem_total: int | None = Field(default=None, serialization_alias="memTotal")
    cpu_load: float | None = Field(default=None, serialization_alias="cpuLoad")


class BuildSummary(WebModel):
    state: str = "none"
    tool: str = "—"
    time: str = "—"
    artifact: str | None = None
    note: str = ""
    # Structured build evidence (spec data contract). None = uncomputable.
    system: str | None = None
    class_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("class_count", "classCount"),
        serialization_alias="classCount",
    )
    jar_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("jar_count", "jarCount"),
        serialization_alias="jarCount",
    )
    module_output_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("module_output_count", "moduleOutputCount"),
        serialization_alias="moduleOutputCount",
    )
    artifact_samples: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("artifact_samples", "artifactSamples"),
        serialization_alias="artifactSamples",
    )
    warnings: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence_refs", "evidenceRefs"),
        serialization_alias="evidenceRefs",
    )

    @field_validator("note", mode="before")
    @classmethod
    def _coerce_note(cls, value: Any) -> str:
        # The metrics path emits note=None when no build command is known
        # (older runs, Gradle). Coerce to "" so model_validate succeeds and the
        # structured build fields survive instead of falling back to defaults.
        return "" if value is None else value


class TestSummary(WebModel):
    state: str = "none"
    pass_count: int = Field(default=0, serialization_alias="pass")
    fail_count: int = Field(default=0, serialization_alias="fail")
    skip_count: int = Field(default=0, serialization_alias="skip")
    total: int = 0
    pass_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("pass_rate", "passRate"),
        serialization_alias="passRate",
    )
    execution_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("execution_rate", "executionRate"),
        serialization_alias="executionRate",
    )
    note: str = ""
    errors: int = 0
    report_file_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("report_file_count", "reportFileCount"),
        serialization_alias="reportFileCount",
    )
    unique_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("unique_total", "uniqueTotal"),
        serialization_alias="uniqueTotal",
    )
    unique_passed: int | None = Field(
        default=None,
        validation_alias=AliasChoices("unique_passed", "uniquePassed"),
        serialization_alias="uniquePassed",
    )
    unique_failed: int | None = Field(
        default=None,
        validation_alias=AliasChoices("unique_failed", "uniqueFailed"),
        serialization_alias="uniqueFailed",
    )
    unique_errors: int | None = Field(
        default=None,
        validation_alias=AliasChoices("unique_errors", "uniqueErrors"),
        serialization_alias="uniqueErrors",
    )
    unique_skipped: int | None = Field(
        default=None,
        validation_alias=AliasChoices("unique_skipped", "uniqueSkipped"),
        serialization_alias="uniqueSkipped",
    )
    raw_executions: int | None = Field(
        default=None,
        validation_alias=AliasChoices("raw_executions", "rawExecutions"),
        serialization_alias="rawExecutions",
    )
    declared_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("declared_total", "declaredTotal"),
        serialization_alias="declaredTotal",
    )
    method_execution_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("method_execution_rate", "methodExecutionRate"),
        serialization_alias="methodExecutionRate",
    )
    failing_names: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("failing_names", "failingNames"),
        serialization_alias="failingNames",
    )
    conflicts: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence_refs", "evidenceRefs"),
        serialization_alias="evidenceRefs",
    )


class EvidenceRecord(WebModel):
    time: str
    status: str
    title: str
    detail: str
    ref: str


class EvidenceGroup(WebModel):
    source: str
    status: str
    counts: str
    time: str
    summary: str
    records: list[EvidenceRecord] = Field(default_factory=list)


class FileChangeItem(WebModel):
    path: str
    change: Literal["added", "modified", "deleted", "renamed"]
    type: Literal["file", "dir", "other"] = "file"
    size: str = "—"
    mtime: str = "—"
    note: str = ""


class FileChangeCounts(WebModel):
    modified: int = 0
    added: int = 0
    deleted: int = 0
    renamed: int = 0


class FileSnapshotRef(WebModel):
    base: str
    head: str
    mode: str


class FileChangeDigest(WebModel):
    snapshot: FileSnapshotRef
    counts: FileChangeCounts
    items: list[FileChangeItem] = Field(default_factory=list)


class ContextReference(WebModel):
    ref: str
    label: str
    kind: str = "output"
    tool: str | None = None
    task_id: str | None = Field(default=None, serialization_alias="taskId")
    timestamp: str | None = None
    content: str | None = None
    content_length: int | None = Field(default=None, serialization_alias="contentLength")


class ContextTraceWindow(WebModel):
    total_chars: int = Field(default=0, serialization_alias="totalChars")
    step_span: int | None = Field(default=None, serialization_alias="stepSpan")
    segments: dict[str, Any] = Field(default_factory=dict)
    delta: dict[str, Any] = Field(default_factory=dict)
    intro_text: str | None = Field(default=None, serialization_alias="introText")
    ledger_text: str | None = Field(default=None, serialization_alias="ledgerText")


class ContextTraceAction(WebModel):
    tool_name: str = Field(serialization_alias="toolName")
    success: bool | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    output: str = ""
    observation: str = ""
    refs: list[ContextReference] = Field(default_factory=list)
    dispatch_status: str | None = Field(default=None, serialization_alias="dispatchStatus")


class ContextTraceIteration(WebModel):
    iteration: int | None = None
    sequence: int
    thoughts: list[str] = Field(default_factory=list)
    actions: list[ContextTraceAction] = Field(default_factory=list)
    window: ContextTraceWindow | None = None


class ContextTraceTask(WebModel):
    id: str
    title: str
    status: str
    iterations: list[ContextTraceIteration] = Field(default_factory=list)


class ContextTracePhase(WebModel):
    id: str
    name: str
    title: str
    status: str
    notes: str = ""
    key_results: str = Field(default="", serialization_alias="keyResults")
    evidence_status: str = Field(
        default="unknown",
        validation_alias=AliasChoices("evidence_status", "evidenceStatus"),
        serialization_alias="evidenceStatus",
    )
    evidence_refs: list[ContextReference] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence_refs", "evidenceRefs"),
        serialization_alias="evidenceRefs",
    )
    conflicts: list[str] = Field(default_factory=list)
    refs: list[ContextReference] = Field(default_factory=list)
    progress: dict[str, int] = Field(default_factory=dict)
    tasks: list[ContextTraceTask] = Field(default_factory=list)


class ContextTraceTrunk(WebModel):
    goal: str
    state: str
    progress: dict[str, int]
    summary: str = ""


class ContextTrace(WebModel):
    trunk: ContextTraceTrunk
    phases: list[ContextTracePhase] = Field(default_factory=list)
    debug: dict[str, Any] = Field(default_factory=dict)


class ReportDocument(WebModel):
    title: str
    path: str | None = None
    generated: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class ExecutionSessionSummary(WebModel):
    id: str
    workspace: str
    title: str
    status: str
    evidence_status: str = Field(
        default="unknown",
        validation_alias=AliasChoices("evidence_status", "evidenceStatus"),
        serialization_alias="evidenceStatus",
    )
    entry: str
    start: str
    finish: str | None = None
    duration: str
    build: str
    test: TestSummary
    report: str
    files: int
    evidence: int
    canonical_verdict: str = Field(default="unknown", serialization_alias="canonicalVerdict")
    snapshot_status: str = Field(default="unavailable", serialization_alias="snapshotStatus")
    legacy: bool = False
    report_delivery_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("report_delivery_status", "reportDeliveryStatus"),
        serialization_alias="reportDeliveryStatus",
    )


class WorkspaceSummary(WebModel):
    id: str
    project: str
    container: str
    stack: str = "Unknown"
    tag: str | None = None
    release: str | None = None
    commit: str | None = None
    docker: DockerSummary
    task: str = "No current task"
    evidence_status: str = Field(
        default="unknown",
        validation_alias=AliasChoices("evidence_status", "evidenceStatus"),
        serialization_alias="evidenceStatus",
    )
    build: BuildSummary | str = "none"
    test: TestSummary = Field(default_factory=TestSummary)
    report: str = "none"
    changed: int = 0
    active_session: str | None = Field(default=None, serialization_alias="activeSession")
    latest_session: str | None = Field(default=None, serialization_alias="latestSession")
    sessions: list[ExecutionSessionSummary] = Field(default_factory=list)
    updated: str = "unknown"


class BlockerSummary(WebModel):
    code: str
    title: str
    detail: str
    hint: str


class ModuleSummary(WebModel):
    name: str = ""
    path: str = ""
    build_status: str = Field(
        default="unknown",
        validation_alias=AliasChoices("build_status", "buildStatus"),
        serialization_alias="buildStatus",
    )
    build_source: str = Field(
        default="none",
        validation_alias=AliasChoices("build_source", "buildSource"),
        serialization_alias="buildSource",
    )
    class_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("class_count", "classCount"),
        serialization_alias="classCount",
    )
    jar_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("jar_count", "jarCount"),
        serialization_alias="jarCount",
    )
    build_warnings: int | None = Field(
        default=None,
        validation_alias=AliasChoices("build_warnings", "buildWarnings"),
        serialization_alias="buildWarnings",
    )
    build_error_samples: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("build_error_samples", "buildErrorSamples"),
        serialization_alias="buildErrorSamples",
    )
    tests_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_total", "testsTotal"),
        serialization_alias="testsTotal",
    )
    tests_passed: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_passed", "testsPassed"),
        serialization_alias="testsPassed",
    )
    tests_failed: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_failed", "testsFailed"),
        serialization_alias="testsFailed",
    )
    tests_errors: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_errors", "testsErrors"),
        serialization_alias="testsErrors",
    )
    tests_skipped: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_skipped", "testsSkipped"),
        serialization_alias="testsSkipped",
    )
    test_source: str = Field(
        default="none",
        validation_alias=AliasChoices("test_source", "testSource"),
        serialization_alias="testSource",
    )
    failing_names: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("failing_names", "failingNames"),
        serialization_alias="failingNames",
    )
    failing_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("failing_count", "failingCount"),
        serialization_alias="failingCount",
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence_refs", "evidenceRefs"),
        serialization_alias="evidenceRefs",
    )
    line_covered: int | None = Field(
        default=None,
        validation_alias=AliasChoices("line_covered", "lineCovered"),
        serialization_alias="lineCovered",
    )
    line_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("line_total", "lineTotal"),
        serialization_alias="lineTotal",
    )
    line_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("line_rate", "lineRate"),
        serialization_alias="lineRate",
    )
    branch_covered: int | None = Field(
        default=None,
        validation_alias=AliasChoices("branch_covered", "branchCovered"),
        serialization_alias="branchCovered",
    )
    branch_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("branch_total", "branchTotal"),
        serialization_alias="branchTotal",
    )
    branch_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("branch_rate", "branchRate"),
        serialization_alias="branchRate",
    )
    coverage_source: str | None = Field(
        default=None,
        validation_alias=AliasChoices("coverage_source", "coverageSource"),
        serialization_alias="coverageSource",
    )


class ModuleRollup(WebModel):
    modules_total: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_total", "modulesTotal"),
        serialization_alias="modulesTotal",
    )
    modules_built: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_built", "modulesBuilt"),
        serialization_alias="modulesBuilt",
    )
    modules_failed: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_failed", "modulesFailed"),
        serialization_alias="modulesFailed",
    )
    modules_skipped: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_skipped", "modulesSkipped"),
        serialization_alias="modulesSkipped",
    )
    modules_with_test_failures: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_with_test_failures", "modulesWithTestFailures"),
        serialization_alias="modulesWithTestFailures",
    )
    build_systems: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("build_systems", "buildSystems"),
        serialization_alias="buildSystems",
    )
    single_module: bool = Field(
        default=False,
        validation_alias=AliasChoices("single_module", "singleModule"),
        serialization_alias="singleModule",
    )
    line_covered: int | None = Field(
        default=None,
        validation_alias=AliasChoices("line_covered", "lineCovered"),
        serialization_alias="lineCovered",
    )
    line_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("line_total", "lineTotal"),
        serialization_alias="lineTotal",
    )
    line_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("line_rate", "lineRate"),
        serialization_alias="lineRate",
    )
    branch_covered: int | None = Field(
        default=None,
        validation_alias=AliasChoices("branch_covered", "branchCovered"),
        serialization_alias="branchCovered",
    )
    branch_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("branch_total", "branchTotal"),
        serialization_alias="branchTotal",
    )
    branch_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("branch_rate", "branchRate"),
        serialization_alias="branchRate",
    )
    coverage_source: str | None = Field(
        default=None,
        validation_alias=AliasChoices("coverage_source", "coverageSource"),
        serialization_alias="coverageSource",
    )


class VerdictSummary(WebModel):
    tone: str  # "success" | "attention" | "failed"
    headline: str
    detail: str | None = None
    verdict: str | None = None
    source: str = "derived"


class ExecutionSessionDetail(WebModel):
    id: str
    workspace: str
    title: str
    status: str
    evidence_status: str = Field(
        default="unknown",
        validation_alias=AliasChoices("evidence_status", "evidenceStatus"),
        serialization_alias="evidenceStatus",
    )
    entry: str
    start: str
    duration: str
    outcome: str
    build: BuildSummary
    test: TestSummary
    modules: list[ModuleSummary] = Field(default_factory=list)
    module_summary: ModuleRollup | None = Field(
        default=None,
        validation_alias=AliasChoices("module_summary", "moduleSummary"),
        serialization_alias="moduleSummary",
    )
    report: str
    report_doc: ReportDocument | None = Field(default=None, serialization_alias="reportDoc")
    blocker: BlockerSummary | None = None
    evidence: list[EvidenceGroup] = Field(default_factory=list)
    files: FileChangeDigest | None = None
    context: ContextTrace | None = None
    logs: list[str] = Field(default_factory=list)
    partial: bool = False
    verdict: VerdictSummary | None = None
    model: str | None = None
    steps: int | None = None
    step_budget: int | None = Field(
        default=None,
        validation_alias=AliasChoices("step_budget", "stepBudget"),
        serialization_alias="stepBudget",
    )
    canonical_verdict: str = Field(default="unknown", serialization_alias="canonicalVerdict")
    snapshot_status: str = Field(default="unavailable", serialization_alias="snapshotStatus")
    legacy: bool = False
    report_delivery_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("report_delivery_status", "reportDeliveryStatus"),
        serialization_alias="reportDeliveryStatus",
    )


class TerminalConnectionState(WebModel):
    container: str
    cwd: str = "/workspace"
    status: str
    tty: str = "120 × 32"
    lines: list[dict[str, str]] = Field(default_factory=list)


class DashboardResponse(WebModel):
    docker: DockerSummary
    workspaces: list[WorkspaceSummary]
