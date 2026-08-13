# tests/test_physical_validator_modules.py
from sag.agent.physical_validator import PhysicalValidator


class FakeOrch:
    def __init__(self, responses):
        # dict: substring -> {"success","output","exit_code"}, or a callable
        # taking the command (for probes whose answer depends on what was asked)
        self.responses = responses
        self.commands = []  # every command issued, in order (probe-shape fences)

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        for needle, resp in self.responses.items():
            if needle in command:
                if callable(resp):
                    resp = resp(command)
                return {"success": True, "exit_code": 0, **resp}
        return {"success": True, "exit_code": 0, "output": ""}


def _disk_holding(*present):
    """Answer the batched `test -d` probe the way a disk would: echo back the
    candidates the command ACTUALLY asked about, and only those that exist.

    A fake that echoes a fixed list regardless of the question answers for the
    parser instead of testing it — an enumeration bug then passes the fence.
    """
    on_disk = set(present)

    def respond(command):
        asked = command.split("for d in ", 1)[1].split(";", 1)[0].split()
        return {"output": "\n".join(d for d in asked if d in on_disk)}

    return respond


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


def test_scan_modules_marks_maven_aggregator_shell_root():
    """Live httpcomponents-client: the reactor root is a packaging=pom aggregator
    with zero own sources. It must be marked aggregator_shell so the summary can
    exclude it from the built/total ratio (the '5/6 built' cap)."""
    responses = {
        # two real submodules found -> multi-module scan
        "-name 'pom.xml'": {"output": "/w/p/httpclient5/pom.xml\n/w/p/httpclient5-fluent/pom.xml"},
        # root produces nothing of its own
        "/w/p/target/classes": {"output": "0"},
        "/w/p/target' -name '*.jar": {"output": "0"},
        # submodules built
        "/httpclient5/target/classes": {"output": "800"},
        "/httpclient5-fluent/target/classes": {"output": "40"},
        # root pom declares <packaging>pom</packaging>
        "grep -q '<packaging>": {"output": "POM"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/p", "maven")
    by_path = {m["path"]: m for m in modules}
    assert by_path["."].get("aggregator_shell") is True
    # real modules never carry the flag
    assert "aggregator_shell" not in by_path["httpclient5"]
    assert "aggregator_shell" not in by_path["httpclient5-fluent"]


def test_scan_modules_root_with_own_sources_is_not_a_shell():
    """commons-chain shape: a root with its OWN compiled classes stays a real,
    counted module — never flagged as a shell even though it has submodules."""
    responses = {
        "-name 'pom.xml'": {"output": "/w/p/apps/example/pom.xml"},
        "/w/p/target/classes": {"output": "33"},  # root has its own sources
        "/w/p/target' -name '*.jar": {"output": "1"},
        # even if the root pom were packaging=pom, artifacts on the root veto the shell
        "grep -q '<packaging>": {"output": "POM"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/p", "maven")
    by_path = {m["path"]: m for m in modules}
    assert "aggregator_shell" not in by_path["."]


def test_scan_modules_zero_source_root_but_jar_packaging_not_a_shell():
    """A zero-class multi-module root whose pom is NOT packaging=pom (e.g. a jar
    root that simply hasn't compiled) must not be misread as a shell — the
    physical packaging probe is the confirmation, never the class count alone."""
    responses = {
        "-name 'pom.xml'": {"output": "/w/p/sub/pom.xml"},
        "/w/p/target/classes": {"output": "0"},
        "/w/p/target' -name '*.jar": {"output": "0"},
        # grep finds no <packaging>pom</packaging> -> echo POM never fires
        "grep -q '<packaging>": {"output": ""},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/p", "maven")
    assert "aggregator_shell" not in {m["path"]: m for m in modules}["."]


def test_scan_modules_gradle_aggregator_shell_root_no_src_main():
    """Gradle aggregator: a multi-project root with no src/main is a shell."""
    responses = {
        "build.gradle": {"output": "/w/g/moduleA/build.gradle\n/w/g/moduleB/build.gradle"},
        "/w/g/build/classes": {"output": "0"},
        "/w/g/build/libs' -name '*.jar": {"output": "0"},
        "/moduleA/build/classes": {"output": "120"},
        "/moduleB/build/classes": {"output": "60"},
        # root has NO src/main -> the "test -d .../src/main && echo EXISTS" is empty
        "src/main": {"output": ""},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/g", "gradle")
    assert {m["path"]: m for m in modules}["."].get("aggregator_shell") is True


# ---------------------------------------------------------------------------
# Centrally declared Gradle subprojects (kafka/samza shape)
# ---------------------------------------------------------------------------

# Kafka's real settings.gradle: ONE `include` statement naming every subproject
# across continuation lines, and NO build.gradle in any subdirectory.
KAFKA_SETTINGS = """\
// Licensed to the Apache Software Foundation (ASF)
include 'clients',
    'connect:api',
    'connect:runtime',
    'core',
    'streams'

rootProject.name = 'kafka'
"""


def test_scan_modules_gradle_enumerates_centrally_declared_subprojects():
    """D2 kafka: a Gradle build declares all subprojects in settings.gradle and
    ships ONE root build.gradle. The per-directory build-file walk therefore
    finds nothing, the denominator collapsed to the root (+ stray poms), and the
    11,421 classes sitting in unenumerated subproject build dirs were invisible.
    A module the settings file declares is a module."""
    responses = {
        # the per-directory walk finds no subdirectory build.gradle at all
        "-name 'build.gradle'": {"output": ""},
        "cat /w/kafka/settings.gradle": {"output": KAFKA_SETTINGS},
        # every declared directory exists on disk
        "for d in": _disk_holding(
            "/w/kafka/clients",
            "/w/kafka/connect/api",
            "/w/kafka/connect/runtime",
            "/w/kafka/core",
            "/w/kafka/streams",
        ),
        "/clients/build/classes": {"output": "1200"},
        "/connect/api/build/classes": {"output": "180"},
        "/connect/runtime/build/classes": {"output": "900"},
        "/core/build/classes": {"output": "2100"},
        "/streams/build/classes": {"output": "1400"},
        "/clients/build/libs' -name '*.jar": {"output": "1"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/kafka", "gradle")
    by_path = {m["path"]: m for m in modules}

    # the real subproject count, not root-plus-strays
    assert set(by_path) == {".", "clients", "connect/api", "connect/runtime", "core", "streams"}
    # and their real outputs
    assert by_path["clients"]["class_count"] == 1200
    assert by_path["connect/api"]["class_count"] == 180
    assert by_path["connect/api"]["name"] == "connect:api"
    assert by_path["core"]["class_count"] == 2100
    assert by_path["clients"]["jar_count"] == 1


def test_scan_modules_gradle_reads_the_kotlin_settings_file():
    """p7d polaris: the declaration lived in settings.gradle.kts, which nothing
    read — a parenthesized, multi-line `include(...)`."""
    settings_kts = 'include(\n    ":api",\n    ":service:common",\n)\n'
    responses = {
        "-name 'build.gradle'": {"output": ""},
        # only the .kts file exists; the groovy read comes back empty
        "cat /w/p/settings.gradle 2>": {"output": ""},
        "cat /w/p/settings.gradle.kts": {"output": settings_kts},
        "for d in": _disk_holding("/w/p/api", "/w/p/service/common"),
        "/api/build/classes": {"output": "12"},
        "/service/common/build/classes": {"output": "34"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    by_path = {m["path"]: m for m in v.scan_modules("/w/p", "gradle")}
    assert set(by_path) == {".", "api", "service/common"}
    assert by_path["service/common"]["class_count"] == 34


def test_scan_modules_gradle_drops_declared_dirs_that_are_not_on_disk():
    """A declaration the disk does not back is not a module: counting it would
    manufacture a permanent shortfall no build could close."""
    responses = {
        "-name 'build.gradle'": {"output": ""},
        "cat /w/p/settings.gradle": {"output": "include ':real', ':relocated'\n"},
        # only ':real' has a directory at the conventional path
        "for d in": _disk_holding("/w/p/real"),
        "/real/build/classes": {"output": "7"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    assert {m["path"] for m in v.scan_modules("/w/p", "gradle")} == {".", "real"}


def test_scan_modules_gradle_never_counts_an_included_build_as_a_module():
    """`includeBuild` names a SEPARATE build (composite), not a subproject."""
    responses = {
        "-name 'build.gradle'": {"output": ""},
        "cat /w/p/settings.gradle": {
            # the composite build's directory sits INSIDE the checkout and
            # exists, so only the parse can keep it out of the denominator
            "output": "includeBuild 'build-logic'\ninclude ':app'\n"
        },
        "for d in": _disk_holding("/w/p/app", "/w/p/build-logic"),
        "/app/build/classes": {"output": "5"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    assert {m["path"] for m in v.scan_modules("/w/p", "gradle")} == {".", "app"}


def test_scan_modules_gradle_records_how_many_modules_were_declared():
    """The scan states what the build DECLARED beside what it enumerated, so a
    downstream reader can tell a small project from a blind scan."""
    responses = {
        "-name 'build.gradle'": {"output": ""},
        "cat /w/kafka/settings.gradle": {"output": KAFKA_SETTINGS},
        "for d in": _disk_holding(
            "/w/kafka/clients",
            "/w/kafka/connect/api",
            "/w/kafka/connect/runtime",
            "/w/kafka/core",
            "/w/kafka/streams",
        ),
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/kafka", "gradle")
    assert all(m["declared_modules"] == 5 for m in modules)


def test_scan_modules_maven_needs_no_declaration_parse():
    """The Maven answer: `<module>` names a DIRECTORY that must contain its own
    pom.xml, so the per-directory pom walk already enumerates every declared
    module — the Gradle gap has no Maven twin. The maven scan therefore reads no
    settings/aggregator declaration at all, and stays byte-identical."""
    responses = {
        "-name 'pom.xml'": {"output": "/w/p/core/pom.xml"},
        "/core/target/classes": {"output": "50"},
    }
    orch = FakeOrch(responses)
    v = PhysicalValidator(docker_orchestrator=orch)
    by_path = {m["path"]: m for m in v.scan_modules("/w/p", "maven")}
    assert set(by_path) == {".", "core"}
    assert not [c for c in orch.commands if "settings.gradle" in c]
    assert "declared_modules" not in by_path["core"]


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


def test_parse_module_test_reports_handles_gradle_attribute_order():
    # Gradle's JUnit XML writer emits attributes in the order
    # name, tests, skipped, failures, errors -- i.e. skipped BEFORE failures/errors.
    # A positional regex (tests..failures..errors..skipped) fails to match this,
    # leaving every count at 0 even though tests ran.
    gradle_xml = (
        '<testsuite name="com.x.BarTest" tests="4" skipped="1" failures="1" '
        'errors="1" time="0.5">'
        '<testcase classname="com.x.BarTest" name="ok"/>'
        '<testcase classname="com.x.BarTest" name="bad"><failure/></testcase>'
        '<testcase classname="com.x.BarTest" name="boom"><error/></testcase>'
        '<testcase classname="com.x.BarTest" name="ign"><skipped/></testcase>'
        '</testsuite>'
    )

    class Orch:
        def execute_command(self, command, **kwargs):
            if "cat" in command and "test-results" in command:
                return {"success": True, "exit_code": 0, "output": gradle_xml}
            if "find" in command and "test-results" in command:
                return {"success": True, "exit_code": 0,
                        "output": "/w/m/build/test-results/test/TEST-com.x.BarTest.xml"}
            return {"success": True, "exit_code": 0, "output": ""}

    v = PhysicalValidator(docker_orchestrator=Orch())
    res = v.parse_module_test_reports("/w/m", ["/w/m/build/test-results/test"])
    assert res["tests_total"] == 4
    assert res["tests_failed"] == 1
    assert res["tests_errors"] == 1
    assert res["tests_skipped"] == 1
    assert res["tests_passed"] == 1
    assert res["failing_count"] == 2  # one <failure> + one <error>


def test_parse_module_test_reports_empty_when_no_dirs():
    v = PhysicalValidator(docker_orchestrator=object())
    assert v.parse_module_test_reports("/w/m", []) == {}


def test_parse_module_test_reports_surefire_attribute_order():
    """Surefire emits <testcase name=... classname=...> (name BEFORE classname)
    plus self-closing passing cases. Failing-name extraction must be
    attribute-order-independent, and failing_count must equal failures+errors
    from the testsuite attrs (not the count of extracted names).

    Live commons-vfs run: tests_failed=3 but failing_count=0 because the
    testcase regex assumed classname-first while surefire is name-first.
    """
    surefire_xml = (
        '<testsuite name="com.x.BarTest" tests="3" skipped="0" failures="2" errors="0">'
        '<testcase name="ok" classname="com.x.BarTest" time="0.1"/>'
        '<testcase name="bad1" classname="com.x.BarTest" time="0.2">'
        '<failure message="boom">stack</failure></testcase>'
        '<testcase name="bad2" classname="com.x.BarTest"><failure/></testcase>'
        '</testsuite>'
    )

    class Orch:
        def execute_command(self, command, **kwargs):
            if "cat" in command and "surefire" in command:
                return {"success": True, "exit_code": 0, "output": surefire_xml}
            if "find" in command and "surefire" in command:
                return {"success": True, "exit_code": 0,
                        "output": "/w/m/target/surefire-reports/TEST-com.x.BarTest.xml"}
            return {"success": True, "exit_code": 0, "output": ""}

    v = PhysicalValidator(docker_orchestrator=Orch())
    res = v.parse_module_test_reports("/w/m", ["/w/m/target/surefire-reports"])
    assert res["tests_total"] == 3
    assert res["tests_failed"] == 2
    assert res["failing_count"] == 2  # authoritative: failures + errors
    assert sorted(res["failing_names"]) == ["com.x.BarTest.bad1", "com.x.BarTest.bad2"]


def test_failing_count_uses_attrs_when_names_unextractable():
    """failing_count reflects failures+errors even when no per-case names parse
    (e.g. an aggregated suite element carrying only counts)."""
    xml = '<testsuite tests="10" failures="3" errors="1" skipped="0"></testsuite>'

    class Orch:
        def execute_command(self, command, **kwargs):
            if "cat" in command:
                return {"success": True, "exit_code": 0, "output": xml}
            if "find" in command:
                return {"success": True, "exit_code": 0, "output": "/w/m/r/TEST-x.xml"}
            return {"success": True, "exit_code": 0, "output": ""}

    v = PhysicalValidator(docker_orchestrator=Orch())
    res = v.parse_module_test_reports("/w/m", ["/w/m/r"])
    assert res["tests_failed"] == 3 and res["tests_errors"] == 1
    assert res["failing_count"] == 4  # 3 failures + 1 error
    assert res["failing_names"] == []
