"""The analyze brief carries the survey's facts (token levers 2, Unit A).

The harness survey already holds the project type, build system, java version,
test framework, static test count, dependencies and documented commands when
the analyze phase opens. ``render_survey_brief`` turns the compact
``survey_facts`` dict on the trunk into a few deterministic ``Survey:`` lines,
so the model does not spend requests re-reading pom.xml and README to find
them again.
"""

from sag.agent.survey_brief import MAX_SURVEY_BRIEF_CHARS, render_survey_brief

DISCLAIMER = (
    "Survey: these are observed facts recorded by the harness, not a plan or a runner choice."
)


def _java_facts(**overrides):
    facts = {
        "project_type": "Java",
        "build_system": "maven",
        "build_root": "/workspace/x",
        "test_root": "/workspace/x",
        "root_shape": "single_module",
        "is_multi_module": False,
        "java_version": "11",
        "java_version_source": "maven-compiler",
        "java_version_enforced": False,
        "test_framework": "JUnit",
        "static_test_count": 473,
        "method_count": 473,
        "test_count_method": "catalog_based_discovery",
        "parameterized": True,
        "dependencies": [
            "org.apache.commons:commons-parent",
            "commons-dbutils:commons-dbutils",
            "junit:junit",
            "org.mockito:mockito-core",
            "org.hamcrest:hamcrest",
        ],
        "dependencies_total": 10,
        "documentation": {
            "java_version_requirement": "11",
            "build_commands": ["mvn clean install"],
            "test_commands": ["mvn test"],
        },
        "existing_files": ["pom.xml", "README.md"],
    }
    facts.update(overrides)
    return facts


def test_java_survey_renders_the_documented_shape():
    lines = render_survey_brief({"survey_facts": _java_facts()})
    assert lines == [
        "Survey: project type Java; build system maven at /workspace/x (single module); "
        "files found: pom.xml, README.md",
        "Survey: java version in build config: 11 (source maven-compiler; enforced: no); "
        "documented requirement: Java 11",
        "Survey: test framework JUnit; 473 test method annotations "
        "(parameterized tests run more than once)",
        "Survey: dependencies 10 (org.apache.commons:commons-parent, "
        "commons-dbutils:commons-dbutils, junit:junit, org.mockito:mockito-core, "
        "org.hamcrest:hamcrest, …)",
        "Survey: documented build commands: mvn clean install; documented test commands: mvn test",
        DISCLAIMER,
    ]


def test_rendering_is_deterministic():
    env = {"survey_facts": _java_facts()}
    assert render_survey_brief(env) == render_survey_brief(dict(env))


def test_nothing_renders_without_survey_facts():
    assert render_survey_brief({}) == []
    assert render_survey_brief(None) == []
    assert render_survey_brief({"build_system": "maven"}) == []
    assert render_survey_brief({"survey_facts": "not a mapping"}) == []
    assert render_survey_brief({"survey_facts": {}}) == []


def test_lines_with_missing_facts_are_omitted():
    lines = render_survey_brief(
        {
            "survey_facts": {
                "project_type": "Python",
                "build_system": "python",
                "build_root": "/workspace/pyyaml",
            }
        }
    )
    assert lines == [
        "Survey: project type Python; build system python at /workspace/pyyaml",
        DISCLAIMER,
    ]


def test_unparameterized_count_carries_no_repeat_note_and_enforced_reads_yes():
    lines = render_survey_brief(
        {
            "survey_facts": _java_facts(
                parameterized=False,
                java_version_enforced=True,
                java_version_source="maven-enforcer",
            )
        }
    )
    assert "Survey: test framework JUnit; 473 test method annotations" in lines
    assert any("(source maven-enforcer; enforced: yes)" in line for line in lines)
    assert not any("more than once" in line for line in lines)


def test_separate_test_root_and_reactor_modules_are_folded_into_the_first_line():
    facts = _java_facts(
        build_root="/workspace/big",
        test_root="/workspace/big/tests",
        root_shape="healthy_reactor",
        is_multi_module=True,
        modules=[f"module-{i}" for i in range(8)],
        modules_total=11,
    )
    first = render_survey_brief({"survey_facts": facts})[0]
    assert first.startswith("Survey: project type Java; build system maven at /workspace/big (")
    assert "multi-module root" in first
    assert (
        "11 modules: module-0, module-1, module-2, module-3, module-4, module-5, module-6, module-7, …"
        in first
    )
    assert "tests at /workspace/big/tests" in first


def test_aggregator_root_is_said_in_plain_words():
    first = render_survey_brief(
        {
            "survey_facts": _java_facts(
                root_shape="pathological_aggregator", build_root="/workspace/bigtop/bigtop-manager"
            )
        }
    )[0]
    assert (
        "build system maven at /workspace/bigtop/bigtop-manager (the project root only aggregates modules)"
        in first
    )
    assert "pathological" not in first


def test_oversized_facts_are_capped_and_every_line_keeps_its_prefix():
    huge = "x" * 500
    facts = _java_facts(
        build_root="/workspace/" + huge,
        existing_files=[f"file-{i}-{huge}" for i in range(20)],
        modules=[f"mod-{i}-{huge}" for i in range(30)],
        modules_total=30,
        dependencies=[f"group:{huge}-{i}" for i in range(40)],
        dependencies_total=40,
        documentation={
            "java_version_requirement": huge,
            "build_commands": [f"mvn {huge}\nrm -rf /" for _ in range(9)],
            "test_commands": [f"mvn test {huge}" for _ in range(9)],
        },
        test_framework=huge,
    )
    lines = render_survey_brief({"survey_facts": facts})
    assert len("\n".join(lines)) <= MAX_SURVEY_BRIEF_CHARS == 900
    assert all(line.startswith("Survey: ") for line in lines)
    assert lines[-1] == DISCLAIMER
    # A newline inside a fact never opens an unprefixed line.
    assert not any(line.startswith("rm -rf") for line in lines)
    assert render_survey_brief({"survey_facts": facts}) == lines


def test_documented_commands_outrank_the_dependency_sample_for_the_budget():
    huge = "x" * 500
    facts = _java_facts(
        build_root="/workspace/" + huge,
        dependencies=[f"group:{huge}-{i}" for i in range(40)],
        dependencies_total=40,
        documentation={"build_commands": [f"mvn {huge}"] * 3, "test_commands": ["mvn test"]},
    )
    lines = render_survey_brief({"survey_facts": facts})
    assert len("\n".join(lines)) <= MAX_SURVEY_BRIEF_CHARS
    assert any(line.startswith("Survey: documented build commands: mvn ") for line in lines)
    # Printed order stays the documented shape even when the budget order differs.
    order = [line.split(" ", 2)[1] for line in lines]
    assert order == sorted(
        order, key=["project", "java", "test", "dependencies", "documented", "these"].index
    )
