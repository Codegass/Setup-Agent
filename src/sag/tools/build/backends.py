"""Build backends: ecosystem-specific verb mappings (spec §4 growth law).

Each backend maps the invariant verbs (deps/compile/test/package) onto an
underlying tool. Stage 1 delegates to the existing MavenTool/GradleTool;
later ecosystems (python/node) add a module here, never a schema change.
"""

import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sag.runtime.container_io import read_container_text
from sag.tools.base import ActualToolExecution, OutputPersistenceError, ToolResult

# Verbs that produce local artifacts. Packaging is NOT a test owner — the `test`
# verb is the only one (live bigtop: a naked `mvn install` ran environment-
# dependent tests during the BUILD phase and manufactured a failure that belongs
# to another environment). The contract rides on the VERB, never on a phase name.
PACKAGING_VERBS = ("package", "install")

MAVEN_SKIP_TESTS_ARG = "-DskipTests"
GRADLE_EXCLUDE_TEST_ARGS = "-x test"

_TEST_OWNERSHIP_REASON = "packaging must not run tests — the 'test' verb owns test execution"

# Caller-owned test policy: an explicit skip or an explicit test selection in the
# caller's argv means the caller already decided, and the backend adds nothing.
# Surveyed lifecycle flags (bigtop's skipTests/skipITs) must survive verbatim
# into execution instead of being doubled or overridden.
_MAVEN_CALLER_TEST_FLAGS = re.compile(
    r"(?:^|\s)-D(?:skipTests|skipITs|maven\.test\.skip|test|it\.test)\b"
)
_GRADLE_CALLER_TEST_FLAGS = re.compile(
    r"(?:^|\s)(?:-x[\s=]|--exclude-task[\s=]|--tests[\s=]|-[DP]skipTests\b)"
)


@dataclass(frozen=True)
class ExecutedAction:
    """What a backend actually ran, and why that differs from the verb.

    Empty `reasons` means a pure task-name translation (compile -> compileJava):
    renaming the same lifecycle is not a semantic change and needs no narration.
    A non-empty `reasons` IS a semantic delta, and the facade narrates it before
    the model reads the result.
    """

    argv_fragment: str
    reasons: Tuple[str, ...] = ()


def _appended(args: Optional[str], addition: str) -> str:
    return " ".join(part for part in ((args or "").strip(), addition) if part)


def _backend_added(recorded: Any, caller_args: Optional[str], flag: str) -> bool:
    """True when `flag` is in the executed argv because the BACKEND put it there."""
    return flag in str(recorded or "") and flag not in (caller_args or "")


# Marker files probed (in priority order) to select a backend.
# python comes AFTER maven/gradle on purpose: a JVM repo with a stray
# requirements.txt (docs tooling, scripts) must stay JVM — dict order IS the
# probe order in BuildTool._detect_system.
BUILD_MARKERS = {
    "maven": ("pom.xml",),
    "gradle": (
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradlew",
    ),
    "python": ("pyproject.toml", "setup.py", "requirements.txt", "Pipfile"),
}


class MavenBackend:
    VERBS = {
        "deps": "dependency:resolve",
        "compile": "compile",
        # Maven's `test` phase omits failsafe/integration tests. The invariant
        # build(action='test') contract means the full project test lifecycle,
        # so route through `verify`; MavenTool still adds fail-at-end and the
        # bounded test-failure-ignore policy for a complete reactor rollup.
        "test": "verify",
        "package": "package",
        # A reactor whose modules depend on siblings' produced artifacts (shaded
        # jars, code-gen, packaged deps) needs those installed to the local repo so
        # later phases resolve them; `compile`/`package` alone don't (e.g.
        # cassandra-java-driver core needs the shaded-guava jar).
        "install": "install",
    }

    def __init__(self, maven_tool):
        self.maven_tool = maven_tool

    @staticmethod
    def _extra_args(verb: str, args: Optional[str]) -> Optional[str]:
        """The caller's args plus the packaging test-skip contract."""
        if verb not in PACKAGING_VERBS or _MAVEN_CALLER_TEST_FLAGS.search(args or ""):
            return args
        return _appended(args, MAVEN_SKIP_TESTS_ARG)

    @staticmethod
    def executed_action(verb: str, params: Dict[str, Any], args: Optional[str]) -> ExecutedAction:
        """The Maven lifecycle actually dispatched, and its semantic delta."""
        goal = str(params.get("command") or verb)
        added = [
            flag
            for flag in (MAVEN_SKIP_TESTS_ARG,)
            if _backend_added(params.get("extra_args"), args, flag)
        ]
        return ExecutedAction(
            argv_fragment=" ".join([goal] + added),
            reasons=(_TEST_OWNERSHIP_REASON,) if added else (),
        )

    def execute(
        self,
        verb: str,
        args: Optional[str],
        working_directory: str,
        timeout: Optional[int],
        maven_version_requirement: Optional[str] = None,
    ) -> ActualToolExecution:
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
        extra_args = self._extra_args(verb, args)
        if extra_args:
            kwargs["extra_args"] = extra_args
        if timeout:
            kwargs["timeout"] = timeout
        if maven_version_requirement:
            kwargs["maven_version_requirement"] = maven_version_requirement
        try:
            result = self.maven_tool.execute(**kwargs)
        except OutputPersistenceError as exc:
            raise exc.attach_invocation("maven", kwargs)
        return ActualToolExecution("maven", kwargs, result)

    def run(
        self,
        verb: str,
        args: Optional[str],
        working_directory: str,
        timeout: Optional[int],
        maven_version_requirement: Optional[str] = None,
    ) -> ToolResult:
        return self.execute(
            verb,
            args,
            working_directory,
            timeout,
            maven_version_requirement=maven_version_requirement,
        ).result


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

    @staticmethod
    def executed_action(verb: str, params: Dict[str, Any], args: Optional[str]) -> ExecutedAction:
        """The python operation dispatched. No test-skip contract applies: the
        wheel build runs no tests, so packaging already owns nothing of test."""
        return ExecutedAction(argv_fragment=str(params.get("operation") or verb))

    def execute(
        self, verb: str, args: Optional[str], working_directory: str, timeout: Optional[int]
    ) -> ActualToolExecution:
        kwargs: Dict[str, Any] = {
            "operation": self.VERBS[verb],
            "working_directory": working_directory,
        }
        if args:
            kwargs["args"] = args
        if timeout:
            kwargs["timeout"] = timeout
        try:
            result = self.python_tool.execute(**kwargs)
        except OutputPersistenceError as exc:
            raise exc.attach_invocation("python", kwargs)
        return ActualToolExecution("python", kwargs, result)

    def run(
        self, verb: str, args: Optional[str], working_directory: str, timeout: Optional[int]
    ) -> ToolResult:
        return self.execute(verb, args, working_directory, timeout).result


class GradleBackend:
    # compileJava is the JVM BASELINE of the compile verb, never its whole
    # answer: GradleBackend.execute runs the union of the compile tasks the
    # project's own source directories require (see _compile_tasks). The key
    # stays for callers that translate a verb without a container to probe
    # (tool_recovery's delegate path).
    VERBS = {
        "deps": "dependencies",
        "compile": "compileJava",
        "test": "test",
        "package": "assemble",
        # Within ONE gradle build, project() deps resolve in-build — but
        # independent build ISLANDS consume each other's artifacts through the
        # local maven repo (live bigtop: transaction-queue failed 13x resolving
        # data-generators' SNAPSHOT because install ran assemble and never
        # published). install therefore publishes to ~/.m2 when the project
        # applies the maven-publish plugin; without the plugin that task would
        # fail, so assemble stays the fallback (see _install_task).
        "install": "assemble",
    }

    _GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")
    _GRADLE_SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")

    # src/main/<lang> directories whose presence ADDS a compile task. compileJava
    # is unconditional (the JVM baseline); these are the languages a hardcoded
    # compileJava silently skipped — live bigtop: bigpetstore-spark is Scala
    # only, so `compileJava NO-SOURCE` was scored as a green compile over a
    # module that was never compiled.
    _COMPILE_LANGUAGE_TASKS = {
        "groovy": "compileGroovy",
        "kotlin": "compileKotlin",
        "scala": "compileScala",
    }
    _COMPILE_BASELINE_TASK = "compileJava"

    def __init__(self, gradle_tool):
        self.gradle_tool = gradle_tool

    def _install_task(self, working_directory: str) -> str:
        """publishToMavenLocal when the build applies maven-publish, else assemble.

        A plain 'maven-publish' substring match covers both DSLs (apply plugin:
        'maven-publish' — incl. subprojects{} blocks — and plugins{} entries);
        the string has no other meaning in gradle build files.
        """
        orch = getattr(self.gradle_tool, "orchestrator", None)
        if orch is None:
            return "assemble"
        root = working_directory.rstrip("/")
        for name in self._GRADLE_BUILD_FILES:
            try:
                content = read_container_text(orch, f"{root}/{name}")
            except Exception:
                continue
            if content is not None and "maven-publish" in content:
                return "publishToMavenLocal"
        return "assemble"

    def _subproject_dirs(self, orch: Any, root: str) -> List[str]:
        """Subproject directories the settings file itself declares.

        A gradle multi-project compiles its children in the same invocation, so
        their source languages belong to this build. Only `include` coordinates
        are read (both DSLs); a child whose projectDir was reassigned resolves to
        a path that simply does not exist, so the probe under-reports rather than
        inventing a task.
        """
        dirs: List[str] = []
        for name in self._GRADLE_SETTINGS_FILES:
            try:
                content = read_container_text(orch, f"{root}/{name}")
            except Exception:
                continue
            if not content:
                continue
            for statement in re.finditer(r"^\s*include\b(.*)$", content, re.MULTILINE):
                for raw in re.findall(r"""['"]([^'"]+)['"]""", statement.group(1)):
                    path = raw.strip().strip(":").replace(":", "/")
                    if path and path not in dirs:
                        dirs.append(path)
        return dirs

    @staticmethod
    def _existing_directories(orch: Any, paths: Sequence[str]) -> List[str]:
        """The subset of `paths` that exist as directories in the container."""
        if not paths:
            return []
        quoted = " ".join(shlex.quote(path) for path in paths)
        probe = f'for d in {quoted}; do test -d "$d" && echo "$d"; done'
        try:
            result = orch.execute_command(probe, workdir=None, timeout=30)
        except Exception:
            return []
        if not isinstance(result, dict):
            return []
        return [
            line.strip() for line in str(result.get("output") or "").splitlines() if line.strip()
        ]

    def _probe_compile_languages(self, working_directory: str) -> List[str]:
        """Compile languages this build has SOURCE DIRECTORIES for.

        Directory existence only — never a plugin name, a file-extension count or
        a project name. Facts the container can confirm, nothing else.
        """
        orch = getattr(self.gradle_tool, "orchestrator", None)
        if orch is None:
            return []
        root = working_directory.rstrip("/")
        roots = [root] + [f"{root}/{sub}" for sub in self._subproject_dirs(orch, root)]
        candidates = {
            f"{base}/src/main/{lang}": lang
            for base in roots
            for lang in self._COMPILE_LANGUAGE_TASKS
        }
        found = set(self._existing_directories(orch, sorted(candidates)))
        return sorted({lang for path, lang in candidates.items() if path in found})

    def _compile_tasks(self, languages: Sequence[str]) -> str:
        """compileJava plus one compile task per language with sources present."""
        tasks = [self._COMPILE_BASELINE_TASK]
        tasks.extend(self._COMPILE_LANGUAGE_TASKS[lang] for lang in languages)
        return " ".join(tasks)

    @staticmethod
    def _gradle_args(verb: str, args: Optional[str]) -> Optional[str]:
        """The caller's args plus the packaging test-skip contract."""
        if verb not in PACKAGING_VERBS or _GRADLE_CALLER_TEST_FLAGS.search(args or ""):
            return args
        return _appended(args, GRADLE_EXCLUDE_TEST_ARGS)

    @staticmethod
    def executed_action(verb: str, params: Dict[str, Any], args: Optional[str]) -> ExecutedAction:
        """The gradle tasks actually dispatched, and their semantic delta."""
        tasks = str(params.get("tasks") or verb)
        added = (
            GRADLE_EXCLUDE_TEST_ARGS
            if _backend_added(params.get("gradle_args"), args, GRADLE_EXCLUDE_TEST_ARGS)
            else ""
        )
        reasons: List[str] = []
        if added:
            reasons.append(_TEST_OWNERSHIP_REASON)
        if verb == "install" and tasks == "assemble":
            reasons.append(
                "no maven-publish plugin — assemble builds the jars but publishes "
                "nothing to the local maven repo"
            )
        return ExecutedAction(
            argv_fragment=" ".join(part for part in (tasks, added) if part),
            reasons=tuple(reasons),
        )

    def execute(
        self, verb: str, args: Optional[str], working_directory: str, timeout: Optional[int]
    ) -> ActualToolExecution:
        compile_languages: List[str] = []
        if verb == "install":
            task = self._install_task(working_directory)
        elif verb == "compile":
            compile_languages = self._probe_compile_languages(working_directory)
            task = self._compile_tasks(compile_languages)
        else:
            task = self.VERBS[verb]
        kwargs: Dict[str, Any] = {
            "tasks": task,
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
        gradle_args = self._gradle_args(verb, args)
        if gradle_args:
            kwargs["gradle_args"] = gradle_args
        if timeout:
            kwargs["timeout"] = timeout
        if compile_languages:
            # The languages this compile MUST cover, so the tool can refuse to
            # score an all-NO-SOURCE compile as a compile of these sources.
            kwargs["_compile_source_languages"] = compile_languages
        try:
            result = self.gradle_tool.execute(**kwargs)
        except OutputPersistenceError as exc:
            raise exc.attach_invocation("gradle", kwargs)
        return ActualToolExecution("gradle", kwargs, result)

    def run(
        self, verb: str, args: Optional[str], working_directory: str, timeout: Optional[int]
    ) -> ToolResult:
        return self.execute(verb, args, working_directory, timeout).result
