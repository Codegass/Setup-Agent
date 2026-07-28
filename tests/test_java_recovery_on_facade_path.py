# tests/test_java_recovery_on_facade_path.py
"""Plan 7 round two — the JDK recovery must recognise Gradle's own wording.

Live p7b-polaris (`logs/session_20260728_020933_55691`): the Gradle build said
"Dependency requires at least JVM runtime version 21. This build uses a Java 17
JVM." The harness typed the fault, proposed provisioning 21 with provenance,
and granted the repair phase when the model asked. The model wrote a report
instead, and the run failed with zero tests.

The build facade already owns a bounded JDK recovery for exactly this: it
classifies the failure text, re-provisions the version the error names, and
re-runs the same materialized argv once. Every precondition held — the verb
takes the pre-flight, the outcome was present, and the detached result carries
its complete log on `raw_output`. What was missing was the pattern: Gradle's
toolchain wording matched none of the shapes the classifier knew, all of which
came from Maven Enforcer or javac. So the recovery had nothing to act on and
the decision fell to the model.

The recovery stays in ONE layer. `gradle_tool` deliberately does not retry on
the facade path (`test_gradle_tool_env_preflight_false_never_retries` pins
that): exactly one layer probes the container and reruns per build.
"""

from sag.tools.internal.build_preflight import classify_version_error

POLARIS_OUTPUT = (
    "FAILURE: Build failed with an exception.\n"
    "* What went wrong:\n"
    "A problem occurred configuring root project 'polaris'.\n"
    "> Dependency requires at least JVM runtime version 21. "
    "This build uses a Java 17 JVM.\n"
)

GRADLE_SETTINGS_OUTPUT = "Build requires Java 21.\n        Detected Java version: 17\n"

ENFORCER_OUTPUT = (
    "[WARNING] Rule 0: RequireJavaVersion failed with message:\n"
    "Detected JDK Version: 11.0.22 is not in the allowed range [17,).\n"
)


def test_the_gradle_toolchain_wording_names_its_required_major():
    """The two sentences polaris failed on."""
    assert classify_version_error(POLARIS_OUTPUT) == "21"
    assert classify_version_error(GRADLE_SETTINGS_OUTPUT) == "21"


def test_the_shapes_that_already_worked_still_work():
    assert classify_version_error(ENFORCER_OUTPUT) == "17"
    assert classify_version_error("release version 21 not supported") == "21"
    assert classify_version_error("invalid target release: 1.8") == "8"
    assert classify_version_error("error: class file version 61.0") == "17"


def test_an_unrelated_failure_names_no_version():
    """A recovery that fired on any failure would reinstall JDKs forever."""
    assert classify_version_error("BUILD FAILED: compilation error in Foo.java") is None
    assert classify_version_error("Could not resolve org.example:lib:1.0") is None
    assert classify_version_error("") is None


def test_the_facade_classifies_the_complete_detached_log():
    """A detached failure's version complaint sits outside the inline window.

    The facade joins `output` and `raw_output` before classifying; for a
    detached dispatch `output` is only the storage reference, so the complete
    log on `raw_output` is what carries the sentence.
    """
    import inspect

    from sag.tools.build.build_tool import BuildTool

    source = inspect.getsource(BuildTool.execute)
    retry = source.split("Bounded retry (spec §1c)", 1)[1][:900]

    assert "inner.raw_output" in retry
    assert "classify_version_error(failure_text)" in retry


def test_a_detached_failure_carries_its_complete_log():
    """`classify_detached_completion` puts the full log where the facade reads it."""
    from sag.tools.internal.build_utils import classify_detached_completion

    result = classify_detached_completion(
        1,
        "stored as output_7c4da0b66f89",
        None,
        full_output=POLARIS_OUTPUT,
        terminal_observation=True,
    )

    assert result.raw_output == POLARIS_OUTPUT
    assert classify_version_error(result.raw_output) == "21"
