"""Every Maven build lifecycle phase counts as a build (spec 2026-10-22, Change 4).

iotdb's pinned command was `mvn clean integration-test ...`: its receipt
recorded the action as `run`, which was not on the list of build actions, so
the 28 modules its reactor summary reported built were never confirmed and the
run read `maven_reactor_unverified` on a passing command.
"""

import pytest
from test_coverage_basis import _current_build_receipt

from sag.agent.physical_validator import PhysicalValidator, receipt_runs_build
from sag.result_card.glosses import gloss

IOTDB = (
    "/usr/local/bin/mvn clean integration-test -Dtest.port.closed=true "
    "-pl iotdb-core/datanode -am -DskipTests -Diotdb.test.only=true -DforkCount=4"
)


@pytest.mark.parametrize(
    "action", ["compile", "test", "package", "integration-test", "verify", "install", "deploy"]
)
def test_every_build_lifecycle_phase_is_a_build_action(action):
    assert receipt_runs_build({"tool": "maven", "requested_action": action})


@pytest.mark.parametrize(
    ("argv", "builds"),
    [
        (IOTDB, True),
        ("/opt/mvn -B package --file pom.xml", True),
        ("/opt/mvn deploy -DskipTests", True),
        ("/opt/mvn verify javadoc:jar", True),
        # A goal, a profile named like a phase, or no phase at all is not a build.
        ("/opt/mvn dependency:tree", False),
        ("/opt/mvn -P test dependency:tree", False),
        ("/opt/mvn clean", False),
        ("/opt/mvn --errors --show-version --batch-mode", False),
    ],
)
def test_a_run_receipt_is_a_build_when_its_command_names_a_build_phase(argv, builds):
    receipt = {"tool": "maven", "requested_action": "run", "effective_action": "run", "argv": argv}
    assert receipt_runs_build(receipt) is builds


def test_the_iotdb_receipt_confirms_the_modules_its_reactor_built():
    modules = [f"IoTDB: Module {index}" for index in range(28)]
    receipt = _current_build_receipt(
        {
            "receipt_id": "inv-maven-1-5d12a6dfad1c-0001",
            "tool": "maven",
            "requested_action": "run",
            "effective_action": "run",
            "argv": IOTDB,
            "exit_code": 0,
            "compliance": "exact",
            "module_outcomes": [{"module": name, "status": "success"} for name in modules],
        }
    )
    validator = PhysicalValidator(project_path="/workspace")
    validator._current_scoped_receipts = lambda _project_dir: [receipt]

    result = validator._terminal_root_maven_reactor_receipt("/workspace/proj")

    assert result is not None
    assert result["modules_succeeded"] == result["modules_total"] == 28
    assert validator.terminal_build_receipt("/workspace/proj") == {
        "receipt_id": "inv-maven-1-5d12a6dfad1c-0001",
        "exit_code": 0,
    }


def test_the_java_requirement_gloss_says_what_could_not_be_decided():
    assert gloss("build_requirements_unavailable") == (
        "whether the installed Java version meets the project's requirement could not be "
        "decided (a Maven profile's activation could not be evaluated)"
    )
