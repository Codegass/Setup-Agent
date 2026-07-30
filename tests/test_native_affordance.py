# tests/test_native_affordance.py
"""Plan 6 Stage E1 — the typed native affordance rides the reactive contract.

Spec §C8: a native repair is available only through a validated
`InvocationContract`, and "raw documentation never directly authorizes a
shell/privileged operation". Two facts have to hold at once for that to be
true, and they pull in opposite directions:

* the affordance must be REAL — a receipt that proves LLVM absent has to be
  answerable by installing LLVM and rebuilding the project against it;
* nothing the model writes may reach a package name, a probe, an environment
  variable or a claim id. `features` and `definitions` are allowlisted
  SELECTORS; the harness owns every token that ends up on a command line, and
  provenance is looked up from stored evidence rather than declared.

So this file is mostly negative controls. The positive path is one test; the
rest assert what a native call CANNOT do:

* an unknown feature, a definition outside the allowlist, and a
  features/definitions pair that disagrees with itself never reach a backend;
* a capability nobody assessed and a definition no claim states are refused as
  `NATIVE_WITHOUT_PROVENANCE` — "the state is unknown, not repairable";
* a maven/gradle tree gets a plain refusal rather than an apt install;
* the resolver's command line carries resolver tokens only;
* `deps` args are exact pins a dependency claim states, or nothing.

Claims and assessments are consumed through their persisted shapes only
(hand-written fixtures; lanes a2/c2 own the producers), and the fake
orchestrator records every command so "no runner was invoked" is checkable
rather than asserted.
"""

import json
import shlex

import pytest

from sag.agent.claim_records import CLAIM_DIR
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.invocation_contracts import CONTRACT_DIR
from sag.agent.repair_contracts import (
    NO_SAFE_PROPOSAL,
    build_repair,
    propose_public_call,
)
from sag.tools.base import ToolResult
from sag.tools.build.backends import (
    NATIVE_DEFINITION_ENV,
    NATIVE_DEFINITION_KEY,
    NATIVE_DEFINITION_VALUES,
    NATIVE_FEATURE_RESOLVER,
    PythonBackend,
    native_cmake_args,
)
from sag.tools.build.build_tool import (
    NATIVE_UNSOURCED_CLAUSE,
    _EDGE_GATED_VERBS,
    _PREFLIGHT_VERBS,
    BuildTool,
)
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_tool import NATIVE_SMOKE_RECEIPT_JSON, PythonTool

PROJECT = "/workspace/proj"
FEATURES = ["llvm"]
DEFINITIONS = {"USE_LLVM": "ON", "BUILD_TESTING": "OFF"}
CMAKE_ARGS = "-DBUILD_TESTING=OFF -DUSE_LLVM=ON"


# ---------------------------------------------------------------------------
# hand-written persisted fixtures (lane a2 / c2 output shapes)
# ---------------------------------------------------------------------------


def capability_assessment(feature="llvm", *, receipt_id="inv-python-1-0001"):
    """A persisted `ReceiptAssessment` naming a capability absent (lane c2)."""
    return {
        "schema_version": 1,
        "assessment_id": f"asm-{receipt_id}-capability_absent_{feature}-0000abcd",
        "receipt_id": receipt_id,
        "typed_code": f"capability_absent_{feature}",
        "detail": f"tests/test_{feature}.py was skipped: need {feature}",
    }


def env_claim(name, value, *, claim_id=None, scope="cmake_definition"):
    """A persisted env `PolicyClaim` stating one build definition (lane a2)."""
    return {
        "schema_version": 1,
        "claim_id": claim_id or f"env-{name.lower()}0000",
        "kind": "env",
        "typed_value": {"scope": scope, "name": name, "value": value},
        "source_class": "config",
        "source_ref": {
            "entry_id": "doc-000000000001",
            "source_hash": "a" * 64,
            "source_range": "L12",
        },
        "source_status": "current",
        "evidence_status": "untested",
        "extraction_method": "cmake_args",
        "applicability": {"domain": PROJECT},
    }


def dependency_claim(package, version, *, specifier="==", claim_id="dependency-000000000001"):
    return {
        "schema_version": 1,
        "claim_id": claim_id,
        "kind": "dependency",
        "typed_value": {
            "ecosystem": "pip",
            "package": package,
            "specifier": specifier,
            "version": version,
        },
        "source_class": "config",
        "source_ref": {
            "entry_id": "doc-000000000002",
            "source_hash": "b" * 64,
            "source_range": "L1",
        },
        "source_status": "current",
        "evidence_status": "untested",
        "extraction_method": "requirements_line",
        "applicability": {"domain": PROJECT},
    }


AUTHORIZED = {
    "assessments": [capability_assessment()],
    "claims": [
        env_claim("USE_LLVM", "ON", claim_id="env-usellvm0001"),
        env_claim("BUILD_TESTING", "OFF", claim_id="env-buildtest001", scope="cmake_option"),
    ],
}


# ---------------------------------------------------------------------------
# the fake container
# ---------------------------------------------------------------------------


class NativeOrchestrator:
    """Marker probes, a manifest, and a readable evidence directory.

    Every command is recorded, which is what makes "no runner was invoked" and
    "no model token reached the command line" checkable rather than asserted.
    Assessment writes land in `self.written` so a refusal's control fact is
    observable; assessment/claim READS are served from the fixtures.
    """

    def __init__(self, requirements=None, markers=("pyproject.toml",), assessments=(), claims=()):
        self.files = {REQUIREMENTS_PATH: json.dumps(requirements or {})}
        self.markers = set(markers)
        self.commands = []
        self.written = []
        self.records = {
            ASSESSMENT_DIR: [dict(item) for item in assessments],
            CLAIM_DIR: [dict(item) for item in claims],
        }
        self.contracts = []

    def read_file(self, path):
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure — a failed read now raises on the exact
            # path, because "could not look" is not "looked and found nothing".
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def _glob_directory(self, command):
        for directory in self.records:
            if f"{directory}/*.json" in command:
                return directory
        return None

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        directory = self._glob_directory(command)
        if directory is not None:
            bodies = [json.dumps(record, sort_keys=True) for record in self.records[directory]]
            return {"success": True, "output": "\n".join(bodies), "exit_code": 0}
        if command.startswith("cat ") and "\n" not in command:
            return {"success": False, "output": "", "exit_code": 1}
        if ASSESSMENT_DIR in command and "mv -f " in command:
            self.written.append(json.loads(command.split("\n")[1]))
            return {"success": True, "output": "", "exit_code": 0}
        if CONTRACT_DIR in command and "mv -f " in command:
            self.contracts.append(json.loads(command.split("\n")[1]))
            return {"success": True, "output": "", "exit_code": 0}
        for marker in self.markers:
            if marker in command:
                return {"success": True, "output": "exists", "exit_code": 0}
        return {"success": True, "output": "missing", "exit_code": 0}


class RecordingBackendTool:
    """Stands in for PythonTool/MavenTool: a call here means a runner ran."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result or ToolResult.completed_success(output="ok")


def native_tool(*, requirements=None, markers=("pyproject.toml",), evidence=None, backend=None):
    evidence = evidence if evidence is not None else {"assessments": (), "claims": ()}
    orchestrator = NativeOrchestrator(
        requirements=requirements,
        markers=markers,
        assessments=evidence.get("assessments") or (),
        claims=evidence.get("claims") or (),
    )
    runner = backend or RecordingBackendTool()
    system = "maven" if "pom.xml" in markers else ("gradle" if "gradlew" in markers else "python")
    tool = BuildTool(orchestrator, **{f"{system}_tool": runner})
    return tool, runner, orchestrator


def run_native(features=FEATURES, definitions=None, *, evidence=None, **kwargs):
    definitions = DEFINITIONS if definitions is None else definitions
    tool, runner, orchestrator = native_tool(evidence=evidence, **kwargs)
    result = tool.execute(
        action="native",
        working_directory=PROJECT,
        features=features,
        definitions=definitions,
    )
    return result, runner, orchestrator


def typed_codes(orchestrator):
    return [record["typed_code"] for record in orchestrator.written]


# ---------------------------------------------------------------------------
# 1. the verb joins the facade WITHOUT joining the gated verb sets
# ---------------------------------------------------------------------------


def test_native_is_a_public_verb():
    schema = BuildTool(NativeOrchestrator())._get_parameters_schema()

    assert "native" in schema["properties"]["action"]["enum"]
    assert set(schema["properties"]) >= {"features", "definitions"}


def test_native_is_neither_edge_gated_nor_preflighted():
    """It repairs the ENVIRONMENT; it consumes no producer's artifact and it
    never reaches a JVM toolchain, so neither law has anything to say."""
    assert "native" not in _EDGE_GATED_VERBS
    assert "native" not in _PREFLIGHT_VERBS


def test_a_locked_domain_edge_does_not_refuse_a_native_call():
    result, runner, _ = run_native(
        evidence=AUTHORIZED,
        requirements={
            "java_version": "8",
            "java_version_source": "pom",
            "domain_edges": [
                {
                    "consumer": PROJECT,
                    "producer": "/workspace/other",
                    "status": "version_incompatible",
                    "detail": "requires g:a 1.0; producer builds 2.0",
                }
            ],
        },
    )

    assert result.error_code != "DOMAIN_EDGE_BLOCKED"
    assert runner.calls, "the edge law governs producing verbs, not this one"


def test_a_maven_project_is_refused_a_native_call():
    result, runner, orchestrator = run_native(evidence=AUTHORIZED, markers=("pom.xml",))

    assert not result.succeeded
    assert result.error_code == "NATIVE_SYSTEM_UNSUPPORTED"
    assert "maven" in result.output
    assert "PYTHON" in result.output
    assert runner.calls == [], "a maven tree must receive no native dispatch"
    assert typed_codes(orchestrator) == ["native_system_unsupported"]


def test_a_gradle_project_is_refused_a_native_call():
    result, runner, _ = run_native(evidence=AUTHORIZED, markers=("gradlew",))

    assert result.error_code == "NATIVE_SYSTEM_UNSUPPORTED"
    assert runner.calls == []


# ---------------------------------------------------------------------------
# 2. the allowlists (plan §Stage E, EXACT)
# ---------------------------------------------------------------------------


def test_the_allowlists_are_the_plan_s_exact_module_data():
    assert NATIVE_DEFINITION_KEY.pattern == r"^(USE_[A-Z0-9_]+|BUILD_TESTING)$"
    assert NATIVE_DEFINITION_VALUES == ("ON", "OFF")
    assert NATIVE_FEATURE_RESOLVER == {
        "llvm": {
            "debian_packages": ["llvm-dev", "libxml2-dev"],
            "probe": "llvm-config --version",
        }
    }


def test_an_unresolvable_feature_is_refused():
    result, runner, orchestrator = run_native(["cuda"], {"USE_CUDA": "ON"}, evidence=AUTHORIZED)

    assert result.error_code == "NATIVE_FEATURE_UNKNOWN"
    assert "cuda" in result.output
    assert runner.calls == []
    assert typed_codes(orchestrator) == ["native_feature_unknown"]


def test_an_empty_feature_list_is_refused():
    result, runner, _ = run_native([], DEFINITIONS, evidence=AUTHORIZED)

    assert result.error_code == "NATIVE_FEATURE_UNKNOWN"
    assert runner.calls == []


@pytest.mark.parametrize(
    "definitions",
    [
        {"CMAKE_C_COMPILER_LAUNCHER": "ON"},
        {"CMAKE_TOOLCHAIN_FILE": "ON"},
        {"USE_LLVM;rm -rf /": "ON"},
        {"use_llvm": "ON"},
        {"USE LLVM": "ON"},
    ],
    ids=["launcher", "toolchain-file", "escaped", "lowercase", "spaced"],
)
def test_a_definition_key_outside_the_allowlist_is_refused(definitions):
    result, runner, orchestrator = run_native(FEATURES, definitions, evidence=AUTHORIZED)

    assert result.error_code == "NATIVE_DEFINITION_REJECTED"
    assert runner.calls == []
    assert typed_codes(orchestrator) == ["native_definition_rejected"]


@pytest.mark.parametrize(
    "value",
    ["/usr/lib/llvm-15", "ON; apt-get remove -y python3", "on", "1", "TRUE", ""],
    ids=["path", "chained", "lowercase", "numeric", "boolean", "empty"],
)
def test_a_definition_value_outside_ON_OFF_is_refused(value):
    result, runner, _ = run_native(FEATURES, {"USE_LLVM": value}, evidence=AUTHORIZED)

    assert result.error_code == "NATIVE_DEFINITION_REJECTED"
    assert runner.calls == []


def test_an_empty_definition_map_is_refused():
    result, runner, _ = run_native(FEATURES, {}, evidence=AUTHORIZED)

    assert result.error_code == "NATIVE_DEFINITION_REJECTED"
    assert runner.calls == []


def test_a_rejected_definition_never_reaches_a_shell_command_line():
    """A refusal may NAME the token in the fact it records; nothing may RUN it.

    The evidence writers put their JSON in a single-quoted heredoc body, which
    the shell never expands, so the property to assert is over the command
    lines themselves rather than over the bytes of a recorded fact.
    """
    injected = "ON; apt-get remove -y python3"
    _, _, orchestrator = run_native(FEATURES, {"USE_LLVM": injected}, evidence=AUTHORIZED)

    command_lines = [command.split("\n", 1)[0] for command in orchestrator.commands]
    assert not [line for line in command_lines if "apt-get remove" in line]
    assert all(
        command.startswith("mkdir -p ") for command in orchestrator.commands if injected in command
    )


# ---------------------------------------------------------------------------
# 3. features and definitions must be consistent (spec §C8), BOTH directions
# ---------------------------------------------------------------------------


def test_a_requested_feature_whose_switch_is_absent_is_inconsistent():
    result, runner, orchestrator = run_native(
        ["llvm"], {"BUILD_TESTING": "OFF"}, evidence=AUTHORIZED
    )

    assert result.error_code == "NATIVE_DEFINITIONS_INCONSISTENT"
    assert "USE_LLVM" in result.output
    assert runner.calls == []
    assert typed_codes(orchestrator) == ["native_definitions_inconsistent"]


def test_a_requested_feature_switched_OFF_is_inconsistent():
    result, runner, _ = run_native(["llvm"], {"USE_LLVM": "OFF"}, evidence=AUTHORIZED)

    assert result.error_code == "NATIVE_DEFINITIONS_INCONSISTENT"
    assert runner.calls == []


def test_a_switch_for_an_unnamed_feature_is_inconsistent():
    result, runner, _ = run_native(
        ["llvm"], {"USE_LLVM": "ON", "USE_CUDA": "ON"}, evidence=AUTHORIZED
    )

    assert result.error_code == "NATIVE_DEFINITIONS_INCONSISTENT"
    assert "cuda" in result.output
    assert runner.calls == []


def test_a_featureless_definition_needs_no_feature():
    """`BUILD_TESTING` names no capability, so it is not a request for one."""
    result, runner, _ = run_native(FEATURES, DEFINITIONS, evidence=AUTHORIZED)

    assert result.error_code is None
    assert runner.calls


# ---------------------------------------------------------------------------
# 4. provenance: a receipt proved it absent, a claim proves the switch owned
# ---------------------------------------------------------------------------


def test_without_a_capability_assessment_the_call_is_refused():
    result, runner, orchestrator = run_native(
        evidence={"assessments": (), "claims": AUTHORIZED["claims"]}
    )

    assert result.error_code == "NATIVE_WITHOUT_PROVENANCE"
    assert "capability_absent_llvm" in result.output
    assert NATIVE_UNSOURCED_CLAUSE in result.output
    assert runner.calls == []
    assert typed_codes(orchestrator) == ["native_without_provenance"]


def test_without_supporting_claims_the_call_is_refused():
    result, runner, orchestrator = run_native(
        evidence={"assessments": AUTHORIZED["assessments"], "claims": ()}
    )

    assert result.error_code == "NATIVE_WITHOUT_PROVENANCE"
    assert "USE_LLVM" in result.output
    assert NATIVE_UNSOURCED_CLAUSE in result.output
    assert runner.calls == []
    assert typed_codes(orchestrator) == ["native_without_provenance"]


def test_a_claim_for_a_different_definition_authorizes_nothing():
    result, runner, _ = run_native(
        evidence={
            "assessments": AUTHORIZED["assessments"],
            "claims": [env_claim("USE_CUDA", "ON"), env_claim("BUILD_TESTING", "OFF")],
        }
    )

    assert result.error_code == "NATIVE_WITHOUT_PROVENANCE"
    assert runner.calls == []


def test_an_assessment_for_a_different_capability_authorizes_nothing():
    result, runner, _ = run_native(
        evidence={"assessments": [capability_assessment("cuda")], "claims": AUTHORIZED["claims"]}
    )

    assert result.error_code == "NATIVE_WITHOUT_PROVENANCE"
    assert runner.calls == []


def test_a_claim_of_another_kind_is_not_definition_provenance():
    """A dependency pin says nothing about whether a build switch exists."""
    result, _, _ = run_native(
        evidence={
            "assessments": AUTHORIZED["assessments"],
            "claims": [dependency_claim("numpy", "1.26.4")],
        }
    )

    assert result.error_code == "NATIVE_WITHOUT_PROVENANCE"


def test_a_claim_stating_the_switch_OFF_still_proves_it_project_owned():
    """The claim proves the knob EXISTS and is the project's; the decision to
    turn it on comes from the receipt that proved the capability absent."""
    result, runner, _ = run_native(
        evidence={
            "assessments": AUTHORIZED["assessments"],
            "claims": [
                env_claim("USE_LLVM", "OFF", scope="cmake_option"),
                env_claim("BUILD_TESTING", "OFF"),
            ],
        }
    )

    assert result.error_code is None
    assert runner.calls


def test_an_authorized_call_freezes_the_claim_ids_it_was_authorized_by():
    _, runner, orchestrator = run_native(evidence=AUTHORIZED)

    contracts = [
        contract for contract in orchestrator.contracts if contract.get("supporting_claim_ids")
    ]
    assert len(contracts) == 1
    assert contracts[0]["supporting_claim_ids"] == ["env-buildtest001", "env-usellvm0001"]
    assert contracts[0]["requested_call"]["params"]["action"] == "native"
    assert runner.calls


def test_provenance_is_not_a_call_parameter():
    """Spec §C8: "`provenance` is not accepted from model parameters"."""
    schema = BuildTool(NativeOrchestrator())._get_parameters_schema()
    assert not {"provenance", "supporting_claim_ids", "claims"} & set(schema["properties"])

    tool, _, _ = native_tool()
    with pytest.raises(TypeError):
        tool.execute(
            action="native",
            working_directory=PROJECT,
            features=FEATURES,
            definitions=DEFINITIONS,
            supporting_claim_ids=["env-usellvm0001"],
        )


def test_a_self_attested_definition_value_cannot_stand_in_for_a_claim():
    """Naming the switch in `definitions` is the request, not its evidence."""
    result, runner, _ = run_native(
        ["llvm"],
        {"USE_LLVM": "ON"},
        evidence={"assessments": AUTHORIZED["assessments"], "claims": ()},
    )

    assert result.error_code == "NATIVE_WITHOUT_PROVENANCE"
    assert runner.calls == []


# ---------------------------------------------------------------------------
# 5. materialization: the bundle the backend receives
# ---------------------------------------------------------------------------


def test_the_authorized_call_materializes_the_native_operation():
    _, runner, _ = run_native(evidence=AUTHORIZED)

    assert runner.calls == [
        {
            "operation": "native",
            "working_directory": PROJECT,
            "native": {"features": ["llvm"], "definitions": DEFINITIONS},
        }
    ]


def test_the_cmake_args_overlay_is_order_independent():
    """The model chose the mapping's order; the environment must not depend on it."""
    forward = native_cmake_args({"USE_LLVM": "ON", "BUILD_TESTING": "OFF"})
    reverse = native_cmake_args({"BUILD_TESTING": "OFF", "USE_LLVM": "ON"})

    assert forward == reverse == CMAKE_ARGS


def test_the_backend_copies_the_bundle_without_repairing_it():
    materialized = PythonBackend(RecordingBackendTool()).materialize(
        "native", None, PROJECT, None, native={"features": ["llvm"], "definitions": DEFINITIONS}
    )

    assert materialized["native"] == {"features": ["llvm"], "definitions": DEFINITIONS}
    assert materialized["operation"] == "native"


# ---------------------------------------------------------------------------
# 6. execution: resolver tokens only, probe verbatim, the same deps ladder
# ---------------------------------------------------------------------------


class ScriptedContainer:
    """A container that answers every command, recording each one.

    `outputs` maps a substring to the result to return, so a test scripts only
    the commands it cares about and everything else succeeds silently.
    """

    def __init__(self, outputs=None, failing=()):
        self.commands = []
        self.outputs = dict(outputs or {})
        self.failing = tuple(failing)
        self.files = {}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if "mv -f " in command and "\n" in command:
            header, _, rest = command.partition("\n")
            heredoc = header.rsplit("<<'", 1)[1].split("'", 1)[0]
            body, _, _ = rest.partition(f"\n{heredoc}")
            self.files[header.rsplit("mv -f ", 1)[1].split()[1]] = body
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("cat > ") and "\n" in command:
            header, _, rest = command.partition("\n")
            self.files[shlex.split(header)[2]] = rest.rpartition("\nSAGEOF")[0]
            return {"success": True, "output": "", "exit_code": 0}
        for needle, output in self.outputs.items():
            if needle in command:
                return {"success": True, "output": output, "exit_code": 0}
        if any(needle in command for needle in self.failing):
            return {"success": False, "output": "boom", "exit_code": 1}
        if command.startswith("cat "):
            return {"success": False, "output": "", "exit_code": 1}
        return {"success": True, "output": "", "exit_code": 0}

    def issued(self, needle):
        """The command LINES containing `needle`.

        First line only: the evidence writers append a single-quoted heredoc
        BODY after theirs, and a receipt that records an argv is not a second
        execution of it.
        """
        lines = [command.split("\n", 1)[0] for command in self.commands]
        return [line for line in lines if needle in line]


PYTHON_MANIFEST = {
    "python_venv": f"{PROJECT}/.venv",
    "python_installer": "pip",
    "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
    "survey": {"project_path": PROJECT},
}


def python_native(container=None, *, bundle=None, manifest=None):
    container = container or ScriptedContainer(
        outputs={"llvm-config --version": "15.0.7\n", "test -x": "EXISTS"}
    )
    container.files[REQUIREMENTS_PATH] = json.dumps(manifest or PYTHON_MANIFEST)
    original = container.execute_command

    def execute_command(command, **kwargs):
        if command.startswith("cat ") and REQUIREMENTS_PATH in command:
            container.commands.append(command)
            return {"success": True, "output": container.files[REQUIREMENTS_PATH], "exit_code": 0}
        return original(command, **kwargs)

    container.execute_command = execute_command
    result = PythonTool(container).execute(
        operation="native",
        working_directory=PROJECT,
        native=bundle or {"features": ["llvm"], "definitions": DEFINITIONS},
    )
    return result, container


def test_the_apt_install_carries_the_resolver_s_packages_and_nothing_else():
    _, container = python_native()

    installs = container.issued("apt-get install")
    assert installs == ["DEBIAN_FRONTEND=noninteractive apt-get install -y llvm-dev libxml2-dev"]


def test_no_model_supplied_token_reaches_the_resolver_command_line():
    """`llvm`, `USE_LLVM`, `ON` and `BUILD_TESTING` are SELECTORS. They pick a
    resolver row and an overlay; they are never typed into an install.

    Asserted as an exact token list, not a substring absence: a token the
    harness does not own must be impossible, not merely unobserved.
    """
    _, container = python_native()

    install = container.issued("apt-get install")[0]
    assert install.split() == [
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "install",
        "-y",
        *NATIVE_FEATURE_RESOLVER["llvm"]["debian_packages"],
    ]
    assert not [
        token for token in ("llvm-config", "USE_LLVM", "BUILD_TESTING") if token in install.split()
    ]


def test_the_feature_probe_runs_verbatim():
    _, container = python_native()

    assert NATIVE_FEATURE_RESOLVER["llvm"]["probe"] in container.commands


def test_the_rebuild_runs_the_project_s_own_install_under_the_overlay():
    _, container = python_native()

    rebuilds = container.issued("pip install -e .")
    assert rebuilds == [
        f"{NATIVE_DEFINITION_ENV}={shlex.quote(CMAKE_ARGS)} "
        f"{PROJECT}/.venv/bin/python -m pip install -e ."
    ]


def test_the_overlay_prefix_is_identical_however_the_definitions_arrived():
    _, forward = python_native(
        bundle={"features": ["llvm"], "definitions": {"USE_LLVM": "ON", "BUILD_TESTING": "OFF"}}
    )
    _, reverse = python_native(
        bundle={"features": ["llvm"], "definitions": {"BUILD_TESTING": "OFF", "USE_LLVM": "ON"}}
    )

    assert forward.issued("pip install -e .") == reverse.issued("pip install -e .")


def test_the_local_provider_and_no_deps_ladder_still_runs_under_the_overlay():
    """The rungs are the reason native goes through the deps machinery at all:
    a native rebuild hits the same unresolvable in-repo provider a plain deps
    install does, and it must recover the same way."""
    container = ScriptedContainer(
        outputs={
            "llvm-config --version": "15.0.7\n",
            "test -x": "EXISTS",
            "pip install -e .": "ERROR: No matching distribution found for provider-dist>=0.2",
        }
    )
    manifest = dict(
        PYTHON_MANIFEST,
        python_declared_dependencies=["provider-dist>=0.2"],
        python_local_providers=[{"distribution_name": "provider-dist", "root": "sub"}],
        python_root=PROJECT,
    )
    container.outputs["cat /workspace/proj/sub/pyproject.toml"] = (
        "[project]\nname = 'provider-dist'\n"
    )
    container.outputs["realpath -m --"] = "/workspace\n/workspace/proj\n/workspace/proj/sub\n"
    container.outputs["EXISTS || echo MISSING"] = "EXISTS"
    _, container = python_native(container, manifest=manifest)

    prefix = f"{NATIVE_DEFINITION_ENV}={shlex.quote(CMAKE_ARGS)} "
    provider = container.issued("pip install -e /workspace/proj/sub")
    no_deps = container.issued("pip install -e . --no-deps")
    assert provider and provider[0].startswith(prefix)
    assert no_deps and no_deps[0].startswith(prefix)


def test_the_receipt_carries_the_probe_line_as_a_capability_observation():
    result, container = python_native()

    receipt = json.loads(
        next(body for path, body in container.files.items() if "/invocation_receipts/" in path)
    )
    assert receipt["capability_observations"] == [
        {"feature": "llvm", "probe": "llvm-config --version", "observation": "15.0.7"}
    ]
    assert result.metadata["capability_observations"] == receipt["capability_observations"]


def test_a_probe_that_answers_nothing_states_no_observation():
    """Absent facts are absent keys: a silent probe saw nothing, and "unknown"
    would be a reading nobody made."""
    container = ScriptedContainer(outputs={"test -x": "EXISTS"})
    result, _ = python_native(container)

    assert result.metadata["capability_observations"] == [
        {"feature": "llvm", "probe": "llvm-config --version"}
    ]


def test_a_native_rebuild_never_writes_the_bounded_smoke_capability_receipt():
    """Existing receipt semantics, asserted unchanged: only an EXECUTED test
    may unlock a full collection, and a rebuild executes no tests."""
    _, container = python_native()

    assert NATIVE_SMOKE_RECEIPT_JSON not in container.files
    assert not container.issued(NATIVE_SMOKE_RECEIPT_JSON)


def test_an_unvalidated_bundle_is_refused_by_the_executor_too():
    result, container = python_native(
        bundle={"features": ["llvm"], "definitions": {"CMAKE_TOOLCHAIN_FILE": "ON"}}
    )

    assert result.error_code == "NATIVE_BUNDLE_REJECTED"
    assert container.issued("apt-get install") == []


# ---------------------------------------------------------------------------
# 7. repair proposals (plan §Stage E item 3)
# ---------------------------------------------------------------------------


def trigger(typed_code, *, receipt_id="inv-python-1-0001"):
    return {
        "schema_version": 1,
        "assessment_id": f"asm-{receipt_id}-{typed_code}-0000abcd",
        "receipt_id": receipt_id,
        "typed_code": typed_code,
    }


def test_a_capability_with_a_matching_switch_claim_proposes_the_native_call():
    claims = [
        env_claim("USE_LLVM", "OFF", claim_id="env-usellvm0001", scope="cmake_option"),
        env_claim("BUILD_TESTING", "OFF", claim_id="env-buildtest001"),
    ]

    repair = build_repair(trigger("capability_absent_llvm"), claims, domain_root=PROJECT)

    assert repair["proposed_public_call"] == {
        "tool": "build",
        "params": {
            "action": "native",
            "features": ["llvm"],
            "definitions": {"BUILD_TESTING": "OFF", "USE_LLVM": "ON"},
            "working_directory": PROJECT,
        },
    }
    assert repair["supporting_claim_ids"] == ["env-usellvm0001", "env-buildtest001"]
    assert repair["typed_failure_or_capability"] == "capability_absent_llvm"


def test_the_proposed_switch_value_comes_from_the_evidence_not_the_claim():
    """The claim states the project's OFF default; the receipt states the
    capability is absent. Enabling it is the repair."""
    repair = build_repair(
        trigger("capability_absent_llvm"),
        [env_claim("USE_LLVM", "OFF", scope="cmake_set")],
    )

    assert repair["proposed_public_call"]["params"]["definitions"] == {"USE_LLVM": "ON"}


def test_a_capability_with_no_switch_claim_proposes_no_native_call():
    call, reason = propose_public_call(
        trigger("capability_absent_llvm"), [env_claim("BUILD_TESTING", "OFF")]
    )

    assert call is None
    assert reason == NO_SAFE_PROPOSAL
    assert (
        build_repair(trigger("capability_absent_llvm"), [env_claim("BUILD_TESTING", "OFF")]) is None
    )


def test_a_switch_claim_for_another_capability_proposes_nothing():
    call, reason = propose_public_call(
        trigger("capability_absent_llvm"), [env_claim("USE_CUDA", "ON")]
    )

    assert call is None
    assert reason == NO_SAFE_PROPOSAL


def test_a_single_exact_pin_proposes_a_targeted_deps_install():
    repair = build_repair(
        trigger("dependency_unresolved_numpy"),
        [dependency_claim("numpy", "1.26.4", claim_id="dependency-111111111111")],
    )

    assert repair["proposed_public_call"]["params"]["action"] == "deps"
    assert repair["proposed_public_call"]["params"]["args"] == "numpy==1.26.4"
    assert repair["supporting_claim_ids"] == ["dependency-111111111111"]


def test_a_range_claim_is_not_an_exact_pin():
    repair = build_repair(
        trigger("dependency_unresolved_numpy"),
        [dependency_claim("numpy", "1.26.4", specifier=">=")],
    )

    assert "args" not in repair["proposed_public_call"]["params"]


def test_several_pins_propose_the_plain_resolution():
    """Choosing one of several pins would cite a claim for a decision the
    claim does not state."""
    repair = build_repair(
        trigger("dependency_unresolved_numpy"),
        [
            dependency_claim("numpy", "1.26.4", claim_id="dependency-111111111111"),
            dependency_claim("scipy", "1.13.0", claim_id="dependency-222222222222"),
        ],
    )

    assert "args" not in repair["proposed_public_call"]["params"]


# ---------------------------------------------------------------------------
# 8. the deps exact-pin path (plan §Stage E item 3)
# ---------------------------------------------------------------------------


def run_deps(args, *, claims=(), markers=("pyproject.toml",)):
    tool, runner, orchestrator = native_tool(
        markers=markers, evidence={"assessments": (), "claims": claims}
    )
    result = tool.execute(action="deps", working_directory=PROJECT, args=args)
    return result, runner, orchestrator


def test_a_pin_a_dependency_claim_states_is_accepted():
    result, runner, _ = run_deps("numpy==1.26.4", claims=[dependency_claim("numpy", "1.26.4")])

    assert result.error_code is None
    assert runner.calls[0]["args"] == "numpy==1.26.4"


def test_a_pin_no_claim_states_is_refused():
    result, runner, orchestrator = run_deps(
        "numpy==9.9.9", claims=[dependency_claim("numpy", "1.26.4")]
    )

    assert result.error_code == "PIN_WITHOUT_PROVENANCE"
    assert runner.calls == []
    assert typed_codes(orchestrator) == ["pin_without_provenance"]


def test_a_pin_with_no_claims_at_all_is_refused():
    result, runner, _ = run_deps("numpy==1.26.4")

    assert result.error_code == "PIN_WITHOUT_PROVENANCE"
    assert runner.calls == []


@pytest.mark.parametrize(
    "args",
    ["--upgrade", "numpy>=1.26", "-r requirements.txt", "numpy==1.26.4 --index-url http://x/"],
    ids=["flag", "range", "requirements-file", "trailing-flag"],
)
def test_a_python_deps_arg_that_is_not_an_exact_pin_is_refused(args):
    result, runner, _ = run_deps(args, claims=[dependency_claim("numpy", "1.26.4")])

    assert result.error_code == "PIN_WITHOUT_PROVENANCE"
    assert "exact pin" in result.output
    assert runner.calls == []


def test_maven_deps_args_stay_a_passthrough():
    """A `-pl` selection is the caller's own scoping, not an install target."""
    result, runner, _ = run_deps("-pl core", markers=("pom.xml",))

    assert result.error_code is None
    assert runner.calls and runner.calls[0]["extra_args"] == "-pl core"


def test_a_python_deps_call_without_args_is_untouched():
    result, runner, _ = run_deps(None)

    assert result.error_code is None
    assert runner.calls and "args" not in runner.calls[0]


def test_the_executor_installs_the_validated_pin_and_nothing_else():
    container = ScriptedContainer(outputs={"test -x": "EXISTS"})
    container.files[REQUIREMENTS_PATH] = json.dumps(PYTHON_MANIFEST)
    original = container.execute_command

    def execute_command(command, **kwargs):
        if command.startswith("cat ") and REQUIREMENTS_PATH in command:
            container.commands.append(command)
            return {"success": True, "output": container.files[REQUIREMENTS_PATH], "exit_code": 0}
        return original(command, **kwargs)

    container.execute_command = execute_command
    PythonTool(container).execute(
        operation="setup_env", working_directory=PROJECT, args="numpy==1.26.4"
    )

    assert container.issued("pip install numpy==1.26.4")
    assert container.issued("pip install -e .") == []
