# tests/test_claim_records.py
"""Plan 6 Stage A Task A2 — the typed claim union and its deterministic extractors.

Design §C1: the document map and the claims drawn from it are different
objects. A map entry says "these bytes exist at this path"; a claim says
"this is what those bytes STATE". Keeping them apart is what lets a claim
carry its own provenance class, so the loop can tell a README sentence from a
probe result without re-reading either.

Three properties are asserted here rather than assumed:

* the union is discriminated on `source_class` and each variant carries only
  its own typed `source_ref` — a receipt id in a document field is a
  validation error, never a coerced value (spec §6 "Claim union");
* documentation alone can never state a capability, so `kind="capability"` is
  refused on the two documentation source classes;
* an `UntrustedDocInterpretation` has no path into a claim at all — no
  constructor, no method, no annotation anywhere in the module accepts one.

The extractors are pure `(entry, text) -> claims` functions over hand-written
`DocumentMapEntry` fixtures (lane a1 owns the real map). Their fixtures are
shaped like the anchors they must survive: the Bigtop README command whose
cwd and three lifecycle flags have to come through verbatim, a TVM-shaped CI
job defining `USE_LLVM=ON`, and a `numpy==1.26.*` pin.

Scripted-orchestrator style (house pattern, shared with
tests/test_receipt_v2_and_assessments.py).
"""

import hashlib
import json

import pytest
from pydantic import TypeAdapter, ValidationError
from test_receipt_v2_and_assessments import ContainerFS

from sag.agent import claim_records
from sag.agent.claim_records import (
    CLAIM_DIR,
    CLAIM_HEREDOC,
    CLAIM_SCHEMA_VERSION,
    EVIDENCE_STATUSES,
    LIFECYCLE_RUNNERS,
    SOURCE_CLASSES,
    SOURCE_STATUSES,
    Applicability,
    CapabilityClaim,
    ClaimRecord,
    InferredClaim,
    InferredSourceRef,
    PhysicalClaim,
    PhysicalSourceRef,
    PolicyClaim,
    PolicySourceRef,
    ReceiptClaim,
    ReceiptSourceRef,
    UntrustedDocInterpretation,
    claim_id,
    extract_dependency_pins,
    extract_env_definitions,
    extract_lifecycle_commands,
    extract_policy_claims,
    extract_tool_constraints,
    find_claim_conflicts,
    parse_claim,
    write_claims,
)

TARGET_SHA = "9f1a2b3c4d5e6f708192a3b4c5d6e7f809111213"
CHECKOUT = "/workspace/bigtop"
TVM_CHECKOUT = "/workspace/tvm"


def entry(path, kind, *, body="", status="indexed", sections=None):
    """A hand-written `DocumentMapEntry` (plan Stage A shared contract)."""
    return {
        "entry_id": "doc-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:12],
        "target_sha": TARGET_SHA,
        "path": path,
        "realpath": path,
        "source_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "kind": kind,
        "section_index": list(sections or []),
        "parser_version": 1,
        "discovery_status": status,
    }


def claims_written(commands):
    """Every claim body persisted through the recorded commands."""
    payloads = []
    for command in commands:
        if CLAIM_DIR not in command or CLAIM_HEREDOC not in command:
            continue
        _, _, rest = command.partition("\n")
        body, _, _ = rest.partition(f"\n{CLAIM_HEREDOC}")
        payloads.append(json.loads(body))
    return payloads


def line_of(text, needle):
    """The 1-based line of `needle`, so fixtures can be edited freely."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"fixture does not contain {needle!r}")


def kinds(claims):
    return sorted({claim.kind for claim in claims})


def values(claims, kind):
    return [claim.typed_value for claim in claims if claim.kind == kind]


DOC_REF = {
    "entry_id": "doc-0123456789ab",
    "source_hash": "a" * 64,
    "source_range": "L12",
}
PHYSICAL_REF = {
    "probe_id": "probe-jdk-version",
    "content_hash": "b" * 64,
    "observed_scope": "/workspace/bigtop",
}
RECEIPT_REF = {
    "receipt_id": "inv-maven-1-0001",
    "assessment_id": "asm-inv_maven_1_0001-ok-1a2b3c4d",
    "predicate_id": "artifact_present",
}
INFERRED_REF = {
    "rule_id": "producer-version-matches-consumer",
    "support_claim_ids": ("dependency-aaaaaaaaaaaa", "dependency-bbbbbbbbbbbb"),
}


def policy_claim(**overrides):
    body = {
        "kind": "tool_constraint",
        "typed_value": {"tool": "maven", "constraint": "[3.9,)"},
        "source_class": "repository_doc",
        "source_ref": dict(DOC_REF),
        "extraction_method": "markdown_prose_version_literal",
    }
    body.update(overrides)
    return PolicyClaim(**body)


# ---------------------------------------------------------------------------
# the union: each source class validates only with its own typed source ref
# ---------------------------------------------------------------------------


def test_policy_claim_carries_the_document_source_ref_variant():
    claim = policy_claim()

    assert claim.source_class == "repository_doc"
    assert claim.source_ref == PolicySourceRef(**DOC_REF)
    assert claim.source_ref.entry_id == "doc-0123456789ab"
    assert claim.source_ref.source_range == "L12"


def test_config_is_a_document_source_class_too():
    claim = policy_claim(source_class="config", extraction_method="ci_run_step")

    assert claim.source_class == "config"


def test_physical_claim_carries_the_probe_source_ref_variant():
    claim = PhysicalClaim(
        kind="capability",
        typed_value={"capability": "llvm", "state": "present"},
        source_class="physical",
        source_ref=dict(PHYSICAL_REF),
    )

    assert claim.source_ref == PhysicalSourceRef(**PHYSICAL_REF)
    assert claim.source_ref.probe_id == "probe-jdk-version"
    assert claim.source_ref.observed_scope == "/workspace/bigtop"


def test_receipt_claim_names_receipt_assessment_and_predicate():
    claim = ReceiptClaim(
        kind="capability",
        typed_value={"capability": "native_smoke", "state": "present"},
        source_class="receipt",
        source_ref=dict(RECEIPT_REF),
    )

    assert claim.source_ref == ReceiptSourceRef(**RECEIPT_REF)
    assert claim.source_ref.receipt_id == "inv-maven-1-0001"
    assert claim.source_ref.assessment_id.startswith("asm-")
    assert claim.source_ref.predicate_id == "artifact_present"


def test_inferred_claim_carries_its_rule_and_complete_support_set():
    claim = InferredClaim(
        kind="dependency",
        typed_value={"ecosystem": "maven", "package": "bigtop", "version": "3.7"},
        source_class="inferred",
        source_ref=dict(INFERRED_REF),
    )

    assert claim.source_ref.rule_id == "producer-version-matches-consumer"
    # The support set lives in exactly ONE place — the typed ref that names it.
    assert claim.support_claim_ids == (
        "dependency-aaaaaaaaaaaa",
        "dependency-bbbbbbbbbbbb",
    )


def test_non_inferred_claims_have_no_support_set():
    assert policy_claim().support_claim_ids == ()


def test_an_inference_without_support_cannot_be_retracted_and_is_invalid():
    with pytest.raises(ValidationError):
        InferredSourceRef(rule_id="producer-version-matches-consumer", support_claim_ids=())


@pytest.mark.parametrize(
    "model,source_class,wrong_ref",
    [
        (PolicyClaim, "repository_doc", PHYSICAL_REF),
        (PolicyClaim, "config", RECEIPT_REF),
        (PhysicalClaim, "physical", DOC_REF),
        (ReceiptClaim, "receipt", DOC_REF),
        (InferredClaim, "inferred", PHYSICAL_REF),
    ],
)
def test_a_wrong_variant_source_ref_is_schema_invalid(model, source_class, wrong_ref):
    """Spec §C1: a receipt id in a document field is schema-invalid rather than
    silently coerced."""
    body = {
        "kind": "dependency",
        "typed_value": {"package": "numpy"},
        "source_class": source_class,
        "source_ref": dict(wrong_ref),
    }
    if model is PolicyClaim:
        body["extraction_method"] = "requirements_line"

    with pytest.raises(ValidationError):
        model(**body)


def test_a_source_ref_rejects_keys_it_does_not_declare():
    with pytest.raises(ValidationError):
        PolicySourceRef(**DOC_REF, receipt_id="inv-maven-1-0001")


def test_a_source_ref_never_coerces_a_non_string_range():
    with pytest.raises(ValidationError):
        PolicySourceRef(entry_id="doc-0123456789ab", source_hash="a" * 64, source_range=12)


def test_every_persisted_claim_round_trips_through_its_own_variant():
    """Lane a3 reads these files by documented schema, so the persisted body
    has to validate back into the variant that wrote it."""
    for claim in (
        policy_claim(),
        PhysicalClaim(
            kind="capability",
            typed_value={"capability": "llvm"},
            source_class="physical",
            source_ref=dict(PHYSICAL_REF),
        ),
        ReceiptClaim(
            kind="capability",
            typed_value={"capability": "llvm"},
            source_class="receipt",
            source_ref=dict(RECEIPT_REF),
        ),
        InferredClaim(
            kind="dependency",
            typed_value={"package": "numpy"},
            source_class="inferred",
            source_ref=dict(INFERRED_REF),
        ),
    ):
        parsed = parse_claim(claim.payload())

        assert type(parsed) is type(claim)
        assert parsed == claim
        assert parsed.claim_id == claim.claim_id


def test_an_unknown_source_class_has_no_variant():
    with pytest.raises(ValidationError):
        TypeAdapter(ClaimRecord).validate_python(
            {
                "kind": "dependency",
                "typed_value": {},
                "source_class": "human_message",
                "source_ref": dict(DOC_REF),
            }
        )


# ---------------------------------------------------------------------------
# capability: documentation alone can never state one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_class", ["repository_doc", "config"])
def test_documentation_can_never_state_a_capability(source_class):
    with pytest.raises(ValidationError):
        policy_claim(kind="capability", source_class=source_class)


def test_a_capability_claim_accepts_physical_receipt_and_inferred_evidence():
    adapter = TypeAdapter(CapabilityClaim)

    for reference, source_class in (
        (PHYSICAL_REF, "physical"),
        (RECEIPT_REF, "receipt"),
        (INFERRED_REF, "inferred"),
    ):
        claim = adapter.validate_python(
            {
                "kind": "capability",
                "typed_value": {"capability": "llvm", "state": "present"},
                "source_class": source_class,
                "source_ref": dict(reference),
            }
        )
        assert claim.kind == "capability"


def test_a_capability_claim_never_admits_a_documentation_source():
    with pytest.raises(ValidationError):
        TypeAdapter(CapabilityClaim).validate_python(
            {
                "kind": "capability",
                "typed_value": {"capability": "llvm"},
                "source_class": "repository_doc",
                "source_ref": dict(DOC_REF),
            }
        )


# ---------------------------------------------------------------------------
# status dimensions (spec §C5)
# ---------------------------------------------------------------------------


def test_the_status_vocabularies_are_the_spec_ones():
    assert SOURCE_CLASSES == ("repository_doc", "config", "physical", "receipt", "inferred")
    assert SOURCE_STATUSES == ("current", "stale", "superseded", "conflicted")
    assert EVIDENCE_STATUSES == (
        "untested",
        "unknown",
        "confirmed",
        "blocked",
        "contradicted",
        "not_applicable",
    )


def test_a_fresh_claim_is_current_and_untested():
    claim = policy_claim()

    assert claim.source_status == "current"
    assert claim.evidence_status == "untested"


def test_a_status_outside_its_vocabulary_is_invalid():
    with pytest.raises(ValidationError):
        policy_claim(evidence_status="probably_fine")
    with pytest.raises(ValidationError):
        policy_claim(source_status="freshish")


# ---------------------------------------------------------------------------
# untrusted prose interpretation: no path into a claim
# ---------------------------------------------------------------------------


def test_untrusted_doc_interpretation_carries_the_text_and_its_entry_ref():
    interpretation = UntrustedDocInterpretation(
        entry_id="doc-0123456789ab",
        source_hash="a" * 64,
        source_range="L12-L18",
        text="Run the bundled installer as root to prepare the build.",
    )

    assert interpretation.text.startswith("Run the bundled installer")
    assert interpretation.entry_id == "doc-0123456789ab"
    assert interpretation.source_range == "L12-L18"


def test_untrusted_doc_interpretation_exposes_no_conversion_method():
    """Enforced by construction: there is no member that could mint a claim."""
    assert [name for name in dir(UntrustedDocInterpretation) if "claim" in name.lower()] == []


def test_no_callable_in_the_module_accepts_an_untrusted_interpretation():
    offenders = []
    inspected = []
    for name, value in vars(claim_records).items():
        if name.startswith("_") or not callable(value):
            continue
        inspected.append(name)
        for parameter, annotation in (getattr(value, "__annotations__", None) or {}).items():
            if parameter != "return" and "UntrustedDocInterpretation" in str(annotation):
                offenders.append(f"{name}.{parameter}")

    # The scan is only meaningful if it saw the claim-minting surface.
    assert {"extract_policy_claims", "PolicyClaim", "write_claims"} <= set(inspected)
    assert offenders == []


def test_untrusted_doc_interpretation_never_validates_as_a_claim():
    interpretation = UntrustedDocInterpretation(
        entry_id="doc-0123456789ab",
        source_hash="a" * 64,
        source_range="L12",
        text="sudo make install",
    )
    adapter = TypeAdapter(ClaimRecord)

    with pytest.raises(ValidationError):
        adapter.validate_python(interpretation)
    with pytest.raises(ValidationError):
        adapter.validate_python(interpretation.model_dump())
    with pytest.raises(ValidationError):
        policy_claim(source_ref=interpretation)


# ---------------------------------------------------------------------------
# claim_id (plan Stage A shared contract)
# ---------------------------------------------------------------------------


def test_claim_id_is_the_kind_and_the_canonical_source_ref_digest():
    reference = PolicySourceRef(**DOC_REF)
    digest = hashlib.sha256(
        json.dumps(reference.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:12]

    assert claim_id("tool_constraint", reference) == f"tool_constraint-{digest}"
    assert policy_claim().claim_id == f"tool_constraint-{digest}"


def test_claim_id_separates_two_kinds_drawn_from_one_source_ref():
    first = policy_claim(kind="tool_constraint")
    second = policy_claim(kind="dependency", typed_value={"package": "numpy"})

    assert first.claim_id != second.claim_id


# ---------------------------------------------------------------------------
# extractor: tool version constraints
# ---------------------------------------------------------------------------


ENFORCER_POM = """<project>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-enforcer-plugin</artifactId>
        <configuration>
          <rules>
            <requireMavenVersion>
              <version>[3.9,)</version>
            </requireMavenVersion>
            <requireJavaVersion>
              <version>[11,)</version>
            </requireJavaVersion>
          </rules>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""


def test_maven_enforcer_require_version_literals_become_tool_constraints():
    pom = entry(f"{CHECKOUT}/pom.xml", "xml", body=ENFORCER_POM)

    claims = extract_tool_constraints(pom, ENFORCER_POM)

    assert [claim.typed_value for claim in claims] == [
        {"tool": "maven", "constraint": "[3.9,)"},
        {"tool": "java", "constraint": "[11,)"},
    ]
    assert {claim.source_class for claim in claims} == {"config"}
    assert {claim.extraction_method for claim in claims} == {"maven_enforcer_require_version"}
    assert claims[0].source_ref.entry_id == pom["entry_id"]
    assert claims[0].source_ref.source_hash == pom["source_hash"]


README_VERSIONS = """# Building

You need Maven 3.9 or newer and JDK 11 to build the project.
The Python bindings require Python >= 3.9.
"""


def test_readme_version_literals_become_tool_constraints():
    readme = entry(f"{CHECKOUT}/README.md", "markdown", body=README_VERSIONS)

    claims = extract_tool_constraints(readme, README_VERSIONS)

    assert [claim.typed_value for claim in claims] == [
        {"tool": "maven", "constraint": "3.9"},
        {"tool": "java", "constraint": "11"},
        {"tool": "python", "constraint": ">=3.9"},
    ]
    assert {claim.source_class for claim in claims} == {"repository_doc"}
    assert {claim.extraction_method for claim in claims} == {"markdown_prose_version_literal"}


README_NO_LITERALS = """# Requirements

You will need a working Maven installation and a Java Development Kit.
Install CMake before configuring the native extension, then run the build.
"""


def test_prose_without_a_version_literal_extracts_nothing():
    readme = entry(f"{CHECKOUT}/INSTALL.md", "markdown", body=README_NO_LITERALS)

    assert extract_tool_constraints(readme, README_NO_LITERALS) == []
    assert extract_policy_claims(readme, README_NO_LITERALS, checkout_root=CHECKOUT) == []


HEADINGS_ONLY = """# Apache Bigtop

## Building

### Prerequisites

## Testing

## License
"""


def test_headings_alone_extract_nothing():
    """Spec §C1: headings are an index, not an extractor."""
    readme = entry(f"{CHECKOUT}/README.md", "markdown", body=HEADINGS_ONLY)

    assert extract_policy_claims(readme, HEADINGS_ONLY, checkout_root=CHECKOUT) == []


# ---------------------------------------------------------------------------
# extractor: lifecycle commands
# ---------------------------------------------------------------------------


BIGTOP_README = """# Apache Bigtop

Bigtop packages and tests the Big Data ecosystem.

## Building the test framework

Install the test framework artifacts into the local repository:

```bash
mvn clean install -DskipTests -DskipITs -DperformRelease -f ./bigtop-test-framework/pom.xml
```
"""


def test_bigtop_readme_command_keeps_its_argv_and_repository_root_cwd():
    """Anchor control: the documented cwd and all three lifecycle flags survive.

    The `-f ./bigtop-test-framework/pom.xml` argument must NOT be rewritten
    into a cwd — the documented command runs from the repository root.
    """
    readme = entry(f"{CHECKOUT}/README.md", "markdown", body=BIGTOP_README)

    claims = extract_lifecycle_commands(readme, BIGTOP_README, checkout_root=CHECKOUT)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.kind == "lifecycle"
    assert claim.typed_value == {
        "tool": "maven",
        "argv": [
            "mvn",
            "clean",
            "install",
            "-DskipTests",
            "-DskipITs",
            "-DperformRelease",
            "-f",
            "./bigtop-test-framework/pom.xml",
        ],
        "cwd": CHECKOUT,
    }
    assert claim.extraction_method == "markdown_fenced_command"
    assert claim.source_ref.source_range == f"L{line_of(BIGTOP_README, 'mvn clean install')}"


MARKDOWN_CD = """# Building

```bash
cd bigtop-test-framework
mvn -B verify
```
"""


def test_a_documented_cd_is_the_only_thing_that_moves_the_cwd():
    readme = entry(f"{CHECKOUT}/BUILDING.md", "markdown", body=MARKDOWN_CD)

    claims = extract_lifecycle_commands(readme, MARKDOWN_CD, checkout_root=CHECKOUT)

    assert [claim.typed_value["cwd"] for claim in claims] == [f"{CHECKOUT}/bigtop-test-framework"]
    assert [claim.typed_value["argv"] for claim in claims] == [["mvn", "-B", "verify"]]


NON_RUNNER_MARKDOWN = """# Building

```bash
git clone https://example.invalid/project.git
make -j8 all
echo "done"
```
"""


def test_commands_whose_first_token_is_not_a_lifecycle_runner_are_ignored():
    readme = entry(f"{CHECKOUT}/README.md", "markdown", body=NON_RUNNER_MARKDOWN)

    assert extract_lifecycle_commands(readme, NON_RUNNER_MARKDOWN, checkout_root=CHECKOUT) == []


def test_the_lifecycle_runner_set_is_the_documented_one():
    assert sorted(LIFECYCLE_RUNNERS) == [
        "cmake",
        "gradle",
        "gradlew",
        "mvn",
        "mvnw",
        "pip",
        "pip3",
        "pytest",
        "python",
        "python3",
    ]


TVM_CI = """name: CI
on: [push]

jobs:
  build-llvm:
    runs-on: ubuntu-22.04
    env:
      CMAKE_ARGS: "-DUSE_LLVM=ON -DUSE_CUDA=OFF"
    steps:
      - uses: actions/checkout@v4
      - name: Configure and build
        working-directory: ./build
        run: |
          cmake .. -GNinja
          ninja
      - name: Install python dependencies
        run: pip install numpy==1.26.* scipy>=1.11
"""


def test_ci_run_steps_keep_their_working_directory_and_job_applicability():
    workflow = entry(f"{TVM_CHECKOUT}/.github/workflows/ci.yml", "yaml", body=TVM_CI)

    claims = extract_lifecycle_commands(workflow, TVM_CI, checkout_root=TVM_CHECKOUT)

    assert [claim.typed_value for claim in claims] == [
        {"tool": "cmake", "argv": ["cmake", "..", "-GNinja"], "cwd": f"{TVM_CHECKOUT}/build"},
        {
            "tool": "pip",
            "argv": ["pip", "install", "numpy==1.26.*", "scipy>=1.11"],
            "cwd": TVM_CHECKOUT,
        },
    ]
    assert {claim.applicability.workflow_job for claim in claims} == {"build-llvm"}
    assert {claim.extraction_method for claim in claims} == {"ci_run_step"}
    assert {claim.source_class for claim in claims} == {"config"}


SHELL_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

BUILD_TYPE=Release
export CMAKE_ARGS="-DUSE_LLVM=ON"
CACHE_DIR="$HOME/.cache"

./gradlew --no-daemon build
"""


def test_a_shell_script_runner_resolves_through_its_relative_path():
    script = entry(f"{CHECKOUT}/build.sh", "shell", body=SHELL_SCRIPT)

    claims = extract_lifecycle_commands(script, SHELL_SCRIPT, checkout_root=CHECKOUT)

    assert [claim.typed_value for claim in claims] == [
        {
            "tool": "gradle",
            "argv": ["./gradlew", "--no-daemon", "build"],
            "cwd": CHECKOUT,
        }
    ]


# ---------------------------------------------------------------------------
# extractor: dependency pins
# ---------------------------------------------------------------------------


def test_pip_install_arguments_with_version_literals_become_dependency_pins():
    workflow = entry(f"{TVM_CHECKOUT}/.github/workflows/ci.yml", "yaml", body=TVM_CI)

    claims = extract_dependency_pins(workflow, TVM_CI)

    assert [claim.typed_value for claim in claims] == [
        {"ecosystem": "pip", "package": "numpy", "specifier": "==", "version": "1.26.*"},
        {"ecosystem": "pip", "package": "scipy", "specifier": ">=", "version": "1.11"},
    ]
    assert {claim.applicability.workflow_job for claim in claims} == {"build-llvm"}


REQUIREMENTS = """# runtime pins
numpy==1.26.*
scipy>=1.11.0 ; python_version < "3.13"
requests
-r base.txt
"""


def test_requirements_lines_become_dependency_pins():
    pins = entry(f"{TVM_CHECKOUT}/python/requirements.txt", "text", body=REQUIREMENTS)

    claims = extract_dependency_pins(pins, REQUIREMENTS)

    assert [claim.typed_value for claim in claims] == [
        {"ecosystem": "pip", "package": "numpy", "specifier": "==", "version": "1.26.*"},
        {"ecosystem": "pip", "package": "scipy", "specifier": ">=", "version": "1.11.0"},
    ]
    # A requirements file is project configuration, not documentation.
    assert {claim.source_class for claim in claims} == {"config"}
    assert {claim.extraction_method for claim in claims} == {"requirements_line"}


DOCKERFILE = """FROM ubuntu:22.04 AS builder
RUN apt-get update \\
    && apt-get install -y --no-install-recommends cmake llvm-14 \\
    && rm -rf /var/lib/apt/lists/*

FROM builder AS python-deps
RUN pip install numpy==1.26.4
"""


def test_docker_run_records_pip_and_apt_package_literals_with_their_stage():
    dockerfile = entry(f"{TVM_CHECKOUT}/docker/Dockerfile", "dockerfile", body=DOCKERFILE)

    claims = extract_dependency_pins(dockerfile, DOCKERFILE)

    assert [claim.typed_value for claim in claims] == [
        {"ecosystem": "apt", "package": "cmake"},
        {"ecosystem": "apt", "package": "llvm-14"},
        {"ecosystem": "pip", "package": "numpy", "specifier": "==", "version": "1.26.4"},
    ]
    assert [claim.applicability.dockerfile_stage for claim in claims] == [
        "builder",
        "builder",
        "python-deps",
    ]
    assert {claim.extraction_method for claim in claims} == {"dockerfile_run"}


def test_two_pins_on_one_line_keep_distinct_claim_ids():
    workflow = entry(f"{TVM_CHECKOUT}/.github/workflows/ci.yml", "yaml", body=TVM_CI)

    claims = extract_dependency_pins(workflow, TVM_CI)

    assert len({claim.claim_id for claim in claims}) == len(claims)
    assert [claim.source_ref.source_range for claim in claims] == [
        f"L{line_of(TVM_CI, 'pip install')}#0",
        f"L{line_of(TVM_CI, 'pip install')}#1",
    ]


# ---------------------------------------------------------------------------
# extractor: env and CMake definitions
# ---------------------------------------------------------------------------


def test_tvm_ci_cmake_args_records_the_variable_and_each_definition():
    workflow = entry(f"{TVM_CHECKOUT}/.github/workflows/ci.yml", "yaml", body=TVM_CI)

    claims = extract_env_definitions(workflow, TVM_CI)

    assert [claim.typed_value for claim in claims] == [
        {
            "scope": "environment",
            "name": "CMAKE_ARGS",
            "value": "-DUSE_LLVM=ON -DUSE_CUDA=OFF",
        },
        {"scope": "cmake_definition", "name": "USE_LLVM", "value": "ON"},
        {"scope": "cmake_definition", "name": "USE_CUDA", "value": "OFF"},
    ]
    assert {claim.applicability.workflow_job for claim in claims} == {"build-llvm"}
    assert len({claim.claim_id for claim in claims}) == 3


CONFIG_CMAKE = """# Licensed to the Apache Software Foundation
set(USE_LLVM ON)
set(USE_CUDA OFF)
set(USE_RELAY_DEBUG ${USE_DEBUG})
option(USE_MICRO "Build with micro TVM" OFF)
option(USE_PROFILER "Build the profiler")
"""


def test_cmake_set_and_option_defaults_become_env_claims():
    config = entry(f"{TVM_CHECKOUT}/cmake/config.cmake", "cmake", body=CONFIG_CMAKE)

    claims = extract_env_definitions(config, CONFIG_CMAKE)

    assert [claim.typed_value for claim in claims] == [
        {"scope": "cmake_set", "name": "USE_LLVM", "value": "ON"},
        {"scope": "cmake_set", "name": "USE_CUDA", "value": "OFF"},
        {"scope": "cmake_option", "name": "USE_MICRO", "value": "OFF"},
    ]
    assert {claim.extraction_method for claim in claims} == {"cmake_set", "cmake_option"}


def test_shell_assignment_literals_become_env_claims():
    script = entry(f"{CHECKOUT}/build.sh", "shell", body=SHELL_SCRIPT)

    claims = extract_env_definitions(script, SHELL_SCRIPT)

    assert [claim.typed_value for claim in claims] == [
        {"scope": "environment", "name": "BUILD_TYPE", "value": "Release"},
        {"scope": "environment", "name": "CMAKE_ARGS", "value": "-DUSE_LLVM=ON"},
        {"scope": "cmake_definition", "name": "USE_LLVM", "value": "ON"},
    ]


# ---------------------------------------------------------------------------
# applicability
# ---------------------------------------------------------------------------


def test_the_domain_hint_is_the_longest_matching_domain_root():
    readme = entry(f"{CHECKOUT}/bigtop-test-framework/README.md", "markdown", body=BIGTOP_README)

    claims = extract_lifecycle_commands(
        readme,
        BIGTOP_README,
        checkout_root=CHECKOUT,
        domain_roots=[CHECKOUT, f"{CHECKOUT}/bigtop-test-framework"],
    )

    assert claims[0].applicability.domain == f"{CHECKOUT}/bigtop-test-framework"


def test_a_path_outside_every_domain_root_states_no_domain():
    readme = entry(f"{CHECKOUT}/README.md", "markdown", body=BIGTOP_README)

    claims = extract_lifecycle_commands(
        readme,
        BIGTOP_README,
        checkout_root=CHECKOUT,
        domain_roots=[f"{CHECKOUT}/bigtop-test-framework"],
    )

    assert claims[0].applicability.domain is None
    assert "applicability" not in claims[0].payload()


def test_applicability_states_only_the_facts_the_entry_carries():
    readme = entry(f"{CHECKOUT}/README.md", "markdown", body=BIGTOP_README)

    claim = extract_lifecycle_commands(readme, BIGTOP_README, checkout_root=CHECKOUT)[0]

    assert claim.applicability == Applicability()
    assert claim.applicability.os is None
    assert claim.applicability.arch is None
    assert claim.applicability.goal is None


# ---------------------------------------------------------------------------
# equal-applicability conflicts
# ---------------------------------------------------------------------------


def conflicting_pair():
    readme = policy_claim(
        typed_value={"tool": "maven", "constraint": "[3.9,)"},
        source_ref={**DOC_REF, "source_range": "L12"},
        applicability={"domain": f"{CHECKOUT}/bigtop-test-framework"},
    )
    workflow = policy_claim(
        source_class="config",
        extraction_method="ci_run_step",
        typed_value={"tool": "maven", "constraint": "3.6"},
        source_ref={**DOC_REF, "entry_id": "doc-ffffffffffff", "source_range": "L4"},
        applicability={"domain": f"{CHECKOUT}/bigtop-test-framework"},
    )
    return readme, workflow


def test_equal_applicability_with_a_different_value_keeps_both_and_records_one_conflict():
    readme, workflow = conflicting_pair()

    conflicts = find_claim_conflicts([readme, workflow])

    assert len(conflicts) == 1
    assert conflicts[0]["kind"] == "claim_conflict"
    assert conflicts[0]["claim_ids"] == sorted([readme.claim_id, workflow.claim_id])
    assert "maven" in conflicts[0]["detail"]


def test_agreeing_sources_are_not_a_conflict():
    readme, workflow = conflicting_pair()
    agreeing = workflow.model_copy(update={"typed_value": dict(readme.typed_value)})

    assert find_claim_conflicts([readme, agreeing]) == []


def test_different_subjects_under_one_applicability_are_not_a_conflict():
    numpy = policy_claim(
        kind="dependency",
        typed_value={"ecosystem": "pip", "package": "numpy", "specifier": "==", "version": "1.26"},
        source_ref={**DOC_REF, "source_range": "L1"},
    )
    scipy = policy_claim(
        kind="dependency",
        typed_value={"ecosystem": "pip", "package": "scipy", "specifier": ">=", "version": "1.11"},
        source_ref={**DOC_REF, "source_range": "L2"},
    )

    assert find_claim_conflicts([numpy, scipy]) == []


def test_differently_applicable_sources_are_not_a_conflict():
    readme, workflow = conflicting_pair()
    other_job = workflow.model_copy(
        update={
            "applicability": Applicability(
                domain=f"{CHECKOUT}/bigtop-test-framework", workflow_job="nightly"
            )
        }
    )

    assert find_claim_conflicts([readme, other_job]) == []


def test_a_conflict_record_names_every_claim_in_the_group_once():
    readme, workflow = conflicting_pair()
    third = policy_claim(
        typed_value={"tool": "maven", "constraint": "3.8"},
        source_ref={**DOC_REF, "entry_id": "doc-eeeeeeeeeeee", "source_range": "L9"},
        applicability={"domain": f"{CHECKOUT}/bigtop-test-framework"},
    )

    conflicts = find_claim_conflicts([readme, workflow, third])

    assert len(conflicts) == 1
    assert len(conflicts[0]["claim_ids"]) == 3


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_write_claims_persists_each_claim_atomically():
    execute = ContainerFS()
    readme, workflow = conflicting_pair()

    assert write_claims(execute, [readme, workflow]) is True

    for claim in (readme, workflow):
        final = f"{CLAIM_DIR}/{claim.claim_id}.json"
        assert json.loads(execute.files[final]) == claim.payload()
    assert all("mv -f " in command for command in execute.writes())
    assert claims_written(execute.commands) == [readme.payload(), workflow.payload()]


def test_a_persisted_claim_states_its_schema_and_omits_absent_facts():
    claim = policy_claim()

    payload = claim.payload()

    assert payload["schema_version"] == CLAIM_SCHEMA_VERSION
    assert payload["claim_id"] == claim.claim_id
    assert set(payload) == {
        "schema_version",
        "claim_id",
        "kind",
        "typed_value",
        "source_class",
        "source_ref",
        "source_status",
        "evidence_status",
        "extraction_method",
    }


def test_write_claims_is_idempotent_for_the_same_body():
    execute = ContainerFS()
    claim = policy_claim()

    assert write_claims(execute, [claim]) is True
    assert write_claims(execute, [claim]) is True

    assert len(execute.writes()) == 1


def test_write_claims_never_overwrites_a_different_body_under_one_id():
    execute = ContainerFS()
    first = policy_claim()
    second = policy_claim(typed_value={"tool": "maven", "constraint": "3.6"})
    assert first.claim_id == second.claim_id
    assert write_claims(execute, [first]) is True
    final = f"{CLAIM_DIR}/{first.claim_id}.json"
    persisted = execute.files[final]

    assert write_claims(execute, [second]) is False

    assert execute.files[final] == persisted
    assert len(execute.writes()) == 1


def test_write_claims_reports_a_failed_persist_without_raising():
    execute = ContainerFS(writable=False)

    assert write_claims(execute, [policy_claim()]) is False


def test_write_claims_never_raises_when_the_container_is_gone():
    def execute(command, **kwargs):
        raise RuntimeError("container is gone")

    assert write_claims(execute, [policy_claim()]) is False


def test_write_claims_persists_nothing_for_an_empty_batch():
    execute = ContainerFS()

    assert write_claims(execute, []) is True
    assert execute.commands == []


# ---------------------------------------------------------------------------
# the whole-entry pass
# ---------------------------------------------------------------------------


def test_extract_policy_claims_runs_every_extractor_over_one_entry():
    workflow = entry(f"{TVM_CHECKOUT}/.github/workflows/ci.yml", "yaml", body=TVM_CI)

    claims = extract_policy_claims(workflow, TVM_CI, checkout_root=TVM_CHECKOUT)

    assert kinds(claims) == ["dependency", "env", "lifecycle"]
    assert len({claim.claim_id for claim in claims}) == len(claims)
    assert {"scope": "cmake_definition", "name": "USE_LLVM", "value": "ON"} in values(claims, "env")


def test_extraction_is_deterministic():
    workflow = entry(f"{TVM_CHECKOUT}/.github/workflows/ci.yml", "yaml", body=TVM_CI)

    first = extract_policy_claims(workflow, TVM_CI, checkout_root=TVM_CHECKOUT)
    second = extract_policy_claims(workflow, TVM_CI, checkout_root=TVM_CHECKOUT)

    assert [claim.payload() for claim in first] == [claim.payload() for claim in second]


def test_an_entry_without_provenance_extracts_nothing():
    """A claim whose source ref cannot be built is not a claim."""
    broken = entry(f"{CHECKOUT}/README.md", "markdown", body=BIGTOP_README)
    broken["source_hash"] = ""

    assert extract_policy_claims(broken, BIGTOP_README, checkout_root=CHECKOUT) == []


def test_shell_continuations_join_and_yield_the_pip_pin():
    """Live tvm r3: docker/install/ubuntu_install_python_package.sh pins
    numpy==1.26.* on a backslash-continued pip line. The continuation joins
    its opener, the pin becomes a dependency claim, and the env-assignment
    parser no longer mints a mangled `numpy` variable."""
    entry = {
        "entry_id": "doc-c96963c0f048",
        "source_hash": "ec" * 32,
        "path": "/workspace/tvm/docker/install/ubuntu_install_python_package.sh",
        "kind": "shell",
    }
    text = (
        "#!/bin/bash\n"
        "pip3 install --upgrade \\\n"
        "    cloudpickle \\\n"
        "    numpy==1.26.* \\\n"
        "    packaging\n"
    )

    pins = extract_dependency_pins(entry, text)
    numpy_pins = [
        pin for pin in pins if pin.typed_value.get("package") == "numpy"
    ]
    assert len(numpy_pins) == 1
    assert numpy_pins[0].typed_value["specifier"] == "=="
    assert numpy_pins[0].typed_value["version"] == "1.26.*"

    envs = extract_env_definitions(entry, text)
    assert not any(claim.typed_value.get("name") == "numpy" for claim in envs)
