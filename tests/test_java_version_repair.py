# tests/test_java_version_repair.py
"""A build that states its own Java mismatch gets a typed observation.

Live evidence. p7-polaris (`logs/session_20260727_182218_41763`): Gradle
printed "requires Java 21." and "Detected Java version: 17", and no typed code
named it. p7-camel
(`logs/session_20260727_182221_41809`): the project's own wrapper ran under
Java 17 against a build needing 17+. In both runs the sentence was right there
and nothing read it.

The requirement is stated by the runner in its own output, so it is receipt
evidence rather than a harness-authored command recommendation.
"""

from sag.agent.evidence_assessments import java_version_mismatch
from sag.tools.internal.build_preflight import classify_version_error

GRADLE_OUTPUT = (
    "FAILURE: Build failed with an exception.\n"
    "* What went wrong:\n"
    "A problem occurred configuring root project 'polaris'.\n"
    "> Dependency requires at least JVM runtime version 21. "
    "This build uses a Java 17 JVM.\n"
    "  Build requires Java 21.\n"
    "        Detected Java version: 17\n"
)

MAVEN_ENFORCER_OUTPUT = (
    "[WARNING] Rule 0: RequireJavaVersion failed with message:\n"
    "Detected JDK Version: 11.0.22 is not in the allowed range [17,).\n"
)

MAVEN_REQUIRED_VERSION_OUTPUT = (
    "[ERROR] Required Java version 17 is not met by current version 11.0.27"
)

RECEIPT = {"receipt_id": "inv-gradle-1-0001", "outcome": "failed"}


def test_the_gradle_shape_yields_both_majors():
    (assessment,) = java_version_mismatch(RECEIPT, GRADLE_OUTPUT)

    assert assessment.typed_code == "java_version_mismatch"
    assert assessment.receipt_id == "inv-gradle-1-0001"
    assert "requires java 21" in assessment.detail
    assert "ran under java 17" in assessment.detail


def test_the_maven_enforcer_shape_yields_both_majors():
    (assessment,) = java_version_mismatch(RECEIPT, MAVEN_ENFORCER_OUTPUT)

    assert assessment.typed_code == "java_version_mismatch"
    assert "requires java 17" in assessment.detail
    assert "ran under java 11" in assessment.detail


def test_runtime_retry_parser_understands_mavens_actual_required_version_wording():
    assert classify_version_error(MAVEN_REQUIRED_VERSION_OUTPUT) == "17"


def test_maven_actual_wording_also_yields_a_typed_receipt_assessment():
    (assessment,) = java_version_mismatch(RECEIPT, MAVEN_REQUIRED_VERSION_OUTPUT)

    assert assessment.typed_code == "java_version_mismatch"
    assert assessment.detail == "build requires java 17, ran under java 11"


def test_one_major_alone_states_nothing():
    """Inferring the missing half would invent a requirement."""
    assert java_version_mismatch(RECEIPT, "Build requires Java 21.\n") == []
    assert java_version_mismatch(RECEIPT, "Detected Java version: 17\n") == []


def test_two_majors_that_agree_are_not_a_mismatch():
    output = "Build requires Java 17.\n        Detected Java version: 17\n"

    assert java_version_mismatch(RECEIPT, output) == []


def test_no_output_states_nothing():
    assert java_version_mismatch(RECEIPT, None) == []
    assert java_version_mismatch(RECEIPT, "") == []
