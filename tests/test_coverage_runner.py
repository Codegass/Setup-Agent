# tests/test_coverage_runner.py
import json

from sag.coverage.runner import run_coverage, apply_coverage, JACOCO_VERSION


class FakeOrch:
    def __init__(self, files, listings=None):
        self.files = files            # path -> content (cat)
        self.listings = listings or {}  # substring -> find output
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat "):
            path = command[4:].strip().strip("'")
            return {"success": path in self.files, "exit_code": 0 if path in self.files else 1,
                    "output": self.files.get(path, "")}
        for needle, out in self.listings.items():
            if needle in command:
                return {"success": True, "exit_code": 0, "output": out}
        return {"success": True, "exit_code": 0, "output": ""}


REPORT = ('<report name="m"><counter type="LINE" missed="20" covered="80"/>'
          '<counter type="BRANCH" missed="30" covered="70"/></report>')


def test_reuses_existing_reports_without_running_build():
    # An existing jacoco.xml under a module -> parse, no test re-run.
    orch = FakeOrch(
        files={"/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml": REPORT},
        listings={"-name 'jacoco*.xml'": "/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml"},
    )
    cov = run_coverage(orch, "/w/p", build_system="gradle")
    assert cov["core"]["line_rate"] == 80.0
    assert cov["core"]["coverage_source"] == "jacoco-existing"
    # no test/build command was issued (reuse path)
    assert not any("jacocoTestReport" in c and "gradle" in c for c in orch.commands)


def test_injects_and_runs_when_no_existing_report_maven():
    # First listing (existing) empty -> inject+run, then second listing finds the produced report.
    calls = {"n": 0}

    class Orch(FakeOrch):
        def execute_command(self, command, **kwargs):
            if "-name 'jacoco.xml'" in command or "jacoco*.xml" in command:
                calls["n"] += 1
                # empty on the pre-check, populated after the run
                if calls["n"] == 1:
                    return {"success": True, "exit_code": 0, "output": ""}
                return {"success": True, "exit_code": 0,
                        "output": "/w/p/core/target/site/jacoco/jacoco.xml"}
            return super().execute_command(command, **kwargs)

    orch = Orch(files={"/w/p/core/target/site/jacoco/jacoco.xml": REPORT})
    cov = run_coverage(orch, "/w/p", build_system="maven")
    assert cov["core"]["coverage_source"] == "jacoco-injected"
    assert any(f"jacoco-maven-plugin:{JACOCO_VERSION}:prepare-agent" in c for c in orch.commands)
    # never edits project files
    assert not any("pom.xml" in c and (">" in c or "sed" in c) for c in orch.commands)


def test_apply_coverage_merges_into_container_metrics():
    metrics = {"version": 1, "module_summary": {"modules_total": 1},
               "modules": [{"name": "core", "path": "core", "build_status": "success"}]}
    written = {}

    class Orch(FakeOrch):
        def execute_command(self, command, **kwargs):
            # Write-back ("cat > .../module_metrics.json ...") must be matched
            # before the read branch, since "cat > " also startswith("cat ").
            if "module_metrics.json" in command and "cat >" in command:
                written["payload"] = command
                return {"success": True, "exit_code": 0, "output": ""}
            if command.startswith("cat ") and "module_metrics.json" in command:
                return {"success": True, "exit_code": 0, "output": json.dumps(metrics)}
            # A cat of an XML report must be matched before the find/listing
            # branch, since the report path also contains "jacoco" and "xml".
            if command.startswith("cat "):
                return {"success": True, "exit_code": 0, "output": REPORT}
            if "jacoco" in command and "xml" in command:
                return {"success": True, "exit_code": 0,
                        "output": "/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml"}
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch(files={})
    ok = apply_coverage(orch, "/w/p", build_system="gradle")
    assert ok is True
    assert "line_rate" in written["payload"]  # merged coverage written back
