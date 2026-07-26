"""Typed build domains and their coordinate graph (P0-B, Stage C task C1).

LIVE EVIDENCE (bigtop): the island machinery groups directories by the nearest
build marker and then calls every root INDEPENDENT — it never reads a single
artifact coordinate. bigtop-data-generators publishes
org.apache.bigtop:bigpetstore-data-generator:3.7.0-SNAPSHOT while
bigpetstore-transaction-queue requires 3.5.0-SNAPSHOT and bigpetstore-spark
requires 3.6.0-SNAPSHOT. The agent obeyed the "independent" guidance and burned
13 attempts on a dependency that cannot resolve.

The fix types each island as a build DOMAIN carrying the coordinates it
produces and requires (schema v1: root, system, languages, produces, requires)
and derives ``domain_edges`` from those coordinates. Independence is a
conclusion of the graph, never a directory heuristic: the two version
mismatches are named BEFORE any attempt. Absent facts stay absent — an
unparseable/interpolated coordinate is omitted, never guessed, and a
single-domain project emits no domain keys at all (byte-compat: the
``build_islands`` list is untouched).
"""

import json
import re

from sag.agent.physical_survey import (
    derive_domain_edges,
    enumerate_build_domains,
    enumerate_build_islands,
    parse_gradle_group_version,
    parse_gradle_requires,
    parse_maven_coordinates,
)
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


# --------------------------------------------------------------------------- #
# Fake orchestrator: a filesystem of canned build-file CONTENTS. Existence,
# `cat`, the maven-publish grep, the packaging grep and both `find`s are all
# answered from the SAME map, so a fixture cannot claim a coordinate that no
# file on disk declares.
# --------------------------------------------------------------------------- #
class FakeOrchestrator:
    def __init__(self, files, source_dirs=(), test_dirs=()):
        self.contents = dict(files)
        self.existing = set(files)
        self.source_dirs = list(source_dirs)
        self.test_dirs = list(test_dirs)
        self.files = {}  # heredoc writes (the persisted manifest)

    @staticmethod
    def _matching(command, candidate_dirs):
        suffixes = re.findall(r"-path '\*(/src/(?:main|test)/[^']+)'", command)
        if not suffixes:
            return list(candidate_dirs)
        return [d for d in candidate_dirs if any(d.endswith(s) for s in suffixes)]

    def _find_by_name(self, command):
        root_match = re.match(r"find (\S+) ", command)
        root = (root_match.group(1) if root_match else "").strip("'\"").rstrip("/")
        names = re.findall(r"-name '([^']+)'", command)
        depth_match = re.search(r"-maxdepth (\d+)", command)
        maxdepth = int(depth_match.group(1)) if depth_match else 99
        hits = []
        for path in sorted(self.contents):
            if not path.startswith(f"{root}/"):
                continue
            rel = path[len(root) + 1 :]
            if rel.count("/") + 1 > maxdepth:
                continue
            if path.rsplit("/", 1)[-1] in names:
                hits.append(path)
        return {"success": True, "output": "\n".join(hits), "exit_code": 0}

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
        if command.startswith("find ") and "-name" in command:
            return self._find_by_name(command)
        probe = re.search(r"test -e (\S+)", command)
        if probe:
            return {
                "success": True,
                "output": "yes" if probe.group(1) in self.existing else "no",
                "exit_code": 0,
            }
        packaging = re.match(r"grep -m1 '<packaging>' (\S+)", command)
        if packaging:
            hit = re.search(
                r"<packaging>[^<]+</packaging>", self.contents.get(packaging.group(1), "")
            )
            return {
                "success": bool(hit),
                "output": hit.group(0) if hit else "",
                "exit_code": 0 if hit else 1,
            }
        if "maven-publish" in command:
            # `grep -lE 'maven-publish' <dir>/build.gradle <dir>/build.gradle.kts`
            hits = [
                path
                for path in re.findall(r"(/\S+/build\.gradle(?:\.kts)?)", command)
                if "maven-publish" in self.contents.get(path, "")
            ]
            return {
                "success": bool(hits),
                "output": "\n".join(hits),
                "exit_code": 0 if hits else 1,
            }
        read = re.match(r"cat (\S+)", command)
        if read:
            path = read.group(1).strip("'\"")
            if path in self.contents:
                return {"success": True, "output": self.contents[path], "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}
        return {"success": True, "output": "", "exit_code": 0}


# --------------------------------------------------------------------------- #
# The bigtop-shaped acceptance fixture: four domains over a packaging=pom
# aggregator with profile-gated modules.
#
#   bigtop-test-framework  maven, no cross-domain refs (junit only; a
#                          ${commons.version} dependency is NOT a coordinate)
#   bigtop-data-generators gradle multi-project PRODUCING
#                          org.apache.bigtop:bigpetstore-data-generator and
#                          :bigtop-samplers at 3.7.0-SNAPSHOT
#   bigpetstore-spark      gradle, REQUIRES bigpetstore-data-generator 3.6.0-SNAPSHOT
#   bigpetstore-transaction-queue gradle, REQUIRES it at 3.5.0-SNAPSHOT (plus an
#                          interpolated samplers GAV that must stay omitted)
# --------------------------------------------------------------------------- #
BIG = "/workspace/bigtop"
TF = f"{BIG}/bigtop-test-framework"
DG = f"{BIG}/bigtop-data-generators"
SPARK = f"{BIG}/bigtop-bigpetstore/bigpetstore-spark"
TQ = f"{BIG}/bigtop-bigpetstore/bigpetstore-transaction-queue"

BIGTOP_FILES = {
    f"{BIG}/pom.xml": (
        "<project>\n"
        "  <groupId>org.apache.bigtop</groupId>\n"
        "  <artifactId>bigtop</artifactId>\n"
        "  <version>3.7.0-SNAPSHOT</version>\n"
        "  <packaging>pom</packaging>\n"
        "</project>\n"
    ),
    f"{TF}/pom.xml": (
        "<project>\n"
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <parent>\n"
        "    <groupId>org.apache.bigtop</groupId>\n"
        "    <artifactId>bigtop</artifactId>\n"
        "    <version>3.7.0-SNAPSHOT</version>\n"
        "  </parent>\n"
        "  <artifactId>bigtop-test-framework</artifactId>\n"
        "  <packaging>jar</packaging>\n"
        "  <dependencies>\n"
        "    <dependency>\n"
        "      <groupId>junit</groupId>\n"
        "      <artifactId>junit</artifactId>\n"
        "      <version>4.13.2</version>\n"
        "    </dependency>\n"
        "    <dependency>\n"
        "      <groupId>org.apache.commons</groupId>\n"
        "      <artifactId>commons-lang3</artifactId>\n"
        "      <version>${commons.version}</version>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n"
    ),
    f"{DG}/settings.gradle": ("include 'bigpetstore-data-generator'\ninclude 'bigtop-samplers'\n"),
    f"{DG}/gradle.properties": "group=org.apache.bigtop\nversion=3.7.0-SNAPSHOT\n",
    f"{DG}/build.gradle": "subprojects {\n  apply plugin: 'java'\n}\n",
    f"{DG}/bigpetstore-data-generator/build.gradle": (
        "apply plugin: 'maven-publish'\n"
        "dependencies {\n  compile 'com.google.guava:guava:18.0'\n}\n"
    ),
    f"{DG}/bigtop-samplers/build.gradle": "apply plugin: 'maven-publish'\n",
    f"{SPARK}/build.gradle": (
        "group 'org.apache.bigtop'\n"
        "version '3.7.0-SNAPSHOT'\n"
        "dependencies {\n"
        "  compile 'org.apache.bigtop:bigpetstore-data-generator:3.6.0-SNAPSHOT'\n"
        "}\n"
    ),
    f"{TQ}/build.gradle": (
        "group 'org.apache.bigtop'\n"
        "version '3.7.0-SNAPSHOT'\n"
        "dependencies {\n"
        "  compile 'org.apache.bigtop:bigpetstore-data-generator:3.5.0-SNAPSHOT'\n"
        '  compile "org.apache.bigtop:bigtop-samplers:${samplersVersion}"\n'
        "}\n"
    ),
}

BIGTOP_SOURCE_DIRS = [
    f"{TF}/src/main/groovy",
    f"{DG}/bigpetstore-data-generator/src/main/java",
    f"{DG}/bigtop-samplers/src/main/groovy",
    f"{SPARK}/src/main/scala",
    f"{TQ}/src/main/java",
]

BIGTOP_TEST_DIRS = [
    f"{TF}/src/test/groovy",
    f"{DG}/bigpetstore-data-generator/src/test/java",
    f"{SPARK}/src/test/scala",
]

DG_ARTIFACT = "org.apache.bigtop:bigpetstore-data-generator"


def _analyze(root, files, source_dirs, test_dirs, maven_modules=()):
    orch = FakeOrchestrator(files, source_dirs=source_dirs, test_dirs=test_dirs)
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": list(maven_modules)}
    analysis["build_recommendation"] = analyzer._recommend_build_approach(root, analysis)
    analyzer._recommend_test_approach(root, analysis["build_recommendation"])
    return orch, analysis


def _analyze_bigtop():
    return _analyze(BIG, BIGTOP_FILES, BIGTOP_SOURCE_DIRS, BIGTOP_TEST_DIRS)


def _bigtop_domains():
    _orch, analysis = _analyze_bigtop()
    return {d["root"]: d for d in analysis["build_recommendation"]["build_domains"]}


# --------------------------------------------------------------------------- #
# 1) Domain schema v1: the four roots, typed with their coordinates.
# --------------------------------------------------------------------------- #
def test_bigtop_emits_four_build_domains_beside_the_islands():
    _orch, analysis = _analyze_bigtop()
    rec = analysis["build_recommendation"]
    assert [d["root"] for d in rec["build_domains"]] == [
        isl["root"] for isl in rec["build_islands"]
    ]
    assert len(rec["build_domains"]) == 4


def test_build_islands_stay_byte_identical_beside_the_domains():
    """The domain keys ride ALONGSIDE build_islands — the directory facts (and
    their prescriptions) are untouched, so every existing island consumer keeps
    reading exactly what it read before."""
    _orch, analysis = _analyze_bigtop()
    islands = {i["root"]: i for i in analysis["build_recommendation"]["build_islands"]}
    assert set(islands) == {TF, DG, SPARK, TQ}
    assert islands[TF] == {
        "root": TF,
        "system": "maven",
        "goal": "install",
        "rationale": (
            "Independent maven build island under the aggregator; "
            "build it on its own with 'install'."
        ),
    }
    assert islands[DG]["goal"] == "build"  # publish is applied per subproject
    assert set(islands[SPARK]) == {"root", "system", "goal", "rationale"}


def test_domain_carries_its_languages_sorted():
    domains = _bigtop_domains()
    assert domains[DG]["languages"] == ["groovy", "java"]
    assert domains[TF]["languages"] == ["groovy"]
    assert domains[SPARK]["languages"] == ["scala"]


def test_gradle_multiproject_produces_each_publishing_subproject():
    domains = _bigtop_domains()
    assert domains[DG]["produces"] == [
        {
            "group": "org.apache.bigtop",
            "name": "bigpetstore-data-generator",
            "version": "3.7.0-SNAPSHOT",
        },
        {
            "group": "org.apache.bigtop",
            "name": "bigtop-samplers",
            "version": "3.7.0-SNAPSHOT",
        },
    ]


def test_gradle_domains_that_publish_nothing_produce_nothing():
    """spark and transaction-queue apply no maven-publish plugin: no produced
    coordinate is invented for them (absent fact = absent key)."""
    domains = _bigtop_domains()
    assert "produces" not in domains[SPARK]
    assert "produces" not in domains[TQ]


def test_gradle_requires_are_the_literal_gavs_only():
    domains = _bigtop_domains()
    assert domains[TQ]["requires"] == [
        {
            "group": "org.apache.bigtop",
            "name": "bigpetstore-data-generator",
            "version": "3.5.0-SNAPSHOT",
        }
    ]
    # The interpolated "org.apache.bigtop:bigtop-samplers:${samplersVersion}"
    # is NOT a coordinate — omitted, never guessed (guessing it would mint a
    # third edge against a domain that really does produce bigtop-samplers).
    assert all(coord["name"] != "bigtop-samplers" for coord in domains[TQ]["requires"])


def test_gradle_multiproject_requires_collect_across_subproject_build_files():
    domains = _bigtop_domains()
    assert {"group": "com.google.guava", "name": "guava", "version": "18.0"} in domains[DG][
        "requires"
    ]


def test_maven_domain_produces_with_parent_group_and_version_fallback():
    domains = _bigtop_domains()
    assert domains[TF]["produces"] == [
        {
            "group": "org.apache.bigtop",
            "name": "bigtop-test-framework",
            "version": "3.7.0-SNAPSHOT",
        }
    ]


def test_maven_domain_skips_property_interpolated_dependency_versions():
    domains = _bigtop_domains()
    assert domains[TF]["requires"] == [{"group": "junit", "name": "junit", "version": "4.13.2"}]


# --------------------------------------------------------------------------- #
# 2) Acceptance: exactly two version-incompatible edges, with exact details.
# --------------------------------------------------------------------------- #
def test_bigtop_yields_exactly_two_version_incompatible_edges():
    _orch, analysis = _analyze_bigtop()
    edges = analysis["build_recommendation"]["domain_edges"]
    incompatible = [e for e in edges if e["status"] == "version_incompatible"]
    assert len(incompatible) == 2
    assert {
        "consumer": SPARK,
        "producer": DG,
        "status": "version_incompatible",
        "detail": (f"requires {DG_ARTIFACT} 3.6.0-SNAPSHOT; producer builds 3.7.0-SNAPSHOT"),
    } in incompatible
    assert {
        "consumer": TQ,
        "producer": DG,
        "status": "version_incompatible",
        "detail": (f"requires {DG_ARTIFACT} 3.5.0-SNAPSHOT; producer builds 3.7.0-SNAPSHOT"),
    } in incompatible


def test_bigtop_has_no_other_edges():
    """guava/junit requires match no domain's produces, and the domain with no
    cross-references gets no edge at all."""
    _orch, analysis = _analyze_bigtop()
    edges = analysis["build_recommendation"]["domain_edges"]
    assert len(edges) == 2
    assert all(edge["consumer"] != TF for edge in edges)


# --------------------------------------------------------------------------- #
# 3) Maven-to-maven graph: compatible vs incompatible vs no edge.
# --------------------------------------------------------------------------- #
ACME = "/workspace/acme"


def _acme_files(app_lib_version, extra_dependency=""):
    return {
        f"{ACME}/pom.xml": (
            "<project>\n  <artifactId>acme</artifactId>\n"
            "  <packaging>pom</packaging>\n</project>\n"
        ),
        f"{ACME}/lib/pom.xml": (
            "<project>\n"
            "  <parent>\n"
            "    <groupId>com.acme</groupId>\n"
            "    <artifactId>acme</artifactId>\n"
            "    <version>1.0.0</version>\n"
            "  </parent>\n"
            "  <artifactId>lib</artifactId>\n"
            "</project>\n"
        ),
        f"{ACME}/app/pom.xml": (
            "<project>\n"
            "  <groupId>com.acme</groupId>\n"
            "  <artifactId>app</artifactId>\n"
            "  <version>1.0.0</version>\n"
            "  <dependencies>\n"
            "    <dependency>\n"
            "      <groupId>com.acme</groupId>\n"
            "      <artifactId>lib</artifactId>\n"
            f"      <version>{app_lib_version}</version>\n"
            "    </dependency>\n"
            f"{extra_dependency}"
            "  </dependencies>\n"
            "</project>\n"
        ),
    }


def _analyze_acme(app_lib_version, extra_dependency=""):
    return _analyze(
        ACME,
        _acme_files(app_lib_version, extra_dependency),
        [f"{ACME}/lib/src/main/java", f"{ACME}/app/src/main/java"],
        [f"{ACME}/app/src/test/java"],
    )


def test_maven_consumer_with_matching_version_is_a_compatible_edge():
    _orch, analysis = _analyze_acme("1.0.0")
    assert analysis["build_recommendation"]["domain_edges"] == [
        {
            "consumer": f"{ACME}/app",
            "producer": f"{ACME}/lib",
            "status": "compatible",
            "detail": "requires com.acme:lib 1.0.0; producer builds 1.0.0",
        }
    ]


def test_maven_consumer_with_stale_version_is_incompatible():
    _orch, analysis = _analyze_acme("2.0.0")
    assert analysis["build_recommendation"]["domain_edges"] == [
        {
            "consumer": f"{ACME}/app",
            "producer": f"{ACME}/lib",
            "status": "version_incompatible",
            "detail": "requires com.acme:lib 2.0.0; producer builds 1.0.0",
        }
    ]


def test_unmatched_requires_create_no_edge_and_no_key():
    """A dependency on something no surveyed domain produces is not an edge —
    and with no edges at all the key stays absent, not empty."""
    orch = FakeOrchestrator(
        {
            f"{ACME}/pom.xml": (
                "<project>\n  <artifactId>acme</artifactId>\n"
                "  <packaging>pom</packaging>\n</project>\n"
            ),
            f"{ACME}/lib/pom.xml": (
                "<project>\n  <groupId>com.acme</groupId>\n"
                "  <artifactId>lib</artifactId>\n  <version>1.0.0</version>\n</project>\n"
            ),
            f"{ACME}/app/pom.xml": (
                "<project>\n  <groupId>com.acme</groupId>\n"
                "  <artifactId>app</artifactId>\n  <version>1.0.0</version>\n"
                "  <dependencies>\n    <dependency>\n"
                "      <groupId>junit</groupId>\n      <artifactId>junit</artifactId>\n"
                "      <version>4.13.2</version>\n    </dependency>\n"
                "  </dependencies>\n</project>\n"
            ),
        },
        source_dirs=[f"{ACME}/lib/src/main/java", f"{ACME}/app/src/main/java"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    rec = analyzer._recommend_build_approach(ACME, analysis)
    assert len(rec["build_domains"]) == 2
    assert "domain_edges" not in rec


# --------------------------------------------------------------------------- #
# 4) Single-domain projects: no domain keys at all (absent, not empty), so the
#    recorded replay fixtures and single-module snapshots stay untouched.
# --------------------------------------------------------------------------- #
PATHO = "/workspace/patho"


def test_single_domain_aggregator_emits_no_domain_keys():
    orch = FakeOrchestrator(
        {
            f"{PATHO}/pom.xml": (
                "<project>\n  <artifactId>patho</artifactId>\n"
                "  <packaging>pom</packaging>\n</project>\n"
            ),
            f"{PATHO}/framework/pom.xml": (
                "<project>\n  <groupId>com.acme</groupId>\n"
                "  <artifactId>framework</artifactId>\n  <version>1.0.0</version>\n</project>\n"
            ),
        },
        source_dirs=[f"{PATHO}/framework/src/main/java"],
        test_dirs=[f"{PATHO}/framework/src/test/java"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    rec = analyzer._recommend_build_approach(PATHO, analysis)
    assert len(rec["build_islands"]) == 1
    assert "build_domains" not in rec
    assert "domain_edges" not in rec


def test_single_module_maven_root_emits_no_domain_keys():
    orch = FakeOrchestrator(
        {
            "/workspace/solo/pom.xml": (
                "<project>\n  <groupId>com.acme</groupId>\n"
                "  <artifactId>solo</artifactId>\n  <version>1.0.0</version>\n</project>\n"
            ),
            "/workspace/solo/src/main/java": "",
        },
        test_dirs=["/workspace/solo/src/test/java"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    rec = analyzer._recommend_build_approach("/workspace/solo", analysis)
    assert "build_domains" not in rec
    assert "domain_edges" not in rec


# --------------------------------------------------------------------------- #
# 5) The manifest carries the graph (the gate reads it there), and single-domain
#    manifests gain no keys.
# --------------------------------------------------------------------------- #
def test_manifest_carries_domains_and_edges():
    orch, analysis = _analyze_bigtop()
    ProjectAnalyzerTool(docker_orchestrator=orch)._persist_build_requirements(BIG, analysis)
    manifest = json.loads(orch.files[REQUIREMENTS_PATH])
    assert len(manifest["build_domains"]) == 4
    assert [edge["status"] for edge in manifest["domain_edges"]] == [
        "version_incompatible",
        "version_incompatible",
    ]


def test_single_domain_manifest_has_no_domain_keys():
    orch = FakeOrchestrator(
        {
            f"{PATHO}/pom.xml": (
                "<project>\n  <artifactId>patho</artifactId>\n"
                "  <packaging>pom</packaging>\n</project>\n"
            ),
            f"{PATHO}/framework/pom.xml": (
                "<project>\n  <groupId>com.acme</groupId>\n"
                "  <artifactId>framework</artifactId>\n  <version>1.0.0</version>\n</project>\n"
            ),
        },
        source_dirs=[f"{PATHO}/framework/src/main/java"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    analysis["build_recommendation"] = analyzer._recommend_build_approach(PATHO, analysis)
    analyzer._persist_build_requirements(PATHO, analysis)
    manifest = json.loads(orch.files[REQUIREMENTS_PATH])
    assert "build_domains" not in manifest
    assert "domain_edges" not in manifest


# --------------------------------------------------------------------------- #
# 6) Model-visible guidance: the mismatch is named BEFORE any attempt, and the
#    independence claim stops when the graph has edges.
# --------------------------------------------------------------------------- #
def _render(analysis):
    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)
    return tool._render_recommended_build_output(analysis)


def test_guidance_names_every_incompatible_edge_before_the_coordinates():
    _orch, analysis = _analyze_bigtop()
    output = _render(analysis)
    assert f"{DG} builds 3.7.0-SNAPSHOT; {TQ} requires {DG_ARTIFACT} 3.5.0-SNAPSHOT" in output
    assert f"{DG} builds 3.7.0-SNAPSHOT; {SPARK} requires {DG_ARTIFACT} 3.6.0-SNAPSHOT" in output
    assert "record the mismatch, do not silently alias" in output
    # Named BEFORE the coordinates the agent would act on — i.e. before any
    # attempt, not after 13 failed ones.
    assert output.index("mismatch") < output.index("Build coordinates")


def test_guidance_stops_claiming_independence_when_edges_exist():
    _orch, analysis = _analyze_bigtop()
    output = _render(analysis)
    assert "independent islands" not in output
    assert "independent" not in output
    # The coordinates themselves are still all named.
    for root in (TF, DG, SPARK, TQ):
        assert root in output


def test_guidance_still_claims_independence_when_the_graph_has_no_edges():
    """No coordinate edge, no dependency: the pre-existing wording survives for
    genuinely independent domains."""
    orch = FakeOrchestrator(
        {
            f"{ACME}/pom.xml": (
                "<project>\n  <artifactId>acme</artifactId>\n"
                "  <packaging>pom</packaging>\n</project>\n"
            ),
            f"{ACME}/lib/pom.xml": (
                "<project>\n  <groupId>com.acme</groupId>\n"
                "  <artifactId>lib</artifactId>\n  <version>1.0.0</version>\n</project>\n"
            ),
            f"{ACME}/app/pom.xml": (
                "<project>\n  <groupId>com.acme</groupId>\n"
                "  <artifactId>app</artifactId>\n  <version>1.0.0</version>\n</project>\n"
            ),
        },
        source_dirs=[f"{ACME}/lib/src/main/java", f"{ACME}/app/src/main/java"],
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    analysis["build_recommendation"] = analyzer._recommend_build_approach(ACME, analysis)
    output = _render(analysis)
    assert "Build coordinates (independent islands):" in output
    assert "mismatch" not in output


def test_compatible_edges_are_not_named_as_mismatches():
    _orch, analysis = _analyze_acme("1.0.0")
    output = _render(analysis)
    assert "mismatch" not in output
    # ... but independence is no longer claimed: an edge exists.
    assert "independent" not in output


# --------------------------------------------------------------------------- #
# 7) The parsers themselves (pure text in, coordinates out).
# --------------------------------------------------------------------------- #
def test_parse_maven_coordinates_absent_facts_are_absent_keys():
    assert parse_maven_coordinates("") == {}
    assert parse_maven_coordinates("<project></project>") == {}


def test_parse_maven_coordinates_prefers_the_projects_own_group_and_version():
    coords = parse_maven_coordinates(
        "<project>"
        "<parent><groupId>p.g</groupId><artifactId>p</artifactId>"
        "<version>9.9</version></parent>"
        "<groupId>own.g</groupId><artifactId>own</artifactId><version>1.1</version>"
        "</project>"
    )
    assert coords["produces"] == [{"group": "own.g", "name": "own", "version": "1.1"}]


def test_parse_maven_coordinates_skips_interpolated_project_version():
    coords = parse_maven_coordinates(
        "<project>"
        "<parent><groupId>p.g</groupId><artifactId>p</artifactId>"
        "<version>9.9</version></parent>"
        "<artifactId>own</artifactId><version>${revision}</version>"
        "</project>"
    )
    # The interpolated own version is not a fact; the parent's literal one is.
    assert coords["produces"] == [{"group": "p.g", "name": "own", "version": "9.9"}]


def test_parse_maven_coordinates_ignores_dependency_ids_for_produces():
    coords = parse_maven_coordinates(
        "<project><artifactId>own</artifactId>"
        "<dependencies><dependency><groupId>d.g</groupId>"
        "<artifactId>dep</artifactId><version>2.0</version></dependency></dependencies>"
        "</project>"
    )
    assert coords["produces"] == [{"name": "own"}]
    assert coords["requires"] == [{"group": "d.g", "name": "dep", "version": "2.0"}]


def test_parse_gradle_group_version_reads_build_file_then_properties():
    assert parse_gradle_group_version("group 'g.one'\nversion '1.0'\n", "") == {
        "group": "g.one",
        "version": "1.0",
    }
    assert parse_gradle_group_version("", "group=g.two\nversion=2.0\n") == {
        "group": "g.two",
        "version": "2.0",
    }
    assert parse_gradle_group_version('group = "g.three"\n', "version=3.0\n") == {
        "group": "g.three",
        "version": "3.0",
    }


def test_parse_gradle_group_version_omits_interpolated_values():
    assert parse_gradle_group_version('version "$rootVersion"\n', "") == {}


def test_parse_gradle_requires_takes_literal_gavs_only():
    text = (
        "dependencies {\n"
        "  compile 'a.g:a:1.0'\n"
        '  testCompile "b.g:b:2.0"\n'
        '  compile "c.g:c:${cVersion}"\n'
        "  compile 'a.g:a:1.0'\n"
        "}\n"
    )
    assert parse_gradle_requires(text) == [
        {"group": "a.g", "name": "a", "version": "1.0"},
        {"group": "b.g", "name": "b", "version": "2.0"},
    ]


def test_derive_domain_edges_ignores_self_references():
    domains = [
        {
            "root": "/w/one",
            "system": "maven",
            "produces": [{"group": "g", "name": "one", "version": "1.0"}],
            "requires": [{"group": "g", "name": "one", "version": "1.0"}],
        }
    ]
    assert derive_domain_edges(domains) == []


def test_derive_domain_edges_without_a_producer_version_is_not_a_mismatch():
    domains = [
        {"root": "/w/p", "system": "gradle", "produces": [{"group": "g", "name": "lib"}]},
        {
            "root": "/w/c",
            "system": "gradle",
            "requires": [{"group": "g", "name": "lib", "version": "1.0"}],
        },
    ]
    assert derive_domain_edges(domains) == [
        {
            "consumer": "/w/c",
            "producer": "/w/p",
            "status": "compatible",
            "detail": "requires g:lib 1.0; producer version not declared",
        }
    ]


def test_enumerate_build_domains_matches_the_island_grouping():
    """A domain IS an island plus its coordinates: same roots, same systems,
    same dedupe (the gradle multi-project's subprojects fold into one root)."""
    orch = FakeOrchestrator(BIGTOP_FILES, source_dirs=BIGTOP_SOURCE_DIRS)
    modules = [
        {
            "module": d[len(BIG) + 1 :],
            "dir": d.rsplit("/src/main/", 1)[0],
            "lang": d.rsplit("/src/main/", 1)[1],
        }
        for d in BIGTOP_SOURCE_DIRS
    ]
    islands = enumerate_build_islands(orch, BIG, modules)
    domains = enumerate_build_domains(orch, BIG, modules)
    assert [d["root"] for d in domains] == [i["root"] for i in islands]
    assert [d["system"] for d in domains] == [i["system"] for i in islands]


def test_name_only_match_yields_unverified_edge_never_a_blocker():
    """Live p5v-bigtop-r1: data-generators derives group/version from the
    parent pom in Groovy — nothing literal. The subproject NAME still links
    the consumer to the producer, but only as an 'unverified' edge."""
    domains = [
        {
            "root": "/workspace/bigtop/bigtop-data-generators",
            "system": "gradle",
            "produces": [{"name": "bigpetstore-data-generator"}],
        },
        {
            "root": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark",
            "system": "gradle",
            "requires": [
                {
                    "group": "org.apache.bigtop",
                    "name": "bigpetstore-data-generator",
                    "version": "3.6.0-SNAPSHOT",
                }
            ],
        },
    ]

    edges = derive_domain_edges(domains)

    assert len(edges) == 1
    edge = edges[0]
    assert edge["status"] == "unverified"
    assert edge["consumer"].endswith("bigpetstore-spark")
    assert edge["producer"].endswith("bigtop-data-generators")
    assert "not literally declared" in edge["detail"]
    assert "org.apache.bigtop:bigpetstore-data-generator 3.6.0-SNAPSHOT" in edge["detail"]


def test_literal_group_match_still_wins_over_name_only():
    domains = [
        {
            "root": "/p/producer",
            "system": "gradle",
            "produces": [
                {"group": "org.example", "name": "lib", "version": "2.0"}
            ],
        },
        {
            "root": "/p/consumer",
            "system": "gradle",
            "requires": [{"group": "org.example", "name": "lib", "version": "1.0"}],
        },
    ]

    edges = derive_domain_edges(domains)

    assert len(edges) == 1
    assert edges[0]["status"] == "version_incompatible"
