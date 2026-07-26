import json

from sag.agent.verdict_finalizer import BuildEvidenceSnapshot, RunVerdictSnapshot
from sag.tools.module_metrics import MODULE_METRICS_CSV_PATH, MODULE_METRICS_PATH
from sag.tools.report_metrics import METRICS_PATH
from sag.tools.report_tool import ReportTool


class CapturingOrchestrator:
    def __init__(self):
        self.writes = {}

    def execute_command(self, command, **kwargs):
        # Capture here-doc writes: "cat > <path> << 'EOF...'\n<body>\nEOF..."
        # The write may be prefixed by a mkdir in the same command
        # ("mkdir -p <dir> && cat > <path> <<'EOF'"), so match anywhere.
        if "cat > " in command and "<<" in command:
            path = command.split("cat > ", 1)[1].split(" <<", 1)[0].split("<<", 1)[0].strip()
            body = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            self.writes[path] = body
        return {"exit_code": 0, "output": ""}


def test_persist_metrics_writes_json_artifact():
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)
    metrics = {"version": 1, "generated_at": "x",
               "build": {"state": "success"}, "test": {"total": 10}}

    tool._persist_report_metrics(metrics)

    assert METRICS_PATH in orch.writes
    parsed = json.loads(orch.writes[METRICS_PATH])
    assert parsed["build"]["state"] == "success"
    assert parsed["test"]["total"] == 10


def test_persist_metrics_no_orchestrator_is_safe():
    ReportTool(docker_orchestrator=None)._persist_report_metrics({"version": 1})  # no raise


def _sealed_snapshot():
    return RunVerdictSnapshot(
        run_id="run-1",
        finalized_at="2026-07-25T00:00:00Z",
        verdict="partial",
        build_evidence=BuildEvidenceSnapshot(
            observed=True,
            judgment="success",
            module_summary={"modules_total": 2, "modules_built": 1, "build_systems": ["maven"]},
            modules=(
                {"path": "core", "build_status": "success", "class_count": 5,
                 "java_file_count": 3, "report_file_count": 2, "tests_total": 7,
                 "tests_passed": 7, "failing_count": 0},
                {"path": "web", "build_status": "unknown"},
            ),
        ),
    )


def test_sealed_report_persists_module_metrics_json_and_csv():
    """The sealed path is the one every modern run takes. It previously wrote
    only report_metrics.json, so module_metrics.json/.csv never appeared -- while
    the sealed markdown told readers to look in module_metrics.json."""
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)

    tool._generate_snapshot_report(_sealed_snapshot(), summary="s", details="d")

    assert MODULE_METRICS_PATH in orch.writes, "sealed run wrote no module_metrics.json"
    assert MODULE_METRICS_CSV_PATH in orch.writes, "sealed run wrote no module_metrics.csv"

    parsed = json.loads(orch.writes[MODULE_METRICS_PATH])
    assert parsed["module_summary"]["modules_built"] == 1
    assert [m["path"] for m in parsed["modules"]] == ["core", "web"]

    rows = orch.writes[MODULE_METRICS_CSV_PATH].strip().splitlines()
    assert rows[0].startswith("module_path,build_system,report_file_count")
    # Sealed rows come from assemble_module_metrics at evidence-close, so the
    # scan-derived columns survive the round trip rather than defaulting to 0.
    assert rows[1].startswith("core,maven,2,7,7,")


def test_sealed_report_without_module_evidence_writes_no_module_artifacts():
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)
    bare = RunVerdictSnapshot(
        run_id="run-2", finalized_at="2026-07-25T00:00:00Z", verdict="unknown"
    )

    tool._generate_snapshot_report(bare, summary="s", details="d")

    assert MODULE_METRICS_PATH not in orch.writes
    assert MODULE_METRICS_CSV_PATH not in orch.writes
