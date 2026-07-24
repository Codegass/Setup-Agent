"""Fail-closed build-graph boundary checks for harness-forced test actions.

This module is intentionally not used by normal model-initiated builds.  Its
sole purpose is to prove that a survey-selected Maven or Gradle coordinate
cannot make the harness traverse outside the verified checkout when the
controller executes its one mandatory test attempt.

The proof covers the statically declared build graph and backend marker
selection.  It is not a sandbox and does not claim that arbitrary build-script
code is safe to execute.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from sag.runtime.container_io import ContainerFileReadError, read_container_text
from sag.tools.build.backends import BUILD_MARKERS

GraphBoundaryStatus = Literal["verified", "unavailable"]

_MAX_GRAPH_DEPTH = 32
_MAX_GRAPH_NODES = 256


@dataclass(frozen=True, slots=True)
class ForcedBuildGraphBoundary:
    status: GraphBoundaryStatus
    reason_code: str
    visited_roots: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status == "verified"


class _GraphUnavailable(Exception):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _normalized_absolute(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or not raw.startswith("/") or "\x00" in raw or "\n" in raw or "\r" in raw:
        return None
    return posixpath.normpath(raw)


def _contained(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _execute(orchestrator: Any, command: str) -> Mapping[str, Any]:
    try:
        result = orchestrator.execute_command(command)
    except Exception as exc:
        raise _GraphUnavailable("graph_probe_io_failed") from exc
    if not isinstance(result, Mapping):
        raise _GraphUnavailable("graph_probe_io_failed")
    return result


def _realpath(orchestrator: Any, path: str) -> str:
    result = _execute(
        orchestrator,
        f"realpath -e -- {shlex.quote(path)}",
    )
    if not result.get("success"):
        raise _GraphUnavailable("graph_path_unresolved")
    lines = str(result.get("output") or "").splitlines()
    if len(lines) != 1:
        raise _GraphUnavailable("graph_path_unresolved")
    resolved = _normalized_absolute(lines[0])
    if resolved is None:
        raise _GraphUnavailable("graph_path_unresolved")
    return resolved


def _read_required(orchestrator: Any, path: str) -> str:
    try:
        content = read_container_text(orchestrator, path)
    except ContainerFileReadError as exc:
        raise _GraphUnavailable("graph_file_unreadable") from exc
    if content is None:
        raise _GraphUnavailable("graph_file_unreadable")
    return content


def _file_state(orchestrator: Any, path: str) -> Literal["file", "absent"]:
    marker = "__SAG_GRAPH_FILE__"
    absent = "__SAG_GRAPH_ABSENT__"
    result = _execute(
        orchestrator,
        (f"if test -f {shlex.quote(path)} ; then printf {marker} ; " f"else printf {absent}; fi"),
    )
    if not result.get("success"):
        raise _GraphUnavailable("graph_probe_io_failed")
    output = str(result.get("output") or "").strip()
    if output == marker:
        return "file"
    if output == absent:
        return "absent"
    raise _GraphUnavailable("graph_probe_io_failed")


def _directory_state(orchestrator: Any, path: str) -> Literal["directory", "absent"]:
    marker = "__SAG_GRAPH_DIRECTORY__"
    absent = "__SAG_GRAPH_ABSENT__"
    result = _execute(
        orchestrator,
        (f"if test -d {shlex.quote(path)} ; then printf {marker} ; " f"else printf {absent}; fi"),
    )
    if not result.get("success"):
        raise _GraphUnavailable("graph_probe_io_failed")
    output = str(result.get("output") or "").strip()
    if output == marker:
        return "directory"
    if output == absent:
        return "absent"
    raise _GraphUnavailable("graph_probe_io_failed")


def probe_forced_build_backend(orchestrator: Any, candidate_root: str) -> str | None:
    """Mirror BuildTool's marker order without invoking any backend."""
    try:
        for backend, markers in BUILD_MARKERS.items():
            for marker in markers:
                if (
                    _file_state(
                        orchestrator,
                        posixpath.join(candidate_root, marker),
                    )
                    == "file"
                ):
                    return "pytest" if backend == "python" else backend
    except _GraphUnavailable:
        return None
    return None


def _xml_local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _xml_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _xml_local_name(child) == name]


def _xml_child(element: ET.Element, name: str) -> ET.Element | None:
    return next(iter(_xml_children(element, name)), None)


def _maven_module_values(project: ET.Element) -> list[str]:
    modules: list[str] = []
    for block in _xml_children(project, "modules"):
        for module in _xml_children(block, "module"):
            value = str(module.text or "").strip()
            if value:
                modules.append(value)

    profiles = _xml_child(project, "profiles")
    if profiles is None:
        return modules
    for profile in _xml_children(profiles, "profile"):
        profile_modules = _xml_child(profile, "modules")
        if profile_modules is None:
            continue
        activation = _xml_child(profile, "activation")
        if activation is None:
            # A profile without activation is inactive for the forced command,
            # which supplies no -P selector.
            continue
        active_default = _xml_child(activation, "activeByDefault")
        if active_default is not None and str(active_default.text or "").strip().lower() == "true":
            for module in _xml_children(profile_modules, "module"):
                value = str(module.text or "").strip()
                if value:
                    modules.append(value)
            continue
        # JDK/OS/property/file activation depends on runtime state not pinned by
        # the forced action. A profile that can add modules is therefore not
        # statically safe.
        if any(_xml_local_name(child) != "activeByDefault" for child in activation):
            raise _GraphUnavailable("maven_profile_activation_unresolved")
    return modules


def _resolve_maven_module_path(module_dir: str, raw_value: str) -> str:
    value = raw_value.strip()
    value = value.replace("${project.basedir}", module_dir)
    value = value.replace("${basedir}", module_dir)
    if not value or "${" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise _GraphUnavailable("maven_module_path_unresolved")
    if not value.startswith("/"):
        value = posixpath.join(module_dir, value)
    return posixpath.normpath(value)


def _read_contained_file(
    orchestrator: Any,
    path: str,
    *,
    project_root: str,
    outside_reason: str,
) -> tuple[str, str]:
    resolved = _realpath(orchestrator, path)
    if not _contained(resolved, project_root):
        raise _GraphUnavailable(outside_reason)
    return resolved, _read_required(orchestrator, resolved)


def _maven_parent_path(project: ET.Element, pom_dir: str) -> str | None:
    parent = _xml_child(project, "parent")
    if parent is None:
        return None
    relative = _xml_child(parent, "relativePath")
    if relative is None:
        value = "../pom.xml"
    else:
        value = str(relative.text or "").strip()
        if not value:
            # An explicitly empty relativePath disables local-parent lookup.
            return None
    if "${" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise _GraphUnavailable("maven_parent_path_unresolved")
    if not value.startswith("/"):
        value = posixpath.join(pom_dir, value)
    return posixpath.normpath(value)


_MAVEN_GRAPH_LONG_OPTIONS = {
    "--activate-profiles",
    "--also-make",
    "--also-make-dependents",
    "--file",
    "--global-settings",
    "--non-recursive",
    "--projects",
    "--resume-from",
    "--settings",
}
_MAVEN_GRAPH_SHORT_OPTIONS = {
    "-N",
    "-P",
    "-am",
    "-amd",
    "-gs",
    "-pl",
    "-rf",
    "-s",
}


def _maven_config_changes_graph(text: str) -> bool:
    try:
        tokens = shlex.split(text, comments=True, posix=True)
    except ValueError as exc:
        raise _GraphUnavailable("maven_config_unreadable") from exc
    for token in tokens:
        if token in _MAVEN_GRAPH_LONG_OPTIONS or any(
            token.startswith(f"{option}=") for option in _MAVEN_GRAPH_LONG_OPTIONS
        ):
            return True
        if token in _MAVEN_GRAPH_SHORT_OPTIONS:
            return True
        if any(
            token.startswith(option) and token != option
            for option in {"-P", "-gs", "-pl", "-rf", "-s"}
        ):
            return True
        # -fae/-ff/-fn are failure-policy switches, not attached -f paths.
        if token.startswith("-f") and token not in {"-fae", "-ff", "-fn"}:
            return True
    return False


def _verify_maven_config(
    orchestrator: Any,
    *,
    project_root: str,
    candidate_root: str,
) -> None:
    current = candidate_root
    while True:
        maven_dir = posixpath.join(current, ".mvn")
        if _directory_state(orchestrator, maven_dir) == "directory":
            # Maven's launcher selects the nearest ancestor containing a
            # .mvn directory as its project basedir.  Once found, an empty
            # directory shadows every higher ancestor; it does not merge
            # maven.config files from multiple levels.
            resolved_maven_dir = _realpath(orchestrator, maven_dir)
            if not _contained(resolved_maven_dir, project_root):
                raise _GraphUnavailable("maven_basedir_outside_project")
            config_path = posixpath.join(maven_dir, "maven.config")
            if _file_state(orchestrator, config_path) == "file":
                _, config = _read_contained_file(
                    orchestrator,
                    config_path,
                    project_root=project_root,
                    outside_reason="maven_config_outside_project",
                )
                if _maven_config_changes_graph(config):
                    raise _GraphUnavailable("maven_config_changes_graph")
            return
        if current == "/":
            return
        current = posixpath.dirname(current)


def _verify_maven(
    orchestrator: Any,
    *,
    project_root: str,
    candidate_root: str,
    max_depth: int,
    max_nodes: int,
) -> tuple[str, ...]:
    visited: list[str] = []
    visited_dirs: set[str] = set()
    known_poms: set[str] = set()
    parsed_poms: dict[str, ET.Element] = {}
    processed_parents: set[str] = set()
    processed_modules: set[str] = set()
    stack: set[str] = set()

    def read_pom(pom_path: str) -> tuple[str, ET.Element]:
        resolved_pom, pom_text = _read_contained_file(
            orchestrator,
            pom_path,
            project_root=project_root,
            outside_reason="maven_pom_outside_project",
        )
        project = parsed_poms.get(resolved_pom)
        if project is None:
            if len(known_poms) >= max_nodes:
                raise _GraphUnavailable("maven_module_cap_exceeded")
            try:
                project = ET.fromstring(pom_text)
            except ET.ParseError as exc:
                raise _GraphUnavailable("maven_pom_unreadable") from exc
            if _xml_local_name(project) != "project":
                raise _GraphUnavailable("maven_pom_unreadable")
            known_poms.add(resolved_pom)
            parsed_poms[resolved_pom] = project
            pom_dir = posixpath.dirname(resolved_pom)
            if pom_dir not in visited_dirs:
                visited_dirs.add(pom_dir)
                visited.append(pom_dir)
        return resolved_pom, project

    def walk_pom(pom_path: str, *, expand_modules: bool, depth: int) -> None:
        if depth > max_depth:
            raise _GraphUnavailable("maven_module_depth_exceeded")
        resolved_pom, project = read_pom(pom_path)
        if resolved_pom in stack:
            raise _GraphUnavailable("maven_module_cycle")
        parent_done = resolved_pom in processed_parents
        modules_done = resolved_pom in processed_modules
        if parent_done and (not expand_modules or modules_done):
            return
        stack.add(resolved_pom)
        try:
            pom_dir = posixpath.dirname(resolved_pom)
            if not parent_done:
                parent_path = _maven_parent_path(project, pom_dir)
                if parent_path is not None and _file_state(orchestrator, parent_path) == "file":
                    walk_pom(
                        parent_path,
                        expand_modules=False,
                        depth=depth + 1,
                    )
                processed_parents.add(resolved_pom)
            if expand_modules and not modules_done:
                for raw_module in _maven_module_values(project):
                    child = _resolve_maven_module_path(pom_dir, raw_module)
                    resolved_child = _realpath(orchestrator, child)
                    if not _contained(resolved_child, project_root):
                        raise _GraphUnavailable("maven_module_outside_project")
                    child_pom = (
                        resolved_child
                        if resolved_child.endswith("/pom.xml")
                        else posixpath.join(resolved_child, "pom.xml")
                    )
                    walk_pom(
                        child_pom,
                        expand_modules=True,
                        depth=depth + 1,
                    )
                processed_modules.add(resolved_pom)
        finally:
            stack.remove(resolved_pom)

    _verify_maven_config(
        orchestrator,
        project_root=project_root,
        candidate_root=candidate_root,
    )
    walk_pom(
        posixpath.join(candidate_root, "pom.xml"),
        expand_modules=True,
        depth=0,
    )
    return tuple(visited)


def _strip_gradle_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
                output.append(char)
            else:
                output.append(" ")
        elif block_comment:
            if char == "*" and nxt == "/":
                output.extend((" ", " "))
                index += 1
                block_comment = False
            else:
                output.append("\n" if char == "\n" else " ")
        elif quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "/" and nxt == "/":
            output.extend((" ", " "))
            index += 1
            line_comment = True
        elif char == "/" and nxt == "*":
            output.extend((" ", " "))
            index += 1
            block_comment = True
        else:
            output.append(char)
        index += 1
    if quote is not None or block_comment:
        raise _GraphUnavailable("gradle_settings_unreadable")
    return "".join(output)


_LITERAL_RE = re.compile(r"""(['"])([^'"\\$\r\n]*)\1""")


def _literal_arguments(value: str) -> list[str]:
    literals = [match.group(2) for match in _LITERAL_RE.finditer(value)]
    remainder = _LITERAL_RE.sub("", value)
    if not literals or remainder.strip(" \t,;"):
        raise _GraphUnavailable("gradle_graph_expression_dynamic")
    return literals


def _collect_gradle_calls(
    source: str,
    name: str,
) -> tuple[list[str], str]:
    values: list[str] = []
    spans: list[tuple[int, int]] = []
    parenthesized = re.compile(rf"\b{name}\s*\(([^()]*)\)", re.DOTALL)
    for match in parenthesized.finditer(source):
        values.extend(_literal_arguments(match.group(1)))
        spans.append(match.span())
    residual_chars = list(source)
    for start, end in spans:
        residual_chars[start:end] = " " * (end - start)
    residual_source = "".join(residual_chars)
    line_call = re.compile(rf"(?m)^\s*{name}\s+([^\r\n]+)$")
    for match in line_call.finditer(residual_source):
        values.extend(_literal_arguments(match.group(1)))
        spans.append(match.span())

    residual = list(source)
    for start, end in spans:
        residual[start:end] = " " * (end - start)
    return values, "".join(residual)


def _normalized_gradle_project_id(project_id: str) -> str:
    normalized = project_id if project_id.startswith(":") else f":{project_id}"
    if not re.fullmatch(
        r":[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*",
        normalized,
    ):
        raise _GraphUnavailable("gradle_project_path_unresolved")
    return normalized


def _gradle_project_path(project_id: str, root: str) -> str:
    normalized = _normalized_gradle_project_id(project_id)
    return posixpath.join(root, *normalized.lstrip(":").split(":"))


def _gradle_literal_relocations(source: str) -> tuple[dict[str, str], str]:
    relocations: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    assignment = re.compile(
        r"""project\s*\(\s*(['"])(?P<project>[^'"\\$\r\n]+)\1\s*\)"""
        r"""\s*\.projectDir\s*=\s*(?P<rhs>[^\r\n;]+)"""
    )
    file_call = re.compile(r"""^file\s*\(\s*(['"])(?P<path>[^'"\\$\r\n]+)\1\s*\)$""")
    root_file = re.compile(
        r"""^(?:new\s+)?File\s*\(\s*(?:rootDir|settingsDir)\s*,\s*"""
        r"""(['"])(?P<path>[^'"\\$\r\n]+)\1\s*\)$"""
    )
    for match in assignment.finditer(source):
        rhs = match.group("rhs").strip()
        parsed = file_call.fullmatch(rhs) or root_file.fullmatch(rhs)
        if parsed is None:
            raise _GraphUnavailable("gradle_project_dir_dynamic")
        project_id = _normalized_gradle_project_id(match.group("project"))
        if project_id in relocations:
            raise _GraphUnavailable("gradle_project_dir_ambiguous")
        relocations[project_id] = parsed.group("path")
        spans.append(match.span())
    residual = list(source)
    for start, end in spans:
        residual[start:end] = " " * (end - start)
    remaining = "".join(residual)
    if re.search(r"\.projectDir\b", remaining):
        raise _GraphUnavailable("gradle_project_dir_dynamic")
    return relocations, remaining


def _verify_gradle(
    orchestrator: Any,
    *,
    project_root: str,
    candidate_root: str,
    max_depth: int,
    max_nodes: int,
) -> tuple[str, ...]:
    visited: list[str] = []
    seen_builds: set[str] = set()
    stack: set[str] = set()
    graph_nodes: set[str] = set()

    def settings_at(build_root: str) -> tuple[str, ...]:
        settings_paths = (
            posixpath.join(build_root, "settings.gradle"),
            posixpath.join(build_root, "settings.gradle.kts"),
        )
        states = tuple(_file_state(orchestrator, settings_path) for settings_path in settings_paths)
        lexical_present = tuple(
            settings_path for settings_path, state in zip(settings_paths, states) if state == "file"
        )
        if len(lexical_present) > 1:
            return lexical_present
        if not lexical_present:
            return ()
        if not _contained(build_root, project_root):
            raise _GraphUnavailable("gradle_settings_outside_project")
        resolved = _realpath(orchestrator, lexical_present[0])
        if not _contained(resolved, project_root):
            raise _GraphUnavailable("gradle_settings_outside_project")
        return (resolved,)

    def discover_initial_build_root() -> str:
        """Mirror Gradle's nearest-ancestor settings discovery up to root."""
        current = candidate_root
        while True:
            present = settings_at(current)
            if len(present) > 1:
                raise _GraphUnavailable("gradle_settings_ambiguous")
            if present:
                return current
            if current == "/":
                return candidate_root
            current = posixpath.dirname(current)

    def checked_path(raw_path: str, base: str, reason: str) -> str:
        if (
            not raw_path
            or "$" in raw_path
            or "\x00" in raw_path
            or "\n" in raw_path
            or "\r" in raw_path
        ):
            raise _GraphUnavailable("gradle_graph_expression_dynamic")
        lexical = (
            posixpath.normpath(raw_path)
            if raw_path.startswith("/")
            else posixpath.normpath(posixpath.join(base, raw_path))
        )
        resolved = _realpath(orchestrator, lexical)
        if not _contained(resolved, project_root):
            raise _GraphUnavailable(reason)
        graph_nodes.add(resolved)
        if len(graph_nodes) > max_nodes:
            raise _GraphUnavailable("gradle_graph_node_cap_exceeded")
        return resolved

    def walk(build_root: str, depth: int) -> None:
        if depth > max_depth:
            raise _GraphUnavailable("gradle_graph_depth_exceeded")
        resolved_build = checked_path(
            build_root,
            build_root,
            "gradle_build_outside_project",
        )
        if resolved_build in stack:
            raise _GraphUnavailable("gradle_include_build_cycle")
        if resolved_build in seen_builds:
            return
        seen_builds.add(resolved_build)
        visited.append(resolved_build)
        stack.add(resolved_build)
        try:
            present = settings_at(resolved_build)
            if len(present) > 1:
                raise _GraphUnavailable("gradle_settings_ambiguous")
            if not present:
                return
            source = _strip_gradle_comments(_read_required(orchestrator, present[0]))
            if re.search(r"\bapply\s*(?:\(|\s)", source):
                raise _GraphUnavailable("gradle_apply_from_unresolved")
            if re.search(r"\bincludeFlat\b", source):
                raise _GraphUnavailable("gradle_include_flat_unsafe")

            include_builds, residual = _collect_gradle_calls(
                source,
                "includeBuild",
            )
            if re.search(r"\bincludeBuild\b", residual):
                raise _GraphUnavailable("gradle_include_build_dynamic")

            included_projects, residual = _collect_gradle_calls(
                residual,
                "include",
            )
            if re.search(r"\binclude\b", residual):
                raise _GraphUnavailable("gradle_include_dynamic")

            relocations, _ = _gradle_literal_relocations(source)
            for raw_project_id in included_projects:
                project_id = _normalized_gradle_project_id(raw_project_id)
                default_path = _gradle_project_path(project_id, resolved_build)
                raw_path = relocations.pop(project_id, default_path)
                checked_path(
                    raw_path,
                    resolved_build,
                    "gradle_project_outside_project",
                )
            if relocations:
                # Relocating a project not present in the statically parsed
                # include set means the graph declaration is incomplete.
                raise _GraphUnavailable("gradle_project_dir_unresolved")
            for included_build in include_builds:
                nested = checked_path(
                    included_build,
                    resolved_build,
                    "gradle_include_build_outside_project",
                )
                walk(nested, depth + 1)
        finally:
            stack.remove(resolved_build)

    walk(discover_initial_build_root(), 0)
    return tuple(visited)


def verify_forced_candidate_build_graph(
    orchestrator: Any,
    *,
    project_root: str,
    candidate_root: str,
    system: str,
    max_depth: int = _MAX_GRAPH_DEPTH,
    max_nodes: int = _MAX_GRAPH_NODES,
) -> ForcedBuildGraphBoundary:
    """Prove the forced candidate's reachable build graph stays in checkout."""
    if orchestrator is None:
        return ForcedBuildGraphBoundary(
            status="unavailable",
            reason_code="graph_probe_io_failed",
        )
    if max_depth < 1 or max_nodes < 1:
        raise ValueError("build-graph limits must be positive")
    normalized_project = _normalized_absolute(project_root)
    normalized_candidate = _normalized_absolute(candidate_root)
    if (
        normalized_project is None
        or normalized_candidate is None
        or not _contained(normalized_candidate, normalized_project)
    ):
        return ForcedBuildGraphBoundary(
            status="unavailable",
            reason_code="candidate_outside_project",
        )
    try:
        if system == "maven":
            visited = _verify_maven(
                orchestrator,
                project_root=normalized_project,
                candidate_root=normalized_candidate,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
        elif system == "gradle":
            visited = _verify_gradle(
                orchestrator,
                project_root=normalized_project,
                candidate_root=normalized_candidate,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
        elif system == "pytest":
            # Pytest has no Maven/Gradle-style settings graph. The candidate
            # realpath containment performed by attempt_policy is sufficient.
            visited = (normalized_candidate,)
        else:
            raise _GraphUnavailable("unsupported_build_system")
    except _GraphUnavailable as exc:
        return ForcedBuildGraphBoundary(
            status="unavailable",
            reason_code=exc.reason_code,
        )
    return ForcedBuildGraphBoundary(
        status="verified",
        reason_code="graph_contained",
        visited_roots=visited,
    )


__all__ = [
    "ForcedBuildGraphBoundary",
    "GraphBoundaryStatus",
    "probe_forced_build_backend",
    "verify_forced_candidate_build_graph",
]
