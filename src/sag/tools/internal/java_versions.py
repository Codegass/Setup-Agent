"""One reader for the versions a JVM states about itself.

A registered runtime states its version however the registrar spelled it:
"21", "21.0.9" and the legacy "1.8" are all the same JDK to a build. The
comparison is therefore between MAJORS, so a spelling difference is never a
conflict — and `1.8.0_361` is major 8, never major 1.

The provision path also reads the block its own verification command prints,
because "the JDK I installed" and "the JVM this container now runs" are two
different facts (live camel-quarkus d2r3: `openjdk version "11.0.31"` under
JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, Optional

# `openjdk version "11.0.31"`, `java version "1.8.0_361"` — the quoted string a
# JVM prints for itself.  `javac 11.0.31` is the compiler's own spelling.
_JAVA_RUNTIME_VERSION_RE = re.compile(r'version\s+"([^"]+)"')
_JAVAC_VERSION_RE = re.compile(r"(?:^|\n)\s*javac\s+([0-9][0-9._]*)")
_JAVA_MAJOR_RE = re.compile(r"^(?:1\.)?(\d+)")
# The major an installation PATH states about itself: `java-17-openjdk-amd64`,
# `jdk-21.0.1`, `java-1.8.0-openjdk-amd64`. Deliberately narrow — only the
# `java`/`jdk`/`openjdk` spellings, and only when a number follows them — so a
# path that states no major yields none rather than a guess.
_JAVA_PATH_MAJOR_RE = re.compile(r"(?:openjdk|jdk|java)[-_]?((?:1\.)?\d+)", re.IGNORECASE)
# The separator the provision path echoes between its two probes.
JAVA_VERIFICATION_SEPARATOR = "---"


def java_major(version: Any) -> Optional[str]:
    """The JDK major a version string names, or None if it names none."""
    if version is None:
        return None
    match = _JAVA_MAJOR_RE.match(str(version).strip())
    return match.group(1) if match else None


def java_major_from_path(path: Any) -> Optional[str]:
    """The JDK major an installation path names, or None when it names none.

    geode d2r4 seq 227 registered `/usr/lib/jvm/java-17-openjdk-amd64/bin/java`.
    The path says which JDK the run wanted; a refusal that answers it with a
    `<major>` placeholder makes the model re-supply a fact it already stated,
    and a re-supplied fact is a fact that can come back different.
    """
    match = _JAVA_PATH_MAJOR_RE.search(str(path or ""))
    return java_major(match.group(1)) if match else None


def java_distribution(output: str) -> Optional[str]:
    """Runtime family observed in Java's own banner; absent identity stays absent."""
    output = "\n".join(line for line in output.splitlines() if not line.strip().startswith("/"))
    if "graalvm" in output.lower():
        return "graalvm"
    if "openjdk" in output.lower():
        return "openjdk"
    if "Java(TM)" in output:
        return "oracle"
    return None


def names_bare_java_major(version: Any) -> bool:
    """Whether a version string names a MAJOR and nothing narrower.

    `21` and the legacy `1.8` name a major; `21.0.1` and `1.8.0_361` name one
    particular runtime within it. The distinction is what keeps "the same JDK
    spelled at two precisions" from turning into "any patch of that major
    satisfies a requirement that named one".
    """
    return _JAVA_MAJOR_RE.fullmatch(str(version or "").strip()) is not None


def parse_java_verification(output: str) -> Dict[str, Optional[str]]:
    """Read the runtime and compiler versions out of one verification block.

    The block is what `java -version` and `javac -version` printed under the
    JAVA_HOME a provision just configured, separated by an echoed `---`.
    Reading it is the only way the provision can know which JVM its own
    configuration actually activated.
    """
    text = str(output or "")
    runtime_text, _, compiler_text = text.partition(f"\n{JAVA_VERIFICATION_SEPARATOR}")
    runtime_match = _JAVA_RUNTIME_VERSION_RE.search(runtime_text)
    compiler_match = _JAVAC_VERSION_RE.search(compiler_text or text)
    return {
        "java_version": runtime_match.group(1) if runtime_match else None,
        "javac_version": compiler_match.group(1) if compiler_match else None,
    }


def _java_version_tuple(raw: Any) -> Optional[tuple[int, ...]]:
    """Numeric Enforcer versions only; unresolved/qualified versions stay unknown."""
    value = str(raw or "").strip()
    if not re.fullmatch(r"\d+(?:[._-]\d+){0,3}", value):
        return None
    parts = [int(part) for part in re.split(r"[._-]", value)]
    if len(parts) > 1 and parts[0] == 1:
        parts.pop(0)
    return tuple(parts + [0] * (4 - len(parts)))


@dataclass(frozen=True)
class _JavaRange:
    lower: Optional[tuple[int, ...]] = None
    upper: Optional[tuple[int, ...]] = None
    lower_closed: bool = True
    upper_closed: bool = True

    def contains(self, version: tuple[int, ...]) -> bool:
        return not (
            (
                self.lower is not None
                and (version < self.lower or (version == self.lower and not self.lower_closed))
            )
            or (
                self.upper is not None
                and (version > self.upper or (version == self.upper and not self.upper_closed))
            )
        )

    def intersect(self, other: "_JavaRange") -> "_JavaRange":
        lower = max((v for v in (self.lower, other.lower) if v is not None), default=None)
        upper = min((v for v in (self.upper, other.upper) if v is not None), default=None)
        return _JavaRange(
            lower,
            upper,
            all(
                bound != lower or closed
                for bound, closed in (
                    (self.lower, self.lower_closed),
                    (other.lower, other.lower_closed),
                )
            ),
            all(
                bound != upper or closed
                for bound, closed in (
                    (self.upper, self.upper_closed),
                    (other.upper, other.upper_closed),
                )
            ),
        )

    @property
    def empty(self) -> bool:
        return bool(
            self.lower is not None
            and self.upper is not None
            and (
                self.lower > self.upper
                or (self.lower == self.upper and not (self.lower_closed and self.upper_closed))
            )
        )


def _java_range(raw: Any) -> Optional[_JavaRange]:
    value = str(raw or "").strip()
    bare = _java_version_tuple(value)
    if bare is not None:
        return _JavaRange(lower=bare)  # Enforcer bare versions are MINIMUMS.
    if value.startswith("[") and value.endswith("]") and "," not in value:
        exact = _java_version_tuple(value[1:-1])
        return _JavaRange(exact, exact) if exact is not None else None
    match = re.fullmatch(r"([\[(])\s*([^,\[\]()]*),\s*([^,\[\]()]*)\s*([\])])", value)
    if not match:
        return None  # Unions and property expressions need Maven's own resolution.
    start, lower_text, upper_text, end = match.groups()
    lower = _java_version_tuple(lower_text) if lower_text.strip() else None
    upper = _java_version_tuple(upper_text) if upper_text.strip() else None
    if (lower_text.strip() and lower is None) or (upper_text.strip() and upper is None):
        return None
    if lower is None and upper is None:
        return None
    return _JavaRange(lower, upper, start == "[", end == "]")


def java_constraint_matches(constraint: str, version: str) -> Optional[bool]:
    """Match one supported Enforcer constraint without reducing it to a major."""
    bound, actual = _java_range(constraint), _java_version_tuple(version)
    return bound.contains(actual) if bound is not None and actual is not None else None


def java_requirements_range(
    requirements: Dict[str, Any], *, observed_major: Optional[str] = None
) -> Optional[_JavaRange]:
    """Intersect the small set of proved JVM constraints, not a Maven model solver."""
    bound = _JavaRange()
    for entry in requirements.get("runtime", []):
        parsed = _java_range(entry.get("constraint"))
        if parsed is None:
            return None
        bound = bound.intersect(parsed)
    release = requirements.get("compiler_release")
    if release and not requirements.get("compiler_toolchain"):
        parsed = _java_range(release)
        if parsed is None:
            return None
        bound = bound.intersect(parsed)
    if observed_major:
        major = java_major(observed_major)
        if major is None:
            return None
        bound = bound.intersect(
            _JavaRange((int(major), 0, 0, 0), (int(major) + 1, 0, 0, 0), True, False)
        )
    return bound


def java_requirement_candidate(
    requirements: Dict[str, Any],
    *,
    active_version: Optional[str] = None,
    observed_major: Optional[str] = None,
) -> Optional[str]:
    bound = java_requirements_range(requirements, observed_major=observed_major)
    if bound is None or bound.empty:
        return None
    active = _java_version_tuple(active_version)
    if active is not None and bound.contains(active):
        return str(active[0])
    if requirements.get("unresolved"):
        return None
    # A major is only an install candidate; the exact activated version is
    # checked against the full range after provisioning. Never claim that
    # installing "11" proves [11] or a patch-specific bound.
    if bound.lower is not None:
        candidate = bound.lower[0]
    elif bound.upper is not None:
        candidate = bound.upper[0] - (not bound.upper_closed)
    else:
        return None
    if candidate < 1:
        return None
    major_floor = (candidate, 0, 0, 0)
    if bound.upper is not None and (
        major_floor > bound.upper or (major_floor == bound.upper and not bound.upper_closed)
    ):
        return None
    return str(candidate)


def _compiler_args_may_select_java(element: ET.Element) -> bool:
    """Diagnostic switches alone do not declare a Java version or toolchain.

    Keep unknown arguments, interpolation and empty overrides unresolved. This
    does not judge whether a given javac supports a warning flag; the actual
    runner reports compiler-option and plugin compatibility failures.
    """
    import shlex

    name = element.tag.rsplit(".", 1)[-1]
    if name == "compilerArgument":
        values = [element.text or ""] if not list(element) else []
    elif name == "compilerArgs":
        values = [child.text or "" for child in element if not list(child)]
        if len(values) != len(element) or (element.text or "").strip():
            return True
    else:  # Legacy compilerArguments map prepends '-' to each key.
        values = [f"-{child.tag} {child.text or ''}" for child in element if not list(child)]
        if len(values) != len(element) or (element.text or "").strip():
            return True
    try:
        args = [arg for value in values for arg in shlex.split(value)]
    except ValueError:
        return True
    return not args or any(
        "${" in arg
        or (
            arg not in {"-Xlint", "-nowarn", "-deprecation", "-verbose", "-Werror"}
            and not arg.startswith("-Xlint:")
        )
        for arg in args
    )


def _profile_declares_java_requirement(profile: ET.Element) -> bool:
    """Track conditional Java declarations, not merely a compiler plugin name.

    A profile that only changes testIncludes does not declare another Java
    version. Conversely Maven compiler properties can override a version even
    when the profile contains no plugin element. Plugin execution and plugin
    dependency compatibility remain the native runner's responsibility.
    """
    compiler_options = {
        "release",
        "source",
        "target",
        "testRelease",
        "testSource",
        "testTarget",
        "jdkToolchain",
        "executable",
        "fork",
        "compilerId",
        "compilerVersion",
    }
    argument_options = {"compilerArgs", "compilerArgument", "compilerArguments"}
    properties = {"java.version"} | {
        "maven.compiler." + key for key in compiler_options | argument_options
    }
    if any(element.tag in properties for element in profile.findall("./properties/*")):
        return True
    if any(True for _ in profile.iter("requireJavaVersion")):
        return True
    return any(
        plugin.findtext("artifactId") == "maven-toolchains-plugin"
        or (
            plugin.findtext("artifactId") == "maven-compiler-plugin"
            and any(
                element.tag in compiler_options
                or (element.tag in argument_options and _compiler_args_may_select_java(element))
                for element in plugin.iter()
            )
        )
        for plugin in profile.iter("plugin")
    )


def maven_java_requirements(
    poms: list[tuple[str, str]],
    *,
    disabled_profiles: frozenset[str] = frozenset(),
    enabled_profiles: frozenset[str] = frozenset(),
    runtime_version: str | None = None,
) -> Dict[str, Any]:
    """Preserve declared runtime bounds and compiler requirements with their source.

    Explicit selections and simple JDK-only activation can be resolved.
    Other activation, remote parents, toolchain selection and arbitrary
    interpolation remain explicit unresolved facts.
    """
    result: Dict[str, Any] = {
        "runtime": [],
        "compiler_release": None,
        "compiler_source": None,
        "compiler_toolchain": False,
        "unresolved": [],
    }
    for content, location in poms:
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            result["unresolved"].append(f"{location}:xml_unreadable")
            continue
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        profiles = root.find("./profiles")
        active_profiles = []
        if profiles is not None:
            for profile in list(profiles):
                name = (profile.findtext("id") or "").strip()
                activation = profile.find("activation")
                applies = None
                if name in enabled_profiles:
                    applies = True
                elif (
                    runtime_version
                    and activation is not None
                    and [e.tag for e in activation] == ["jdk"]
                ):
                    raw = (activation.findtext("jdk") or "").strip()
                    # Maven's bare JDK activation is a prefix, unlike an
                    # Enforcer lower bound. Qualified/compound values stay unknown.
                    if raw.startswith(("[", "(")):
                        applies = java_constraint_matches(raw, runtime_version)
                    elif re.fullmatch(r"!?\d+(?:\.\d+)*", raw):
                        applies = runtime_version.startswith(raw.lstrip("!"))
                        if raw.startswith("!"):
                            applies = not applies
                if name in disabled_profiles or applies is False:
                    profiles.remove(profile)
                elif applies is True:
                    active_profiles.append(profile)
        # Resolve only one local, unconditional literal property. This covers
        # <release>${javaVersion}</release> without implementing Maven's model
        # interpolation or guessing which profile/parent supplies a value.
        properties = {}
        ambiguous = set()
        for element in root.findall("./properties/*"):
            if element.tag in properties:
                ambiguous.add(element.tag)
            properties[element.tag] = (element.text or "").strip()
        for profile in root.findall("./profiles/profile"):
            for element in profile.findall("./properties/*"):
                if profile in active_profiles:
                    properties[element.tag] = (element.text or "").strip()
                else:
                    ambiguous.add(element.tag)

        def local_literal(value):
            match = re.fullmatch(r"\$\{([^{}]+)\}", value)
            if match and match[1] not in ambiguous:
                literal = properties.get(match[1], "")
                if literal and "${" not in literal:
                    return literal
            return value

        for rule in root.iter("requireJavaVersion"):
            raw = (rule.findtext("version") or "").strip()
            if raw:
                result["runtime"].append(
                    {"constraint": local_literal(raw), "source": f"{location}:requireJavaVersion"}
                )
        for profile in root.findall("./profiles/profile"):
            if profile not in active_profiles and _profile_declares_java_requirement(profile):
                result["unresolved"].append(
                    f"{location}:profile_activation:{profile.findtext('id') or 'unknown'}"
                )
            elif profile in active_profiles and any(
                element.tag in {"compilerArgs", "compilerArgument", "compilerArguments"}
                and _compiler_args_may_select_java(element)
                for plugin in profile.iter("plugin")
                if plugin.findtext("artifactId") == "maven-compiler-plugin"
                for element in plugin.iter()
            ):
                result["unresolved"].append(
                    f"{location}:profile_compiler_arguments:{profile.findtext('id') or 'unknown'}"
                )
        compiler_plugins = [
            plugin
            for plugin in root.iter("plugin")
            if plugin.findtext("artifactId") == "maven-compiler-plugin"
        ]
        if result["compiler_release"] is None:
            compiler_unresolved = []
            for tag in (
                "maven.compiler.release",
                "maven.compiler.target",
                "maven.compiler.source",
                "java.version",
                "release",
                "target",
                "source",
            ):
                if "." in tag:
                    values = [local_literal(properties[tag])] if tag in properties else []
                else:
                    active_elements = [
                        element
                        for profile in active_profiles
                        for plugin in profile.iter("plugin")
                        if plugin.findtext("artifactId") == "maven-compiler-plugin"
                        for element in plugin.iter(tag)
                    ]
                    elements = active_elements or [
                        element for plugin in compiler_plugins for element in plugin.iter(tag)
                    ]
                    values = [local_literal((element.text or "").strip()) for element in elements]
                if len(set(values)) > 1:
                    result["unresolved"].append(f"{location}:{tag}:multiple_values")
                value = next((value for value in values if names_bare_java_major(value)), None)
                if value:
                    result["compiler_release"] = java_major(value)
                    result["compiler_source"] = f"{location}:{tag}"
                    break
                if values:
                    compiler_unresolved.append(f"{location}:{tag}:{values[0]}")
            if result["compiler_release"] is None:
                result["unresolved"].extend(compiler_unresolved)
        if any(
            plugin.find(".//jdkToolchain") is not None
            or (
                plugin.find(".//executable") is not None
                and any(
                    (element.text or "").strip().lower() == "true"
                    for element in plugin.iter("fork")
                )
            )
            for plugin in compiler_plugins
        ) or any(
            plugin.findtext("artifactId") == "maven-toolchains-plugin"
            for plugin in root.iter("plugin")
        ):
            result["compiler_toolchain"] = True
    return result


def resolve_maven_java_requirements(
    orchestrator,
    manifest,
    *,
    runtime_version=None,
    disabled_profiles=frozenset(),
    enabled_profiles=frozenset(),
):
    """Re-evaluate the pinned root POM only if it reproduces the entire survey.

    Used by the invocation reader; a different POM or additional
    inherited requirements cannot be erased by a convenient local parse.
    """
    import posixpath
    from sag.agent.physical_survey import config_fingerprint
    from sag.runtime.container_io import read_container_text

    requirements = manifest.get("java_requirements")
    if not isinstance(requirements, dict) or not requirements.get("unresolved"):
        return requirements
    survey = manifest.get("survey") or {}
    root, fingerprint = survey.get("project_path"), survey.get("config_fingerprint")
    if not root or not fingerprint or not survey.get("target_sha"):
        return requirements
    if config_fingerprint(orchestrator, root) != fingerprint:
        return requirements
    path = posixpath.join(root, "pom.xml")
    content = read_container_text(orchestrator, path, exact_bytes=True)
    if content is None or len(content.encode("utf-8")) > 1024 * 1024:
        return requirements
    poms = [(content, path)]
    if (
        maven_java_requirements(poms) != requirements
        or config_fingerprint(orchestrator, root) != fingerprint
    ):
        return requirements
    return maven_java_requirements(
        poms,
        runtime_version=runtime_version,
        disabled_profiles=disabled_profiles,
        enabled_profiles=enabled_profiles,
    )
