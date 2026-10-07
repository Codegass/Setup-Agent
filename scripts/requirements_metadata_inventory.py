"""Offline inventory of effective reactor models, never a lifecycle resolver.

The preparation cache stays outside agent runs. These declared defaults and
review exceptions cannot, by themselves, make a requirement definition complete.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _metadata_options(argv):
    from scripts.build_benchmark_requirements import VALUE_OPTIONS
    options, value = [], False
    for arg in argv[1:]:
        if value:
            options.append(arg)
            value = False
        elif arg in VALUE_OPTIONS:
            options.append(arg)
            value = True
        elif arg.startswith("-"):
            options.append(arg)
    if value:
        raise ValueError("Frozen Maven option is missing its value")
    return options


def effective_reactor_inventory(raw, *, workspace, source_poms, endpoint,
                                local_repository, provenance):
    from scripts.build_benchmark_requirements import (
        import_effective_pom, pom_execution_bindings, text_at, xml_root,
    )
    if provenance.get("sha256") != _sha(raw):
        raise ValueError("Effective reactor bytes differ from preparation")
    work = PurePosixPath(workspace)
    if not work.is_absolute() or ".." in work.parts:
        raise ValueError("Metadata workspace must be an explicit absolute path")
    root = xml_root(raw)
    models = [root] if root.tag == "project" else list(root) if root.tag == "projects" else []
    if not models or any(model.tag != "project" for model in models):
        raise ValueError("Expected effective project or reactor projects")
    rows, identities, paths = [], set(), set()
    for model in models:
        module = text_at(model, "artifactId")
        build = text_at(model, "build/directory") or ""
        candidate = PurePosixPath(build)
        mapping_errors = []
        path = None
        if candidate.name == "target" and candidate.is_relative_to(work) and ".." not in candidate.parts:
            relative = candidate.parent.relative_to(work)
            path = str(relative)
            source_path = str(relative / "pom.xml")
            source = source_poms.get(source_path)
            if not isinstance(source, bytes):
                mapping_errors.append("pinned_source_pom_not_captured")
            else:
                source_root = xml_root(source)
                if source_root.tag != "project" or text_at(source_root, "artifactId") != module:
                    mapping_errors.append("effective_and_pinned_source_module_disagree")
        else:
            mapping_errors.append("custom_build_directory_needs_explicit_module_mapping")
        if not module or module in identities or path is not None and path in paths:
            mapping_errors.append("nonunique_module_identity_or_path")
        identities.add(module)
        if path is not None:
            paths.add(path)
        mapped = deepcopy(model)
        # Relocate only known workspace-relative build paths. Preserve the raw
        # effective XML separately, and leave outside/custom paths unresolved.
        if not mapping_errors:
            for tag in ("build/directory", "build/outputDirectory", "build/testOutputDirectory"):
                node = mapped.find(tag)
                if node is not None and node.text:
                    value = PurePosixPath(node.text)
                    if value.is_absolute() and value.is_relative_to(work) and ".." not in value.parts:
                        node.text = str(value.relative_to(work))
        fragment = ET.tostring(mapped, encoding="utf-8")
        record = import_effective_pom(
            fragment, module=module, endpoint=endpoint, local_repository=local_repository,
            provenance={**provenance, "sha256": _sha(fragment),
                        "source_reactor_sha256": _sha(raw),
                        "mapping": "workspace_relative_paths_with_pinned_module_pom"},
        )
        record.update(module_path=path, mapping_errors=mapping_errors,
                      effective_pom_bindings=pom_execution_bindings(model))
        if path is not None and source_poms.get(str(PurePosixPath(path) / "pom.xml")) is not None:
            record["source_pom_sha256"] = _sha(source_poms[str(PurePosixPath(path) / "pom.xml")])
        record["exceptions"] = sorted(set(record["exceptions"] + mapping_errors))
        record["resolution_status"] = "unresolved" if record["exceptions"] else "declared"
        rows.append(record)
    return {"models": rows, "model_count": len(rows),
            "default_artifact_declarations": sum(len(r["artifacts"]) + len(r["installs"]) for r in rows),
            "models_requiring_manual_review": sum(bool(r["exceptions"]) for r in rows),
            "scope": "effective models; not proof of executed reactor or complete lifecycle requirements",
            "endpoint": endpoint, "source_sha256": _sha(raw)}


def attach_preparation_inventory(spec, task, preparation, output):
    """Bind captured preparation to the task; add a non-authoritative audit.

    Failed preparations are retained as explicit gaps. Malformed or changed
    source references fail the import, rather than silently becoming absent.
    """
    from scripts.build_benchmark_requirements import (
        archive_source, lifecycle_endpoint, maven_goals, text_at, xml_root,
    )
    from sag.benchmark.requirements import bound_file, load_json

    preparation = Path(preparation).resolve()
    project = spec["project_id"]
    status_path = preparation / project / "metadata-status.json"
    audit = {"status": "unavailable", "authorizes_complete": False, "steps": [], "sources": {},
             "cache_policy": "isolated_metadata_only_not_shared_with_agent"}
    result = deepcopy(spec)

    def archive(name, path):
        ref = archive_source(path, output, basis="metadata_inventory_" + name)
        audit["sources"][name] = ref
        return ref

    source_poms = {}
    pom_index_path = preparation / project / "source-poms" / "index.json"
    if pom_index_path.exists():
        index = load_json(pom_index_path)
        if index.get("commit") != task["sha"]:
            raise ValueError("Source POM inventory has a different commit")
        archive("source_pom_index", pom_index_path)
        if index.get("inventory_stdout"):
            inventory_path = bound_file(preparation, index["inventory_stdout"])
            error_path = bound_file(preparation, index["inventory_stderr"])
            archive("source_pom_inventory", inventory_path)
            archive("source_pom_inventory_stderr", error_path)
            names = inventory_path.read_bytes().split(b"\0")
            if (index.get("inventory_exit_code") != 0 or names[-1] != b""
                    or sorted(n.decode() for n in names[:-1]) != sorted(r["source_path"] for r in index.get("files", []))):
                raise ValueError("Source POM inventory is incomplete or inconsistent")
        for row in index.get("files", []):
            source_path = row.get("source_path", "")
            relative = PurePosixPath(source_path)
            if relative.is_absolute() or ".." in relative.parts or relative.name != "pom.xml":
                raise ValueError("Invalid source POM path")
            if row.get("pinned_bytes_equal") is not True or row.get("kind") != "file":
                continue
            raw_path = bound_file(preparation, row)
            if source_path in source_poms:
                raise ValueError("Duplicate source POM")
            source_poms[source_path] = raw_path.read_bytes()
            archive("source_pom:" + source_path, raw_path)
    if not status_path.exists():
        reviewed = spec.get("reviewed_execution_plan")
        if reviewed and len(task["steps"]) == 1:
            # Original preparation used a shell runner; its existing reviewed
            # raw sources remain the authority. Do not invent a status receipt.
            review = load_json(bound_file(output, reviewed["review"]))
            raw = bound_file(output, reviewed["sources"]["effective_pom"]).read_bytes()
            runtime = bound_file(output, reviewed["sources"]["runtime"]).read_text()
            version = re.search(r"Apache Maven\s+(\d+\.\d+\.\d+)", runtime)
            step = task["steps"][0]
            inventory = effective_reactor_inventory(
                raw, workspace=review["metadata_workspace"], source_poms=source_poms,
                endpoint=spec["steps"][0]["lifecycle_endpoint"],
                local_repository=reviewed["metadata_local_repository"],
                provenance={"task_sha256": spec["task_sha256"], "commit": task["sha"],
                            "argv": [], "command_source": reviewed["sources"]["preparation_script"],
                            "java_major": step["java_major"], "maven_version": version[1] if version else None,
                            "reactor_resolved": True, "sha256": _sha(raw)},
            )
            audit.update(status="source_bound_review", steps=[{"step_id": step["id"],
                         "status": "source_bound_review", "inventory": inventory,
                         "review": reviewed["review"], "gaps": []}])
            result["metadata_preparation"] = audit
            return result
        audit["reason"] = "metadata_status_not_captured; existing reviewed sources remain authoritative where present"
        audit["steps"] = [{"step_id": s["id"], "status": "not_prepared", "gaps": [audit["reason"]]} for s in task["steps"]]
        result["metadata_preparation"] = audit
        return result
    status = load_json(status_path)
    if status.get("repo") != task["repo"] or status.get("commit") != task["sha"]:
        raise ValueError("Preparation identity differs from task")
    request_path = preparation / ("request-" + project + ".json")
    request = load_json(request_path)
    if (request.get("repo") != task["repo"] or request.get("commit") != task["sha"]
            or request.get("cache_policy") != audit["cache_policy"]):
        raise ValueError("Preparation request identity/cache policy differs")
    declared_steps = request.get("original_steps", [request.get("original_task", {})])
    fields = ("id", "runner", "argv", "cwd", "java_major", "maven_version")
    if [{k: s.get(k) for k in fields} for s in declared_steps] != [{k: s.get(k) for k in fields} for s in task["steps"]]:
        raise ValueError("Preparation did not preserve ordered frozen task steps")

    archive("status", status_path)
    archive("request", request_path)
    if status.get("preparation_script"):
        archive("preparation_script", bound_file(preparation, status["preparation_script"]))
    if status.get("launcher_inputs", {}).get("index"):
        inputs_path = bound_file(preparation, status["launcher_inputs"]["index"])
        inputs = load_json(inputs_path)
        if inputs.get("commit") != task["sha"]:
            raise ValueError("Launcher inputs belong to another revision")
        archive("launcher_inputs_index", inputs_path)
        for key in ("tracked_inventory", "tracked_inventory_stderr"):
            archive("launcher_inputs_" + key, bound_file(preparation, inputs[key]))
        for row in inputs.get("files", []):
            if row.get("kind") == "file":
                archive("launcher_input:" + row["source_path"], bound_file(preparation, row))
    clean = True
    for name, expected in (("HEAD.txt", task["sha"]), ("tracked-diff.txt", ""), ("untracked.txt", "")):
        path = preparation / project / name
        if not path.exists():
            clean = False
            continue
        archive(name, path)
        if path.read_text().strip() != expected:
            clean = False
    rows = {row["id"]: row for row in status.get("steps", [])}
    if len(rows) != len(status.get("steps", [])) or set(rows) - {s["id"] for s in task["steps"]}:
        raise ValueError("Unexpected preparation step inventory")
    for step in task["steps"]:
        state = rows.get(step["id"], {})
        record = {"step_id": step["id"], "status": state.get("status", "not_prepared"), "gaps": []}
        audit["steps"].append(record)
        if state.get("frozen_argv") not in (None, step["argv"]):
            raise ValueError("Preparation step argv differs")
        if state.get("status") != "captured_review_required" or not clean:
            record["gaps"].append("metadata_not_successfully_captured_at_clean_pinned_source")
            continue
        probe = state.get("runtime_probe", {})
        if (probe.get("exit_code") != 0
                or state.get("observed_java_major") != step.get("java_major")
                or step.get("maven_version") and state.get("observed_maven_version") != step["maven_version"]):
            raise ValueError("Captured runtime differs from frozen requirements")
        archive(step["id"] + ":runtime", bound_file(preparation, probe["log"]))
        from scripts.build_benchmark_requirements import ANSI
        runtime_text = ANSI.sub("", bound_file(preparation, probe["log"]).read_text())
        maven = re.search(r"Apache Maven\s+(\d+\.\d+\.\d+)(?:\s|$)", runtime_text)
        java = re.search(r"Java version:\s*(?:1\.)?(\d+)(?:[.,\s]|$)", runtime_text)
        if (not maven or not java or maven[1] != state.get("observed_maven_version")
                or int(java[1]) != state.get("observed_java_major")):
            raise ValueError("Raw metadata runtime contradicts the recorded version")
        launcher, checkout = state.get("launcher"), probe.get("cwd")
        if (not isinstance(checkout, str) or not PurePosixPath(checkout).is_absolute()
                or not isinstance(launcher, str) or not PurePosixPath(launcher).is_absolute()
                or probe.get("argv") != [launcher, "--version"]
                or step["argv"][0] == "mvn" and PurePosixPath(launcher).name != "mvn"
                or step["argv"][0] != "mvn" and PurePosixPath(launcher) != PurePosixPath(checkout) / step["argv"][0]):
            raise ValueError("Metadata runtime launcher is not bound to the frozen command")
        commands = state.get("commands", [])
        if len(commands) != 2 or any(c.get("exit_code") != 0 or c.get("status") != "completed" for c in commands):
            raise ValueError("Effective metadata lacks successful preparation commands")
        outputs = state.get("outputs", {})
        options = _metadata_options(step["argv"])
        for i, command in enumerate(commands):
            goal = ("effective-pom", "effective-settings")[i]
            actual = command.get("argv", [])
            expected_prefix = [launcher, *options, "org.apache.maven.plugins:maven-help-plugin:3.5.2:" + goal, "-Dverbose"]
            if (command.get("cwd") != str(PurePosixPath(checkout) / step.get("cwd", "."))
                    or len(actual) != len(expected_prefix) + 1 or actual[:-1] != expected_prefix
                    or not actual[-1].startswith("-Doutput=/")
                    or not actual[-1].endswith("/" + outputs[goal]["path"])):
                raise ValueError("Metadata command changed frozen configuration, scope or output")
            archive(step["id"] + ":command:" + str(i), bound_file(preparation, command["log"]))
        raw_path = bound_file(preparation, outputs["effective-pom"])
        settings_path = bound_file(preparation, outputs["effective-settings"])
        settings = xml_root(settings_path.read_bytes())
        if settings.findall("servers/server") or any(settings.findall(".//" + tag) for tag in ("password", "privateKey", "passphrase")):
            raise ValueError("Credential-bearing metadata settings are not exportable")
        archive(step["id"] + ":effective_pom", raw_path)
        archive(step["id"] + ":effective_settings", settings_path)
        goals = maven_goals(step["argv"])
        if not goals:
            root = xml_root(raw_path.read_bytes())
            primary = root if root.tag == "project" else root.find("project")
            import shlex
            goals = shlex.split(text_at(primary, "build/defaultGoal") or "") if primary is not None else []
        record["inventory"] = effective_reactor_inventory(
            raw_path.read_bytes(), workspace=commands[0]["cwd"], source_poms=source_poms,
            endpoint=lifecycle_endpoint(goals), local_repository=text_at(settings, "localRepository"),
            provenance={"task_sha256": spec["task_sha256"], "commit": task["sha"],
                        "argv": commands[0]["argv"], "java_major": state["observed_java_major"],
                        "maven_version": state["observed_maven_version"], "reactor_resolved": True,
                        "sha256": outputs["effective-pom"]["sha256"]})
        record["gaps"].append("effective_models_do_not_resolve_all_native_execution_bindings_or_attached_artifacts")
        if step["argv"][0] != "mvn":
            record["gaps"].append("upstream_wrapper_semantics_need_explicit_review")
        if "-q" in step["argv"] or "--quiet" in step["argv"]:
            record["gaps"].append("quiet_output_may_prevent_native_goal_observation")
    audit["status"] = "captured_for_review" if audit["steps"] and all("inventory" in s for s in audit["steps"]) else "partial_or_unavailable"
    result["metadata_preparation"] = audit
    return result


def append_known_requirements(spec, task, review_path, output):
    """Add source-bound known obligations to an incomplete definition only."""
    from scripts.build_benchmark_requirements import archive_source, task_digest
    from sag.benchmark.requirements import bound_file, load_json, validate_requirements

    review_path = Path(review_path).resolve()
    review = load_json(review_path)
    if (spec["annotation_completeness"]["status"] != "review_required"
            or review.get("annotation_completeness") != "review_required"
            or review.get("task_sha256") != task_digest(task)
            or review.get("project_id") != spec["project_id"]
            or review.get("repository") != task["repo"] or review.get("commit") != task["sha"]):
        raise ValueError("Known requirement supplement must bind the same incomplete task")
    result = deepcopy(spec)
    review_ref = archive_source(review_path, output, basis="known_requirement_source_review")
    rows = deepcopy(review.get("requirements", []))
    if not rows:
        raise ValueError("Known requirement supplement is empty")
    for row in rows:
        if not row.get("sources"):
            raise ValueError("Known requirement needs source evidence")
        sources = []
        for ref in row["sources"]:
            path = bound_file(review_path.parent, ref)
            archived = archive_source(path, output, basis=ref.get("basis", "known_requirement_source"))
            for key in ("line_start", "line_end"):
                if key in ref:
                    archived[key] = ref[key]
            sources.append(archived)
        row["sources"] = [review_ref, *sources]
    result["requirements"].extend(rows)
    result["group_labels"] = sorted({r["kind"] for r in result["requirements"] if r["kind"] != "unclassified"})
    result.setdefault("migration", {})["new_categories_not_expressible_by_legacy_stages"] = [
        kind for kind in result["group_labels"] if kind not in {"compile", "package", "install", "test", "native_compile"}
    ]
    result.setdefault("known_requirement_reviews", []).append(review_ref)
    return validate_requirements(result, task)
