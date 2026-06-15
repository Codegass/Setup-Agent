import json

from sag.tools.report_metrics import METRICS_PATH
from sag.tools.report_tool import ReportTool


class CapturingOrchestrator:
    def __init__(self):
        self.writes = {}

    def execute_command(self, command, **kwargs):
        # Capture here-doc writes: "cat > <path> << 'EOF...'\n<body>\nEOF..."
        if command.startswith("cat > ") and "<<" in command:
            path = command.split("cat > ", 1)[1].split(" <<", 1)[0].strip()
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
