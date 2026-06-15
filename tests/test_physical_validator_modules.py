# tests/test_physical_validator_modules.py
from sag.agent.physical_validator import PhysicalValidator


class FakeOrch:
    def __init__(self, responses):
        self.responses = responses  # dict: substring -> {"success","output","exit_code"}

    def execute_command(self, command, **kwargs):
        for needle, resp in self.responses.items():
            if needle in command:
                return {"success": True, "exit_code": 0, **resp}
        return {"success": True, "exit_code": 0, "output": ""}


def test_scan_modules_maven_counts_artifacts_and_report_dirs():
    responses = {
        "-name 'pom.xml'": {"output": "/w/p/connect/api/pom.xml\n/w/p/core/pom.xml"},
        "/connect/api/target/classes": {"output": "180"},
        "/connect/api/target' -name '*.jar": {"output": "3"},
        "/core/target/classes": {"output": "50"},
        "/core/target' -name '*.jar": {"output": "1"},
        "/connect/api/target/surefire-reports": {"output": "EXISTS"},
        "/core/target/surefire-reports": {"output": "EXISTS"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/p", "maven")
    by_path = {m["path"]: m for m in modules}
    assert by_path["connect/api"]["name"] == "connect:api"
    assert by_path["connect/api"]["class_count"] == 180
    assert by_path["connect/api"]["jar_count"] == 3
    assert any("surefire" in d for d in by_path["connect/api"]["report_dirs"])
    assert by_path["core"]["class_count"] == 50


def test_scan_modules_single_module_returns_root():
    v = PhysicalValidator(docker_orchestrator=FakeOrch({"-name 'pom.xml'": {"output": ""}}))
    modules = v.scan_modules("/w/solo", "maven")
    assert len(modules) == 1 and modules[0]["path"] == "."
