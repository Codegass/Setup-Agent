"""Shared, prose-free contract for project survey ToolResults."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from sag.runtime.paths import BUILD_REQUIREMENTS_PATH

PROJECT_FACT_SHEET_SCHEMA = "sag.project-facts"
PROJECT_FACT_SHEET_VERSION = 1
PROJECT_ANALYSIS_ERROR_SCHEMA = "sag.project-analysis-error"
PROJECT_ANALYSIS_ERROR_VERSION = 1

_LIST_LIMIT = 8
_STRING_LIMIT = 256
_RAW_OUTPUT_LIMIT = 8_000
_METADATA_LIMIT = 32_000

_TOP_LEVEL_SCALAR_FACT_KEYS = (
    "analyzer_version",
    "project_path",
    "project_type",
    "build_system",
    "root_listing",
    "java_version",
    "java_version_source",
    "java_version_enforced",
    "is_multi_module",
    "test_framework",
    "static_test_count",
    "method_count",
    "test_count_method",
    "config_fingerprint",
    "context_updated",
    "context_error",
)

_TOP_LEVEL_LIST_FACT_KEYS = (
    "existing_files",
    "detection_checked",
    "dependencies",
    "plugins",
    "profiles",
    "maven_modules",
    "test_directories",
    "test_patterns",
    "special_requirements",
)

_PARAMETERIZED_INFO_KEYS = (
    "regular_tests",
    "parameterized_methods",
    "parameterized_expansions",
    "repeated_tests",
    "test_factory_methods",
    "test_template_methods",
    "dynamic_tests",
)

_PYTHON_SCALAR_FACT_KEYS = (
    "python_constraint",
    "python_constraint_source",
    "python_distribution_name",
    "python_build_backend",
    "python_root",
    "has_c_extensions",
    "has_native_build",
    "native_build_mode",
)

_PYTHON_LIST_FACT_KEYS = (
    "python_packages",
    "python_declared_dependencies",
    "native_artifact_roots",
)


def _bounded_string(value: Any, *, limit: int = _STRING_LIMIT) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    # The deepest supported public fact is a build-domain coordinate value:
    # envelope -> recommendation -> domains -> domain -> produces -> coordinate
    # -> scalar. Ten leaves defensive headroom without admitting unbounded
    # recursive caller data.
    if depth >= 10:
        return "<depth-limited>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_string(value)
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_value(child, depth=depth + 1)
            for key, child in list(value.items())[:32]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_bounded_value(child, depth=depth + 1) for child in list(value)[:_LIST_LIMIT]]
    return _bounded_string(value)


def _count(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value)
    return int(value not in (None, ""))


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_string(value)
    return None


def _add_bounded_list(
    target: dict[str, Any],
    key: str,
    value: Any,
    *,
    item_keys: Sequence[str] | None = None,
) -> None:
    if not _is_sequence(value):
        return
    if len(value) == 0:
        return
    items: list[Any] = []
    for raw in list(value)[:_LIST_LIMIT]:
        if item_keys is None:
            bounded = _bounded_value(raw)
        elif isinstance(raw, Mapping):
            bounded = {
                field: _bounded_value(raw[field])
                for field in item_keys
                if raw.get(field) not in (None, "", [], {})
            }
        else:
            continue
        if bounded not in (None, "", [], {}):
            items.append(bounded)
    if items:
        target[key] = items
    target[f"{key}_total"] = len(value)


def _documentation_facts(documentation: Any) -> dict[str, Any]:
    if not isinstance(documentation, Mapping):
        return {}
    facts: dict[str, Any] = {}
    for key in ("source_path", "java_version_requirement"):
        value = _scalar(documentation.get(key))
        if value not in (None, ""):
            facts[key] = value
    for key in ("setup_instructions", "build_commands", "test_commands", "requirements"):
        _add_bounded_list(facts, key, documentation.get(key))
    return facts


def _parameterized_facts(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: value[key]
        for key in _PARAMETERIZED_INFO_KEYS
        if isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool)
    }


def _domain_facts(domain: Any) -> dict[str, Any]:
    """Project one physical build domain without accepting arbitrary nesting."""
    if not isinstance(domain, Mapping):
        return {}
    facts: dict[str, Any] = {}
    for key in ("root", "system"):
        value = _scalar(domain.get(key))
        if value not in (None, ""):
            facts[key] = value
    _add_bounded_list(facts, "languages", domain.get("languages"))
    for key in ("produces", "requires"):
        _add_bounded_list(
            facts,
            key,
            domain.get(key),
            item_keys=("group", "name", "version"),
        )
    return facts


def _domain_edge_facts(edge: Any) -> dict[str, Any]:
    """Project the canonical edge schema emitted by ``physical_survey``."""
    if not isinstance(edge, Mapping):
        return {}
    facts: dict[str, Any] = {}
    for key in ("consumer", "producer", "status", "detail"):
        value = edge.get(key)
        if not isinstance(value, str) or not value:
            return {}
        facts[key] = _bounded_string(value)
    return facts


def _coordinate_facts(recommendation: Any) -> dict[str, Any]:
    if not isinstance(recommendation, Mapping):
        return {}

    facts: dict[str, Any] = {}
    for key in ("build_system", "build_root", "test_system", "test_root"):
        value = _scalar(recommendation.get(key))
        if value not in (None, ""):
            facts[key] = value
    for key in ("build_islands", "test_islands"):
        _add_bounded_list(
            facts,
            key,
            recommendation.get(key),
            item_keys=("root", "system"),
        )

    raw_domains = recommendation.get("build_domains")
    if _is_sequence(raw_domains):
        domains: list[dict[str, Any]] = []
        for domain in list(raw_domains)[:_LIST_LIMIT]:
            observed = _domain_facts(domain)
            if observed:
                domains.append(observed)
        if domains:
            facts["build_domains"] = domains
        facts["build_domains_total"] = len(raw_domains)

    raw_edges = recommendation.get("domain_edges")
    if _is_sequence(raw_edges):
        typed_edges: list[dict[str, Any]] = []
        for edge in raw_edges:
            physical = _domain_edge_facts(edge)
            if physical:
                typed_edges.append(physical)
        mismatches = [edge for edge in typed_edges if edge.get("status") == "version_incompatible"]
        other_edges = [edge for edge in typed_edges if edge.get("status") != "version_incompatible"]
        # A bounded public view must never hide a blocker behind earlier
        # compatible links. Preserve relative order within each class.
        edges = (mismatches + other_edges)[:_LIST_LIMIT]
        if edges:
            facts["domain_edges"] = edges
        facts["domain_edges_total"] = len(typed_edges)
        facts["domain_mismatches_total"] = len(mismatches)
    return facts


def _python_facts(config: Any) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    facts: dict[str, Any] = {}
    for key in _PYTHON_SCALAR_FACT_KEYS:
        value = _scalar(config.get(key))
        if value not in (None, ""):
            facts[key] = value
    for key in _PYTHON_LIST_FACT_KEYS:
        _add_bounded_list(facts, key, config.get(key))
    _add_bounded_list(
        facts,
        "python_package_paths",
        config.get("python_package_paths"),
        item_keys=("import_name", "path", "source"),
    )
    _add_bounded_list(
        facts,
        "python_local_providers",
        config.get("python_local_providers"),
        item_keys=("distribution_name", "root", "requirement", "build_backend"),
    )
    _add_bounded_list(
        facts,
        "python_smoke_candidates",
        config.get("python_smoke_candidates"),
        item_keys=("path", "source"),
    )
    return facts


def _test_catalog_summary(analysis: Mapping[str, Any]) -> dict[str, Any]:
    catalog = analysis.get("test_catalog")
    if not isinstance(catalog, Mapping):
        return {}
    summary: dict[str, Any] = {}
    if isinstance(catalog.get("total_count"), int):
        summary["total_count"] = catalog["total_count"]
    by_module = catalog.get("by_module")
    if isinstance(by_module, Mapping):
        clean = {
            _bounded_string(module): count
            for module, count in list(by_module.items())[:_LIST_LIMIT]
            if isinstance(count, int)
        }
        if clean:
            summary["by_module"] = clean
        summary["by_module_total"] = len(by_module)
    return summary


def _compact_metadata_fallback(
    analysis: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    """Guaranteed-small identity and counts when selected facts exceed budget."""
    recommendation = analysis.get("build_recommendation")
    python_config = analysis.get("python_config")
    documentation = analysis.get("documentation")
    compact = {
        "fact_sheet_schema": PROJECT_FACT_SHEET_SCHEMA,
        "fact_sheet_version": PROJECT_FACT_SHEET_VERSION,
        "fact_sheet_truncated": True,
        "authoritative_source": BUILD_REQUIREMENTS_PATH,
        "project_path": _bounded_string(analysis.get("project_path", "")),
        "project_type": _bounded_string(analysis.get("project_type", "unknown")),
        "build_system": _bounded_string(analysis.get("build_system", "unknown")),
        "build_recommendation": {
            key: _bounded_string(recommendation.get(key))
            for key in ("build_system", "build_root", "test_system", "test_root")
            if isinstance(recommendation, Mapping) and recommendation.get(key) not in (None, "")
        },
        "python_config": {
            key: _bounded_string(python_config.get(key))
            for key in (
                "python_distribution_name",
                "python_build_backend",
                "python_root",
                "native_build_mode",
            )
            if isinstance(python_config, Mapping) and python_config.get(key) not in (None, "")
        },
        "fact_counts": {
            "existing_files": _count(analysis.get("existing_files")),
            "dependencies": _count(analysis.get("dependencies")),
            "build_islands": _count(
                recommendation.get("build_islands") if isinstance(recommendation, Mapping) else None
            ),
            "domain_edges": _count(
                recommendation.get("domain_edges") if isinstance(recommendation, Mapping) else None
            ),
            "documentation_build_commands": _count(
                documentation.get("build_commands") if isinstance(documentation, Mapping) else None
            ),
            "selected_top_level_fields": len(facts),
        },
    }
    return compact


def project_fact_sheet_metadata(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Project analyzer state onto the bounded, observed-only public schema."""
    facts: dict[str, Any] = {}
    for key in _TOP_LEVEL_SCALAR_FACT_KEYS:
        value = _scalar(analysis.get(key))
        if value not in (None, ""):
            facts[key] = value
    for key in _TOP_LEVEL_LIST_FACT_KEYS:
        _add_bounded_list(facts, key, analysis.get(key))
    parameterized = _parameterized_facts(analysis.get("parameterized_info"))
    if parameterized:
        facts["parameterized_info"] = parameterized
    documentation = _documentation_facts(analysis.get("documentation"))
    if documentation:
        facts["documentation"] = documentation
    recommendation = _coordinate_facts(analysis.get("build_recommendation"))
    if recommendation:
        facts["build_recommendation"] = recommendation
    python = _python_facts(analysis.get("python_config"))
    if python:
        facts["python_config"] = python
    catalog = _test_catalog_summary(analysis)
    if catalog:
        facts["test_catalog_summary"] = catalog

    # Every admitted field was already bounded by its typed projector. Avoid a
    # second recursive pass: legitimate domain coordinates are six levels deep
    # once wrapped in the public envelope and must not become "<depth-limited>".
    projected = with_project_fact_sheet_identity(facts)
    if len(_json(projected).encode("utf-8")) > _METADATA_LIMIT:
        return _compact_metadata_fallback(analysis, facts)
    return projected


def with_project_fact_sheet_identity(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(metadata),
        "fact_sheet_schema": PROJECT_FACT_SHEET_SCHEMA,
        "fact_sheet_version": PROJECT_FACT_SHEET_VERSION,
    }


def is_project_fact_sheet_metadata(metadata: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(metadata, Mapping)
        and metadata.get("fact_sheet_schema") == PROJECT_FACT_SHEET_SCHEMA
        and metadata.get("fact_sheet_version") == PROJECT_FACT_SHEET_VERSION
    )


def project_fact_sheet_identity(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not is_project_fact_sheet_metadata(metadata):
        return {}
    return {
        "fact_sheet_schema": PROJECT_FACT_SHEET_SCHEMA,
        "fact_sheet_version": PROJECT_FACT_SHEET_VERSION,
    }


def _reported_totals(metadata: Mapping[str, Any]) -> dict[str, int]:
    totals: dict[str, int] = {}

    def collect(prefix: str, value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for key, raw in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key.endswith("_total") and isinstance(raw, int) and not isinstance(raw, bool):
                totals[path[: -len("_total")]] = raw

    collect("", metadata)
    collect("documentation", metadata.get("documentation"))
    collect("build_coordinates", metadata.get("build_recommendation"))
    collect("python", metadata.get("python_config"))
    collect("test_catalog", metadata.get("test_catalog_summary"))
    return totals


def _raw_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    documentation = metadata.get("documentation")
    if not isinstance(documentation, Mapping):
        documentation = {}
    payload = {
        "schema": PROJECT_FACT_SHEET_SCHEMA,
        "version": PROJECT_FACT_SHEET_VERSION,
        "project": {
            key: metadata[key]
            for key in ("project_path", "project_type", "build_system", "java_version")
            if metadata.get(key) not in (None, "")
        },
        "build_coordinates": metadata.get("build_recommendation") or {},
        "python": metadata.get("python_config") or {},
        "testing": {
            key: metadata[key]
            for key in (
                "test_framework",
                "static_test_count",
                "method_count",
                "test_count_method",
            )
            if metadata.get(key) not in (None, "")
        },
        "documentation": {
            "java_version_requirement": documentation.get("java_version_requirement"),
            "build_commands": documentation.get("build_commands") or [],
            "test_commands": documentation.get("test_commands") or [],
        },
        "files": metadata.get("existing_files") or [],
        "persistence": {
            "context_updated": metadata.get("context_updated"),
            "context_error": metadata.get("context_error"),
        },
        "truncated": bool(metadata.get("fact_sheet_truncated")),
        "fact_counts": metadata.get("fact_counts") or {},
        "fact_totals": _reported_totals(metadata),
        "authoritative_source": metadata.get("authoritative_source") or BUILD_REQUIREMENTS_PATH,
    }
    # The serializer is public and can also receive a caller-built schema
    # envelope. Its defensive walk therefore remains, with enough typed depth
    # for the canonical domain-coordinate graph.
    return dict(_bounded_value(payload))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def serialize_project_fact_sheet(metadata: Mapping[str, Any]) -> str:
    """Return valid JSON below BaseTool's generic 10k truncation threshold."""
    payload = _raw_payload(metadata)
    encoded = _json(payload)
    if len(encoded) <= _RAW_OUTPUT_LIMIT:
        return encoded

    fallback = {
        "schema": PROJECT_FACT_SHEET_SCHEMA,
        "version": PROJECT_FACT_SHEET_VERSION,
        "truncated": True,
        "authoritative_source": BUILD_REQUIREMENTS_PATH,
        "project": payload.get("project") or {},
        "build_coordinates": {
            key: (payload.get("build_coordinates") or {}).get(key)
            for key in ("build_system", "build_root", "test_system", "test_root")
            if (payload.get("build_coordinates") or {}).get(key) not in (None, "")
        },
        "python": {
            key: (payload.get("python") or {}).get(key)
            for key in (
                "python_distribution_name",
                "python_build_backend",
                "python_root",
                "native_build_mode",
            )
            if (payload.get("python") or {}).get(key) not in (None, "")
        },
        "fact_counts": {
            **(
                payload.get("fact_counts")
                if isinstance(payload.get("fact_counts"), Mapping)
                else {}
            ),
            "files": _count(payload.get("files")),
            "build_islands": _count((payload.get("build_coordinates") or {}).get("build_islands")),
            "domain_edges": _count((payload.get("build_coordinates") or {}).get("domain_edges")),
            "python_fields": _count(payload.get("python")),
        },
        "fact_totals": payload.get("fact_totals") or {},
    }
    # ``serialize_project_fact_sheet`` is public and may receive a caller-built
    # schema envelope rather than projector output. Bound this shallow minimal
    # fallback defensively; it contains no nested domain graph to corrupt.
    encoded = _json(_bounded_value(fallback))
    if len(encoded) > _RAW_OUTPUT_LIMIT:
        raise ValueError("minimal project fact sheet exceeds raw output budget")
    return encoded


def project_analysis_error_metadata(code: str, **facts: Any) -> dict[str, Any]:
    metadata = {
        "analysis_error_schema": PROJECT_ANALYSIS_ERROR_SCHEMA,
        "analysis_error_version": PROJECT_ANALYSIS_ERROR_VERSION,
        "analysis_error_code": _bounded_string(code),
        "error_facts": _bounded_value(facts),
    }
    if len(_json(metadata)) > _RAW_OUTPUT_LIMIT:
        metadata["error_facts"] = {
            "truncated": True,
            "fact_keys": [_bounded_string(key) for key in list(facts)[:_LIST_LIMIT]],
        }
    return metadata


def is_project_analysis_error_metadata(metadata: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(metadata, Mapping)
        and metadata.get("analysis_error_schema") == PROJECT_ANALYSIS_ERROR_SCHEMA
        and metadata.get("analysis_error_version") == PROJECT_ANALYSIS_ERROR_VERSION
        and isinstance(metadata.get("analysis_error_code"), str)
    )


def serialize_project_analysis_error(metadata: Mapping[str, Any]) -> str:
    payload = {
        "schema": PROJECT_ANALYSIS_ERROR_SCHEMA,
        "version": PROJECT_ANALYSIS_ERROR_VERSION,
        "code": metadata.get("analysis_error_code"),
        "facts": metadata.get("error_facts") or {},
    }
    encoded = _json(payload)
    if len(encoded) <= _RAW_OUTPUT_LIMIT:
        return encoded
    return _json(
        {
            "schema": PROJECT_ANALYSIS_ERROR_SCHEMA,
            "version": PROJECT_ANALYSIS_ERROR_VERSION,
            "code": _bounded_string(metadata.get("analysis_error_code")),
            "facts": {"truncated": True},
        }
    )
