# tests/test_metamorphic_fixtures.py
"""Metamorphic proof: the derived graph is STRUCTURE, never project names.

Plan 6 Stage F2 item 1, spec §6 ("Metamorphic names": renamed project roots,
modules, GAVs and versions produce equivalent contracts/edge behavior).

The bigtop-shaped fixtures in ``tests/test_build_domain_graph.py`` and
``tests/test_domain_facts_projection.py`` are the anchor the whole domain graph
was built against, which is exactly what makes them dangerous as a proof: an
implementation that special-cased ``bigtop`` would pass every one of them. So
this suite re-runs the SAME fixtures through a consistent renaming —

    bigtop -> orchard, bigpetstore -> fruitstand, org.apache -> org.example,
    3.7.0/3.6.0/3.5.0 -> 9.1.0/9.0.0/8.9.0, and fresh claim ids

— and asserts the derived structures are ISOMORPHIC: same domains in the same
order, same edge statuses and counts, same domain-fact key sets, same conflict
kinds, same contract shape. Identifiers are content-derived (``dom-<sha12>``,
``edge-<sha12>``, ``ic-<sha12>``) so they MUST differ; the normalizer below
replaces them with placeholders derived from the record that defines them, so
a dangling or swapped reference still shows up as a difference.

Two negative controls keep the proof from being vacuous: a rename that also
moves a required version must NOT normalize equal, and the normalized bodies
must still carry the load-bearing words ("version_incompatible", "producer
builds") rather than having been scrubbed into agreement.

The renamed fixtures are DERIVED from the originals by substitution, so the two
sides cannot drift: editing the anchor fixture edits both.
"""

import json
import re

import pytest
from test_build_domain_graph import (
    BIG,
    BIGTOP_FILES,
    BIGTOP_SOURCE_DIRS,
    BIGTOP_TEST_DIRS,
    DG,
    SPARK,
    TF,
    TQ,
    _analyze,
)
from test_domain_facts_projection import (
    DG_CONSTRAINT_ID,
    PRESCRIPTION_MARKERS,
    SPARK_DEPENDENCY_ID,
    STAGE_A_FILES,
    TF_LIFECYCLE_ID,
    StageAOrch,
    _analyze_bigtop_stage_a,
)
from test_invocation_contracts import RecordingOrchestrator

from sag.agent.invocation_contracts import compliance_class, freeze_contract
from sag.agent.project_fact_projection import render_recommended_build_facts
from sag.project_fact_sheet import project_fact_sheet_metadata
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

# --------------------------------------------------------------------------- #
# The renaming, stated ONCE.
#
# Each row is (canonical placeholder, bigtop token, orchard token). The same
# table both GENERATES the renamed fixture (bigtop -> orchard) and NORMALIZES
# either side (token -> placeholder), so the fixture and the normalizer cannot
# disagree about what a "consistent rename" means.
# --------------------------------------------------------------------------- #
ORCHARD = "/workspace/orchard"

VOCABULARY = (
    ("<workspace-root>", BIG, ORCHARD),
    ("<group>", "org.apache.bigtop", "org.example.orchard"),
    ("<framework-module>", "bigtop-test-framework", "orchard-test-framework"),
    ("<generators-module>", "bigtop-data-generators", "orchard-data-generators"),
    ("<generator-artifact>", "bigpetstore-data-generator", "fruitstand-data-generator"),
    ("<samplers-artifact>", "bigtop-samplers", "orchard-samplers"),
    ("<consumers-dir>", "bigtop-bigpetstore", "orchard-fruitstand"),
    ("<spark-module>", "bigpetstore-spark", "fruitstand-spark"),
    ("<queue-module>", "bigpetstore-transaction-queue", "fruitstand-transaction-queue"),
    ("<producer-version>", "3.7.0-SNAPSHOT", "9.1.0-SNAPSHOT"),
    ("<spark-version>", "3.6.0-SNAPSHOT", "9.0.0-SNAPSHOT"),
    ("<queue-version>", "3.5.0-SNAPSHOT", "8.9.0-SNAPSHOT"),
    ("<lifecycle-claim>", TF_LIFECYCLE_ID, "lifecycle-777777777777"),
    ("<constraint-claim>", DG_CONSTRAINT_ID, "tool_constraint-888888888888"),
    ("<dependency-claim>", SPARK_DEPENDENCY_ID, "dependency-999999999999"),
)

RENAMES = {original: renamed for _placeholder, original, renamed in VOCABULARY}
BIGTOP_TOKENS = {original: placeholder for placeholder, original, _renamed in VOCABULARY}
ORCHARD_TOKENS = {renamed: placeholder for placeholder, _original, renamed in VOCABULARY}

# Content-derived identifiers. They are SUPPOSED to differ between the two
# runs; what must not differ is which record each one belongs to.
DERIVED_ID_RE = re.compile(r"\b(?:dom|edge|ic)-[0-9a-f]{12}\b")
DIGEST_RE = re.compile(r"\b[0-9a-f]{64}\b")


def _substitute(text, table):
    """Replace every token in `table`, longest first so no token clips another.

    Longest-first is what makes the substitution well-defined: ``bigtop`` is a
    prefix of ``bigtop-samplers``, and the shorter rule must never fire inside
    the longer one's match.
    """
    for source in sorted(table, key=lambda token: (-len(token), token)):
        text = text.replace(source, table[source])
    return text


def rename(value):
    """The bigtop fixture, spelled as the orchard project."""
    return _substitute(value, RENAMES)


# --------------------------------------------------------------------------- #
# The renamed fixture, derived from the anchor.
# --------------------------------------------------------------------------- #
ORCHARD_FILES = {rename(path): rename(body) for path, body in BIGTOP_FILES.items()}
ORCHARD_SOURCE_DIRS = [rename(path) for path in BIGTOP_SOURCE_DIRS]
ORCHARD_TEST_DIRS = [rename(path) for path in BIGTOP_TEST_DIRS]
ORCHARD_STAGE_A_FILES = {rename(path): rename(body) for path, body in STAGE_A_FILES.items()}

ORCHARD_TF = rename(TF)
ORCHARD_DG = rename(DG)
ORCHARD_SPARK = rename(SPARK)
ORCHARD_TQ = rename(TQ)


def _analyze_orchard():
    return _analyze(ORCHARD, ORCHARD_FILES, ORCHARD_SOURCE_DIRS, ORCHARD_TEST_DIRS)


def _analyze_orchard_stage_a(stage_a_files=ORCHARD_STAGE_A_FILES):
    """The orchard twin of `_analyze_bigtop_stage_a` — same calls, same order."""
    orch = StageAOrch(
        ORCHARD_FILES,
        source_dirs=ORCHARD_SOURCE_DIRS,
        test_dirs=ORCHARD_TEST_DIRS,
        stage_a_files=stage_a_files,
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    analysis = {"build_system": "maven", "maven_modules": []}
    analysis["build_recommendation"] = analyzer._recommend_build_approach(ORCHARD, analysis)
    analyzer._recommend_test_approach(ORCHARD, analysis["build_recommendation"])
    analyzer._persist_build_requirements(ORCHARD, analysis)
    return orch, analysis, json.loads(orch.files[REQUIREMENTS_PATH])


# --------------------------------------------------------------------------- #
# The normalizer: names by table, identifiers by the record that defines them.
# --------------------------------------------------------------------------- #
def _map_strings(structure, mapper):
    if isinstance(structure, dict):
        return {mapper(key): _map_strings(value, mapper) for key, value in structure.items()}
    if isinstance(structure, list):
        return [_map_strings(item, mapper) for item in structure]
    if isinstance(structure, str):
        return mapper(structure)
    return structure


def _id_aliases(structure):
    """derived id -> a placeholder built from the record that DEFINES it.

    An id is opaque, so normalizing it positionally would let two structures
    agree by accident. Instead each id is replaced by the content of its own
    record: a fact's `domain_id` becomes its root, an edge's `edge_id` becomes
    its endpoints and detail. Every OTHER mention of that id (a conflict's
    `edge_id`, a contract's `blocking_conflict_ids`) then resolves through the
    same alias, so a reference that points at the wrong record still differs.
    """
    aliases = {}

    def define(identifier, alias):
        identifier = str(identifier or "")
        if not DERIVED_ID_RE.fullmatch(identifier):
            return
        previous = aliases.setdefault(identifier, alias)
        assert previous == alias, f"{identifier} defined twice: {previous!r} vs {alias!r}"

    def walk(node):
        if isinstance(node, dict):
            if "domain_id" in node and "root" in node:
                define(node["domain_id"], f"<domain-of {node['root']}>")
            if "edge_id" in node and "consumer" in node and "producer" in node:
                define(
                    node["edge_id"],
                    f"<edge-of {node['consumer']} -> {node['producer']}: {node.get('detail')}>",
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(structure)
    assert len(set(aliases.values())) == len(aliases), f"ambiguous id aliases: {aliases}"
    return aliases


def normalized(structure, tokens):
    """`structure` with project names AND derived ids replaced by placeholders.

    `tokens` is the concrete->placeholder table for the side being normalized
    (`BIGTOP_TOKENS` or `ORCHARD_TOKENS`). Names go first so the aliases are
    themselves already name-free.
    """
    named = _map_strings(structure, lambda text: _substitute(text, tokens))
    aliases = _id_aliases(named)
    return _map_strings(named, lambda text: _substitute(text, aliases) if aliases else text)


def normalized_bigtop(structure):
    return normalized(structure, BIGTOP_TOKENS)


def normalized_orchard(structure):
    return normalized(structure, ORCHARD_TOKENS)


# --------------------------------------------------------------------------- #
# 1) The domain graph itself.
# --------------------------------------------------------------------------- #
def _analyze_bigtop_graph():
    return _analyze(BIG, BIGTOP_FILES, BIGTOP_SOURCE_DIRS, BIGTOP_TEST_DIRS)


def test_renamed_project_yields_an_isomorphic_build_recommendation():
    _orch_a, analysis_a = _analyze_bigtop_graph()
    _orch_b, analysis_b = _analyze_orchard()

    assert normalized_bigtop(analysis_a["build_recommendation"]) == normalized_orchard(
        analysis_b["build_recommendation"]
    )


def test_renamed_project_yields_the_same_domains_in_the_same_order():
    _orch_a, analysis_a = _analyze_bigtop_graph()
    _orch_b, analysis_b = _analyze_orchard()
    domains_a = analysis_a["build_recommendation"]["build_domains"]
    domains_b = analysis_b["build_recommendation"]["build_domains"]

    assert [domain["root"] for domain in domains_a] == [TF, DG, SPARK, TQ]
    assert [domain["root"] for domain in domains_b] == [
        ORCHARD_TF,
        ORCHARD_DG,
        ORCHARD_SPARK,
        ORCHARD_TQ,
    ]
    assert [sorted(domain) for domain in domains_a] == [sorted(domain) for domain in domains_b]


def test_renamed_project_yields_the_same_edge_statuses_and_counts():
    _orch_a, analysis_a = _analyze_bigtop_graph()
    _orch_b, analysis_b = _analyze_orchard()
    edges_a = analysis_a["build_recommendation"]["domain_edges"]
    edges_b = analysis_b["build_recommendation"]["domain_edges"]

    assert len(edges_a) == len(edges_b) == 2
    assert [edge["status"] for edge in edges_a] == [edge["status"] for edge in edges_b]
    assert [edge["status"] for edge in edges_b] == ["version_incompatible"] * 2
    # The ids are content-derived, so they MUST differ — and still normalize
    # onto the same endpoints.
    assert {edge["edge_id"] for edge in edges_a}.isdisjoint({e["edge_id"] for e in edges_b})
    assert normalized_bigtop(edges_a) == normalized_orchard(edges_b)


def test_renamed_project_never_leaks_a_bigtop_token_into_its_own_facts():
    """The orchard run is genuinely a different project, not a relabelled dump."""
    _orch_b, analysis_b = _analyze_orchard()
    body = json.dumps(analysis_b["build_recommendation"])

    for token in ("bigtop", "bigpetstore", "org.apache", "3.7.0", "3.6.0", "3.5.0"):
        assert token not in body, token


# --------------------------------------------------------------------------- #
# 2) The Stage A manifest: domain facts, conflicts, claim references.
# --------------------------------------------------------------------------- #
def test_renamed_project_yields_an_isomorphic_persisted_manifest():
    _orch_a, _analysis_a, manifest_a = _analyze_bigtop_stage_a()
    _orch_b, _analysis_b, manifest_b = _analyze_orchard_stage_a()

    assert normalized_bigtop(manifest_a) == normalized_orchard(manifest_b)


def test_renamed_project_yields_the_same_domain_fact_shapes():
    _orch_a, _analysis_a, manifest_a = _analyze_bigtop_stage_a()
    _orch_b, _analysis_b, manifest_b = _analyze_orchard_stage_a()
    facts_a = manifest_a["domain_facts"]
    facts_b = manifest_b["domain_facts"]

    assert len(facts_a) == len(facts_b) == 4
    assert [sorted(fact) for fact in facts_a] == [sorted(fact) for fact in facts_b]
    assert [fact["fact_epoch"] for fact in facts_a] == [fact["fact_epoch"] for fact in facts_b]
    assert {fact["domain_id"] for fact in facts_a}.isdisjoint(
        {fact["domain_id"] for fact in facts_b}
    )


def test_renamed_project_yields_the_same_conflict_kinds_per_domain():
    _orch_a, _analysis_a, manifest_a = _analyze_bigtop_stage_a()
    _orch_b, _analysis_b, manifest_b = _analyze_orchard_stage_a()

    def kinds(manifest):
        return [
            [conflict["kind"] for conflict in fact.get("open_conflicts", ())]
            for fact in manifest["domain_facts"]
        ]

    assert kinds(manifest_a) == kinds(manifest_b)
    # The framework domain has no cross-reference and no excluded file; the
    # producer carries both mismatches plus its own partial-map entry.
    assert kinds(manifest_b) == [
        [],
        ["version_incompatible", "version_incompatible", "partial_map"],
        ["version_incompatible"],
        ["version_incompatible"],
    ]


def test_renamed_project_resolves_its_own_claim_ids_the_same_way():
    """Documented actions follow the CLAIM, not the project's spelling."""
    _orch_b, _analysis_b, manifest_b = _analyze_orchard_stage_a()
    facts = {fact["root"]: fact for fact in manifest_b["domain_facts"]}

    assert facts[ORCHARD_TF]["documented_actions"] == [rename(TF_LIFECYCLE_ID)]
    assert facts[ORCHARD_DG]["documented_actions"] == [rename(DG_CONSTRAINT_ID)]
    assert facts[ORCHARD_SPARK]["documented_actions"] == [rename(SPARK_DEPENDENCY_ID)]
    assert "documented_actions" not in facts[ORCHARD_TQ]


def test_renamed_project_supports_the_same_edge_with_its_own_claim():
    _orch_b, _analysis_b, manifest_b = _analyze_orchard_stage_a()
    edges = {edge["consumer"]: edge for edge in manifest_b["domain_edges"]}

    assert edges[ORCHARD_SPARK]["support_claim_ids"] == [rename(SPARK_DEPENDENCY_ID)]
    assert "support_claim_ids" not in edges[ORCHARD_TQ]


# --------------------------------------------------------------------------- #
# 3) The model-visible projection and rendering (spec §7 stays binding).
# --------------------------------------------------------------------------- #
def test_renamed_project_projects_an_isomorphic_public_fact_sheet():
    _orch_a, analysis_a, _manifest_a = _analyze_bigtop_stage_a()
    _orch_b, analysis_b, _manifest_b = _analyze_orchard_stage_a()

    public_a = project_fact_sheet_metadata(analysis_a)["build_recommendation"]
    public_b = project_fact_sheet_metadata(analysis_b)["build_recommendation"]

    assert normalized_bigtop(public_a) == normalized_orchard(public_b)
    assert public_b["domain_edges_total"] == 2
    assert public_b["domain_mismatches_total"] == 2


def test_renamed_project_renders_the_same_guidance_structure():
    _orch_a, analysis_a, _manifest_a = _analyze_bigtop_stage_a()
    _orch_b, analysis_b, _manifest_b = _analyze_orchard_stage_a()

    rendered_a = render_recommended_build_facts(project_fact_sheet_metadata(analysis_a))
    rendered_b = render_recommended_build_facts(project_fact_sheet_metadata(analysis_b))

    assert _substitute(rendered_a, BIGTOP_TOKENS) == _substitute(rendered_b, ORCHARD_TOKENS)
    assert "independent" not in rendered_b
    assert "record the mismatch, do not silently alias" in rendered_b


def test_renamed_project_rendering_stays_prescription_free():
    """Spec §7: the Category-3 boundary is not name-conditioned either."""
    _orch_b, analysis_b, manifest_b = _analyze_orchard_stage_a()
    rendered = render_recommended_build_facts(project_fact_sheet_metadata(analysis_b))

    for marker in PRESCRIPTION_MARKERS:
        assert marker not in rendered, marker
    # The documented action reaches the manifest as an ID and reaches the
    # rendering not at all.
    assert rename(TF_LIFECYCLE_ID) in json.dumps(manifest_b)
    assert rename(TF_LIFECYCLE_ID) not in rendered
    assert "-DskipITs" not in rendered


# --------------------------------------------------------------------------- #
# 4) Contract shape: freezing a renamed dispatch yields the same contract.
# --------------------------------------------------------------------------- #
BIGTOP_ARGV = f"--fail-at-end -f {SPARK}/pom.xml verify"
ORCHARD_ARGV = rename(BIGTOP_ARGV)


def _freeze(root, argv, requirements):
    orchestrator = RecordingOrchestrator()
    contract = freeze_contract(
        orchestrator.execute_command,
        envelope_id="envelope-000012",
        tool="build",
        params={"action": "test", "working_directory": root},
        effective_action="verify",
        expected_cwd=root,
        expected_argv=argv,
        intent_source="model",
        requirements=requirements,
    )
    assert contract is not None
    return contract


def _frozen_pair():
    """((contract, manifest), (contract, manifest)) for the two spellings.

    The manifest travels with the contract because a contract REFERENCES ids
    (`domain_id`, `blocking_conflict_ids`) that only the manifest defines. The
    normalizer resolves an id through its defining record, so normalizing the
    pair together is what makes "the same conflict" mean the same edge rather
    than merely the same number of edges.
    """
    _orch_a, _analysis_a, manifest_a = _analyze_bigtop_stage_a()
    _orch_b, _analysis_b, manifest_b = _analyze_orchard_stage_a()
    return (
        (_freeze(SPARK, BIGTOP_ARGV, manifest_a), manifest_a),
        (_freeze(ORCHARD_SPARK, ORCHARD_ARGV, manifest_b), manifest_b),
    )


def test_freezing_a_renamed_dispatch_yields_the_same_contract_key_set():
    (contract_a, _manifest_a), (contract_b, _manifest_b) = _frozen_pair()

    assert set(contract_a) == set(contract_b)
    assert "blocking_conflict_ids" in contract_b
    assert len(contract_b["blocking_conflict_ids"]) == len(contract_a["blocking_conflict_ids"]) == 1


def test_freezing_a_renamed_dispatch_yields_an_isomorphic_contract_body():
    (contract_a, manifest_a), (contract_b, manifest_b) = _frozen_pair()

    def canonical(contract, manifest, tokens):
        # Normalized WITH the manifest so `domain_id`/`blocking_conflict_ids`
        # resolve to the records that define them.
        body = normalized({"contract": contract, "manifest": manifest}, tokens)["contract"]
        # `contract_id`/`contract_hash` digest the renamed material, so they are
        # REQUIRED to differ — asserted separately below.
        body["contract_id"] = "<contract-id>"
        body["contract_hash"] = "<contract-hash>"
        return body

    assert canonical(contract_a, manifest_a, BIGTOP_TOKENS) == canonical(
        contract_b, manifest_b, ORCHARD_TOKENS
    )
    assert contract_a["contract_id"] != contract_b["contract_id"]
    assert contract_a["contract_hash"] != contract_b["contract_hash"]
    # The reference really was resolved, not left opaque and equal by accident.
    assert contract_a["blocking_conflict_ids"] != contract_b["blocking_conflict_ids"]
    assert contract_a["domain_id"] != contract_b["domain_id"]


def test_the_renamed_contract_binds_the_renamed_blocking_conflict():
    """The frozen conflict id is the renamed project's OWN edge, not a copy."""
    _orch_a, _analysis_a, manifest_a = _analyze_bigtop_stage_a()
    _orch_b, _analysis_b, manifest_b = _analyze_orchard_stage_a()
    contract_a = _freeze(SPARK, BIGTOP_ARGV, manifest_a)
    contract_b = _freeze(ORCHARD_SPARK, ORCHARD_ARGV, manifest_b)

    edges_a = {edge["consumer"]: edge for edge in manifest_a["domain_edges"]}
    edges_b = {edge["consumer"]: edge for edge in manifest_b["domain_edges"]}
    assert contract_a["blocking_conflict_ids"] == [edges_a[SPARK]["edge_id"]]
    assert contract_b["blocking_conflict_ids"] == [edges_b[ORCHARD_SPARK]["edge_id"]]


@pytest.mark.parametrize(
    "suffix, expected",
    [
        ("", "exact"),
        (" -B --no-transfer-progress", "equivalent"),
    ],
)
def test_compliance_semantics_survive_the_rename(suffix, expected):
    assert compliance_class(BIGTOP_ARGV, f"mvn {BIGTOP_ARGV}{suffix}") == expected
    assert compliance_class(ORCHARD_ARGV, f"mvn {ORCHARD_ARGV}{suffix}") == expected


def test_a_deviated_dispatch_is_deviated_under_either_spelling():
    dropped_a = BIGTOP_ARGV.replace("--fail-at-end ", "")
    dropped_b = ORCHARD_ARGV.replace("--fail-at-end ", "")

    assert compliance_class(BIGTOP_ARGV, f"mvn {dropped_a}") == "deviated"
    assert compliance_class(ORCHARD_ARGV, f"mvn {dropped_b}") == "deviated"


def test_compliance_never_reads_the_project_name_out_of_the_argv():
    """Cross-spelled argvs deviate: the tokens are compared, not recognized."""
    assert compliance_class(BIGTOP_ARGV, f"mvn {ORCHARD_ARGV}") == "deviated"


# --------------------------------------------------------------------------- #
# 5) Negative controls — the isomorphism proof is not vacuous.
# --------------------------------------------------------------------------- #
def test_a_rename_that_also_moves_a_version_is_not_isomorphic():
    """If the normalizer could absorb a real structural change it would prove
    nothing. Aligning the spark consumer onto the producer's version turns a
    mismatch into a compatible edge, and that MUST survive normalization."""
    aligned_files = {
        path: (
            body.replace("9.0.0-SNAPSHOT", "9.1.0-SNAPSHOT")
            if path.endswith(f"{rename('bigpetstore-spark')}/build.gradle")
            else body
        )
        for path, body in ORCHARD_FILES.items()
    }
    _orch_a, analysis_a = _analyze_bigtop_graph()
    _orch_c, analysis_c = _analyze(ORCHARD, aligned_files, ORCHARD_SOURCE_DIRS, ORCHARD_TEST_DIRS)

    statuses = [edge["status"] for edge in analysis_c["build_recommendation"]["domain_edges"]]
    assert statuses == ["compatible", "version_incompatible"]
    assert normalized_bigtop(analysis_a["build_recommendation"]) != normalized_orchard(
        analysis_c["build_recommendation"]
    )


def test_a_missing_domain_is_not_isomorphic():
    """Dropping a domain changes the counts, which no placeholder can hide."""
    fewer_files = {path: body for path, body in ORCHARD_FILES.items() if rename(TF) not in path}
    fewer_sources = [path for path in ORCHARD_SOURCE_DIRS if rename(TF) not in path]
    _orch_a, analysis_a = _analyze_bigtop_graph()
    _orch_c, analysis_c = _analyze(ORCHARD, fewer_files, fewer_sources, ORCHARD_TEST_DIRS)

    assert len(analysis_c["build_recommendation"]["build_domains"]) == 3
    assert normalized_bigtop(analysis_a["build_recommendation"]) != normalized_orchard(
        analysis_c["build_recommendation"]
    )


def test_the_normalizer_keeps_the_load_bearing_facts():
    """Normalization replaces NAMES and IDS; it must not scrub the semantics."""
    _orch_a, _analysis_a, manifest_a = _analyze_bigtop_stage_a()
    body = json.dumps(normalized_bigtop(manifest_a))

    for kept in ("version_incompatible", "partial_map", "producer builds", "requires"):
        assert kept in body, kept
    # ... and it leaves no raw project name or raw derived id behind.
    for token in ("bigtop", "bigpetstore", "3.7.0", "3.6.0", "3.5.0"):
        assert token not in body, token
    assert DERIVED_ID_RE.search(body) is None


def test_the_normalizer_refuses_to_alias_one_id_to_two_records():
    """A collision would let two different graphs normalize onto one body."""
    with pytest.raises(AssertionError):
        _id_aliases(
            [
                {"domain_id": "dom-aaaaaaaaaaaa", "root": "/w/one"},
                {"domain_id": "dom-aaaaaaaaaaaa", "root": "/w/two"},
            ]
        )


def test_digests_are_not_silently_normalized():
    """`DIGEST_RE` documents the 64-hex shape; nothing in the compared bodies
    relies on one being rewritten, so a hash difference is a real difference."""
    _bigtop, (contract_b, _manifest_b) = _frozen_pair()

    assert DIGEST_RE.fullmatch(contract_b["contract_hash"])
