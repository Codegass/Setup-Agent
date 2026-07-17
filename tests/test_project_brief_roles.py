from sag.agent.project_brief import (
    BriefFragment,
    InputRole,
    ProjectBriefComposer,
    ProjectBriefInputs,
)


def _inputs():
    return ProjectBriefInputs(
        manifest={"pom.xml": "<project/>"},
        detected_toolchain={"build_system": "maven"},
        submodule_state=(),
        build_roots=(".",),
        repo_docs={},
        analyzer_version="analyzer-v1",
        composer_version="composer-v1",
    )


def _java_fragments():
    return [
        BriefFragment(
            instruction_id="java-required",
            subject="java.version",
            role=InputRole.REQUIREMENT,
            value="8",
            text="Project requires JDK 8.",
            source="manifest",
            refs=("manifest://pom.xml#java-version",),
        ),
        BriefFragment(
            instruction_id="java-current",
            subject="java.version",
            role=InputRole.EVIDENCE,
            value="17",
            text="Current environment has JDK 17.",
            source="env-overlay",
            refs=("env-overlay://java",),
        ),
        BriefFragment(
            instruction_id="java-default",
            subject="java.version",
            role=InputRole.DEFAULT,
            value="21",
            text="Assume JDK 21 when no requirement is known.",
            source="runtime-default",
            refs=("policy://java-default",),
        ),
    ]


def test_requirement_and_environment_mismatch_emits_provisioning_action():
    brief = ProjectBriefComposer().compose(_inputs(), _java_fragments())

    actions = brief.section("actions").instructions
    assert [item.instruction_id for item in actions] == ["provision-jdk"]
    assert "JDK 8" in actions[0].text
    assert "JDK 17" in actions[0].text
    assert actions[0].refs == (
        "env-overlay://java",
        "manifest://pom.xml#java-version",
    )

    projection = brief.to_planner_projection()
    assert "Provision JDK 8" in projection
    assert "use JDK 17" not in projection
    assert "Assume JDK 21" not in projection


def test_default_applies_only_when_requirement_is_unknown_and_is_marked_assumption():
    default = BriefFragment(
        instruction_id="java-default",
        subject="java.version",
        role=InputRole.DEFAULT,
        value="17",
        text="Assume JDK 17.",
        source="runtime-default",
        refs=("policy://java-default",),
    )

    brief = ProjectBriefComposer().compose(_inputs(), [default])

    assumptions = brief.section("assumptions").instructions
    assert len(assumptions) == 1
    assert assumptions[0].markers == ("assumption",)
    assert "[assumption]" in brief.to_planner_projection()


def test_policy_wins_without_becoming_a_total_role_order():
    fragments = [
        BriefFragment(
            instruction_id="runtime-policy",
            subject="runtime.network",
            role=InputRole.POLICY,
            value="offline",
            text="Network access is disabled.",
            source="runtime-policy",
            refs=("policy://network",),
        ),
        BriefFragment(
            instruction_id="repo-network",
            subject="runtime.network",
            role=InputRole.REQUIREMENT,
            value="online",
            text="Repository docs request network access.",
            source="repo-doc",
            refs=("repo-doc://README.md#setup",),
        ),
    ]

    brief = ProjectBriefComposer().compose(_inputs(), fragments)

    policy = brief.section("requirements").instructions[0]
    assert policy.markers == ("policy-wins",)
    assert "policy" in brief.to_planner_projection().lower()
    assert any(
        item.instruction_id == "policy-conflict-runtime-network"
        for item in brief.section("actions").instructions
    )


def test_conflicting_requirements_are_retained_and_require_reconciliation():
    manifest = _java_fragments()[0]
    documentation = BriefFragment(
        instruction_id="java-doc-required",
        subject="java.version",
        role=InputRole.REQUIREMENT,
        value="11",
        text="Repository documentation requires JDK 11.",
        source="repo-doc",
        refs=("repo-doc://README.md#java-version",),
    )

    brief = ProjectBriefComposer().compose(_inputs(), [manifest, documentation])

    assert any(
        item.instruction_id == "reconcile-requirements-java-version"
        for item in brief.section("actions").instructions
    )
    doc_instruction = next(
        item
        for item in brief.section("requirements").instructions
        if item.instruction_id == "java-doc-required"
    )
    assert "UNTRUSTED project input" in doc_instruction.markers


def test_fragment_registration_order_and_duplicate_keys_do_not_change_brief():
    fragments = _java_fragments()
    duplicate = fragments[0].model_copy(update={"refs": ("manifest://parent.xml#java-version",)})
    composer = ProjectBriefComposer()

    forward = composer.compose(_inputs(), [*fragments, duplicate])
    reverse = composer.compose(_inputs(), [duplicate, *reversed(fragments)])

    assert forward.model_dump(mode="json") == reverse.model_dump(mode="json")
    requirement = next(
        item
        for item in forward.section("requirements").instructions
        if item.instruction_id == "java-required"
    )
    assert requirement.refs == (
        "manifest://parent.xml#java-version",
        "manifest://pom.xml#java-version",
    )
