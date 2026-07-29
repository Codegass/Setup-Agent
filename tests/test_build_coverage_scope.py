# tests/test_build_coverage_scope.py
"""Plan 7 — the build system sets the coverage SCOPE, file counting verifies
the SUBSTANCE.

Build coverage was `classes_found / classes_expected`, where the denominator
counted every `src/main/java` in the tree. That measures modules this
invocation never attempted: a scoped build (`mvn -pl core`), a reactor that
stopped at the third of twenty modules, a profile-excluded subtree — all of
them arrive as "missing", which is a different fact from "failed to compile".
camel's campaign run reported "31/37 modules" without being able to say which
of the six were failures and which were never tried.

The build system says which modules it attempted, and it always did: Maven
prints a reactor summary, Gradle prints per-task outcomes. Both were parsed
and neither reached the validator. They now ride the invocation receipt —
per dispatch, contract-bound, hashed — and the receipt sets the denominator.

The narrowing is deliberately conservative. It happens only when every module
the build named maps to an expectation; a partial mapping would drop
expectations for modules that DID run and inflate the score, which is worse
than the wide denominator it replaces. When the mapping is incomplete the wide
denominator stands and the disagreement is recorded.
"""

from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.gradle_tool import _gradle_module_outcomes
from sag.tools.internal.maven_tool import _reactor_module_outcomes

REACTOR_OUTPUT = """[INFO] Reactor Summary for Apache Camel 4.20.0:
[INFO]
[INFO] Apache Camel :: Core ............................... SUCCESS [ 12.345 s]
[INFO] Apache Camel :: JMS ................................ FAILURE [  3.210 s]
[INFO] Apache Camel :: FTP ................................ SKIPPED
[INFO] ------------------------------------------------------------------------
[INFO] BUILD FAILURE
"""

GRADLE_OUTPUT = """> Task :core:compileJava
> Task :jms:compileJava NO-SOURCE
> Task :ftp:test FAILED
"""


def scope(expectations, attempted):
    return PhysicalValidator._scope_expectations_to_attempted(
        PhysicalValidator, expectations, attempted
    )


def expectation(module_dir, min_count=10):
    return {
        "path": f"/workspace/proj/{module_dir}/target/classes",
        "type": "classes",
        "artifact": f"compiled classes (from {min_count} source files)",
        "min_count": min_count,
    }


# --------------------------------------------------------------------------
# what each build system states
# --------------------------------------------------------------------------


def test_the_reactor_summary_states_every_module_and_its_outcome():
    assert _reactor_module_outcomes(REACTOR_OUTPUT) == [
        {"module": "Apache Camel :: Core", "status": "success"},
        {"module": "Apache Camel :: JMS", "status": "failure"},
        {"module": "Apache Camel :: FTP", "status": "skipped"},
    ]


def test_a_single_module_build_states_no_reactor():
    """Maven prints no summary for one module; absence is the honest answer."""
    assert _reactor_module_outcomes("[INFO] BUILD SUCCESS\n") == []


def test_gradle_states_attempted_and_failed_but_never_success():
    """A task list does not prove a module built correctly.

    Claiming "success" from `> Task :core:compileJava` would be the overclaim
    the physical check exists to catch, so the status stops at "attempted".
    """
    assert _gradle_module_outcomes(GRADLE_OUTPUT) == [
        {"module": "core", "status": "attempted"},
        {"module": "jms", "status": "attempted"},
        {"module": "ftp", "status": "failure"},
    ]


# --------------------------------------------------------------------------
# how the scope narrows the denominator
# --------------------------------------------------------------------------


def test_a_module_the_build_never_tried_leaves_the_denominator():
    """The `-pl core` case: two modules on disk, one in the build."""
    expectations = [expectation("core"), expectation("jms")]

    scoped, untried, conflict = scope(expectations, ("Apache Camel :: Core",))

    assert [item["path"] for item in scoped] == ["/workspace/proj/core/target/classes"]
    assert untried == ["jms"]
    assert conflict is None


def test_maven_module_names_match_their_directories_after_normalising():
    """`Apache Camel :: Core` is the directory `core`; the tail decides."""
    assert PhysicalValidator._module_key("Apache Camel :: Core") == "core"
    assert PhysicalValidator._module_key("/workspace/camel/core") == "core"
    assert PhysicalValidator._module_key("camel-jms") == "cameljms"


def test_an_incomplete_mapping_keeps_the_wide_denominator_and_says_so():
    """A module the expectations do not contain means the mapping is unsafe.

    Dropping expectations here would inflate coverage for modules that really
    did run, so nothing is narrowed and the disagreement is recorded instead.
    """
    expectations = [expectation("core")]

    scoped, untried, conflict = scope(expectations, ("core", "an-unmapped-module"))

    assert len(scoped) == 1
    assert untried == []
    assert conflict == "build_coverage_scope_unverified"


def test_no_stated_modules_changes_nothing():
    """Single-module builds and receipt-free runs keep their numbers."""
    expectations = [expectation("core"), expectation("jms")]

    scoped, untried, conflict = scope(expectations, None)

    assert scoped == expectations
    assert untried == []
    assert conflict is None


def test_an_expectation_with_no_module_of_its_own_is_never_dropped():
    """A root-level or unparseable expectation stays in the denominator."""
    root_expectation = {"path": "/target/classes", "type": "classes", "min_count": 3}

    scoped, _untried, _conflict = scope([root_expectation], ("core",))

    assert root_expectation in scoped
