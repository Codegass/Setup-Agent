#!/usr/bin/env python3
"""Freeze reviewable CI requirements without building or installing a project.

The archived manifest supplies task identity, never requirement authority via its
legacy ``stages`` field.  Ordinary POMs and CI goal banners give a lower bound;
only separately captured effective POMs can supply resolved artifact defaults.
All unresolved execution plans remain mandatory, unavailable requirements.
This module intentionally uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from sag.benchmark.ci_native import ci_native_text, ci_binding_inventory
from sag.benchmark.empty_test_selection import reviewed_empty_surefire_selection

POLICY_VERSION = "ci-requirements-v2"
PHASES = [
    "validate",
    "initialize",
    "generate-sources",
    "process-sources",
    "generate-resources",
    "process-resources",
    "compile",
    "process-classes",
    "generate-test-sources",
    "process-test-sources",
    "generate-test-resources",
    "process-test-resources",
    "test-compile",
    "process-test-classes",
    "test",
    "prepare-package",
    "package",
    "pre-integration-test",
    "integration-test",
    "post-integration-test",
    "verify",
    "install",
    "deploy",
]
VALUE_OPTIONS = {
    "-f",
    "--file",
    "-pl",
    "--projects",
    "-P",
    "--activate-profiles",
    "-s",
    "--settings",
    "-gs",
    "--global-settings",
    "-t",
    "--toolchains",
    "-gt",
    "--global-toolchains",
    "-T",
    "--threads",
    "-rf",
    "--resume-from",
    "-D",
    "--define",
    "-l",
    "--log-file",
}
CHECK_GOALS = {
    "apache-rat:check": "license",
    "japicmp:cmp": "api_compatibility",
    "spotbugs:check": "static_analysis",
    "pmd:check": "static_analysis",
    "pmd:cpd-check": "duplicate_code",
    "checkstyle:check": "style",
    "enforcer:enforce": "environment_and_dependency_policy",
    "spotless:check": "format",
    "jacoco:check": "coverage_threshold",
    "artifact:check-buildplan": "build_plan",
}
GOAL_BANNER = re.compile(
    r"---\s+(?:[\w.\-]+:)?([\w.\-]+):([^\s:]+):([^\s:]+)\s+\(([^)]+)\)\s+@\s+([^\s]+)\s+---"
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()


def task_digest(task: dict) -> str:
    """Match AcceptanceTask's defaults and legacy Maven-version serializer."""
    normalized = {"schema_version": 1, **task, "steps": []}
    for step in task["steps"]:
        item = {"cwd": ".", "java_major": None, **step}
        if item.get("maven_version") is None:
            item.pop("maven_version", None)
        normalized["steps"].append(item)
    return digest(normalized)


def xml_root(raw: bytes) -> ET.Element:
    root = ET.fromstring(raw)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return root


def text_at(root: ET.Element, path: str) -> str | None:
    found = root.findtext(path)
    return found.strip() if found and found.strip() else None


def literal(value: str | None) -> bool:
    return bool(value) and "${" not in value


def maven_goals(argv: list[str]) -> list[str]:
    goals, skip = [], False
    for token in argv[1:]:
        if skip:
            skip = False
        elif token in VALUE_OPTIONS:
            skip = True
        elif not token.startswith("-"):
            goals.append(token)
    return goals


def lifecycle_endpoint(goals: list[str]) -> str | None:
    found = [phase for phase in goals if phase in PHASES]
    return max(found, key=PHASES.index) if found else None


def normalize_goal(goal: str) -> str:
    parts = goal.split(":")
    if len(parts) < 2:
        return goal
    prefix = parts[-2] if len(parts) == 2 else parts[1]
    if prefix.startswith("maven-") and prefix.endswith("-plugin"):
        prefix = prefix[6:-7]
    return prefix + ":" + parts[-1]


def skip_tests(argv: list[str]) -> bool:
    props = {}
    for item in argv:
        if item.startswith("-D"):
            key, _, value = item[2:].partition("=")
            props[key] = value.lower() if value else "true"
    return any(props.get(key) == "true" for key in ("skipTests", "maven.test.skip"))


def import_effective_pom(
    raw: bytes,
    *,
    module: str,
    endpoint: str | None,
    local_repository: str | None,
    provenance: dict,
    bundle_mapping_confirmed: bool = False,
) -> dict:
    """Read one *effective* POM; never mistake a source POM for one.

    ``provenance`` must bind the metadata preparation to its task SHA, reactor,
    command/configuration, Maven/JDK versions and output bytes. This function
    validates the binding information; a caller must archive the referenced
    metadata bytes. It never starts Maven or downloads/prewarms an agent cache.
    """
    required = {
        "task_sha256",
        "commit",
        "argv",
        "java_major",
        "maven_version",
        "reactor_resolved",
        "sha256",
    }
    gaps = [
        "effective_pom_provenance_missing:" + key for key in sorted(required - provenance.keys())
    ]
    if not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("task_sha256", ""))):
        gaps.append("effective_pom_task_identity_invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", str(provenance.get("commit", ""))):
        gaps.append("effective_pom_commit_invalid")
    if not isinstance(provenance.get("argv"), list) or not provenance.get("argv"):
        gaps.append("effective_pom_resolution_command_missing")
    if type(provenance.get("java_major")) is not int or provenance.get("java_major", 0) < 1:
        gaps.append("effective_pom_runtime_invalid")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(provenance.get("maven_version", ""))):
        gaps.append("effective_pom_maven_version_invalid")
    if provenance.get("sha256") != hashlib.sha256(raw).hexdigest():
        gaps.append("effective_pom_hash_mismatch")
    if provenance.get("reactor_resolved") is not True:
        gaps.append("reactor_unresolved")
    root = xml_root(raw)
    if root.tag != "project":
        raise ValueError("Import each effective reactor project separately; expected project root")
    packaging = text_at(root, "packaging") or "jar"
    group = text_at(root, "groupId") or text_at(root, "parent/groupId")
    artifact = text_at(root, "artifactId")
    version = text_at(root, "version") or text_at(root, "parent/version")
    coords = {"group_id": group, "artifact_id": artifact, "version": version}
    if not all(literal(value) for value in coords.values()):
        gaps.append("coordinates_unresolved")
    final_name = text_at(root, "build/finalName")
    build_directory = text_at(root, "build/directory")
    classes_directory = text_at(root, "build/outputDirectory")
    artifacts: list[dict] = []
    reaches_package = endpoint in PHASES and PHASES.index(endpoint) >= PHASES.index("package")
    reaches_install = endpoint in {"install", "deploy"}
    if packaging not in {"jar", "bundle", "war", "pom"}:
        gaps.append("custom_packaging:" + packaging)
    if packaging == "bundle" and not bundle_mapping_confirmed:
        gaps.append("bundle_extension_mapping_unconfirmed")
    # Custom artifact production needs a reviewed exception, not an assumed JAR.
    for plugin in root.findall("build/plugins/plugin") if reaches_package else []:
        aid = text_at(plugin, "artifactId") or "unknown-plugin"
        if aid in {"maven-shade-plugin", "maven-assembly-plugin"}:
            gaps.append("artifact_plugin_requires_review:" + aid)
        if any(text_at(c, "skipIfEmpty") == "true" for c in plugin.findall(".//configuration")):
            gaps.append("skip_if_empty_requires_review:" + aid)
        if aid in {"maven-jar-plugin", "maven-war-plugin"}:
            configurations = plugin.findall("configuration")
            for execution in plugin.findall("executions/execution"):
                goals = {node.text for node in execution.findall("goals/goal")}
                if goals & {"jar", "war"}:
                    configurations.extend(execution.findall("configuration"))
            if any(text_at(c, "classifier") for c in configurations):
                gaps.append("main_classifier_requires_review:" + aid)
        if aid not in {
            "maven-jar-plugin",
            "maven-war-plugin",
            "maven-bundle-plugin",
            "maven-source-plugin",
            "maven-javadoc-plugin",
            "maven-shade-plugin",
            "maven-assembly-plugin",
            "maven-install-plugin",
            "maven-deploy-plugin",
        }:
            if any(
                text_at(e, "phase") in {"prepare-package", "package", "install"}
                for e in plugin.findall("executions/execution")
            ):
                gaps.append("custom_artifact_binding_requires_review:" + aid)
    if reaches_package and packaging != "pom":
        if not literal(final_name) or not literal(build_directory):
            gaps.append("artifact_output_path_unresolved")
        elif packaging in {"jar", "bundle", "war"}:
            extension = "war" if packaging == "war" else "jar"
            if PurePosixPath(build_directory).is_absolute():
                gaps.append("absolute_metadata_workspace_path_requires_mapping")
            artifacts.append(
                {
                    "role": "main",
                    "module": module,
                    "path": str(PurePosixPath(build_directory) / (final_name + "." + extension)),
                    "extension": extension,
                    "classifier": None,
                    "coordinates": coords,
                    "evidence_tier": "declared",
                }
            )
    installs = []
    if reaches_install:
        if not literal(local_repository):
            gaps.append("local_repository_unresolved")
        elif all(literal(value) for value in coords.values()):
            prefix = PurePosixPath(local_repository) / group.replace(".", "/") / artifact / version
            for extension in ["pom"] + (
                ["war" if packaging == "war" else "jar"]
                if packaging in {"jar", "war", "bundle"}
                else []
            ):
                installs.append(
                    {
                        "role": "installed_pom" if extension == "pom" else "installed_main",
                        "module": module,
                        "metadata_path": str(prefix / f"{artifact}-{version}.{extension}"),
                        "repository_relative_path": str(
                            PurePosixPath(group.replace(".", "/"))
                            / artifact
                            / version
                            / f"{artifact}-{version}.{extension}"
                        ),
                        "extension": extension,
                        "classifier": None,
                        "coordinates": coords,
                        "evidence_tier": "declared",
                    }
                )
    return {
        "module": module,
        "packaging": packaging,
        "coordinates": coords,
        "final_name": final_name,
        "build_directory": build_directory,
        "classes_directory": classes_directory,
        "local_repository": local_repository,
        "artifacts": artifacts,
        "installs": installs,
        "evidence_tier": "declared",
        "resolution_status": "unresolved" if gaps else "declared",
        "exceptions": sorted(set(gaps)),
        "provenance": provenance,
    }


def archive_source(
    path: Path, output: Path, *, basis: str, official_url: str | None = None
) -> dict:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    relative = (
        path.resolve().relative_to(output.resolve())
        if path.resolve().is_relative_to((output / "sources").resolve())
        else Path("sources") / (sha[:16] + "-" + path.name)
    )
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    elif hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        raise ValueError("Archived source path contains different bytes")
    return {
        "path": relative.as_posix(),
        "sha256": sha,
        "bytes": len(data),
        "basis": basis,
        "source_archive": str(path),
        "official_url": official_url,
    }


def find_source_pom(project: dict, bundle: Path, repo: Path) -> Path | None:
    for item in project["ci"]["evidence"]:
        if item["path"].endswith("source-pom.xml"):
            candidate = bundle / item["path"]
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != item.get("sha256"):
                raise ValueError("frozen source POM bytes changed: " + project["id"])
            return candidate
    candidate = (
        repo
        / "output/java-benchmark-20260916/raw/configs"
        / project["repo"]
        / project["commit"]
        / "pom.xml"
    )
    metadata = candidate.with_name("pom.xml.meta.json")
    if candidate.exists() and metadata.exists():
        declared = json.loads(metadata.read_text())
        expected_url = (
            f"https://raw.githubusercontent.com/{project['repo']}/{project['commit']}/pom.xml"
        )
        if (
            declared.get("url") != expected_url
            or declared.get("sha256") != hashlib.sha256(candidate.read_bytes()).hexdigest()
        ):
            raise ValueError("pinned source POM provenance mismatch: " + project["id"])
        return candidate
    if project["id"] == "commons-cli":
        archive = repo / "logs/ci-scope-small-projects-20260907/ci-targets/commons-cli"
        target_path = archive / "target_record.json"
        if target_path.exists():
            target = json.loads(target_path.read_text())
            if target.get("repo") == project["repo"] and target.get("sha") == project["commit"]:
                return archive / "sources/pom.xml"
    # A directory called source-at-ci-sha alone does not prove which SHA its
    # bytes belong to. Do not borrow a nearby historical checkout.
    return None


def observed_checks(raw: str) -> list[dict]:
    """Positive goal observations only; absence never removes a requirement."""
    result = []
    for line_number, line in enumerate(ANSI.sub("", raw).splitlines(), 1):
        match = GOAL_BANNER.search(line)
        if not match:
            continue
        plugin, version, goal, execution, module = match.groups()
        normalized = normalize_goal(plugin + ":" + goal)
        if (
            normalized in CHECK_GOALS
            or normalized.startswith("javadoc:")
            or normalized in {"failsafe:integration-test", "failsafe:verify"}
        ):
            result.append(
                {
                    "goal": normalized,
                    "plugin_version": version,
                    "execution_id": execution,
                    "module_display_label": module,
                    "line": line_number,
                }
            )
    return result


def build_project(project: dict, bundle: Path, output: Path, repo: Path) -> dict:
    task_path = bundle / project["task"]["path"]
    raw_task = task_path.read_bytes()
    if hashlib.sha256(raw_task).hexdigest() != project["task"]["sha256"]:
        raise ValueError("frozen task bytes changed: " + project["id"])
    task = json.loads(raw_task)
    if task["repo"] != project["repo"] or task["sha"] != project["commit"]:
        raise ValueError("task and manifest identities differ")
    destination = output / project["task"]["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw_task)
    source = archive_source(task_path, output, basis="frozen_task_argv")
    ci_index = bundle / "ci" / project["id"] / "index.json"
    ci_index_ref = archive_source(ci_index, output, basis="frozen_ci_index") if ci_index.is_file() else None
    pom_path = find_source_pom(project, bundle, repo)
    pom_ref, pom_root, defaults = None, None, []
    if pom_path and pom_path.exists():
        pom_ref = archive_source(
            pom_path,
            output,
            basis="pinned_source_pom_not_effective",
            official_url=f"https://raw.githubusercontent.com/{project['repo']}/{project['commit']}/pom.xml",
        )
        pom_root = xml_root(pom_path.read_bytes())
        defaults = shlex.split(text_at(pom_root, "build/defaultGoal") or "")
    requirements, step_metadata, gaps = [], [], []

    def add(
        step: dict,
        kind: str,
        subtype: str,
        *,
        rule: str = "native_goal",
        goals: list[str] | None = None,
        sources: list[dict] | None = None,
        expectations: dict | None = None,
    ) -> dict:
        key = step["id"] + "-" + re.sub(r"[^a-zA-Z0-9_-]+", "-", kind + "-" + subtype)
        existing = next((r for r in requirements if r["id"] == key), None)
        if existing:
            for item in sources or []:
                if item not in existing["sources"]:
                    existing["sources"].append(item)
            return existing
        item = {
            "id": key,
            "step_id": step["id"],
            "kind": kind,
            "subtype": subtype,
            "module": None,
            "depends_on": [],
            "dependencies_complete": False,
            "scope": {
                "status": "unresolved",
                "reason": "effective_execution_plan_and_module_paths_not_frozen",
            },
            "validation": {"rule": rule, "goals": goals or []},
            "expectations": expectations or {},
            "sources": sources or [source],
        }
        requirements.append(item)
        return item

    for step in task["steps"]:
        legacy_step = next(s for s in project["steps"] if s["id"] == step["id"])
        argv = step["argv"]
        goals = maven_goals(argv) if step["runner"] == "maven" else []
        use_default = step["runner"] == "maven" and not goals
        if use_default:
            goals = defaults[:]
            if not goals:
                gaps.append(step["id"] + ":default_goal_unresolved")
        endpoint = lifecycle_endpoint(goals)
        sources = [source] + ([pom_ref] if use_default and pom_ref else [])
        step_metadata.append(
            {
                "step_id": step["id"],
                "argv": argv,
                "runner": step["runner"],
                "environment": legacy_step.get("environment", {}),
                "effective_goals": goals,
                "uses_default_goal": use_default,
                "lifecycle_endpoint": endpoint,
                "source_default_goal": defaults,
                "execution_plan_resolved": False,
                "effective_pom_status": "unavailable",
            }
        )
        if step["runner"] == "maven":
            level = PHASES.index(endpoint) if endpoint else -1
            if level >= PHASES.index("compile"):
                add(step, "compile", "production", goals=["compiler:compile"], sources=sources)
            if level >= PHASES.index("test") and not skip_tests(argv):
                add(step, "test", "unit", rule="junit", goals=["surefire:test"], sources=sources)
            if level >= PHASES.index("package"):
                add(
                    step,
                    "package",
                    "primary_artifacts",
                    rule="artifact",
                    sources=sources,
                    expectations={
                        "artifacts": [],
                        "status": "unresolved",
                        "reason": "effective_reactor_poms_required",
                    },
                )
            if level >= PHASES.index("install"):
                add(
                    step,
                    "install",
                    "local_repository",
                    rule="install",
                    goals=["install:install"],
                    sources=sources,
                    expectations={
                        "artifacts": [],
                        "local_repository": None,
                        "status": "unresolved",
                    },
                )
            for goal in goals:
                normalized = normalize_goal(goal)
                if normalized in CHECK_GOALS:
                    requirement = add(
                        step, "quality_check", normalized, goals=[normalized], sources=sources
                    )
                    requirement["expectations"]["check_type"] = CHECK_GOALS[normalized]
                elif normalized.startswith("javadoc:"):
                    add(step, "documentation", normalized, goals=[normalized], sources=sources)
                elif goal not in PHASES and goal != "clean":
                    add(
                        step,
                        "unclassified",
                        normalized,
                        rule="unclassified",
                        goals=[normalized],
                        sources=sources,
                    )
            if "-PintegrationTesting" in argv:
                add(
                    step,
                    "test",
                    "integration",
                    rule="junit",
                    sources=sources,
                    expectations={
                        "status": "unresolved",
                        "basis": "explicit_integrationTesting_profile",
                    },
                )
        elif step["runner"] == "gradle":
            if any(goal.endswith(":nativeCompile") for goal in argv):
                add(
                    step,
                    "native_compile",
                    "graalvm",
                    rule="artifact",
                    goals=[argv[-1]],
                    sources=sources,
                    expectations={
                        "artifacts": [
                            {"role": "native_executable", "path": p, "evidence_tier": "declared"}
                            for p in legacy_step.get("expected_artifacts", [])
                        ]
                    },
                )
            elif "build" in argv:
                add(step, "compile", "production", goals=["compileJava"])
                add(
                    step,
                    "package",
                    "jvm_artifacts",
                    rule="artifact",
                    goals=["assemble"],
                    expectations={"status": "unresolved"},
                )
                add(step, "test", "jvm", rule="junit", goals=["test"])
        elif step["runner"] == "native":
            add(
                step,
                "test",
                "native",
                rule="native_exit",
                sources=sources,
                expectations={"executable": argv[0], "requires_binary_lineage": True},
            )
        else:
            add(step, "unclassified", "opaque_command", rule="unclassified")

        # Multi-step Curator has individually captured invocation logs. Never
        # attach every goal from a whole CI job to each of its different steps.
        for ref in project["ci"]["evidence"]:
            if not ref["path"].endswith(".log") or step["runner"] != "maven":
                continue
            if len(task["steps"]) > 1:
                suffix = (
                    "official-install-invocation.log"
                    if endpoint == "install"
                    else "official-verify-invocation.log"
                )
                if not ref["path"].endswith(suffix):
                    continue
            log_path = bundle / ref["path"]
            if hashlib.sha256(log_path.read_bytes()).hexdigest() != ref["sha256"]:
                raise ValueError("frozen CI evidence changed: " + ref["path"])
            facts = observed_checks(log_path.read_text(errors="replace"))
            if not facts:
                continue
            log_ref = archive_source(
                log_path,
                output,
                basis="official_ci_positive_goal_observation",
                official_url=ref.get("official_url"),
            )
            for fact in facts:
                goal = fact["goal"]
                if goal.startswith("failsafe:"):
                    if skip_tests(argv):
                        continue
                    kind, subtype, rule = "test", "integration", "junit"
                elif goal.startswith("javadoc:"):
                    kind, subtype, rule = "documentation", goal, "native_goal"
                else:
                    kind, subtype, rule = "quality_check", goal, "native_goal"
                entry = add(
                    step,
                    kind,
                    subtype,
                    rule=rule,
                    goals=[goal],
                    sources=[{**log_ref, "line": fact["line"]}],
                )
                entry.setdefault("observed_ci_bindings", []).append(fact)
        add(
            step,
            "unclassified",
            "unresolved_execution_plan",
            rule="unclassified",
            sources=sources,
            expectations={
                "status": "unresolved",
                "reason": "all_inherited_profile_and_custom_bindings_need_review",
            },
        )
        gaps.append(step["id"] + ":effective_execution_plan_and_artifact_scope_unresolved")

    # Keep known prerequisite order only at the acceptance-step boundary. The
    # unresolved module/fork bindings deliberately cannot authorize fail-fast inference.
    for requirement in requirements:
        if requirement["kind"] == "test" and requirement["subtype"] == "native":
            requirement["depends_on"] = [
                r["id"] for r in requirements if r["kind"] == "native_compile"
            ]
    labels = sorted({r["kind"] for r in requirements if r["kind"] != "unclassified"})
    return {
        "schema_version": 2,
        "policy_version": POLICY_VERSION,
        "project_id": project["id"],
        "repository": project["repo"],
        "commit": project["commit"],
        "task_sha256": task_digest(task),
        "task_file_sha256": project["task"]["sha256"],
        "annotation_completeness": {
            "status": "review_required",
            "gaps": sorted(set(gaps)),
            "labels_are_known_lower_bound": True,
        },
        "preconditions": [
            {
                "id": "worktree_integrity",
                "kind": "worktree_integrity",
                "policy": {
                    "tracked_changes": "forbidden",
                    "untracked_files": "record_and_attribute_without_automatic_exclusion",
                    "snapshots": [
                        "task_start",
                        "acceptance_before",
                        "acceptance_after",
                        "evidence_close",
                    ],
                },
            },
            {
                "id": "runtime_conformance",
                "kind": "runtime_conformance",
                "steps": [
                    {
                        "step_id": s["id"],
                        "java_major": s.get("java_major"),
                        "maven_version": s.get("maven_version"),
                        "requires_native_image": next(
                            x for x in project["steps"] if x["id"] == s["id"]
                        ).get("requires_native_image", False),
                    }
                    for s in task["steps"]
                ],
            },
        ],
        "ci_alignment": {
            "selected_url": project["ci"]["selected_url"],
            "selected_cell": project["ci"]["selected_cell"],
            "reference_status": project["ci"]["evidence_status"],
            "scope_note": project["scope_note"],
            "comparison_admitted": project["ci"]["comparison_admitted"],
            "source_module_labels": project["ci"]["modules"],
            "module_paths_resolved": False,
            **({"archived_ci_index": ci_index_ref} if ci_index_ref else {}),
        },
        "group_labels": labels,
        "steps": step_metadata,
        "requirements": requirements,
        "migration": {
            "legacy_stages_authoritative": False,
            "legacy_stages": {s["id"]: s.get("stages", []) for s in project["steps"]},
            "new_categories_not_expressible_by_legacy_stages": [
                value
                for value in labels
                if value not in {"compile", "package", "install", "test", "native_compile"}
            ],
        },
    }


def plugin_prefix(plugin: ET.Element, descriptors: dict | None = None) -> str:
    coordinates = (text_at(plugin, "groupId") or "org.apache.maven.plugins",
                   text_at(plugin, "artifactId"), text_at(plugin, "version"))
    if coordinates in (descriptors or {}):
        return descriptors[coordinates]
    return re.sub(r"(?:-maven-plugin|-plugin)$", "", (coordinates[1] or "").removeprefix("maven-"))


def reviewed_plugin_descriptors(review: dict, base: Path, models: dict) -> tuple[dict, dict]:
    """Resolve non-conventional prefixes from the actual, byte-bound plugin JAR.

    A reviewer cannot rename a goal by assertion. The descriptor coordinates
    must occur in a captured effective model and its bytes remain in the bundle.
    """
    from sag.benchmark.requirements import bound_file

    declared = {(text_at(p, "groupId") or "org.apache.maven.plugins", text_at(p, "artifactId"), text_at(p, "version"))
                for model in models.values()
                for path in ("build/plugins/plugin", "build/pluginManagement/plugins/plugin")
                for p in model.findall(path)}
    prefixes, sources = {}, {}
    for index, ref in enumerate(review.get("plugin_descriptors", [])):
        path = bound_file(base, ref)
        try:
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.infolist() if m.filename == "META-INF/maven/plugin.xml"]
                if len(members) != 1 or members[0].file_size > 4 * 1024 * 1024:
                    raise ValueError("Plugin JAR needs one bounded descriptor")
                root = xml_root(archive.read(members[0]))
        except (zipfile.BadZipFile, ET.ParseError) as exc:
            raise ValueError("Invalid plugin descriptor archive") from exc
        coordinates = tuple(text_at(root, key) for key in ("groupId", "artifactId", "version"))
        prefix = text_at(root, "goalPrefix")
        if (root.tag != "plugin" or coordinates not in declared or coordinates in prefixes
                or not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", prefix)):
            raise ValueError("Plugin descriptor does not uniquely match a captured plugin")
        prefixes[coordinates] = prefix
        sources["plugin_descriptor_" + str(index)] = path
    return prefixes, sources


def reviewed_install_poms(review, base, modules, bindings, rows, output):
    """Bind generated consumer POMs to a producer and the installed coordinate.

    The repository POM is the durable output. A reviewed temporary source is
    not also a package artifact that must survive the Maven process's exit.
    """
    from sag.benchmark.requirements import bound_file

    paths = {m["id"]: str(PurePosixPath(m["path"]) / "pom.xml") for m in modules}
    seen, archived = set(), {}
    for entry in review.get("generated_install_poms", []):
        module, relative = entry.get("module"), entry.get("path")
        if (module not in paths or module in seen or not isinstance(relative, str)
                or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
                or str(PurePosixPath(relative)) != relative or relative == paths[module]
                or not entry.get("review_basis") or not entry.get("producer_sources")):
            raise ValueError("Generated installed POM needs a unique relative source and producer review")
        parent = str(PurePosixPath(paths[module]).parent)
        if parent != "." and not relative.startswith(parent + "/"):
            raise ValueError("Generated POM source lies outside its declared module")
        fields = ("goal", "execution", "occurrence", "position", "version", "module")
        producers = [b for b in bindings if all(b.get(k) == entry.get("producer", {}).get(k) for k in fields)]
        owners = [row for row in rows if row["kind"] == "install" and row.get("module") == module
                  and any(a.get("role") == "installed_pom" for a in row.get("expectations", {}).get("artifacts", []))]
        if len(producers) != 1 or len(owners) != 1 or producers[0]["module"] != module:
            raise ValueError("Generated POM needs one observed producer and one installed POM obligation")
        validation = owners[0]["validation"]
        if producers[0]["position"] >= validation["position"]:
            raise ValueError("Consumer POM producer must precede its installation")
        existing = validation.get("native_bindings") or [
            {"goal": validation["goals"][0], **{k: validation[k] for k in ("execution", "occurrence", "position")}}]
        producer = {k: producers[0][k] for k in ("goal", "execution", "occurrence", "position")}
        validation["native_bindings"] = [producer, *existing]
        validation["goals"] = list(dict.fromkeys(b["goal"] for b in validation["native_bindings"]))
        validation["position"] = producer["position"]
        for index, ref in enumerate(entry["producer_sources"]):
            archived[f"generated_pom_{module}_{index}"] = archive_source(
                bound_file(base, ref), output, basis="reviewed_generated_consumer_pom_producer")
        paths[module] = relative
        seen.add(module)
    return paths, archived


def pom_execution_bindings(root: ET.Element, descriptors: dict | None = None) -> list[dict]:
    """Inventory declarations for review; this is not a Maven lifecycle resolver."""
    rows = []
    for plugin in root.findall("build/plugins/plugin"):
        artifact = text_at(plugin, "artifactId") or ""
        prefix = plugin_prefix(plugin, descriptors)
        for execution in plugin.findall("executions/execution"):
            for goal in execution.findall("goals/goal"):
                rows.append({
                    "plugin": artifact, "version": text_at(plugin, "version"),
                    "goal": prefix + ":" + (goal.text or "").strip(),
                    "execution": text_at(execution, "id") or "default",
                    "phase": text_at(execution, "phase"),
                })
    return rows


def ci_execution_bindings(raw: str) -> list[dict]:
    return ci_binding_inventory(raw)[0]


def reviewed_preparation(status, sources, task, step, workspace):
    """Check captured commands rather than trusting a preparation status label."""
    from scripts.requirements_metadata_inventory import _metadata_options
    if (status.get("repo") != task["repo"] or status.get("commit") != task["sha"]
            or status.get("metadata_only") is not True
            or status.get("preparation_script", {}).get("sha256") != sources["preparation_script"]["sha256"]):
        raise ValueError("Reviewed metadata preparation identity differs")
    matching = [s for s in status.get("steps", []) if s.get("id") == step["id"]]
    if len(matching) != 1:
        raise ValueError("Reviewed metadata preparation step is missing or ambiguous")
    recorded = matching[0]
    launcher = recorded.get("launcher")
    probe = recorded.get("runtime_probe", {})
    if (recorded.get("frozen_argv") != step["argv"] or not launcher
            or probe.get("argv") != [launcher, "--version"] or probe.get("cwd") != workspace
            or probe.get("status") != "completed" or probe.get("exit_code") != 0
            or probe.get("log", {}).get("sha256") != sources["runtime"]["sha256"]):
        raise ValueError("Reviewed metadata runtime probe differs from the frozen command")
    options = _metadata_options(step["argv"])
    commands = recorded.get("commands", [])
    if recorded.get("metadata_options") != options or len(commands) != 2:
        raise ValueError("Reviewed metadata options differ from the frozen command")
    for command, goal, output_key, log_key in zip(
            commands, ("effective-pom", "effective-settings"),
            ("effective_pom", "effective_settings"), ("pom_log", "settings_log")):
        output = recorded.get("outputs", {}).get(goal, {})
        argv = command.get("argv", [])
        expected = [launcher, *options, "org.apache.maven.plugins:maven-help-plugin:3.5.2:" + goal, "-Dverbose"]
        if (argv[:-1] != expected or not argv[-1].startswith("-Doutput=/")
                or not argv[-1].endswith("/" + output.get("path", ""))
                or output.get("sha256") != sources[output_key]["sha256"]
                or command.get("cwd") != workspace or command.get("exit_code") != 0
                or command.get("status") != "completed"
                or command.get("log", {}).get("sha256") != sources[log_key]["sha256"]):
            raise ValueError("Reviewed metadata command or output differs from its recorded source")


def reviewed_disabled_binding(binding, segment, model, effective_pom_sha256):
    witness = binding.get("disabled_witness", {})
    if witness.get("type") == "effective_pom_configuration":
        # A reviewed skip is configuration, never proof that a check passed.
        if (binding["goal"] != "japicmp:cmp" or witness.get("plugin") != "japicmp-maven-plugin"
                or witness.get("path") != "configuration/skip" or witness.get("value") != "true"
                or witness.get("effective_pom_sha256") != effective_pom_sha256
                or any(witness.get(k) != binding[k] for k in ("module", "goal", "execution"))):
            raise ValueError("CI-disabled effective POM witness is not an explicit matching skip")
        plugins = [p for p in model.findall("build/plugins/plugin")
                   if text_at(p, "artifactId") == witness["plugin"]]
        if len(plugins) != 1:
            raise ValueError("CI-disabled plugin configuration is ambiguous")
        executions = [e for e in plugins[0].findall("executions/execution")
                      if (text_at(e, "id") or "default") == binding["execution"]]
        skip = text_at(executions[0], "configuration/skip") if len(executions) == 1 else None
        if (skip if skip is not None else text_at(plugins[0], "configuration/skip")) != "true":
            raise ValueError("CI-disabled effective POM witness does not disable this execution")
    else:
        if witness and (witness.get("type") != "native_log" or not isinstance(witness.get("text"), str)
                        or not witness["text"] or witness["text"] not in segment):
            raise ValueError("CI-disabled binding needs its explicit native skip witness")
        inapplicable = {
            "compiler:compile": r"^\[INFO\]\s+No sources to compile\s*$",
            "compiler:testCompile": r"^\[INFO\]\s+No sources to compile\s*$",
            "surefire:test": r"^\[INFO\]\s+(?:No tests to run|Tests are skipped)\.\s*$",
            "artifact:buildinfo": r"^\[INFO\]\s+Auto-skipping goal because module skips install and/or deploy\s*$",
            "javadoc:jar": r"^\[INFO\]\s+Not executing Javadoc as the project is not a Java classpath-capable package\s*$",
            "site:attach-descriptor": r"^\[INFO\]\s+No site descriptor found: nothing to attach\.\s*$",
            "changes:changes-validate": r"^\[WARNING\]\s+changes\.xml file /[^\r\n]+/src/changes/changes\.xml does not exist\.\s*$",
        }
        if not (re.search(r"^\[INFO\]\s+Skipping\b", segment, re.M)
                or witness and binding["goal"] in inapplicable
                and re.search(inapplicable[binding["goal"]], segment, re.M)):
            raise ValueError("CI-disabled binding needs its explicit native skip witness")


def import_launcher_review(review, source_base, task, step, output):
    """Archive a reviewed wrapper's pinned input closure, without executing it."""
    from sag.benchmark.requirements import bound_file, load_json
    from sag.benchmark.wrapper_review import make_launcher_review, validate_launcher_review
    additional = review.get("additional_sources", {})
    index_path = bound_file(source_base, additional.get("launcher_inventory", {}))
    index = load_json(index_path)
    if index.get("commit") != task["sha"]:
        raise ValueError("Wrapper input inventory has a different commit")
    expected = [row for row in index.get("files", [])
                if row.get("source_path") == "mvnw" or row.get("source_path", "").startswith(".mvn/")]
    refs = {name.removeprefix("launcher_source:"): ref for name, ref in additional.items()
            if name.startswith("launcher_source:")}
    if set(refs) != {row["source_path"] for row in expected} or len(refs) != len(expected):
        raise ValueError("Wrapper source review omits or duplicates captured inputs")
    raw, archived = {}, {"launcher_inventory": archive_source(index_path, output, basis="reviewed_launcher_inventory")}
    for row in expected:
        path = row["source_path"]
        if (row.get("kind") != "file" or row.get("pinned_bytes_equal") is not True
                or any(refs[path].get(k) != row.get(k) for k in ("sha256", "bytes"))):
            raise ValueError("Wrapper review differs from pinned input bytes")
        source = bound_file(source_base, refs[path])
        raw[path] = source.read_bytes()
        archived["launcher_source:" + path] = archive_source(source, output, basis="reviewed_launcher_source")
    launcher = make_launcher_review(task["sha"], raw)
    # Earlier source reviews deliberately treat every .mvn file as immutable
    # input. Preserve that stricter policy; never grant a new state exemption
    # merely by re-importing an old review with newer code.
    if "runtime_state" not in review["launcher_review"]:
        launcher.pop("runtime_state", None)
    validate_launcher_review(launcher, task["sha"], step)
    if launcher != review["launcher_review"] or step.get("maven_version") not in (None, launcher["maven_version"]):
        raise ValueError("Launcher review differs from its source or frozen runtime")
    for state in launcher.get("runtime_state", []):
        for name, expected_ref in state.get("reviewed_sources", {}).items():
            ref = additional.get(name, {})
            if any(ref.get(k) != expected_ref.get(k) for k in ("sha256", "bytes")):
                raise ValueError("Launcher runtime-state interpretation lacks its reviewed producer source")
            archived[name] = archive_source(bound_file(source_base, ref), output, basis="reviewed_launcher_runtime_state")
    for name, ref in additional.items():
        if name.startswith("launcher_state_") and name not in archived:
            archived[name] = archive_source(bound_file(source_base, ref), output, basis="reviewed_launcher_runtime_state_analysis")
    return launcher, archived


def import_jvm_config_review(review, source_base, task, output):
    from sag.benchmark.requirements import bound_file, load_json
    from sag.benchmark.jvm_inputs import literal_tokens, validate_review

    declaration = review["jvm_config_review"]
    validate_review(declaration, task["sha"])
    additional = review.get("additional_sources", {})
    index_path = bound_file(source_base, additional.get("launcher_inventory", {}))
    index = load_json(index_path)
    ref = additional.get("launcher_source:.mvn/jvm.config", {})
    raw_path = bound_file(source_base, ref)
    entries = [row for row in index.get("files", []) if row.get("source_path") == declaration["source_path"]]
    if (index.get("commit") != task["sha"] or len(entries) != 1
            or entries[0].get("kind") != "file" or entries[0].get("pinned_bytes_equal") is not True
            or any(ref.get(k) != entries[0].get(k) or ref.get(k) != declaration.get(k) for k in ("sha256", "bytes"))
            or literal_tokens(raw_path.read_bytes()) != declaration["tokens"]):
        raise ValueError("JVM review differs from its pinned source inventory")
    return declaration, {
        "jvm_config_inventory": archive_source(index_path, output, basis="reviewed_jvm_input_inventory"),
        "jvm_config_source": archive_source(raw_path, output, basis="reviewed_pinned_jvm_config"),
    }


def _reviewed_gha_command(review, source_base, spec, task, allowed_ci):
    """Select one whole runner command from independently frozen job bytes.

    The slice cannot choose a favorable Maven terminal or remove an earlier
    failure: it starts at the exact command heading and ends at the next runner
    command heading (or EOF). Environment echoes and every native line remain.
    """
    from sag.benchmark.requirements import bound_file

    source = review["ci_source"]
    raw_ref = source.get("raw_job", {})
    raw_path = bound_file(source_base, raw_ref)
    if raw_ref.get("sha256") not in allowed_ci or raw_path.stat().st_size != raw_ref.get("bytes"):
        raise ValueError("Selected job is not an independently frozen CI source")
    raw = raw_path.read_bytes().decode("utf-8-sig")
    lines = raw.splitlines(keepends=True)
    start, end = source.get("start_line"), source.get("end_line_exclusive")
    if (type(start) is not int or type(end) is not int
            or not 1 <= start < end <= len(lines) + 1):
        raise ValueError("Invalid whole-command CI line range")
    heading = ci_native_text(lines[start - 1]).strip()
    prefix = "##[group]Run "
    if not heading.startswith(prefix) or shlex.split(heading[len(prefix):]) != task["steps"][0]["argv"]:
        raise ValueError("Selected CI heading differs from the frozen command")
    next_heading = next((i + 1 for i in range(start, len(lines))
                         if ci_native_text(lines[i]).startswith(prefix)), len(lines) + 1)
    if end != next_heading:
        raise ValueError("Selected CI slice must retain the complete runner command")
    native_path = bound_file(source_base, review["sources"]["official_ci"])
    if native_path.read_bytes() != "".join(lines[start - 1:end - 1]).encode("utf-8"):
        raise ValueError("Selected CI command bytes differ from the frozen job slice")
    if review.get("declared_variant"):
        raise ValueError("GHA command slicing cannot change the frozen command")
    return {"source_paths": {"ci_original_job": raw_path}, "variant": None,
            "lineage": {"transport": "github-actions-command-v1",
                        "selected_url": spec["ci_alignment"]["selected_url"],
                        "raw_job_sha256": raw_ref["sha256"], "start_line": start,
                        "end_line_exclusive": end,
                        "native_text_sha256": review["sources"]["official_ci"]["sha256"]}}


def _reviewed_idle_pom(module, model, ci_text, selected_goals):
    """A POM reactor member can legitimately have no work before install.

    This is an explicit review disposition with a native reactor-success
    witness, not permission to omit a model, skip a child or ignore a goal.
    """
    declaration = module.get("no_ci_bindings")
    if (not isinstance(declaration, dict)
            or declaration.get("disposition") != "pom_without_goal_execution"
            or not declaration.get("reason")
            or (text_at(model, "packaging") or "jar") != "pom"
            or not selected_goals
            or any(goal not in PHASES or PHASES.index(goal) >= PHASES.index("install")
                   for goal in selected_goals)):
        raise ValueError("Module without CI goals needs a reviewed pre-install POM disposition")
    name = text_at(model, "name") or text_at(model, "artifactId")
    version = text_at(model, "version")
    witness = declaration.get("reactor_summary_witness")
    # Maven prints either the project name or the name followed by its version.
    pattern = (r"\[INFO\]\s+" + re.escape(name) + r"(?:\s+" + re.escape(version)
               + r")?\s+\.+\s+SUCCESS\s+\[[^\]\r\n]+\]\s*")
    lines = ci_text.splitlines()
    summary = [i for i, line in enumerate(lines) if re.fullmatch(r"\[INFO\]\s+Reactor Summary:.*", line)]
    if (not isinstance(witness, str) or not re.fullmatch(pattern, witness)
            or len(summary) != 1 or lines[summary[0] + 1:].count(witness) != 1):
        raise ValueError("Idle POM needs its exact successful reactor summary witness")


def _apply_multi_step_review(spec, task, review, review_path, output):
    """Compose fully reviewed commands without weakening any individual review."""
    from copy import deepcopy
    from sag.benchmark.requirements import bound_file, normalize_task, validate_requirements

    task = normalize_task(task)
    if (review.get("schema_version") != 1 or review.get("task_sha256") != task_digest(task)
            or review.get("project_id") != spec["project_id"] or review.get("commit") != task["sha"]
            or not review.get("reviewed_by") or not review.get("review_notes")
            or review.get("unresolved_obligations")):
        raise ValueError("Multi-step review identity or completeness differs from frozen task")
    entries = review.get("step_reviews", [])
    if (len(task["steps"]) < 2 or any(s["runner"] != "maven" for s in task["steps"])
            or not isinstance(entries, list)
            or [e.get("step_id") for e in entries] != [s["id"] for s in task["steps"]]):
        raise ValueError("Multi-step review must cover every Maven command in frozen order")
    validate_requirements(spec, task)
    outputs = []
    for step, entry in zip(task["steps"], entries):
        path = bound_file(review_path.parent, entry.get("review", {}))
        if path.stat().st_size != entry["review"].get("bytes"):
            raise ValueError("Step review byte length mismatch")
        child_task = {**task, "steps": [step]}
        child = deepcopy(spec)
        child["task_sha256"] = task_digest(child_task)
        child.pop("task_file_sha256", None)  # This is an internal view, not a new task file.
        child["requirements"] = [r for r in child["requirements"] if r["step_id"] == step["id"]]
        child["steps"] = [s for s in child.get("steps", []) if s["step_id"] == step["id"]]
        if len(child["steps"]) != 1:
            raise ValueError("Frozen step metadata is missing or ambiguous")
        for condition in child["preconditions"]:
            if condition["id"] == "runtime_conformance":
                condition["steps"] = [s for s in condition.get("steps", []) if s["step_id"] == step["id"]]
        outputs.append(apply_reviewed_plan(child, child_task, path, output, _parent_task=task))
    rows = [row for child in outputs for row in child["requirements"]]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Step requirements need globally unique IDs; no cross-step pool merging")
    result = deepcopy(spec)
    result["requirements"] = rows
    result["steps"] = [s for child in outputs for s in child["steps"]]
    review_ref = archive_source(review_path, output, basis="explicit_reviewed_multi_step_plan")
    result["annotation_completeness"] = {"status": "complete", "gaps": [],
        "labels_are_known_lower_bound": False, "basis": "explicit_source_bound_multi_step_review", "review": review_ref}
    result["group_labels"] = sorted({r["kind"] for r in rows})
    result["ci_alignment"]["module_paths_resolved"] = True
    result["reviewed_execution_plan"] = {"review": review_ref, "notes": review["review_notes"],
        "steps": [{"step_id": step["id"], **child["reviewed_execution_plan"]}
                  for step, child in zip(task["steps"], outputs)],
        "dependency_policy": "Within-step reviewed dependencies only; original task order remains mandatory"}
    if review.get("compilation_reuse"):
        result["compilation_reuse"] = deepcopy(review["compilation_reuse"])
    return validate_requirements(result, task)


def apply_reviewed_plan(spec: dict, task: dict, review_path: Path, output: Path,
                        *, _parent_task: dict | None = None) -> dict:
    """Import a reviewed, byte-bound Maven plan without running Maven.

    This narrow path deliberately requires an explicit disposition for every
    effective-POM execution and every CI goal occurrence. A green CI log alone
    never supplies completeness. Reactor module paths need their pinned POMs.
    """
    from copy import deepcopy
    from sag.benchmark.requirements import bound_file, load_json, validate_requirements, repository_artifact_path

    review_path = review_path.resolve()
    review = load_json(review_path)
    if review.get("review_protocol") == "multi-step-maven-v1":
        if _parent_task is not None:
            raise ValueError("Nested multi-step reviews are not supported")
        return _apply_multi_step_review(spec, task, review, review_path, output)
    if _parent_task is not None:
        from sag.benchmark.requirements import normalize_task
        _parent_task = normalize_task(_parent_task)
        if (len(task["steps"]) != 1
                or [s for s in _parent_task["steps"] if s["id"] == review.get("step_id")] != task["steps"]
                or any(task[k] != _parent_task[k] for k in ("repo", "sha", "schema_version"))):
            raise ValueError("Step review does not match its original frozen parent task")
    multi = review.get("review_protocol") == "multi-module-maven-v1"
    if (review.get("schema_version") != 1 or review.get("review_protocol") not in {"single-module-maven-v1", "multi-module-maven-v1"}
            or review.get("task_sha256") != task_digest(_parent_task or task)
            or review.get("project_id") != spec["project_id"]
            or review.get("commit") != task["sha"]):
        raise ValueError("Reviewed plan identity differs from frozen task")
    if not review.get("reviewed_by") or not review.get("review_notes"):
        raise ValueError("A reviewed plan must identify its review and rationale")
    if review.get("unresolved_obligations"):
        raise ValueError("Reviewed plan retains unresolved obligations")
    if len(task["steps"]) != 1 or task["steps"][0]["runner"] != "maven":
        raise ValueError("This importer resolves one Maven command only")
    step = task["steps"][0]
    if step.get("cwd", ".") != "." or not multi and review.get("module", {}).get("path") != ".":
        raise ValueError("This importer resolves a command from the checkout root only")
    names = {"effective_pom", "effective_settings", "pom_log", "settings_log", "runtime", "head",
             "tracked_diff", "untracked", "preparation_request", "preparation_script", "official_ci"}
    if multi:
        names.update({"preparation_status", "source_pom_index"})
    sources = review.get("sources", {})
    if set(sources) != names:
        raise ValueError("The reviewed metadata preparation source set is incomplete")
    source_base = review_path.parent
    if review.get("source_paths_relative_to") == "requirements_bundle_root" and source_base.name == "sources":
        source_base = source_base.parent
    files = {name: bound_file(source_base, ref) for name, ref in sources.items()}
    # The source CI is independently anchored in the original frozen manifest.
    allowed_ci = {source.get("sha256") for row in spec["requirements"] for source in row.get("sources", [])
                  if source.get("basis") == "official_ci_positive_goal_observation"}
    selected_source = None
    if isinstance(review.get("ci_source"), dict) and review["ci_source"].get("kind") == "github-actions-command-v1":
        # Multi-command GHA jobs need a job-level anchor independent of the
        # draft goal inventory, which intentionally does not duplicate a whole
        # job's observations across every command.
        index_ref = spec["ci_alignment"].get("archived_ci_index")
        if index_ref:
            ci_index = load_json(bound_file(output, index_ref))
            if (ci_index.get("repo") != task["repo"] or ci_index.get("sha") != task["sha"]
                    or ci_index.get("selected_url") != spec["ci_alignment"]["selected_url"]):
                raise ValueError("Frozen CI job index identity differs from selected task")
            job_ref = ci_index.get("sources", {}).get("job_log")
            if job_ref:
                bound_file(output, job_ref)
                allowed_ci.add(job_ref["sha256"])
        selected_source = _reviewed_gha_command(review, source_base, spec, task, allowed_ci)
    elif review.get("ci_source"):
        from scripts.requirements_ci_sources import load_selected_ci_source
        selected_source = load_selected_ci_source(
            review, source_base, frozen_ci_index_ref=spec["ci_alignment"].get("archived_ci_index", {}),
            frozen_ci_index_base=output, task=task, selected_url=spec["ci_alignment"]["selected_url"],
        )
    elif sources["official_ci"]["sha256"] not in allowed_ci:
        raise ValueError("Reviewed CI log is not the frozen official CI source")
    if files["head"].read_text().strip() != task["sha"] or any(files[k].read_bytes().strip() for k in ("tracked_diff", "untracked")):
        raise ValueError("Metadata checkout does not prove the pinned clean source")
    request = load_json(files["preparation_request"])
    original = request.get("original_task", {})
    if _parent_task is not None and request.get("original_steps") is not None:
        if request["original_steps"] != _parent_task["steps"] or original != _parent_task["steps"][0]:
            raise ValueError("Metadata preparation changed the original multi-step task")
        original = next(s for s in request["original_steps"] if s["id"] == step["id"])
    if (request.get("repo") != task["repo"] or request.get("commit") != task["sha"]
            or any(original.get(k) != step.get(k) for k in ("id", "runner", "argv", "java_major", "maven_version"))
            or request.get("cache_policy") != "isolated_metadata_only_not_shared_with_agent"):
        raise ValueError("Metadata preparation did not preserve the frozen task/configuration")
    runtime = ANSI.sub("", files["runtime"].read_text())
    # Match the metadata-inventory parser: legacy 1.8 reports major 8.
    java = re.search(r"Java version:\s*(?:1\.)?(\d+)(?:[.,\s]|$)", runtime)
    if (step.get("maven_version") and not re.search(r"Apache Maven\s+" + re.escape(step["maven_version"]) + r"(?:\s|$)", runtime)
            or step.get("java_major") and (java is None or int(java[1]) != step["java_major"])):
        raise ValueError("Metadata preparation runtime differs from the task")
    for name in ("pom_log", "settings_log"):
        if not re.search(r"^\[INFO\]\s+BUILD SUCCESS\s*$", ANSI.sub("", files[name].read_text()), re.M):
            raise ValueError("Metadata preparation lacks a successful native terminal")
    effective = xml_root(files["effective_pom"].read_bytes())
    roots = list(effective) if multi and effective.tag == "projects" else [effective]
    modules = review.get("modules", []) if multi else [review["module"]]
    if (not modules or len(roots) != len(modules)
            or any(root.tag != "project" for root in roots)
            or len({m.get("id") for m in modules}) != len(modules)
            or len({m.get("path") for m in modules}) != len(modules)
            or not multi and roots[0].find("modules") is not None):
        raise ValueError("Reviewed module inventory differs from the effective POM")
    workspace = review.get("metadata_workspace")
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        raise ValueError("Metadata workspace mapping is not explicit and consistent")
    if multi:
        reviewed_preparation(load_json(files["preparation_status"]), sources, task, step, workspace)
    models, module_map, module_files = {}, {}, {}
    pom_index = load_json(files["source_pom_index"]) if multi else None
    if pom_index and pom_index.get("commit") != task["sha"]:
        raise ValueError("Pinned reactor POM inventory has a different commit")
    for root, reviewed_module in zip(roots, modules):
        module, path = text_at(root, "artifactId"), reviewed_module.get("path")
        if (reviewed_module.get("id") != module or not isinstance(path, str)
                or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or str(PurePosixPath(path)) != path
                or text_at(root, "build/directory") != str(PurePosixPath(workspace) / path / "target")):
            raise ValueError("Metadata workspace mapping is not explicit and consistent")
        models[module], module_map[module] = root, reviewed_module
        if multi:
            source = bound_file(source_base, reviewed_module.get("source_pom", {}))
            source_root = xml_root(source.read_bytes())
            coordinates = {key: text_at(root, tag) for key, tag in (("group_id", "groupId"), ("artifact_id", "artifactId"), ("version", "version"))}
            if (source_root.tag != "project" or text_at(source_root, "artifactId") != module
                    or reviewed_module.get("coordinates") != coordinates
                    or reviewed_module.get("packaging") != (text_at(root, "packaging") or "jar")):
                raise ValueError("Reviewed module differs from its effective or pinned source POM")
            indexed = [r for r in pom_index.get("files", []) if r.get("source_path") == str(PurePosixPath(path) / "pom.xml")]
            if (len(indexed) != 1 or indexed[0].get("sha256") != reviewed_module["source_pom"]["sha256"]
                    or indexed[0].get("pinned_bytes_equal") is not True
                    or indexed[0].get("git_show_exit_code") != 0
                    or indexed[0].get("git_show_argv") != ["git", "show", task["sha"] + ":" + indexed[0]["source_path"]]):
                raise ValueError("Reviewed module POM is absent from the pinned source inventory")
            module_files[module] = source
    root = roots[0]
    settings = xml_root(files["effective_settings"].read_bytes())
    if settings.findall("servers/server") or any(settings.findall(".//" + tag) for tag in ("password", "privateKey", "passphrase")):
        raise ValueError("Settings with credential material cannot be exported by this importer")
    local_repo = text_at(settings, "localRepository")
    if not local_repo or not local_repo.startswith("/"):
        raise ValueError("Captured metadata settings have no resolved repository")
    ci_text = ci_native_text(files["official_ci"].read_text(errors="replace"))
    actual_ci, nested_ci = ci_binding_inventory(ci_text)
    bindings = review.get("ci_bindings", [])
    fields = ("goal", "version", "execution", "module", "occurrence", "position", "line")
    observed_modules = {b["module"] for b in actual_ci}
    if ([{k: b.get(k) for k in fields} for b in bindings] != actual_ci
            or not observed_modules <= set(models)):
        raise ValueError("Every ordered CI occurrence must be explicitly reviewed")
    selected = maven_goals(step["argv"]) or shlex.split(text_at(root, "build/defaultGoal") or "")
    for module, model in models.items():
        if module not in observed_modules:
            _reviewed_idle_pom(module_map[module], model, ci_text, selected)
        elif module_map[module].get("no_ci_bindings") is not None:
            raise ValueError("A module with native goals cannot claim an idle POM disposition")
    nested_review = review.get("nested_ci_bindings", [])
    if [{k: b.get(k) for k in fields} for b in nested_review] != nested_ci or any(
        b.get("disposition") != "fork_support" or not b.get("reason") or b.get("requirement_id")
        for b in nested_review
    ):
        raise ValueError("Every nested CI binding needs review and cannot substitute for a top-level outcome")
    descriptors, descriptor_sources = reviewed_plugin_descriptors(review, source_base, models)
    declarations = [({**b, "module": module} if multi else b)
                    for module, model in models.items() for b in pom_execution_bindings(model, descriptors)]
    reviewed_declarations = review.get("pom_bindings", [])
    pom_fields = ("plugin", "version", "goal", "execution", "phase") + (("module",) if multi else ())
    if [{k: b.get(k) for k in pom_fields} for b in reviewed_declarations] != declarations:
        raise ValueError("Every effective POM execution must be explicitly reviewed")
    observed = {(b["goal"], b["execution"], b["version"], b["module"] if multi else None) for b in actual_ci}
    for declaration in reviewed_declarations:
        key = (declaration["goal"], declaration["execution"], declaration["version"], declaration.get("module"))
        if not declaration.get("reason") or declaration.get("disposition") not in {"observed", "outside_task_lifecycle", "not_lifecycle_bound"}:
            raise ValueError("Unresolved effective POM binding disposition")
        if (key in observed) != (declaration["disposition"] == "observed"):
            raise ValueError("POM binding review contradicts the official execution")
    selected_goals = maven_goals(step["argv"])
    if not selected_goals:
        selected_goals = shlex.split(text_at(root, "build/defaultGoal") or "")
        if review.get("selected_goals") != selected_goals or spec["steps"][0].get("effective_goals") != selected_goals:
            raise ValueError("Default goal order must match source POM, effective POM and explicit review")
    explicit_goals = [normalize_goal(goal) for goal in selected_goals if ":" in goal]
    explicit_bindings = [b for b in actual_ci if b["execution"] == "default-cli"]
    if any([b["goal"] for b in explicit_bindings if b["module"] == module] != explicit_goals for module in models):
        raise ValueError("Explicit command goals must preserve their full frozen order")
    plugin_versions = {}
    for module, model in models.items():
        for path in ("build/pluginManagement/plugins/plugin", "build/plugins/plugin"):
            for plugin in model.findall(path):
                prefix = plugin_prefix(plugin, descriptors)
                if text_at(plugin, "version"):
                    plugin_versions[module, prefix] = text_at(plugin, "version")
    declared_keys = {(p["goal"], p["execution"], p["version"], p.get("module")) for p in declarations}
    for binding in actual_ci:
        if (binding["goal"], binding["execution"], binding["version"], binding["module"] if multi else None) in declared_keys:
            continue
        if (binding["execution"] != "default-cli" or binding["goal"] not in explicit_goals
                or plugin_versions.get((binding["module"], binding["goal"].split(":")[0])) != binding["version"]):
            raise ValueError("CI binding has no matching effective POM or explicit command declaration")
    archived = {name: archive_source(path, output, basis="reviewed_" + name) for name, path in files.items()}
    archived.update({name: archive_source(path, output, basis="reviewed_plugin_descriptor")
                     for name, path in descriptor_sources.items()})
    archived_modules = {module: archive_source(path, output, basis="reviewed_pinned_module_pom")
                        for module, path in module_files.items()}
    if selected_source:
        archived.update({name: archive_source(path, output, basis="reviewed_" + name)
                         for name, path in selected_source["source_paths"].items()})
    review_ref = archive_source(review_path, output, basis="explicit_reviewed_execution_plan")
    launcher_review = None
    if review.get("launcher_review") is not None:
        launcher_review, launcher_sources = import_launcher_review(review, source_base, task, step, output)
        archived.update(launcher_sources)
    jvm_review = None
    if review.get("jvm_config_review") is not None:
        if step["argv"][0] != "mvn":
            raise ValueError("JVM configuration review currently requires direct Maven")
        jvm_review, jvm_sources = import_jvm_config_review(review, source_base, task, output)
        archived.update(jvm_sources)
    rows = deepcopy(review.get("requirements", []))
    by_id = {r["id"]: r for r in rows}
    if not rows or len(by_id) != len(rows):
        raise ValueError("Reviewed requirements must be nonempty and uniquely named")
    mapped = set()
    task_bindings = bindings
    if selected_source:
        from scripts.requirements_ci_sources import project_declared_variant_bindings
        task_bindings = project_declared_variant_bindings(actual_ci, bindings, selected_source["variant"])
    outcome_bindings = []
    for binding in task_bindings:
        keys = binding.get('requirement_ids')
        if keys is None:
            outcome_bindings.append(binding)
            continue
        # Generating documentation and producing its archive are separate
        # obligations with one native producer. Preserve the source occurrence
        # once; do not duplicate a test invocation or a report pool.
        if (binding.get('disposition') != 'requirement' or binding.get('requirement_id')
                or binding.get('goal') != 'javadoc:jar' or not isinstance(keys,list)
                or len(keys)!=2 or len(set(keys))!=2 or any(k not in by_id for k in keys)
                or {(by_id[k]['kind'],by_id[k]['validation']['rule']) for k in keys}
                   != {('documentation','native_goal'),('package','artifact')}):
            raise ValueError('Shared documentation/archive producer requires two distinct typed obligations')
        outcome_bindings.extend({**binding, 'requirement_id':key} for key in keys)
    for binding in outcome_bindings:
        disposition = binding.get("disposition")
        if not binding.get("reason") or disposition not in {"requirement", "support", "ci_disabled", "same_configuration_reuse"}:
            raise ValueError("Unresolved CI binding disposition")
        module = binding["module"]
        model = models[module]
        index = binding.get("ci_position", binding["position"])
        end = bindings[index + 1]["line"] - 1 if index + 1 < len(bindings) else len(ci_text.splitlines())
        segment = "\n".join(ci_text.splitlines()[binding["line"] - 1:end])
        if disposition == "same_configuration_reuse":
            previous = binding.get("reuse_of", {})
            earlier = [b for b in task_bindings if all(b.get(k) == previous.get(k) for k in
                       ("goal", "version", "execution", "module", "occurrence", "position"))]
            key = binding.get("requirement_id")
            witness = "Skipping execution of surefire because it has already been run for this configuration"
            if (binding["goal"] != "surefire:test" or key not in mapped
                    or by_id[key]["validation"]["rule"] != "junit"
                    or binding.get("reuse_witness") != witness
                    or not re.search(r"^\[INFO\]\s+" + re.escape(witness) + r"\s*$", segment, re.M)
                    or len(earlier) != 1 or earlier[0].get("disposition") != "requirement"
                    or earlier[0].get("requirement_id") != key
                    or any(previous.get(k) != binding[k] for k in ("goal", "version", "execution", "module"))
                    or previous["position"] >= binding["position"]):
                raise ValueError("Same-configuration reuse needs an earlier matching test and exact native witness")
            by_id[key]["validation"].setdefault("reused_native_bindings", []).append(
                {k: binding[k] for k in ("goal", "execution", "occurrence", "position")})
            by_id[key]["sources"].append({**archived["official_ci"], "line": binding["line"]})
            continue
        if disposition != "requirement":
            if binding.get("requirement_id"):
                raise ValueError("Support and disabled occurrences cannot claim a requirement outcome")
            if disposition == "ci_disabled":
                witness = binding.get('disabled_witness', {})
                if witness.get('type') == 'source_empty_surefire_selection':
                    if witness.get('effective_pom_sha256') != sources['effective_pom']['sha256']:
                        raise ValueError('Empty selection review has a different effective model')
                    if witness.get('official_ci',{}).get('sha256') != sources['official_ci']['sha256']:
                        raise ValueError('Empty selection review has a different official invocation')
                    reviewed_empty_surefire_selection(binding, model, witness, source_base, task, workspace)
                    for name in ('git_tree','git_commit','surefire_sources','official_ci'):
                        archived['empty_selection_'+name] = archive_source(bound_file(source_base,witness[name]),output,basis='reviewed_empty_test_selection')
                    for index, source in enumerate(witness['test_sources']):
                        archived['empty_selection_test_'+str(index)] = archive_source(bound_file(source_base,source),output,basis='reviewed_empty_test_selection')
                    if witness['compiler_discovery_review'].get('dependency_bundle'):
                        archived['empty_selection_processor_bundle'] = archive_source(bound_file(source_base,witness['compiler_discovery_review']['dependency_bundle']),output,basis='reviewed_empty_test_selection')
                else:
                    reviewed_disabled_binding(binding, segment, model, sources["effective_pom"]["sha256"])
            elif binding["goal"] in CHECK_GOALS or binding["goal"] in {"compiler:compile", "compiler:testCompile", "surefire:test", "install:install"}:
                raise ValueError("A mandatory outcome cannot be relabeled as support")
            elif binding["goal"] == "checkstyle:checkstyle":
                for plugin in model.findall("build/plugins/plugin"):
                    if text_at(plugin, "artifactId") != "maven-checkstyle-plugin":
                        continue
                    executions = [e for e in plugin.findall("executions/execution")
                                  if (text_at(e, "id") or "default") == binding["execution"]]
                    flag = text_at(executions[0], "configuration/failsOnError") if len(executions) == 1 else None
                    if (flag if flag is not None else text_at(plugin, "configuration/failsOnError")) == "true":
                        raise ValueError("Checkstyle configured to fail on errors is a mandatory check")
            continue
        key = binding.get("requirement_id")
        if key not in by_id or key in mapped and not multi:
            raise ValueError("Each outcome needs one explicit native binding")
        row = by_id[key]
        if multi and row.get("module") != module:
            raise ValueError("A requirement cannot merge native outcomes from different modules")
        if row.get("dependencies_complete") is not True or "depends_on" not in row:
            raise ValueError("Requirement dependencies need explicit review")
        row.update(step_id=step["id"], module=module, scope={"status": "declared", "module_path": module_map[module]["path"]})
        if key not in mapped:
            row["validation"] = {"rule": row["validation"]["rule"],
                                 **({"report_directories": row["validation"]["report_directories"]}
                                    if "report_directories" in row["validation"] else {}),
                                 "goals": [], "position": binding["position"], "plan_resolved": True}
            row["sources"] = [review_ref, archived["effective_pom"]]
            if multi:
                row["sources"].append(archived_modules[module])
        mapped.add(key)
        if binding["goal"] not in row["validation"]["goals"]:
            row["validation"]["goals"].append(binding["goal"])
        if multi:
            row["validation"].setdefault("native_bindings", []).append(
                {k: binding[k] for k in ("goal", "execution", "occurrence", "position")})
        else:
            row["validation"].update(execution=binding["execution"], occurrence=binding["occurrence"])
        row["sources"].append({**archived["official_ci"], "line": binding["line"]})
        coordinates = {key: text_at(model, tag) for key, tag in (("group_id", "groupId"), ("artifact_id", "artifactId"), ("version", "version"))}
        for artifact in row.get("expectations", {}).get("artifacts", []):
            if artifact.get("module") != module or artifact.get("evidence_tier") != "declared":
                raise ValueError("Declared artifacts must name the reviewed module")
            if artifact.get("coordinates") and artifact["coordinates"] != coordinates:
                raise ValueError("Artifact coordinates differ from the effective POM")
            path = artifact.get("path", artifact.get("repository_relative_path"))
            if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
                raise ValueError("Reviewed artifact path must remain relative to its declared root")
    if mapped != set(by_id):
        raise ValueError("A requirement lacks its reviewed native occurrence")
    installed_poms, generated_pom_sources = reviewed_install_poms(review, source_base, modules, task_bindings, rows, output)
    archived.update(generated_pom_sources)
    installs = [a for row in rows if row["kind"] == "install" for a in row.get("expectations", {}).get("artifacts", [])]
    if installs:
        # The narrow install importer covers every actual repository write in
        # the selected green CI cell, including custom and attached artifacts.
        writes = re.findall(r"^\[INFO\] Installing (.+) to (.+)\s*$", ci_text, re.M)
        expected = {repository_artifact_path(a): a for a in installs}
        if len(expected) != len(installs) or len(writes) != len(installs):
            raise ValueError("Reviewed install scope must cover every CI repository write")
        seen, packaged, ci_root = set(), set(), None
        for source, destination in writes:
            matches = [path for path in expected if destination.endswith("/" + path)]
            if len(matches) != 1 or matches[0] in seen:
                raise ValueError("Reviewed install coordinate differs from official CI")
            seen.add(matches[0])
            if expected[matches[0]].get("role") == "installed_pom":
                suffix = "/" + installed_poms[expected[matches[0]]["module"]]
                if not source.endswith(suffix):
                    raise ValueError("Official installed POM does not map to its reviewed module")
                candidate_root = source.removesuffix(suffix)
                if ci_root is not None and candidate_root != ci_root:
                    raise ValueError("Official installed modules do not share the reviewed checkout")
                ci_root = candidate_root
        if not ci_root:
            raise ValueError("Install scope must include its POM coordinate")
        pom_paths = set(installed_poms.values())
        for source, _ in writes:
            if not source.startswith(ci_root + "/"):
                raise ValueError("Custom installed source lies outside reviewed checkout")
            relative = source.removeprefix(ci_root + "/")
            if relative not in pom_paths:
                packaged.add(relative)
        declared = {a["path"] for row in rows if row["kind"] == "package" for a in row.get("expectations", {}).get("artifacts", [])}
        if packaged != declared:
            raise ValueError("Package outputs do not cover all CI installed binaries and reports")
    result = deepcopy(spec)
    result["requirements"] = rows
    result["annotation_completeness"] = {"status": "complete", "gaps": [], "labels_are_known_lower_bound": False,
                                          "basis": "explicit_source_bound_multi_module_review" if multi else "explicit_source_bound_single_module_review", "review": review_ref}
    result["group_labels"] = sorted({r["kind"] for r in rows})
    result["ci_alignment"]["module_paths_resolved"] = True
    result["reviewed_execution_plan"] = {"review": review_ref, "sources": archived, "ci_bindings": bindings,
                                        "pom_bindings": reviewed_declarations, "metadata_local_repository": local_repo,
                                        "cache_policy": request["cache_policy"], "notes": review["review_notes"]}
    if multi:
        result["reviewed_execution_plan"]["modules"] = [
            {**m, "source_pom": archived_modules[m["id"]]} for m in modules]
    if selected_source:
        result["reviewed_execution_plan"].update(
            ci_source_lineage=selected_source["lineage"], declared_variant=selected_source["variant"],
            task_bindings=task_bindings,
        )
        result["ci_alignment"]["declared_variant"] = selected_source["variant"]
    if nested_review or not maven_goals(step["argv"]):
        result["reviewed_execution_plan"].update(nested_ci_bindings=nested_review, selected_goals=selected_goals)
    for s in result["steps"]:
        s.update(execution_plan_resolved=True, effective_pom_status="captured_reviewed",
                 modules=[{"id": m["id"], "path": m["path"]} for m in modules])
        if launcher_review:
            s["launcher_review"] = launcher_review
        if jvm_review:
            s["jvm_config_review"] = jvm_review
    return validate_requirements(result, task)


def build_bundle(manifest_path: Path, output: Path, repo: Path, review_paths: list[Path] | None = None,
                 preparation: Path | None = None, known_paths: list[Path] | None = None) -> dict:
    if output.resolve() == manifest_path.parent.resolve():
        raise ValueError("requirements v2 output must not overwrite the frozen v1 bundle")
    if preparation is not None and not Path(preparation).is_dir():
        raise ValueError("Metadata preparation must name its root directory, not a status file")
    manifest = json.loads(manifest_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    projects = []
    reviews = {}
    for path in review_paths or []:
        identity = json.loads(path.read_text())["project_id"]
        if identity in reviews:
            raise ValueError("Duplicate reviewed project")
        reviews[identity] = path
    if set(reviews) - {p["id"] for p in manifest["projects"]}:
        raise ValueError("Reviewed project is outside the frozen manifest")
    known_reviews = {}
    for path in known_paths or []:
        identity = json.loads(path.read_text())["project_id"]
        if identity in known_reviews or identity not in {p["id"] for p in manifest["projects"]}:
            raise ValueError("Duplicate or unplanned known requirement supplement")
        known_reviews[identity] = path
    counts: Counter = Counter()
    for project in manifest["projects"]:
        spec = build_project(project, manifest_path.parent, output, repo)
        task = json.loads((manifest_path.parent / project["task"]["path"]).read_text())
        if project["id"] in reviews:
            spec = apply_reviewed_plan(spec, task, reviews[project["id"]], output)
        if preparation is not None:
            from scripts.requirements_metadata_inventory import attach_preparation_inventory
            spec = attach_preparation_inventory(spec, task, preparation, output)
        if project["id"] in known_reviews:
            from scripts.requirements_metadata_inventory import append_known_requirements
            spec = append_known_requirements(spec, task, known_reviews[project["id"]], output)
        from sag.benchmark.ci_count_semantics import build_jenkins_count_semantics, unavailable
        from sag.benchmark.requirements import bound_file, validate_ci_count_metadata
        alignment = spec["ci_alignment"]
        index_ref = alignment.get("archived_ci_index")
        if index_ref and project["ci"].get("comparison_admitted") is True:
            original_index = {**index_ref, "path": "ci/" + project["id"] + "/index.json"}
            ci_counts = build_jenkins_count_semantics(
                original_index, base=manifest_path.parent,
                selected_url=alignment["selected_url"], selected_cell=alignment["selected_cell"])
            archived_count_sources = {}
            for name, ref in ci_counts.get("sources", {}).items():
                try:
                    path = bound_file(manifest_path.parent, ref)
                except (ValueError, OSError):
                    if ci_counts["status"] == "available":
                        raise
                    continue
                archived_count_sources[name] = archive_source(path, output, basis="ci_count_semantics_" + name)
            ci_counts["sources"] = archived_count_sources
        else:
            ci_counts = unavailable("Frozen task has no admitted matching CI count reference; legacy totals are not used",
                                 selected_url=alignment["selected_url"], selected_cell=alignment["selected_cell"])
        alignment["test_count_semantics"] = ci_counts
        validate_ci_count_metadata(spec, base=output, required=True)
        target = output / "requirements" / (project["id"] + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
        counts.update(spec["group_labels"])
        projects.append(
            {
                "id": project["id"],
                "task": project["task"],
                "task_sha256": spec["task_sha256"],
                "requirements": {
                    "path": str(target.relative_to(output)),
                    "sha256": digest(spec),
                    "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                },
                "group_labels": spec["group_labels"],
                "readiness": spec["annotation_completeness"]["status"],
                "ci_test_count_reference": {key: ci_counts.get(key) for key in
                    ("status", "reported_count", "skipped_count", "assessed_count", "reason")},
                **({"preparation": {
                    "status": spec["metadata_preparation"]["status"],
                    "effective_models": sum(s.get("inventory", {}).get("model_count", 0) for s in spec["metadata_preparation"]["steps"]),
                    "models_with_default_derivation_exceptions": sum(s.get("inventory", {}).get("models_requiring_manual_review", 0) for s in spec["metadata_preparation"]["steps"]),
                    "models_awaiting_artifact_exception_review": 0 if spec["annotation_completeness"]["status"] == "complete" else sum(s.get("inventory", {}).get("models_requiring_manual_review", 0) for s in spec["metadata_preparation"]["steps"]),
                    "steps_awaiting_evidence": sum("inventory" not in s for s in spec["metadata_preparation"]["steps"]),
                }} if "metadata_preparation" in spec else {}),
            }
        )
    index = {
        "schema_version": 2,
        "policy_version": POLICY_VERSION,
        "benchmark": "sag-original20-requirements-v2-review",
        "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "planned_project_count": len(projects),
        "projects": projects,
        "group_counts_known_lower_bound": dict(sorted(counts.items())),
        "campaign_ready": all(p["readiness"] == "complete" for p in projects),
        "ready_project_count": sum(p["readiness"] == "complete" for p in projects),
        "ci_test_count_reference_available": sum(p["ci_test_count_reference"]["status"] == "available" for p in projects),
        "definition_and_ci_count_reference_ready": sum(p["readiness"] == "complete" and p["ci_test_count_reference"]["status"] == "available" for p in projects),
        "readiness_reason": "effective execution plans, reactor module paths and declared artifacts require metadata preparation/review",
    }
    (output / "manifest.json").write_text(json.dumps(index, indent=2) + "\n")
    rows = [
        "# CI requirements v2: frozen review metadata",
        "",
        "The original task bytes and portable v1 bundle are unchanged.",
        "These sidecars replace legacy stages for v2 analysis. Only rows marked complete have a source-bound reviewed requirement definition. This is metadata readiness, never a new successful agent run; remaining rows are drafts.",
        "",
        "Every unresolved execution plan remains a mandatory unclassified requirement. Missing effective POMs prevent complete verdicts; source POMs are not relabeled as effective POMs.",
        "Positive archived CI goal observations establish a lower bound only. They do not resolve module paths, repeated goal occurrences, forked tasks or all inherited checks.",
        "",
        "| Project | Known categories | Definition readiness | CI reported / skipped / assessed |",
        "|---|---|---|---|",
    ]
    rows.extend(
        f"| {p['id']} | {', '.join(p['group_labels'])} | {p['readiness']} | "
        + (" / ".join(str(p['ci_test_count_reference'][key]) for key in ('reported_count', 'skipped_count', 'assessed_count'))
           if p['ci_test_count_reference']['status'] == 'available' else "unavailable") + " |" for p in projects
    )
    rows += [
        "",
        "Before a formal campaign, capture effective POMs/settings in a separate metadata workspace under the task JDK/profiles/properties, freeze reactor membership and review artifact/plugin exceptions. Do not pass its warmed cache to one agent arm.",
        "The importer in scripts/build_benchmark_requirements.py reads resolved POM bytes and records declared artifact expectations without executing Maven. It never guesses missing artifact scope from old empty arrays.",
        "",
        "Reviewed-plan import is explicit: repeat `--reviewed-plan review-PROJECT.json` when regenerating this bundle. Omitting reviewed inputs generates drafts. A review names every CI occurrence and every effective-POM execution, with separate dispositions for active requirements, lifecycle support and explicitly disabled CI bindings. The copied review and all selected source bytes are retained under sources/; preparation caches are not exported.",
        "Commons DbUtils ends at test: its reviewed scope includes production/test compilation, JUnit tests and active checks, including coverage-report generation, but no JAR or coverage threshold. Commons Net ends at install: its reviewed scope includes nine package files and ten local-repository coordinates, including attached JARs and SBOM files. The actual repository root and fresh output bytes must still be observed during each future run.",
        "Commons CSV and DBCP use their exact declared defaultGoal, including verify, repeated license checks, API compatibility, static analysis, duplicate-code/style checks and Javadoc. Each has seven distinct package files; neither task requests install. Their review separates every nested Maven fork from the top-level requirement occurrences. DBCP puts Checkstyle before SpotBugs, while CSV puts it after Javadoc: these orders are preserved, not standardized.",
        "Tomcat Migration stops at test. Its generated JARs are test fixtures, not additional distribution obligations. The Sling reviews preserve the old declared deploy-to-install variant: the original selected-node HTML, CI stage and index remain archived, while publication is explicitly outside the frozen benchmark task.",
        "Captured effective reactor models are preparation inventories, not executed CI module denominators. A repeated module in two commands remains two model appearances. Ordinary packaging defaults and custom artifact exceptions are retained separately from the final reviewed requirements. A capture or a known-requirement supplement never upgrades review_required to complete.",
        "CI counts describe the selected native report pool, not proof that every required test type is represented. Whisker and RAT also require non-Surefire tests. Native report counts and observed case outcomes are reconciled without claiming case identity equality; assessed never comes from an untyped legacy executed field. Unsupported, missing or contradictory reference evidence stays unavailable and supplies no denominator.",
    ]
    (output / "README.md").write_text("\n".join(rows) + "\n")
    return index


def import_request(request_path: Path, output: Path) -> dict:
    """Archive and import captured effective metadata; never execute its argv."""
    request = json.loads(request_path.read_text())
    pom_path = (request_path.parent / request["pom_path"]).resolve()
    imported = import_effective_pom(
        pom_path.read_bytes(),
        module=request["module"],
        endpoint=request.get("lifecycle_endpoint"),
        local_repository=request.get("local_repository"),
        provenance=request["provenance"],
        bundle_mapping_confirmed=request.get("bundle_mapping_confirmed", False),
    )
    output.mkdir(parents=True, exist_ok=True)
    imported["source"] = archive_source(pom_path, output, basis="captured_effective_pom")
    imported["request_source"] = archive_source(
        request_path, output, basis="effective_pom_resolution_request"
    )
    imported["schema_version"] = 2
    imported["policy_version"] = POLICY_VERSION
    imported["note"] = (
        "Declared artifact metadata only. Review and freeze the full execution plan before marking a requirements sidecar complete."
    )
    (output / "effective-pom-import.json").write_text(
        json.dumps(imported, ensure_ascii=False, indent=2) + "\n"
    )
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--manifest", type=Path, default=root / "output/sag-benchmark20-20260917/manifest.json"
    )
    parser.add_argument(
        "--output", type=Path, default=root / "output/sag-benchmark20-requirements-20260922"
    )
    parser.add_argument("--repo-root", type=Path, default=root)
    parser.add_argument("--reviewed-plan", type=Path, action="append", default=[], help="Apply an explicit source-bound reviewed single-module plan; repeat for multiple projects")
    parser.add_argument("--metadata-preparation", type=Path, help="Root directory of captured per-project metadata; attach artifact defaults and gaps without upgrading annotation completeness")
    parser.add_argument("--known-requirements", type=Path, action="append", default=[], help="Append source-bound known requirements while retaining review_required status")
    parser.add_argument(
        "--effective-pom-request",
        type=Path,
        help="Import a captured effective POM request JSON instead of generating the cohort drafts",
    )
    args = parser.parse_args()
    if args.effective_pom_request:
        result = import_request(args.effective_pom_request.resolve(), args.output.resolve())
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "status": result["resolution_status"],
                    "exceptions": result["exceptions"],
                },
                indent=2,
            )
        )
        return
    index = build_bundle(args.manifest.resolve(), args.output.resolve(), args.repo_root.resolve(), [p.resolve() for p in args.reviewed_plan], args.metadata_preparation, args.known_requirements)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "projects": index["planned_project_count"],
                "campaign_ready": index["campaign_ready"],
                "groups": index["group_counts_known_lower_bound"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
