"""Build backends: ecosystem-specific verb mappings (spec §4 growth law).

Each backend maps the invariant verbs (deps/compile/test/package) onto an
underlying tool. Stage 1 delegates to the existing MavenTool/GradleTool;
later ecosystems (python/node) add a module here, never a schema change.
"""

from typing import Any, Dict, Optional

from sag.tools.base import ToolResult

# Marker files probed (in priority order) to select a backend.
# python comes AFTER maven/gradle on purpose: a JVM repo with a stray
# requirements.txt (docs tooling, scripts) must stay JVM — dict order IS the
# probe order in BuildTool._detect_system.
BUILD_MARKERS = {
    "maven": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle",
               "settings.gradle.kts", "gradlew"),
    "python": ("pyproject.toml", "setup.py", "requirements.txt", "Pipfile"),
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
            # Single pre-flight ownership: the facade (BuildTool.execute) runs
            # the JDK pre-flight, bounded retry and [scope] narration BEFORE
            # delegating here; the internal tool must not run them again
            # (duplicate probes, duplicate narration, a second rerun).
            "_env_preflight": False,
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


class PythonBackend:
    VERBS = {
        "deps": "setup_env",
        "compile": "compile",
        "test": "test",
        # Both packaging verbs map to the wheel build: Python has no local-repo
        # install step to mirror Maven's, and the wheel is extra evidence only
        # (spec settled decision: never required for a green verdict).
        "package": "build",
        "install": "build",
    }

    def __init__(self, python_tool):
        self.python_tool = python_tool

    def run(self, verb: str, args: Optional[str], working_directory: str,
            timeout: Optional[int]) -> ToolResult:
        kwargs: Dict[str, Any] = {
            "operation": self.VERBS[verb],
            "working_directory": working_directory,
        }
        if args:
            kwargs["args"] = args
        if timeout:
            kwargs["timeout"] = timeout
        return self.python_tool.execute(**kwargs)


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
            # Single pre-flight ownership: the facade owns pre-flight/retry/
            # [scope] on this path (see MavenBackend.run).
            "_env_preflight": False,
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
