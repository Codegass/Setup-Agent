import inspect
import itertools
import posixpath
from functools import wraps

import pytest

from sag.agent.action_intents import action_fingerprint
from sag.agent.control_events import ControlEventSink
from sag.agent.evidence_publications import (
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.output_storage import OutputStorageManager
from sag.agent.invocation_receipts import active_receipt_run_id, set_active_receipt_run_id
from sag.agent.invocation_contracts import (
    ARGV_EXECUTION_BINDING,
    PYTHON_FACADE_EXECUTION_BINDING,
    action_context,
    build_contract,
    current_action_context,
    current_contract,
    dispatch_contract,
)
from sag.tools.base import bind_tool_result_output_storage
from container_evidence_fakes import ContainerFS


@pytest.fixture(scope="session")
def durable_tool_result_storage(tmp_path_factory):
    return OutputStorageManager(tmp_path_factory.mktemp("tool-result-output"))


@pytest.fixture(autouse=True)
def bind_durable_tool_result_storage(durable_tool_result_storage):
    with bind_tool_result_output_storage(
        durable_tool_result_storage,
        task_id="pytest",
        tool_name="test",
    ):
        yield


@pytest.fixture(autouse=True)
def bind_host_evidence_publication_authority(tmp_path):
    """Give unit writers the same host-owned authority a SetupAgent installs."""

    sink = ControlEventSink(tmp_path / "host-control-events.jsonl")
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-pytest",
        sink=sink,
    )
    previous_run_id = active_receipt_run_id()
    set_active_receipt_run_id("run-pytest")
    token = install_evidence_publication_authority(authority)
    try:
        yield authority
    finally:
        reset_evidence_publication_authority(token)
        set_active_receipt_run_id(previous_run_id)


@pytest.fixture
def facade_contract_authority():
    """A precise Maven-test authority for legacy units below BuildTool.

    Tests exercising another executor/action must freeze their own exact v2
    contract; this fixture is deliberately not wildcard dispatch authority.
    """

    params = {"action": "test", "working_directory": "/workspace"}
    domain_id = "test:/workspace"
    contract = build_contract(
        run_id="run-test-facade",
        envelope_id="envelope-test-runner",
        tool="build",
        params=params,
        effective_tool="maven",
        effective_action="test",
        expected_cwd="/workspace",
        expected_argv="--fail-at-end test",
        execution_binding=ARGV_EXECUTION_BINDING,
        intent_source="controller",
        intent_id="intent-test-facade",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
    )
    with action_context(
        envelope_id=None,
        intent_source="controller",
        intent_id="intent-test-facade",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=contract["action_fingerprint"],
    ):
        with dispatch_contract(contract):
            yield contract


@pytest.fixture
def exact_build_facade_authority(monkeypatch):
    """Mint one exact ActionIntent scope per direct BuildTool unit call.

    Opt-in only. Tests that already open a recorded envelope keep their own
    authority; the fixture replaces only the legacy sinkless test scope.
    """

    from sag.tools.build.build_tool import BuildTool

    original = BuildTool.execute
    signature = inspect.signature(original)
    sequence = itertools.count(1)
    contract_stores = {}

    def bind_contract_store(orchestrator):
        """Give legacy marker doubles a real append-only contract store.

        Their catch-all ``success`` response predates the contract CAS.  It is
        not proof that ``cat <contract>`` found a file, so forwarding those
        commands would make every first write look like a collision.  Keep the
        project-marker surface untouched and route only the contract namespace
        through the shared atomic container fake.
        """

        key = id(orchestrator)
        cached = contract_stores.get(key)
        if cached is not None:
            return
        store = ContainerFS()
        original_execute = orchestrator.execute_command

        @wraps(original_execute)
        def execute_command(command, **kwargs):
            if "/workspace/.setup_agent/invocation_contracts" in command:
                return store(command, **kwargs)
            return original_execute(command, **kwargs)

        monkeypatch.setattr(orchestrator, "execute_command", execute_command)
        contract_stores[key] = store

    @wraps(original)
    def execute(self, *args, **kwargs):
        if current_action_context().envelope_id:
            return original(self, *args, **kwargs)
        bind_contract_store(self.docker_orchestrator)
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        params = {
            "action": values["action"],
            "working_directory": values["working_directory"],
        }
        for key in (
            "args",
            "timeout",
            "maven_version_requirement",
            "features",
            "definitions",
        ):
            value = values.get(key)
            if value is None:
                continue
            if key == "features":
                value = list(value)
            elif key == "definitions":
                value = dict(value)
            params[key] = value
        ordinal = next(sequence)
        domain_id = f"test:{values['working_directory']}"
        with action_context(
            envelope_id=f"envelope-build-fixture-{ordinal:06d}",
            intent_source="controller",
            intent_id=f"intent-build-fixture-{ordinal:06d}",
            intent_domain_id=domain_id,
            intent_exact_params=params,
            action_fingerprint=action_fingerprint(
                domain_id=domain_id,
                tool="build",
                params=params,
            ),
        ):
            return original(self, *args, **kwargs)

    monkeypatch.setattr(BuildTool, "execute", execute)


def _direct_contract(
    *,
    ordinal,
    effective_tool,
    effective_action,
    cwd,
    expected_argv,
    public_action,
    public_params=None,
    binding=ARGV_EXECUTION_BINDING,
):
    params = dict(public_params or {"action": public_action, "working_directory": cwd})
    domain_id = f"test:{cwd}"
    return build_contract(
        run_id="run-direct-runner-fixture",
        envelope_id=f"envelope-direct-{effective_tool}-{ordinal:06d}",
        tool="build",
        params=params,
        effective_tool=effective_tool,
        effective_action=effective_action,
        expected_cwd=cwd,
        expected_argv=expected_argv,
        execution_binding=binding,
        intent_source="controller",
        intent_id=f"intent-direct-{effective_tool}-{ordinal:06d}",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
    )


@pytest.fixture
def exact_internal_runner_authority(monkeypatch):
    """Bind direct Maven/Gradle units to their exact executor/action/cwd.

    The contract freezes the requested lifecycle/task vector. Runner-selected
    executable and harness-owned flags may classify as mechanically equivalent,
    exactly as production's argv_v1 matcher specifies.
    """

    from sag.tools.internal.gradle_tool import GradleTool
    from sag.tools.internal.maven_tool import MavenTool

    sequence = itertools.count(1)

    def install(tool_class, executor):
        original = tool_class.execute
        signature = inspect.signature(original)

        @wraps(original)
        def execute(self, *args, **kwargs):
            existing = current_contract()
            if existing and existing.get("run_id") != "run-test-facade":
                return original(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            cwd = str(values.get("working_directory") or "/workspace")
            if executor == "maven":
                pom_file = str(values.get("pom_file") or "")
                if pom_file.endswith("pom.xml") and posixpath.dirname(pom_file):
                    cwd = posixpath.dirname(pom_file)
                requested = MavenTool._action_text(values.get("command"))
                goals = MavenTool._action_text(values.get("goals"))
                effective = " ".join(part for part in (requested, goals) if part) or "build"
            else:
                effective = str(values.get("tasks") or values.get("command") or "build")
                requested = effective
            public_action = (
                "test"
                if any(token in effective.lower() for token in ("test", "verify", "check"))
                else "compile"
                if "compile" in effective.lower()
                else "deps"
                if "depend" in effective.lower()
                else "install"
                if "install" in effective.lower() or "publishtomavenlocal" in effective.lower()
                else "package"
            )
            contract = _direct_contract(
                ordinal=next(sequence),
                effective_tool=executor,
                effective_action=effective,
                cwd=cwd,
                expected_argv=effective,
                public_action=public_action,
            )
            with dispatch_contract(contract):
                return original(self, *args, **kwargs)

        monkeypatch.setattr(tool_class, "execute", execute)

    install(MavenTool, "maven")
    install(GradleTool, "gradle")


@pytest.fixture
def exact_python_runner_authority(monkeypatch):
    """Bind direct PythonTool units to one exact semantic facade call."""

    from sag.agent.invocation_contracts import PYTHON_PUBLIC_ACTION_TO_OPERATION
    from sag.tools.internal.python_tool import PythonTool

    reverse = {operation: action for action, operation in PYTHON_PUBLIC_ACTION_TO_OPERATION.items()}
    reverse["build"] = "package"
    original = PythonTool.execute
    signature = inspect.signature(original)
    sequence = itertools.count(1)

    @wraps(original)
    def execute(self, *args, **kwargs):
        existing = current_contract()
        if existing and existing.get("run_id") != "run-test-facade":
            return original(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        operation = str(values.get("operation") or "").strip().lower()
        public_action = reverse.get(operation)
        if public_action is None:
            return original(self, *args, **kwargs)
        cwd = str(values.get("working_directory") or "/workspace")
        params = {"action": public_action, "working_directory": cwd}
        if values.get("args") is not None:
            params["args"] = values["args"]
        if values.get("timeout") != 600:
            params["timeout"] = values["timeout"]
        native = values.get("native")
        if operation == "native" and isinstance(native, dict):
            params["features"] = list(native.get("features") or ())
            params["definitions"] = dict(native.get("definitions") or {})
        contract = _direct_contract(
            ordinal=next(sequence),
            effective_tool="python",
            effective_action=operation,
            cwd=cwd,
            expected_argv=None,
            public_action=public_action,
            public_params=params,
            binding=PYTHON_FACADE_EXECUTION_BINDING,
        )
        with dispatch_contract(contract):
            return original(self, *args, **kwargs)

    monkeypatch.setattr(PythonTool, "execute", execute)
