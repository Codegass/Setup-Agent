"""Frozen requirements and common aggregation, shared by every agent adapter.

This is a versioned analysis layer; it does not redefine SAG's historical verdict.
All requirements are mandatory. Unknown definitions cannot earn free passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import posixpath
from pathlib import Path

POLICY_VERSION = "ci-requirements-v2"
KINDS = frozenset(
    {
        "compile",
        "package",
        "install",
        "test",
        "documentation",
        "quality_check",
        "native_compile",
        "unclassified",
    }
)
BUILD_KINDS = frozenset({"compile", "package", "install", "native_compile"})
STATUSES = frozenset({"passed", "failed", "not_run", "unavailable"})
RULES = frozenset({"native_goal", "junit", "maven_invoker", "antunit", "artifact", "install", "native_exit", "unclassified"})


def normalize_task(task):
    """Match AcceptanceTask v1 defaults/omissions without a Pydantic dependency."""
    if hasattr(task, "model_dump"):
        task = task.model_dump(mode="json")
    task = json.loads(json.dumps(task, allow_nan=False))
    if not isinstance(task, dict) or set(task) - {"schema_version", "repo", "sha", "steps"}:
        raise ValueError("Invalid frozen task fields")
    task.setdefault("schema_version", 1)
    if (
        type(task["schema_version"]) is not int
        or task["schema_version"] != 1
        or not re.fullmatch(r"[0-9a-f]{40}", str(task.get("sha", "")))
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(task.get("repo", "")))
    ):
        raise ValueError("Invalid frozen task identity")
    steps = task.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise ValueError("Invalid frozen steps")
    ids = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
            "id",
            "runner",
            "argv",
            "cwd",
            "java_major",
            "maven_version",
        }:
            raise ValueError("Invalid frozen step fields")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(step.get("id", ""))) or step.get(
            "runner"
        ) not in {"maven", "gradle", "native", "shell"}:
            raise ValueError("Invalid step identity or runner")
        ids.append(step["id"])
        argv = step.get("argv")
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 256
            or any(not isinstance(a, str) or any(c in a for c in "\0\r\n") for a in argv)
            or not argv[0]
        ):
            raise ValueError("Expected literal argv")
        step.setdefault("cwd", ".")
        cwd = step["cwd"]
        if (
            not isinstance(cwd, str)
            or not cwd
            or posixpath.isabs(cwd)
            or posixpath.normpath(cwd) != cwd
            or cwd == ".."
            or cwd.startswith("../")
        ):
            raise ValueError("Task cwd must remain within checkout")
        step.setdefault("java_major", None)
        java = step["java_major"]
        if java is not None and (
            type(java) is not int
            or not 1 <= java <= 99
            or step["runner"] not in {"maven", "gradle"}
        ):
            raise ValueError("Invalid launcher Java constraint")
        if step.get("maven_version") is None:
            step.pop("maven_version", None)
        elif step["runner"] != "maven" or not re.fullmatch(
            r"\d+\.\d+\.\d+", str(step["maven_version"])
        ):
            raise ValueError("Invalid Maven version constraint")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate step identity")
    return task


def repository_artifact_path(item):
    """Maven layout, independent of any metadata machine's absolute home path."""
    coordinates = item.get("coordinates") or {}
    parts = [coordinates.get(k) for k in ("group_id", "artifact_id", "version")]
    if any(
        not isinstance(p, str) or not p or p in {".", ".."} or "/" in p or "\\" in p or "${" in p
        for p in parts
    ):
        raise ValueError("Unresolved installation coordinates")
    extension, classifier = item.get("extension"), item.get("classifier")
    if (
        not isinstance(extension, str)
        or not re.fullmatch(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", extension)
        or classifier is not None
        and (
            not isinstance(classifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", classifier)
            or classifier in {".", ".."}
        )
    ):
        raise ValueError("Invalid installation extension/classifier")
    group, artifact, version = parts
    path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{'-' + classifier if classifier else ''}.{extension}"
    if (
        posixpath.normpath(path) != path
        or path.startswith("/")
        or item.get("repository_relative_path") != path
    ):
        raise ValueError("Installation path differs from frozen coordinates")
    return path


def canonical_digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    return json.loads(Path(path).read_bytes(), object_pairs_hook=unique, parse_constant=invalid)


def file_digest(path):
    with Path(path).open("rb") as source:
        h = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def bound_file(base, ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise ValueError("Missing evidence file reference")
    path = Path(ref["path"])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Evidence path escapes recorder directory")
    resolved = (Path(base) / path).resolve()
    if not resolved.is_relative_to(Path(base).resolve()) or not resolved.is_file():
        raise ValueError("Missing or escaped evidence file")
    if file_digest(resolved) != ref.get("sha256"):
        raise ValueError("Evidence byte hash mismatch")
    return resolved


def report_directories(row):
    """Explicit checkout-relative JUnit roots, or None for the legacy default."""
    directories = row.get("validation", {}).get("report_directories")
    if directories is None:
        return None
    if not isinstance(directories, list) or not directories:
        raise ValueError("Report directories must be a nonempty list")
    for directory in directories:
        if (
            not isinstance(directory, str)
            or directory in {"", ".", ".."}
            or posixpath.isabs(directory)
            or posixpath.normpath(directory) != directory
            or directory.startswith("../")
            or any(c in directory for c in "\\\0\r\n*?[")
        ):
            raise ValueError("Report directory must be a literal checkout-relative directory")
    if any(
        a == b or a.startswith(b + "/") or b.startswith(a + "/")
        for index, a in enumerate(directories)
        for b in directories[index + 1 :]
    ):
        raise ValueError("Report directories overlap within a requirement")
    return directories


def report_scope_errors(rows, runners=None):
    """Several test obligations cannot share an unpartitioned report pool."""
    groups, producer_scopes, errors = {}, {}, {}
    def distinct_producers(left, right):
        a,b = left.get('validation',{}),right.get('validation',{})
        one,two = a.get('native_bindings',[]),b.get('native_bindings',[])
        return (a.get('producer_scoped_reports') is True and b.get('producer_scoped_reports') is True
                and (runners or {}).get(left['step_id']) == 'maven'
                and len(one)==len(two)==1 and left.get('module')==right.get('module')
                and (one[0]['goal'],one[0]['execution'],one[0]['occurrence'])
                    != (two[0]['goal'],two[0]['execution'],two[0]['occurrence']))
    for row in rows:
        if row.get("validation", {}).get("rule") not in {"junit", "maven_invoker"}:
            continue
        directories = report_directories(row)
        key = (row["step_id"], row.get("module"), row.get("subtype", "unit"))
        groups.setdefault(key, []).append((row, directories))
        effective = directories
        module_path = row.get("scope", {}).get("module_path")
        if (
            effective is None
            and (runners or {}).get(row["step_id"]) == "maven"
            and isinstance(module_path, str)
        ):
            effective = report_directories(
                {
                    "validation": {
                        "report_directories": [
                            posixpath.normpath(
                                posixpath.join(
                                    module_path,
                                    "target",
                                    (
                                        "failsafe-reports"
                                        if row.get("subtype") == "integration"
                                        else "surefire-reports"
                                    ),
                                )
                            )
                        ]
                    }
                }
            )
        if effective is not None:
            producer_scopes.setdefault(row["step_id"], []).append((row, effective))
        if directories is None and (
            (runners or {}).get(row["step_id"]) == "gradle"
            or row.get("validation", {}).get("tasks")
        ):
            errors[row["id"]] = "Gradle test requirements need explicit report directories"
    for group in groups.values():
        if len(group) < 2:
            continue
        if any(directories is None for _, directories in group):
            errors.update(
                (row["id"], "Repeated test requirements need explicit report directories")
                for row, _ in group
            )
    # Files have one producer within an invocation. Relabelling the same XML as
    # another module or test kind cannot make it satisfy another obligation.
    for group in producer_scopes.values():
        for index, (left, roots) in enumerate(group):
            for right, others in group[index + 1 :]:
                if any(
                    a == b or a.startswith(b + "/") or b.startswith(a + "/")
                    for a in roots
                    for b in others
                ):
                    if distinct_producers(left,right):
                        continue
                    errors[left["id"]] = errors[right["id"]] = "Test report directories overlap"
    return errors


def requires_native_image(spec, step_id):
    return any(
        step.get("step_id") == step_id and step.get("requires_native_image") is True
        for precondition in spec.get("preconditions", [])
        if precondition.get("id") == "runtime_conformance"
        for step in precondition.get("steps", [])
    )


def validate_requirements(spec, task=None):
    if task is not None:
        task = normalize_task(task)
    if (
        not isinstance(spec, dict)
        or spec.get("schema_version") != 2
        or spec.get("policy_version") != POLICY_VERSION
    ):
        raise ValueError("Unsupported requirements protocol")
    if not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("task_sha256", ""))):
        raise ValueError("Missing canonical task digest")
    validate_ci_count_metadata(spec)
    rows = spec.get("requirements")
    if not isinstance(rows, list) or not rows:
        raise ValueError("A task must retain all mandatory requirements")
    ids = [r.get("id") for r in rows]
    if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("Invalid or duplicate requirement IDs")
    pre = spec.get("preconditions", [])
    if {r.get("id") for r in pre} != {"worktree_integrity", "runtime_conformance"} or len(pre) != 2:
        raise ValueError("Both task preconditions are mandatory")
    runtime_steps = next(r for r in pre if r["id"] == "runtime_conformance").get("steps", [])
    runtime_ids = [step.get("step_id") for step in runtime_steps]
    if len(set(runtime_ids)) != len(runtime_ids):
        raise ValueError("Duplicate runtime precondition step")
    for step in runtime_steps:
        if "requires_native_image" in step and type(step["requires_native_image"]) is not bool:
            raise ValueError("Native-image requirement must be a boolean")
        if (
            step.get("requires_native_image") is True
            and task is not None
            and not any(
                actual["id"] == step.get("step_id") and actual["runner"] in {"maven", "gradle"}
                for actual in task["steps"]
            )
        ):
            raise ValueError("Native-image capability must bind to a JVM launcher step")
    for declared in spec.get("steps", []):
        if "jvm_environment_review" in declared:
            from .jvm_inputs import validate_environment_review
            actual = next((s for s in (task or {}).get("steps", []) if s["id"] == declared.get("step_id")), None)
            if task is not None and actual is None:
                raise ValueError("JVM environment review requires its frozen task step")
            reviewed = validate_environment_review(declared["jvm_environment_review"],
                task or {"sha": spec["commit"], "repo": spec["repository"]},
                actual or {"id": declared["step_id"], "runner": declared["runner"], "argv": declared["argv"]})
            if any(declared.get("environment", {}).get(name) != item["value"]
                   for name, item in reviewed["values"].items()):
                raise ValueError("JVM environment review differs from the declared execution inputs")
        if "jvm_config_review" in declared:
            from .jvm_inputs import validate_review
            actual = next((s for s in (task or {}).get("steps", []) if s["id"] == declared.get("step_id")), None)
            if task is not None and (actual is None or actual["runner"] != "maven" or actual["argv"][0] != "mvn"):
                raise ValueError("JVM configuration review requires its direct Maven step")
            validate_review(declared["jvm_config_review"], task["sha"] if task else spec.get("commit"))
        if "launcher_review" in declared:
            from .wrapper_review import validate_launcher_review
            actual = next((s for s in (task or {}).get("steps", []) if s["id"] == declared.get("step_id")), None)
            if task is not None and actual is None:
                raise ValueError("Launcher review requires its frozen task step")
            # Inventory-only readers can validate schema; evaluation always
            # repeats this check with the actual frozen task and its digest.
            validate_launcher_review(declared["launcher_review"],
                                     task["sha"] if task else spec.get("commit", declared["launcher_review"].get("source_commit")),
                                     actual or {"runner": "maven", "argv": ["./mvnw"], "cwd": "."})
    for row in rows:
        if row.get("kind") not in KINDS or not row.get("step_id"):
            raise ValueError("Unsupported requirement kind or missing step")
        if "optional" in row or "required" in row:
            raise ValueError("Requirements cannot be weakened by optional flags")
        if not set(row.get("depends_on", [])) <= set(ids) or row["id"] in row.get("depends_on", []):
            raise ValueError("Unknown or self prerequisite")
        if row.get("validation", {}).get("rule", "unclassified") not in RULES:
            raise ValueError("Unsupported evidence rule")
        validation = row.get("validation", {})
        if 'nested_gradle_task' in validation:
            if (not isinstance(validation['nested_gradle_task'], str)
                    or not re.fullmatch(r'(?::[A-Za-z0-9_.-]+)+', validation['nested_gradle_task'])
                    or validation.get('goals') != ['exec:exec']
                    or len(validation.get('native_bindings', [])) != 1
                    or validation.get('rule') not in {'native_goal', 'junit', 'artifact'}
                    or task is not None and not any(
                        s['id'] == row['step_id'] and s['runner'] == 'maven' for s in task['steps'])):
                raise ValueError('Nested Gradle tasks require one exact Maven exec occurrence')
        from .installed_evidence import validate_source as validate_install_source
        for artifact in row.get("expectations", {}).get("artifacts", []):
            if artifact.get("install_source_path") is not None:
                if validation.get("rule") != "install":
                    raise ValueError("Installed source proof belongs only to an install obligation")
                validate_install_source(artifact)
        from .native_tests import validate as validate_native_test
        validate_native_test(row)
        bindings = validation.get("native_bindings")
        if validation.get('producer_scoped_reports') is not None:
            if (validation['producer_scoped_reports'] is not True or validation.get('rule')!='junit'
                    or not bindings or len(bindings)!=1 or not report_directories(row)
                    or bindings[0].get('goal') not in {'surefire:test','failsafe:integration-test'}):
                raise ValueError('Producer-scoped JUnit needs one frozen Maven test occurrence and explicit roots')
        if bindings is not None:
            if not isinstance(bindings, list) or not bindings:
                raise ValueError("Native bindings must be an explicit nonempty occurrence list")
            keys = []
            for binding in bindings:
                if (not isinstance(binding, dict)
                        or not isinstance(binding.get("goal"), str) or ":" not in binding["goal"]
                        or not isinstance(binding.get("execution"), str) or not binding["execution"]
                        or any(type(binding.get(k)) is not int or binding[k] < 0
                               for k in ("occurrence", "position"))):
                    raise ValueError("Invalid native occurrence binding")
                keys.append((binding["goal"], binding["execution"], binding["occurrence"]))
            if len(set(keys)) != len(keys):
                raise ValueError("Duplicate native occurrence binding")
            positions = [binding["position"] for binding in bindings]
            if positions != sorted(set(positions)):
                raise ValueError("Native occurrence positions must preserve unique frozen order")
            if (validation.get("goals") != list(dict.fromkeys(b["goal"] for b in bindings))
                    or validation.get("position") != min(b["position"] for b in bindings)):
                raise ValueError("Native binding summary differs from its occurrence list")
            if validation.get("rule") == "junit" and len(bindings) != 1:
                raise ValueError("One JUnit pool cannot certify multiple executed test occurrences")
        reused = validation.get("reused_native_bindings", [])
        if reused:
            if validation.get("rule") != "junit" or not bindings or len(bindings) != 1:
                raise ValueError("Test configuration reuse needs one original JUnit occurrence")
            first = bindings[0]
            seen = set()
            for binding in reused:
                if validation.get('reuse_protocol') == 'surefire-3.5.3-context':
                    key=(binding.get('goal'),binding.get('execution'),binding.get('occurrence'))
                    if (binding.get('goal') not in {'surefire:test','failsafe:integration-test'}
                            or binding.get('goal')!=first['goal'] or not isinstance(binding.get('execution'),str)
                            or type(binding.get('occurrence')) is not int or binding['occurrence']<0
                            or type(binding.get('position')) is not int or binding['position']<=first['position']
                            or key==(first['goal'],first['execution'],first['occurrence']) or key in seen):
                        raise ValueError('Invalid native context test reuse')
                    seen.add(key)
                    continue
                if (binding.get("goal") != "surefire:test" or first["goal"] != "surefire:test"
                        or binding.get("execution") != first["execution"]
                        or type(binding.get("occurrence")) is not int
                        or binding["occurrence"] <= first["occurrence"]
                        or type(binding.get("position")) is not int
                        or binding["position"] <= first["position"]
                        or binding["occurrence"] in seen):
                    raise ValueError("Invalid same-configuration test reuse")
                seen.add(binding["occurrence"])
    # Dependency edges explain causality; they never schedule commands.
    visiting, visited = set(), set()
    by_id = {r["id"]: r for r in rows}

    def visit(key):
        if key in visiting:
            raise ValueError("Cyclic requirement dependencies")
        if key in visited:
            return
        visiting.add(key)
        for parent in by_id[key].get("depends_on", []):
            visit(parent)
        visiting.remove(key)
        visited.add(key)

    for key in ids:
        visit(key)
    completeness = spec.get("annotation_completeness", {})
    if completeness.get("status") not in {"complete", "review_required"}:
        raise ValueError("Annotation completeness must be explicit")
    scope_errors = report_scope_errors(
        rows, {step["id"]: step["runner"] for step in task["steps"]} if task else None
    )
    if completeness["status"] == "complete":
        if completeness.get("gaps"):
            raise ValueError("Complete annotations cannot retain unresolved gaps")
        if scope_errors:
            raise ValueError(next(iter(scope_errors.values())))
        for row in rows:
            rule = row.get("validation", {}).get("rule")
            if row["kind"] == "unclassified" or row.get("scope", {}).get("status") not in {
                "declared",
                "resolved",
            }:
                raise ValueError("Complete annotations need resolved mandatory scope")
            allowed = {
                "compile": {"native_goal"},
                "package": {"artifact"},
                "install": {"install"},
                "native_compile": {"artifact"},
                "test": {"junit", "maven_invoker", "antunit", "native_exit"},
                "documentation": {"native_goal"},
                "quality_check": {"native_goal"},
            }
            if rule not in allowed[row["kind"]]:
                raise ValueError("Evidence rule cannot weaken the requirement kind")
            if rule in {"artifact", "install"} and not row.get("expectations", {}).get("artifacts"):
                raise ValueError("Artifact requirements need declared roles and paths")
            if rule == "install":
                for item in row["expectations"]["artifacts"]:
                    repository_artifact_path(item)
    if task is not None:
        task = normalize_task(task)
        if canonical_digest(task) != spec["task_sha256"]:
            raise ValueError("Requirements refer to another frozen task")
        if (
            spec.get("commit", task["sha"]) != task["sha"]
            or spec.get("repository", task["repo"]) != task["repo"]
        ):
            raise ValueError("Requirements subject differs from task")
        steps = {s["id"] for s in task["steps"]}
        if {r["step_id"] for r in rows} != steps:
            raise ValueError("Requirements must cover every frozen command")
    if task is not None and spec.get("compilation_reuse"):
        from .compilation_evidence import validate_plans
        validate_plans(spec, task)
    return spec


def load_requirements(path, task=None):
    return validate_requirements(load_json(path), task)


def requirements_evidence_root(path):
    """Exports keep definitions in requirements/ and sources at bundle root.

    A standalone per-attempt requirements.json keeps sources beside itself.
    No search outside these documented roots is performed.
    """
    parent = Path(path).resolve().parent
    return parent.parent if parent.name == "requirements" else parent


def validate_ci_count_metadata(spec, *, base=None, required=False):
    from .ci_count_semantics import validate_count_semantics

    alignment = spec.get("ci_alignment", {})
    value = alignment.get("test_count_semantics")
    if value is None and not required:
        return None  # Historical definitions are readable, not formal launch inputs.
    validate_count_semantics(value, base=base)
    if value["status"] == "available":
        if any(value.get(k) != alignment.get(k) for k in ("selected_url", "selected_cell")):
            raise ValueError("CI test count semantics describe another selected reference")
        anchor = alignment.get("archived_ci_index", {})
        if any(anchor.get(k) != value["sources"]["ci_index"].get(k) for k in ("sha256", "bytes")):
            raise ValueError("CI test count semantics differ from the frozen reference index")
    return value


def copy_ci_count_sources(requirements_path, destination):
    """Preserve raw CI and module provenance when a campaign relocates a definition."""
    from .ci_sources import archive_sources

    spec = load_json(requirements_path)
    base = requirements_evidence_root(requirements_path)
    validate_ci_count_metadata(spec, base=base, required=True)
    archive_sources(spec, base, destination)


def evaluation_identity(spec, requirements_file=None):
    identity = {
        "protocol": "requirements-v2",
        "task_sha256": spec["task_sha256"],
        "requirements_sha256": canonical_digest(spec),
        "policy_version": POLICY_VERSION,
    }
    if requirements_file is not None:
        identity["requirements_file_sha256"] = file_digest(requirements_file)
    return identity


def task_status(statuses):
    values = list(statuses)
    if any(x in {"failed", "not_run", "incomplete"} for x in values):
        return "incomplete"
    if values and all(x in {"passed", "complete"} for x in values):
        return "complete"
    return "unavailable"


def summarize(spec, results, preconditions, commands, *, identity_valid=True):
    by_id = {r["id"]: r for r in results}
    if len(by_id) != len(results) or set(by_id) != {r["id"] for r in spec["requirements"]}:
        raise ValueError("Results must preserve the entire frozen denominator")
    if any(r.get("status") not in STATUSES for r in results):
        raise ValueError("Invalid requirement status")
    status = task_status(
        [r["status"] for r in results]
        + [r["status"] for r in preconditions]
        + [r["status"] for r in commands]
    )
    if not identity_valid:
        status = "unavailable"
    elif status == "complete" and spec["annotation_completeness"]["status"] != "complete":
        status = "unavailable"
    groups = {}
    for name, kinds in [
        ("build", BUILD_KINDS),
        ("test", {"test"}),
        ("documentation", {"documentation"}),
        ("quality_check", {"quality_check"}),
    ]:
        subset = [by_id[r["id"]] for r in spec["requirements"] if r["kind"] in kinds]
        groups[name] = (
            task_status([r["status"] for r in subset] + [r["status"] for r in preconditions])
            if subset and identity_valid
            else "not_applicable" if not subset else "unavailable"
        )
        # A known lower bound does not establish the entire group's scope.
        # Preserve individual passes/failures, but neither certify a whole
        # stage nor declare it absent while requirements are still incomplete.
        if spec["annotation_completeness"]["status"] != "complete" and groups[name] in {
            "complete",
            "not_applicable",
        }:
            groups[name] = "unavailable"
    blockers = []
    for r in spec["requirements"]:
        if by_id[r["id"]]["status"] == "failed" and not any(
            by_id[p]["status"] in {"failed", "not_run"} for p in r.get("depends_on", [])
        ):
            blockers.append(r["id"])
    return {
        "status": status,
        "groups": groups,
        "known_blockers": blockers,
        "requirements": results,
        "preconditions": preconditions,
        "commands": commands,
        "annotation_completeness": spec["annotation_completeness"],
    }


def aggregate(planned, scores):
    """One arm/replicate; group denominator is planned tasks, never returned successes."""
    by_id = {x["project_id"]: x for x in scores}
    if len(by_id) != len(scores) or not set(by_id) <= set(planned):
        raise ValueError("Duplicate/unplanned project results")
    if len({s.get("agent") for s in scores}) > 1:
        raise ValueError("Aggregate one agent arm at a time")
    if len({(s.get("execution_policy"), s.get("execution_origin"), s.get("required_execution_origin"))
            for s in scores}) > 1:
        raise ValueError("Cannot aggregate different execution boundaries")
    for project, score in by_id.items():
        if score.get("evaluation_identity") != evaluation_identity(planned[project]):
            raise ValueError("Cannot aggregate different requirement versions")

    def count(ids, group=None):
        states = [
            (
                by_id.get(i, {}).get("groups", {}).get(group, "unavailable")
                if group
                else by_id.get(i, {}).get("status", "unavailable")
            )
            for i in ids
        ]
        counts = {state: states.count(state) for state in ("complete", "incomplete", "unavailable")}
        return {
            **counts,
            "denominator": len(ids),
            "rate": counts["complete"] / len(ids) if ids else None,
        }

    def start_state(project, kinds):
        rows = {r["id"]: r for r in by_id.get(project, {}).get("requirements", [])}
        values = [rows.get(r["id"], {}).get("started")
                  for r in planned[project]["requirements"] if r["kind"] in kinds]
        # One project vote: any real member starts the group; all members must
        # explicitly not start to establish false. Missing evidence stays null.
        return True if any(v is True for v in values) else False if values and all(v is False for v in values) else None

    def start_counts(ids, kinds):
        values = [start_state(project, kinds) for project in ids]
        return {"started": sum(v is True for v in values),
                "not_started": sum(v is False for v in values),
                "started_unknown": sum(v is None for v in values)}

    groups = {}
    for kind in sorted(KINDS):
        ids = [
            k for k, spec in planned.items() if any(r["kind"] == kind for r in spec["requirements"])
        ]
        if ids:
            # Complete task within overlapping requirement category; entry is
            # reported separately from completion or eligibility.
            groups[kind] = {**count(ids), **start_counts(ids, {kind})}
    stage_groups = {}
    for name, kinds in [
        ("build", BUILD_KINDS),
        ("test", {"test"}),
        ("documentation", {"documentation"}),
        ("quality_check", {"quality_check"}),
    ]:
        ids = [
            k
            for k, spec in planned.items()
            if any(r["kind"] in kinds for r in spec["requirements"])
        ]
        stage_groups[name] = {**count(ids, name), **start_counts(ids, kinds)}
    # Each project gets one vote per overlapping category, not one per module.
    conditional = {}
    for kind in sorted(KINDS):
        c = {
            "planned": 0,
            "reached": 0,
            "passed": 0,
            "failed": 0,
            "not_run": 0,
            "unavailable": 0,
            "prerequisites_unresolved": 0,
            "eligible_passed": 0,
            "prerequisites_satisfied": 0,
            "started": 0,
            "not_started": 0,
            "started_unknown": 0,
        }
        for project, spec in planned.items():
            results = {r["id"]: r for r in by_id.get(project, {}).get("requirements", [])}
            subset = [r for r in spec["requirements"] if r["kind"] == kind]
            if not subset:
                continue
            c["planned"] += 1
            started = start_state(project, {kind})
            c["started" if started is True else "not_started" if started is False else "started_unknown"] += 1
            states = [results.get(r["id"], {}).get("status", "unavailable") for r in subset]
            state = (
                "failed"
                if "failed" in states
                else (
                    "not_run"
                    if "not_run" in states
                    else "passed" if all(s == "passed" for s in states) else "unavailable"
                )
            )
            c[state] += 1
            # Internal edges belong to the group's own outcomes. Only external
            # prerequisites determine entry into this conditional comparison.
            group_ids = {r["id"] for r in subset}
            ready = all(
                r.get("dependencies_complete") is True
                and all(
                    results.get(p, {}).get("status") == "passed"
                    for p in r.get("depends_on", [])
                    if p not in group_ids
                )
                for r in subset
            )
            c["prerequisites_satisfied"] += ready
            if ready and state in {"passed", "failed"}:
                c["reached"] += 1
                c["eligible_passed"] += state == "passed"
            elif not ready:
                c["prerequisites_unresolved"] += 1
        if c["planned"]:
            c["conditional_pass_rate"] = (
                c["eligible_passed"] / c["reached"] if c["reached"] else None
            )
            conditional[kind] = c
    costs = {}
    for field in ("total_tokens", "unattended_seconds"):
        values = [s[field] for s in scores if s.get(field) is not None]
        costs[field] = {
            "known_sum": sum(values),
            "known_projects": len(values),
            "unavailable_or_missing": len(planned) - len(values),
            "complete_cohort_sum": sum(values) if len(values) == len(planned) else None,
        }
    return {
        "metric_version": POLICY_VERSION,
        "planned_projects": len(planned),
        "missing_projects": sorted(set(planned) - set(by_id)),
        "task_success": count(planned),
        "task_success_by_requirement_group": groups,
        "requirement_stage_success": stage_groups,
        "conditional_requirements": conditional,
        "costs_including_failures": costs,
    }


def paired_comparison(planned, left, right):
    """Expose the mutually observable subset; never replace all-task rates with it."""
    left_summary, right_summary = aggregate(planned, left), aggregate(planned, right)
    by_side = [{s["project_id"]: s for s in side} for side in (left, right)]
    groups = {}
    for kind in sorted(KINDS):
        members, pairs = [], []
        for project, spec in planned.items():
            subset = [r for r in spec["requirements"] if r["kind"] == kind]
            if not subset:
                continue
            members.append(project)
            states = []
            group_ids = {r["id"] for r in subset}
            for side in by_side:
                rows = {r["id"]: r for r in side.get(project, {}).get("requirements", [])}
                ready = all(
                    r.get("dependencies_complete") is True
                    and all(
                        rows.get(p, {}).get("status") == "passed"
                        for p in r.get("depends_on", [])
                        if p not in group_ids
                    )
                    for r in subset
                )
                values = [rows.get(r["id"], {}).get("status", "unavailable") for r in subset]
                states.append(
                    "failed"
                    if ready and "failed" in values
                    else "passed" if ready and all(s == "passed" for s in values) else None
                )
            if all(states):
                pairs.append({"project_id": project, "left": states[0], "right": states[1]})
        if members:
            groups[kind] = {
                "planned": len(members),
                "both_observable": len(pairs),
                "excluded_or_unavailable": len(members) - len(pairs),
                "pairs": pairs,
            }
    return {
        "left_all_tasks": left_summary,
        "right_all_tasks": right_summary,
        "paired_conditional_groups": groups,
        "interpretation": "Descriptive selected subset; all-task denominators above remain primary.",
    }
