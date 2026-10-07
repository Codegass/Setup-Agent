"""Portable, byte-bound inputs for CI verification and module naming.

These files are supplied by the benchmark controller, outside the checkout.
The agent's output and a matching display name are never provenance.
"""

from pathlib import Path
import json
import shutil
import xml.etree.ElementTree as ET

from .requirements import (
    bound_file,
    canonical_digest,
    evaluation_identity,
    normalize_task,
    report_directories,
    validate_requirements,
)


def read_source(base, ref):
    path = bound_file(base, ref)
    data = path.read_bytes()
    if type(ref.get("bytes")) is not int or len(data) != ref["bytes"]:
        raise ValueError("CI source byte length mismatch")
    return data


def reviewed_steps(spec):
    plan = spec.get("reviewed_execution_plan") or {}
    if "steps" in plan:
        return plan["steps"]
    if len(spec.get("steps", [])) == 1 and plan:
        return [{**plan, "step_id": spec["steps"][0]["step_id"]}]
    raise ValueError("Complete reviewed execution plan unavailable")


def frozen_test_scope(task, spec, protocol):
    """None means unknown; an empty mapping means explicitly no test pools."""
    if not isinstance(spec, dict) or not isinstance(protocol, dict) or task is None:
        return None
    if hasattr(task, "model_dump"):
        task = task.model_dump(mode="json")
    validate_requirements(spec, normalize_task(task))
    if any(protocol.get(k) != v for k, v in evaluation_identity(spec).items()):
        raise ValueError("Test scope differs from the pinned requirements protocol")
    if spec["annotation_completeness"]["status"] != "complete":
        return None
    pools = {}
    for row in spec["requirements"]:
        if row["kind"] != "test":
            continue
        directories = report_directories(row)
        path = row.get("scope", {}).get("module_path")
        if directories is None or not path:
            return None
        pools.setdefault(path, set()).update(directories)
    return pools


def module_aliases(spec, base):
    """Resolve Maven names from archived effective models; reject collisions."""
    aliases = {}
    for step in reviewed_steps(spec):
        sources = step["sources"]
        if read_source(base, sources["head"]).decode().strip() != spec["commit"]:
            raise ValueError("Module model belongs to another commit")
        if read_source(base, sources["tracked_diff"]).strip():
            raise ValueError("Module metadata was prepared from modified sources")
        root = ET.fromstring(read_source(base, sources["effective_pom"]))
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]
        models = [root] if root.tag == "project" else root.findall("project")
        declared = next(s["modules"] for s in spec["steps"] if s["step_id"] == step["step_id"])
        for module in step.get("modules", declared):
            coord = module.get("coordinates")
            matches = [m for m in models if m.findtext("artifactId") == module["id"]]
            if coord:
                matches = [
                    m
                    for m in matches
                    if (m.findtext("groupId"), m.findtext("artifactId"), m.findtext("version"))
                    == (coord["group_id"], coord["artifact_id"], coord["version"])
                ]
            if len(matches) != 1:
                raise ValueError("Module has no unique effective POM")
            if coord and module["id"] != coord["artifact_id"]:
                raise ValueError("Module identifier differs from its effective POM")
            if module.get("source_pom"):
                read_source(base, module["source_pom"])
                index = json.loads(read_source(base, sources["source_pom_index"]))
                relative = "pom.xml" if module["path"] == "." else module["path"] + "/pom.xml"
                pinned = [r for r in index["files"] if r.get("source_path") == relative]
                if (
                    index.get("repo") != spec["repository"]
                    or index.get("commit") != spec["commit"]
                    or index.get("head_observed") != spec["commit"]
                    or len(pinned) != 1
                    or pinned[0].get("pinned_bytes_equal") is not True
                    or pinned[0].get("git_show_exit_code") != 0
                    or any(pinned[0].get(k) != module["source_pom"][k] for k in ("sha256", "bytes"))
                ):
                    raise ValueError("Module path is not bound to the pinned source POM")
            elif module["path"] != ".":
                raise ValueError("Child module pinned POM unavailable")
            path = module["path"]
            if Path(path).is_absolute() or ".." in Path(path).parts or not path:
                raise ValueError("Invalid frozen module path")
            name = matches[0].findtext("name") or module["id"]
            version = matches[0].findtext("version")
            if not version or "${" in name or "${" in version:
                raise ValueError("Unresolved effective module display name or version")
            for alias in {path, module["id"], name, name + " " + version}:
                if alias in aliases and aliases[alias] != path:
                    raise ValueError("Ambiguous Maven module alias")
                aliases[alias] = path
    if not aliases:
        raise ValueError("Module mapping unavailable")
    return aliases


def maven_plugin_bindings(spec, base, step_id):
    """Bind banner spellings to unique, frozen module/plugin executions.

    A prefix is the reviewed requirement's spelling, not an artifact-name
    heuristic. The POM must uniquely assign its goal and execution in that
    module, and the observed version must match. Ambiguity earns no alias.
    """
    module_aliases(spec, base)  # validates commit, clean metadata, paths and models
    review = next(s for s in reviewed_steps(spec) if s['step_id'] == step_id)
    ref = review['sources']['effective_pom']
    root = ET.fromstring(read_source(base, ref))
    for node in root.iter():
        node.tag = node.tag.rsplit('}', 1)[-1]
    models = [root] if root.tag == 'project' else root.findall('project')
    bindings = []
    for row in spec['requirements']:
        if row['step_id'] != step_id:
            continue
        matching = [m for m in models if m.findtext('artifactId') == row.get('module')]
        if len(matching) != 1:
            continue
        for native in row.get('validation', {}).get('native_bindings', []):
            prefix, separator, goal = native['goal'].rpartition(':')
            if not separator or ':' in prefix:
                continue
            candidates = []
            for plugin in matching[0].findall('build/plugins/plugin'):
                executions = [e for e in plugin.findall('executions/execution')
                              if (e.findtext('id') or 'default') == native['execution']
                              and goal in [g.text for g in e.findall('goals/goal')]]
                if executions:
                    candidates.append(plugin)
            if len(candidates) != 1:
                continue
            plugin = candidates[0]
            group = plugin.findtext('groupId') or 'org.apache.maven.plugins'
            artifact, version = plugin.findtext('artifactId'), plugin.findtext('version')
            if not artifact or not version or '${' in group + artifact + version:
                continue
            item = {'module': row['module'], 'execution': native['execution'], 'goal': goal,
                    'prefix': prefix, 'artifact': artifact, 'coordinates': group + ':' + artifact,
                    'version': version, 'source': ref}
            if item not in bindings:
                bindings.append(item)
    return bindings


def source_references(spec, base):
    """Explicit provenance allowlist; never copy the checkout or caches."""
    alignment = spec.get("ci_alignment") or {}
    refs = []
    index_ref = alignment.get("archived_ci_index")
    if index_ref:
        refs.append(index_ref)
        index = json.loads(read_source(base, index_ref))
        refs.extend(index.get("sources", {}).values())
    refs.extend((alignment.get("test_count_semantics") or {}).get("sources", {}).values())
    for declared in spec.get("steps", []):
        from .jvm_inputs import validate_environment_review
        review = declared.get("jvm_environment_review")
        if review:
            frozen_step = {"id": declared["step_id"], "runner": declared["runner"], "argv": declared["argv"]}
            validate_environment_review(review, {"sha": spec["commit"], "repo": spec["repository"]}, frozen_step, base=base)
            refs.extend(item["source"] for item in review["values"].values())
    try:
        steps = reviewed_steps(spec)
    except ValueError:
        steps = []
    for step in steps:
        refs.extend(
            ref
            for key, ref in step.get("sources", {}).items()
            if key
            in {
                "head",
                "tracked_diff",
                "effective_pom",
                "source_pom_index",
                "official_ci",
                "ci_original_job",
            }
        )
        refs.extend(m["source_pom"] for m in step.get("modules", []) if m.get("source_pom"))
        def witness_refs(value):
            if isinstance(value, dict):
                if {"path", "sha256", "bytes"} <= value.keys():
                    yield value
                else:
                    for child in value.values():
                        yield from witness_refs(child)
            elif isinstance(value, list):
                for child in value:
                    yield from witness_refs(child)
        for binding in step.get("ci_bindings", []):
            refs.extend(witness_refs(binding.get("disabled_witness")))
    # Legacy Jenkins indexes may describe bytes not yet relocated into this
    # definition. Preserve available sources; absent ones remain unavailable
    # in verification rather than preventing local task recording.
    if index_ref:
        for ref in index.get("evidence", []):
            relative = Path(ref.get("path", ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Jenkins source path escapes its explicit archive")
            if (Path(base) / relative).is_file():
                refs.append(ref)
    unique = {}
    for ref in refs:
        # Index entries are archival references, not paths to follow arbitrarily.
        read_source(base, ref)
        if ref["path"] in unique and any(
            unique[ref["path"]][k] != ref[k] for k in ("sha256", "bytes")
        ):
            raise ValueError("Conflicting CI source references")
        unique[ref["path"]] = ref
    return list(unique.values())


def archive_sources(spec, source_base, destination):
    destination = Path(destination)
    for ref in source_references(spec, source_base):
        source = bound_file(source_base, ref)
        target = destination / ref["path"]
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError("CI archive path escapes its root")
        if target.exists():
            read_source(destination, ref)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            read_source(destination, ref)


def archived_source_root(spec, run, base):
    reference = run.get("requirements_sources")
    if reference is None:
        return None
    manifest_path = bound_file(base, reference)
    manifest = json.loads(read_source(base, reference))
    if manifest.get("evaluation_identity") != evaluation_identity(spec):
        raise ValueError("CI source archive belongs to another requirements definition")
    if manifest.get("requirements_digest") != canonical_digest(spec):
        raise ValueError("CI source archive digest mismatch")
    return manifest_path.parent
