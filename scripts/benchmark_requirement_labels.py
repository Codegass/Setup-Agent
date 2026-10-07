"""Audit archived Java task labels without executing repository code.

CI-job observations are candidate labels, never an executable acceptance manifest.
Only a fully matched existing requirements-v2 task can transfer its readiness.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import shlex
import xml.etree.ElementTree as ET
import zipfile


KINDS = ("compile", "package", "install", "test", "documentation", "quality_check", "native_compile")
STAGES = ("validate", "initialize", "generate-sources", "process-sources", "generate-resources", "process-resources", "compile", "process-classes", "generate-test-sources", "process-test-sources", "generate-test-resources", "process-test-resources", "test-compile", "process-test-classes", "test", "prepare-package", "package", "pre-integration-test", "integration-test", "post-integration-test", "verify", "install", "deploy")
MAVEN_VALUES = {"-f", "--file", "-pl", "--projects", "-P", "--activate-profiles", "-D", "--define", "-s", "--settings", "-gs", "--global-settings", "-t", "--toolchains", "-gt", "--global-toolchains", "-T", "--threads", "-rf", "--resume-from", "-l", "--log-file"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STAMP = re.compile(r"^(?:\d{2}:\d{2}:\d{2}\s+)?\[?\d{4}-\d{2}-\d{2}T[^\s\]]+\]?\s+")
BANNER = re.compile(r"^\[INFO\]\s+---\s+([^\s]+)\s+\(([^)]*)\)\s+@\s+(.+?)\s+---\s*$")
GRADLE = re.compile(r"^> Task (:\S+?)(?:\s+(UP-TO-DATE|FROM-CACHE|NO-SOURCE|SKIPPED|FAILED))?\s*$")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode())


@lru_cache(maxsize=None)
def file_ref(path):
    path = Path(path).resolve()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


@lru_cache(maxsize=None)
def read_json(path):
    return json.loads(Path(path).read_text())


def local(base, value):
    path = (base / value).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("Archive reference escapes the declared dataset")
    return path


def bound(base, ref):
    path = local(base, ref.get("file") or ref.get("path") or ref.get("archive"))
    actual = file_ref(str(path))
    expected = ref.get("sha256") or ref.get("archive_sha256")
    if expected is not None and actual["sha256"] != expected:
        raise ValueError("Source hash differs: " + str(path))
    if ref.get("bytes") is not None and ref["bytes"] != actual["bytes"]:
        raise ValueError("Source length differs: " + str(path))
    return path, actual


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def research_row(base, task):
    source = task["review_source"]
    path = local(base, source["file"])
    document = read_json(str(path))
    rows = document.get("rows", document.get("decisions", []))
    row = rows[source["row_index"]]
    if row["repo"] != task["repo"]:
        raise ValueError("Review row repository mismatch")
    revision = row.get("sha") or row.get("ci_sha") or row.get("sourceSHA")
    if revision is not None and revision != task["sha"]:
        raise ValueError("Review row revision mismatch")
    return row, {**file_ref(str(path)), "json_pointer": "/" + ("rows" if "rows" in document else "decisions") + "/" + str(source["row_index"])}


def selected_logs(base, task, row):
    """Use an explicit selected job/member or selected console, never all ZIPs."""
    selected = []
    cell_path = base / "actions-cell-evidence" / (task["repo"].replace("/", "__") + ".json")
    if cell_path.is_file():
        for run in read_json(str(cell_path)).get("runs", []):
            for cell in run.get("cells", []):
                if cell.get("job_url") == task["official_ci_url"]:
                    selection = cell.get("log_selection", {})
                    if selection.get("job_binding") in {"exact_job_name", "direct_job_id"}:
                        for member in selection.get("members", []):
                            selected.append((run["log_archive"], member, "selected_cell_metadata", file_ref(str(cell_path))))
    if not selected:
        evidence = row.get("evidence")
        if isinstance(evidence, dict) and evidence.get("selected_console"):
            selected.append((evidence["selected_console"], None, "explicit_selected_console", None))
        ci_log = (row.get("ci") or {}).get("log", {})
        if ci_log.get("zip_member") and (ci_log.get("archive") or ci_log.get("file")):
            selected.append((ci_log, ci_log["zip_member"], "explicit_ci_log", None))
        elif ci_log.get("extracted_file"):
            selected.append(({"file": ci_log["extracted_file"], "sha256": ci_log.get("extracted_sha256")}, None, "explicit_selected_extracted_log", None))
        elif isinstance(ci_log.get("file"), str) and ci_log["file"].endswith((".log", ".txt")):
            selected.append((ci_log, None, "explicit_selected_ci_log", None))
        raw = row.get("raw_evidence")
        if not selected and isinstance(raw, dict) and raw.get("selected_complete_job"):
            selected.append((raw["selected_complete_job"], None, "explicit_selected_complete_job", None))
        if not selected and isinstance(raw, dict) and raw.get("single_cell_derived_plaintext"):
            selected.append((raw["single_cell_derived_plaintext"], None, "explicit_selected_cell_derived_log", raw.get("single_cell_complete_html")))
        if not selected and isinstance(raw, dict) and raw.get("log") and raw.get("member"):
            selected.append((raw["log"], raw["member"], "explicit_selected_raw_member", None))
        if not selected and isinstance(raw, dict) and raw.get("console") and raw.get("console_sha256"):
            selected.append(({"file": raw["console"], "sha256": raw["console_sha256"]}, None, "explicit_selected_raw_console", None))
        if not selected and row.get("log_archive") and row.get("log_members"):
            for member in row["log_members"]:
                selected.append((row["log_archive"], member, "explicit_review_log_members", None))
        refs = row.get("evidence_files")
        if not selected and isinstance(refs, dict) and refs.get("logs_archive") and refs.get("selected_member"):
            selected.append(({"file": refs["logs_archive"], "sha256": refs.get("logs_sha256")}, refs["selected_member"], "explicit_review_selected_member", None))
    if not selected:
        # Bound review excerpts can identify a single complete selected-job log.
        # Multiple candidates remain unknown instead of choosing by recency/name.
        candidates = {}
        selected_urls = {j.get("url") or j.get("job_url") for j in (row.get("ci") or {}).get("selected_jobs", [])}
        allowed_urls = {task["official_ci_url"], task["official_ci_url"].rstrip("/") + "/consoleText", *selected_urls}
        for item in dictionaries(row.get("evidence", [])):
            f = item.get("file")
            if isinstance(f, str) and f.endswith((".log", ".txt")) and item.get("sha256") and item.get("url") in allowed_urls:
                candidates[f] = item
        if selected_urls and {c.get("url") for c in candidates.values()} == selected_urls:
            selected.extend((item, None, "explicit_selected_job_closure", None) for item in candidates.values())
        elif len(candidates) == 1:
            selected.append((next(iter(candidates.values())), None, "unique_ci_url_bound_review_log", None))
        if not selected:
            candidates = {item["file"]: item for item in dictionaries(row.get("evidence_files", []))
                          if isinstance(item.get("file"), str) and item["file"].endswith(".log") and item.get("sha256")}
            if len(candidates) == 1:
                selected.append((next(iter(candidates.values())), None, "unique_archived_log_in_selected_task_review", None))
    logs, errors = [], []
    for ref, member, basis, selection_ref in selected:
        try:
            if isinstance(ref, str):
                ref = {"file": ref}
            path, source = bound(base, ref)
            if selection_ref and basis == "explicit_selected_cell_derived_log":
                _, selection_ref = bound(base, selection_ref)
            if member is None:
                raw = path.read_bytes()
            else:
                with zipfile.ZipFile(path) as archive:
                    if archive.namelist().count(member) != 1:
                        raise ValueError("Missing or duplicate selected ZIP member")
                    raw = archive.read(member)
            logs.append({"text": raw.decode("utf-8", errors="replace"), "ref": {**source, "zip_member": member,
                         "content_sha256": sha(raw), "content_bytes": len(raw), "selection_basis": basis,
                         "selection_ref": selection_ref, "ci_url": task["official_ci_url"], "origin_url": ref.get("url")}})
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            errors.append(str(exc))
    return logs, errors


def command_rows(contract):
    """Retain declared order, source field and verbatim command; no shell eval."""
    rows = []
    keys = {"command", "commands", "steps", "actual_commands", "official_commands", "command_groups", "original_ci_command_lines", "prerequisite_commands", "preparation_commands", "effective_build_test_command", "shell_script", "consumer_shell_script", "commands_by_job"}
    def collect(value, pointer):
        if isinstance(value, str):
            rows.append({"text": value, "source_field": pointer})
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, pointer + "/" + str(index))
        elif isinstance(value, dict):
            if isinstance(value.get("command") or value.get("text"), str):
                rows.append({"text": value.get("command") or value["text"], "source_field": pointer,
                             **{k: value[k] for k in ("cwd", "jdk", "purpose", "role", "line", "zip_member") if k in value}})
            else:
                for key, child in value.items():
                    if key in keys or key in {"expanded_maven_command", "expanded_command"} or isinstance(child, (list, dict)):
                        collect(child, pointer + "/" + key)
    for key, value in contract.items():
        if key in keys:
            collect(value, "/environment_and_commands/" + key)
    return rows


def command_goals(command, tool):
    try:
        tokens = shlex.split(command)
    except ValueError:
        return {"goals": [], "options": [], "unresolved_shell": True, "launcher": None}
    launch = next((i for i, t in enumerate(tokens) if re.fullmatch(r"(?:\S*/)?(?:mvn|mvnw|gradle|gradlew|gradlewStrict)", t)), None)
    if launch is None:
        return {"goals": [], "options": [], "unresolved_shell": True, "launcher": None}
    goals, options, exclusions = [], [], set()
    expect, exclude = False, False
    shell = any(t in {"&&", "||", ";", "|", "&"} or "$" in t or "`" in t for t in tokens[launch:])
    for token in tokens[launch + 1:]:
        if token in {"&&", "||", ";", "|", "&"}:
            break
        if expect:
            options.append(token)
            if exclude:
                exclusions.add(token)
            expect = exclude = False
        elif token.startswith("-"):
            options.append(token)
            expect = token in (MAVEN_VALUES if tool == "maven" else {"-x", "--exclude-task", "-p", "--project-dir", "-b", "--build-file", "-I", "--init-script", "--max-workers"})
            exclude = token in {"-x", "--exclude-task"}
            if token.startswith("--exclude-task="):
                exclusions.add(token.partition("=")[2])
        else:
            goals.append(token)
    goals = [g for g in goals if g not in exclusions and g.rsplit(":", 1)[-1] not in exclusions]
    return {"launcher": tokens[launch], "goals": goals, "options": options, "excluded_tasks": sorted(exclusions), "unresolved_shell": shell or expect}


def goal_label(goal, tool):
    if tool == "gradle":
        name = goal.rsplit(":", 1)[-1]
        if name in {"compileJava", "compileTestJava"}:
            return "compile", "test" if name == "compileTestJava" else "production"
        if name in {"jar", "testJar", "shadowJar", "war", "bootJar", "bootWar", "distZip", "distTar"}:
            return "package", name
        if name in {"test", "unitTest", "integrationTest", "integration-test"}:
            # Names alone do not establish a custom Gradle Test task's pool.
            return "test", "unknown"
        if name in {"javadoc", "javadocJar", "asciidoctor", "antora"}:
            return "documentation", name
        if name in {"checkstyleMain", "checkstyleTest", "spotbugsMain", "spotbugsTest", "pmdMain", "pmdTest", "jacocoTestCoverageVerification"}:
            return "quality_check", name
        if name in {"nativeCompile", "nativeImage"}:
            return "native_compile", name
        if name == "nativeTest":
            return "test", "native"
        if name == "publishToMavenLocal":
            return "install", "maven_local"
        return None
    parts = goal.split(":")
    if len(parts) not in {2, 3, 4}:
        return None
    plugin, name = parts[-3] if len(parts) == 4 else parts[0], parts[-1]
    plugin = re.sub(r"^maven-|(?:-maven)?-plugin$", "", plugin)
    if plugin == "compiler" and name in {"compile", "testCompile"}:
        return "compile", "test" if name == "testCompile" else "production"
    if plugin == "surefire" and name == "test":
        return "test", "unit"
    if plugin == "failsafe" and name in {"integration-test", "verify"}:
        return "test", "integration"
    if plugin in {"native", "native-image"} and name in {"compile", "compile-no-fork", "native-image"}:
        return "native_compile", name
    if plugin in {"native", "native-image"} and name == "test":
        return "test", "native"
    if plugin == "javadoc" and name not in {"help", "test-fix", "fix"}:
        return "documentation", name
    if plugin in {"site", "asciidoctor", "antora"} and name in {"site", "generate", "process-asciidoc", "run"}:
        return "documentation", plugin + ":" + name
    if plugin == "install" and name == "install":
        return "install", "maven_local"
    if (plugin, name) in {("jar", "jar"), ("jar", "test-jar"), ("war", "war"), ("bundle", "bundle"), ("shade", "shade"), ("assembly", "single"), ("source", "jar"), ("source", "jar-no-fork"), ("source", "test-jar"), ("source", "test-jar-no-fork"), ("cyclonedx", "makeAggregateBom"), ("cyclonedx", "makeBom"), ("spdx", "createSPDX") }:
        return "package", plugin + ":" + name
    quality = {"check", "cpd-check", "cmp", "enforce", "check-buildplan", "validate"}
    if plugin in {"apache-rat", "rat", "checkstyle", "spotbugs", "pmd", "japicmp", "enforcer", "jacoco", "cobertura", "animal-sniffer", "revapi", "artifact", "license", "impsort", "sortpom", "spotless"} and name in quality:
        return "quality_check", plugin + ":" + name
    return None


def native_events(text):
    """Invocation labels only: no success inference, no lifecycle expansion."""
    events, depth = [], 0
    lines = [STAMP.sub("", ANSI.sub("", line)) for line in text.splitlines()]
    for i, line in enumerate(lines):
        if re.match(r"^\[INFO\]\s+>>>", line):
            depth += 1
        elif re.match(r"^\[INFO\]\s+<<<", line):
            depth = max(0, depth - 1)
        match = BANNER.match(line)
        gradle = GRADLE.match(line)
        if match:
            events.append({"goal": match[1], "execution": match[2], "module": match[3], "line": i + 1, "tool": "maven", "nested": depth > 0})
        elif gradle:
            events.append({"goal": gradle[1], "module": gradle[1].rsplit(":", 1)[0] or ":", "line": i + 1, "tool": "gradle", "nested": False, "state": gradle[2] or "invoked"})
    for i, event in enumerate(events):
        end = events[i + 1]["line"] - 1 if i + 1 < len(events) else len(lines)
        segment = "\n".join(lines[event["line"]:end])
        if event["tool"] == "maven":
            event["state"] = "same_configuration_reuse" if "Skipping execution of surefire because it has already been run" in segment else "skipped" if re.search(r"^(?:\[INFO\]|\[WARNING\])\s+(?:Skipping\b|Tests are skipped\b)", segment, re.M) else "no_sources_or_tests" if re.search(r"\b(?:No sources to compile|No tests to run)\b", segment) else "invoked"
        event["classification"] = goal_label(event["goal"], event["tool"])
    return events


def candidate_labels(goals, tool):
    labels = set()
    for goal in goals:
        known = goal_label(goal, tool)
        if known:
            labels.add(known)
        elif tool == "maven" and goal in STAGES:
            index = STAGES.index(goal)
            if index >= STAGES.index("compile"):
                labels.add(("compile", "production"))
            if index >= STAGES.index("test"):
                labels.add(("test", "unknown"))
            if index >= STAGES.index("package"):
                labels.add(("package", "unknown"))
            if index >= STAGES.index("install"):
                labels.add(("install", "maven_local"))
        elif tool == "gradle" and goal.rsplit(":", 1)[-1] == "build":
            labels.update({("compile", "production"), ("package", "unknown"), ("test", "unknown")})
        elif tool == "gradle" and goal.rsplit(":", 1)[-1] == "check":
            labels.add(("test", "unknown"))
    return sorted(labels)


def old_overlap(task, commands, old_base):
    if old_base is None:
        return []
    result = []
    manifest = read_json(str(old_base / "manifest.json"))
    for project in manifest["projects"]:
        task_path, task_ref = bound(old_base, project["task"])
        original = read_json(str(task_path))
        if original["repo"] != task["repo"]:
            continue
        requirements_ref = project["requirements"]
        spec_path, spec_ref = bound(old_base, {"path": requirements_ref["path"], "sha256": requirements_ref["file_sha256"]})
        spec = read_json(str(spec_path))
        if (canonical_digest(original) != project["task_sha256"]
                or spec["task_sha256"] != project["task_sha256"]
                or canonical_digest(spec) != requirements_ref["sha256"]):
            raise ValueError("Prior requirements manifest canonical identity differs")
        checks = {"commit": original["sha"] == task["sha"], "ci_identity": spec.get("ci_alignment", {}).get("selected_url") == task["official_ci_url"]}
        # The v1 frozen task cannot express external environment/settings.
        # A newly explicit override therefore needs review rather than inheriting
        # readiness from equal argv. Options already present in argv are compared
        # exactly below, including -P/-D/-s and wrapper selection.
        extra_configuration = configuration_overrides(task["environment_and_commands"])
        checks["no_unmatched_explicit_configuration"] = not extra_configuration
        steps = task["environment_and_commands"].get("steps")
        checks["ordered_commands_and_runtime"] = False
        if isinstance(steps, list) and len(steps) == len(original["steps"]):
            try:
                versions = task["environment_and_commands"].get("build_tool_versions", [])
                checks["ordered_commands_and_runtime"] = all(
                    shlex.split(new["command"]) == old["argv"]
                    and new.get("cwd", ".") == old.get("cwd", ".")
                    and re.match(r"(?:1\.)?(\d+)", str(new.get("jdk", "")))[1] == str(old.get("java_major"))
                    and (old.get("maven_version") is None or versions == [old["maven_version"]])
                    for new, old in zip(steps, original["steps"]))
            except (KeyError, TypeError, ValueError):
                pass
        exact = all(checks.values()) and task["environment_and_commands"].get("nonpublishing_projection") is None
        result.append({"project_id": project["id"], "match": "exact_task" if exact else "same_sha_different_task" if checks["commit"] else "different_sha",
                       "checks": checks, "readiness_transfer": exact,
                       "unmatched_explicit_configuration": extra_configuration,
                       "old_readiness": spec["annotation_completeness"], "task_ref": task_ref,
                       "requirements_ref": spec_ref,
                       "source_details": {"ci_alignment": spec.get("ci_alignment"), "ci_test_count_reference": project.get("ci_test_count_reference"), "preparation": project.get("preparation"), "group_labels": spec.get("group_labels"),
                                          "steps": spec.get("steps"), "requirement_count": len(spec["requirements"])}})
    return result


def configuration_overrides(contract):
    """Record extra literal configuration that v1 tasks cannot express."""
    fields = {"env", "environment", "config", "configuration", "profiles", "active_profiles", "properties", "settings", "settings_file", "maven_settings", "maven_config", "maven_args", "maven_opts", "gradle_properties", "gradle_opts", "java_tool_options", "_java_options", "jdk_java_options", "jvm_args", "jvm_options", "toolchains", "toolchain_file"}
    result = []
    def scan(value, pointer):
        if isinstance(value, dict):
            for key, child in value.items():
                child_pointer = pointer + "/" + key
                if key.lower() in fields and child not in (None, {}, [], ""):
                    result.append({"source_field": child_pointer, "value": child})
                elif isinstance(child, (list, dict)):
                    scan(child, child_pointer)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                scan(child, pointer + "/" + str(index))
    scan(contract, "/environment_and_commands")
    return result


def audit_task(base, task, old_base=None, source_task=None):
    row, review_ref = research_row(base, task)
    logs, log_errors = selected_logs(base, task, row)
    original = command_rows(task["environment_and_commands"])
    commands = [{**command, "parsed": command_goals(command["text"], task["tool"])} for command in original]
    labels = defaultdict(lambda: {"observations": [], "declarations": [], "candidates": []})
    unknown_goals = Counter()
    declared_test_tasks = {value for value in task["environment_and_commands"].get("required_test_tasks", []) if isinstance(value, str)}
    for log_index, log in enumerate(logs):
        for event in native_events(log["text"]):
            category = event.pop("classification")
            if category is None and event["tool"] == "gradle" and event["goal"] in declared_test_tasks:
                category = ("test", "unknown")
                event["classification_basis"] = "native_goal_matches_required_test_task_in_archived_task_review"
            if category is None:
                unknown_goals[event["goal"]] += 1
            else:
                labels[category]["observations"].append({**event, "source_ref": {"log_source_index": log_index}, "scope": "selected_reference_job_goal_invocation_not_full_requirement_verification", "requirement_membership": "unresolved_without_command_scope_and_execution_plan_review"})
    for command in commands:
        for category in candidate_labels(command["parsed"]["goals"], task["tool"]):
            labels[category]["candidates"].append({"source_ref": review_ref, "source_field": command["source_field"], "command": command["text"], "basis": "command_candidate_only_binding_and_effective_configuration_not_resolved"})
    defaults = task["environment_and_commands"].get("default_goals") or []
    default_records = []
    for item in defaults:
        text = item.get("text", "")
        match = re.search(r"<defaultGoal>(.*?)</defaultGoal>", text, re.S)
        tokens = shlex.split(match[1]) if match else []
        record = {**item, "goals": tokens, "status": "recorded_source_declaration_pending_raw_descriptor_match", "source_ref": review_ref}
        # Explicit source audit may strengthen this claim; a source declaration
        # still does not prove an active profile, effective bindings, or success.
        for descriptor in (source_task or {}).get("descriptors", []):
            if descriptor.get("pinned_blob_verified") is True and descriptor.get("source_path") == item.get("path") and descriptor.get("default_goal") == (match[1] if match else None):
                record.update(status="declared", descriptor_ref=descriptor)
        default_records.append(record)
        has_default_command = any(c["parsed"]["launcher"] and not c["parsed"]["goals"] for c in commands)
        if has_default_command:
            for category in candidate_labels(tokens, task["tool"]):
                bucket = "declarations" if record["status"] == "declared" and any(goal_label(g, task["tool"]) == category for g in tokens) else "candidates"
                labels[category][bucket].append({"basis": "source_default_goal" if bucket == "declarations" else "recorded_default_goal_candidate", "source_ref": record})
    # Counts support that a test pool exists, but cannot distinguish its kind.
    if task.get("reported_test_results_by_invocation"):
        labels[("test", "unknown")]["declarations"].append({"basis": "archived_reference_test_count_vectors_test_kind_not_inferred", "source_ref": review_ref})
    overlaps = old_overlap(task, commands, old_base)
    exact = next((o for o in overlaps if o["readiness_transfer"]), None)
    if exact:
        spec = read_json(exact["requirements_ref"]["path"])
        for requirement in spec["requirements"]:
            category = (requirement["kind"], requirement.get("subtype") or "unspecified")
            labels[category]["declarations"].append({"basis": "exact_frozen_task_reviewed_requirement", "requirement_membership": "reviewed_mandatory", "requirement_id": requirement["id"], "module": requirement.get("module"), "source_ref": exact["requirements_ref"]})
    packed = []
    for (kind, subtype), evidence in sorted(labels.items()):
        membership = "contains_reviewed_mandatory_requirement" if any(d.get("requirement_membership") == "reviewed_mandatory" for d in evidence["declarations"]) else "unresolved"
        packed.append({"kind": kind, "subtype": subtype, "status": "observed" if evidence["observations"] else "declared" if evidence["declarations"] else "candidate", "requirement_membership": membership, **evidence})
    present = {r["kind"] for r in packed}
    for kind in KINDS:
        if kind not in present:
            packed.append({"kind": kind, "subtype": "unknown", "status": "unknown", "requirement_membership": "unresolved", "basis": "no_positive_task_bound_evidence_not_an_absence_claim"})
    requested = [g for c in commands for g in c["parsed"]["goals"]]
    if any(c["parsed"]["launcher"] and not c["parsed"]["goals"] for c in commands):
        requested.extend(g for record in default_records for g in record["goals"])
    endpoints = [s for s in STAGES if s in requested]
    endpoint = {"raw_requested": task.get("build_test_endpoint"), "declared_goals_in_order": requested,
                "normalized_stages": endpoints if task["tool"] == "maven" else [],
                "highest_requested_maven_stage": endpoints[-1] if endpoints and task["tool"] == "maven" else None,
                "ci_job_observed_categories": sorted({r["kind"] for r in packed if r["status"] == "observed"}),
                "scope_complete": bool(exact and exact["old_readiness"]["status"] == "complete")}
    risks = []
    for kind, pattern in [("docker_runtime", r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?docker(?:-compose)?\s+(?:run|build|compose|start)\b"), ("android_target", r"\b(?:connectedAndroidTest|assembleDebug|assembleRelease|sdkmanager|avdmanager)\b"), ("additional_toolchain", r"(?:^|[;&|]\s*)\s*(?:npm|yarn|pnpm|cmake|gcc|g\+\+|make|bazel|ant|ruby|bundle)\b")]:
        witnesses = [c for c in commands if re.search(pattern, c["text"])]
        observed = []
        for log_index, log in enumerate(logs):
            for number, raw_line in enumerate(log["text"].splitlines(), 1):
                line = STAMP.sub("", ANSI.sub("", raw_line))
                match = re.match(r"^(?:\+ |\[command\])(.+)", line)
                if match and re.search(pattern, match[1]):
                    observed.append({"log_source_index": log_index, "line": number, "text": match[1]})
        risks.append({"kind": kind, "status": "declared_task_command_requires_review" if witnesses else "observed_ci_job_command_requires_scope_review" if observed else "unknown_no_positive_command_evidence", "commands": witnesses, "observed_job_commands": observed,
                      "automatic_exclusion": False, "reason": "Repository file presence and naming are not task dependency evidence."})
    return {"task_id": task["task_id"], "repo": task["repo"], "sha": task["sha"], "ci_identity": task["ci_identity"], "official_ci_url": task["official_ci_url"], "tool": task["tool"],
            "commands": commands, "frozen_configuration": task["environment_and_commands"], "recorded_default_goals": default_records,
            "declared_projection": task.get("projection_label"), "labels": packed, "build_endpoint": endpoint,
            "requirements_readiness": exact["old_readiness"]["status"] if exact else "review_required",
            "readiness_basis": "exact_existing_requirements_v2_task" if exact else "descriptive_labels_only_not_full_effective_plan_or_artifact_inventory",
            "old20_overlap": overlaps, "source_ref": review_ref, "source_audit": source_task,
            "log_sources": [log["ref"] for log in logs], "log_errors": log_errors,
            "unclassified_native_goals": dict(sorted(unknown_goals.items())),
            "unknowns": ["Observed plugin/task invocation is not proof of success or complete requirement scope.", "A selected CI job can contain preparation, publication or other commands outside the frozen task; job observations do not establish task requirement membership.", "Absent category evidence does not prove that category is not required."] + ([] if exact else ["Effective execution graph, artifacts, scope and configuration need requirements-v2 review before formal scoring."]) + ([] if logs else ["No unambiguous selected native log retained by this resolver."]),
            "task_risks": risks, "archived_dependency_review": row.get("dependency_review") or row.get("dependency_notes") or row.get("dependencies") or row.get("eligibility_review")}


def verified_descriptors(source_audit, task):
    source = next((t for t in source_audit.get("tasks", []) if t["task_id"] == task["task_id"]), None)
    if source is None:
        return None
    revision = source.get("sha") or source.get("commit")
    if source.get("repo") != task["repo"] or revision != task["sha"]:
        raise ValueError("Descriptor audit task identity differs")
    result = {"task_id": task["task_id"], "repo": task["repo"], "sha": task["sha"], "descriptors": []}
    for descriptor in source.get("descriptors", []):
        path, ref = bound(Path(source_audit["audit_base"]), descriptor)
        tree = ET.fromstring(path.read_bytes())
        strip = lambda tag: tag.rsplit("}", 1)[-1]
        goals = [item.text.strip() if item.text else "" for build in tree if strip(build.tag) == "build" for item in build if strip(item.tag) == "defaultGoal"]
        declared = goals[0] if len(goals) == 1 else None
        if descriptor.get("default_goal") != declared:
            raise ValueError("Descriptor defaultGoal differs from its raw source bytes")
        result["descriptors"].append({**descriptor, "verified_raw_ref": ref})
    return result


def make_audit(base, old_base=None, source_audit=None):
    dataset = read_json(str(base / "dataset.json"))
    tasks = [audit_task(base, t, old_base, verified_descriptors(source_audit, t) if source_audit else None) for t in dataset["reference_tasks"]]
    if len({t["task_id"] for t in tasks}) != len(tasks):
        raise ValueError("Duplicate task identity")
    counts = {}
    for kind in KINDS:
        count = Counter()
        for task in tasks:
            rows = [r for r in task["labels"] if r["kind"] == kind]
            for status, evidence in [("observed", "observations"), ("declared", "declarations"), ("candidate", "candidates")]:
                if any(r.get(evidence) for r in rows):
                    count[status] += 1
            if all(r["status"] == "unknown" for r in rows):
                count["unknown"] += 1
        counts[kind] = dict(count)
    return {"schema": "sag-requirement-label-audit-v1", "policy_version": "ci-requirements-v2", "dataset_ref": file_ref(str(base / "dataset.json")), "audit_script": file_ref(__file__),
            "old_requirements_manifest_ref": file_ref(str(old_base / "manifest.json")) if old_base else None,
            "policy": {"no_execution": True, "no_new_collection": True, "labels_are_overlapping_observations_and_candidates": True, "ordinal_difficulty_weights": None,
                       "observed": "A classified native goal/task appears in a selected archived CI job; its execution state is retained. This is not a passed requirement.",
                       "declared": "Pinned source declaration, archived test-pool declaration, or exact requirements-v2 review; stated basis distinguishes each.",
                       "candidate": "Command/defaultGoal suggests the category; bindings and active configuration are not fully resolved.",
                       "unknown": "No positive mapped evidence; never an absence claim.", "requirement_membership": "Unresolved except for exact reviewed requirements: selected jobs can include commands outside the frozen task.", "readiness_transfer": "Exact repository, SHA, CI URL, ordered commands, cwd and pinned runtime only; additional explicit environment or configuration prevents transfer."},
            "summary": {"task_count": len(tasks), "project_count": len({t["repo"] for t in tasks}), "tasks_with_selected_logs": sum(bool(t["log_sources"]) for t in tasks),
                        "readiness": dict(Counter(t["requirements_readiness"] for t in tasks)), "category_task_counts_by_evidence": counts,
                        "default_goal_declarations": dict(Counter(r["status"] for t in tasks for r in t["recorded_default_goals"])),
                        "exact_old20_task_overlap": [t["task_id"] for t in tasks if any(x["readiness_transfer"] for x in t["old20_overlap"])]}, "tasks": tasks}


def markdown(audit):
    s = audit["summary"]
    lines = ["# CI-job observations and candidate requirement labels", "", f"Reviewed **{s['task_count']} reference tasks / {s['project_count']} repositories** using archived evidence only. No project code, build, test, or model was executed.", "",
             "This is a descriptive label audit, not a new acceptance manifest. A native goal banner shows that a goal was invoked somewhere in the selected CI job; it does not establish success, complete module coverage, every artifact obligation, or membership in the frozen task. A job may include preparation or publication commands outside that task. Missing labels mean unknown, not 'not required'.", "",
             f"Selected native logs resolved for **{s['tasks_with_selected_logs']} tasks**. Requirements-v2 readiness: `{json.dumps(s['readiness'])}`. Full readiness transfers only from an exact existing task; other rows retain explicit review gaps.", "",
             "| Category | CI-job invocation observed | Source/review declared | Command candidate present | Unknown |", "|---|---:|---:|---:|---:|"]
    for kind, values in s["category_task_counts_by_evidence"].items():
        lines.append(f"| {kind} | {values.get('observed',0)} | {values.get('declared',0)} | {values.get('candidate',0)} | {values.get('unknown',0)} |")
    lines += ["", "Columns can overlap because one task has several subtypes and evidence sources. There is no weighted difficulty score. `verify` does not establish integration tests; `build` does not establish every Gradle dependency; aggregate POMs do not automatically require a JAR. Javadoc and explicit quality goals are retained separately from the lifecycle endpoint.", "",
              "## Exact development-set overlap", ""]
    for task in audit["tasks"]:
        for overlap in task["old20_overlap"]:
            if overlap["readiness_transfer"]:
                lines.append(f"- `{task['task_id']}` {task['repo']} @ `{task['sha']}`: {task['requirements_readiness']}; [official CI]({task['official_ci_url']}).")
    lines += ["", "Other overlaps retain old source details for inspection but do not borrow another SHA/cell/JDK's requirements or readiness. All existing results and source packages remain unchanged.", "",
              "## Per-task observations and gaps", "", "| Task | Repository | CI-job categories observed | Ready for requirements-v2 scoring | Requested endpoint (archived) |", "|---|---|---|---|---|"]
    for task in audit["tasks"]:
        categories = ", ".join(task["build_endpoint"]["ci_job_observed_categories"]) or "unavailable"
        endpoint = str(task["build_endpoint"]["raw_requested"]).replace("|", "\\|")
        lines.append(f"| `{task['task_id']}` | {task['repo']} | {categories} | {task['requirements_readiness']} | {endpoint} |")
    lines += ["", "## Selection implications", "", "These overlapping CI-job observations identify candidates for stratified sampling. First review command boundaries and complete effective task requirements; only then use verified requirement categories for sampling or failure analysis. Do not treat a job's extra site/deploy goal as a mandatory task requirement, or force a linear difficulty ladder across unrelated checks. Native compile, installation, documentation and quality checks are separate branches; sample size and uncertainty must remain visible.", "", "Docker/Android/extra-language flags are based only on explicit task commands or retained human-reviewed task evidence. A Dockerfile, Gradle Kotlin build script, fixture, or example directory does not exclude a project. Observed CI container launchers are distinguished from an actual nested-Docker task dependency. The JSON preserves dependency reviews and positive command witnesses without making an automatic exclusion.", "", "Reproduction: `uv run python scripts/benchmark_requirement_labels.py --dataset output/java-benchmark-20260916 --old-requirements output/sag-benchmark20-requirements-validated-20260922 --source-audit output/java-benchmark-rescreen-20260922/source-audit/audit.json --output output/java-benchmark-rescreen-20260922/requirement-audit`.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--old-requirements", type=Path)
    parser.add_argument("--source-audit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = make_audit(args.dataset.resolve(), args.old_requirements.resolve() if args.old_requirements else None, read_json(str(args.source_audit.resolve())) if args.source_audit else None)
    audit["source_audit_ref"] = file_ref(str(args.source_audit.resolve())) if args.source_audit else None
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    (args.output / "report.md").write_text(markdown(audit))
    print(json.dumps(audit["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
