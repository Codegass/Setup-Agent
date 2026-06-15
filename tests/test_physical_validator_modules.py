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


def test_parse_module_test_reports_counts_per_module():
    surefire_xml = (
        '<testsuite tests="3" failures="1" errors="0" skipped="1">'
        '<testcase classname="com.x.FooTest" name="ok"/>'
        '<testcase classname="com.x.FooTest" name="bad"><failure/></testcase>'
        '<testcase classname="com.x.FooTest" name="ign"><skipped/></testcase>'
        '</testsuite>'
    )

    class Orch:
        def execute_command(self, command, **kwargs):
            if "cat" in command and "surefire" in command:
                return {"success": True, "exit_code": 0, "output": surefire_xml}
            if "find" in command and "surefire" in command:
                return {"success": True, "exit_code": 0,
                        "output": "/w/m/target/surefire-reports/TEST-com.x.FooTest.xml"}
            return {"success": True, "exit_code": 0, "output": ""}

    v = PhysicalValidator(docker_orchestrator=Orch())
    res = v.parse_module_test_reports("/w/m", ["/w/m/target/surefire-reports"])
    assert res["tests_total"] == 3
    assert res["tests_failed"] == 1
    assert res["tests_skipped"] == 1
    assert res["failing_count"] == 1
    assert any("FooTest" in n for n in res["failing_names"])
    assert res["evidence_refs"] == ["/w/m/target/surefire-reports"]


def test_parse_module_test_reports_empty_when_no_dirs():
    v = PhysicalValidator(docker_orchestrator=object())
    assert v.parse_module_test_reports("/w/m", []) == {}
