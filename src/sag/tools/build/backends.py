"""Build backends: ecosystem-specific verb mappings (spec §4 growth law).

Each backend maps the invariant verbs (deps/compile/test/package) onto an
underlying tool. Stage 1 delegates to the existing MavenTool/GradleTool;
later ecosystems (python/node) add a module here, never a schema change.
"""

from typing import Any, Dict, Optional

from sag.tools.base import ToolResult

# Marker files probed (in priority order) to select a backend.
BUILD_MARKERS = {
    "maven": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle",
               "settings.gradle.kts", "gradlew"),
}


class MavenBackend:
    VERBS = {
        "deps": "dependency:resolve",
        "compile": "compile",
        "test": "test",
        "package": "package",
        # A reactor whose modules depend on siblings' produced artifacts (shaded
        # jars, code-gen, packaged deps) needs those installed to the local repo so
        # later phases resolve them; `compile`/`package` alone don't (e.g.
        # cassandra-java-driver core needs the shaded-guava jar).
        "install": "install",
    }

    def __init__(self, maven_tool):
        self.maven_tool = maven_tool

    def run(self, verb: str, args: Optional[str], working_directory: str,
            timeout: Optional[int]) -> ToolResult:
        kwargs: Dict[str, Any] = {
            "command": self.VERBS[verb],
            "working_directory": working_directory,
        }
        # --fail-at-end for every reactor-building verb (not just test): one pass
        # builds ALL modules and reports every module's failure at once, instead
        # of aborting at the first error and making the agent rediscover failures
        # one module per iteration. Pairs with the coverage-based build verdict
        # (a partial compile -> PARTIAL listing the modules that failed).
        if verb in ("compile", "package", "test", "install"):
            kwargs["fail_at_end"] = True
        if args:
            kwargs["extra_args"] = args
        if timeout:
            kwargs["timeout"] = timeout
        return self.maven_tool.execute(**kwargs)


class GradleBackend:
    VERBS = {
        "deps": "dependencies",
        "compile": "compileJava",
        "test": "test",
        "package": "assemble",
        # Gradle resolves sibling modules via project() deps in-build, so there is
        # no local-repo install step to mirror Maven's; `assemble` builds every
        # subproject's artifacts, which is the closest equivalent.
        "install": "assemble",
    }

    def __init__(self, gradle_tool):
        self.gradle_tool = gradle_tool

    def run(self, verb: str, args: Optional[str], working_directory: str,
            timeout: Optional[int]) -> ToolResult:
        kwargs: Dict[str, Any] = {
            "tasks": self.VERBS[verb],
            "working_directory": working_directory,
        }
        # Gradle's equivalent of Maven --fail-at-end is --continue (set via
        # fail_at_end=True): build every subproject in one pass and report all
        # failures, rather than stopping at the first.
        if verb in ("compile", "package", "test", "install"):
            kwargs["fail_at_end"] = True
        if args:
            kwargs["gradle_args"] = args
        if timeout:
            kwargs["timeout"] = timeout
        return self.gradle_tool.execute(**kwargs)
