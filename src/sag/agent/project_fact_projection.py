"""Engine-owned model projection for structured project-survey facts.

The analyzer is a surveyor: its result is a machine-readable fact sheet.  The
agent engine owns the prose shown to the model.  Keeping those two products
separate prevents the analyzer from growing another prescription channel while
still giving weaker models a compact, consistent view of the observed facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sag.project_fact_sheet import is_project_analysis_error_metadata
from sag.runtime.paths import BUILD_REQUIREMENTS_PATH

_FACT_ATOM_LIMIT = 160
_PROJECTION_ITEM_LIMIT = 8
_PROJECTION_TOTAL_LIMIT = 12_000

_ANALYSIS_ERROR_PROJECTIONS = {
    "ANALYSIS_INVALID_PARAMETERS": (
        "The project survey call contained unsupported parameters.",
        (
            "Use project(action='analyze', project_path=...) with only the "
            "documented facade fields.",
        ),
    ),
    "PROJECT_NOT_FOUND": (
        "No project was found at the requested survey path.",
        (
            "Confirm that cloning completed and retry with the exact checkout path.",
            "Use a bounded file listing only if the checkout location is still unknown.",
        ),
    ),
    "ANALYSIS_FAILED": (
        "The survey could not establish a valid project structure.",
        (
            "Check that project build files are readable at the surveyed path.",
            "Record the project as unknown if physical evidence remains inconclusive.",
        ),
    ),
    "ANALYSIS_INVALID_ACTION": (
        "The project survey action is invalid.",
        ("Use project(action='analyze').",),
    ),
    "ANALYSIS_EXCEPTION": (
        "The project survey encountered an internal error.",
        ("Retry only after the underlying checkout or survey environment changes.",),
    ),
}


def _fact_atom(value: Any, *, limit: int = _FACT_ATOM_LIMIT) -> str:
    """Render one project-controlled fact as a bounded single line."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _scalar_items(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, (str, int, float, bool))]


def _declared_total(container: Mapping[str, Any], key: str, shown: int) -> int:
    value = container.get(f"{key}_total")
    if isinstance(value, int) and not isinstance(value, bool) and value >= shown:
        return value
    return shown


def _omitted_suffix(total: int, shown: int) -> str:
    omitted = max(0, total - shown)
    if not omitted:
        return ""
    return f" (+{omitted} more in {BUILD_REQUIREMENTS_PATH})"


def _is_renderable_domain_mismatch(edge: Mapping[str, Any]) -> bool:
    return edge.get("status") == "version_incompatible" and all(
        isinstance(edge.get(key), str) and bool(edge.get(key))
        for key in ("consumer", "producer", "detail")
    )


def render_project_analysis_error(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Project a typed analyzer failure into bounded model-facing copy."""
    if not is_project_analysis_error_metadata(metadata):
        return {}
    code = str(metadata.get("analysis_error_code"))
    message, suggestions = _ANALYSIS_ERROR_PROJECTIONS.get(
        code,
        (
            "The project survey failed.",
            ("Inspect the typed failure code before deciding whether to retry.",),
        ),
    )
    facts = metadata.get("error_facts")
    details = ""
    if isinstance(facts, Mapping) and facts:
        details = ", ".join(
            f"{_fact_atom(key)}={_fact_atom(value, limit=240)}"
            for key, value in list(facts.items())[:_PROJECTION_ITEM_LIMIT]
        )
    return {
        "message": message,
        "details": details,
        "suggestions": list(suggestions),
    }


def render_domain_mismatches(edges: Any, *, total: int | None = None) -> str:
    """Render physically observed version-incompatible domain edges."""
    from sag.agent.physical_survey import domain_mismatch_clause

    raw_edges = _mapping_items(edges)
    clauses = []
    for edge in raw_edges[:_PROJECTION_ITEM_LIMIT]:
        if not _is_renderable_domain_mismatch(edge):
            continue
        clause = domain_mismatch_clause(edge)
        if clause:
            clauses.append(_fact_atom(clause, limit=400))
    observed_total = max(len(clauses), total or 0)
    if observed_total == 0:
        return ""
    lines = "\n".join(
        f"   • {clause} — record the mismatch, do not silently alias" for clause in clauses
    )
    omitted = max(0, observed_total - len(clauses))
    if omitted:
        prefix = "\n" if lines else ""
        lines += f"{prefix}   • … {omitted} more mismatches remain in " f"{BUILD_REQUIREMENTS_PATH}"
    return (
        "\n⚠️ Coordinate mismatch between build domains "
        f"({observed_total} observed, before any attempt):\n{lines}\n"
    )


def render_recommended_build_facts(metadata: Mapping[str, Any]) -> str:
    """Project build coordinates without recommendation prose."""
    recommendation = metadata.get("build_recommendation") or {}
    if not isinstance(recommendation, Mapping) or not recommendation:
        return ""
    islands = _mapping_items(recommendation.get("build_islands"))
    island_total = _declared_total(recommendation, "build_islands", len(islands))
    if island_total > 1:
        coordinates = "; ".join(
            f"{_fact_atom(island.get('system') or 'unknown')} in "
            f"{_fact_atom(island.get('root'))}"
            for island in list(islands)[:_PROJECTION_ITEM_LIMIT]
        )
        if island_total > _PROJECTION_ITEM_LIMIT:
            coordinates += (
                f"; … +{island_total - _PROJECTION_ITEM_LIMIT} more "
                f"in {BUILD_REQUIREMENTS_PATH}"
            )
        edges = _mapping_items(recommendation.get("domain_edges"))
        edge_total = _declared_total(recommendation, "domain_edges", len(edges))
        mismatch_count = sum(_is_renderable_domain_mismatch(edge) for edge in edges)
        mismatch_total = (
            _declared_total(
                recommendation,
                "domain_mismatches",
                mismatch_count,
            )
            if mismatch_count
            else 0
        )
        label = "independent islands" if edge_total == 0 else "coordinate-linked domains"
        projection = (
            render_domain_mismatches(edges, total=mismatch_total)
            + f"\n🏝️ Build coordinates ({label}): {coordinates}\n"
        )
        omitted_edges = max(0, edge_total - len(edges))
        if omitted_edges:
            projection += (
                f"🔗 Domain graph: {len(edges)} of {edge_total} observed links shown; "
                f"{omitted_edges} more in {BUILD_REQUIREMENTS_PATH}.\n"
            )
        return projection
    return (
        f"\n📍 Build coordinates: {_fact_atom(recommendation.get('build_system'))} at "
        f"{_fact_atom(recommendation.get('build_root'))}\n"
    )


def render_python_facts(metadata: Mapping[str, Any]) -> str:
    """Project a bounded, descriptive subset of Python survey facts."""
    config = metadata.get("python_config") or {}
    if not isinstance(config, Mapping) or not config:
        return ""

    lines: list[str] = []

    def scalar(label: str, key: str) -> None:
        value = _fact_atom(config.get(key))
        if value:
            lines.append(f"   • {label}: {value}")

    def bounded(
        label: str,
        values: list[str],
        *,
        total: int | None = None,
        limit: int = 3,
    ) -> None:
        clean = [value for value in values if value]
        if not clean:
            return
        rendered = ", ".join(clean[:limit])
        rendered += _omitted_suffix(max(len(clean), total or 0), min(len(clean), limit))
        lines.append(f"   • {label}: {rendered}")

    scalar("Distribution", "python_distribution_name")
    scalar("Build backend", "python_build_backend")
    scalar("Install root", "python_root")

    providers: list[str] = []
    providers_raw = _mapping_items(config.get("python_local_providers"))
    for provider in providers_raw:
        name = _fact_atom(provider.get("distribution_name"))
        root = _fact_atom(provider.get("root"))
        if name and root:
            providers.append(f"{name} at {root}")
    bounded(
        "Local providers",
        providers,
        total=_declared_total(config, "python_local_providers", len(providers_raw)),
    )

    artifact_values = _scalar_items(config.get("native_artifact_roots"))
    artifact_roots = [_fact_atom(root) for root in artifact_values]
    bounded(
        "Native artifact roots",
        artifact_roots,
        total=_declared_total(config, "native_artifact_roots", len(artifact_values)),
    )

    smoke_coordinates: list[str] = []
    smoke_raw = _mapping_items(config.get("python_smoke_candidates"))
    for candidate in smoke_raw:
        path = _fact_atom(candidate.get("path"))
        source = _fact_atom(candidate.get("source"))
        if path:
            smoke_coordinates.append(f"{path} ({source})" if source else path)
    bounded(
        "Verified smoke coordinates",
        smoke_coordinates,
        total=_declared_total(config, "python_smoke_candidates", len(smoke_raw)),
    )

    if not lines:
        return ""
    return "\n🐍 Python facts (observed):\n" + "\n".join(lines) + "\n"


def render_project_fact_sheet(metadata: Mapping[str, Any]) -> str:
    """Render analyzer facts at the engine/model boundary."""
    if not isinstance(metadata, Mapping):
        return "⚠️ Project survey facts are malformed; no fact projection is available."
    output = "🔍 PROJECT ANALYSIS COMPLETED\n\n"

    project_path = _fact_atom(metadata.get("project_path", "Unknown"), limit=400)
    output += f"📁 Analyzed Path: {project_path}\n"

    project_type = _fact_atom(metadata.get("project_type", "Unknown"))
    build_system = _fact_atom(metadata.get("build_system", "Unknown"))
    output += f"📂 Project Type: {project_type}\n"
    output += f"🔧 Build System: {build_system}\n"

    recommendation = metadata.get("build_recommendation") or {}
    if not isinstance(recommendation, Mapping):
        recommendation = {}
    if recommendation:
        output += render_recommended_build_facts(metadata)
        test_root = recommendation.get("test_root")
        if test_root and (
            test_root != recommendation.get("build_root")
            or (
                recommendation.get("test_system") != recommendation.get("build_system")
                and str(recommendation.get("build_system", "")).strip().lower() != "python"
            )
        ):
            output += (
                f"🧪 Test coordinates: {_fact_atom(recommendation.get('test_system'))} "
                f"at {_fact_atom(test_root, limit=400)}\n"
            )

    output += render_python_facts(metadata)

    existing_files = _scalar_items(metadata.get("existing_files"))
    existing_files_total = _declared_total(metadata, "existing_files", len(existing_files))
    if existing_files:
        output += (
            "📄 Project Files Found: "
            + ", ".join(_fact_atom(path) for path in list(existing_files)[:5])
            + "\n"
        )
        if existing_files_total > 5:
            output += (
                f"    ... and {existing_files_total - 5} more files "
                f"in {BUILD_REQUIREMENTS_PATH}\n"
            )
    elif not (
        metadata.get("fact_sheet_truncated")
        and isinstance(metadata.get("fact_counts"), Mapping)
        and metadata["fact_counts"].get("existing_files")
    ):
        output += "⚠️ No project files detected\n"
    else:
        output += (
            f"📄 Project Files Found: {metadata['fact_counts']['existing_files']} "
            f"(list compacted; see {BUILD_REQUIREMENTS_PATH})\n"
        )

    if str(project_type).lower() == "unknown":
        checked = _scalar_items(metadata.get("detection_checked"))
        if checked:
            checked_total = _declared_total(metadata, "detection_checked", len(checked))
            output += (
                "🔎 Detection evidence: checked for "
                + ", ".join(_fact_atom(item) for item in list(checked)[:_PROJECTION_ITEM_LIMIT])
                + _omitted_suffix(
                    checked_total,
                    min(len(checked), _PROJECTION_ITEM_LIMIT),
                )
                + " — none present\n"
            )
        root_listing = metadata.get("root_listing")
        if root_listing:
            output += "📁 Project root contains:\n" f"{_fact_atom(root_listing, limit=1_000)}\n"
        output += (
            "⚠️ This 'unknown' verdict is a detection result, not ground truth — "
            "if build evidence exists (wrapper scripts, compiled artifacts), trust that instead.\n"
        )

    if metadata.get("java_version"):
        output += f"☕ Java Version: {_fact_atom(metadata['java_version'])}\n"

    dependencies = _scalar_items(metadata.get("dependencies"))
    if dependencies:
        dependencies_total = _declared_total(metadata, "dependencies", len(dependencies))
        output += (
            f"📦 Dependencies: {dependencies_total} found "
            f"({', '.join(_fact_atom(item) for item in list(dependencies)[:3])}...)\n"
        )
    elif (
        metadata.get("fact_sheet_truncated")
        and isinstance(metadata.get("fact_counts"), Mapping)
        and metadata["fact_counts"].get("dependencies")
    ):
        output += (
            f"📦 Dependencies: {metadata['fact_counts']['dependencies']} found "
            f"(list compacted; see {BUILD_REQUIREMENTS_PATH})\n"
        )

    documentation = metadata.get("documentation", {})
    if not isinstance(documentation, Mapping):
        documentation = {}
    if documentation.get("java_version_requirement"):
        output += (
            "📋 Required Java Version: "
            f"{_fact_atom(documentation['java_version_requirement'])}\n"
        )
    build_commands = _scalar_items(documentation.get("build_commands"))
    if build_commands:
        build_total = _declared_total(documentation, "build_commands", len(build_commands))
        output += (
            "🔨 Build Commands Found: "
            + ", ".join(_fact_atom(command, limit=300) for command in list(build_commands)[:3])
            + _omitted_suffix(build_total, min(len(build_commands), 3))
            + "\n"
        )
    test_commands = _scalar_items(documentation.get("test_commands"))
    if test_commands:
        test_total = _declared_total(documentation, "test_commands", len(test_commands))
        output += (
            "🧪 Test Commands Found: "
            + ", ".join(_fact_atom(command, limit=300) for command in list(test_commands)[:3])
            + _omitted_suffix(test_total, min(len(test_commands), 3))
            + "\n"
        )

    test_framework = _fact_atom(metadata.get("test_framework", "unknown"))
    if test_framework and test_framework.lower() != "unknown":
        output += f"🧪 Test Framework: {test_framework}\n"

    static_test_count = metadata.get("static_test_count")
    method_count = metadata.get("method_count")
    test_count_method = _fact_atom(metadata.get("test_count_method", "unknown"))
    numeric_static_count = isinstance(static_test_count, (int, float)) and not isinstance(
        static_test_count, bool
    )
    numeric_method_count = isinstance(method_count, (int, float)) and not isinstance(
        method_count, bool
    )
    if numeric_static_count:
        if test_count_method == "accurate_expansion_counting":
            output += "📊 Test Count Analysis (Accurate with Expansions):\n"
            output += (
                f"   • Total Test Cases: {static_test_count} "
                "(includes parameterized expansions)\n"
            )
            if numeric_method_count and method_count and method_count != static_test_count:
                output += (
                    f"   • Method Annotations: {method_count} "
                    "(@Test, @ParameterizedTest, etc.)\n"
                )
                expansion = static_test_count / method_count if method_count > 0 else 1
                output += f"   • Expansion Factor: {expansion:.1f}x " "(from parameterized tests)\n"
            parameterized_info = metadata.get("parameterized_info", {})
            if isinstance(parameterized_info, Mapping) and parameterized_info:
                regular = parameterized_info.get("regular_tests", 0)
                parameterized = parameterized_info.get("parameterized_expansions", 0)
                if (
                    isinstance(regular, (int, float))
                    and isinstance(parameterized, (int, float))
                    and (regular or parameterized)
                ):
                    output += (
                        f"   • Breakdown: {regular} regular tests + "
                        f"{parameterized} parameterized expansions\n"
                    )
        elif test_count_method == "actual_executions":
            output += (
                f"📊 Test Count: {static_test_count} actual test executions "
                "(from test reports)\n"
            )
            output += "   ℹ️ This includes all parameterized test expansions\n"
        else:
            output += f"📊 Test Count: {static_test_count} test method annotations found\n"
            output += "   ℹ️ Note: Parameterized tests will execute multiple times\n"

    if metadata.get("fact_sheet_truncated"):
        counts = metadata.get("fact_counts")
        rendered_counts = ""
        if isinstance(counts, Mapping):
            pairs = [
                f"{_fact_atom(key)}={_fact_atom(value)}"
                for key, value in list(counts.items())[:_PROJECTION_ITEM_LIMIT]
            ]
            if pairs:
                rendered_counts = " Retained counts: " + ", ".join(pairs) + "."
        output += (
            "\n⚠️ Public fact sheet compacted because its metadata budget was exceeded."
            f"{rendered_counts} Full survey manifest: {BUILD_REQUIREMENTS_PATH}\n"
        )

    if metadata.get("context_updated"):
        output += "\n✅ Survey facts recorded on the trunk\n"
    elif metadata.get("context_updated") is False:
        output += "\n⚠️ Survey facts could not be recorded on the trunk.\n"

    if project_type.strip().lower() != "unknown" and build_system.strip().lower() != "unknown":
        output += "\n🎯 Survey complete — facts persisted for the build/test phases."
    else:
        output += "\n⚠️ Project analysis incomplete - manual investigation may be needed"
    if len(output) > _PROJECTION_TOTAL_LIMIT:
        suffix = (
            "\n… [project fact projection truncated; "
            f"full survey facts remain in {BUILD_REQUIREMENTS_PATH}]"
        )
        return output[: _PROJECTION_TOTAL_LIMIT - len(suffix)] + suffix
    return output
