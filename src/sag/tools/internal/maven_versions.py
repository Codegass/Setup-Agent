"""One reader for the version Apache Maven states about itself, and one table
of the distributions a requested floor can be answered with.

A Maven version reaches this harness three ways and all three are read here:
the banner `mvn -version` prints (ANSI escapes included — the live jackrabbit
receipt recorded `"\\u001b[1mApache Maven 3.8.7\\u001b[m"`), the requirement a
build states (`[3.9,)`, `3.9.6`), and the floor a provision is asked for
("3.9"). Comparison is by numeric components, so `3.9.9` satisfies `3.9` and
`3.8.7` does not — which is the entire jackrabbit/gora wall in one sentence.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# `Apache Maven 3.9.9 (8e8579a...)` — the banner Maven prints for itself.
_MAVEN_BANNER_RE = re.compile(r"(?:^|\n)\s*Apache Maven\s+([0-9]+(?:\.[0-9]+){0,3})\b")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}")
_FLOOR_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}\Z")

# The last release of each Maven 3 line, which is what an `apache-maven-X-bin`
# archive is published as. A floor names a LINE ("3.9"); this table names the
# one distribution that answers it. Maven publishes every one of these under
# https://archive.apache.org/dist/maven/maven-3/<version>/binaries/.
MAVEN_DISTRIBUTIONS: Dict[str, str] = {
    "3.3": "3.3.9",
    "3.5": "3.5.4",
    "3.6": "3.6.3",
    "3.8": "3.8.8",
    "3.9": "3.9.9",
}
# The default standalone distribution: the newest line this harness installs.
MAVEN_PROVISION_VERSION = MAVEN_DISTRIBUTIONS["3.9"]


def parse_maven_version(output: Any) -> Optional[str]:
    """The version an `mvn -version` block names, or None when it names none."""
    text = _ANSI_ESCAPE_RE.sub("", str(output or ""))
    match = _MAVEN_BANNER_RE.search(text)
    return match.group(1) if match else None


def maven_version_tuple(version: Any) -> Tuple[int, ...]:
    """The numeric components of a version, for comparison."""
    return tuple(int(part) for part in re.findall(r"\d+", str(version or "")))


def normalize_maven_floor(raw: Any) -> Optional[str]:
    """One requested floor as `major[.minor[.patch]]`, or None when it is not one.

    Deliberately strict: a floor selects a download, and guessing what an
    unparseable string meant is how a provision installs a Maven nobody asked
    for.
    """
    cleaned = str(raw or "").strip()
    return cleaned if cleaned and _FLOOR_RE.fullmatch(cleaned) else None


def satisfies_maven_floor(version: Any, floor: Any) -> bool:
    """Whether an observed version is at or above the floor.

    Component-wise, so `3.9` is met by `3.9.9` and not by `3.8.7`.
    """
    observed = maven_version_tuple(version)
    wanted = maven_version_tuple(floor)
    return bool(observed) and bool(wanted) and observed >= wanted


def maven_distribution_for_floor(floor: Any) -> Optional[str]:
    """The exact distribution version that answers this floor, or None.

    Every floor resolves through the table above, because the table is what
    this harness can actually download. A floor naming a line takes that line's
    last release; a floor naming a patch takes the same release when it
    satisfies the patch, and NONE when it does not — `3.7.1` used to be handed
    back verbatim, which named `apache-maven-3.7.1-bin.tar.gz`, an archive
    Apache never published, and told `nearest_installable_floor` that a floor
    with no line was installable. A floor naming only a major takes the newest
    line of it.
    """
    normalized = normalize_maven_floor(floor)
    if normalized is None:
        return None
    parts = normalized.split(".")
    if len(parts) >= 3:
        release = MAVEN_DISTRIBUTIONS.get(".".join(parts[:2]))
        return release if release and satisfies_maven_floor(release, normalized) else None
    if len(parts) == 2:
        return MAVEN_DISTRIBUTIONS.get(normalized)
    lines = [
        version for line, version in MAVEN_DISTRIBUTIONS.items() if line.split(".")[0] == normalized
    ]
    return max(lines, key=maven_version_tuple) if lines else None


def nearest_installable_floor(floor: Any) -> Optional[str]:
    """The lowest floor this harness can actually install that satisfies `floor`.

    `floor_from_requirement` reads whatever lower bound a build states, and a
    build may state one no published `apache-maven-X-bin` archive answers —
    `[3.7,)` is a real Maven range and there is no 3.7 line. Naming
    `maven_version='3.7'` as a move offers a call whose only possible answer is
    MAVEN_DISTRIBUTION_UNKNOWN, which is the #19 defect class: guidance that
    cannot be acted on. The nearest line whose distribution SATISFIES the ask is
    a move that can be acted on; when no line does, there is no move and the
    caller must offer none rather than invent one.
    """
    normalized = normalize_maven_floor(floor)
    if normalized is None:
        return None
    if maven_distribution_for_floor(normalized) is not None:
        return normalized
    satisfying = [
        line
        for line, version in MAVEN_DISTRIBUTIONS.items()
        if satisfies_maven_floor(version, normalized)
    ]
    return min(satisfying, key=maven_version_tuple) if satisfying else None


def floor_from_requirement(requirement: Any) -> Optional[str]:
    """The floor a stated requirement asks for: `[3.9,)` -> `3.9`, `3.9.6` -> `3.9.6`.

    The first version a requirement names is its lower bound in every spelling
    this harness has met — Maven ranges (`[3.9,)`, `(3.6,4.0)`), bare pins, and
    the `>=`/`~` forms `ToolVersionRequirement` normalizes.
    """
    match = _VERSION_RE.search(str(requirement or ""))
    return normalize_maven_floor(match.group(0)) if match else None


def maven_installable_floors() -> str:
    """The lines this harness can install, for a refusal to name."""
    return ", ".join(sorted(MAVEN_DISTRIBUTIONS, key=maven_version_tuple))
