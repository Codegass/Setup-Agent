"""JDK/Python pre-flight + build-requirements manifest.

The pre-flight CONSUMES the phase-1 analysis (it is a guarantee layer, not a
second analyzer): the analyzer persists requirements into the container at
REQUIREMENTS_PATH; MavenTool/GradleTool call JdkPreflight at the top of every
build/test execution. When the environment already matches, the pre-flight is
a single `java -version` no-op. See
docs/superpowers/specs/2026-07-06-java-execution-strategy-fixes-design.md.

PythonPreflight is the same contract for Python interpreters (uv -> apt
provision ladder), per
docs/superpowers/specs/2026-07-07-python-project-support-design.md Component 2.
"""

import hashlib
import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from loguru import logger

from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    evidence_publication_authority_for,
    latest_publication_raw_sha256,
    publish_evidence_revision,
    verify_latest_evidence_bytes,
)
from sag.agent.evidence_records import (
    EvidencePublicationBinding,
    PublishedJsonObjectRead,
    read_live_published_mutable_json_object,
)
from sag.runtime.container_io import ContainerFileReadError, read_container_text
from sag.runtime.paths import BUILD_REQUIREMENTS_PATH
from sag.tools.internal.python_env import (
    _REPAIR_ACTION_PHRASE,
    ensure_venv_pip,
    resolve_python_version,
)
from sag.utils.container_io import (
    WRITE_COMPARE_CONFLICT,
    compare_publish_container_text_atomic,
)

# Backward-compatible export for existing tool consumers. New engine/shared
# layers import the dependency-neutral runtime constant instead.
REQUIREMENTS_PATH = BUILD_REQUIREMENTS_PATH

# The host publication proves who authorized the exact bytes.  This schema
# separately proves what those bytes mean.  There is deliberately no implicit
# v0 on the live path: an old/stampless object remains available through the
# forensic ``read_build_requirements`` projection, but it cannot authorize a
# dispatch, verdict, or derived evidence record.
BUILD_REQUIREMENTS_SCHEMA_VERSION = 1
BUILD_REQUIREMENTS_SEQUENCE_CAP = 512
BUILD_REQUIREMENTS_DOMAIN_CAP = 256
BUILD_REQUIREMENTS_TEXT_MAX_BYTES = 4096

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{7,64}")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_IMPORT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_RELATIVE_PATH_RE = re.compile(r"[^\x00-\x1f\x7f]+")
_BUILD_SYSTEMS = frozenset(
    {
        "maven",
        "gradle",
        "python",
        "pytest",
        "cmake",
        "cargo",
        "Cargo",
        "go modules",
        "Go modules",
        "Maven",
        "Gradle",
        "npm/yarn",
        "pip/poetry",
        "unknown",
    }
)
_DOMAIN_SYSTEMS = frozenset({"maven", "gradle"})
_DOMAIN_LANGUAGES = frozenset({"java", "groovy", "scala", "kotlin"})
_ROOT_SHAPES = frozenset({"healthy_reactor", "pathological_aggregator", "single_module"})
_EDGE_STATUSES = frozenset({"compatible", "unverified", "version_incompatible"})
_CAPABILITY_STATES = frozenset({"present", "absent", "unknown"})
_PYTHON_INSTALLERS = frozenset({"pip", "pipenv", "poetry"})
_PYTHON_CONSTRAINT_SOURCES = frozenset({"pyproject.toml", "setup.py", "setup.cfg"})
_NATIVE_BUILD_MODES = frozenset({"cmake", "pep517-integrated"})
_SMOKE_SOURCES = frozenset(
    {
        "filesystem:test-file",
        "pyproject.toml:tool.cibuildwheel.test-command",
    }
)

_CORE_KEYS = frozenset(
    {
        "schema_version",
        "survey",
        "java_version",
        "java_version_source",
        "java_version_enforced",
        "root_shape",
        "build_root",
        "fail_at_end",
        "test_root",
        "test_system",
        "test_fail_at_end",
        "build_islands",
        "test_islands",
    }
)
_PYTHON_KEYS = frozenset(
    {
        "python_version",
        "python_constraint",
        "python_constraint_source",
        "python_installer",
        "python_install_commands",
        "python_install_note",
        "python_install_source",
        "python_packages",
        "python_distribution_name",
        "python_build_backend",
        "python_declared_dependencies",
        "python_package_paths",
        "python_local_providers",
        "python_smoke_candidates",
        "python_venv",
        "python_root",
        "has_c_extensions",
        "has_native_build",
        "native_build_mode",
        "native_artifact_roots",
        "test_hints",
    }
)
_OPTIONAL_KEYS = (
    frozenset(
        {
            "build_domains",
            "domain_edges",
            "domain_facts",
            "module_structure",
        }
    )
    | _PYTHON_KEYS
)


def _exact_keys(
    value: Any,
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
    field: str,
) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{field} must be an object")
    keys = set(value)
    missing = set(required) - keys
    extra = keys - set(required) - set(optional)
    if missing or extra:
        raise ValueError(
            f"{field} keys are invalid (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    return dict(value)


def _text(
    value: Any,
    field: str,
    *,
    nullable: bool = False,
    empty: bool = False,
    choices: frozenset[str] | None = None,
    pattern: re.Pattern[str] | None = None,
    max_bytes: int = BUILD_REQUIREMENTS_TEXT_MAX_BYTES,
) -> Optional[str]:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise ValueError(f"{field} must be a string")
    try:
        byte_count = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} is not valid Unicode") from exc
    if (not value and not empty) or byte_count > max_bytes:
        raise ValueError(f"{field} is empty or oversized")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    if choices is not None and value not in choices:
        raise ValueError(f"{field} is outside its enum")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{field} is not canonical")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer in [{minimum}, {maximum}]")
    return value


def _workspace_path(
    value: Any,
    field: str,
    *,
    project_root: str = "/workspace",
    nullable: bool = False,
) -> Optional[str]:
    path = _text(value, field, nullable=nullable, max_bytes=1024)
    if path is None:
        return None
    if (
        not path.startswith("/")
        or posixpath.normpath(path) != path
        or (path != "/workspace" and not path.startswith("/workspace/"))
    ):
        raise ValueError(f"{field} is not a canonical workspace path")
    root = project_root.rstrip("/") or "/"
    if path != root and not path.startswith(root + "/"):
        raise ValueError(f"{field} escapes the surveyed project")
    return path


def _relative_path(value: Any, field: str) -> str:
    path = _text(value, field, max_bytes=1024)
    assert path is not None
    if (
        path.startswith("/")
        or posixpath.normpath(path) != path
        or path in {".", ".."}
        or path.startswith("../")
        or _RELATIVE_PATH_RE.fullmatch(path) is None
    ):
        raise ValueError(f"{field} is not a canonical contained relative path")
    return path


def _sequence(value: Any, field: str, *, cap: int = BUILD_REQUIREMENTS_SEQUENCE_CAP) -> list:
    if type(value) is not list or len(value) > cap:
        raise ValueError(f"{field} must be a bounded list")
    return value


def _unique(values: list[Any], field: str, *, key=lambda item: item) -> None:
    observed: set[Any] = set()
    for item in values:
        identity = key(item)
        if identity in observed:
            raise ValueError(f"{field} contains duplicates")
        observed.add(identity)


def _string_list(
    value: Any,
    field: str,
    *,
    cap: int = BUILD_REQUIREMENTS_SEQUENCE_CAP,
    pattern: re.Pattern[str] | None = None,
    choices: frozenset[str] | None = None,
    item_max_bytes: int = 512,
) -> list[str]:
    values = _sequence(value, field, cap=cap)
    for index, item in enumerate(values):
        _text(
            item,
            f"{field}[{index}]",
            pattern=pattern,
            choices=choices,
            max_bytes=item_max_bytes,
        )
    _unique(values, field)
    return values


def _validate_survey(value: Any) -> Dict[str, Any]:
    survey = _exact_keys(
        value,
        required={
            "project_path",
            "analyzer_version",
            "config_fingerprint",
            "target_sha",
            "document_map_fingerprint",
        },
        field="survey",
    )
    root = _workspace_path(survey["project_path"], "survey.project_path")
    assert root is not None
    # Imported lazily to keep build_preflight's module import independent from
    # the analyzer, which imports this module from its runtime methods.
    from .project_analyzer import SURVEY_FACTS_VERSION

    if (
        _integer(
            survey["analyzer_version"],
            "survey.analyzer_version",
            minimum=SURVEY_FACTS_VERSION,
            maximum=SURVEY_FACTS_VERSION,
        )
        != SURVEY_FACTS_VERSION
    ):  # pragma: no cover - range check states this
        raise ValueError("survey analyzer version is not current")
    _text(
        survey["config_fingerprint"],
        "survey.config_fingerprint",
        nullable=True,
        max_bytes=256,
    )
    _text(
        survey["target_sha"],
        "survey.target_sha",
        nullable=True,
        pattern=_GIT_OBJECT_RE,
        max_bytes=64,
    )
    _text(
        survey["document_map_fingerprint"],
        "survey.document_map_fingerprint",
        nullable=True,
        pattern=_SHA256_RE,
        max_bytes=64,
    )
    return survey


def _validate_islands(value: Any, field: str, *, project_root: str) -> list[Dict[str, Any]]:
    islands = _sequence(value, field, cap=BUILD_REQUIREMENTS_DOMAIN_CAP)
    roots: list[str] = []
    for index, candidate in enumerate(islands):
        island = _exact_keys(
            candidate,
            required={"root", "system"},
            optional={"goal"},
            field=f"{field}[{index}]",
        )
        root = _workspace_path(island["root"], f"{field}[{index}].root", project_root=project_root)
        assert root is not None
        roots.append(root)
        _text(
            island["system"],
            f"{field}[{index}].system",
            nullable=True,
            choices=_DOMAIN_SYSTEMS,
        )
        if "goal" in island:
            _text(
                island["goal"],
                f"{field}[{index}].goal",
                choices=frozenset({"build", "install", "publishToMavenLocal"}),
            )
    _unique(roots, field)
    return [dict(item) for item in islands]


def _validate_coordinate(value: Any, field: str) -> Dict[str, str]:
    coordinate = _exact_keys(
        value,
        required={"name"},
        optional={"group", "version"},
        field=field,
    )
    for key in coordinate:
        _text(coordinate[key], f"{field}.{key}", max_bytes=512)
    return coordinate


def _validate_coordinates(
    value: Any,
    field: str,
    *,
    require_complete: bool = False,
) -> list[Dict[str, str]]:
    coordinates = _sequence(value, field, cap=BUILD_REQUIREMENTS_SEQUENCE_CAP)
    canonical: list[tuple[tuple[str, str], ...]] = []
    for index, candidate in enumerate(coordinates):
        coordinate = _validate_coordinate(candidate, f"{field}[{index}]")
        if require_complete and set(coordinate) != {"group", "name", "version"}:
            raise ValueError(f"{field}[{index}] is not a complete required coordinate")
        canonical.append(tuple(sorted(coordinate.items())))
    _unique(canonical, field)
    return [dict(item) for item in coordinates]


def _validate_domains(value: Any, *, project_root: str) -> list[Dict[str, Any]]:
    domains = _sequence(value, "build_domains", cap=BUILD_REQUIREMENTS_DOMAIN_CAP)
    roots: list[str] = []
    for index, candidate in enumerate(domains):
        domain = _exact_keys(
            candidate,
            required={"root", "system"},
            optional={"languages", "produces", "requires"},
            field=f"build_domains[{index}]",
        )
        root = _workspace_path(
            domain["root"], f"build_domains[{index}].root", project_root=project_root
        )
        assert root is not None
        roots.append(root)
        _text(
            domain["system"],
            f"build_domains[{index}].system",
            choices=_DOMAIN_SYSTEMS,
        )
        if "languages" in domain:
            _string_list(
                domain["languages"],
                f"build_domains[{index}].languages",
                cap=len(_DOMAIN_LANGUAGES),
                choices=_DOMAIN_LANGUAGES,
                item_max_bytes=16,
            )
        for key in ("produces", "requires"):
            if key in domain:
                _validate_coordinates(
                    domain[key],
                    f"build_domains[{index}].{key}",
                    require_complete=key == "requires",
                )
    _unique(roots, "build_domains")
    if domains and len(domains) < 2:
        raise ValueError("build_domains is absent unless it describes a graph")
    return [dict(item) for item in domains]


def _validate_edges(
    value: Any,
    *,
    project_root: str,
    domain_roots: set[str],
) -> list[Dict[str, Any]]:
    edges = _sequence(value, "domain_edges", cap=BUILD_REQUIREMENTS_SEQUENCE_CAP)
    identities: list[str] = []
    for index, candidate in enumerate(edges):
        edge = _exact_keys(
            candidate,
            required={"consumer", "producer", "status", "detail", "edge_id"},
            optional={"support_claim_ids"},
            field=f"domain_edges[{index}]",
        )
        consumer = _workspace_path(
            edge["consumer"],
            f"domain_edges[{index}].consumer",
            project_root=project_root,
        )
        producer = _workspace_path(
            edge["producer"],
            f"domain_edges[{index}].producer",
            project_root=project_root,
        )
        if consumer == producer or consumer not in domain_roots or producer not in domain_roots:
            raise ValueError(f"domain_edges[{index}] endpoints are not distinct domains")
        _text(edge["status"], f"domain_edges[{index}].status", choices=_EDGE_STATUSES)
        _text(edge["detail"], f"domain_edges[{index}].detail", max_bytes=2048)
        identity = _text(
            edge["edge_id"],
            f"domain_edges[{index}].edge_id",
            pattern=re.compile(r"edge-[0-9a-f]{12}"),
            max_bytes=17,
        )
        assert identity is not None
        identities.append(identity)
        if "support_claim_ids" in edge:
            _string_list(
                edge["support_claim_ids"],
                f"domain_edges[{index}].support_claim_ids",
                pattern=_SAFE_ID_RE,
                item_max_bytes=200,
            )
    _unique(identities, "domain_edges")
    return [dict(item) for item in edges]


def _validate_open_conflicts(value: Any, field: str, *, project_root: str) -> None:
    conflicts = _sequence(value, field, cap=BUILD_REQUIREMENTS_SEQUENCE_CAP)
    canonical: list[str] = []
    for index, candidate in enumerate(conflicts):
        if type(candidate) is not dict:
            raise ValueError(f"{field}[{index}] must be an object")
        kind = candidate.get("kind")
        if kind == "version_incompatible":
            record = _exact_keys(
                candidate,
                required={"kind", "edge_id", "detail"},
                field=f"{field}[{index}]",
            )
            _text(record["edge_id"], f"{field}[{index}].edge_id", pattern=_SAFE_ID_RE)
            _text(record["detail"], f"{field}[{index}].detail", max_bytes=2048)
        elif kind == "partial_map":
            record = _exact_keys(
                candidate,
                required={"kind", "path"},
                optional={"reason"},
                field=f"{field}[{index}]",
            )
            _workspace_path(record["path"], f"{field}[{index}].path", project_root=project_root)
            if "reason" in record:
                _text(record["reason"], f"{field}[{index}].reason", max_bytes=256)
        elif kind == "document_map_integrity_unavailable":
            record = _exact_keys(
                candidate,
                required={"kind", "reason"},
                field=f"{field}[{index}]",
            )
            _text(record["reason"], f"{field}[{index}].reason", max_bytes=256)
        else:
            raise ValueError(f"{field}[{index}].kind is outside its enum")
        canonical.append(json.dumps(candidate, sort_keys=True, separators=(",", ":")))
    _unique(canonical, field)


def _validate_domain_facts(
    value: Any,
    *,
    project_root: str,
    domains: list[Dict[str, Any]],
) -> None:
    facts = _sequence(value, "domain_facts", cap=BUILD_REQUIREMENTS_DOMAIN_CAP)
    roots: list[str] = []
    for index, candidate in enumerate(facts):
        fact = _exact_keys(
            candidate,
            required={"domain_id", "root", "role", "environment", "fact_epoch"},
            optional={
                "system",
                "languages",
                "produces",
                "requires",
                "documented_actions",
                "capability_state",
                "open_conflicts",
            },
            field=f"domain_facts[{index}]",
        )
        root = _workspace_path(
            fact["root"], f"domain_facts[{index}].root", project_root=project_root
        )
        assert root is not None
        roots.append(root)
        _text(
            fact["domain_id"],
            f"domain_facts[{index}].domain_id",
            pattern=re.compile(r"dom-[0-9a-f]{12}"),
        )
        expected_domain_id = "dom-" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
        if fact["domain_id"] != expected_domain_id:
            raise ValueError(f"domain_facts[{index}].domain_id does not match its root")
        _text(fact["role"], f"domain_facts[{index}].role", choices=frozenset({"unknown"}))
        _text(
            fact["environment"],
            f"domain_facts[{index}].environment",
            choices=frozenset({"unknown"}),
        )
        _integer(fact["fact_epoch"], f"domain_facts[{index}].fact_epoch", minimum=1, maximum=1)
        if "system" in fact:
            _text(fact["system"], f"domain_facts[{index}].system", choices=_DOMAIN_SYSTEMS)
        if "languages" in fact:
            _string_list(
                fact["languages"],
                f"domain_facts[{index}].languages",
                cap=len(_DOMAIN_LANGUAGES),
                choices=_DOMAIN_LANGUAGES,
                item_max_bytes=16,
            )
        for key in ("produces", "requires"):
            if key in fact:
                _validate_coordinates(
                    fact[key],
                    f"domain_facts[{index}].{key}",
                    require_complete=key == "requires",
                )
        if "documented_actions" in fact:
            _string_list(
                fact["documented_actions"],
                f"domain_facts[{index}].documented_actions",
                pattern=_SAFE_ID_RE,
                item_max_bytes=200,
            )
        if "capability_state" in fact:
            _text(
                fact["capability_state"],
                f"domain_facts[{index}].capability_state",
                choices=_CAPABILITY_STATES,
            )
        if "open_conflicts" in fact:
            _validate_open_conflicts(
                fact["open_conflicts"],
                f"domain_facts[{index}].open_conflicts",
                project_root=root,
            )
    _unique(roots, "domain_facts")
    domain_roots = [str(domain["root"]) for domain in domains]
    if roots != domain_roots:
        raise ValueError("domain_facts does not exactly cover build_domains in order")
    for index, (fact, domain) in enumerate(zip(facts, domains)):
        for key in ("system", "languages", "produces", "requires"):
            if fact.get(key) != domain.get(key):
                raise ValueError(f"domain_facts[{index}].{key} diverges from build_domains")


def _validate_module_structure(value: Any, *, survey: Mapping[str, Any]) -> None:
    structure = _exact_keys(
        value,
        required={"schema_version", "provenance", "modules", "keys"},
        optional={"target_sha", "config_fingerprint"},
        field="module_structure",
    )
    if (
        _integer(
            structure["schema_version"],
            "module_structure.schema_version",
            minimum=2,
            maximum=2,
        )
        != 2
    ):  # pragma: no cover
        raise ValueError("module_structure is not v2")
    _text(structure["provenance"], "module_structure.provenance", pattern=_SAFE_ID_RE)
    modules = _string_list(structure["modules"], "module_structure.modules", item_max_bytes=512)
    keys = _string_list(
        structure["keys"],
        "module_structure.keys",
        pattern=re.compile(r"[a-z0-9]+"),
        item_max_bytes=256,
    )
    if not modules or not keys:
        raise ValueError("module_structure must state modules and keys")
    expected_keys = list(
        dict.fromkeys(
            "".join(
                char
                for char in str(module)
                .replace("\\", "/")
                .rstrip("/")
                .split("/")[-1]
                .split("::")[-1]
                .lower()
                if char.isalnum()
            )
            for module in modules
        )
    )
    expected_keys = [key for key in expected_keys if key]
    if keys != expected_keys:
        raise ValueError("module_structure.keys is not the canonical module projection")
    for pin in ("target_sha", "config_fingerprint"):
        observed = structure.get(pin)
        expected = survey.get(pin)
        if observed != expected:
            raise ValueError(f"module_structure.{pin} does not match survey")
        if pin in structure:
            _text(
                observed,
                f"module_structure.{pin}",
                pattern=_GIT_OBJECT_RE if pin == "target_sha" else None,
                max_bytes=256,
            )


def _validate_install_command(command: Any, field: str) -> None:
    text = _text(command, field, max_bytes=4096)
    assert text is not None
    if text in {"poetry install", "pipenv install --dev"}:
        return
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not valid shell tokenization") from exc
    if tokens[:4] != ["{venv}/bin/python", "-m", "pip", "install"] or len(tokens) < 5:
        raise ValueError(f"{field} is outside the installer command grammar")
    # The fixed tool prefix plus shell-tokenized arguments is the only command
    # family detect_installer can emit.  Explicit shell operators are never an
    # argument; PEP dependency markers remain safe because shlex.join quotes
    # them before they enter this manifest.
    for index, token in enumerate(tokens[4:], start=4):
        _text(token, f"{field}.argv[{index}]", max_bytes=512)
    if tokens[4] == "-e":
        if len(tokens) != 6 or not (
            tokens[5] == "."
            or re.fullmatch(r"\.\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\]", tokens[5])
        ):
            raise ValueError(f"{field} has a noncanonical editable target")
    elif tokens[4] == "-r":
        if (
            len(tokens) != 6
            or "/" in tokens[5]
            or not re.fullmatch(r"requirements[^/]*\.txt", tokens[5])
        ):
            raise ValueError(f"{field} has a noncanonical requirements target")
    elif any(token.startswith("-") for token in tokens[4:]):
        raise ValueError(f"{field} contains an unowned pip option")
    elif shlex.join(tokens[4:]) != text.split(" install ", 1)[1]:
        raise ValueError(f"{field} dependency argv is not canonically shell-quoted")


def _validate_python(body: Mapping[str, Any], *, project_root: str) -> None:
    present = set(body) & set(_PYTHON_KEYS)
    if not present:
        return
    missing = set(_PYTHON_KEYS) - present
    if missing:
        raise ValueError(f"python manifest keys are incomplete: {sorted(missing)}")
    for key in (
        "python_version",
        "python_constraint",
        "python_distribution_name",
        "python_build_backend",
        "python_install_note",
    ):
        _text(body[key], key, nullable=True, max_bytes=1024)
    _text(
        body["python_constraint_source"],
        "python_constraint_source",
        nullable=True,
        choices=_PYTHON_CONSTRAINT_SOURCES,
    )
    _text(body["python_installer"], "python_installer", choices=_PYTHON_INSTALLERS)
    install_source = body["python_install_source"]
    _text(install_source, "python_install_source", nullable=True, max_bytes=256)
    if (
        install_source is not None
        and install_source
        not in {
            "poetry.lock",
            "Pipfile.lock",
            "pyproject.toml",
            "setup.py",
        }
        and re.fullmatch(r"requirements[^/]*\.txt", install_source) is None
    ):
        raise ValueError("python_install_source is outside its enum")
    commands = _sequence(body["python_install_commands"], "python_install_commands", cap=64)
    for index, command in enumerate(commands):
        _validate_install_command(command, f"python_install_commands[{index}]")
    _unique(commands, "python_install_commands")
    _string_list(
        body["python_packages"],
        "python_packages",
        pattern=_IMPORT_NAME_RE,
        item_max_bytes=256,
    )
    _string_list(
        body["python_declared_dependencies"],
        "python_declared_dependencies",
        item_max_bytes=512,
    )
    package_paths = _sequence(body["python_package_paths"], "python_package_paths")
    package_identities: list[tuple[str, str]] = []
    for index, candidate in enumerate(package_paths):
        record = _exact_keys(
            candidate,
            required={"import_name", "path", "source"},
            field=f"python_package_paths[{index}]",
        )
        import_name = _text(
            record["import_name"],
            f"python_package_paths[{index}].import_name",
            pattern=_IMPORT_NAME_RE,
        )
        path = _relative_path(record["path"], f"python_package_paths[{index}].path")
        _text(
            record["source"],
            f"python_package_paths[{index}].source",
            choices=frozenset({"pyproject.toml:tool.scikit-build.wheel.packages"}),
        )
        assert import_name is not None
        package_identities.append((import_name, path))
    _unique(package_identities, "python_package_paths")
    providers = _sequence(body["python_local_providers"], "python_local_providers")
    provider_identities: list[tuple[str, str]] = []
    for index, candidate in enumerate(providers):
        record = _exact_keys(
            candidate,
            required={"distribution_name", "root", "requirement", "build_backend"},
            field=f"python_local_providers[{index}]",
        )
        name = _text(
            record["distribution_name"],
            f"python_local_providers[{index}].distribution_name",
            max_bytes=256,
        )
        root = _relative_path(record["root"], f"python_local_providers[{index}].root")
        _text(
            record["requirement"],
            f"python_local_providers[{index}].requirement",
            max_bytes=512,
        )
        _text(
            record["build_backend"],
            f"python_local_providers[{index}].build_backend",
            nullable=True,
            max_bytes=512,
        )
        assert name is not None
        provider_identities.append((name, root))
    _unique(provider_identities, "python_local_providers")
    smoke = _sequence(body["python_smoke_candidates"], "python_smoke_candidates", cap=64)
    smoke_paths: list[str] = []
    for index, candidate in enumerate(smoke):
        record = _exact_keys(
            candidate,
            required={"path", "source"},
            field=f"python_smoke_candidates[{index}]",
        )
        smoke_paths.append(_relative_path(record["path"], f"python_smoke_candidates[{index}].path"))
        _text(
            record["source"],
            f"python_smoke_candidates[{index}].source",
            choices=_SMOKE_SOURCES,
        )
    _unique(smoke_paths, "python_smoke_candidates")
    python_root = _workspace_path(body["python_root"], "python_root", project_root=project_root)
    assert python_root is not None
    _workspace_path(body["python_venv"], "python_venv", project_root=python_root)
    _boolean(body["has_c_extensions"], "has_c_extensions")
    has_native_build = _boolean(body["has_native_build"], "has_native_build")
    mode = _text(
        body["native_build_mode"],
        "native_build_mode",
        nullable=True,
        choices=_NATIVE_BUILD_MODES,
    )
    roots = _string_list(body["native_artifact_roots"], "native_artifact_roots")
    for index, root in enumerate(roots):
        _relative_path(root, f"native_artifact_roots[{index}]")
    if not has_native_build and (mode is not None or roots):
        raise ValueError("native build details require has_native_build=true")
    hints = _exact_keys(
        body["test_hints"],
        required={"pytest_args", "test_deps"},
        field="test_hints",
    )
    args = _text(hints["pytest_args"], "test_hints.pytest_args", nullable=True, max_bytes=2048)
    if args is not None:
        if any(marker in args for marker in ("$", "`", ";", "|", "&", ">", "<")):
            raise ValueError("test_hints.pytest_args contains shell syntax")
        try:
            tokens = shlex.split(args)
        except ValueError as exc:
            raise ValueError("test_hints.pytest_args is not valid tokenization") from exc
        if any(token in {";", "&&", "||", "|", ">", ">>", "<", "&"} for token in tokens):
            raise ValueError("test_hints.pytest_args contains a shell operator")
    _string_list(hints["test_deps"], "test_hints.test_deps", item_max_bytes=512)


def validate_build_requirements_v1(
    payload: Mapping[str, Any],
    expected_id: str = BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
) -> Dict[str, Any]:
    """Validate one complete, canonical, bounded build-requirements v1.

    This is the single semantic boundary shared by the survey writer, strict
    live reader, and receipt-structure promotion.  It performs no coercion:
    booleans are never integers, tuples are never lists, paths are canonical
    and checkout-contained, all object keys are closed, and future versions
    fail closed.  Returning a byte-equivalent dict lets the generic live
    reader enforce canonical no-normalization semantics too.
    """

    if expected_id != BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID:
        raise ValueError("build requirements publication identity is invalid")
    body = _exact_keys(
        payload,
        required=_CORE_KEYS,
        optional=_OPTIONAL_KEYS,
        field="build requirements",
    )
    if (
        _integer(
            body["schema_version"],
            "schema_version",
            minimum=BUILD_REQUIREMENTS_SCHEMA_VERSION,
            maximum=BUILD_REQUIREMENTS_SCHEMA_VERSION,
        )
        != BUILD_REQUIREMENTS_SCHEMA_VERSION
    ):  # pragma: no cover
        raise ValueError("build requirements schema is not v1")
    survey = _validate_survey(body["survey"])
    project_root = str(survey["project_path"])
    _text(body["java_version"], "java_version", nullable=True, max_bytes=64)
    _text(
        body["java_version_source"],
        "java_version_source",
        nullable=True,
        choices=frozenset({"maven-compiler", "maven-enforcer"}),
    )
    _boolean(body["java_version_enforced"], "java_version_enforced")
    _text(body["root_shape"], "root_shape", choices=_ROOT_SHAPES)
    build_root = _workspace_path(body["build_root"], "build_root", project_root=project_root)
    fail_at_end = _boolean(body["fail_at_end"], "fail_at_end")
    test_root = _workspace_path(
        body["test_root"], "test_root", project_root=project_root, nullable=True
    )
    _text(body["test_system"], "test_system", nullable=True, choices=_BUILD_SYSTEMS)
    test_fail_at_end = _boolean(body["test_fail_at_end"], "test_fail_at_end")
    expected_shape = (
        "healthy_reactor"
        if fail_at_end
        else "pathological_aggregator" if build_root != project_root else "single_module"
    )
    if body["root_shape"] != expected_shape:
        raise ValueError("root_shape disagrees with the canonical build target")
    if test_fail_at_end != (fail_at_end and test_root == project_root):
        raise ValueError("test_fail_at_end disagrees with reactor scope")
    build_islands = _validate_islands(
        body["build_islands"], "build_islands", project_root=project_root
    )
    test_islands = _validate_islands(
        body["test_islands"], "test_islands", project_root=project_root
    )
    if build_islands and str(build_islands[0]["root"]) != build_root:
        raise ValueError("build_islands[0] must be the selected build_root")

    domains: list[Dict[str, Any]] = []
    if "build_domains" in body:
        domains = _validate_domains(body["build_domains"], project_root=project_root)
        domain_roots = [str(domain["root"]) for domain in domains]
        if domain_roots != [str(island["root"]) for island in build_islands]:
            raise ValueError("build_domains does not exactly align with build_islands")
    else:
        domain_roots = []
    if "domain_edges" in body:
        if not domains:
            raise ValueError("domain_edges requires build_domains")
        _validate_edges(
            body["domain_edges"],
            project_root=project_root,
            domain_roots=set(domain_roots),
        )
    if "domain_facts" in body:
        if not domains:
            raise ValueError("domain_facts requires build_domains")
        _validate_domain_facts(
            body["domain_facts"],
            project_root=project_root,
            domains=domains,
        )
    if "module_structure" in body:
        _validate_module_structure(body["module_structure"], survey=survey)
    _validate_python(body, project_root=project_root)
    return body


def write_build_requirements(orchestrator, data: Dict[str, Any]) -> bool:
    """Persist the analyzer's build requirements into the container.

    Plan 8 §3.6: this rewrites the WHOLE manifest, and a survey re-run carries
    the same blind spot the first survey had (polaris's Kotlin settings are
    unparsed on every pass). A receipt-proven module structure therefore
    survives the rewrite — only a receipt may replace a receipt.

    Plan 8 §3.9: the preservation read is STRICT. `read_build_requirements`'
    degrade-to-{} contract serves its eighteen read-only consumers, but this
    is a read-modify-WRITE: degrading a failed read to {} here would conclude
    "no proven structure exists" from "could not look" and overwrite a
    manifest that may hold one. A read that did not succeed, or a current
    host-authorized body that fails the v1 schema, refuses the whole write;
    only verified absence (None) may create the first survey revision.
    """
    from sag.agent.receipt_structure import preserve_receipt_structure

    try:
        incoming = validate_build_requirements_v1(data)
    except (TypeError, ValueError) as exc:
        logger.warning(f"Refusing invalid build requirements before persistence: {exc}")
        return False

    for _attempt in range(3):
        authority = evidence_publication_authority_for(orchestrator)
        publication_head = authority.latest_head(BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID)
        expected_publication = latest_publication_raw_sha256(
            orchestrator, BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
        )
        try:
            content = read_container_text(orchestrator, REQUIREMENTS_PATH, exact_bytes=True)
        except ContainerFileReadError as exc:
            logger.warning(
                f"Refusing to rewrite build requirements: the current manifest "
                f"could not be read ({exc})"
            )
            return False
        try:
            current: Dict[str, Any] = {}
            base_is_current = False
            if publication_head is None:
                # A reused container may carry a foreign-run manifest.  It is
                # an overwrite CAS base, never an input to the new survey.
                base_is_current = content is None
            elif publication_head.publication_state == "revoked":
                base_is_current = content is None
            elif content is not None:
                base_is_current = verify_latest_evidence_bytes(
                    orchestrator,
                    record_kind="build_requirements",
                    record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                    logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                    raw=content.encode("utf-8"),
                ).authorized
            if content is not None and base_is_current:
                try:
                    loaded = json.loads(content)
                    current = validate_build_requirements_v1(loaded)
                except (TypeError, ValueError):
                    # Host authorization does not make a malformed semantic
                    # body safe to derive from.  Refuse the whole RMW; a fresh
                    # survey may replace it only after the host head is revoked.
                    logger.warning("Refusing to rewrite invalid current build requirements")
                    return False
            candidate = preserve_receipt_structure(dict(incoming), current)
            candidate = validate_build_requirements_v1(candidate)
            body = json.dumps(candidate, indent=2, sort_keys=True)
            if publication_head is not None and not base_is_current:
                # A prior host failure may leave the independently derived
                # survey body on disk.  Complete that exact publication, but
                # never preserve fields from an unverified container body.
                independent = preserve_receipt_structure(dict(incoming), {})
                independent = validate_build_requirements_v1(independent)
                independent_body = json.dumps(independent, indent=2, sort_keys=True)
                if content == independent_body:
                    publication = publish_evidence_revision(
                        orchestrator,
                        record_kind="build_requirements",
                        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                        raw=independent_body.encode("utf-8"),
                        expected_previous_raw_sha256=expected_publication,
                    )
                    return publication.published
                logger.warning(
                    "Refusing to derive build requirements from bytes that are "
                    "not the current host publication"
                )
                return False
            result = compare_publish_container_text_atomic(
                orchestrator,
                REQUIREMENTS_PATH,
                body,
                expected_content=content,
                validate_json=True,
            )
            if result.persisted:
                raw = body.encode("utf-8")
                publication = publish_evidence_revision(
                    orchestrator,
                    record_kind="build_requirements",
                    record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                    logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                    raw=raw,
                    expected_previous_raw_sha256=expected_publication,
                )
                if publication.published:
                    return True
                logger.warning(
                    "Build requirements reached the container but host publication "
                    f"failed: {publication.status}"
                )
                return False
            if result.code == WRITE_COMPARE_CONFLICT:
                continue
            logger.warning(f"Failed to write build requirements: {result.code}")
            return False
        except Exception as exc:
            logger.warning(f"Failed to write build requirements: {exc}")
            return False
    logger.warning("Failed to write build requirements: compare retry exhausted")
    return False


def read_build_requirements(orchestrator) -> Dict[str, Any]:
    """Forensic compatibility read; never use it to authorize live control.

    This projection deliberately preserves the historical ``{} on failure``
    contract for display and migration callers.  Any live gate, dispatch, or
    derived-evidence writer must use :func:`read_live_build_requirements` so a
    container-authored mirror cannot masquerade as a host-published manifest.
    """
    try:
        content = read_container_text(orchestrator, REQUIREMENTS_PATH, exact_bytes=True)
        if content is None:
            return {}
        payload = json.loads(content)
        return dict(payload) if isinstance(payload, Mapping) else {}
    except Exception:
        return {}


def read_live_build_requirements(orchestrator) -> PublishedJsonObjectRead:
    """Read the one current host-authorized build-requirements revision.

    This boundary owns both halves of authority: exact current host bytes and
    the complete v1 semantic schema.  A host-published v0/stampless object is
    still forensic data, but cannot become live control authority.
    """

    return read_live_published_mutable_json_object(
        orchestrator,
        REQUIREMENTS_PATH,
        record_kind="build_requirements",
        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        validator=validate_build_requirements_v1,
        publication_binding=lambda _payload: EvidencePublicationBinding(
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        ),
    )


_JAVA_VERSION_RE = re.compile(r'version "(?:1\.)?(\d+)')

# Adoptium/Temurin apt repo for JDKs missing from the base image's Debian
# release (e.g. JDK 8 on bookworm). One-shot, idempotent.
_TEMURIN_SETUP = (
    "apt-get install -y wget apt-transport-https gnupg >/dev/null 2>&1; "
    "wget -qO- https://packages.adoptium.net/artifactory/api/gpg/key/public "
    "| gpg --dearmor -o /usr/share/keyrings/adoptium.gpg 2>/dev/null; "
    'echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] '
    "https://packages.adoptium.net/artifactory/deb "
    '$(. /etc/os-release && echo $VERSION_CODENAME) main" '
    "> /etc/apt/sources.list.d/adoptium.list && apt-get update"
)


# One probe answers both questions the activation check asks: which `java` a
# dispatch will actually run, and which major that is. Keeping them in one
# command means the check costs no round trip a build did not already pay.
_JAVA_RUNTIME_PROBE = "command -v java 2>/dev/null; java -version 2>&1"

# The named conflict for "the build is not running the runtime we registered".
JAVA_RUNTIME_CONFLICT = "java_runtime_not_activated"

# A registered runtime states its version however the registrar spelled it:
# "21", "21.0.9" and the legacy "1.8" are all the same JDK to a build. The
# comparison is between MAJORS, so a spelling difference is never a conflict.
_JAVA_MAJOR_RE = re.compile(r"^(?:1\.)?(\d+)")


def _java_major(version: Any) -> Optional[str]:
    """The JDK major a version string names, or None."""
    if version is None:
        return None
    match = _JAVA_MAJOR_RE.match(str(version).strip())
    return match.group(1) if match else None


def active_java_runtime(orchestrator) -> Dict[str, str]:
    """The `java` this container resolves right now.

    Absent facts are absent keys: a container that names no executable and
    prints no version banner returns `{}` rather than a dict of Nones.
    """
    result = orchestrator.execute_command(_JAVA_RUNTIME_PROBE)
    output = result.get("output") or ""
    runtime: Dict[str, str] = {}
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("/"):
            runtime["executable"] = candidate
            break
    match = _JAVA_VERSION_RE.search(output)
    if match:
        runtime["major"] = match.group(1)
    return runtime


def active_java_major(orchestrator) -> Optional[str]:
    """Major version of the currently active `java`, or None."""
    return active_java_runtime(orchestrator).get("major")


def registered_java_runtime(orchestrator) -> Dict[str, str]:
    """The java runtime the env overlay states is active, or `{}`."""
    try:
        from sag.runtime.env_overlay import EnvOverlayStore

        candidate = EnvOverlayStore(orchestrator).active_candidate("java") or {}
    except Exception as exc:
        logger.debug(f"registered java runtime unreadable: {exc}")
        return {}
    runtime: Dict[str, str] = {}
    executable = str(candidate.get("executable") or "").strip()
    if executable:
        runtime["executable"] = executable
    major = _java_major(candidate.get("version"))
    if major:
        runtime["major"] = major
    return runtime


def java_activation_conflict(
    registered: Dict[str, str],
    active: Dict[str, str],
) -> Optional[str]:
    """The mismatch between the registered java and the one a dispatch runs.

    Live polaris (`logs/session_20260727_065557_97847`): Java 21 was registered
    and the compile still resolved Java 17, and the run's only record of that
    was the model's own guess. A disagreement between the two is a fact about
    the environment; it is stated, never silently accepted.
    """
    if not registered or not active:
        return None
    registered_executable = registered.get("executable")
    active_executable = active.get("executable")
    if registered_executable and active_executable:
        if registered_executable != active_executable:
            return (
                f"[conflict] {JAVA_RUNTIME_CONFLICT}: the registered runtime is "
                f"{registered_executable} but this dispatch runs {active_executable}"
            )
    registered_major = registered.get("major")
    active_major = active.get("major")
    if registered_major and active_major and registered_major != active_major:
        return (
            f"[conflict] {JAVA_RUNTIME_CONFLICT}: the registered runtime is "
            f"Java {registered_major} but this dispatch runs Java {active_major}"
        )
    return None


def _register_overlay(orchestrator, java_home: str, version: str) -> bool:
    """Register the provisioned JDK in the shared env overlay (report-visible)."""
    try:
        from sag.runtime.env_overlay import EnvOverlayStore
        from sag.tools.internal.toolchain_manager import record_registered_runtime

        executable = f"{java_home}/bin/java"
        EnvOverlayStore(orchestrator).register(
            "java",
            executable,
            version=version,
            source="build_preflight",
            env={"JAVA_HOME": java_home},
            path_prepend=[f"{java_home}/bin"],
            activate=True,
        )
        # The overlay is what the dispatch shell sources; the registry is what
        # the dispatch's identity is taken over. A registration that reaches
        # only one of them cannot reach the build.
        record_registered_runtime(
            orchestrator,
            "java",
            executable,
            version=version,
            source="registered",
        )
        return True
    except Exception as exc:
        logger.warning(f"Pre-flight overlay registration failed: {exc}")
        return False


@dataclass
class PreflightOutcome:
    matched: bool
    active_version: Optional[str]
    required_version: Optional[str]
    provisioned: bool = False
    mismatch: bool = False
    narration: str = ""
    conflicts: Tuple[str, ...] = ()


class JdkPreflight:
    """Check-and-fix JDK guarantee. Never raises; never blocks (spec §1b)."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def run(self, required_version: Optional[str], source: str = "unknown") -> PreflightOutcome:
        try:
            return self._run(required_version, source)
        except Exception as exc:  # never let the pre-flight kill a build
            logger.warning(f"JDK pre-flight error (continuing): {exc}")
            return PreflightOutcome(True, None, required_version)

    def _run(self, required: Optional[str], source: str) -> PreflightOutcome:
        # The activation check needs something to compare AGAINST, so it reads
        # the overlay first and probes only when a runtime was registered. A
        # container with no registered java behaves exactly as before: no
        # probe, no narration, no conflict.
        registered = registered_java_runtime(self.orchestrator)
        if not required:
            if not registered:
                return PreflightOutcome(True, None, None)
            active_runtime = active_java_runtime(self.orchestrator)
            conflict = java_activation_conflict(registered, active_runtime)
            return PreflightOutcome(
                matched=True,
                active_version=active_runtime.get("major"),
                required_version=None,
                narration=conflict or "",
                conflicts=(JAVA_RUNTIME_CONFLICT,) if conflict else (),
            )

        active_runtime = active_java_runtime(self.orchestrator)
        active = active_runtime.get("major")
        if active == required:
            logger.debug(f"JDK pre-flight: active Java {active} matches requirement")
            # The version the manifest asked for is satisfied, which does not
            # by itself prove the build inherited the runtime that was
            # registered for it — the executables can still differ.
            conflict = java_activation_conflict(registered, active_runtime)
            return PreflightOutcome(
                matched=True,
                active_version=active,
                required_version=required,
                narration=conflict or "",
                conflicts=(JAVA_RUNTIME_CONFLICT,) if conflict else (),
            )

        header = (
            f"[pre-flight] Required: Java {required} (source: {source}). "
            f"Active: Java {active or 'unknown'}."
        )
        java_home = self._provision(required)
        if java_home:
            registered_ok = _register_overlay(self.orchestrator, java_home, required)
            if not registered_ok:
                return PreflightOutcome(
                    matched=False,
                    active_version=active,
                    required_version=required,
                    provisioned=False,
                    mismatch=True,
                    narration=(
                        f"{header}\n→ installed JDK {required} at {java_home}, but "
                        "overlay registration failed; the new runtime is not "
                        "durably active"
                    ),
                )
            # Registration is a write, not proof that the shell used for the
            # build reads it.  Probe through the same orchestrator path every
            # runner dispatch uses; only that environment may authorize the
            # retry.  This catches the live shape where Java 21 was registered
            # while the next build still resolved Java 17.
            post_runtime = active_java_runtime(self.orchestrator)
            post_major = post_runtime.get("major")
            registered_runtime = registered_java_runtime(self.orchestrator)
            activation_conflict = java_activation_conflict(
                registered_runtime,
                post_runtime,
            )
            if post_major != required or activation_conflict:
                observed = post_major or "unknown"
                reason = activation_conflict or (
                    f"the dispatch environment still reports Java {observed}"
                )
                return PreflightOutcome(
                    matched=False,
                    active_version=post_major or active,
                    required_version=required,
                    provisioned=False,
                    mismatch=True,
                    narration=(
                        f"{header}\n→ installed and registered JDK {required} at "
                        f"{java_home}, but the same-dispatch postcondition failed: {reason}"
                    ),
                    conflicts=(JAVA_RUNTIME_CONFLICT,) if activation_conflict else (),
                )
            return PreflightOutcome(
                matched=False,
                active_version=post_major,
                required_version=required,
                provisioned=True,
                narration=(
                    f"{header}\n→ installed JDK {required}, "
                    f"JAVA_HOME={java_home} (overlay registered; dispatch postcondition verified)"
                ),
            )
        return PreflightOutcome(
            matched=False,
            active_version=active,
            required_version=required,
            provisioned=False,
            mismatch=True,
            narration=(
                f"{header}\n→ could not provision JDK {required} "
                f"(apt + Temurin exhausted); continuing on Java {active or 'unknown'} — "
                "the verdict will record jdk_mismatch"
            ),
        )

    def _provision(self, version: str) -> Optional[str]:
        """apt -> Temurin ladder; returns JAVA_HOME on success, None on failure."""
        apt = self.orchestrator.execute_command(
            f"DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1; "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y openjdk-{version}-jdk"
        )
        if not apt.get("success"):
            self.orchestrator.execute_command(_TEMURIN_SETUP)
            temurin = self.orchestrator.execute_command(
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y temurin-{version}-jdk"
            )
            if not temurin.get("success"):
                return None
        home = self.orchestrator.execute_command(
            f"ls -d /usr/lib/jvm/java-{version}-openjdk-* "
            f"/usr/lib/jvm/temurin-{version}-jdk* 2>/dev/null | head -1"
        )
        java_home = (home.get("output") or "").strip().splitlines()
        java_home = java_home[0].strip() if java_home else ""
        if not java_home:
            return None
        self.orchestrator.execute_command(
            f"update-alternatives --install /usr/bin/java java {java_home}/bin/java 100 "
            f"&& update-alternatives --set java {java_home}/bin/java; "
            f"test -x {java_home}/bin/javac && "
            f"update-alternatives --install /usr/bin/javac javac {java_home}/bin/javac 100 "
            f"&& update-alternatives --set javac {java_home}/bin/javac"
        )
        return java_home


# Version-shaped build failures, in match priority. Each pattern captures the
# JDK major the build ACTUALLY needs (the honest, authoritative signal that
# static pom analysis cannot always see — spec §1c).
_VERSION_ERROR_PATTERNS = [
    # Maven Enforcer's RequireJavaVersion rule (real camel-quarkus wording):
    #   "Required Java version 17 is not met by current version 11.0.27"
    re.compile(
        r"Required\s+Java\s+version\s+(\d+)\s+is\s+not\s+met\s+by\s+current\s+version",
        re.IGNORECASE,
    ),
    # enforcer: "... allowed range [17,)" / "allowed version range [11,17)"
    re.compile(
        r"RequireJavaVersion.*?allowed(?:\s+version)?\s+range\s*\[?(\d+)", re.DOTALL | re.IGNORECASE
    ),
    # javac: "invalid target release: 21" / "release version 17 not supported"
    re.compile(r"invalid (?:target|source) release:?\s*(?:1\.)?(\d+)", re.IGNORECASE),
    re.compile(r"release version (\d+) not supported", re.IGNORECASE),
    # Gradle's own toolchain checks (live p7b-polaris):
    #   "Dependency requires at least JVM runtime version 21."
    #   "Build requires Java 21."
    # Both name the version the build needs, in the build's own words.
    re.compile(r"requires at least JVM runtime version\s+(\d+)", re.IGNORECASE),
    re.compile(r"(?:build|project|settings)\s+requires\s+Java\s+(\d+)", re.IGNORECASE),
]
_CLASS_FILE_VERSION = re.compile(r"class file version (\d+)\.")

# Old-Groovy vs new-JDK compiler transform incompatibility (live bigtop R3):
# bigtop's Groovy AST-transforming plugins emit this on JDK >= 11 while the
# maven modules are meant to build on JDK 8. Unlike the other signatures the
# error text names no target version — the remediation is the classic
# groovy-on-jdk8 downgrade, so this is a fixed "8" SENTINEL. classify_version_error
# is pure-text; the caller's `needed != active` gate makes it a no-op when the
# build is already on JDK 8 (needed == active), so the retry stays bounded and
# never fires spuriously.
_GROOVY_TRANSFORM_TYPERESOLVER = re.compile(
    r"wrong descriptors and a potential NullPointerException in TypeResolver"
    r"|Groovy:A transform used a generics containing ClassNode",
    re.IGNORECASE,
)


def classify_version_error(output: str) -> Optional[str]:
    """Extract the JDK major a failed build says it needs, else None."""
    if not output:
        return None
    for pattern in _VERSION_ERROR_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(1)
    match = _CLASS_FILE_VERSION.search(output)
    if match:
        # Class-file major 52 = JDK 8, 61 = JDK 17: major - 44.
        return str(int(match.group(1)) - 44)
    if _GROOVY_TRANSFORM_TYPERESOLVER.search(output):
        # Old-Groovy AST transform breaks on JDK >= 11: remediate to JDK 8.
        return "8"
    return None


# ---------------------------------------------------------------------------
# Python pre-flight (spec 2026-07-07 Component 2): same PreflightOutcome
# contract as JdkPreflight — check-and-fix, never raises, never blocks.
# ---------------------------------------------------------------------------

_PYTHON_VERSION_RE = re.compile(r"Python\s+(\d+\.\d+)")

_UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"
# uv lands in ~/.local/bin; every uv invocation prepends it so the install
# rung and the provisioning calls agree on PATH.
_UV_PATH = 'export PATH="$HOME/.local/bin:$PATH"'


def active_python_version(orchestrator) -> Optional[str]:
    """major.minor of the currently active `python3`, or None."""
    result = orchestrator.execute_command("python3 --version 2>&1")
    match = _PYTHON_VERSION_RE.search(result.get("output") or "")
    return match.group(1) if match else None


def _register_python_overlay(orchestrator, venv: str, version: str) -> bool:
    """Register the provisioned interpreter/venv in the shared env overlay."""
    try:
        from sag.runtime.env_overlay import EnvOverlayStore

        EnvOverlayStore(orchestrator).register(
            "python",
            f"{venv}/bin/python",
            version=version,
            source="python_preflight",
            env={"VIRTUAL_ENV": venv},
            path_prepend=[f"{venv}/bin"],
            activate=True,
        )
        return True
    except Exception as exc:
        logger.warning(f"Python pre-flight overlay registration failed: {exc}")
        return False


class PythonPreflight:
    """Check-and-fix Python interpreter guarantee (uv -> apt ladder).

    Mirrors JdkPreflight: satisfied (or no) requirement -> no-op; mismatch ->
    provision + narrate; ladder exhausted -> mismatch=True, which the verifier
    maps to the python_version_mismatch conflict. Never raises, never blocks.
    """

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def run(
        self,
        required_version: Optional[str],
        constraint: Optional[str] = None,
        source: str = "unknown",
        venv_path: Optional[str] = None,
    ) -> PreflightOutcome:
        try:
            return self._run(required_version, constraint, source, venv_path)
        except Exception as exc:  # never let the pre-flight kill a build
            logger.warning(f"Python pre-flight error (continuing): {exc}")
            return PreflightOutcome(True, None, required_version)

    def _run(
        self,
        required: Optional[str],
        constraint: Optional[str],
        source: str,
        venv_path: Optional[str],
    ) -> PreflightOutcome:
        if not required:
            return PreflightOutcome(True, None, None)
        active = active_python_version(self.orchestrator)
        if active == required or self._constraint_satisfied(active, constraint):
            logger.debug(f"Python pre-flight: active {active} satisfies requirement")
            # A version MATCH is necessary but not sufficient: the live TVM run
            # (session 20260713_014403_27874) had system python 3.12.3 satisfy
            # '>=3.10' yet ship NO ensurepip, so it could not create a working
            # venv and the broken toolchain sailed through to fail later in
            # deps. Verify FUNCTION, not just version — cheaply (module presence
            # probe, no side effects), and repair via the SAME rungs a mismatch
            # would use. Never flips the (correct) version match to a mismatch.
            if self._can_create_venvs():
                return PreflightOutcome(True, active, required)
            return self._repair_matched_but_broken(
                active,
                required,
                source,
                venv_path or "/workspace/.venv",
            )

        header = (
            f"[pre-flight] Required: Python {required} (source: {source}). "
            f"Active: {active or 'unknown'}."
        )
        venv = venv_path or "/workspace/.venv"
        rung = self._provision(required, venv)
        if rung:
            pip_note = self._ensure_venv_pip(venv, required)
            registered = _register_python_overlay(self.orchestrator, venv, required)
            if not registered:
                return PreflightOutcome(
                    matched=False,
                    active_version=active,
                    required_version=required,
                    provisioned=False,
                    mismatch=True,
                    narration=(
                        f"{header}\n→ {rung}-provisioned {required} at {venv}, "
                        "but overlay registration failed; the interpreter is "
                        "not durably active"
                    ),
                )
            narration = (
                f"{header}\n→ {rung}-provisioned {required}, "
                f"venv at {venv} (overlay registered)"
            )
            if pip_note:
                narration += f"\n{pip_note}"
            return PreflightOutcome(
                matched=False,
                active_version=active,
                required_version=required,
                provisioned=True,
                narration=narration,
            )
        return PreflightOutcome(
            matched=False,
            active_version=active,
            required_version=required,
            provisioned=False,
            mismatch=True,
            narration=(
                f"{header}\n→ could not provision Python {required} "
                f"(uv + apt exhausted); continuing on Python {active or 'unknown'} — "
                "the verdict will record python_version_mismatch"
            ),
        )

    @staticmethod
    def _constraint_satisfied(active: Optional[str], constraint: Optional[str]) -> bool:
        """The active interpreter may satisfy the raw constraint without being
        the resolved newest (e.g. 3.12 for '>=3.9'): the pre-flight guarantees
        requirements, it does not chase the newest interpreter."""
        if not active or not constraint:
            return False
        return resolve_python_version(constraint, [active]) == active

    def _can_create_venvs(self) -> bool:
        """Cheaply verify the ACTIVE interpreter can create a working venv:
        probe for the ``ensurepip`` module (Debian splits it out, so a
        version-matching python can still yield a pip-less venv — the live TVM
        trap). ``python3 -m ensurepip --version`` is a pure module-presence
        check with no side effects; the ``importlib.util.find_spec`` fallback
        covers interpreters whose ensurepip lacks ``--version``.

        Cached PER RUN on the shared orchestrator: multiple tools each build
        their own PythonPreflight in one run, but the probe touches the
        container exactly once (the interpreter does not change mid-run)."""
        cached = getattr(self.orchestrator, "_python_ensurepip_ok", None)
        if cached is not None:
            return bool(cached)
        probe = self.orchestrator.execute_command(
            "python3 -m ensurepip --version 2>/dev/null "
            '|| python3 -c "import importlib.util,sys; '
            "sys.exit(0 if importlib.util.find_spec('ensurepip') else 1)\""
        )
        ok = bool(probe.get("success"))
        try:
            self.orchestrator._python_ensurepip_ok = ok
        except Exception:  # a read-only/exotic orchestrator: skip the cache
            pass
        return ok

    def _repair_matched_but_broken(
        self,
        active: Optional[str],
        required: str,
        source: str,
        venv: str,
    ) -> PreflightOutcome:
        """Version matched but the interpreter cannot create venvs (no
        ensurepip). Narrate the honest defect and run the SAME apt/uv repair
        rungs a mismatch uses (shared ``ensure_venv_pip`` via
        ``_ensure_venv_pip`` — never duplicated). Provisioning failure keeps the
        existing degrade semantics: narrated, never a hard block, and — because
        the VERSION genuinely matched — never a version mismatch flag."""
        header = (
            f"[pre-flight] Python {active} matches the constraint but cannot "
            f"create venvs (no ensurepip) — repairing (source: {source})"
        )
        pip_note = self._ensure_venv_pip(venv, required)
        # A successful repair means later tools in this run need neither probe
        # nor repair: flip the per-run cache to healthy. A still-broken pip
        # leaves the cache False so the honest narration recurs (never silent).
        if pip_note is None or "still missing" not in pip_note:
            try:
                self.orchestrator._python_ensurepip_ok = True
            except Exception:
                pass
        registered = _register_python_overlay(self.orchestrator, venv, required)
        narration = header
        if pip_note:
            narration += f"\n{pip_note}"
        if not registered:
            narration += (
                "\n→ overlay registration failed; the repaired interpreter is " "not durably active"
            )
            return PreflightOutcome(
                matched=False,
                active_version=active,
                required_version=required,
                provisioned=False,
                mismatch=True,
                narration=narration,
            )
        return PreflightOutcome(
            matched=True,
            active_version=active,
            required_version=required,
            provisioned=True,
            mismatch=False,
            narration=narration,
        )

    def _provision(self, version: str, venv: str) -> Optional[str]:
        """uv -> apt ladder; returns the rung name on success, None on failure."""
        if self._uv_available() and self._uv_provision(version, venv):
            return "uv"
        if self._apt_provision(version, venv):
            return "apt"
        return None

    def _uv_available(self) -> bool:
        probe = self.orchestrator.execute_command(f"{_UV_PATH}; command -v uv")
        if probe.get("success"):
            return True
        install = self.orchestrator.execute_command(_UV_INSTALL)
        if not install.get("success"):
            return False
        probe = self.orchestrator.execute_command(f"{_UV_PATH}; command -v uv")
        return bool(probe.get("success"))

    def _uv_provision(self, version: str, venv: str) -> bool:
        install = self.orchestrator.execute_command(f"{_UV_PATH}; uv python install {version}")
        if not install.get("success"):
            return False
        # --seed: a plain `uv venv` ships NO pip inside the venv (bug #12),
        # which broke every `{venv}/bin/python -m pip ...` rung downstream.
        made = self.orchestrator.execute_command(
            f"{_UV_PATH}; uv venv --seed --python {version} {venv}"
        )
        return bool(made.get("success"))

    def _ensure_venv_pip(self, venv: str, version: Optional[str] = None) -> Optional[str]:
        """Verify pip exists inside the fresh venv; repair via the shared
        python_env.ensure_venv_pip ladder (bug #12 / bug #13 defect 1, live
        TVM failure): probe -> ensurepip -> recreate -> apt python3-venv ->
        uv venv --seed -> re-probe between each. Never blocks: a still-missing
        pip returns a narration line naming every rung and the run continues."""
        repair = ensure_venv_pip(self.orchestrator, venv, python_version=version)
        action = repair.get("action")
        ladder = repair.get("ladder") or []
        if repair.get("ok"):
            if action is None:
                return None
            phrase = _REPAIR_ACTION_PHRASE.get(action, "repaired")
            return f"→ venv had no pip; {phrase} at {venv}"
        tried = "; ".join(ladder) if ladder else "ensurepip and recreation"
        return (
            f"→ pip still missing in {venv} after the repair ladder "
            f"(tried: {tried}) — pip-based install commands will fail; "
            f"continuing (never blocks)"
        )

    def _apt_provision(self, version: str, venv: str) -> bool:
        apt = self.orchestrator.execute_command(
            f"DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1; "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y "
            f"python{version}-venv python{version}"
        )
        if not apt.get("success"):
            return False
        made = self.orchestrator.execute_command(f"python{version} -m venv {venv}")
        return bool(made.get("success"))


# pip's version-shaped rejections, in match priority. Each captures the
# Requires-Python constraint; the interpreter to provision is the newest
# supported CPython satisfying it (spec Component 1 policy, via
# resolve_python_version). A bare SyntaxError is deliberately NOT classified:
# it cannot distinguish "needs a newer interpreter" from "broken source".
_PIP_REQUIRES_PYTHON_PATTERNS = [
    # "requires a different Python: 3.8.10 not in '>=3.10'"
    re.compile(r"requires a different Python:\s*[\d.]+\s+not in\s+['\"]([^'\"]+)['\"]"),
    # "... 24.2 Requires-Python >=3.10; ..." / metadata "Requires-Python: >=3.10"
    re.compile(r"Requires-Python[:\s]\s*([^;\n]+)"),
]


def classify_python_version_error(output: str) -> Optional[str]:
    """Extract the Python major.minor a failed pip install says it needs.

    Only pip's explicit Requires-Python rejections are actionable; anything
    non-version-shaped (including bare SyntaxErrors) returns None so the
    bounded retry never fires on ambiguity."""
    if not output:
        return None
    for pattern in _PIP_REQUIRES_PYTHON_PATTERNS:
        match = pattern.search(output)
        if match:
            return resolve_python_version(match.group(1).strip())
    return None
