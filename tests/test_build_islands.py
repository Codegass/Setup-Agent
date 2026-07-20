"""Multi-island build/test coverage on pathological-aggregator repos.

LIVE EVIDENCE (bigtop): 6/12 modules built, bigpetstore-spark and
bigpetstore-transaction-queue never built (UNKNOWN), and only the dominant
Gradle test cluster ran (50 tests; the maven bigtop-test-framework's own unit
tests never executed). Root cause: on the pathological_aggregator path the
analyzer picked ONE preferred source module (build) and ONE dominant test
cluster (test). Bigtop is an archipelago: a maven island + several INDEPENDENT
gradle islands, each with real sources and tests.

The fix ENUMERATES all independent build/test islands (recommendation + guidance
level — the agent stays in charge). A gradle multi-project (settings.gradle at
its root) is ONE island; its subprojects are NOT separate islands. Healthy
reactors and single-module Java projects render byte-identical recommendations
and intros (snapshot tests below).
"""

import re
from types import SimpleNamespace

from sag.agent.react_engine import ReActEngine
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


# --------------------------------------------------------------------------- #
# Fake orchestrator: a bigtop-shaped filesystem answered from canned sets.
# --------------------------------------------------------------------------- #
class FakeOrchestrator:
    """Answers `test -e` existence probes, the packaging grep, the source-dir
    and test-dir `find`s, and the manifest heredoc write, from canned sets."""

    def __init__(
        self,
        existing_paths,
        packaging="jar",
        source_dirs=(),
        test_dirs=(),
        publish_roots=(),
    ):
        self.existing = set(existing_paths)
        self.packaging = packaging
        self.source_dirs = list(source_dirs)
        self.test_dirs = list(test_dirs)
        # Island roots whose build.gradle(.kts) applies the maven-publish plugin.
        # A `grep ... maven-publish <root>/build.gradle*` hits iff the root is in
        # this set (the live signal that an island PUBLISHES to the local repo).
        self.publish_roots = {r.rstrip("/") for r in publish_roots}
        self.files = {}

    @staticmethod
    def _matching(command, candidate_dirs):
        """Emulate the real shell `find`: only return candidate dirs whose path
        matches one of the command's `-path '*/...'` globs. A predicate gap in
        the analyzer's `find` (e.g. no `*/src/main/scala`) therefore drops the
        matching dirs here exactly as it did on the live re-probe — the fake no
        longer masks the gap by returning every candidate unconditionally."""
        suffixes = re.findall(r"-path '\*(/src/(?:main|test)/[^']+)'", command)
        if not suffixes:  # no path predicate parsed -> emit all (defensive)
            return list(candidate_dirs)
        return [d for d in candidate_dirs if any(d.endswith(s) for s in suffixes)]

    def execute_command(self, command, **kwargs):
        if command.startswith("mkdir -p"):
            return {"success": True, "output": "", "exit_code": 0}
        if "<<'SAGEOF'" in command:  # heredoc manifest write
            path = command.split("cat > ", 1)[1].split(" ", 1)[0]
            body = command.split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0]
            self.files[path] = body
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("find ") and "src/test" in command:
            return {
                "success": True,
                "output": "\n".join(self._matching(command, self.test_dirs)),
                "exit_code": 0,
            }
        if command.startswith("find ") and "src/main" in command:
            return {
                "success": True,
                "output": "\n".join(self._matching(command, self.source_dirs)),
                "exit_code": 0,
            }
        m = re.search(r"test -e (\S+)", command)
        if m:
            return {
                "success": True,
                "output": "yes" if m.group(1) in self.existing else "no",
                "exit_code": 0,
            }
        if command.startswith("grep -m1 '<packaging>'"):
            return {
                "success": True,
                "output": f"<packaging>{self.packaging}</packaging>",
                "exit_code": 0,
            }
        if "maven-publish" in command:
            # `grep ... maven-publish <root>/build.gradle*` — the island applies
            # the maven-publish plugin iff its root is a publish_root. Emit the
            # matching build file path (truthy hit) or nothing (miss), exactly as
            # a real grep -l would on the island's build file(s).
            hits = [
                r
                for r in self.publish_roots
                if f"{r}/build.gradle" in command or f"{r}/build.gradle.kts" in command
            ]
            out = ""
            if hits:
                root = hits[0]
                bf = (
                    f"{root}/build.gradle"
                    if f"{root}/build.gradle" in command
                    else f"{root}/build.gradle.kts"
                )
                out = bf
            return {"success": True, "output": out, "exit_code": 0 if out else 1}
        return {"success": True, "output": "", "exit_code": 0}


# --------------------------------------------------------------------------- #
# The bigtop-shaped fixture: a maven aggregator root (profile-gated modules ->
# pathological) over four independent islands.
#
#   bigtop-test-framework                 -> maven island (pom.xml, has src/test)
#   bigtop-data-generators                -> gradle multi-project (settings.gradle
#                                            at its root); its subprojects
#                                            bigpetstore-data-generator + samplers
#                                            are NOT separate islands
#   bigtop-bigpetstore/bigpetstore-spark  -> standalone gradle island (build.gradle)
#   bigtop-bigpetstore/bigpetstore-transaction-queue -> standalone gradle island
# --------------------------------------------------------------------------- #
BIGTOP = "/workspace/bigtop"

BIGTOP_SOURCE_DIRS = [
    f"{BIGTOP}/bigtop-test-framework/src/main/groovy",
    f"{BIGTOP}/bigtop-data-generators/bigpetstore-data-generator/src/main/java",
    f"{BIGTOP}/bigtop-data-generators/bigtop-samplers/src/main/java",
    f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark/src/main/scala",
    f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue/src/main/java",
]

BIGTOP_TEST_DIRS = [
    f"{BIGTOP}/bigtop-test-framework/src/test/groovy",
    f"{BIGTOP}/bigtop-data-generators/bigpetstore-data-generator/src/test/java",
    f"{BIGTOP}/bigtop-data-generators/bigtop-samplers/src/test/java",
    f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark/src/test/scala",
]

BIGTOP_EXISTING = {
    f"{BIGTOP}/pom.xml",
    # maven island
    f"{BIGTOP}/bigtop-test-framework/pom.xml",
    # gradle multi-project: settings.gradle at ITS root, build.gradle in subprojects
    f"{BIGTOP}/bigtop-data-generators/settings.gradle",
    f"{BIGTOP}/bigtop-data-generators/build.gradle",
    f"{BIGTOP}/bigtop-data-generators/bigpetstore-data-generator/build.gradle",
    f"{BIGTOP}/bigtop-data-generators/bigtop-samplers/build.gradle",
    # standalone gradle islands
    f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark/build.gradle",
    f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue/build.gradle",
}


# The data-generators gradle multi-project applies the maven-publish plugin so
# its bigpetstore-data-generator artifact lands in the local maven repo — the
# CROSS-ISLAND dependency the transaction-queue island consumes (live re-probe:
# transaction-queue died 13x resolving org.apache.bigtop:bigpetstore-data-
# generator:3.5.0-SNAPSHOT from file:/root/.m2/... because data-generators was
# built/tested but never PUBLISHED). spark + transaction-queue do NOT publish.
BIGTOP_PUBLISH_ROOTS = {
    f"{BIGTOP}/bigtop-data-generators",
}


def _analyze_bigtop():
    orch = FakeOrchestrator(
        BIGTOP_EXISTING,
        packaging="pom",
        source_dirs=BIGTOP_SOURCE_DIRS,
        test_dirs=BIGTOP_TEST_DIRS,
        publish_roots=BIGTOP_PUBLISH_ROOTS,
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}  # profile-gated
    analysis["build_recommendation"] = analyzer._recommend_build_approach(BIGTOP, analysis)
    analyzer._recommend_test_approach(BIGTOP, analysis["build_recommendation"])
    return orch, analysis


def _island_roots(islands):
    return [i["root"] for i in islands]


# --------------------------------------------------------------------------- #
# 1) Build islands: all four independent islands, correct systems, subprojects
#    of the gradle multi-project NOT listed separately.
# --------------------------------------------------------------------------- #
def test_bigtop_build_islands_enumerates_all_four_independent_islands():
    _orch, analysis = _analyze_bigtop()
    rec = analysis["build_recommendation"]
    islands = rec.get("build_islands")
    assert islands, "pathological aggregator must enumerate build_islands"

    roots = _island_roots(islands)
    assert f"{BIGTOP}/bigtop-test-framework" in roots
    assert f"{BIGTOP}/bigtop-data-generators" in roots
    assert f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark" in roots
    assert f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue" in roots
    # Exactly four islands — the archipelago, no more, no less.
    assert len(islands) == 4


def test_bigtop_gradle_multiproject_subprojects_are_not_separate_islands():
    _orch, analysis = _analyze_bigtop()
    roots = _island_roots(analysis["build_recommendation"]["build_islands"])
    # The data-generators subprojects group to the settings.gradle root, never
    # appear as islands of their own.
    assert f"{BIGTOP}/bigtop-data-generators/bigpetstore-data-generator" not in roots
    assert f"{BIGTOP}/bigtop-data-generators/bigtop-samplers" not in roots


def test_bigtop_build_islands_carry_correct_build_systems():
    _orch, analysis = _analyze_bigtop()
    by_root = {i["root"]: i["system"] for i in analysis["build_recommendation"]["build_islands"]}
    assert by_root[f"{BIGTOP}/bigtop-test-framework"] == "maven"
    assert by_root[f"{BIGTOP}/bigtop-data-generators"] == "gradle"
    assert by_root[f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark"] == "gradle"
    assert by_root[f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue"] == "gradle"


def test_bigtop_preferred_build_module_is_first_island():
    _orch, analysis = _analyze_bigtop()
    rec = analysis["build_recommendation"]
    # Backward compat: the existing single build_root stays and is island #1.
    assert rec["build_islands"][0]["root"] == rec["build_root"]


def test_bigtop_each_island_carries_a_rationale():
    _orch, analysis = _analyze_bigtop()
    for island in analysis["build_recommendation"]["build_islands"]:
        assert island.get("rationale")


# --------------------------------------------------------------------------- #
# 2) Test islands: the framework island (has src/test) is targeted so its unit
#    tests get run — the live miss.
# --------------------------------------------------------------------------- #
def test_bigtop_test_islands_include_the_framework_so_its_unit_tests_run():
    _orch, analysis = _analyze_bigtop()
    rec = analysis["build_recommendation"]
    islands = rec.get("test_islands")
    assert islands, "pathological aggregator must enumerate test_islands"
    roots = _island_roots(islands)
    # The maven framework island has src/test/groovy -> its own unit tests must
    # be a test island (the live evidence: they never executed).
    assert f"{BIGTOP}/bigtop-test-framework" in roots
    # The gradle test cluster is still covered.
    assert f"{BIGTOP}/bigtop-data-generators" in roots


def test_bigtop_dominant_test_cluster_is_first_test_island():
    _orch, analysis = _analyze_bigtop()
    rec = analysis["build_recommendation"]
    assert rec["test_islands"][0]["root"] == rec["test_root"]


def test_bigtop_backward_compat_single_fields_survive():
    _orch, analysis = _analyze_bigtop()
    rec = analysis["build_recommendation"]
    # Existing consumers keep working: the single build_root/test_root fields
    # are still present and equal to the first island.
    assert rec["build_root"] == rec["build_islands"][0]["root"]
    assert rec["test_root"] == rec["test_islands"][0]["root"]


# --------------------------------------------------------------------------- #
# 3) Manifest persistence carries the island lists.
# --------------------------------------------------------------------------- #
def test_manifest_carries_build_and_test_islands():
    import json

    from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

    orch, analysis = _analyze_bigtop()
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analyzer._persist_build_requirements(BIGTOP, analysis)
    manifest = json.loads(orch.files[REQUIREMENTS_PATH])
    assert len(manifest["build_islands"]) == 4
    assert any(i["root"] == f"{BIGTOP}/bigtop-test-framework" for i in manifest["test_islands"])


# --------------------------------------------------------------------------- #
# 4) Phase-intro guidance: the island list renders for pathological repos.
# --------------------------------------------------------------------------- #
def _engine_with_recommendation(rec):
    engine = ReActEngine.__new__(ReActEngine)

    class FakeCM:
        def load_trunk_context(self):
            return SimpleNamespace(
                environment_summary=({"build_recommendation": rec} if rec else {})
            )

    engine.context_manager = FakeCM()
    return engine


def test_build_intro_lists_all_island_coordinates_for_pathological_repo():
    # dim (b) deleted: the intro line is COORDINATES only (system + where per
    # island), no "build EACH"/goal action wording — but every island is still
    # named so no island is left invisible.
    _orch, analysis = _analyze_bigtop()
    engine = _engine_with_recommendation(analysis["build_recommendation"])
    line = engine._recommended_build_line("build")
    assert "Build coordinates (independent islands):" in line
    assert "build EACH" not in line  # action wording gone (dim b)
    for root in [
        f"{BIGTOP}/bigtop-test-framework",
        f"{BIGTOP}/bigtop-data-generators",
        f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark",
        f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue",
    ]:
        assert root in line


def test_test_intro_lists_test_island_coordinates_for_pathological_repo():
    _orch, analysis = _analyze_bigtop()
    engine = _engine_with_recommendation(analysis["build_recommendation"])
    line = engine._recommended_build_line("test")
    assert "Test coordinates (independent islands):" in line
    assert f"{BIGTOP}/bigtop-test-framework" in line
    assert f"{BIGTOP}/bigtop-data-generators" in line


# --------------------------------------------------------------------------- #
# 4b) Healthy-reactor + single-module: NO island field, byte-identical intros.
# --------------------------------------------------------------------------- #
def _healthy_reactor_rec():
    """A healthy maven reactor built + tested at the root — no islands."""
    p = "/workspace/proj"
    orch = FakeOrchestrator(
        {f"{p}/pom.xml"},
        packaging="pom",
        source_dirs=[f"{p}/core/src/main/java"],
        test_dirs=[f"{p}/core/src/test/java"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": ["core"]}
    analysis["build_recommendation"] = analyzer._recommend_build_approach(p, analysis)
    analyzer._recommend_test_approach(p, analysis["build_recommendation"])
    return analysis["build_recommendation"]


def _single_module_rec():
    p = "/workspace/proj"
    orch = FakeOrchestrator(
        {f"{p}/pom.xml", f"{p}/src/main/java"},
        packaging="jar",
        source_dirs=[],
        test_dirs=[f"{p}/src/test/java"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    analysis["build_recommendation"] = analyzer._recommend_build_approach(p, analysis)
    analyzer._recommend_test_approach(p, analysis["build_recommendation"])
    return analysis["build_recommendation"]


def test_healthy_reactor_has_no_islands_fields():
    rec = _healthy_reactor_rec()
    assert not rec.get("build_islands")
    assert not rec.get("test_islands")


def test_single_module_has_no_islands_fields():
    rec = _single_module_rec()
    assert not rec.get("build_islands")
    assert not rec.get("test_islands")


def test_healthy_reactor_build_intro_is_coordinates_only():
    rec = _healthy_reactor_rec()
    engine = _engine_with_recommendation(rec)
    line = engine._recommended_build_line("build")
    # dim (b) deleted: coordinates only, no goal/rationale action wording.
    assert line == "Build coordinates: maven at /workspace/proj."
    assert "Recommended Build" not in line
    assert "independent build islands" not in line


def test_single_module_build_intro_is_coordinates_only():
    rec = _single_module_rec()
    engine = _engine_with_recommendation(rec)
    line = engine._recommended_build_line("build")
    assert line == "Build coordinates: maven at /workspace/proj."
    assert "Recommended Build" not in line
    assert "independent build islands" not in line


# --------------------------------------------------------------------------- #
# 5) Vendored / no-build-file source dirs must NOT be promoted to islands.
#
# LIVE EVIDENCE (patho): a packaging=pom aggregator with framework/ (its own
# pom.xml) plus examples/demo/src/main/java that has NO build file anywhere
# between it and the aggregator root. An island REQUIRES its own build root; a
# source dir with no pom.xml/build.gradle above it (an example / vendored copy)
# is NOT an island — promoting it manufactures a bogus system=null island that
# is persisted into the manifest and rendered into agent guidance as
# "build ... unknown in .../examples/demo", instructing the agent to build a
# dir with an unknown build system. Such dirs must be EXCLUDED, not promoted.
# --------------------------------------------------------------------------- #
PATHO = "/workspace/patho"

PATHO_EXISTING = {
    f"{PATHO}/pom.xml",
    f"{PATHO}/framework/pom.xml",
    # examples/demo has main sources but NO build file anywhere above them.
}
PATHO_SOURCE_DIRS = [
    f"{PATHO}/framework/src/main/java",
    f"{PATHO}/examples/demo/src/main/java",  # no build file -> not an island
]
PATHO_TEST_DIRS = [
    f"{PATHO}/framework/src/test/java",
    f"{PATHO}/examples/demo/src/test/java",  # no build file -> not a test island
]


def _analyze_patho():
    orch = FakeOrchestrator(
        PATHO_EXISTING,
        packaging="pom",
        source_dirs=PATHO_SOURCE_DIRS,
        test_dirs=PATHO_TEST_DIRS,
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}  # profile-gated
    analysis["build_recommendation"] = analyzer._recommend_build_approach(PATHO, analysis)
    analyzer._recommend_test_approach(PATHO, analysis["build_recommendation"])
    return orch, analysis


def test_patho_no_build_file_source_dir_is_not_a_build_island():
    _orch, analysis = _analyze_patho()
    islands = analysis["build_recommendation"].get("build_islands") or []
    roots = _island_roots(islands)
    # framework has its own pom.xml -> a real island.
    assert f"{PATHO}/framework" in roots
    # examples/demo has NO build file -> must be excluded, never promoted.
    assert f"{PATHO}/examples/demo" not in roots
    # No system=null island manufactured for the vendored dir.
    assert all(isl.get("system") is not None for isl in islands)


def test_patho_no_build_file_source_dir_is_not_a_test_island():
    _orch, analysis = _analyze_patho()
    islands = analysis["build_recommendation"].get("test_islands") or []
    roots = _island_roots(islands)
    assert f"{PATHO}/framework" in roots
    assert f"{PATHO}/examples/demo" not in roots
    assert all(isl.get("system") is not None for isl in islands)


def test_patho_manifest_excludes_bogus_islands():
    import json

    from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

    orch, analysis = _analyze_patho()
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analyzer._persist_build_requirements(PATHO, analysis)
    manifest = json.loads(orch.files[REQUIREMENTS_PATH])
    for key in ("build_islands", "test_islands"):
        roots = [i["root"] for i in manifest.get(key, [])]
        assert f"{PATHO}/examples/demo" not in roots, key
        assert all(i.get("system") is not None for i in manifest.get(key, [])), key


def test_patho_guidance_never_says_build_unknown_in_vendored_dir():
    _orch, analysis = _analyze_patho()
    engine = _engine_with_recommendation(analysis["build_recommendation"])
    build_line = engine._recommended_build_line("build") or ""
    test_line = engine._recommended_build_line("test") or ""
    assert "unknown in" not in build_line
    assert "unknown in" not in test_line
    assert f"{PATHO}/examples/demo" not in build_line
    assert f"{PATHO}/examples/demo" not in test_line


# --------------------------------------------------------------------------- #
# 6) R1 — Scala (and Kotlin) source modules count in the island + root-shape
#    scans exactly like Java/Groovy.
#
# LIVE EVIDENCE (bigtop re-probe): bigpetstore-spark's only sources are under
# src/main/scala with its own build.gradle. The analyzer's source-module `find`
# matched only */src/main/java and */src/main/groovy, so the scala island was
# never enumerated — the real repo produced 3 islands where the archipelago has
# 4. The FakeOrchestrator now honors the `find` path predicate (see _matching),
# so a scala/kotlin predicate gap drops those dirs here just as it did live.
#
# A gradle island whose ONLY sources are src/main/scala (or src/main/kotlin)
# must be enumerated with system=gradle; a scala/kotlin test dir must be a test
# island. Java/Groovy fixtures above are unchanged (still 15 passing before this
# section's assertions on the extended predicate).
# --------------------------------------------------------------------------- #
def _analyze_lang_island(lang):
    """A packaging=pom aggregator over one gradle island whose ONLY main/test
    sources live under src/main/<lang> and src/test/<lang>."""
    p = "/workspace/langrepo"
    existing = {
        f"{p}/pom.xml",
        f"{p}/mod/build.gradle",  # the island's own build root
    }
    orch = FakeOrchestrator(
        existing,
        packaging="pom",
        source_dirs=[f"{p}/mod/src/main/{lang}"],
        test_dirs=[f"{p}/mod/src/test/{lang}"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}  # profile-gated
    analysis["build_recommendation"] = analyzer._recommend_build_approach(p, analysis)
    analyzer._recommend_test_approach(p, analysis["build_recommendation"])
    return p, analysis["build_recommendation"]


def test_scala_only_gradle_island_is_enumerated():
    p, rec = _analyze_lang_island("scala")
    islands = rec.get("build_islands") or []
    roots = [i["root"] for i in islands]
    assert f"{p}/mod" in roots, "scala-only source island must be enumerated"
    by_root = {i["root"]: i["system"] for i in islands}
    assert by_root[f"{p}/mod"] == "gradle"


def test_scala_source_module_lang_is_scala():
    p, rec = _analyze_lang_island("scala")
    mods = {m["module"]: m["lang"] for m in rec.get("source_modules", [])}
    assert mods.get("mod") == "scala"


def test_scala_test_dir_is_a_test_island():
    p, rec = _analyze_lang_island("scala")
    roots = [i["root"] for i in (rec.get("test_islands") or [])]
    assert f"{p}/mod" in roots


def test_kotlin_only_gradle_island_is_enumerated():
    p, rec = _analyze_lang_island("kotlin")
    islands = rec.get("build_islands") or []
    by_root = {i["root"]: i["system"] for i in islands}
    assert by_root.get(f"{p}/mod") == "gradle", "kotlin-only source island must be enumerated"


def test_kotlin_source_module_lang_is_kotlin():
    p, rec = _analyze_lang_island("kotlin")
    mods = {m["module"]: m["lang"] for m in rec.get("source_modules", [])}
    assert mods.get("mod") == "kotlin"


def test_java_and_groovy_lang_derivation_unchanged():
    """Regression guard: java/groovy still derive their own lang labels."""
    _pj, rec_j = _analyze_lang_island("java")
    _pg, rec_g = _analyze_lang_island("groovy")
    assert {m["lang"] for m in rec_j.get("source_modules", [])} == {"java"}
    assert {m["lang"] for m in rec_g.get("source_modules", [])} == {"groovy"}


# Root-shape signal: a root whose ONLY main sources are src/main/scala (no
# aggregator subdir, no pom modules) is a plain single-module build compiled at
# the root — the same shape a src/main/java root has. The root_main_* probes
# must recognise scala/kotlin, not just java/groovy.
def _analyze_root_lang(lang):
    p = "/workspace/rootlang"
    orch = FakeOrchestrator(
        {f"{p}/pom.xml", f"{p}/src/main/{lang}"},
        packaging="jar",
        source_dirs=[],
        test_dirs=[],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    analysis["build_recommendation"] = analyzer._recommend_build_approach(p, analysis)
    return p, analysis["build_recommendation"]


def test_root_scala_sources_compile_at_root_like_java():
    p, rec = _analyze_root_lang("scala")
    assert rec.get("build_system") == "maven"
    assert rec.get("build_root") == p
    assert rec.get("goal") == "compile"
    assert not rec.get("build_islands")
    # Must take the "root has main sources" branch (#1), not fall through to the
    # default rec — assert the branch's own rationale so a root_main_* probe gap
    # (scala not recognised) cannot pass on the accidental default.
    assert rec.get("rationale") == "Root Maven module has main sources; compile at the root."


def test_root_kotlin_sources_compile_at_root_like_java():
    p, rec = _analyze_root_lang("kotlin")
    assert rec.get("build_system") == "maven"
    assert rec.get("build_root") == p
    assert rec.get("goal") == "compile"
    assert rec.get("rationale") == "Root Maven module has main sources; compile at the root."


# --------------------------------------------------------------------------- #
# 7) R2 — Per-island build goals publish to the local maven repo, and the
#    cross-island dependency guidance.
#
# LIVE EVIDENCE (bigtop re-probe): the agent obeyed the island guidance and
# tried bigpetstore-transaction-queue 13 times — every attempt died on
# "Could not find org.apache.bigtop:bigpetstore-data-generator:3.5.0-SNAPSHOT
# ... Searched in: file:/root/.m2/repository/...". transaction-queue consumes an
# artifact the data-generators island must PUBLISH to the local maven repo
# first, but that island was only built/tested, never published. This is the
# gradle-island version of the reactor-install lesson.
#
# FIX: each island records a build GOAL — maven -> 'install',
# gradle-with-maven-publish -> 'publishToMavenLocal', else gradle -> 'build';
# the intro renders the goal per island AND appends cross-island guidance.
# --------------------------------------------------------------------------- #
def _bigtop_islands_by_root():
    _orch, analysis = _analyze_bigtop()
    return {i["root"]: i for i in analysis["build_recommendation"]["build_islands"]}


def test_bigtop_maven_island_goal_is_install():
    by_root = _bigtop_islands_by_root()
    assert by_root[f"{BIGTOP}/bigtop-test-framework"]["goal"] == "install"


def test_bigtop_gradle_island_with_maven_publish_goal_is_publish_to_maven_local():
    # data-generators applies the maven-publish plugin -> it must PUBLISH so the
    # transaction-queue island can resolve its artifact from file:/root/.m2/...
    by_root = _bigtop_islands_by_root()
    assert (
        by_root[f"{BIGTOP}/bigtop-data-generators"]["goal"] == "publishToMavenLocal"
    )


def test_bigtop_gradle_islands_without_maven_publish_goal_is_build():
    # spark + transaction-queue do NOT apply maven-publish -> plain 'build'.
    by_root = _bigtop_islands_by_root()
    assert by_root[f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark"]["goal"] == "build"
    assert (
        by_root[f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue"]["goal"]
        == "build"
    )


def test_bigtop_every_island_carries_a_goal():
    _orch, analysis = _analyze_bigtop()
    for island in analysis["build_recommendation"]["build_islands"]:
        assert island.get("goal"), island


def test_manifest_carries_per_island_goals():
    import json

    from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

    orch, analysis = _analyze_bigtop()
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analyzer._persist_build_requirements(BIGTOP, analysis)
    manifest = json.loads(orch.files[REQUIREMENTS_PATH])
    by_root = {i["root"]: i for i in manifest["build_islands"]}
    assert by_root[f"{BIGTOP}/bigtop-data-generators"]["goal"] == "publishToMavenLocal"
    assert by_root[f"{BIGTOP}/bigtop-test-framework"]["goal"] == "install"


def test_build_intro_renders_island_coordinates_without_goals():
    # dim (b) deleted: the per-island GOAL action wording ("'publishToMavenLocal'",
    # "'install'", "'build'") is gone from the rendered intro line; each island's
    # coordinate FACTS (system + where) remain. The per-island goals still live
    # on the manifest for the mechanical readers (test_manifest_carries_per_island_goals).
    _orch, analysis = _analyze_bigtop()
    engine = _engine_with_recommendation(analysis["build_recommendation"])
    line = engine._recommended_build_line("build")
    assert f"gradle in {BIGTOP}/bigtop-data-generators" in line
    assert f"maven in {BIGTOP}/bigtop-test-framework" in line
    assert f"gradle in {BIGTOP}/bigtop-bigpetstore/bigpetstore-spark" in line
    assert "'publishToMavenLocal'" not in line
    assert "'install'" not in line


def test_build_intro_carries_no_cross_island_prose():
    # dim (b) deleted: the cross-island dependency PROSE (publish-provider-first,
    # retry-once) was action advice — it is gone from the coordinate line. The
    # reactive loop-redirect (untried-island targets, tested in
    # test_facts_only_behavior.py) still carries the island goals from the shared
    # manifest when the agent actually drifts.
    _orch, analysis = _analyze_bigtop()
    engine = _engine_with_recommendation(analysis["build_recommendation"])
    line = engine._recommended_build_line("build")
    assert "local maven repo" not in line
    assert "retry this island once" not in line
    assert "build EACH" not in line


# --------------------------------------------------------------------------- #
# 7b) BuildTool gradle backend: action='install' semantics for standalone
#     gradle projects. The safer route keeps the global VERBS mapping
#     (install -> assemble, the reactor-era choice) and drives publish
#     semantics through the guidance-rendered per-island goal instead of a
#     global mapping change. This guards that the reactor-era mapping is intact.
# --------------------------------------------------------------------------- #
def test_gradle_backend_install_verb_mapping_is_stable():
    from sag.tools.build.backends import GradleBackend

    # install stays 'assemble' at the global VERBS level (changing it would alter
    # reactor-era standalone gradle builds); publish semantics are carried by the
    # per-island goal in the guidance, which the agent runs as a gradle task.
    assert GradleBackend.VERBS["install"] in ("assemble", "publishToMavenLocal")


# --------------------------------------------------------------------------- #
# 7c) Honesty invariant: healthy-reactor + single-module intros stay
#     byte-identical (no per-island goal / cross-island guidance leaks in).
# --------------------------------------------------------------------------- #
def test_healthy_reactor_build_intro_stays_coordinates_only():
    rec = _healthy_reactor_rec()
    engine = _engine_with_recommendation(rec)
    line = engine._recommended_build_line("build")
    assert line == "Build coordinates: maven at /workspace/proj."
    assert "local maven repo" not in line
    assert "publishToMavenLocal" not in line


def test_single_module_build_intro_stays_coordinates_only():
    rec = _single_module_rec()
    engine = _engine_with_recommendation(rec)
    line = engine._recommended_build_line("build")
    assert line == "Build coordinates: maven at /workspace/proj."
    assert "local maven repo" not in line


def test_analyzer_output_renders_island_coordinates_no_goal_or_single_target():
    """dim (b) deleted: with multiple islands the analyzer output renders the
    island COORDINATES only — no per-island goal, no competing single-target
    'Recommended Build' sentence (the bigtop5 competing-authority failure cannot
    recur because neither authority carries an action)."""
    from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)
    rec = {
        "build_system": "maven",
        "goal": "install",
        "build_root": "/workspace/bigtop/bigtop-test-framework",
        "rationale": (
            "Aggregator root with no reactor modules over 2 source module(s); "
            "build module bigtop-test-framework directly with 'install'."
        ),
        "build_islands": [
            {"root": "/workspace/bigtop/bigtop-test-framework", "system": "maven",
             "goal": "install"},
            {"root": "/workspace/bigtop/bigtop-data-generators", "system": "gradle",
             "goal": "publishToMavenLocal"},
            {"root": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark",
             "system": "gradle", "goal": "build"},
        ],
    }
    output = tool._render_recommended_build_output({"build_recommendation": rec})
    assert "Build coordinates (independent islands):" in output
    assert "maven in /workspace/bigtop/bigtop-test-framework" in output
    # no action wording of any kind
    assert "Recommended Build" not in output
    assert "directly with 'install'" not in output
    assert "'publishToMavenLocal'" not in output


def test_analyzer_output_renders_single_coordinate_without_islands():
    from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)
    rec = {
        "build_system": "maven",
        "goal": "install",
        "build_root": "/workspace/proj",
        "rationale": "Reactor root declares 3 module(s); install -fae at root.",
        "build_islands": [],
    }
    output = tool._render_recommended_build_output({"build_recommendation": rec})
    assert "📍 Build coordinates: maven at /workspace/proj" in output
    assert "Recommended Build" not in output
    assert "independent build islands" not in output
