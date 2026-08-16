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
