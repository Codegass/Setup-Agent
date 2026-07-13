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
    ):
        self.existing = set(existing_paths)
        self.packaging = packaging
        self.source_dirs = list(source_dirs)
        self.test_dirs = list(test_dirs)
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


def _analyze_bigtop():
    orch = FakeOrchestrator(
        BIGTOP_EXISTING,
        packaging="pom",
        source_dirs=BIGTOP_SOURCE_DIRS,
        test_dirs=BIGTOP_TEST_DIRS,
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


def test_build_intro_lists_all_islands_for_pathological_repo():
    _orch, analysis = _analyze_bigtop()
    engine = _engine_with_recommendation(analysis["build_recommendation"])
    line = engine._recommended_build_line("build")
    assert "independent build islands" in line
    assert "build EACH" in line
    for root in [
        f"{BIGTOP}/bigtop-test-framework",
        f"{BIGTOP}/bigtop-data-generators",
        f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark",
        f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue",
    ]:
        assert root in line


def test_test_intro_lists_test_islands_for_pathological_repo():
    _orch, analysis = _analyze_bigtop()
    engine = _engine_with_recommendation(analysis["build_recommendation"])
    line = engine._recommended_build_line("test")
    assert "test island" in line
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


def test_healthy_reactor_build_intro_byte_identical_to_pre_change_snapshot():
    rec = _healthy_reactor_rec()
    engine = _engine_with_recommendation(rec)
    line = engine._recommended_build_line("build")
    # Pre-change snapshot: the single-target recommendation, unchanged. The
    # expected string is a FULL literal (not prefix + rec["rationale"]) so a
    # rationale drift on the healthy path cannot slip past the byte-compare.
    assert line == (
        "Recommended Build: maven 'install' in /workspace/proj — "
        "Aggregator root over 1 source module(s) (0 Groovy); "
        "build the reactor at the root with 'install'."
    )
    assert "independent build islands" not in line


def test_single_module_build_intro_byte_identical_to_pre_change_snapshot():
    rec = _single_module_rec()
    engine = _engine_with_recommendation(rec)
    line = engine._recommended_build_line("build")
    # Full literal (see healthy-reactor snapshot rationale above).
    assert line == (
        "Recommended Build: maven 'compile' in /workspace/proj — "
        "Root Maven module has main sources; compile at the root."
    )
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
