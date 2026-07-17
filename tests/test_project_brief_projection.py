from sag.agent.project_brief import (
    BriefFragment,
    InputRole,
    ProjectBriefComposer,
    ProjectBriefInputs,
)


def _inputs(build_roots=()):
    return ProjectBriefInputs(
        manifest={"pyproject.toml": "[build-system]"},
        detected_toolchain={"build_system": "python"},
        submodule_state=(),
        build_roots=build_roots,
        repo_docs={"README.md": "Build native core before installing Python."},
        analyzer_version="analyzer-v1",
        composer_version="composer-v1",
    )


def _fragment(instruction_id, text, *, subject=None, source="analyzer"):
    return BriefFragment(
        instruction_id=instruction_id,
        subject=subject or instruction_id,
        role=InputRole.REQUIREMENT,
        value=text,
        text=text,
        source=source,
        refs=(f"{source}://{instruction_id}",),
    )


def test_tvm_shape_dedupes_fragments_and_projects_under_1200_chars():
    fragments = [
        _fragment("install-deps", "Create the venv and install Python dependencies."),
        _fragment("install-deps", "Create the venv and install Python dependencies."),
        _fragment(
            "native-first",
            "Build the native core before installing the Python package.",
            source="repo-doc",
        ),
        _fragment("test-command", "Run pytest through build(action='test')."),
    ]
    recommendation = {
        "build_root": "python",
        "build_system": "python",
        "goal": "deps",
    }

    brief = ProjectBriefComposer().compose(
        _inputs(), fragments, build_recommendation=recommendation
    )
    projection = brief.to_planner_projection(max_chars=1200)

    instruction_ids = [
        item.instruction_id for section in brief.sections for item in section.instructions
    ]
    assert instruction_ids.count("install-deps") == 1
    assert instruction_ids.count("native-first") == 1
    assert instruction_ids.count("test-command") == 1
    assert len(brief.section("recommended-build").build_steps) == 1
    assert len(projection) <= 1200
    assert "UNTRUSTED project input" in projection
    assert "project_brief.json" in projection
    words = projection.lower().split()
    grams = [tuple(words[index : index + 12]) for index in range(len(words) - 11)]
    assert len(grams) == len(set(grams))


def test_large_projection_is_bounded_and_points_to_complete_artifact():
    fragments = [_fragment(f"requirement-{index}", "x" * 180) for index in range(30)]
    brief = ProjectBriefComposer().compose(_inputs(), fragments)

    projection = brief.to_planner_projection(max_chars=1200)

    assert len(projection) <= 1200
    assert "omitted=" in projection
    assert "/workspace/.setup_agent/project_brief.json" in projection


def test_island_recommendation_renders_a_dag_with_visible_dependencies():
    islands = [
        {
            "root": "bigtop-bigpetstore/bigpetstore-transaction-queue",
            "system": "gradle",
            "goal": "build",
        },
        {
            "root": "bigtop-data-generators",
            "system": "gradle",
            "goal": "publishToMavenLocal",
        },
        {
            "root": "bigtop-test-framework",
            "system": "maven",
            "goal": "install",
        },
    ]
    recommendation = {"build_islands": islands}

    brief = ProjectBriefComposer().compose(
        _inputs(build_roots=tuple(islands)),
        [],
        build_recommendation=recommendation,
    )

    steps = brief.section("recommended-build").build_steps
    assert [step.root for step in steps[:2]] == [
        "bigtop-data-generators",
        "bigtop-test-framework",
    ]
    transaction = next(step for step in steps if "transaction-queue" in step.root)
    assert transaction.depends_on == (
        "bigtop-data-generators",
        "bigtop-test-framework",
    )
    assert "depends_on=" in brief.to_planner_projection()
