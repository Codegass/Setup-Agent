# src/sag/metrics/build_scope.py
"""The reactor a CI command selects on one commit.

Exact for the declared scope and subject to no log cliff: `settings.gradle`
includes (with `projectDir` remaps) or the pom `<modules>` tree under the
command's `-P` and `-pl`.  Pure: the caller supplies file text and a reader
for child poms; nothing here touches a filesystem or the network.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import xml.etree.ElementTree as ET
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from sag.metrics.module_keys import module_key, module_keys

_BUILD_TOOLS: dict[str, Literal["maven", "gradle"]] = {
    "mvn": "maven",
    "mvnw": "maven",
    "./mvnw": "maven",
    "gradle": "gradle",
    "gradlew": "gradle",
    "./gradlew": "gradle",
}
# Flags whose value is the NEXT token.
_MAVEN_VALUED = {
    "-P",
    "--activate-profiles",
    "-pl",
    "--projects",
    "-f",
    "--file",
    "-s",
    "--settings",
    "-T",
    "--threads",
    "-rf",
    "--resume-from",
    "-l",
    "--log-file",
}
_GRADLE_VALUED = {
    "-x",
    "--exclude-task",
    "--max-workers",
    "-p",
    "--project-dir",
    "-b",
    "--build-file",
    "-c",
    "--settings-file",
    "-I",
    "--init-script",
}

_GRADLE_INCLUDE_RE = re.compile(r"""\binclude\s*\(?\s*((?:['"][^'"]+['"]\s*,?\s*)+)\)?""")
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")
_GRADLE_PROJECTDIR_RE = re.compile(
    r"""project\s*\(\s*['"](?P<path>:?[^'"]+)['"]\s*\)\s*\.projectDir\s*=\s*"""
    r"""(?:new\s+File\s*\(|file\s*\()\s*(?:rootDir\s*,\s*|rootProject\.projectDir\s*,\s*)?['"](?P<dir>[^'"]+)['"]"""
)


class CiCommand(BaseModel):
    """One build/test invocation as a workflow wrote it, taken apart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: Literal["maven", "gradle", "unknown"] = "unknown"
    goals: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    excluded_tasks: tuple[str, ...] = ()
    text: str = ""
    build_file: str | None = None
    project_dir: str | None = None
    unsupported_scope_reason: str | None = None


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    return list(lexer)


def _invocation_start(tokens: list[str]) -> int | None:
    index = 1 if tokens and tokens[0] == "env" else 0
    while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
        index += 1
    return index if index < len(tokens) and _build_tool(tokens[index]) else None


def _build_tool(executable: str) -> Literal["maven", "gradle"] | None:
    # The dispatch can replace a PATH launcher with an observed absolute path.
    # This identifies syntax only; it does not certify that executable or scope.
    launcher = posixpath.basename(executable) if posixpath.isabs(executable) else executable
    return _BUILD_TOOLS.get(launcher)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_ci_command(text: str) -> CiCommand:
    """Read a literal invocation; unsupported scope never silently loses flags."""

    lines: list[tuple[str, list[str], int]] = []
    unparsed_lines = False
    for line in (text or "").replace("\\\n", " ").splitlines():
        try:
            tokens = _tokens(line.strip())
        except ValueError:
            unparsed_lines = True
            continue
        start = _invocation_start(tokens)
        if start is not None:
            lines.append((line.strip(), tokens, start))
        elif tokens:
            unparsed_lines = True
    if not lines:
        return CiCommand(text=(text or "").strip()[:2_000])
    line, tokens, start = lines[0]
    tool = _build_tool(tokens[start])
    assert tool is not None  # _invocation_start accepted this literal launcher.
    args = tokens[start + 1 :]
    reason = "multiple build invocations" if len(lines) > 1 else None
    if unparsed_lines:
        reason = "script contains unresolved shell context"
    if any(op in args for op in ("&&", "||", ";", "|", ">", ">>", "<", "&")):
        reason = "compound shell command"
    if "$" in line or "`" in line:
        reason = "command contains unresolved interpolation"
    goals: list[str] = []
    profiles: list[str] = []
    projects: list[str] = []
    excluded: list[str] = []
    build_file = project_dir = None
    valued = (
        (_MAVEN_VALUED | {"-D", "--define", "-gs", "--global-settings"})
        if tool == "maven"
        else (_GRADLE_VALUED | {"-D", "-P", "--console", "--warning-mode"})
    )
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"&&", "||", ";", "|", ">", ">>", "<", "&"}:
            break
        flag, equals, attached = token.partition("=")
        if token in {"-fae", "-ff", "-fn"}:
            index += 1
            continue
        if flag not in valued:
            for prefix in ("-pl", "-P", "-f") if tool == "maven" else ("-x", "-p"):
                if token.startswith(prefix) and len(token) > len(prefix):
                    flag, equals, attached = prefix, "=", token[len(prefix) :].lstrip("=")
                    break
        if flag in valued:
            value = attached if equals else (args[index + 1] if index + 1 < len(args) else "")
            index += 1 if equals else 2
            if not value or value.startswith("-"):
                reason = f"missing value for {flag}"
            if tool == "maven" and flag in {"-P", "--activate-profiles"}:
                profiles.extend(_split_csv(value))
            elif tool == "maven" and flag in {"-pl", "--projects"}:
                projects.extend(_split_csv(value))
            elif tool == "maven" and flag in {"-f", "--file"}:
                build_file = value
            elif tool == "gradle" and flag in {"-p", "--project-dir"}:
                project_dir = value
            elif tool == "gradle" and flag in {"-x", "--exclude-task"}:
                excluded.append(value)
            elif flag in {"-D", "--define", "-P"}:
                reason = "property-controlled build scope is unresolved"
            elif flag in {
                "-rf",
                "--resume-from",
                "-b",
                "--build-file",
                "-c",
                "--settings-file",
                "-I",
                "--init-script",
                "-s",
                "--settings",
                "-gs",
                "--global-settings",
            }:
                reason = f"unsupported scope option {flag}"
            continue
        if token in {
            "-am",
            "--also-make",
            "-amd",
            "--also-make-dependents",
            "-N",
            "--non-recursive",
        }:
            reason = f"unsupported scope option {token}"
        elif token.startswith("-D") or (tool == "gradle" and token.startswith("-P")):
            reason = "property-controlled build scope is unresolved"
        elif not token.startswith("-"):
            goals.append(token)
        index += 1
    if any(v.startswith(("!", "-", "?")) for v in profiles + projects):
        reason = "unsupported profile or project selector"
    if tool == "gradle" and any(":" in goal for goal in goals):
        reason = "project-qualified Gradle task scope is unresolved"
    return CiCommand(
        tool=tool,
        goals=tuple(goals),
        profiles=tuple(profiles),
        projects=tuple(projects),
        excluded_tasks=tuple(excluded),
        text=(text or "").strip(),
        build_file=build_file,
        project_dir=project_dir,
        unsupported_scope_reason=reason,
    )


def gradle_project_directories(settings_text: str) -> dict[str, str]:
    """Static included project key to directory; empty means unresolved settings."""

    text = re.sub(r"/\*.*?\*/", "", settings_text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    # A static reader cannot decide conditional includes or evaluate scripts.
    if re.search(r"\b(if|for|while|apply|includeBuild)\b|\.each\b", text):
        return {}
    includes = list(_GRADLE_INCLUDE_RE.finditer(text))
    if len(includes) != len(re.findall(r"\binclude\b", text)):
        return {}
    for include in includes:
        # A regex match of the literal prefix is not a complete include call.
        remainder = text[include.end() :]
        if remainder and include.group(0)[-1] not in "\n\r" and remainder[0] not in "\n\r;":
            return {}
    remap_matches = list(_GRADLE_PROJECTDIR_RE.finditer(text))
    if len(remap_matches) != len(re.findall(r"\.projectDir\s*=", text)):
        return {}
    paths = {".": "."}
    for include in includes:
        for quoted in _QUOTED_RE.findall(include.group(1)):
            if "$" in quoted or ".." in quoted.split("/"):
                return {}
            key = module_key(quoted)
            segments = key.split("/")
            for length in range(1, len(segments) + 1):
                ancestor = "/".join(segments[:length])
                paths[ancestor] = ancestor
    for match in remap_matches:
        directory = match.group("dir")
        if "$" in directory or directory.startswith("/") or ".." in directory.split("/"):
            return {}
        key = module_key(match.group("path"))
        if key not in paths:
            return {}
        paths[key] = module_key(directory)
    return paths


def gradle_declared_projects(settings_text: str) -> tuple[str, ...]:
    """Every statically included project by directory; unresolved scope is empty."""

    return module_keys(gradle_project_directories(settings_text).values())


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node: ET.Element, name: str) -> str | None:
    for child in node:
        if _strip_ns(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _strip_ns(child.tag) == name]


def _parse_pom(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def maven_default_goal(pom_text: str) -> str | None:
    """`<build><defaultGoal>` of a pom, which a bare `mvn` runs."""

    root = _parse_pom(pom_text)
    if root is None:
        return None
    for build in _children(root, "build"):
        goal = _child_text(build, "defaultGoal")
        if goal:
            return " ".join(goal.split())
    return None


def maven_declared_modules(
    root_pom: str,
    read_pom: Callable[[str], str | None],
    *,
    active_profiles: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """The reactor under `-P`/`-pl`, each module as the name Maven prints.

    ``read_pom(relative_dir)`` returns a child pom's text or ``None``; any
    unresolved child withdraws the declared scope rather than shrinking it.
    ``projects`` narrows by directory or artifactId. Callers must not use this
    reader for commands with ``unsupported_scope_reason`` such as ``-am``.
    """

    selected = set(projects)
    if any(p.startswith(("!", "-", "?")) for p in selected | set(active_profiles)):
        return ()
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    unresolved = False

    def visit(directory: str, text: str | None) -> None:
        nonlocal unresolved
        if directory in seen:
            unresolved = True
            return
        seen.add(directory)
        root = _parse_pom(text) if text else None
        if root is None:
            unresolved = True
            return
        artifact_id = _child_text(root, "artifactId")
        name = _child_text(root, "name") or artifact_id
        if not artifact_id or not name or "$" in name:
            unresolved = True
            return
        rows.append((directory, artifact_id, name))
        module_nodes = _children(root, "modules")
        for profiles in _children(root, "profiles"):
            profile_nodes = _children(profiles, "profile")
            explicitly_active = any(_child_text(p, "id") in active_profiles for p in profile_nodes)
            for profile in profile_nodes:
                active = _child_text(profile, "id") in active_profiles
                for activation in _children(profile, "activation"):
                    if not active and any(
                        _strip_ns(c.tag) != "activeByDefault" for c in activation
                    ):
                        unresolved = True
                    if (
                        not explicitly_active
                        and _child_text(activation, "activeByDefault") == "true"
                    ):
                        active = True
                if active:
                    module_nodes.extend(_children(profile, "modules"))
        for modules in module_nodes:
            for child in _children(modules, "module"):
                path = (child.text or "").strip()
                child_dir = posixpath.normpath(posixpath.join(directory, path))
                if not path or "$" in path or child_dir.startswith(("/", "../")):
                    unresolved = True
                    continue
                visit(child_dir, read_pom(child_dir))

    visit(".", root_pom)
    if unresolved or len({name for _, _, name in rows}) != len(rows):
        return ()
    if selected:
        matched = {
            selector
            for selector in selected
            if any(
                selector in {directory, artifact, ":" + artifact} for directory, artifact, _ in rows
            )
        }
        if matched != selected:
            return ()
        rows = [row for row in rows if selected & {row[0], row[1], ":" + row[1]}]
    if len(seen) == 1 and rows:
        return (".",)
    return module_keys(name for _, _, name in rows)


__all__ = [
    "CiCommand",
    "gradle_declared_projects",
    "gradle_project_directories",
    "maven_declared_modules",
    "maven_default_goal",
    "parse_ci_command",
]
