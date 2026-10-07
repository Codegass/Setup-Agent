#!/usr/bin/env python3
"""Prepare eight pinned pilot requirement reviews using archived bytes only.

This command never invokes a project, downloads a dependency, or resolves an
effective model. Metadata command requests are output data, never executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile
import xml.etree.ElementTree as ET

from scripts.benchmark_requirement_labels import (
    ANSI, STAMP, canonical_digest, dictionaries, file_ref, native_events, research_row,
    selected_logs,
)
from scripts.build_benchmark_requirements import VALUE_OPTIONS
from sag.benchmark.requirements import validate_requirements


PILOT = [
    ("commons-dbutils", "2f1ae1ca232dd52a258f", 17, "3.9.16", None),
    ("commons-csv", "4ff17df30f0451053d38", 21, "3.9.16", None),
    ("commons-dbcp", "0c038995886f1aa75239", 8, "3.9.16", None),
    ("gson", "e5b9ad5bb459b283401b", 21, "3.9.16", None),
    ("google-java-format", "c16d98e10b7ec7b30d8d", 25, "3.9.16", None),
    ("calcite-avatica", "fb5c244128011eb99445", 17, None, "8.14.4"),
    ("spring-ws", "b557e184669d9d1b57d7", 25, None, "9.7.1"),
    ("commons-math", "6339d9e6b6bf6f933521", 17, "3.9.16", None),
]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return {**file_ref(str(path)), "path": str(path)}


def copied_ref(path, output, *, basis, source=None):
    path = Path(path)
    raw = path.read_bytes()
    destination = output / "sources" / (hashlib.sha256(raw).hexdigest()[:16] + "-" + path.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return {**file_ref(str(destination)), "path": destination.relative_to(output).as_posix(),
            "basis": basis, "origin": source or file_ref(str(path))}


def selected_commands(task):
    config = task["environment_and_commands"]
    if "steps" in config:
        values = config["steps"]
    elif "command" in config:
        values = [config["command"]]
    elif "actual_commands" in config:
        values = config["actual_commands"]
    else:
        values = config["commands"]
    result = []
    for value in values:
        row = {"command": value} if isinstance(value, str) else value
        text = row.get("command") or row.get("text")
        argv = shlex.split(text)
        if not argv or any(x in {"&&", "||", "|", ";", "&"} or "$" in x or "`" in x for x in argv):
            raise ValueError("Pilot needs a reviewed literal command; shell expansion is not inferred")
        result.append({"text": text, "argv": argv, "cwd": row.get("cwd", "."),
                       "declared_source": row})
    return result


def config_path(path):
    p = PurePosixPath(path)
    return (p.name == "pom.xml" or p.suffix in {".gradle", ".kts"}
            or p.name.startswith("profile.") or p.suffix == ".pro"
            or p.name in {"gradle.properties", "gradle-wrapper.properties", "gradle-wrapper.jar", ".gitattributes",
                          "build.properties", "MANIFEST.MF",
                          "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat", "Jenkinsfile"}
            or path.startswith((".mvn/", ".github/workflows/", ".github/actions/", "gradle/", "buildSrc/")))


def blob_hash(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def capture_configs(dataset, task, source_audit, output):
    """Copy only regular build configuration bytes and validate pinned Git blobs."""
    row = next(t for t in source_audit["tasks"] if t["task_id"] == task["task_id"])
    if row["repo"] != task["repo"] or row["sha"] != task["sha"]:
        raise ValueError("Source audit identity differs")
    archive = dataset / task["source_archive"]
    if file_ref(str(archive))["sha256"] != task["source_archive_sha256"]:
        raise ValueError("Pinned source archive changed")
    binding = row.get("tree_binding") or {}
    if binding.get("valid") is not True or binding.get("response_sha") != task["sha"]:
        raise ValueError("Pilot configuration capture requires its exact complete Git tree")
    tree_path = dataset / binding["path"]
    if file_ref(str(tree_path))["sha256"] != binding["sha256"]:
        raise ValueError("Pinned Git tree bytes changed")
    tree = json.loads(tree_path.read_text())
    if tree.get("truncated") or tree.get("sha") != task["sha"]:
        raise ValueError("Incomplete or wrong Git tree")
    tracked = {x["path"]: x for x in tree["tree"] if x["type"] == "blob" and config_path(x["path"])}
    found = {}
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            relative = member.name.partition("/")[2]
            if not config_path(relative):
                continue
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise ValueError("Unsafe archive config path")
            if relative not in tracked:
                if member.isfile():
                    raise ValueError("Untracked configuration in source archive: " + relative)
                continue
            if relative in found or not member.isfile() or tracked[relative]["mode"] == "120000":
                raise ValueError("Duplicate or symlink configuration requires explicit review")
            raw = source.extractfile(member).read()
            pinned_bytes_equal = blob_hash(raw) == tracked[relative]["sha"]
            destination = output / "source-config" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            found[relative] = {**file_ref(str(destination)), "path": destination.relative_to(output).as_posix(),
                               "source_path": relative, "archive_member": member.name,
                               "git_blob_sha1": tracked[relative]["sha"], "observed_blob_sha1": blob_hash(raw),
                               "pinned_blob_verified": pinned_bytes_equal}
    if set(found) != set(tracked):
        raise ValueError("Pinned build configuration files are absent from source archive")
    return {"schema": "pinned-pilot-build-config-v1", "repo": task["repo"], "commit": task["sha"],
            "archive_ref": file_ref(str(archive)), "tree_ref": file_ref(str(tree_path)),
            "files": list(found.values()), "complete_for_declared_path_selector": True,
            "all_selected_config_bytes_match_pinned_blobs": all(x["pinned_blob_verified"] for x in found.values()),
            "config_byte_mismatches": [x["source_path"] for x in found.values() if not x["pinned_blob_verified"]],
            "selector": "all POMs, Gradle scripts/build logic, launchers, .mvn, Gradle config and workflow files; not an effective-model claim"}


def pom_declarations(raw):
    root = ET.fromstring(raw)
    for item in root.iter():
        item.tag = item.tag.rsplit("}", 1)[-1]
    def xml(node):
        return ET.tostring(node, encoding="unicode") if node is not None else None
    return {"group_id": root.findtext("groupId"), "artifact_id": root.findtext("artifactId"),
            "version": root.findtext("version"), "packaging": root.findtext("packaging"),
            "parent": {k: root.findtext("parent/" + k) for k in ["groupId", "artifactId", "version", "relativePath"]},
            "default_goal": root.findtext("build/defaultGoal"),
            "declared_modules": [x.text for x in root.findall("modules/module")],
            "build_paths": {k: root.findtext("build/" + k) for k in ["directory", "outputDirectory", "finalName"]},
            "profiles": [{"id": p.findtext("id"), "activation_xml": xml(p.find("activation")),
                          "modules": [x.text for x in p.findall("modules/module")],
                          "declaration_xml": xml(p)} for p in root.findall("profiles/profile")],
            "plugins": [{"artifact_id": p.findtext("artifactId"), "declaration_xml": xml(p)} for p in root.findall("build/plugins/plugin")],
            "plugin_management": [{"artifact_id": p.findtext("artifactId"), "declaration_xml": xml(p)} for p in root.findall("build/pluginManagement/plugins/plugin")],
            "interpretation": "direct source declarations only; inheritance, active profiles and lifecycle bindings are unresolved"}


def metadata_requests(task, commands, runtime, output):
    requests = []
    for index, command in enumerate(commands, 1):
        step_id = "ci-step-" + str(index)
        if task["tool"] == "maven":
            kept, value = [command["argv"][0]], False
            for token in command["argv"][1:]:
                if value:
                    kept.append(token)
                    value = False
                elif token.startswith("-"):
                    kept.append(token)
                    value = token in VALUE_OPTIONS
            if value:
                raise ValueError("Missing Maven option value")
            probes = [
                {"kind": "effective_pom", "argv": kept + ["org.apache.maven.plugins:maven-help-plugin:3.5.1:effective-pom", "-Dverbose", "-Doutput=${OUTSIDE_CHECKOUT}/" + step_id + "-effective-pom.xml"]},
                {"kind": "effective_settings", "argv": kept + ["org.apache.maven.plugins:maven-help-plugin:3.5.1:effective-settings", "-Doutput=${OUTSIDE_CHECKOUT}/" + step_id + "-effective-settings.xml"]},
                {"kind": "active_profiles", "argv": kept + ["org.apache.maven.plugins:maven-help-plugin:3.5.1:active-profiles"]},
            ]
            needs = ["one source-bound effective model for every selected reactor project", "active profiles and exact settings, argv, cwd, runtime and source HEAD receipts", "main/attached artifact path, packaging, classifier, extension and coordinates", "every POM execution and native goal occurrence reviewed against the selected command segment", "before any metadata request review .mvn/maven.config and MAVEN_ARGS for hidden lifecycle goals; do not execute an unreviewed inherited command"]
        else:
            probes = [{"kind": "dry_run_graph_candidate", "argv": command["argv"] + ["--dry-run"],
                       "caveat": "Configuration executes project build scripts; requires a later isolated metadata phase. A dry-run by itself does not expose artifact/report paths, types or task dependencies."}]
            needs = ["source-bound Gradle task type and dependsOn/finalizedBy graph for the exact target", "project/included-build mapping and archive outputs", "Test task report directories, test engine/suite role and Java launcher", "wrapper distribution/JAR/config provenance and Gradle runtime version", "NO-SOURCE/cached tasks reviewed individually; selected CI job banners alone do not establish the required graph"]
        requests.append({"step_id": step_id, "cwd": command["cwd"], "frozen_build_argv": command["argv"],
                         "runtime": runtime, "proposed_metadata_probes": probes, "required_outputs": needs,
                         "status": "not_executed", "template_values": {"OUTSIDE_CHECKOUT": "task-owned evidence directory selected by the later resource/capture plan"}})
    return {"schema": "pilot-metadata-requests-v1", "task_id": task["task_id"],
            "repo": task["repo"], "sha": task["sha"], "requests": requests,
            "execution_policy": {"not_ci_replay": True, "no_lifecycle_goal": task["tool"] == "maven",
                                 "isolate_cache_from_agent": True, "no_agent_cache_transfer": True,
                                 "checkout": "Materialize a real exact-SHA Git checkout, with required pinned submodules, or separately prove an equivalent method. Extracted configuration files are not a checkout; never create a synthetic commit and present it as the official SHA.",
                                 "settings": "Use isolated public-only settings; preserve receipt and do not archive credentials.",
                                 "state": "requests only; no command has been run"}}


def runtime_observations(logs):
    """Retain public execution-input echoes, never arbitrary environment secrets."""
    inputs, versions = {}, []
    allowed = re.compile(r"^\s*(JAVA_HOME(?:_\d+_X64)?|MAVEN_ARGS|MAVEN_OPTS|GRADLE_OPTS|JAVA_TOOL_OPTIONS|JDK_JAVA_OPTIONS|_JAVA_OPTIONS|GRADLE_USER_HOME):\s*(.*)$")
    for index, log in enumerate(logs):
        for number, raw in enumerate(log["text"].splitlines(), 1):
            line = STAMP.sub("", ANSI.sub("", raw))
            match = allowed.match(line)
            ref = {"log_source_index": index, "line": number, "text": line}
            if match:
                inputs.setdefault(match[1], {}).setdefault(match[2], []).append(ref)
            if re.match(r"^(?:Apache Maven \d|Java version:|Welcome to Gradle \d)", line):
                versions.append(ref)
    return {"environment_values": [{"name": name, "value": value, "sources": sources}
                                    for name, values in inputs.items() for value, sources in values.items()],
            "tool_version_lines": versions,
            "scope": "selected CI-job input echoes; each future capture must preserve corresponding command context"}


def source_witness(output, index, path, contains):
    ref = next(x for x in index["files"] if x["source_path"] == path)
    if ref["pinned_blob_verified"] is not True:
        raise ValueError("An unverified config byte sequence cannot support a source claim")
    lines = (output / ref["path"]).read_text().splitlines()
    matches = [{"line": i, "text": value} for i, value in enumerate(lines, 1) if contains in value]
    if not matches:
        raise ValueError("Reviewed source witness no longer exists: " + path + " / " + contains)
    return {"source_ref": ref, "matches": matches}


def review_scope(slug, task, commands, output, index, declarations, logs):
    """Explicit small review notes, never an automatic lifecycle resolver."""
    facts, gaps = [], []
    def fact(text, *witnesses, tier="source_declared"):
        facts.append({"claim": text, "tier": tier,
                      "sources": [source_witness(output, index, p, s) for p, s in witnesses]})
    if slug == "commons-dbutils":
        facts.append({"claim": "The exact selected Jenkins task, Maven/JDK, cwd and argv match the previously complete reviewed definition. Test endpoint has no main-JAR obligation.",
                      "tier": "complete_existing_requirements_v2", "sources": [file_ref(str(output / "requirements.json"))]})
        gaps = ["Definition ready; official-reference replay, runtime/worktree receipts and campaign resource/telemetry gates are separate and remain pending."]
    elif slug in {"commons-csv", "commons-dbcp"}:
        fact("The command has no explicit goal; the root POM's literal defaultGoal determines the ordered requested target sequence.", ("pom.xml", "<defaultGoal>"))
        fact("The project inherits commons-parent 105; packaging is not explicitly overridden in the source POM. Maven's jar default is a declaration, not proof of a produced artifact.", ("pom.xml", "<version>105</version>"))
        if slug == "commons-csv":
            fact("The source declares an attached test JAR. The defaultGoal ends with Javadoc then Checkstyle; inherited package/quality obligations must be retained.", ("pom.xml", "<goal>test-jar</goal>"), ("pom.xml", "<defaultGoal>"))
            fact("The pinned Surefire configuration excludes PerformanceTest and compiler excludes benchmark sources; preserve upstream scope, never add a benchmark-specific skip.", ("pom.xml", "PerformanceTest.java"), ("pom.xml", "**/*Benchmark*"))
            gaps = ["Capture effective POM/settings/active profiles under Temurin Java 21.0.12.1 and Maven 3.9.16 with -Ddoclint=all and MAVEN_ARGS=-ntp; old Java17 preparation is not this task.", "Review every inherited/defaultGoal binding including Javadoc forks and final Checkstyle; derive all binary/test/source/SBOM/auxiliary artifact obligations from that effective model."]
        else:
            fact("The local jdk9-plus and jdk11-plus profiles have explicit JDK ranges; they are not active on this Java8 cell. Inherited Java8 profiles still require resolution.", ("pom.xml", "<id>jdk9-plus</id>"), ("pom.xml", "<id>jdk11-plus</id>"))
            fact("Checkstyle precedes SpotBugs/PMD/CPD and final Javadoc in this defaultGoal, unlike CSV. The source's Surefire exclusions remain part of the frozen test scope.", ("pom.xml", "<defaultGoal>"), ("pom.xml", "**/Tester*.java"))
            gaps = ["Capture effective POM/settings/active profiles under Temurin Java 1.8.0_504 and Maven 3.9.16 with MAVEN_ARGS=-ntp; old Java17 preparation is not this task.", "Review Java8-dependent inherited plugin versions/animal-sniffer and all forked/defaultGoal checks; derive actual main/attached artifact obligations and explicit no-input dispositions."]
        gaps += ["Freeze resolved last-release coordinates used by japicmp, public network requirements, and RAT's sensitivity to agent-created untracked files without exempting such files."]
    elif slug == "gson":
        fact("This revision declares seven children plus the parent reactor. The selected official task is one `mvn verify javadoc:jar` invocation; the earlier pilot draft's multi-step description was incorrect.", ("pom.xml", "<module>"), tier="source_declaration_and_frozen_command")
        fact("Gson and test-shrinker explicitly declare Failsafe integration-test/verify. Unit and integration pools must remain separate; verify by itself was not used to infer these goals.", ("gson/pom.xml", "<artifactId>maven-failsafe-plugin</artifactId>"), ("test-shrinker/pom.xml", "<artifactId>maven-failsafe-plugin</artifactId>"))
        fact("The native-image-test profile is explicitly inactive by default; the test-graal-native-image directory alone does not require GraalVM or native compilation for this Java21 task.", ("test-graal-native-image/pom.xml", "<id>native-image-test</id>"), ("test-graal-native-image/pom.xml", "<activeByDefault>false</activeByDefault>"))
        fact("The native-image test module has skipIfEmpty for its JAR; the parent skips several artifact/documentation actions for gson.isTestModule. Eight modules cannot become eight assumed JARs.", ("test-graal-native-image/pom.xml", "<skipIfEmpty>true</skipIfEmpty>"), ("pom.xml", "<skip>${gson.isTestModule}</skip>"))
        fact("test-shrinker replaces its main shaded output and invokes R8 as Java. This source alone is not an Android application target or a second production-language toolchain.", ("test-shrinker/pom.xml", "<shadedArtifactAttached>false</shadedArtifactAttached>"), ("test-shrinker/pom.xml", "com.android.tools.r8.R8"))
        fact("The Gson JAR receives a module descriptor through ModiTect; proto generates protobuf Java test sources. These custom producers require artifact/source-scope review.", ("gson/pom.xml", "<goal>add-module-info</goal>"), ("proto/pom.xml", "<goal>generate-test</goal>"))
        gaps = ["Capture the eight-project effective reactor under Temurin21.0.12+1/Maven3.9.16, preserving project file/JDK-dependent profiles and implicit CI environment.", "Resolve main JAR replacements, skipped empty test-module JARs, buildinfo/source/Javadoc attachments, module-info rewrite and any generated test compilation before freezing artifacts.", "Map Surefire and Failsafe report directories/occurrences per module and Javadoc fork bindings; retain Spotless/Enforcer and all effective verifier bindings."]
    elif slug == "google-java-format":
        fact("The JDK>=17 eclipse profile adds eclipse_plugin to the root/core reactor; selected JDK25 therefore requires review of that third module.", ("pom.xml", "<id>eclipse</id>"), ("pom.xml", "<jdk>[17,)</jdk>"), ("pom.xml", "<module>eclipse_plugin</module>"))
        fact("eclipse_plugin uses eclipse-plugin packaging and Tycho, not the ordinary jar lifecycle. Its produced/installed JAR and POM coordinates need a reviewed custom-packaging declaration.", ("eclipse_plugin/pom.xml", "<packaging>eclipse-plugin</packaging>"), ("eclipse_plugin/pom.xml", "<artifactId>tycho-maven-plugin</artifactId>"))
        fact("core attaches an all-deps shaded JAR in addition to its main artifact.", ("core/pom.xml", "<shadedArtifactAttached>true</shadedArtifactAttached>"), ("core/pom.xml", "<shadedClassifierName>all-deps</shadedClassifierName>"))
        gaps = ["Capture separate effective models for both exact commands under Zulu25.0.4+7/Maven3.9.16. Do not propagate step1's -DskipTests=true or -Dmaven.javadoc.skip=true into step2.", "Step1 install requires all main/attached/custom Tycho artifacts and installed POMs. Explicitly review Tycho's lifecycle and any JAR replacement/classifier behavior.", "Step2 mvn test -B requires actual test evidence for its configured pools; no test success may be inferred from step1's successful install."]
    elif slug == "calcite-avatica":
        fact("The frozen command explicitly overrides org.gradle.parallel=true with --no-parallel and requests build plus javadoc. Preserve that command instead of silently changing worker behavior.", ("gradle.properties", "org.gradle.parallel=true"), tier="source_declaration_and_frozen_command")
        fact("The project defines build behavior in Kotlin Gradle configuration. These files are build logic, not evidence of a required second production-language target.", ("settings.gradle.kts", "rootProject.name"))
        gaps = ["Capture Gradle8.14.4/Zulu17.0.20+8 task types, target dependency/finalizer graph, included builds, archive outputs and Test XML paths for exact build+javadoc.", "Reconcile all 13 declared/implicit projects with task graph. Review six executed Test tasks and two NO-SOURCE tasks individually; a no-source test task contributes no synthetic test records.", "Derive ordinary/shaded/distribution/SBOM/Javadoc artifact obligations from task properties; do not infer output names from project count or build banners.", "Archive wrapper distribution checksum and actual runtime evidence; the Windows gradlew.bat byte mismatch is disclosed separately and does not relax Linux launcher proof."]
    elif slug == "spring-ws":
        fact("The root includes eight projects and a separate Gradle plugin build. Root check does not automatically request the included build's own standalone tests.", ("settings.gradle", "includeBuild"), ("settings.gradle", 'include "spring-ws-core"'))
        fact("The conventions plugin sets each Test task's Java launcher from toolchainVersion; compilation/release and launcher runtime are different constraints.", ("gradle/plugins/conventions-plugin/src/main/java/org/springframework/ws/gradle/conventions/toolchain/ToolchainPlugin.java", "launcherFor"))
        fact("The project enables parallelism and caching. CI cache observations are not proof that a fresh acceptance run generated every required output.", ("gradle.properties", "org.gradle.parallel=true"), ("gradle.properties", "org.gradle.caching=true"))
        fact("The conventions plugin disables checkstyleTest and configures JUnit Platform for Test. Retain upstream disabled behavior rather than adding a mandatory inactive check.", ("gradle/plugins/conventions-plugin/src/main/java/org/springframework/ws/gradle/conventions/CheckstyleConventions.java", 'named("checkstyleTest")'), ("gradle/plugins/conventions-plugin/src/main/java/org/springframework/ws/gradle/conventions/JavaPluginConventions.java", "useJUnitPlatform"))
        gaps = ["Capture Gradle9.7.1 graph and report/output paths using runtime Liberica25.0.4+9, separate test Liberica17.0.20+10 and the CI user-home Gradle property toolchainVersion=17.", "AcceptanceTask v1 currently represents launcher Java only. Before complete, bind/enforce Test launcher17 and Gradle version via supported verifier receipts; never relabel launcher25 as17.", "Resolve exact root-check dependencies including included-build compilation, checkFormat/checkstyle, test fixtures and five executed Test pools. Do not impose whole-repository JAR/Javadoc generation.", "Preserve public snapshot repositories and record resolved dependency inputs for reference replay; optional scan/notification secrets must not become setup prerequisites."]
    elif slug == "commons-math":
        fact("The source inherits commons-parent97 and declares ordinary modules plus conditional release/site modules; seventeen source POMs are not seventeen executed CI modules.", ("pom.xml", "<version>97</version>"), ("pom.xml", "<id>release</id>"), ("pom.xml", "<module>dist-archive</module>"))
        gaps = ["Capture effective reactor/settings/active profiles for the frozen projected clean install command under Java17.0.12/Maven3.9.16, including all14 selected CI modules and inherited file-activated profiles.", "Retain all production/test compilation, binary/test/source/SBOM/module-info/site-descriptor and local-install obligations. Aggregator POMs require installation of POMs, never synthetic main JARs.", "Review separate default-test and flaky-tests occurrences in legacy; final reports must not be reused to certify unrelated invocations or duplicate test identities.", "The archived original clean deploy→clean install projection is only statically reviewed. Freeze its source proof and replay exactly the projection before claiming executable equivalence; no remote publication is authorized.", "Review every explicit quality-check configuration, including PMD failOnViolation=false and JaCoCo disabled/no-data dispositions; goal banners alone do not establish a gate."]
    return {"schema": "pilot-static-scope-review-v1", "task_id": task["task_id"], "repo": task["repo"], "sha": task["sha"],
            "fixed_commands": commands, "facts": facts, "remaining_gates": gaps,
            "definition_readiness": "complete" if slug == "commons-dbutils" else "review_required",
            "module_denominator_status": "reviewed" if slug == "commons-dbutils" else "not_frozen_by_source_pom_count_or_job_banners",
            "execution_performed": False,
            "prohibited_inferences": ["No all-Java-file compilation claim", "No complete task from whole-job goal inventory", "No cross-JDK effective-model reuse", "No mandatory JAR for an aggregate/empty/custom module without output review"]}


def copy_complete_definition(task, old, output):
    manifest = json.loads((old / "manifest.json").read_text())
    project = next(p for p in manifest["projects"] if p["id"] == "commons-dbutils")
    task_path = old / project["task"]["path"]
    spec_path = old / project["requirements"]["path"]
    frozen = json.loads(task_path.read_text())
    spec = json.loads(spec_path.read_text())
    if (frozen["repo"] != task["repo"] or frozen["sha"] != task["sha"]
            or spec["ci_alignment"]["selected_url"] != task["official_ci_url"]
            or file_ref(str(task_path))["sha256"] != project["task"]["sha256"]
            or file_ref(str(spec_path))["sha256"] != project["requirements"]["file_sha256"]
            or canonical_digest(spec) != project["requirements"]["sha256"]):
        raise ValueError("Prior complete DbUtils definition does not match frozen pilot")
    selected = selected_commands(task)
    if [x["argv"] for x in selected] != [x["argv"] for x in frozen["steps"]]:
        raise ValueError("Prior DbUtils command differs")
    validate_requirements(spec, frozen)
    (output / "task.json").write_bytes(task_path.read_bytes())
    (output / "requirements.json").write_bytes(spec_path.read_bytes())
    copied = {}
    for item in dictionaries(spec):
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("sources/") or not item.get("sha256"):
            continue
        source = (old / path).resolve()
        if not source.is_relative_to(old.resolve()) or file_ref(str(source))["sha256"] != item["sha256"]:
            raise ValueError("Prior definition source changed")
        destination = output / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        copied[path] = {**file_ref(str(destination)), "path": path}
    return {"status": "complete", "basis": "exact prior reviewed task copied byte-for-byte", "sources_copied": len(copied),
            "task": file_ref(str(output / "task.json")), "requirements": file_ref(str(output / "requirements.json")),
            "previous_manifest_ref": file_ref(str(old / "manifest.json")), "canonical_task_sha256": canonical_digest(frozen)}


def recovery_reference(task, index, recovery_path):
    if recovery_path is None:
        return None
    audit = json.loads(recovery_path.read_text())
    row = next((r for r in audit["revisions"] if r["repo"] == task["repo"] and r["sha"] == task["sha"]), None)
    if row is None:
        return None
    result = []
    base = Path(audit["evidence_base"]).resolve()
    for recovered in row.get("recovered_blobs", []):
        old = next((x for x in index["files"] if x["source_path"] == recovered["repository_path"]), None)
        if old is None:
            continue
        file = (base / recovered["file"]["path"]).resolve()
        if not file.is_relative_to(base) or file_ref(str(file))["sha256"] != recovered["file"]["sha256"]:
            raise ValueError("Recovered configuration source hash/path differs")
        if (blob_hash(file.read_bytes()) != old["git_blob_sha1"]
                or recovered["git_blob_sha"] != old["git_blob_sha1"]
                or recovered["original_archived_sha256"] != old["sha256"]):
            raise ValueError("Recovered configuration does not bind to original and pinned bytes")
        result.append({"original_archive_config": old, "recovery": recovered,
                       "verified_restored_ref": file_ref(str(file)),
                       "original_bytes_preserved": True, "does_not_upgrade_requirement_readiness": True})
    return {"audit_ref": file_ref(str(recovery_path)), "repo": task["repo"], "sha": task["sha"],
            "selected_config_recoveries": result, "checkout_materialized": False}


def prepare(dataset, source_audit_path, labels_path, old, output, checkout_audit=None):
    data = json.loads((dataset / "dataset.json").read_text())
    source_audit = json.loads(source_audit_path.read_text())
    labels = {x["task_id"]: x for x in json.loads(labels_path.read_text())["tasks"]}
    records = []
    for slug, ident, java, maven, gradle in PILOT:
        task = next(t for t in data["reference_tasks"] if t["task_id"] == ident)
        destination = output / slug
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "reference-task.json", task)
        commands = selected_commands(task)
        runtime = {"java_major": java, "maven_version": maven, "gradle_version": gradle,
                   "original_environment_and_commands": task["environment_and_commands"],
                   "image_availability": "not_inspected_by_readonly_requirements_preparation"}
        if slug == "spring-ws":
            runtime.update(test_java_major=17, javac_release=17,
                           limitation="AcceptanceTask v1 java_major constrains launcher only; separate Test JVM conformance still needs a supported evidence field.")
        index = capture_configs(dataset, task, source_audit, destination)
        write_json(destination / "source-config-index.json", index)
        recovery = recovery_reference(task, index, checkout_audit)
        if recovery:
            write_json(destination / "source-recovery-reference.json", recovery)
        declarations = []
        for ref in index["files"]:
            if ref["source_path"].endswith("pom.xml"):
                declarations.append({"source": ref, **pom_declarations((destination / ref["path"]).read_bytes())})
        write_json(destination / "pom-declarations.json", declarations)
        row, review_ref = research_row(dataset, task)
        write_json(destination / "archived-review-row.json", {"source_ref": review_ref, "row": row})
        logs, errors = selected_logs(dataset, task, row)
        if errors or not logs:
            raise ValueError("Cannot bind selected CI logs for " + slug + ": " + str(errors))
        runtime["archived_observations"] = runtime_observations(logs)
        log_rows = []
        for number, log in enumerate(logs, 1):
            path = destination / "ci" / ("selected-job-" + str(number) + ".log")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(log["text"])
            log_rows.append({"raw_source": log["ref"], "decoded_log": file_ref(str(path)),
                             "native_events": native_events(log["text"]),
                             "membership": "CI job observation only; command scope review needed"})
        write_json(destination / "ci-job-inventory.json", log_rows)
        write_json(destination / "metadata-requests.json", metadata_requests(task, commands, runtime, destination))
        if slug == "commons-dbutils":
            readiness = copy_complete_definition(task, old, destination)
        else:
            frozen = {"schema_version": 1, "repo": task["repo"], "sha": task["sha"], "steps": [
                {"id": "ci-step-" + str(i), "runner": task["tool"], "argv": c["argv"], "cwd": c["cwd"], "java_major": java,
                 **({"maven_version": maven} if maven else {})} for i, c in enumerate(commands, 1)]}
            write_json(destination / "task.json", frozen)
            readiness = {"status": "review_required", "basis": "pinned configuration and selected CI bytes; effective task plan not yet resolved",
                         "canonical_task_sha256": canonical_digest(frozen), "task": file_ref(str(destination / "task.json")), "requirements": None}
        scope_review = review_scope(slug, task, commands, destination, index, declarations, log_rows)
        write_json(destination / "scope-review.json", scope_review)
        record = {"project_id": slug, "task_id": ident, "repo": task["repo"], "sha": task["sha"],
                  "official_ci_url": task["official_ci_url"], "ci_identity": task["ci_identity"],
                  "commands": commands, "runtime": runtime, "readiness": readiness,
                  "source_config_count": len(index["files"]), "pom_count": len(declarations),
                  "source_config_index": file_ref(str(destination / "source-config-index.json")),
                  "metadata_request": file_ref(str(destination / "metadata-requests.json")),
                  "scope_review": file_ref(str(destination / "scope-review.json")),
                  "source_recovery": file_ref(str(destination / "source-recovery-reference.json")) if recovery else None,
                  "old20_overlap": labels[ident]["old20_overlap"],
                  "next_gate": "reference_replay_and_resource_plan" if readiness["status"] == "complete" else "effective_configuration_and_requirement_review"}
        write_json(destination / "preparation.json", record)
        records.append(record)
    parents = []
    for receipt in sorted((output / "parent-poms").glob("*.receipt.json")):
        value = json.loads(receipt.read_text())
        path = output / "parent-poms" / value["file"]
        if file_ref(str(path))["sha256"] != value["sha256"] or path.stat().st_size != value["bytes"]:
            raise ValueError("Official parent source differs from its download receipt")
        parents.append({"source_ref": file_ref(str(path)), "receipt_ref": file_ref(str(receipt)),
                        "coordinates": value["coordinates"], "url": value["url"],
                        "declarations": pom_declarations(path.read_bytes())})
    write_json(output / "parent-pom-index.json", parents)
    result = {"schema": "pinned-pilot-requirements-preparation-v1", "execution_performed": False,
              "dataset_ref": file_ref(str(dataset / "dataset.json")), "source_audit_ref": file_ref(str(source_audit_path)),
              "labels_ref": file_ref(str(labels_path)), "script_ref": file_ref(__file__),
              "checkout_audit_ref": file_ref(str(checkout_audit)) if checkout_audit else None,
              "parent_pom_index": file_ref(str(output / "parent-pom-index.json")), "projects": records}
    write_json(output / "manifest.json", result)
    lines = ["# Eight fixed pilot tasks: requirements preparation", "",
             "This package preserves the eight task IDs chosen before new runs. It is source/configuration review plus requests for a later metadata phase. No project code, Maven/Gradle command, build, test, model or Docker operation was executed by this preparation.", "",
             "Only DbUtils currently has a complete requirements-v2 definition. The remaining seven tasks are intentionally not supplied with a runnable-looking incomplete requirements.json. Their full source-bound review and exact metadata requests state the gates needed before freezing one. Neither definition readiness nor metadata capture is a CI replay or a successful SAG run.", "",
             "| Project | Frozen launcher | Commands | Source POMs | Requirements |", "|---|---|---:|---:|---|"]
    for row in records:
        runtime = row["runtime"]
        launcher = f"Java {runtime['java_major']} / " + ("Maven " + runtime["maven_version"] if runtime["maven_version"] else "Gradle " + runtime["gradle_version"])
        lines.append(f"| [{row['project_id']}]({row['project_id']}/scope-review.json) | {launcher} | {len(row['commands'])} | {row['pom_count']} | {row['readiness']['status']} |")
    lines += ["", "Source POM count is an inventory, not a task-module denominator. Commons Math has seventeen archived POMs while the selected original CI reports fourteen reactor modules. Gradle projects are reviewed through settings/build logic, not POM count.", "",
              "Gson correction: its selected task is one `mvn verify javadoc:jar` invocation. The previous pilot proposal described multiple steps; no additional step was added here. Google Java Format genuinely has two commands: install with upstream skips, then required test without inheriting those skips.", "",
              "CSV uses Java21 and DBCP Java8. Their earlier same-SHA Java17 effective models are retained only as context, never transferred to these cells. The published commons-parent105→apache39 and commons-parent97→apache37 POM chains have been fetched read-only from fixed Maven Central coordinates and hashed; these source POMs are not effective models.", "",
              "Spring WS requires Gradle launcher25 and Test launcher17 with toolchainVersion=17 in the CI user-home Gradle properties. Existing task-v1 java_major represents only the launcher. This verifier/configuration gap must be resolved before complete. Avatica's archived Windows gradlew.bat differs from the pinned Git blob only in line endings. Its source-recovery-reference.json links the separately recovered exact blob, verifies both original and recovered hashes, and preserves original bytes. This is not a Linux launcher failure or a readiness upgrade.", "",
              "Each project contains the unchanged dataset reference task, a literal task.json, complete selected CI-job inventory (observations, not automatic requirements), pinned configuration copies with Git-blob comparison, source POM declarations, scope-review.json, and metadata-requests.json. Metadata requests preserve each command's flags/properties independently and never run as part of this script. A future capture must review hidden .mvn/MAVEN_ARGS inputs, isolate its cache from all agent arms, and retain runtime/HEAD/settings/command receipts.", "",
              "Configuration copies and source overlays are archived bytes, not a complete Git checkout. Later capture/replay must materialize the real exact-SHA Git checkout, including required pinned submodules, or independently verify an equivalent reconstruction. Never synthesize a commit and label it as the upstream SHA. Original archive transformations remain disclosed even if a separate exact-blob overlay repairs them.", "",
              "Resource/image availability remains for the parent task's execution plan; this preparation did not inspect Docker. Exact versions and any split test runtime are recorded per project. Public source download receipts are in parent-poms; no CI reports were newly collected here.", "",
              "Reproduce offline from retained inputs: `PYTHONPATH=. uv run --offline --no-sync python scripts/prepare_pilot_requirements.py --dataset output/java-benchmark-20260916 --source-audit output/java-benchmark-rescreen-20260922/source-audit/audit.json --labels output/java-benchmark-rescreen-20260922/requirement-audit/audit.json --old-requirements output/sag-benchmark20-requirements-validated-20260922 --checkout-audit output/java-benchmark-acquisition-20260922/checkout/audit.json --output output/java-benchmark-acquisition-20260922/pilot-requirements`.", ""]
    (output / "README.md").write_text("\n".join(lines))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--source-audit", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--old-requirements", type=Path, required=True)
    p.add_argument("--checkout-audit", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = prepare(a.dataset.resolve(), a.source_audit.resolve(), a.labels.resolve(), a.old_requirements.resolve(), a.output.resolve(), a.checkout_audit.resolve() if a.checkout_audit else None)
    print(json.dumps({r["project_id"]: {"readiness": r["readiness"]["status"], "config_files": r["source_config_count"], "poms": r["pom_count"]} for r in result["projects"]}))


if __name__ == "__main__":
    main()
