# tests/test_coverage_runner.py
import json

import pytest
from test_container_io import FakeContainer as AtomicContainer

from sag.coverage.runner import (
    JACOCO_VERSION,
    _CoverageControlFailure,
    _module_path,
    apply_coverage,
    run_coverage,
)
from sag.tools.module_metrics import MODULE_METRICS_PATH


class FakeOrch:
    def __init__(self, files, listings=None):
        self.files = files  # path -> content (cat)
        self.listings = listings or {}  # substring -> find output
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat "):
            path = command[4:].strip().strip("'")
            return {
                "success": path in self.files,
                "exit_code": 0 if path in self.files else 1,
                "output": self.files.get(path, ""),
            }
        for needle, out in self.listings.items():
            if needle in command:
                return {"success": True, "exit_code": 0, "output": out}
        return {"success": True, "exit_code": 0, "output": ""}


REPORT = (
    '<report name="m"><counter type="LINE" missed="20" covered="80"/>'
    '<counter type="BRANCH" missed="30" covered="70"/></report>'
)


def test_reuses_existing_reports_without_running_build():
    # An existing jacoco.xml under a module -> parse, no test re-run.
    orch = FakeOrch(
        files={"/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml": REPORT},
        listings={
            "-name 'jacoco*.xml'": "/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml\0"
        },
    )
    cov = run_coverage(orch, "/w/p", build_system="gradle")
    assert cov["core"]["line_rate"] == 80.0
    assert cov["core"]["coverage_source"] == "jacoco-existing"
    # no test/build command was issued (reuse path)
    assert not any("jacocoTestReport" in c and "gradle" in c for c in orch.commands)


def test_maven_reuses_own_jacoco_via_report_only():
    """A Maven project shipping its own JaCoCo has jacoco.exec but no XML after
    setup. The runner must materialize the report with a report-only goal (no
    second prepare-agent agent -> avoids the double-agent StackOverflowError seen
    live on commons-cli) and treat the result as existing coverage."""
    report = (
        '<report name="m"><counter type="LINE" missed="10" covered="90"/>'
        '<counter type="BRANCH" missed="0" covered="0"/></report>'
    )
    state = {"xml_finds": 0}

    class Orch:
        def __init__(self):
            self.commands = []

        def execute_command(self, command, **kwargs):
            self.commands.append(command)
            if "find" in command and "jacoco.xml" in command:
                state["xml_finds"] += 1
                # no XML before report-only; XML present after it runs
                if state["xml_finds"] == 1:
                    return {"success": True, "exit_code": 0, "output": ""}
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": "/w/p/target/site/jacoco/jacoco.xml",
                }
            if "find" in command and "jacoco.exec" in command:
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": "/w/p/target/jacoco.exec\0",
                }
            if command.startswith("cat "):
                return {"success": True, "exit_code": 0, "output": report}
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch()
    cov = run_coverage(orch, "/w/p", build_system="maven")
    assert cov["."]["coverage_source"] == "jacoco-existing"
    assert cov["."]["line_rate"] == 90.0
    # report-only goal used; NO prepare-agent injected (no second agent)
    assert any(":report" in c for c in orch.commands)
    assert not any("prepare-agent" in c for c in orch.commands)


def test_injects_and_runs_when_no_existing_report_maven():
    # First listing (existing) empty -> inject+run, then second listing finds the produced report.
    calls = {"n": 0}

    class Orch(FakeOrch):
        def execute_command(self, command, **kwargs):
            if command.startswith("find ") and (
                "jacoco.xml" in command or "jacoco*.xml" in command
            ):
                calls["n"] += 1
                # empty on the pre-check, populated after the run
                if calls["n"] == 1:
                    return {"success": True, "exit_code": 0, "output": ""}
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": "/w/p/core/target/site/jacoco/jacoco.xml",
                }
            return super().execute_command(command, **kwargs)

    orch = Orch(files={"/w/p/core/target/site/jacoco/jacoco.xml": REPORT})
    cov = run_coverage(orch, "/w/p", build_system="maven")
    assert cov["core"]["coverage_source"] == "jacoco-injected"
    mvn_cmd = next(c for c in orch.commands if "prepare-agent" in c)
    assert f"jacoco-maven-plugin:{JACOCO_VERSION}:prepare-agent" in mvn_cmd
    # DockerOrchestrator injects the host-authorized environment; project shell
    # files never become executable coverage-runner authority.
    assert "env_overlay.sh" not in mvn_cmd
    assert "source /etc/profile" not in mvn_cmd
    assert "source ~/.bashrc" not in mvn_cmd
    # never edits project files
    assert not any("pom.xml" in c and (">" in c or "sed" in c) for c in orch.commands)


def test_apply_coverage_merges_into_container_metrics():
    metrics = {
        "version": 1,
        "module_summary": {"modules_total": 1},
        "modules": [{"name": "core", "path": "core", "build_status": "success"}],
    }
    class Orch:
        def __init__(self):
            self.atomic = AtomicContainer()
            self.files = self.atomic.files
            self.files[MODULE_METRICS_PATH] = json.dumps(metrics)
            self.control_kwargs = []

        def execute_control_command(self, command, **kwargs):
            self.control_kwargs.append(dict(kwargs))
            # A cat of an XML report must be matched before the find/listing
            # branch, since the report path also contains "jacoco" and "xml".
            if command.startswith("cat ") and "jacoco" in command:
                return {"success": True, "exit_code": 0, "output": REPORT}
            if "jacoco" in command and "xml" in command:
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": "/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml",
                }
            result = self.atomic.execute_control_command(command, **kwargs)
            if result.get("exit_code") == 0:
                result = {**result, "success": True}
            return result

    orch = Orch()
    ok = apply_coverage(orch, "/w/p", build_system="gradle")
    assert ok is True
    persisted = json.loads(orch.files[MODULE_METRICS_PATH])
    assert persisted["modules"][0]["line_rate"] == 80.0
    assert any(call.get("truncate_output") is False for call in orch.control_kwargs)
    assert max(map(len, orch.atomic.commands)) <= 60200


def test_apply_coverage_preserves_large_metrics_without_truncation(monkeypatch):
    modules = [
        {"name": "core", "path": "core", "build_status": "success"},
        *[
            {
                "name": f"module-{index}",
                "path": f"module-{index}",
                "build_status": "success",
                "detail": "x" * 128,
            }
            for index in range(800)
        ],
    ]
    metrics = {
        "version": 1,
        "module_summary": {"modules_total": len(modules)},
        "modules": modules,
    }
    monkeypatch.setattr(
        "sag.coverage.runner.run_coverage",
        lambda *_args, **_kwargs: {
            "core": {
                "line_rate": 80.0,
                "line_covered": 8,
                "line_total": 10,
                "branch_rate": None,
                "branch_covered": 0,
                "branch_total": 0,
                "coverage_source": "jacoco-existing",
            }
        },
    )

    class Orch:
        def __init__(self):
            self.atomic = AtomicContainer()
            self.files = self.atomic.files
            self.files[MODULE_METRICS_PATH] = json.dumps(metrics)
            self.calls = []

        def execute_control_command(self, command, **kwargs):
            self.calls.append((command, dict(kwargs)))
            result = self.atomic.execute_control_command(command, **kwargs)
            if result.get("exit_code") == 0:
                result = {**result, "success": True}
            return result

    orchestrator = Orch()

    assert apply_coverage(orchestrator, "/w/p", build_system="gradle") is True

    persisted = json.loads(orchestrator.files[MODULE_METRICS_PATH])
    assert len(persisted["modules"]) == len(modules)
    assert {module["path"] for module in persisted["modules"]} == {
        module["path"] for module in modules
    }
    assert persisted["modules"][0]["line_rate"] == 80.0
    assert max(map(len, orchestrator.atomic.commands)) <= 60200


def test_apply_coverage_preserves_exact_metrics_predecessor_with_terminal_newline(monkeypatch):
    metrics = {
        "version": 1,
        "module_summary": {"modules_total": 1},
        "modules": [{"name": "core", "path": "core", "build_status": "success"}],
    }
    monkeypatch.setattr(
        "sag.coverage.runner.run_coverage",
        lambda *_args, **_kwargs: {
            "core": {
                "line_rate": 80.0,
                "line_covered": 8,
                "line_total": 10,
                "branch_rate": None,
                "branch_covered": 0,
                "branch_total": 0,
                "coverage_source": "jacoco-existing",
            }
        },
    )

    class Orch:
        def __init__(self):
            self.atomic = AtomicContainer()
            self.files = self.atomic.files
            self.files[MODULE_METRICS_PATH] = json.dumps(metrics) + "\n"

        def execute_control_command(self, command, **kwargs):
            result = self.atomic.execute_control_command(command, **kwargs)
            if result.get("exit_code") == 0:
                result = {**result, "success": True}
            return result

    orchestrator = Orch()

    assert apply_coverage(orchestrator, "/w/p", build_system="gradle") is True
    assert json.loads(orchestrator.files[MODULE_METRICS_PATH])["modules"][0]["line_rate"] == 80.0


def test_coverage_evidence_io_prefers_clean_control_transport():
    class Orch:
        def __init__(self):
            self.normal = []
            self.control = []

        def execute_command(self, command, **kwargs):
            self.normal.append(command)
            return {"success": True, "exit_code": 0, "output": ""}

        def execute_control_command(self, command, **kwargs):
            self.control.append(command)
            if "find" in command and "jacoco*.xml" in command:
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": "/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml",
                }
            if command.startswith("cat "):
                return {"success": True, "exit_code": 0, "output": REPORT}
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch()

    coverage = run_coverage(orch, "/w/p", build_system="gradle")

    assert coverage["core"]["line_rate"] == 80.0
    assert orch.normal == []
    assert any("find" in command for command in orch.control)
    assert any(command.startswith("cat ") for command in orch.control)


def test_apply_coverage_reports_a_failed_metrics_write(monkeypatch):
    monkeypatch.setattr(
        "sag.coverage.runner.run_coverage",
        lambda *_args, **_kwargs: {
            "core": {
                "line_rate": 80.0,
                "line_covered": 8,
                "line_total": 10,
                "branch_rate": None,
                "branch_covered": 0,
                "branch_total": 0,
            }
        },
    )

    class Orch:
        def execute_control_command(self, command, **kwargs):
            if command.startswith("cat ") and "cat >" not in command:
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": json.dumps(
                        {
                            "version": 1,
                            "module_summary": {"modules_total": 1},
                            "modules": [
                                {"name": "core", "path": "core", "build_status": "success"}
                            ],
                        }
                    ),
                }
            return {"success": False, "exit_code": 97, "output": "write rejected"}

    assert apply_coverage(Orch(), "/w/p", build_system="gradle") is False


def test_maven_control_discovery_failure_dispatches_no_normal_runner():
    class Orch:
        def __init__(self):
            self.control_calls = []
            self.normal_calls = []

        def execute_control_command(self, command, **kwargs):
            self.control_calls.append(command)
            return {"success": False, "exit_code": 73, "output": "find failed"}

        def execute_command(self, command, **kwargs):
            self.normal_calls.append(command)
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch()

    assert run_coverage(orch, "/w/p", build_system="maven") == {}
    assert orch.normal_calls == []


def test_gradle_init_control_write_failure_dispatches_no_normal_runner():
    class Orch:
        def __init__(self):
            self.control_calls = []
            self.normal_calls = []

        def execute_control_command(self, command, **kwargs):
            self.control_calls.append(command)
            if command.startswith("find "):
                return {"success": True, "exit_code": 0, "output": ""}
            return {"success": False, "exit_code": 74, "output": "write failed"}

        def execute_command(self, command, **kwargs):
            self.normal_calls.append(command)
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch()

    assert run_coverage(orch, "/w/p", build_system="gradle") == {}
    assert orch.normal_calls == []
    assert any(".setup_agent_jacoco.init.gradle" in call for call in orch.control_calls)


@pytest.mark.parametrize(
    "forged_path",
    [
        "/outside/core/build/reports/jacoco/test/jacocoTestReport.xml",
        "/w/p/core/build/reports/jacoco/test/evil'$(touch /tmp/pwn).xml",
        '/w/p/core/build/reports/jacoco/test/evil"quote.xml',
        "/w/p/core/build/reports/jacoco/test/evil\nname.xml",
        "/w/p/../escape/build/reports/jacoco/test/jacocoTestReport.xml",
    ],
)
def test_rejects_noncanonical_or_injectable_find_report_paths(forged_path):
    class Orch:
        def __init__(self):
            self.control_calls = []
            self.normal_calls = []

        def execute_control_command(self, command, **kwargs):
            self.control_calls.append(command)
            if command.startswith("find "):
                return {"success": True, "exit_code": 0, "output": forged_path}
            return {"success": True, "exit_code": 0, "output": REPORT}

        def execute_command(self, command, **kwargs):
            self.normal_calls.append(command)
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch()

    assert run_coverage(orch, "/w/p", build_system="gradle") == {}
    assert orch.normal_calls == []
    assert not any(command.startswith("cat ") for command in orch.control_calls)


def test_coverage_shell_quotes_project_and_report_paths():
    project_dir = "/w/project with spaces"
    report_path = f"{project_dir}/core/build/reports/jacoco/test/jacoco report.xml"

    class Orch:
        def __init__(self):
            self.control_calls = []

        def execute_control_command(self, command, **kwargs):
            self.control_calls.append(command)
            if command.startswith("find "):
                return {"success": True, "exit_code": 0, "output": report_path}
            return {"success": True, "exit_code": 0, "output": REPORT}

    orch = Orch()

    coverage = run_coverage(orch, project_dir, build_system="gradle")

    assert coverage["core"]["line_rate"] == 80.0
    assert any("find '/w/project with spaces'" in call for call in orch.control_calls)
    assert any("cat '/w/project with spaces/" in call for call in orch.control_calls)


def test_apply_coverage_reads_metrics_before_any_normal_runner():
    class Orch:
        def __init__(self):
            self.control_calls = []
            self.normal_calls = []

        def execute_control_command(self, command, **kwargs):
            self.control_calls.append(command)
            if "module_metrics.json" in command:
                return {"success": False, "exit_code": 75, "output": "read failed"}
            return {"success": True, "exit_code": 0, "output": ""}

        def execute_command(self, command, **kwargs):
            self.normal_calls.append(command)
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch()

    assert apply_coverage(orch, "/w/p", build_system="maven") is False
    assert orch.normal_calls == []


@pytest.mark.parametrize(
    ("project_root", "report_path", "build_system", "expected_module"),
    [
        (
            "/workspace/build/checkout",
            "/workspace/build/checkout/core/build/reports/jacoco/test/jacocoTestReport.xml",
            "gradle",
            "core",
        ),
        (
            "/workspace/target/checkout",
            "/workspace/target/checkout/target/site/jacoco/jacoco.xml",
            "maven",
            ".",
        ),
        (
            "/workspace/project",
            "/workspace/project/generated/build/cache/core/build/reports/jacoco/test/jacocoTestReport.xml",
            "gradle",
            "generated/build/cache/core",
        ),
        (
            "/workspace/project",
            "/workspace/project/generated/target/cache/core/target/site/jacoco/jacoco.xml",
            "maven",
            "generated/target/cache/core",
        ),
    ],
)
def test_module_path_uses_last_marker_after_relativizing_to_project_root(
    project_root,
    report_path,
    build_system,
    expected_module,
):
    assert _module_path(project_root, report_path, build_system) == expected_module


def test_module_path_does_not_reuse_marker_from_project_root():
    with pytest.raises(_CoverageControlFailure, match="output marker"):
        _module_path(
            "/workspace/build/checkout",
            "/workspace/build/checkout/reports/jacoco/jacocoTestReport.xml",
            "gradle",
        )


def test_module_path_rejects_report_outside_project_root():
    with pytest.raises(_CoverageControlFailure, match="outside the project root"):
        _module_path(
            "/workspace/build/checkout",
            "/workspace/build/other/core/build/reports/jacoco/test/jacocoTestReport.xml",
            "gradle",
        )
