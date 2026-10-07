"""Narrow offline transport and declared-variant checks for archived CI sources.

This module never fetches CI data or changes task commands. The caller supplies
an independently bound frozen CI index; review-authored metadata is not its own
provenance authority.
"""

from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import shlex
from urllib.parse import urljoin

from sag.benchmark.requirements import bound_file


class _JenkinsConsole(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.count = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "pre" and "console-output" in dict(attrs).get("class", "").split():
            if self.active:
                raise ValueError("Nested Jenkins console pre")
            self.active = True
            self.count += 1
        elif self.active and tag in {"pre", "script", "style"}:
            raise ValueError("Unexpected container inside Jenkins console")

    def handle_endtag(self, tag):
        if tag == "pre":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def extract_jenkins_console(raw: bytes) -> str:
    """Decode all text in one closed console-output pre, without line filtering."""
    parser = _JenkinsConsole()
    parser.feed(raw.decode("utf-8"))
    parser.close()
    if parser.count != 1 or parser.active:
        raise ValueError("Expected exactly one complete Jenkins console-output pre")
    return "".join(parser.parts)


def _matching_index_entry(index: dict, ref: dict, suffix: str) -> dict:
    found = [entry for entry in index.get("evidence", [])
             if entry.get("sha256") == ref.get("sha256")
             and entry.get("bytes") == ref.get("bytes")
             and str(entry.get("path", "")).endswith(suffix)
             and entry.get("official_url") == ref.get("official_url")]
    if len(found) != 1:
        raise ValueError("Selected CI source is not anchored in frozen CI index")
    return found[0]


def _url(value: str) -> str:
    return value.rstrip("/")


def _declared_variant(index: dict, original: list[str], task_argv: list[str], declaration: dict | None) -> dict | None:
    if original == task_argv:
        if declaration:
            raise ValueError("Declared variant is unnecessary for identical commands")
        return None
    phrase = "publication-only deploy is replaced by local install before the run"
    notes = [note for note in index.get("notes", []) if isinstance(note, str) and phrase in note]
    if len(notes) != 1 or not isinstance(declaration, dict):
        raise ValueError("Task variant lacks a preexisting frozen declaration")
    options = [arg for arg in original if arg.startswith("-DaltDeploymentRepository=")]
    if (original.count("deploy") != 1 or len(options) != 1
            or not re.fullmatch(r"-DaltDeploymentRepository=[^:\s]+::(?:default::)?file:/[^\s]+", options[0])):
        raise ValueError("Only the declared local-file deploy-to-install variant is supported")
    projected = ["install" if arg == "deploy" else arg for arg in original if arg != options[0]]
    if projected != task_argv:
        raise ValueError("Frozen task has changes beyond the declared deployment variant")
    expected = {
        "classification": "ci_declared_variant",
        "original_command": index["original_command"],
        "task_argv": task_argv,
        "preexisting_frozen_note": notes[0],
        "removed_goal": "deploy:deploy",
        "removed_option": "-DaltDeploymentRepository",
        "publication_comparable": False,
    }
    if declaration != expected:
        raise ValueError("Reviewed deployment variant differs from its frozen declaration")
    return expected


def load_selected_ci_source(
    review: dict,
    source_base: Path,
    *,
    frozen_ci_index_ref: dict,
    frozen_ci_index_base: Path,
    task: dict,
    selected_url: str,
) -> dict:
    """Return a byte-verified native log and its source lineage, without I/O outside archives.

    ``frozen_ci_index_ref`` must come from the original manifest/package import,
    not from ``review``. The caller archives ``source_paths`` in the resulting
    reviewed plan so the copied HTML/stage/index remain available for replay.
    """
    source = review.get("ci_source", {})
    if source.get("kind") == "jenkins-timestamped-command-v1":
        return load_timestamped_command_ci_source(
            review, source_base, frozen_ci_index_ref=frozen_ci_index_ref,
            frozen_ci_index_base=frozen_ci_index_base, task=task, selected_url=selected_url,
        )
    if source.get("kind") != "jenkins-selected-node-v1":
        raise ValueError("Unsupported reviewed CI source transport")
    index_path = bound_file(frozen_ci_index_base, frozen_ci_index_ref)
    index = json.loads(index_path.read_text())
    review_index_path = bound_file(source_base, source.get("index", {}))
    if index_path.read_bytes() != review_index_path.read_bytes():
        raise ValueError("Reviewed CI index differs from independently frozen index")
    if _url(index.get("selected_url", "")) != _url(selected_url):
        raise ValueError("Selected CI run differs from frozen task cell")
    html_ref, stage_ref = source.get("raw_html", {}), source.get("stage", {})
    html_path, stage_path = bound_file(source_base, html_ref), bound_file(source_base, stage_ref)
    html_entry = _matching_index_entry(index, html_ref, "node-console.html")
    stage_entry = _matching_index_entry(index, stage_ref, "stage.json")
    node_url = source.get("selected_node_url")
    if (not isinstance(node_url, str) or _url(node_url) != _url(html_entry["official_url"])
            or not _url(node_url).startswith(selected_url.rstrip("/") + "/execution/node/")):
        raise ValueError("Selected Jenkins node URL differs from frozen source")
    stage = json.loads(stage_path.read_text())
    if (stage.get("status") != "SUCCESS"
            or _url(urljoin(selected_url, stage.get("_links", {}).get("self", {}).get("href", "")))
            != _url(stage_entry["official_url"])):
        raise ValueError("Selected Jenkins stage identity or terminal differs")
    nodes = [n for n in stage.get("stageFlowNodes", [])
             if _url(urljoin(selected_url, n.get("_links", {}).get("console", {}).get("href", ""))) == _url(node_url)]
    if len(nodes) != 1 or nodes[0].get("status") != "SUCCESS" or nodes[0].get("name") != "Shell Script":
        raise ValueError("Selected node is not the successful shell step in the frozen stage")
    text = extract_jenkins_console(html_path.read_bytes())
    native_path = bound_file(source_base, review.get("sources", {}).get("official_ci", {}))
    if native_path.read_bytes() != text.encode("utf-8"):
        raise ValueError("Extracted CI text differs from the complete HTML console payload")
    lines = text.splitlines()
    original = shlex.split(index.get("original_command", ""))
    if (not original or original[0] != "mvn" or not lines or not lines[0].startswith("+ ")
            or shlex.split(lines[0][2:]) != original
            or shlex.split(nodes[0].get("parameterDescription", "")) != original):
        raise ValueError("Official command differs across console, stage and frozen index")
    steps = task.get("steps", [])
    if len(steps) != 1 or steps[0].get("runner") != "maven":
        raise ValueError("Selected-node transport supports one frozen Maven step")
    step = steps[0]
    if (step.get("java_major") and not re.search(r"^Java version:\s*" + str(step["java_major"]) + r"[.,\s]", text, re.M)
            or step.get("maven_version") and not re.search(r"^Apache Maven\s+" + re.escape(step["maven_version"]) + r"(?:\s|$)", text, re.M)):
        raise ValueError("Selected CI node runtime differs from frozen task")
    variant = _declared_variant(index, original, step["argv"], review.get("declared_variant"))
    return {
        "text": text,
        "source_paths": {"ci_original_html": html_path, "ci_selected_stage": stage_path, "ci_frozen_index": index_path},
        "lineage": {"transport": "jenkins-selected-node-v1", "selected_node_url": node_url,
                    "selected_stage_url": stage_entry["official_url"], "original_html_sha256": html_ref["sha256"],
                    "native_text_sha256": review["sources"]["official_ci"]["sha256"],
                    "frozen_index_sha256": frozen_ci_index_ref["sha256"]},
        "variant": variant,
    }


_JENKINS_TIMESTAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] (.*)\n$")


def extract_timestamped_jenkins_command(raw: bytes, original_argv: list[str]) -> dict:
    """Select one complete Build-stage shell block without filtering native lines.

    This v1 transport covers the archived Creadur Jenkins layout: an exact shell
    echo, contiguous millisecond-UTC-prefixed output, then literal ``Post stage``.
    A different framing must get its own reviewed transport rather than heuristics.
    """
    text = raw.decode("utf-8")
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    if not lines or lines[-1] != "Finished: SUCCESS\n":
        raise ValueError("Jenkins job console is incomplete or unsupported")
    normalized = [m[1] + "\n" if (m := _JENKINS_TIMESTAMP.fullmatch(line)) else line for line in lines]
    starts = []
    for index, line in enumerate(normalized):
        if line.startswith("+ "):
            try:
                if shlex.split(line[2:]) == original_argv:
                    starts.append(index)
            except ValueError as exc:
                raise ValueError("Malformed Jenkins shell command echo") from exc
    if len(starts) != 1:
        raise ValueError("Expected one unique exact original command in Jenkins job")
    start = starts[0]
    if start == 0 or lines[start - 1] != "[Pipeline] sh\n" or not _JENKINS_TIMESTAMP.fullmatch(lines[start]):
        raise ValueError("Selected command has no complete Jenkins shell boundary")
    stage_starts = [i for i in range(start) if lines[i] == "[Pipeline] { (Build)\n"]
    if not stage_starts or any(line == "[Pipeline] // stage\n" for line in lines[stage_starts[-1]:start]):
        raise ValueError("Selected command is outside the archived Build stage")
    end = start
    while end < len(lines) and _JENKINS_TIMESTAMP.fullmatch(lines[end]):
        end += 1
    if end == len(lines) or lines[end] != "Post stage\n":
        raise ValueError("Selected command output lacks its complete Post stage boundary")
    native = "".join(normalized[start:end])
    if "\r" in native:
        raise ValueError("Selected command uses unsupported carriage-return transport")
    if sum(line.startswith("+ ") for line in normalized[start:end]) != 1:
        raise ValueError("Selected shell block contains additional command echoes")
    if re.findall(r"^\[INFO\]\s+BUILD (SUCCESS|FAILURE)\s*$", native, re.M) != ["SUCCESS"]:
        raise ValueError("Selected command needs one successful native terminal")
    terminal = native.index("[INFO] BUILD SUCCESS")
    if len(re.findall(r"^\[INFO\] Finished at: .+$", native[terminal:], re.M)) != 1:
        raise ValueError("Selected command lacks its native completion footer")
    before = "".join(lines[:start]).encode("utf-8")
    selected = "".join(lines[start:end]).encode("utf-8")
    return {
        "text": native,
        "selection": {
            "source_first_line": start + 1, "source_last_line": end,
            "byte_start": len(before), "byte_end_exclusive": len(before) + len(selected),
            "selected_raw_sha256": hashlib.sha256(selected).hexdigest(),
            "native_text_sha256": hashlib.sha256(native.encode("utf-8")).hexdigest(),
            "native_to_source_line_offset": start,
            "start_boundary": "[Pipeline] sh", "end_boundary": "Post stage",
            "stage": "Build", "timestamp_rule": "bracketed-UTC-milliseconds-v1",
        },
    }


def load_timestamped_command_ci_source(
    review: dict, source_base: Path, *, frozen_ci_index_ref: dict,
    frozen_ci_index_base: Path, task: dict, selected_url: str,
) -> dict:
    """Verify full-job archives and the predeclared Creadur publication variant."""
    source = review.get("ci_source", {})
    if source.get("kind") != "jenkins-timestamped-command-v1":
        raise ValueError("Unsupported reviewed CI source transport")
    index_path = bound_file(frozen_ci_index_base, frozen_ci_index_ref)
    index = json.loads(index_path.read_text())
    if index_path.read_bytes() != bound_file(source_base, source.get("index", {})).read_bytes():
        raise ValueError("Reviewed CI index differs from independently frozen index")
    if _url(index.get("selected_url", "")) != _url(selected_url):
        raise ValueError("Selected CI run differs from frozen task cell")
    console_ref, build_ref = source.get("raw_console", {}), source.get("build", {})
    console_path, build_path = bound_file(source_base, console_ref), bound_file(source_base, build_ref)
    console_entry = _matching_index_entry(index, console_ref, "console.log")
    build_entry = _matching_index_entry(index, build_ref, "build.json")
    if (console_entry["official_url"] != selected_url.rstrip("/") + "/consoleText"
            or build_entry["official_url"] != selected_url.rstrip("/") + "/api/json?depth=1"):
        raise ValueError("Selected console and build URLs differ from frozen run")
    build = json.loads(build_path.read_text())
    revisions = {a.get("lastBuiltRevision", {}).get("SHA1") for a in build.get("actions", [])
                 if isinstance(a, dict) and a.get("lastBuiltRevision")}
    if (build.get("result") != "SUCCESS" or build.get("building") is not False
            or _url(build.get("url", "")) != _url(selected_url) or revisions != {task["sha"]}):
        raise ValueError("Archived Jenkins build terminal, URL or commit differs")
    steps = task.get("steps", [])
    original = shlex.split(index.get("original_command", ""))
    if (len(steps) != 1 or steps[0].get("runner") != "maven" or not original
            or original not in (["./mvnw", "-B", "-U", "-V", "clean", "deploy"],
                                ["mvn", "-B", "-U", "-V", "clean", "deploy"])):
        raise ValueError("Timestamp transport supports one exact reviewed Maven deployment command")
    step = steps[0]
    extracted = extract_timestamped_jenkins_command(console_path.read_bytes(), original)
    native_path = bound_file(source_base, review.get("sources", {}).get("official_ci", {}))
    if native_path.read_bytes() != extracted["text"].encode("utf-8") or source.get("selection") != extracted["selection"]:
        raise ValueError("Derived command text or source boundaries differ from the complete native block")
    text = extracted["text"]
    if (step.get("java_major") and not re.search(r"^Java version:\s*" + str(step["java_major"]) + r"[.,\s]", text, re.M)
            or step.get("maven_version") and not re.search(r"^Apache Maven\s+" + re.escape(step["maven_version"]) + r"(?:\s|$)", text, re.M)):
        raise ValueError("Selected CI command runtime differs from frozen task")
    notes = [note for note in index.get("notes", []) if isinstance(note, str)
             and "publication-only deploy is replaced by local install before the run" in note]
    if len(notes) != 1 or ["install" if a == "deploy" else a for a in original] != step["argv"]:
        raise ValueError("Task variant lacks a matching preexisting frozen declaration")
    variant = {
        "classification": "ci_declared_variant", "original_command": index["original_command"],
        "task_argv": step["argv"], "preexisting_frozen_note": notes[0],
        "removed_goal": "deploy:deploy", "removed_option": None,
        "module_projection": "exclude_default_deploy_once_per_module", "publication_comparable": False,
    }
    if review.get("declared_variant") != variant:
        raise ValueError("Reviewed deployment variant differs from its frozen declaration")
    return {
        "text": text,
        "source_paths": {"ci_original_console": console_path, "ci_build_record": build_path, "ci_frozen_index": index_path},
        "lineage": {"transport": source["kind"], "original_console_sha256": console_ref["sha256"],
                    "frozen_index_sha256": frozen_ci_index_ref["sha256"], "build_sha256": build_ref["sha256"],
                    "console_url": console_entry["official_url"], "build_url": build_entry["official_url"],
                    "original_argv": original, "task_argv": step["argv"], **extracted["selection"]},
        "variant": variant,
    }


def project_declared_variant_bindings(actual_ci: list[dict], reviewed_ci: list[dict], variant: dict | None) -> list[dict]:
    """Keep the original inventory outside this function; project only task order.

    The sole permitted exclusion is one already-declared default deploy goal.
    Every other occurrence must retain its review and native identity.
    """
    fields = ("goal", "version", "execution", "module", "occurrence", "position", "line")
    if [{k: row.get(k) for k in fields} for row in reviewed_ci] != actual_ci:
        raise ValueError("Every original CI occurrence must remain explicitly reviewed")
    excluded = [row for row in reviewed_ci if row.get("disposition") == "excluded_declared_variant"]
    if variant is None:
        if excluded:
            raise ValueError("CI exclusion requires a verified declared variant")
        return deepcopy(reviewed_ci)
    deploy = [row for row in reviewed_ci if row["goal"] == "deploy:deploy"]
    expected_modules = {row["module"] for row in reviewed_ci} if variant.get("module_projection") == "exclude_default_deploy_once_per_module" else None
    valid_number = (
        len(deploy) == len(expected_modules) and {row["module"] for row in deploy} == expected_modules
        if expected_modules is not None else len(deploy) == 1
    )
    if (not valid_number or excluded != deploy
            or any(row["execution"] != "default-deploy" or not row.get("reason")
                   or row.get("requirement_id") or row.get("task_position") is not None for row in excluded)):
        raise ValueError("Declared deployment variant must exclude exactly its default deploy occurrence")
    projected = []
    for row in reviewed_ci:
        if row in excluded:
            continue
        item = deepcopy(row)
        if item.get("task_position", len(projected)) != len(projected):
            raise ValueError("Projected task order differs from the reviewed variant")
        item["ci_position"] = item["position"]
        item["position"] = len(projected)
        projected.append(item)
    return projected
