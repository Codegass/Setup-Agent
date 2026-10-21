#!/usr/bin/env python3
"""Freeze review-required benchmark definitions from the acquired Java dataset.

A review-required definition holds everything the portable harness needs to
run an agent and record the task outcome: the frozen task, a requirements
draft whose annotation_completeness is "review_required", the official CI
target, the archived CI evidence and a metadata-only effective POM capture.
Scoring keeps the task result and leaves the requirement groups unavailable.
A later source-bound review can promote a definition without rerunning it.

Subcommands, in order:
  select   list eligible Maven tasks with their extracted Java/Maven versions
  prepare  write task.json, the requirements bundle and the metadata scripts
  capture  run one metadata container per JDK major, then write the status
  build    build requirements.json, ci-target.json, sources/ and freeze.json
  verify   reload every definition the way the harness does

Nothing here runs a lifecycle goal. Docker and network are used only by
capture, for git fetch, Maven downloads and maven-help-plugin goals.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "output/java-benchmark-20260916"
DEFAULT_TWENTY = ROOT / "output/three-harness-twenty-20260924/formal/definitions"
DEFAULT_OUT = ROOT / "output/benchmark-fifty-20261021"
OBSERVER_DEFINITION = "commons-csv"
TIER = "review_required"
DEFAULT_MAVEN = "3.9.16"
HELP = "org.apache.maven.plugins:maven-help-plugin:3.5.2:"
GOALS = ("effective-pom", "effective-settings")
CACHE_POLICY = "isolated_metadata_only_not_shared_with_agent"
LAUNCHERS = ("mvn", "./mvnw")
SHELL_TOKENS = {"&&", "||", "|", ";", "&"}
JAVA_MIN, JAVA_MAX = 8, 26
CHECKOUTS = "/worktrees"
METADATA = "/metadata"
FETCH_TIMEOUT = 600
COMMAND_TIMEOUT = 900
PLATFORM = "linux/amd64"
DEFINITION_FILES = {"task.json", "requirements.json", "ci-target.json", "ci", "sources", "freeze.json"}
BUILT_FILES = ("requirements.json", "ci-target.json", "freeze.json", "ci", "sources", "tasks")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RUNTIME_MAVEN = re.compile(r"Apache Maven\s+(\d+\.\d+\.\d+)(?:\s|$)")
RUNTIME_JAVA = re.compile(r"Java version:\s*(?:1\.)?(\d+)(?:[.,\s]|$)")


# ------------------------------------------------------------------ helpers

def now():
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    return sha256_bytes(Path(path).read_bytes())


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def write_once(path, raw):
    """Create a frozen file, or confirm it already holds the same bytes."""
    path = Path(path)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"{path} already exists with different content; it is frozen")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def file_ref(path, base):
    path = Path(path)
    raw = path.read_bytes()
    return {"path": path.resolve().relative_to(Path(base).resolve()).as_posix(),
            "sha256": sha256_bytes(raw), "bytes": len(raw)}


def tool_identity():
    """The script bytes, and whether the recorded commit really contains them."""
    def git(*argv):
        try:
            return subprocess.run(["git", "-C", str(ROOT), *argv], capture_output=True, text=True, timeout=20,
                                  check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    commit = git("rev-parse", "HEAD") or None
    changed = bool(git("status", "--porcelain", "--", "scripts/freeze_benchmark_definitions.py"))
    return {"script": "scripts/freeze_benchmark_definitions.py", "script_sha256": file_sha(__file__),
            "git_commit": commit, "script_differs_from_commit": changed if commit else None}


def canonical_task_digest(task):
    from sag.benchmark.requirements import canonical_digest, normalize_task
    return canonical_digest(normalize_task(task))


def literal_relative(path):
    return (isinstance(path, str) and bool(path) and not posixpath.isabs(path)
            and posixpath.normpath(path) == path and path != ".." and not path.startswith("../"))


# --------------------------------------------------------- command extraction

def literal_argv(text):
    """The prepare_pilot_requirements rule, plus glued separators and redirects."""
    if "\n" in text or "\r" in text:
        raise ValueError("Not a literal command: a multi-line shell script needs review")
    argv = shlex.split(text)
    if not argv or any(x in SHELL_TOKENS or "$" in x or "`" in x for x in argv):
        raise ValueError("Not a literal command: shell operators, expansion or substitution need review")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    if any(token and set(token) <= set("();<>|&") for token in lexer):
        raise ValueError("Not a literal command: shell separators or redirection need review")
    return argv


def literal_steps(config):
    """Ordered literal commands from steps | command | actual_commands | commands."""
    if not isinstance(config, dict):
        raise ValueError("environment_and_commands is not a mapping; no literal command")
    key = next((k for k in ("steps", "command", "actual_commands", "commands") if k in config), None)
    if key is None:
        raise ValueError("No literal command field (steps, command, actual_commands or commands)")
    values = [config[key]] if key == "command" else config[key]
    if not isinstance(values, list) or not values:
        raise ValueError("No literal command in " + key)
    default_cwd = next((config[k] for k in ("cwd", "working_directory") if isinstance(config.get(k), str)), ".")
    projection = config.get("projection") if isinstance(config.get("projection"), dict) else {}
    steps = []
    for value in values:
        row = {"command": value} if isinstance(value, str) else value
        if not isinstance(row, dict):
            raise ValueError("Command row is not a literal command")
        text = row.get("command") or row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Command row has no literal command text")
        step = {"argv": literal_argv(text), "cwd": row.get("cwd", default_cwd), "text": text, "source": key}
        # The dataset's reviewed non-publishing projection (e.g. deploy -> install).
        if (isinstance(projection.get("benchmark_command"), str)
                and text.strip() == str(projection.get("official_command", "")).strip()):
            step.update(argv=literal_argv(projection["benchmark_command"]), text=projection["benchmark_command"],
                        source=key + " via projection.benchmark_command", projected_from=text)
        steps.append(step)
    return steps


PUBLISHING = {"deploy", "site-deploy", "deploy:deploy", "deploy:deploy-file", "release:prepare",
              "release:perform", "release:stage", "site:deploy", "site:stage-deploy"}


def publishing_goals(argv):
    """Goals that publish to a remote repository; agents are told never to publish."""
    from scripts.build_benchmark_requirements import maven_goals, normalize_goal
    return [goal for goal in maven_goals(argv) if goal in PUBLISHING or normalize_goal(goal) in PUBLISHING]


# --------------------------------------------------------- version extraction

JAVA_KEYS = {"jdk", "java", "java_version", "java-version", "jdk_version", "jdk_versions"}
MAVEN_KEYS = {"maven", "maven_version", "maven-version"}
# Alternative cells, URLs, line numbers and revision notes are not this task's runtime.
SKIPPED_KEYS = {"selection_comparison", "zip_member", "archive", "url", "run_url", "job_url",
                "workflow_path", "workflow_name", "source_revision", "line", "log_job_binding"}
JAVA_LINES = (
    ("maven_runtime", re.compile(r"\bJava version:\s*((?:1\.)?\d+)")),
    ("setup_java", re.compile(r"(?i)\bjava-version:\s*(1\.\d+|\d+)(?!\d)")),
    ("java_home", re.compile(r"\bJAVA_HOME(?:_\w+)?:\s*\S*?(?:_jdk|/jdk)/(1\.\d+|\d+)(?=[.\-/])")),
)
JAVA_VENDOR = re.compile(r"(?i)(?:temurin|zulu|adopt(?:openjdk)?|liberica|corretto|microsoft|semeru|oracle|jdk|java)"
                         r"[\s_-]?(1\.8|\d{1,2})(?=[.+\-_\s;,)]|$)")
JAVA_TOOLCACHE = re.compile(r"(?:_jdk|/jdk)/(1\.\d+|\d+)(?=[.\-/])")
JAVA_BARE = re.compile(r"^\s*(1\.\d+|\d{1,2})(?:\.\d+)*(?:[+\-][\w.]+)?\s*$")
JDK_LABEL = re.compile(r"\bJDK\s?(\d{1,2})\b")
APACHE_MAVEN = re.compile(r"\bApache Maven (\d+\.\d+\.\d+(?:-[A-Za-z][\w.-]*)?)(?=[\s(]|$)")
MAVEN_LINE = re.compile(r"(?i)\bmaven-version:\s*(\d+\.\d+\.\d+(?:-[A-Za-z][\w.-]*)?)")
MAVEN_VALUE = re.compile(r"(\d+\.\d+\.\d+(?:-[A-Za-z][\w.-]*)?)")


def normalize_major(value):
    """"1.8" is Java 8; "17.0.19" is Java 17."""
    parts = str(value).split(".")
    return int(parts[1]) if parts[0] == "1" and len(parts) > 1 else int(parts[0])


def _strings(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in SKIPPED_KEYS:
                continue
            yield from _strings(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _strings(child, path + (f"[{index}]",))
    elif isinstance(value, str):
        yield path, value


def _label(path):
    return "".join(p if p.startswith("[") else ("/" + p) for p in path).lstrip("/") or "."


def _build_tools(value, path=()):
    if isinstance(value, dict):
        tool = value.get("build_tool")
        if (isinstance(tool, dict) and str(tool.get("name", "")).lower() == "maven"
                and isinstance(tool.get("version"), str)):
            yield path + ("build_tool", "version"), tool["version"]
        for key, child in value.items():
            if key not in SKIPPED_KEYS:
                yield from _build_tools(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _build_tools(child, path + (f"[{index}]",))


def extract_versions(config):
    """Java major and Maven version, each with the strings it came from.

    Every matching statement is collected. Disagreement is a problem to
    review, never resolved by picking one value.
    """
    java, maven = [], []

    def add(rows, path, kind, text, value):
        rows.append({"source": _label(path), "kind": kind, "text": text, "value": value})

    for path, text in _strings(config):
        keys = [p.lower() for p in path if not p.startswith("[")]
        for kind, pattern in JAVA_LINES:
            for match in pattern.finditer(text):
                add(java, path, kind, text, match[1])
        if any(k in JAVA_KEYS for k in keys):
            found = [m[1] for m in JAVA_VENDOR.finditer(text)] + [m[1] for m in JAVA_TOOLCACHE.finditer(text)]
            bare = JAVA_BARE.match(text)
            if bare:
                found.append(bare[1])
            for value in dict.fromkeys(found):
                add(java, path, "jdk_key", text, value)
        if keys and keys[-1] == "job_name":
            for match in JDK_LABEL.finditer(text):
                add(java, path, "job_name", text, match[1])
        for match in APACHE_MAVEN.finditer(text):
            add(maven, path, "maven_runtime", text, match[1])
        for match in MAVEN_LINE.finditer(text):
            add(maven, path, "setup_maven", text, match[1])
        if keys and keys[-1] in MAVEN_KEYS:
            for match in MAVEN_VALUE.finditer(text):
                add(maven, path, "maven_key", text, match[1])
    for path, text in _build_tools(config):
        for match in MAVEN_VALUE.finditer(text):
            add(maven, path, "build_tool_key", text, match[1])
    for row in java:
        row["major"] = normalize_major(row["value"])
    problems = []
    majors = {row["major"] for row in java}
    java_major = None
    if not majors:
        problems.append("java_major_not_found")
    elif len(majors) > 1:
        problems.append("java_major_conflict")
    elif not JAVA_MIN <= next(iter(majors)) <= JAVA_MAX:
        problems.append("java_major_out_of_range")
    else:
        java_major = next(iter(majors))
    versions = {row["value"] for row in maven}
    maven_version = None
    if len(versions) > 1:
        problems.append("maven_version_conflict")
    elif versions:
        value = next(iter(versions))
        if re.fullmatch(r"\d+\.\d+\.\d+", value):
            maven_version = value
        else:
            problems.append("maven_version_unsupported")
    return {"java_major": java_major, "java_evidence": java, "maven_version": maven_version,
            "maven_evidence": maven, "problems": problems}


# ------------------------------------------------------------ frozen task

def frozen_task(repo, sha, steps, java_major, maven_version):
    from sag.benchmark.requirements import normalize_task
    task = {"schema_version": 1, "repo": repo, "sha": sha, "steps": []}
    for index, step in enumerate(steps, 1):
        item = {"id": f"ci-step-{index}", "runner": "maven", "argv": list(step["argv"]),
                "cwd": step.get("cwd", "."), "java_major": java_major}
        if maven_version is not None:
            item["maven_version"] = maven_version
        task["steps"].append(item)
    return normalize_task(task)


def manifest_steps(task, *, environment):
    """Task steps plus the legacy fields build_project and the request read."""
    from scripts.build_benchmark_requirements import skip_tests
    return [{**step, "stages": ["build"] if skip_tests(step["argv"]) else ["build", "test"],
             "environment": dict(environment), "expected_artifacts": [], "requires_native_image": False}
            for step in task["steps"]]


def preparation_request(project_id, task, steps):
    """The request fields attach_preparation_inventory checks against the task."""
    return {"project_id": project_id, "repo": task["repo"], "commit": task["sha"],
            "original_task": steps[0], "original_steps": steps, "cache_policy": CACHE_POLICY}


# ------------------------------------------------------- metadata capture

def metadata_plan(project_id, task, *, batch):
    """Exact commands the container runs, recorded once and reused for the status."""
    from scripts.requirements_metadata_inventory import _metadata_options
    checkout = f"{CHECKOUTS}/{project_id}"
    steps, versions = [], set()
    for step in task["steps"]:
        target = project_id if len(task["steps"]) == 1 else f"{project_id}/{step['id']}"
        if step["argv"][0] == "mvn":
            version = step.get("maven_version") or DEFAULT_MAVEN
            versions.add(version)
            launcher = f"/opt/apache-maven-{version}/bin/mvn"
        elif step["argv"][0] == "./mvnw":
            launcher = str(PurePosixPath(checkout) / "mvnw")
        else:
            raise ValueError("Only mvn and ./mvnw launchers have a metadata preparation")
        workspace = str(PurePosixPath(checkout) / step["cwd"])
        options = _metadata_options(step["argv"])
        commands = [{"goal": goal, "cwd": workspace, "log": f"{target}/{goal}.log", "output": f"{target}/{goal}.xml",
                     "argv": [launcher, *options, HELP + goal, "-Dverbose", f"-Doutput={METADATA}/{target}/{goal}.xml"]}
                    for goal in GOALS]
        steps.append({"id": step["id"], "frozen_argv": step["argv"], "cwd": step["cwd"],
                      "java_major": step["java_major"], "maven_version": step.get("maven_version"),
                      "launcher": launcher, "workspace": workspace, "directory": target,
                      "metadata_options": options,
                      "runtime_probe": {"argv": [launcher, "--version"], "cwd": checkout, "log": f"{target}/runtime.txt"},
                      "commands": commands})
    return {"project_id": project_id, "repo": task["repo"], "commit": task["sha"], "batch": batch,
            "checkout": checkout, "directory": project_id, "maven_versions": sorted(versions),
            "source_commands": [
                {"name": "fetch", "argv": ["git", "-C", checkout, "fetch", "--depth", "1", "origin", task["sha"]],
                 "cwd": "/", "log": f"{project_id}/fetch.log", "timeout_seconds": FETCH_TIMEOUT},
                {"name": "checkout", "argv": ["git", "-C", checkout, "checkout", "--detach", "FETCH_HEAD"],
                 "cwd": "/", "log": f"{project_id}/checkout.log"},
            ],
            "receipts": f"{project_id}/receipts.tsv", "steps": steps}


def maven_url(version):
    # The zip, not the tar.gz: GNU tar 1.35 in the Ubuntu 26.04 Temurin images
    # fails every extraction under amd64 emulation ("Function not implemented").
    return (f"https://archive.apache.org/dist/maven/maven-{version.split('.')[0]}/{version}"
            f"/binaries/apache-maven-{version}-bin.zip")


def preparation_script(batch, plans):
    """One shell script per JDK container, mirroring the surviving 2026-09-22 script.

    Each command's exit status and time go to receipts.tsv, so a failed
    effective POM is a recorded gap and the batch continues.
    """
    tools = f"{METADATA}/{batch}-tools"
    lines = [
        "#!/bin/sh",
        f"# Metadata-only preparation, batch {batch}; generated by scripts/freeze_benchmark_definitions.py.",
        "# It fetches each pinned commit and asks Maven only for the effective POM and settings.",
        "set -u",
        "now() { date +%s.%N; }",
        "receipt() { printf '%s\\t%s\\t%s\\t%s\\n' \"$2\" \"$3\" \"$4\" \"$(now)\" >>\"$1\"; }",
        f"mkdir -p {CHECKOUTS}",
        f": >{tools}.tsv",
        "started=$(now)",
        f"apt-get update >{tools}.log 2>&1 && apt-get install -y --no-install-recommends git ca-certificates curl"
        f" unzip >>{tools}.log 2>&1",
        f"receipt {tools}.tsv tools $? \"$started\"",
    ]
    for version in sorted({v for plan in plans for v in plan["maven_versions"]}):
        archive, url = f"/opt/apache-maven-{version}-bin.zip", maven_url(version)
        lines += [
            "started=$(now)",
            f"curl -fsSL --retry 3 -o {archive} {url} >>{tools}.log 2>&1"
            f" && curl -fsSL --retry 3 -o {archive}.sha512 {url}.sha512 >>{tools}.log 2>&1"
            f" && test \"$(cut -d' ' -f1 {archive}.sha512)\" = \"$(sha512sum {archive} | cut -d' ' -f1)\""
            f" && unzip -q -o {archive} -d /opt",
            f"receipt {tools}.tsv maven-{version} $? \"$started\"",
        ]
    for plan in plans:
        checkout, base = plan["checkout"], f"{METADATA}/{plan['directory']}"
        receipts = f"{METADATA}/{plan['receipts']}"
        fetch, attach = plan["source_commands"]
        lines += [
            f"# {plan['project_id']}: {plan['repo']} at {plan['commit']}",
            f"mkdir -p {base}",
            f": >{receipts}",
            f"git init -q {checkout}",
            f"git -C {checkout} remote add origin https://github.com/{plan['repo']}.git",
            "started=$(now)",
            f"timeout {fetch['timeout_seconds']} {shlex.join(fetch['argv'])} >{METADATA}/{fetch['log']} 2>&1",
            f"receipt {receipts} fetch $? \"$started\"",
            "started=$(now)",
            f"{shlex.join(attach['argv'])} >{METADATA}/{attach['log']} 2>&1",
            f"receipt {receipts} checkout $? \"$started\"",
            f"git -C {checkout} rev-parse HEAD >{base}/HEAD.txt",
            f"cd {checkout}",
            "env | grep -E '^(JAVA_HOME|MAVEN_OPTS|MAVEN_ARGS|MAVEN_CONFIG|MAVEN_SKIP_RC|JAVA_TOOL_OPTIONS|"
            f"JDK_JAVA_OPTIONS|_JAVA_OPTIONS)=' | sort >{base}/metadata-environment.txt",
        ]
        for step in plan["steps"]:
            probe = step["runtime_probe"]
            if step["directory"] != plan["directory"]:
                lines.append(f"mkdir -p {METADATA}/{step['directory']}")
            lines += [
                f"cd {probe['cwd']}",
                "started=$(now)",
                f"{shlex.join(probe['argv'])} >{METADATA}/{probe['log']} 2>&1",
                f"receipt {receipts} {step['id']}:runtime_probe $? \"$started\"",
            ]
            for command in step["commands"]:
                lines += [
                    f"cd {command['cwd']}",
                    "started=$(now)",
                    f"timeout {COMMAND_TIMEOUT} {shlex.join(command['argv'])} >{METADATA}/{command['log']} 2>&1",
                    f"receipt {receipts} {step['id']}:{command['goal']} $? \"$started\"",
                ]
        lines += [
            f"cd {checkout}",
            f"git diff --name-only HEAD -- >{base}/tracked-diff.txt",
            f"git ls-files --others --exclude-standard >{base}/untracked.txt",
            f"find /root/.m2/repository -name \"*.pom\" -o -name \"*.jar\" | sort >{base}/metadata-repository-files.txt",
            "cd /",
        ]
    return "\n".join(lines) + "\n"


def read_receipts(path):
    rows = {}
    if not Path(path).is_file():
        return rows
    for line in Path(path).read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, code, started, finished = parts
        try:
            rows[name] = {"exit_code": int(code), "started": float(started), "finished": float(finished)}
        except ValueError:
            continue
    return rows


def metadata_status(preparation, plan, *, script_name, image):
    """Write <project>/metadata-status.json with the fields the importer reads.

    Field names follow the original metadata runner (2026-09-22) and
    attach_preparation_inventory: a step is captured_review_required only
    after a matching runtime probe and two successful help-plugin commands.
    """
    preparation = Path(preparation)
    receipts = read_receipts(preparation / plan["receipts"])

    def ref(relative):
        path = preparation / relative
        return file_ref(path, preparation) if path.is_file() else None

    def command(name, argv, cwd, log, timeout=None):
        row = receipts.get(name)
        code = row["exit_code"] if row else None
        item = {"argv": argv, "cwd": cwd, "exit_code": code,
                "status": "not_run" if row is None else "timeout" if code == 124 else "completed",
                "elapsed_seconds": round(row["finished"] - row["started"], 3) if row else None, "log": ref(log)}
        if timeout:
            item["timeout_seconds"] = timeout
        return item

    state = {"project": plan["project_id"], "repo": plan["repo"], "commit": plan["commit"], "status": "preparing",
             "metadata_only": True, "preparation_script": ref(script_name), "batch": plan["batch"], "image": image,
             "commands": [command(c["name"], c["argv"], c["cwd"], c["log"], c.get("timeout_seconds"))
                          for c in plan["source_commands"]],
             "steps": [], "receipts": ref(plan["receipts"]), "written_at": now()}
    head = preparation / plan["directory"] / "HEAD.txt"
    source_ok = (all(c["status"] == "completed" and c["exit_code"] == 0 for c in state["commands"])
                 and head.is_file() and head.read_text().strip() == plan["commit"])
    environment = {}
    environment_path = preparation / plan["directory"] / "metadata-environment.txt"
    if environment_path.is_file():
        for line in environment_path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                environment[key] = value
    for step in plan["steps"]:
        probe_plan = step["runtime_probe"]
        probe = command(f"{step['id']}:runtime_probe", probe_plan["argv"], probe_plan["cwd"], probe_plan["log"])
        log = preparation / probe_plan["log"]
        text = ANSI.sub("", log.read_text(errors="replace")) if log.is_file() else ""
        maven, java = RUNTIME_MAVEN.search(text), RUNTIME_JAVA.search(text)
        result = {"id": step["id"], "frozen_argv": step["frozen_argv"], "java_major": step["java_major"],
                  "maven_version_constraint": step["maven_version"], "status": None,
                  "metadata_environment": environment, "launcher": step["launcher"], "runtime_probe": probe,
                  "observed_maven_version": maven[1] if maven else None,
                  "observed_java_major": int(java[1]) if java else None,
                  "metadata_options": step["metadata_options"],
                  "commands": [command(f"{step['id']}:{c['goal']}", c["argv"], c["cwd"], c["log"], COMMAND_TIMEOUT)
                               for c in step["commands"]]}
        outputs = {c["goal"]: ref(c["output"]) for c in step["commands"]}
        substitute = bool((image or {}).get("substitute"))
        succeeded = (probe["status"] == "completed" and probe["exit_code"] == 0 and probe["log"] is not None
                     and all(c["status"] == "completed" and c["exit_code"] == 0 and c["log"] is not None
                             for c in result["commands"])
                     and all(value is not None for value in outputs.values()))
        if not source_ok:
            result["status"] = "source_unavailable"
        elif substitute and succeeded and result["observed_java_major"] == image.get("used_java_major"):
            # Recorded for a reviewer, never imported as this task's capture.
            result["status"] = "captured_with_substitute_jdk"
            result["substitute_outputs"] = outputs
        elif (probe["status"] != "completed" or probe["exit_code"] != 0 or probe["log"] is None
              or result["observed_java_major"] != step["java_major"] or result["observed_maven_version"] is None
              or step["maven_version"] and result["observed_maven_version"] != step["maven_version"]):
            result["status"] = "runtime_probe_failed_or_mismatch"
        elif (any(c["status"] != "completed" or c["exit_code"] != 0 or c["log"] is None for c in result["commands"])
              or any(value is None for value in outputs.values())):
            result["status"] = "metadata_capture_failed"
        else:
            result["status"] = "captured_review_required"
            result["outputs"] = outputs
        state["steps"].append(result)
    if not source_ok:
        state["status"] = "source_fetch_unavailable"
    elif all(s["status"] == "captured_review_required" for s in state["steps"]):
        state["status"] = "captured_review_required"
    else:
        state["status"] = "incomplete_metadata"
    write_json(preparation / plan["directory"] / "metadata-status.json", state)
    return state


# ------------------------------------------------------------- CI target

LOG_TOTAL = re.compile(r"^\[(?:INFO|WARNING|ERROR)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), "
                       r"Skipped: (\d+)(?:, Flakes: \d+)?\s*$", re.M)
COUNT_FIELDS = ("reported_total", "failures", "errors", "skipped")


def log_test_totals(raw):
    """Sum of aggregate Surefire/Failsafe summaries; per-class lines are excluded."""
    from sag.benchmark.ci_native import ci_native_text
    rows = LOG_TOTAL.findall(ci_native_text(raw))
    if not rows:
        return None
    total = [sum(int(row[i]) for row in rows) for i in range(4)]
    return {"summary_lines": len(rows), **dict(zip(COUNT_FIELDS, total))}


def dataset_totals(rows):
    if not rows:
        return None
    counts = [row.get("counts", {}) for row in rows]
    if any(not all(isinstance(c.get(k), int) for k in COUNT_FIELDS) for c in counts):
        return None
    return {"invocations": len(counts), **{k: sum(c[k] for c in counts) for k in COUNT_FIELDS}}


def _describe(counts):
    return (f"reported {counts['reported_total']}, failures {counts['failures']}, errors {counts['errors']}, "
            f"skipped {counts['skipped']}")


def ci_target(task, *, cell_id, ci_url, logs_url, build, dataset_counts, log_counts, module_count, harvested_at):
    """Offline TargetRecord, as prepare_pilot_commons_reviews.py builds one.

    Counts come from the dataset. When the archived log disagrees (or has no
    aggregate summary), both are recorded and no count is asserted.
    """
    from sag.metrics.target_record import TargetRecord
    agree = bool(dataset_counts and log_counts
                 and all(log_counts.get(k) == dataset_counts.get(k) for k in COUNT_FIELDS))
    notes = ["Review-required tier: prepared offline from the archived official job log and the acquired dataset. "
             "harvested_at records this offline construction, not a new CI download."]
    notes.append("Dataset counts for the selected Maven invocation: "
                 + (_describe(dataset_counts) + "." if dataset_counts else "none recorded."))
    notes.append("Archived job log aggregate Tests run summaries: "
                 + (_describe(log_counts) + f" over {log_counts['summary_lines']} summary line(s)." if log_counts
                    else "none found."))
    if agree:
        executed, red, skipped = (dataset_counts["reported_total"],
                                  dataset_counts["failures"] + dataset_counts["errors"], dataset_counts["skipped"])
        notes.append("The dataset and the log agree. executed_count is the native reported total including skipped records.")
    else:
        executed = red = skipped = 0
        notes.append("The dataset and the log disagree, so no count is asserted: zero here means unavailable, "
                     "not an observed zero-test run. The schema has no grade below B, so the cell keeps grade B.")
    notes.append("Grade B: only aggregate console summaries are archived; there is no JUnit XML or complete case pool.")
    if len(task["steps"]) > 1:
        notes.append("The command field names the final frozen step; all steps: "
                     + " then ".join(shlex.join(s["argv"]) for s in task["steps"]) + ".")
    cell = {"cell_id": cell_id, "build": build, "executed_count": executed, "red_count": red, "skipped": skipped,
            "command": shlex.join(task["steps"][-1]["argv"]), "grade": "B",
            "evidence_refs": [ref for ref in (ci_url, logs_url) if ref]}
    if module_count == 1:
        cell.update(modules=["."], modules_basis="declared")
    record = TargetRecord.model_validate({"schema_version": 2, "repo": task["repo"], "sha": task["sha"],
                                          "harvested_at": harvested_at, "cells": [cell], "matched_cell": cell_id,
                                          "notes": notes})
    return record.model_dump(mode="json"), "agree" if agree else "disagree"


# ---------------------------------------------------------------- select

def _cell(cell_file, run_id, attempt, job_id):
    data = json.loads(cell_file.read_text())
    for run in data.get("runs", []):
        if run.get("run_id") == run_id and run.get("run_attempt") == attempt:
            for cell in run.get("cells", []):
                if cell.get("job_id") == job_id:
                    return run, cell
    return None, None


def candidate(task, project, *, dataset_root, twenty_repos, allow_two_steps, max_java_lines, max_modules):
    """One selection row: eligibility, reasons and everything prepare needs."""
    dataset_root = Path(dataset_root)
    repo, sha = task["repo"], task["sha"]
    reasons = []
    ci = task.get("ci_identity") or {}
    config = task.get("environment_and_commands") or {}
    row = {"project_id": repo.split("/", 1)[1], "repo": repo, "task_id": task["task_id"], "sha": sha,
           "eligible": False, "reasons": reasons, "steps": [], "java_major": None, "java_evidence": [],
           "maven_version": None, "maven_evidence": [],
           "environment": {k: v for k, v in config["environment"].items() if isinstance(v, str)}
           if isinstance(config.get("environment"), dict) else {},
           "java_physical_lines": (task.get("source_size") or {}).get("java_physical_lines"),
           "module_count": task.get("module_count"), "evidence_level": task.get("evidence_level"),
           "scope": task.get("scope"), "dataset_counts": task.get("reported_test_results_by_invocation") or [],
           "ci": {}, "source_pom": {}}
    if task["task_id"] != project.get("canonical_task_id"):
        reasons.append("not_canonical_task")
    if task.get("tool") != "maven":
        reasons.append("not_maven")
    if repo in twenty_repos:
        reasons.append("in_frozen_twenty")
    url = task.get("official_ci_url") or ""
    if (not url.startswith(f"https://github.com/{repo}/actions/runs/")
            or not all(ci.get(k) for k in ("run_id", "run_attempt", "job_id"))):
        reasons.append("ci_not_github_actions")
    else:
        relative = f"raw/actions/{repo}/runs/{ci['run_id']}/attempt-{ci['run_attempt']}/logs.zip"
        logs_zip, meta = dataset_root / relative, dataset_root / (relative + ".meta.json")
        cells = dataset_root / "actions-cell-evidence" / (repo.replace("/", "__") + ".json")
        row["ci"] = {"url": url, "run_id": ci["run_id"], "run_attempt": ci["run_attempt"], "job_id": ci["job_id"],
                     "job_name": ci.get("job_name"), "run_url": ci.get("run_url"), "logs_zip": relative,
                     "cell_evidence": cells.relative_to(dataset_root).as_posix()}
        if not logs_zip.is_file() or not meta.is_file():
            reasons.append("ci_logs_zip_missing")
        else:
            declared = json.loads(meta.read_text())
            row["ci"].update(logs_zip_sha256=file_sha(logs_zip), logs_url=declared.get("url"))
            if declared.get("sha256") != row["ci"]["logs_zip_sha256"]:
                reasons.append("ci_logs_zip_hash_mismatch")
        run, cell = _cell(cells, ci["run_id"], ci["run_attempt"], ci["job_id"]) if cells.is_file() else (None, None)
        fallback = None if cell else jobs_page_cell(dataset_root, PurePosixPath(relative).parent, config, ci["job_id"])
        if cell is None and fallback is None:
            reasons.append("ci_cell_evidence_missing")
        else:
            if cell is not None:
                members = (cell.get("log_selection") or {}).get("members") or []
                row["ci"].update(cell_evidence_sha256=file_sha(cells), cell_evidence_basis="actions_cell_summary",
                                 job_conclusion=cell.get("job_conclusion"), run_head_sha=run.get("head_sha"))
            else:
                members, job = fallback["members"], fallback["job"]
                run = {"head_sha": job.get("head_sha")}
                row["ci"].update(cell_evidence=fallback["page"].relative_to(dataset_root).as_posix(),
                                 cell_evidence_sha256=file_sha(fallback["page"]),
                                 cell_evidence_basis="run_attempt_jobs_page_with_dataset_log_binding",
                                 job_conclusion=job.get("conclusion"), run_head_sha=job.get("head_sha"))
                if job.get("name") != ci.get("job_name"):
                    reasons.append("ci_job_name_differs")
            row["ci"]["zip_member"] = members[0] if len(members) == 1 else None
            if run.get("head_sha") not in (None, sha):
                reasons.append("ci_run_revision_differs")
            if len(members) != 1:
                reasons.append("ci_log_member_ambiguous")
            elif logs_zip.is_file():
                with zipfile.ZipFile(logs_zip) as archive:
                    if members[0] not in archive.namelist():
                        reasons.append("ci_log_member_missing")
    pom_relative = f"raw/configs/{repo}/{sha}/pom.xml"
    pom, pom_meta = dataset_root / pom_relative, dataset_root / (pom_relative + ".meta.json")
    if not pom.is_file() or not pom_meta.is_file():
        reasons.append("source_pom_missing")
    else:
        declared = json.loads(pom_meta.read_text())
        raw_pom = pom.read_bytes()
        row["source_pom"] = {"path": pom_relative, "meta": pom_relative + ".meta.json", "sha256": sha256_bytes(raw_pom),
                             "url": declared.get("url"), "provenance": source_pom_provenance(declared, raw_pom, repo, sha)}
        if row["source_pom"]["provenance"] is None:
            reasons.append("source_pom_provenance_mismatch")
    try:
        steps = literal_steps(config)
    except ValueError as exc:
        steps = []
        reasons.append("command_not_literal")
        row["command_error"] = str(exc)
    row["steps"] = steps
    if len(steps) > (2 if allow_two_steps else 1):
        reasons.append("more_than_two_steps" if allow_two_steps else "more_than_one_step")
    if any(step["argv"][0] not in LAUNCHERS for step in steps):
        reasons.append("launcher_not_maven")
    if any(publishing_goals(step["argv"]) for step in steps):
        reasons.append("publishing_goal")
    if any(not literal_relative(step["cwd"]) for step in steps):
        reasons.append("cwd_not_literal")
    versions = extract_versions(config)
    row.update(java_major=versions["java_major"], java_evidence=versions["java_evidence"],
               maven_version=versions["maven_version"], maven_evidence=versions["maven_evidence"])
    reasons.extend(versions["problems"])
    lines, modules = row["java_physical_lines"], row["module_count"]
    if max_java_lines is not None and (lines is None or lines > max_java_lines):
        reasons.append("java_lines_above_cap")
    if max_modules is not None and (modules is None or modules > max_modules):
        reasons.append("modules_above_cap")
    row["eligible"] = not reasons
    return row


def git_blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def source_pom_provenance(declared, raw, repo, sha):
    """Pinned source POM bytes: a raw.githubusercontent fetch, or the contents API with its Git blob."""
    if declared.get("sha256") != sha256_bytes(raw):
        return None
    if declared.get("url") == f"https://raw.githubusercontent.com/{repo}/{sha}/pom.xml":
        return "raw_githubusercontent"
    if (declared.get("source_sha") == sha
            and declared.get("api_url") == f"https://api.github.com/repos/{repo}/contents/pom.xml?ref={sha}"
            and declared.get("blob_sha") == git_blob_sha(raw)):
        return "github_contents_api_blob"
    return None


def jobs_page_cell(dataset_root, relative_attempt, config, job_id):
    """Fallback when the cell summary lacks this job: the run attempt's jobs page
    plus the dataset task's own exact-name log binding."""
    binding = config.get("log_job_binding") if isinstance(config.get("log_job_binding"), dict) else {}
    members = binding.get("members") or []
    if config.get("job_id") != job_id or binding.get("job_binding") != "exact_job_name" or len(members) != 1:
        return None
    for page in sorted((dataset_root / relative_attempt / "jobs").glob("page-*.json")):
        meta = page.with_name(page.name + ".meta.json")
        if not meta.is_file() or json.loads(meta.read_text()).get("sha256") != file_sha(page):
            continue
        for job in json.loads(page.read_text()).get("jobs", []):
            if job.get("id") == job_id:
                return {"page": page, "job": job, "members": members}
    return None


def twenty_repositories(twenty):
    repos = set()
    for path in sorted(Path(twenty).glob("*/task.json")):
        repos.add(json.loads(path.read_text())["repo"])
    return repos


def select(dataset_root, twenty, out, *, allow_two_steps=False, max_java_lines=None, max_modules=None,
           projects=None):
    dataset_root, out = Path(dataset_root), Path(out)
    dataset_path = dataset_root / "dataset.json"
    dataset = json.loads(dataset_path.read_text())
    tasks = {t["task_id"]: t for t in dataset["reference_tasks"]}
    repos = twenty_repositories(twenty)
    rows = []
    for project in dataset["projects"]:
        task = tasks.get(project.get("canonical_task_id"))
        if task is None or task.get("tool") != "maven":
            continue
        rows.append(candidate(task, project, dataset_root=dataset_root, twenty_repos=repos,
                              allow_two_steps=allow_two_steps, max_java_lines=max_java_lines,
                              max_modules=max_modules))
    if projects:
        unknown = sorted(set(projects) - {row["project_id"] for row in rows})
        if unknown:
            raise ValueError("Requested projects are not candidates: " + ", ".join(unknown))
        for row in rows:
            if row["project_id"] not in projects:
                row["reasons"].append("not_in_requested_set")
                row["eligible"] = False
    seen = {}
    for row in rows:
        seen.setdefault(row["project_id"], []).append(row)
    for group in seen.values():
        if len(group) > 1:
            for row in group:
                row["reasons"].append("project_id_collision")
                row["eligible"] = False
    selection = {
        "schema_version": 1, "tier": TIER, "created_at": now(),
        "dataset": {"path": str(dataset_root), "sha256": file_sha(dataset_path)},
        "twenty": {"path": str(twenty), "repos": sorted(repos)},
        "criteria": {"pool": "canonical Maven reference task of each dataset project",
                     "ci": "GitHub Actions with the run attempt logs.zip and the selected cell log on disk",
                     "source_pom": "raw/configs/<repo>/<sha>/pom.xml with matching .meta.json",
                     "commands": "steps | command | actual_commands | commands, literal argv only",
                     "launchers": list(LAUNCHERS), "allow_two_steps": allow_two_steps,
                     "max_java_lines": max_java_lines, "max_modules": max_modules,
                     "java_major_range": [JAVA_MIN, JAVA_MAX],
                     "maven_version": "Apache Maven x.y.z lines or maven keys; null when absent",
                     "excluded": "projects already frozen in the twenty, by repository",
                     "requested_projects": sorted(projects) if projects else None},
        "tool": tool_identity(),
        "counts": {"candidates": len(rows), "eligible": sum(r["eligible"] for r in rows)},
        "projects": rows,
    }
    write_json(out / "selection.json", selection)
    return selection


def _evidence_text(rows):
    return " | ".join(dict.fromkeys(row["text"] for row in rows)) or "-"


def print_selection(selection, stream=sys.stdout):
    rows = selection["projects"]
    eligible = [r for r in rows if r["eligible"]]
    print(f"Candidates: {len(rows)}; eligible: {len(eligible)}", file=stream)
    print("", file=stream)
    print("| # | project | java | maven | java lines | modules | command | java evidence | maven evidence |", file=stream)
    print("|---|---|---|---|---|---|---|---|---|", file=stream)
    for index, row in enumerate(sorted(eligible, key=lambda r: r["project_id"]), 1):
        command = " ; ".join(shlex.join(s["argv"]) for s in row["steps"])
        print(f"| {index} | {row['project_id']} | {row['java_major']} | {row['maven_version'] or 'null'} | "
              f"{row['java_physical_lines']} | {row['module_count']} | `{command}` | "
              f"{_evidence_text(row['java_evidence'])} | {_evidence_text(row['maven_evidence'])} |", file=stream)
    print("", file=stream)
    print("Not eligible:", file=stream)
    for row in sorted((r for r in rows if not r["eligible"]), key=lambda r: r["project_id"]):
        print(f"- {row['project_id']}: {', '.join(row['reasons'])}", file=stream)


# --------------------------------------------------------------- prepare

def prepare_project(row, out, dataset_root, *, batch, selection_sha256, prepared_at):
    """Frozen task, bundle evidence, manifest entry, request and metadata plan."""
    dataset_root, out = Path(dataset_root), Path(out)
    project = row["project_id"]
    task = frozen_task(row["repo"], row["sha"], row["steps"], row["java_major"], row["maven_version"])
    task_bytes = json_bytes(task)
    write_once(out / "definitions" / project / "task.json", task_bytes)
    write_once(out / "bundle/tasks" / project / "task.json", task_bytes)
    bundle, ci_dir = out / "bundle", out / "bundle/ci" / project
    ci = row["ci"]
    raw_zip = (dataset_root / ci["logs_zip"]).read_bytes()
    if sha256_bytes(raw_zip) != ci["logs_zip_sha256"]:
        raise ValueError("Archived logs.zip changed since selection: " + project)
    with zipfile.ZipFile(dataset_root / ci["logs_zip"]) as archive:
        log = archive.read(ci["zip_member"])
    cells = (dataset_root / ci["cell_evidence"]).read_bytes()
    pom = (dataset_root / row["source_pom"]["path"]).read_bytes()
    if sha256_bytes(cells) != ci["cell_evidence_sha256"] or sha256_bytes(pom) != row["source_pom"]["sha256"]:
        raise ValueError("Archived CI cell evidence or source POM changed since selection: " + project)
    log_name = f"job-{ci['job_id']}.log"
    paths = {"job_log": write_once(ci_dir / log_name, log), "raw_zip": write_once(ci_dir / "logs.zip", raw_zip),
             "cell_evidence": write_once(ci_dir / ("cell-evidence.json" if ci.get("cell_evidence_basis")
                                                   == "actions_cell_summary" else "jobs-page.json"), cells),
             "source_pom": write_once(ci_dir / "source-pom.xml", pom)}
    index = {"schema_version": 1, "tier": TIER, "repo": row["repo"], "sha": row["sha"], "selected_url": ci["url"],
             "ci_identity": {k: ci[k] for k in ("run_id", "run_attempt", "job_id", "job_name")},
             "zip_member": ci["zip_member"],
             "sources": {name: file_ref(path, bundle) for name, path in paths.items()},
             "origins": {"raw_zip": {"dataset_path": ci["logs_zip"], "url": ci.get("logs_url")},
                         "cell_evidence": {"dataset_path": ci["cell_evidence"]},
                         "source_pom": {"dataset_path": row["source_pom"]["path"], "url": row["source_pom"]["url"]}},
             "scope": "The exact selected job of the archived run attempt, extracted byte for byte from logs.zip; "
                      "not the latest CI. No reviewed execution plan or count denominator is claimed."}
    write_once(ci_dir / "index.json", json_bytes(index))
    steps = manifest_steps(task, environment=row["environment"])
    entry = {
        "id": project, "repo": row["repo"], "commit": row["sha"],
        "task": {"path": f"tasks/{project}/task.json", "sha256": sha256_bytes(task_bytes)},
        "steps": steps, "scope_note": row["scope"] or "Selected official GitHub Actions cell.",
        "ci": {"selected_url": ci["url"], "selected_cell": ci["job_name"],
               "evidence_status": "official_job_log_archived_review_required", "comparison_admitted": False,
               "modules": ["."] if row["module_count"] == 1 else [],
               "evidence": [
                   {"path": f"ci/{project}/source-pom.xml", "sha256": sha256_bytes(pom),
                    "official_url": row["source_pom"]["url"]},
                   {"path": f"ci/{project}/{log_name}", "sha256": sha256_bytes(log), "official_url": ci["url"]},
               ]},
        "dataset": {"task_id": row["task_id"], "selection_sha256": selection_sha256, "prepared_at": prepared_at,
                    "ci": ci, "source_pom": row["source_pom"], "counts": row["dataset_counts"],
                    "module_count": row["module_count"], "java_physical_lines": row["java_physical_lines"],
                    "evidence_level": row["evidence_level"],
                    "extraction": {"java_major": row["java_major"], "java_evidence": row["java_evidence"],
                                   "maven_version": row["maven_version"], "maven_evidence": row["maven_evidence"],
                                   "steps": row["steps"]},
                    "batch": batch},
    }
    request = preparation_request(project, task, steps)
    write_once(out / "preparation" / f"request-{project}.json", json_bytes(request))
    return entry, metadata_plan(project, task, batch=batch)


def prepare(selection_path, out, projects, dataset_root, *, batch_label="batch"):
    out, dataset_root = Path(out), Path(dataset_root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", batch_label):
        raise ValueError("Batch label must be a short plain name")
    raw = Path(selection_path).read_bytes()
    selection = json.loads(raw)
    rows = {row["project_id"]: row for row in selection["projects"]}
    names = list(projects) if projects else sorted(n for n, r in rows.items() if r["eligible"])
    for name in names:
        if name not in rows or not rows[name]["eligible"]:
            raise ValueError(f"{name} is not an eligible project in the selection")
    manifest_path = out / "bundle/manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "schema_version": 1, "tier": TIER, "projects": []}
    entries = {entry["id"]: entry for entry in manifest["projects"]}
    batches, prepared_at = {}, now()
    already = sorted(set(names) & set(entries))
    if already:
        raise ValueError("Already prepared in this bundle: " + ", ".join(already))
    for name in names:
        row = rows[name]
        batch = f"{batch_label}-java{row['java_major']}"
        entry, plan = prepare_project(row, out, dataset_root, batch=batch, selection_sha256=sha256_bytes(raw),
                                      prepared_at=prepared_at)
        entries[name] = entry
        batches.setdefault(batch, []).append(plan)
    for batch, plans in batches.items():
        major = plans[0]["batch"].rsplit("java", 1)[1]
        script = f"prepare-{batch}.sh"
        write_once(out / "preparation" / script, preparation_script(batch, plans).encode())
        write_once(out / "preparation" / f"plan-{batch}.json", json_bytes({
            "schema_version": 1, "batch": batch, "script": script, "java_major": int(major),
            "image": f"eclipse-temurin:{major}-jdk", "platform": PLATFORM,
            "maven_versions": sorted({v for p in plans for v in p["maven_versions"]}), "projects": plans}))
    manifest.update(tier=TIER, updated_at=now(), projects=[entries[k] for k in sorted(entries)])
    write_json(manifest_path, manifest)
    return {"projects": names, "batches": {b: [p["project_id"] for p in plans] for b, plans in batches.items()}}


# ---------------------------------------------------------------- images

LTS_MAJORS = (8, 11, 17, 21, 25)


def image_candidates(major):
    """The JDK tag, the bare tag, then the Ubuntu-based Temurin variants (the script uses apt)."""
    return [f"eclipse-temurin:{major}-jdk", f"eclipse-temurin:{major}",
            *(f"eclipse-temurin:{major}-jdk-{base}" for base in ("noble", "jammy", "focal"))]


def resolve_image(major, available):
    """First available Temurin image for this Java major, else the nearest newer LTS.

    A newer-LTS image is a flagged substitute: its capture never counts as
    captured for a task pinned to another major.
    """
    tried = []
    for tag in image_candidates(major):
        tried.append(tag)
        if available(tag):
            return {"java_major": major, "requested": f"eclipse-temurin:{major}-jdk", "used": tag,
                    "substitute": False, "tried": tried}
    for lts in (m for m in LTS_MAJORS if m > major):
        for tag in image_candidates(lts):
            tried.append(tag)
            if available(tag):
                return {"java_major": major, "requested": f"eclipse-temurin:{major}-jdk", "used": tag,
                        "substitute": True, "used_java_major": lts, "tried": tried,
                        "reason": f"No eclipse-temurin image for Java {major}; the nearest newer LTS, Java {lts}, "
                                  f"was used. Its capture is recorded but is not a capture for a Java {major} task."}
    return {"java_major": major, "requested": f"eclipse-temurin:{major}-jdk", "used": None, "substitute": False,
            "tried": tried, "reason": f"No eclipse-temurin image for Java {major} or any newer LTS."}


def images(out, batches=None, *, log=print):
    """Pull one image per JDK major in the prepared batches; record the choice and any gap."""
    preparation = Path(out) / "preparation"
    resolution_path = preparation / "image-resolution.json"
    resolution = json.loads(resolution_path.read_text()) if resolution_path.exists() else {}
    for plan_path in sorted(preparation.glob("plan-*.json")):
        batch = json.loads(plan_path.read_text())
        if batches and batch["batch"] not in batches:
            continue
        major = str(batch["java_major"])
        if major in resolution and resolution[major].get("used"):
            continue

        def available(tag):
            pulled = docker(["pull", "--platform", batch["platform"], tag], timeout=1800, log=log)
            return pulled.returncode == 0
        choice = resolve_image(int(major), available)
        if choice["used"]:
            info = json.loads(docker(["image", "inspect", choice["used"]], log=log).stdout)[0]
            choice.update(id=info["Id"], repo_digests=info.get("RepoDigests", []),
                          architecture=info.get("Architecture"), os=info.get("Os"), pulled_at=now())
        resolution[major] = choice
        log(f"Java {major}: {choice['used'] or 'no image'}" + (" (substitute)" if choice["substitute"] else ""))
        write_json(resolution_path, resolution)
    notes = [f"Java {k}: {v['used']} ({v['id'][:19] if v.get('id') else 'no id'})"
             + (f" SUBSTITUTE: {v['reason']}" if v["substitute"] or not v["used"] else "")
             for k, v in sorted(resolution.items(), key=lambda kv: int(kv[0]))]
    gaps = [v for v in resolution.values() if v["substitute"] or not v["used"]]
    (preparation / "image-notes.txt").write_text(
        "Metadata capture images, one per Java major (linux/amd64).\n"
        + ("No image gaps: every major has its own eclipse-temurin JDK image.\n" if not gaps
           else f"{len(gaps)} major(s) without their own image; see SUBSTITUTE lines.\n")
        + "\n".join(notes) + "\n")
    return resolution


# --------------------------------------------------------------- capture

def docker(argv, *, timeout=120, attempts=5, wait=30, log=None):
    """Retry while the Docker daemon is unresponsive instead of skipping work."""
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0 or "Cannot connect to the Docker daemon" not in result.stderr:
                return result
            reason = result.stderr.strip()
        except subprocess.TimeoutExpired:
            reason = "docker did not answer within " + str(timeout) + " seconds"
        if log:
            log(f"docker {' '.join(argv[:2])}: {reason}; waiting {wait}s before attempt {attempt + 1}")
        time.sleep(wait)
    raise RuntimeError("Docker stayed unresponsive: " + reason)


def capture(out, batches=None, *, pull=True, log=print):
    preparation = (Path(out) / "preparation").resolve()
    results = {}
    for plan_path in sorted(preparation.glob("plan-*.json")):
        batch = json.loads(plan_path.read_text())
        if batches and batch["batch"] not in batches:
            continue
        resolution_path = preparation / "image-resolution.json"
        choice = (json.loads(resolution_path.read_text()) if resolution_path.exists() else {}).get(
            str(batch["java_major"]), {"used": batch["image"], "substitute": False})
        image = choice["used"]
        if not image:
            raise RuntimeError(f"No capture image for Java {batch['java_major']}: {choice.get('reason')}")
        if pull:
            pulled = docker(["pull", "--platform", batch["platform"], image], timeout=1800, log=log)
            if pulled.returncode:
                raise RuntimeError(f"Could not pull {image}: {pulled.stderr.strip()[-500:]}")
        inspected = docker(["image", "inspect", image], log=log)
        if inspected.returncode:
            raise RuntimeError(f"Image {image} is unavailable: {inspected.stderr.strip()[-500:]}")
        info = json.loads(inspected.stdout)[0]
        image_info = {"name": image, "id": info["Id"], "repo_digests": info.get("RepoDigests", []),
                      "architecture": info.get("Architecture"), "os": info.get("Os"), "platform": batch["platform"],
                      "requested": batch["image"], "substitute": bool(choice.get("substitute"))}
        if image_info["substitute"]:
            image_info.update(used_java_major=choice["used_java_major"], reason=choice["reason"])
        argv = ["run", "--rm", "--platform", batch["platform"], "--network", "bridge",
                "--label", "sag.freeze.batch=" + batch["batch"], "-v", f"{preparation}:{METADATA}",
                image, "sh", f"{METADATA}/{batch['script']}"]
        log_path = preparation / f"capture-{batch['batch']}.log"
        started, clock = now(), time.monotonic()
        log(f"{batch['batch']}: running {image} for {len(batch['projects'])} project(s)")
        with log_path.open("w") as stream:
            process = subprocess.run(["docker", *argv], stdout=stream, stderr=subprocess.STDOUT,
                                     timeout=1200 + len(batch["projects"]) * (FETCH_TIMEOUT + 2 * COMMAND_TIMEOUT + 300))
        receipt = {"schema_version": 1, "batch": batch["batch"], "image": image_info, "argv": ["docker", *argv],
                   "started_at": started, "finished_at": now(), "seconds": round(time.monotonic() - clock, 1),
                   "exit_code": process.returncode, "log": file_ref(log_path, preparation),
                   "tools": read_receipts(preparation / f"{batch['batch']}-tools.tsv")}
        statuses = {}
        for plan in batch["projects"]:
            state = metadata_status(preparation, plan, script_name=batch["script"], image=image_info)
            statuses[plan["project_id"]] = {"status": state["status"],
                                            "steps": {s["id"]: s["status"] for s in state["steps"]}}
        receipt["projects"] = statuses
        write_json(preparation / f"capture-{batch['batch']}.json", receipt)
        results[batch["batch"]] = receipt
        log(f"{batch['batch']}: exit {process.returncode} after {receipt['seconds']}s; "
            + ", ".join(f"{k}={v['status']}" for k, v in statuses.items()))
    return results


# ----------------------------------------------------------------- build

def observer_policy(twenty):
    """The frozen passive-observer block; the harness compares it with the image."""
    spec = json.loads((Path(twenty) / OBSERVER_DEFINITION / "requirements.json").read_text())
    return spec["maven_observer"]


def _remove(path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def build_definition(entry, out, *, repo_root, observer, rebuild=False):
    from scripts.build_benchmark_requirements import archive_source, build_project, digest, task_digest
    from scripts.requirements_metadata_inventory import attach_preparation_inventory
    from sag.benchmark.ci_count_semantics import unavailable
    from sag.benchmark.ci_sources import archive_sources
    from sag.benchmark.requirements import (
        canonical_digest, load_requirements, normalize_task, validate_ci_count_metadata, validate_requirements,
    )
    from sag.metrics.target_record import TargetRecord

    out = Path(out)
    project, bundle = entry["id"], out / "bundle"
    definition, preparation = out / "definitions" / project, out / "preparation"
    task_bytes = (definition / "task.json").read_bytes()
    if (sha256_bytes(task_bytes) != entry["task"]["sha256"]
            or (bundle / entry["task"]["path"]).read_bytes() != task_bytes):
        raise ValueError("Frozen task bytes differ between the definition and the bundle")
    existing = [name for name in BUILT_FILES if (definition / name).exists()]
    if existing:
        if not rebuild:
            raise ValueError(f"{project} is already built ({', '.join(existing)}); pass --rebuild to rebuild it")
        for name in existing:
            _remove(definition / name)
    task = normalize_task(json.loads(task_bytes))
    spec = build_project(entry, bundle, definition, Path(repo_root))
    # build_project also copies the task to its bundle-relative path; the
    # definition keeps the one task.json at its root (same bytes).
    stray = definition / entry["task"]["path"]
    if stray.read_bytes() != task_bytes:
        raise ValueError("build_project wrote different task bytes")
    shutil.rmtree(definition / PurePosixPath(entry["task"]["path"]).parts[0])
    spec = attach_preparation_inventory(spec, task, preparation, definition)
    shutil.copytree(bundle / "ci" / project, definition / "ci" / project)
    index = json.loads((definition / "ci" / project / "index.json").read_text())
    job_log = definition / index["sources"]["job_log"]["path"]
    job_ref = archive_source(job_log, definition, basis="exact_selected_job_log",
                             official_url=index["selected_url"])
    alignment = spec["ci_alignment"]
    alignment["test_count_semantics"] = unavailable(
        "The selected GitHub Actions job log is archived, but the shared typed CI-count validator supports only "
        "Jenkins testReport; this review-required definition supplies no count denominator.",
        sources={"job_log": job_ref}, selected_url=alignment["selected_url"],
        selected_cell=alignment["selected_cell"])
    spec["maven_observer"] = deepcopy(observer)
    validate_requirements(spec, task)
    requirements_path = definition / "requirements.json"
    write_json(requirements_path, spec)
    spec = load_requirements(requirements_path, task)
    validate_ci_count_metadata(spec, base=definition, required=True)
    archive_sources(spec, definition, definition)  # every referenced source is present and byte-bound

    dataset = entry["dataset"]
    log_counts = log_test_totals(job_log.read_bytes().decode("utf-8", errors="replace"))
    counts = dataset_totals(dataset["counts"])
    conclusion = dataset["ci"].get("job_conclusion")
    target, agreement = ci_target(
        task, cell_id=dataset["ci"]["job_name"], ci_url=dataset["ci"]["url"], logs_url=dataset["ci"].get("logs_url"),
        build={"success": "ok", "failure": "failed"}.get(conclusion, "unknown"), dataset_counts=counts,
        log_counts=log_counts, module_count=dataset["module_count"], harvested_at=now())
    target_path = definition / "ci-target.json"
    write_json(target_path, target)
    record = TargetRecord.model_validate_json(target_path.read_bytes())
    if (record.repo, record.sha) != (task["repo"], task["sha"]):
        raise ValueError("CI target subject differs from the task")
    status_path = preparation / project / "metadata-status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    freeze = {
        "schema_version": 1, "tier": TIER, "project_id": project, "repo": task["repo"], "commit": task["sha"],
        "dataset_task_id": dataset["task_id"], "selection_sha256": dataset["selection_sha256"],
        "task": {"file_sha256": sha256_bytes(task_bytes), "canonical_sha256": task_digest(task)},
        "requirements": {"canonical_sha256": canonical_digest(spec), "file_sha256": file_sha(requirements_path),
                         "annotation_completeness": spec["annotation_completeness"]["status"],
                         "group_labels": spec["group_labels"], "requirement_count": len(spec["requirements"])},
        "ci_target": {"file_sha256": file_sha(target_path), "cell": target["matched_cell"], "grade": "B"},
        "evidence": {
            "logs_zip": {"dataset_path": dataset["ci"]["logs_zip"], "sha256": dataset["ci"]["logs_zip_sha256"],
                         "url": dataset["ci"].get("logs_url"), "zip_member": dataset["ci"]["zip_member"]},
            "cell_evidence": {"dataset_path": dataset["ci"]["cell_evidence"],
                              "sha256": dataset["ci"]["cell_evidence_sha256"]},
            "source_pom": {"dataset_path": dataset["source_pom"]["path"], "sha256": dataset["source_pom"]["sha256"],
                           "url": dataset["source_pom"]["url"]},
            "archived_ci_index": alignment.get("archived_ci_index"),
            "definition_ci_sources": index["sources"],
        },
        "extraction": dataset["extraction"],
        "ci_counts": {"dataset": counts, "log": log_counts, "agreement": agreement,
                      "asserted_in_ci_target": agreement == "agree"},
        "metadata_capture": {"status": spec.get("metadata_preparation", {}).get("status"),
                             "status_file_status": status.get("status"), "batch": dataset.get("batch"),
                             "image": status.get("image"),
                             "image_substitute": ({k: status["image"].get(k) for k in
                                                   ("requested", "used", "used_java_major", "reason")}
                                                  | {"used": status["image"].get("name")}
                                                  if (status.get("image") or {}).get("substitute") else None),
                             "steps": {s["id"]: s["status"] for s in status.get("steps", [])}},
        "timestamps": {"prepared_at": dataset.get("prepared_at"), "captured_at": status.get("written_at"),
                       "built_at": now()},
        "tool": tool_identity(),
        "notes": [
            "Review-required tier: requirement groups stay unavailable in scoring; the task outcome is still recorded.",
            "The effective POM capture is a non-authoritative inventory and never upgrades the annotation to complete.",
            "Java major and Maven version come from the dataset's recorded CI environment strings listed under extraction.",
        ],
    }
    write_json(definition / "freeze.json", freeze)
    names = {p.name for p in definition.iterdir()}
    if names != DEFINITION_FILES:
        raise ValueError(f"Unexpected definition contents: {sorted(names ^ DEFINITION_FILES)}")
    return {"status": "built", "requirements": len(spec["requirements"]), "group_labels": spec["group_labels"],
            "metadata": freeze["metadata_capture"]["status"], "ci_counts": agreement,
            "requirements_digest": digest(spec)}


def build(out, projects=None, *, repo_root, observer, rebuild=False):
    manifest = json.loads((Path(out) / "bundle/manifest.json").read_text())
    entries = {entry["id"]: entry for entry in manifest["projects"]}
    results = {}
    for name in projects or sorted(entries):
        try:
            results[name] = build_definition(entries[name], out, repo_root=repo_root, observer=observer,
                                             rebuild=rebuild)
        except (ValueError, OSError, KeyError) as exc:
            results[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return results


# ---------------------------------------------------------------- verify

def verify_definition(definition, *, observer):
    from sag.agent.acceptance_task import load_acceptance_task
    from sag.agent.ci_comparison import load_ci_target
    from sag.benchmark.ci_sources import archive_sources
    from sag.benchmark.requirements import (
        file_digest, load_json, load_requirements, normalize_task, requirements_evidence_root,
        validate_ci_count_metadata,
    )
    from scripts.run_portable_harness import admit_definition

    row = {"project": definition.name, "result": "ok", "problems": []}
    problems = row["problems"]
    task = normalize_task(load_json(definition / "task.json"))
    acceptance = load_acceptance_task(definition / "task.json")
    requirements_path = definition / "requirements.json"
    spec = load_requirements(requirements_path, task)
    validate_ci_count_metadata(spec, base=requirements_evidence_root(requirements_path), required=True)
    admission = admit_definition(spec, allow_review_required=True)
    target = load_ci_target(definition / "ci-target.json").record
    if (target.repo, target.sha) != (acceptance.repo, acceptance.sha) or target.matched_cell is None:
        problems.append("ci_target_subject_or_cell")
    with tempfile.TemporaryDirectory() as temporary:
        archive_sources(spec, definition, temporary)
        copied = sum(1 for p in Path(temporary).rglob("*") if p.is_file())
    if observer is not None and spec.get("maven_observer") != observer:
        problems.append("maven_observer_differs")
    names = {p.name for p in definition.iterdir()}
    if names != DEFINITION_FILES:
        problems.append("contents:" + ",".join(sorted(names ^ DEFINITION_FILES)))
    record = load_json(definition / "freeze.json")
    if (record.get("task", {}).get("file_sha256") != file_digest(definition / "task.json")
            or record.get("requirements", {}).get("file_sha256") != file_digest(requirements_path)
            or record.get("ci_target", {}).get("file_sha256") != file_digest(definition / "ci-target.json")):
        problems.append("freeze_record_hashes")
    cell = target.cells[0]
    step = task["steps"][0]
    row.update(java=step.get("java_major"), maven=step.get("maven_version"), steps=len(task["steps"]),
               annotation=admission["status"], harness=admission["admission"],
               metadata=spec.get("metadata_preparation", {}).get("status"),
               requirements=len(spec["requirements"]), groups=",".join(spec["group_labels"]),
               counts=spec["ci_alignment"]["test_count_semantics"]["status"],
               target=f"{cell.executed_count}/{cell.red_count}/{cell.skipped} grade {cell.grade}",
               count_agreement=record.get("ci_counts", {}).get("agreement"), sources=copied,
               image=((record.get("metadata_capture", {}).get("image") or {}).get("name") or "-")
               + (" (substitute)" if record.get("metadata_capture", {}).get("image_substitute") else ""))
    if problems:
        row["result"] = "failed"
    return row


def verify(out, *, observer=None):
    rows = []
    for definition in sorted(p for p in (Path(out) / "definitions").iterdir() if p.is_dir()):
        try:
            rows.append(verify_definition(definition, observer=observer))
        except Exception as exc:  # report every definition, not only the first failure
            rows.append({"project": definition.name, "result": "failed",
                         "problems": [f"{type(exc).__name__}: {exc}"]})
    return rows


def print_verify(rows, stream=sys.stdout):
    print("| project | result | java | maven | steps | annotation | harness | metadata | requirements | groups | "
          "CI count semantics | ci-target executed/red/skipped | dataset vs log | sources copied | capture image | "
          "problems |", file=stream)
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|", file=stream)
    for row in rows:
        print("| " + " | ".join(str(row.get(k, "-")) for k in (
            "project", "result", "java", "maven", "steps", "annotation", "harness", "metadata", "requirements",
            "groups", "counts", "target", "count_agreement", "sources", "image")) + " | "
              + ("; ".join(row["problems"]) or "-") + " |", file=stream)


# ------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--twenty", type=Path, default=DEFAULT_TWENTY,
                        help="Directory of the twenty frozen definitions (excluded; source of the observer policy)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    commands = parser.add_subparsers(dest="command", required=True)
    chooser = commands.add_parser("select", help="List eligible tasks and write selection.json")
    chooser.add_argument("--allow-two-steps", action="store_true")
    chooser.add_argument("--max-java-lines", type=int)
    chooser.add_argument("--max-modules", type=int)
    chooser.add_argument("--projects", nargs="*", help="Restrict eligibility to these project ids (a decided list)")
    preparer = commands.add_parser("prepare", help="Write tasks, the bundle and the metadata scripts")
    preparer.add_argument("--projects", nargs="*")
    preparer.add_argument("--batch-label", default="batch")
    puller = commands.add_parser("images", help="Pull one capture image per Java major and record any gap")
    puller.add_argument("--batches", nargs="*")
    capturer = commands.add_parser("capture", help="Run the metadata containers and write metadata-status.json")
    capturer.add_argument("--batches", nargs="*")
    capturer.add_argument("--no-pull", action="store_true")
    builder = commands.add_parser("build", help="Build the definition directories")
    builder.add_argument("--projects", nargs="*")
    builder.add_argument("--repo-root", type=Path)
    builder.add_argument("--rebuild", action="store_true")
    commands.add_parser("verify", help="Reload every definition as the harness does")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if args.command == "select":
        selection = select(args.dataset.resolve(), args.twenty.resolve(), out, allow_two_steps=args.allow_two_steps,
                           max_java_lines=args.max_java_lines, max_modules=args.max_modules,
                           projects=args.projects)
        print_selection(selection)
    elif args.command == "prepare":
        print(json.dumps(prepare(out / "selection.json", out, args.projects, args.dataset.resolve(),
                                 batch_label=args.batch_label), indent=2))
    elif args.command == "images":
        images(out, args.batches)
    elif args.command == "capture":
        capture(out, args.batches, pull=not args.no_pull)
    elif args.command == "build":
        repo_root = (args.repo_root or args.dataset.resolve().parents[1]).resolve()
        results = build(out, args.projects, repo_root=repo_root, observer=observer_policy(args.twenty),
                        rebuild=args.rebuild)
        print(json.dumps(results, indent=2))
        if any(r["status"] != "built" for r in results.values()):
            sys.exit(1)
    else:
        rows = verify(out, observer=observer_policy(args.twenty))
        print_verify(rows)
        if any(r["result"] != "ok" for r in rows):
            sys.exit(1)


if __name__ == "__main__":
    main()
