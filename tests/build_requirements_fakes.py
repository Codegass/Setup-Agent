"""Canonical build-requirements v1 fixtures for strict live-authority tests."""

from __future__ import annotations

from typing import Any

from sag.tools.internal.build_preflight import (
    BUILD_REQUIREMENTS_SCHEMA_VERSION,
    survey_facts_fingerprint,
)
from sag.tools.internal.project_analyzer import SURVEY_FACTS_VERSION


def complete_build_requirements_v1(
    *,
    project_root: str = "/workspace/project",
    build_system: str = "maven",
    target_sha: str | None = None,
    config_fingerprint: str | None = None,
    document_map_fingerprint: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return the smallest complete current manifest, with explicit null pins.

    Callers may add one complete optional group (for example all Python keys)
    through ``overrides``.  The helper intentionally does not normalize those
    values; the production validator remains the authority on whether the
    resulting fixture is legal.
    """

    manifest: dict[str, Any] = {
        "schema_version": BUILD_REQUIREMENTS_SCHEMA_VERSION,
        "survey": {
            "project_path": project_root,
            "analyzer_version": SURVEY_FACTS_VERSION,
            "config_fingerprint": config_fingerprint,
            "target_sha": target_sha,
            "document_map_fingerprint": document_map_fingerprint,
        },
        "java_version": None,
        "java_version_source": None,
        "java_version_enforced": False,
        "root_shape": "single_module",
        "build_root": project_root,
        "fail_at_end": False,
        "test_root": project_root,
        "test_system": build_system,
        "test_fail_at_end": False,
        "build_islands": [],
        "test_islands": [],
    }
    manifest.update(overrides)
    # The production writer owns this stamp; the fixture mirrors it so a
    # helper-built manifest is publishable as-is. A caller-supplied survey
    # block that already states a fingerprint is respected verbatim — that is
    # how forged/stale-pin negative cases are built.
    survey = manifest.get("survey")
    if isinstance(survey, dict) and "survey_fingerprint" not in survey:
        manifest["survey"] = {
            **survey,
            "survey_fingerprint": survey_facts_fingerprint(manifest),
        }
    return manifest


def complete_python_build_requirements_v1(
    *,
    project_root: str = "/workspace/project",
    dependencies: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return one complete v1 manifest with the all-or-nothing Python group."""

    python_fields: dict[str, Any] = {
        "python_version": "3.11",
        "python_constraint": ">=3.11",
        "python_constraint_source": "pyproject.toml",
        "python_installer": "pip",
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
        "python_install_note": None,
        "python_install_source": "pyproject.toml",
        "python_packages": ["project"],
        "python_distribution_name": "project",
        "python_build_backend": "setuptools.build_meta",
        "python_declared_dependencies": list(dependencies or []),
        "python_package_paths": [],
        "python_local_providers": [],
        "python_smoke_candidates": [],
        "python_venv": f"{project_root}/.venv",
        "python_root": project_root,
        "has_c_extensions": False,
        "has_native_build": False,
        "native_build_mode": None,
        "native_artifact_roots": [],
        "test_hints": {"pytest_args": None, "test_deps": []},
    }
    python_fields.update(overrides)
    return complete_build_requirements_v1(
        project_root=project_root,
        build_system="pytest",
        **python_fields,
    )
