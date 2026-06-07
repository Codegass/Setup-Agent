"""Pydantic read models consumed by the SAG Workbench frontend."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WebModel(BaseModel):
    __test__: ClassVar[bool] = False

    model_config = ConfigDict(populate_by_name=True)


class DockerSummary(WebModel):
    status: str
    image: str | None = None
    version: str | None = None
    endpoint: str | None = None


class BuildSummary(WebModel):
    state: str = "none"
    tool: str = "—"
    time: str = "—"
    artifact: str | None = None
    note: str = ""


class TestSummary(WebModel):
    state: str = "none"
    pass_count: int = Field(default=0, serialization_alias="pass")
    fail_count: int = Field(default=0, serialization_alias="fail")
    skip_count: int = Field(default=0, serialization_alias="skip")
    total: int = 0
    note: str = ""


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


class ContextTask(WebModel):
    id: str
    title: str
    status: str
    summary: str = ""
    refs: list[ContextReference] = Field(default_factory=list)
    recovered: bool = False

    @field_validator("refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [
            {"ref": str(ref), "label": str(ref), "kind": "reference"}
            if isinstance(ref, str)
            else ref
            for ref in value
        ]


class TrunkSummary(WebModel):
    goal: str
    state: str
    progress: dict[str, int]
    summary: str = ""


class ActiveBranchSummary(WebModel):
    task: str = ""
    why: str = ""
    memory: list[str] = Field(default_factory=list)
    last_refs: list[dict[str, str]] = Field(default_factory=list, serialization_alias="lastRefs")
    pressure: float = 0.0


class ContextMap(WebModel):
    trunk: TrunkSummary
    tasks: list[ContextTask] = Field(default_factory=list)
    active_branch: ActiveBranchSummary = Field(
        default_factory=ActiveBranchSummary, serialization_alias="activeBranch"
    )
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
    entry: str
    start: str
    finish: str | None = None
    duration: str
    build: str
    test: TestSummary
    report: str
    files: int
    evidence: int


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


class ExecutionSessionDetail(WebModel):
    id: str
    workspace: str
    title: str
    status: str
    entry: str
    start: str
    duration: str
    outcome: str
    build: BuildSummary
    test: TestSummary
    report: str
    report_doc: ReportDocument | None = Field(default=None, serialization_alias="reportDoc")
    blocker: BlockerSummary | None = None
    evidence: list[EvidenceGroup] = Field(default_factory=list)
    files: FileChangeDigest | None = None
    context: ContextMap | None = None
    logs: list[str] = Field(default_factory=list)
    partial: bool = False


class TerminalConnectionState(WebModel):
    container: str
    cwd: str = "/workspace"
    status: str
    tty: str = "120 × 32"
    lines: list[dict[str, str]] = Field(default_factory=list)


class DashboardResponse(WebModel):
    docker: DockerSummary
    workspaces: list[WorkspaceSummary]
