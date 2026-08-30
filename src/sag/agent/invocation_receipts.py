"""Invocation receipts for runner calls (Plan 5 Stage B P0-A; Plan 6 Stage 0).

Ground-truth review 2026-07-26 (§"Evidence is snapshot-global instead of
receipt-scoped"): the validator scanned the project tree after several
invocations and treated every matching XML as current evidence. It could not
say which invocation wrote which report, so auxiliary reports and stale
retries entered the primary rollup (Bigtop's 54/54).

A receipt makes that answerable. Every physical maven/gradle/pytest runner
call brackets itself with a content-hash snapshot of the report XMLs under
its own scan roots and persists ONE atomic JSON file:

    /workspace/.setup_agent/invocation_receipts/<receipt_id>.json

Schema v2 (Plan 6 Stage 0, spec §C4) adds the binding facts the contract loop
needs before it can bind anything: the target sha, the survey/config pins the
run was decided on, the domain the invocation belongs to, the cwd it actually
used, its compliance class, the runner's own toolchain fingerprint, a content
hash of the output, and a bounded per-testcase outcome list parsed from THIS
invocation's report delta (review binding note (b)). Every schema-v1 key keeps
its exact name and shape — the Plan 5 consumers read v2 receipts unchanged —
and every v2 fact is absent when unknown, never null and never defaulted.

Gradle test evidence (2026-08-30 study) rides that same rule as two further
optional sections on the SAME schema version: `gradle_suite_summaries`, the
complete per (project, task-dir) totals summed from the reports' own
`<testsuite>` roots, and `gradle_row_disclosure`, which states what the bounded
identity harvest beside them kept and what it dropped. They are separate
measurements from `testcase_execution_rows` and are named separately so no
reader can mistake a bounded sample for a project-wide rollup.

When the authorizing contract names an effective JDK, the receipt copies that
binding and its scoped provenance verbatim. A detached dispatch carries it
through the job obligation so settlement cannot reconstruct runtime authority
from a newer survey.

The receipt is finalized ONCE and never rewritten (spec §C4). Semantic
classification — "this exit 0 compiled nothing" — is an append-only
`ReceiptAssessment` in `evidence_assessments`, not an edit of this file.

Persistence is best effort HERE: this module never raises and never blocks
the command result the model is waiting for. A failed write is reported as a
fact (`receipt_persisted: false` in ToolResult metadata); turning that fact
into a closure failure is the phase gate's business, not the runner's.
"""

import hashlib
import html
import itertools
import json
import posixpath
import re
import shlex
import threading
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.receipt_structure import promote_structure
from sag.agent.receipt_test_rows import (
    TestcaseRowContractError,
    diagnostic_testcase_outcomes,
    read_delta_testcase_rows,
    read_gradle_project_map,
    seal_testcase_execution_rows,
    validate_testcase_execution_row,
)
from sag.runtime.container_io import resolve_control_execute
from sag.utils.container_io import (
    WRITE_COMPARE_CONFLICT,
    WRITE_INVALID_ARGUMENTS,
    ContainerWriteResult,
    compare_publish_container_text_atomic,
)

RECEIPT_SCHEMA_VERSION = 2
RECEIPT_DIR = "/workspace/.setup_agent/invocation_receipts"
RECEIPT_ID_COLLISION = "receipt_id_collision"
HOST_PUBLICATION_FAILED = "host_publication_failed"
RECEIPT_MAX_CANONICAL_BYTES = 16 * 1024 * 1024
RECEIPT_MAX_RAW_BYTES = 20 * 1024 * 1024
RECEIPT_TEXT_MAX_BYTES = 4096
RECEIPT_ARGV_MAX_BYTES = 1 << 20
RECEIPT_SEQUENCE_MAX_ITEMS = 256
# `module_outcomes` is the ONE receipt sequence whose length is set by the
# project, not by us: it is the build system's own reactor summary. Camel's
# reactor prints 652 rows, and the generic 256 bound refused them — on the
# DETACHED path only, because the synchronous monitor hands back a clipped log
# that parses to 31. A reactor bound has to be a reactor's size. 4096 rows
# serialize to roughly 250 KB against a 16 MB canonical budget, so nothing
# downstream of it is threatened.
RECEIPT_MODULE_OUTCOMES_MAX_ITEMS = 4096
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# receipt content can ever collide with it.
RECEIPT_HEREDOC = "SAGRECEIPT"

# Review binding note (b): the per-testcase list is bounded, and a truncation
# is recorded rather than silently dropped.
TESTCASE_OUTCOME_CAP = 50
# Transport bounds for the outcome parse. A surefire XML can carry megabytes of
# system-out, so the container returns only the report's TAGS (one round trip,
# `grep -oE`), never the report bodies.
TESTCASE_FILE_CAP = 50
TESTCASE_TAG_CAP = 400
TESTCASE_PARSE_CAP = 500
SKIP_REASON_MAX_CHARS = 200
# Gradle test evidence (evidence study 2026-08-30). A Gradle reactor writes its
# reports per (project, TASK dir) — geode proves `test-results/distributedTest`
# exists alongside `test-results/test` — so the summary unit is the pair, not
# the module. 256 pairs is generous against the measured shape (kafka 19
# modules, geode 27) and is a safety bound, not an expected one; exceeding it
# truncates red-bearing-first and records the drop.
GRADLE_SUITE_SUMMARY_CAP = 256
# The per-testcase identity rows are the receipt's only unbounded list. kafka's
# one measured run left 27,219 executed tests; a sealed row canonicalizes to
# roughly a kilobyte, so an uncapped envelope would blow the 16 MB canonical
# budget and VOID THE WHOLE RECEIPT at write time (the `_attach` gate checks
# per-field validity, not the byte budget). 2048 rows is ~2 MB — room for the
# argv, the reactor list and the delta beside it — and no measured project has
# more than a handful of reds, so the red set always survives the cap.
GRADLE_TESTCASE_ROW_CAP = 2048
# The one transport these rows may claim: XML this engine parsed in-container
# from bytes hash-bound to the receipt's own report delta.
GRADLE_ROWS_SOURCE = "gradle_xml"
_GRADLE_SUITE_COUNT_FIELDS = ("tests", "failures", "errors", "skipped")
_GRADLE_SUITE_FIELDS = frozenset({"module", "task", "xml_files", *_GRADLE_SUITE_COUNT_FIELDS})
_GRADLE_RED_OUTCOMES = frozenset({"failed", "error"})
_GRADLE_GREEN_OUTCOMES = frozenset({"passed", "skipped"})
# The line a bounded per-report tag read prints before each report's tags, so
# one round trip over many reports still attributes every tag to the file it
# came from. It carries that file's digest, because a row is only evidence of
# THIS invocation when the bytes it was read from are the bytes the receipt's
# own report delta claims.
REPORT_TAG_MARKER = "###sag-report###"
_REPORT_TAG_HEADER_RE = re.compile(r"^([0-9a-f]{64})\s+(\S.*)$")
# The reasons an ENGINE harvest may state for a field it could not carry. A
# declared omission is still engine-written evidence, so its vocabulary is
# closed: a runner may say which measurement failed, never write free prose
# into the receipt.
GRADLE_NO_TEST_REPORTS = "gradle_no_test_reports_on_disk"
GRADLE_SUITE_TOTALS_UNREADABLE = "gradle_suite_totals_unreadable"
# Reports exist on disk and NONE of them is this invocation's: every one is
# byte-identical to what an earlier attempt left, and `report_delta` already
# refused to claim them. Summing them would attribute another dispatch's tests
# to this receipt, so this dispatch states what it can prove — that it wrote no
# report of its own — instead of borrowing the tree's.
GRADLE_NO_CLAIMED_TEST_REPORTS = "gradle_no_claimed_test_reports"
# The one tag round trip did not complete. The totals still stand (they came
# from a different read), but there IS no row sample, and a disclosure that
# described one would be describing a sample the receipt does not carry.
GRADLE_ROW_SAMPLE_UNREADABLE = "gradle_row_sample_unreadable"
DECLARED_OMISSION_REASONS = frozenset(
    {
        GRADLE_NO_TEST_REPORTS,
        GRADLE_NO_CLAIMED_TEST_REPORTS,
        GRADLE_SUITE_TOTALS_UNREADABLE,
        GRADLE_ROW_SAMPLE_UNREADABLE,
    }
)
# Not an omission reason: a scan that never ran proved nothing, and an
# unanswered probe must stay an absent key rather than becoming a receipt-level
# claim that the build produced no test evidence.
GRADLE_DISCOVERY_INCOMPLETE = "gradle_report_discovery_incomplete"
# Python setup/build/compile observations are part of the immutable receipt,
# never ToolResult prose.  Refuse an observation that cannot fit this exact
# canonical budget: truncating a package list, artifact list, or source/PYC
# basis would change the claim while leaving a superficially valid receipt.
PRODUCER_OBSERVATIONS_SCHEMA_VERSION = 1
PRODUCER_OBSERVATIONS_MAX_CANONICAL_BYTES = 32 * 1024
PRODUCER_OBSERVATION_STEP_CAP = 32
PRODUCER_OBSERVATION_TARGET_CAP = 64
PRODUCER_OBSERVATION_ARTIFACT_CAP = 32
PRODUCER_OBSERVATION_SAMPLE_CAP = 20
PRODUCER_OBSERVATION_PATH_MAX_CHARS = 1024
# Marker that keeps an absent executable path from sliding into the version
# slot of a one-round-trip toolchain probe.
TOOLCHAIN_MARKER = "SAGTOOLCHAIN"
VERSION_LINE_MAX_CHARS = 200
# How each runner states its own version. `python -V`, `mvn -v`, `gradle -v`.
VERSION_FLAGS = {"maven": "-v", "gradle": "-v", "python": "-V"}
RUNNER_DEFAULTS = {"maven": "mvn", "gradle": "gradle", "python": "python"}

# A sha the container actually printed, not whatever text a broken probe
# echoed back (a fake/failing container answers every command with log text).
_OBJECT_NAME_RE = re.compile(r"[0-9a-f]{7,64}")
# JUnit report tags the outcome parse understands, in the container's own
# `grep -oE` token order.
TESTCASE_TAG_PATTERN = "<(testcase|skipped|failure|error)[^>]*>|</testcase>"
_TESTCASE_TAG_RE = re.compile(r"<(/?)(testcase|skipped|failure|error)\b([^>]*)>")
_STATUS_PRIORITY = {"error": 0, "failed": 1, "skipped": 2, "passed": 3}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_RECEIPT_SEQUENCE_RE = re.compile(r".+-([0-9]+)")
_PYTHON_IMPORT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_PRODUCER_PROJECT_ROLES = {
    "dependency_install",
    "pip_check",
    "import_probe",
    "wheel_build",
    "compileall",
    "compile_metrics",
}
_PRODUCER_MECHANICAL_ROLES = {"venv_create", "build_prerequisite"}

_RECEIPT_V2_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "run_id",
        "tool",
        "requested_action",
        "effective_action",
        "argv",
        "working_directory",
        "actual_cwd",
        "outcome",
        "report_delta",
    }
)
_RECEIPT_V2_OPTIONAL_FIELDS = frozenset(
    {
        "exit_code",
        "lifecycle_state",
        "termination_reason",
        "contract_id",
        "contract_hash",
        "execution_binding",
        "compliance",
        "target_sha",
        "survey_fingerprint",
        "config_fingerprint",
        "document_map_fingerprint",
        "fact_epoch",
        "domain_id",
        "toolchain_fingerprint",
        "effective_jdk",
        "output_content_hash",
        "producer_sequence",
        "producer_observations",
        "producer_observations_sha256",
        "testcase_outcomes",
        "testcase_execution_rows",
        "gradle_suite_summaries",
        "gradle_row_disclosure",
        "capability_observations",
        "module_outcomes",
        "excluded_claimed_paths",
        "evidence_omissions",
    }
)
_RECEIPT_V2_FIELDS = _RECEIPT_V2_REQUIRED_FIELDS | _RECEIPT_V2_OPTIONAL_FIELDS
# The OBSERVABILITY fields. Each one describes the dispatch; none of them IS
# the dispatch. When a producer hands one over in a shape the receipt cannot
# carry, `build_receipt` drops that field alone and records WHY here, rather
# than letting the write-time validator void the exit code, the argv, the
# contract binding and the report delta along with it.
_RECEIPT_OMITTABLE_EVIDENCE_FIELDS = frozenset(
    {
        "testcase_outcomes",
        "testcase_execution_rows",
        "gradle_suite_summaries",
        "gradle_row_disclosure",
        "capability_observations",
        "module_outcomes",
    }
)
_RECEIPT_V1_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "tool",
        "requested_action",
        "effective_action",
        "argv",
        "working_directory",
        "exit_code",
        "outcome",
        "report_delta",
    }
)

# What makes an XML a TEST REPORT. Mirrors the in-container `is_report_file`
# of physical_validator (surefire / failsafe / gradle test-results / pytest
# junit); the two must agree or a receipt would claim files the validator
# never scans — or miss files it does.
REPORT_PATH_MARKERS = (
    "/target/surefire-reports/",
    "/target/failsafe-reports/",
    "/build/test-results/",
    "/.setup_agent/pytest-reports/",
)

_PROCESS_NONCE = uuid.uuid4().hex[:12]
_PROCESS_RUN_ID = f"process-{uuid.uuid4().hex}"
_ACTIVE_RUN_ID: ContextVar[str] = ContextVar("sag_receipt_run_id", default=_PROCESS_RUN_ID)
_SEQUENCE = itertools.count(1)
_SEQUENCE_LOCK = threading.Lock()


def next_sequence() -> int:
    """Process-global monotonic sequence — receipt ids cannot collide."""
    with _SEQUENCE_LOCK:
        return next(_SEQUENCE)


def next_receipt_id(scope: str, attempt: Any) -> str:
    """Return a process-unique id whose final component preserves ordering.

    A plain process-local counter reused ``...-0001`` after every SAG restart,
    so a long-lived container could silently replace a receipt from an older
    run.  The startup nonce prevents that collision while the final numeric
    component remains the invocation order consumed by row aggregation.
    """

    return (
        f"inv-{_slug(scope) or 'runner'}-{_slug(attempt) or '1'}-"
        f"{_PROCESS_NONCE}-{next_sequence():04d}"
    )


def set_active_receipt_run_id(run_id: str) -> str:
    """Bind future receipts in this execution context to one durable run id."""

    normalized = str(run_id or "").strip()
    if not normalized:
        raise ValueError("receipt run id must be non-empty")
    _ACTIVE_RUN_ID.set(normalized)
    return normalized


def active_receipt_run_id() -> str:
    """The run identity inherited by synchronous and copied task contexts."""

    return _ACTIVE_RUN_ID.get()


def _producer_canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("producer observations must be canonical JSON") from exc


def producer_observations_sha256(value: Any) -> str:
    """SHA-256 of the exact canonical JSON domain used by receipt validation."""

    return hashlib.sha256(_producer_canonical_bytes(value)).hexdigest()


def producer_sequence_from_receipt_id(value: Any) -> Optional[int]:
    """Return the explicit monotonic suffix of a safe producer receipt id.

    Production ids come from :func:`next_receipt_id` and end in the process
    sequence.  Tests and settlement fixtures may use a shorter prefix, but a
    producer record still has to state the same positive decimal suffix in its
    typed ``producer_sequence`` field.  A non-producer receipt need not carry
    one.
    """

    receipt_id = str(value or "")
    if _SAFE_RECEIPT_ID_RE.fullmatch(receipt_id) is None:
        return None
    match = _RECEIPT_SEQUENCE_RE.fullmatch(receipt_id)
    if match is None:
        return None
    try:
        sequence = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return sequence if sequence > 0 else None


def _producer_mapping(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _producer_exact_keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    field: str,
) -> None:
    required_keys = set(required)
    allowed = required_keys | set(optional)
    keys = set(value)
    if not required_keys.issubset(keys) or not keys.issubset(allowed):
        raise ValueError(f"{field} has missing or untyped fields")


def _producer_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    text = value.strip()
    if not text or len(text) > maximum or "\x00" in text or "\n" in text:
        raise ValueError(f"{field} is blank or out of bounds")
    return text


def _producer_digest(value: Any, field: str) -> str:
    text = _producer_text(value, field=field, maximum=64).lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text


def _producer_count(value: Any, field: str, *, maximum: int = 10_000_000) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise ValueError(f"{field} must be a bounded non-negative integer")
    return value


def _producer_relative_path(value: Any, field: str, *, allow_dot: bool = False) -> str:
    text = _producer_text(
        value,
        field=field,
        maximum=PRODUCER_OBSERVATION_PATH_MAX_CHARS,
    )
    normalized = posixpath.normpath(text)
    if (
        normalized.startswith("/")
        or normalized == ".."
        or normalized.startswith("../")
        or (normalized == "." and not allow_dot)
    ):
        raise ValueError(f"{field} must stay relative to the contracted cwd")
    return normalized


def _validate_producer_step(
    raw: Any,
    *,
    roles: set[str],
    field: str,
) -> Dict[str, Any]:
    step = _producer_mapping(raw, field)
    _producer_exact_keys(
        step,
        required=("ordinal", "role", "argv_sha256", "outcome"),
        optional=(
            "exit_code",
            "output_sha256",
            "lifecycle_state",
            "termination_reason",
            "semantic_failure",
        ),
        field=field,
    )
    ordinal = _producer_count(step["ordinal"], f"{field}.ordinal", maximum=10_000)
    if ordinal <= 0:
        raise ValueError(f"{field}.ordinal must be positive")
    role = _producer_text(step["role"], field=f"{field}.role", maximum=64)
    if role not in roles:
        raise ValueError(f"{field}.role is not allowlisted")
    outcome = _producer_text(step["outcome"], field=f"{field}.outcome", maximum=16)
    if outcome not in {"completed", "failed"}:
        raise ValueError(f"{field}.outcome is not terminal")
    normalized: Dict[str, Any] = {
        "ordinal": ordinal,
        "role": role,
        "argv_sha256": _producer_digest(step["argv_sha256"], f"{field}.argv_sha256"),
        "outcome": outcome,
    }
    if "exit_code" in step:
        exit_code = step["exit_code"]
        if type(exit_code) is not int:
            raise ValueError(f"{field}.exit_code must be an integer")
        normalized["exit_code"] = exit_code
    if "output_sha256" in step:
        normalized["output_sha256"] = _producer_digest(
            step["output_sha256"], f"{field}.output_sha256"
        )
    for key in ("lifecycle_state", "termination_reason"):
        if key in step:
            normalized[key] = _producer_text(step[key], field=f"{field}.{key}", maximum=128)
    if "semantic_failure" in step:
        semantic_failure = _producer_text(
            step["semantic_failure"],
            field=f"{field}.semantic_failure",
            maximum=64,
        )
        if (
            role != "dependency_install"
            or outcome != "failed"
            or semantic_failure != "install_error_signature"
        ):
            raise ValueError(f"{field}.semantic_failure is not valid for this role/outcome")
        normalized["semantic_failure"] = semantic_failure
    return normalized


def _validate_setup_observation(raw: Any) -> Dict[str, Any]:
    setup = _producer_mapping(raw, "producer_observations.setup")
    _producer_exact_keys(
        setup,
        required=("venv", "installer", "install_outcome"),
        optional=("pip_check", "imports"),
        field="producer_observations.setup",
    )
    venv = _producer_text(
        setup["venv"],
        field="producer_observations.setup.venv",
        maximum=PRODUCER_OBSERVATION_PATH_MAX_CHARS,
    )
    if not venv.startswith("/"):
        raise ValueError("producer_observations.setup.venv must be absolute")
    installer = _producer_text(
        setup["installer"], field="producer_observations.setup.installer", maximum=64
    )
    install_outcome = _producer_text(
        setup["install_outcome"],
        field="producer_observations.setup.install_outcome",
        maximum=16,
    )
    if install_outcome not in {"success", "failed"}:
        raise ValueError("producer_observations.setup.install_outcome is invalid")
    normalized: Dict[str, Any] = {
        "venv": posixpath.normpath(venv),
        "installer": installer,
        "install_outcome": install_outcome,
    }
    if "pip_check" in setup:
        pip_check = _producer_mapping(setup["pip_check"], "producer_observations.setup.pip_check")
        _producer_exact_keys(
            pip_check,
            required=("status", "exit_code", "output_sha256"),
            field="producer_observations.setup.pip_check",
        )
        status = _producer_text(
            pip_check["status"],
            field="producer_observations.setup.pip_check.status",
            maximum=16,
        )
        if status not in {"clean", "broken"}:
            raise ValueError("producer_observations.setup.pip_check.status is invalid")
        exit_code = pip_check["exit_code"]
        if type(exit_code) is not int or (status == "clean" and exit_code != 0):
            raise ValueError("producer_observations.setup.pip_check exit disagrees")
        normalized["pip_check"] = {
            "status": status,
            "exit_code": exit_code,
            "output_sha256": _producer_digest(
                pip_check["output_sha256"],
                "producer_observations.setup.pip_check.output_sha256",
            ),
        }
    if "imports" in setup:
        imports = _producer_mapping(setup["imports"], "producer_observations.setup.imports")
        status = imports.get("status")
        if status == "unavailable":
            _producer_exact_keys(
                imports,
                required=("status", "reason_code"),
                field="producer_observations.setup.imports",
            )
            reason = _producer_text(
                imports["reason_code"],
                field="producer_observations.setup.imports.reason_code",
                maximum=64,
            )
            if reason not in {
                "no_survey_targets",
                "invalid_survey_targets",
                "probe_failed",
                "invalid_probe_output",
            }:
                raise ValueError("producer_observations.setup.imports.reason_code is invalid")
            normalized["imports"] = {"status": "unavailable", "reason_code": reason}
        else:
            _producer_exact_keys(
                imports,
                required=(
                    "status",
                    "targets",
                    "targets_sha256",
                    "target_count",
                    "importable_count",
                    "failed_count",
                    "failures",
                    "output_sha256",
                ),
                optional=("truncated",),
                field="producer_observations.setup.imports",
            )
            if status != "complete":
                raise ValueError("producer_observations.setup.imports.status is invalid")
            targets = imports["targets"]
            if (
                not isinstance(targets, list)
                or not 0 < len(targets) <= PRODUCER_OBSERVATION_TARGET_CAP
            ):
                raise ValueError("producer_observations.setup.imports.targets is out of bounds")
            normalized_targets = [
                _producer_text(target, field="python import target", maximum=256)
                for target in targets
            ]
            if len(set(normalized_targets)) != len(normalized_targets) or any(
                not _PYTHON_IMPORT_RE.fullmatch(target) for target in normalized_targets
            ):
                raise ValueError("producer_observations.setup.imports.targets is invalid")
            if imports["targets_sha256"] != producer_observations_sha256(normalized_targets):
                raise ValueError("producer_observations.setup.imports target hash disagrees")
            target_count = _producer_count(
                imports["target_count"], "python import target_count", maximum=64
            )
            imported = _producer_count(
                imports["importable_count"], "python import importable_count", maximum=64
            )
            failed = _producer_count(
                imports["failed_count"], "python import failed_count", maximum=64
            )
            if target_count != len(normalized_targets) or imported + failed != target_count:
                raise ValueError("producer_observations.setup.import counts disagree")
            failures = imports["failures"]
            if not isinstance(failures, list) or len(failures) > PRODUCER_OBSERVATION_SAMPLE_CAP:
                raise ValueError("producer_observations.setup.import failures is out of bounds")
            normalized_failures = []
            for index, raw_failure in enumerate(failures):
                failure = _producer_mapping(raw_failure, f"python import failure {index}")
                _producer_exact_keys(
                    failure,
                    required=("target", "error_type"),
                    field=f"python import failure {index}",
                )
                target = _producer_text(
                    failure["target"], field="python import failure target", maximum=256
                )
                if target not in normalized_targets:
                    raise ValueError("python import failure target was not surveyed")
                normalized_failures.append(
                    {
                        "target": target,
                        "error_type": _producer_text(
                            failure["error_type"],
                            field="python import error type",
                            maximum=128,
                        ),
                    }
                )
            truncated = imports.get("truncated")
            if truncated is not None and type(truncated) is not bool:
                raise ValueError("producer_observations.setup.imports.truncated must be boolean")
            if len(normalized_failures) > failed or (len(normalized_failures) < failed) != bool(
                truncated
            ):
                raise ValueError("producer_observations.setup.import failure sample disagrees")
            normalized_imports: Dict[str, Any] = {
                "status": "complete",
                "targets": normalized_targets,
                "targets_sha256": producer_observations_sha256(normalized_targets),
                "target_count": target_count,
                "importable_count": imported,
                "failed_count": failed,
                "failures": normalized_failures,
                "output_sha256": _producer_digest(
                    imports["output_sha256"],
                    "producer_observations.setup.imports.output_sha256",
                ),
            }
            if truncated:
                normalized_imports["truncated"] = True
            normalized["imports"] = normalized_imports
    if install_outcome == "failed" and ("pip_check" in normalized or "imports" in normalized):
        raise ValueError("failed setup cannot claim postconditions it did not reach")
    return normalized


def _validate_build_observation(raw: Any) -> Dict[str, Any]:
    build = _producer_mapping(raw, "producer_observations.build")
    _producer_exact_keys(
        build,
        required=("artifact_status", "artifacts"),
        field="producer_observations.build",
    )
    status = _producer_text(
        build["artifact_status"],
        field="producer_observations.build.artifact_status",
        maximum=32,
    )
    if status not in {"produced", "missing", "stale_only", "probe_failed"}:
        raise ValueError("producer_observations.build.artifact_status is invalid")
    artifacts = build["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > PRODUCER_OBSERVATION_ARTIFACT_CAP:
        raise ValueError("producer_observations.build.artifacts is out of bounds")
    normalized_artifacts = []
    for index, raw_artifact in enumerate(artifacts):
        artifact = _producer_mapping(raw_artifact, f"wheel artifact {index}")
        _producer_exact_keys(
            artifact,
            required=("path", "sha256", "size_bytes", "change"),
            field=f"wheel artifact {index}",
        )
        change = _producer_text(artifact["change"], field="wheel artifact change", maximum=16)
        if change not in {"new", "changed"}:
            raise ValueError("wheel artifact change is invalid")
        normalized_artifacts.append(
            {
                "path": _producer_relative_path(artifact["path"], "wheel artifact path"),
                "sha256": _producer_digest(artifact["sha256"], "wheel artifact sha256"),
                "size_bytes": _producer_count(
                    artifact["size_bytes"], "wheel artifact size", maximum=2**63 - 1
                ),
                "change": change,
            }
        )
    if any(
        posixpath.dirname(artifact["path"]) != "dist"
        or not artifact["path"].lower().endswith(".whl")
        for artifact in normalized_artifacts
    ):
        raise ValueError("wheel artifact delta must name direct dist/*.whl files")
    artifact_paths = [artifact["path"] for artifact in normalized_artifacts]
    if len(set(artifact_paths)) != len(artifact_paths):
        raise ValueError("wheel artifact delta contains duplicate paths")
    if (status == "produced") != bool(normalized_artifacts):
        raise ValueError("wheel artifact status disagrees with its delta")
    return {"artifact_status": status, "artifacts": normalized_artifacts}


def _validate_compile_observation(raw: Any) -> Dict[str, Any]:
    compile_observation = _producer_mapping(raw, "producer_observations.compile")
    status = compile_observation.get("status")
    if status == "unavailable":
        _producer_exact_keys(
            compile_observation,
            required=("status", "roots", "roots_sha256", "reason_code"),
            field="producer_observations.compile",
        )
    else:
        _producer_exact_keys(
            compile_observation,
            required=(
                "status",
                "roots",
                "roots_sha256",
                "source_count",
                "compiled_source_count",
                "missing_source_count",
                "foreign_pyc_count",
                "cache_tag",
                "source_basis_sha256",
                "pyc_basis_sha256",
                "source_basis_entry_count",
                "pyc_basis_entry_count",
                "missing_sources",
                "foreign_pycs",
                "conflicts",
            ),
            optional=("coverage",),
            field="producer_observations.compile",
        )
    if not isinstance(status, str) or status not in {"valid", "invalid", "unavailable"}:
        raise ValueError("producer_observations.compile.status is invalid")
    roots = compile_observation.get("roots")
    if not isinstance(roots, list) or not roots or len(roots) > PRODUCER_OBSERVATION_TARGET_CAP:
        raise ValueError("producer_observations.compile.roots is out of bounds")
    normalized_roots = [
        _producer_relative_path(root, "python compile root", allow_dot=True) for root in roots
    ]
    if len(set(normalized_roots)) != len(normalized_roots):
        raise ValueError("producer_observations.compile.roots contains duplicates")
    if compile_observation.get("roots_sha256") != producer_observations_sha256(normalized_roots):
        raise ValueError("producer_observations.compile roots hash disagrees")
    if status == "unavailable":
        reason = _producer_text(
            compile_observation["reason_code"],
            field="producer_observations.compile.reason_code",
            maximum=64,
        )
        if reason not in {
            "compileall_failed",
            "metrics_command_failed",
            "metrics_output_invalid",
            "basis_escaped",
            "no_sources",
        }:
            raise ValueError("producer_observations.compile.reason_code is invalid")
        return {
            "status": "unavailable",
            "roots": normalized_roots,
            "roots_sha256": producer_observations_sha256(normalized_roots),
            "reason_code": reason,
        }
    counts = {
        key: _producer_count(compile_observation[key], f"python compile {key}")
        for key in (
            "source_count",
            "compiled_source_count",
            "missing_source_count",
            "foreign_pyc_count",
            "source_basis_entry_count",
            "pyc_basis_entry_count",
        )
    }
    if (
        counts["compiled_source_count"] > counts["source_count"]
        or counts["missing_source_count"]
        != counts["source_count"] - counts["compiled_source_count"]
        or counts["source_basis_entry_count"] != counts["source_count"]
        or counts["pyc_basis_entry_count"] < counts["compiled_source_count"]
    ):
        raise ValueError("producer_observations.compile basis counts disagree")
    coverage = compile_observation.get("coverage")
    if status == "valid":
        if (
            counts["source_count"] <= 0
            or isinstance(coverage, bool)
            or not isinstance(coverage, (int, float))
        ):
            raise ValueError("valid compile observation requires coverage")
        expected = counts["compiled_source_count"] / counts["source_count"]
        if abs(float(coverage) - expected) > 1e-12 or counts["foreign_pyc_count"]:
            raise ValueError("valid compile observation has inconsistent coverage")
    elif coverage is not None or counts["foreign_pyc_count"] <= 0:
        raise ValueError("invalid compile observation requires a foreign pyc basis")
    normalized: Dict[str, Any] = {
        "status": status,
        "roots": normalized_roots,
        "roots_sha256": producer_observations_sha256(normalized_roots),
        **counts,
        "cache_tag": _producer_text(
            compile_observation["cache_tag"], field="python compile cache_tag", maximum=128
        ),
        "source_basis_sha256": _producer_digest(
            compile_observation["source_basis_sha256"], "python source basis sha256"
        ),
        "pyc_basis_sha256": _producer_digest(
            compile_observation["pyc_basis_sha256"], "python pyc basis sha256"
        ),
    }
    if coverage is not None:
        normalized["coverage"] = float(coverage)
    for key in ("missing_sources", "foreign_pycs"):
        samples = compile_observation[key]
        if not isinstance(samples, list) or len(samples) > PRODUCER_OBSERVATION_SAMPLE_CAP:
            raise ValueError(f"producer_observations.compile.{key} is out of bounds")
        normalized[key] = [
            _producer_relative_path(path, f"python compile {key} path") for path in samples
        ]
    conflicts = compile_observation["conflicts"]
    if not isinstance(conflicts, list) or any(
        conflict not in {"metrics_conflict"} for conflict in conflicts
    ):
        raise ValueError("producer_observations.compile.conflicts is invalid")
    if status == "invalid" and "metrics_conflict" not in conflicts:
        raise ValueError("invalid compile observation requires metrics_conflict")
    if status == "valid" and conflicts:
        raise ValueError("valid compile observation cannot carry a conflict")
    normalized["conflicts"] = list(conflicts)
    return normalized


def _producer_role_steps(observations: Mapping[str, Any], role: str) -> List[Mapping[str, Any]]:
    return [
        step
        for step in observations.get("recorded_project_steps") or ()
        if isinstance(step, Mapping) and step.get("role") == role
    ]


def _producer_one_role_step(observations: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    steps = _producer_role_steps(observations, role)
    if len(steps) != 1:
        raise ValueError(f"producer observation requires exactly one {role} step")
    return steps[0]


def _validate_producer_step_result(
    step: Mapping[str, Any],
    *,
    role: str,
    allow_semantic_zero_failure: bool = False,
) -> None:
    """Bind a typed role to the terminal result that actually produced it.

    Every project role that supports a postcondition needs both the physical
    exit and exact output digest.  The sole deliberate exception is a
    dependency installer whose output contains an install-error signature
    despite a lying zero exit: the producer marks that *semantic* result
    failed while preserving the real exit code and output hash.
    """

    if "exit_code" not in step or "output_sha256" not in step:
        raise ValueError(f"producer {role} step is missing its terminal result binding")
    exit_code = step["exit_code"]
    outcome = step.get("outcome")
    semantic_failure = step.get("semantic_failure")
    if semantic_failure is not None and (
        not allow_semantic_zero_failure
        or outcome != "failed"
        or exit_code != 0
        or semantic_failure != "install_error_signature"
    ):
        raise ValueError(f"producer {role} semantic failure has no matching physical shape")
    if outcome == "completed" and exit_code != 0:
        raise ValueError(f"producer {role} completed outcome disagrees with exit code")
    if outcome == "failed" and exit_code == 0:
        if not allow_semantic_zero_failure or semantic_failure != "install_error_signature":
            raise ValueError(f"producer {role} failed outcome disagrees with exit code")


def _validate_setup_step_bindings(observations: Mapping[str, Any]) -> None:
    setup = observations["setup"]
    dependency_steps = _producer_role_steps(observations, "dependency_install")
    if not dependency_steps:
        raise ValueError("python setup observation has no dependency_install step")
    for step in dependency_steps:
        _validate_producer_step_result(
            step,
            role="dependency_install",
            allow_semantic_zero_failure=True,
        )
    expected_install_outcome = (
        "success" if dependency_steps[-1].get("outcome") == "completed" else "failed"
    )
    if setup.get("install_outcome") != expected_install_outcome:
        raise ValueError("python setup outcome disagrees with final dependency_install step")

    pip_steps = _producer_role_steps(observations, "pip_check")
    pip_check = setup.get("pip_check")
    if isinstance(pip_check, Mapping):
        pip_step = _producer_one_role_step(observations, "pip_check")
        _validate_producer_step_result(pip_step, role="pip_check")
        if (
            pip_check.get("exit_code") != pip_step.get("exit_code")
            or pip_check.get("output_sha256") != pip_step.get("output_sha256")
            or (pip_check.get("status") == "clean") != (pip_step.get("outcome") == "completed")
        ):
            raise ValueError("python pip_check postcondition disagrees with its step")
    elif pip_steps:
        raise ValueError("python setup recorded pip_check without its typed postcondition")

    import_steps = _producer_role_steps(observations, "import_probe")
    imports = setup.get("imports")
    if not isinstance(imports, Mapping):
        if import_steps:
            raise ValueError("python setup recorded import_probe without its typed postcondition")
    elif imports.get("status") == "complete":
        import_step = _producer_one_role_step(observations, "import_probe")
        _validate_producer_step_result(import_step, role="import_probe")
        if import_step.get("outcome") != "completed" or imports.get(
            "output_sha256"
        ) != import_step.get("output_sha256"):
            raise ValueError("python import postcondition disagrees with its probe step")
    else:
        reason = imports.get("reason_code")
        if reason in {"no_survey_targets", "invalid_survey_targets"}:
            if import_steps:
                raise ValueError("python import no-target reason cannot have run a probe")
        elif reason in {"probe_failed", "invalid_probe_output"}:
            import_step = _producer_one_role_step(observations, "import_probe")
            _validate_producer_step_result(import_step, role="import_probe")
            expected = "failed" if reason == "probe_failed" else "completed"
            if import_step.get("outcome") != expected:
                raise ValueError("python import unavailable reason disagrees with its probe")

    if setup.get("install_outcome") == "failed" and (pip_steps or import_steps):
        raise ValueError("failed python install cannot have post-install probe steps")


def _validate_build_step_bindings(observations: Mapping[str, Any]) -> None:
    wheel_step = _producer_one_role_step(observations, "wheel_build")
    _validate_producer_step_result(wheel_step, role="wheel_build")
    if (
        observations["build"].get("artifact_status") == "produced"
        and wheel_step.get("outcome") != "completed"
    ):
        raise ValueError("produced wheel delta requires a successful wheel_build step")


def _validate_compile_step_bindings(observations: Mapping[str, Any]) -> None:
    compile_step = _producer_one_role_step(observations, "compileall")
    metrics_step = _producer_one_role_step(observations, "compile_metrics")
    _validate_producer_step_result(compile_step, role="compileall")
    _validate_producer_step_result(metrics_step, role="compile_metrics")
    compile_observation = observations["compile"]
    status = compile_observation.get("status")
    if status in {"valid", "invalid"}:
        if compile_step.get("outcome") != "completed" or metrics_step.get("outcome") != "completed":
            raise ValueError("typed compile basis requires both producer steps to complete")
        return
    reason = compile_observation.get("reason_code")
    expected_outcomes = {
        "compileall_failed": ("failed", "completed"),
        "metrics_command_failed": (None, "failed"),
        "metrics_output_invalid": (None, "completed"),
        "basis_escaped": (None, "completed"),
        "no_sources": ("completed", "completed"),
    }
    compile_outcome, metrics_outcome = expected_outcomes[reason]
    if compile_outcome is not None and compile_step.get("outcome") != compile_outcome:
        raise ValueError("compile unavailable reason disagrees with compileall step")
    if metrics_step.get("outcome") != metrics_outcome:
        raise ValueError("compile unavailable reason disagrees with metrics step")


def _validate_producer_step_bindings(observations: Mapping[str, Any]) -> None:
    {
        "setup_env": _validate_setup_step_bindings,
        "build": _validate_build_step_bindings,
        "compile": _validate_compile_step_bindings,
    }[str(observations.get("operation") or "")](observations)


def normalize_producer_observations(value: Any) -> Dict[str, Any]:
    """Validate and copy one strict, bounded producer-observation record.

    No field is truncated or coerced.  A producer either persists the complete
    typed fact sheet it observed, or persists no receipt and returns the typed
    evidence-persistence failure to its caller.
    """

    observations = _producer_mapping(value, "producer_observations")
    operation = observations.get("operation")
    variant = operation if operation in {"setup_env", "build", "compile"} else None
    variant_key = "setup" if variant == "setup_env" else variant
    _producer_exact_keys(
        observations,
        required=(
            "schema_version",
            "ecosystem",
            "operation",
            "recorded_project_steps",
            variant_key or "invalid_variant",
        ),
        optional=("recorded_mechanical_steps",),
        field="producer_observations",
    )
    if observations.get("schema_version") != PRODUCER_OBSERVATIONS_SCHEMA_VERSION:
        raise ValueError("producer observations schema version is unsupported")
    if observations.get("ecosystem") != "python" or variant is None:
        raise ValueError("producer observations ecosystem/operation is unsupported")

    normalized: Dict[str, Any] = {
        "schema_version": PRODUCER_OBSERVATIONS_SCHEMA_VERSION,
        "ecosystem": "python",
        "operation": variant,
    }
    all_ordinals: List[int] = []
    for key, roles in (
        ("recorded_project_steps", _PRODUCER_PROJECT_ROLES),
        ("recorded_mechanical_steps", _PRODUCER_MECHANICAL_ROLES),
    ):
        raw_steps = observations.get(key, [])
        if not isinstance(raw_steps, list) or len(raw_steps) > PRODUCER_OBSERVATION_STEP_CAP:
            raise ValueError(f"producer_observations.{key} is out of bounds")
        steps = [
            _validate_producer_step(step, roles=roles, field=f"{key}[{index}]")
            for index, step in enumerate(raw_steps)
        ]
        if key == "recorded_project_steps" or steps:
            normalized[key] = steps
        all_ordinals.extend(step["ordinal"] for step in steps)
    if sorted(all_ordinals) != list(range(1, len(all_ordinals) + 1)):
        raise ValueError("producer observation step ordinals are incomplete or duplicated")
    allowed_by_operation = {
        "setup_env": {
            "project": {"dependency_install", "pip_check", "import_probe"},
            "mechanical": {"venv_create"},
        },
        "build": {
            "project": {"wheel_build"},
            "mechanical": {"build_prerequisite"},
        },
        "compile": {
            "project": {"compileall", "compile_metrics"},
            "mechanical": set(),
        },
    }[variant]
    if any(
        step["role"] not in allowed_by_operation["project"]
        for step in normalized["recorded_project_steps"]
    ) or any(
        step["role"] not in allowed_by_operation["mechanical"]
        for step in normalized.get("recorded_mechanical_steps", ())
    ):
        raise ValueError("producer observation step role does not belong to the operation")

    normalized[variant_key] = {
        "setup": _validate_setup_observation,
        "build": _validate_build_observation,
        "compile": _validate_compile_observation,
    }[variant_key](observations[variant_key])
    _validate_producer_step_bindings(normalized)
    body = _producer_canonical_bytes(normalized)
    if len(body) > PRODUCER_OBSERVATIONS_MAX_CANONICAL_BYTES:
        raise ValueError("producer observations exceed the canonical byte budget")
    return normalized


def read_producer_observations(receipt: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return hash-verified typed observations, or None on any integrity gap."""

    raw = receipt.get("producer_observations") if isinstance(receipt, Mapping) else None
    digest = receipt.get("producer_observations_sha256") if isinstance(receipt, Mapping) else None
    if raw is None or not isinstance(digest, str):
        return None
    try:
        normalized = normalize_producer_observations(raw)
        expected = producer_observations_sha256(normalized)
    except (TypeError, ValueError):
        return None
    return normalized if digest.lower() == expected else None


class ReportSnapshot(dict):
    """A report hash mapping plus whether the machine read completed.

    It intentionally remains a ``dict`` subclass so historical parser and
    test callers keep their mapping contract.  The completeness bit prevents
    a failed BEFORE read from masquerading as a proven empty directory and
    laundering unchanged reports into an invocation's ``new`` delta.
    """

    def __init__(self, values: Optional[Mapping[str, str]] = None, *, complete: bool) -> None:
        super().__init__(values or {})
        self.complete = complete


def report_snapshot_complete(value: Mapping[str, str]) -> bool:
    return not isinstance(value, ReportSnapshot) or value.complete


def snapshot_reports(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    scan_roots: Iterable[str],
) -> ReportSnapshot:
    """Content hashes of every report XML under `scan_roots`, path -> sha256.

    ONE shell round-trip per side of an invocation: `find` filters the report
    shapes itself (no xargs, no second `cat` pass) and hashes what it kept.
    A transport failure yields an incomplete empty mapping rather than an
    exception.  It does not break the build, but ``report_delta`` refuses to
    project positive path evidence from an unreadable bracket.
    """
    roots = _unique_roots(scan_roots)
    if not roots:
        return ReportSnapshot(complete=True)
    predicates = " -o ".join(
        f"-path {shlex.quote(f'*{marker}*.xml')}" for marker in REPORT_PATH_MARKERS
    )
    command = (
        "find "
        + " ".join(shlex.quote(root) for root in roots)
        + f" -type f \\( {predicates} \\) -exec sha256sum {{}} + 2>/dev/null"
    )
    control_execute = resolve_control_execute(execute)
    if not callable(control_execute):
        return ReportSnapshot(complete=False)
    try:
        # The MACHINE path, not the presentation path: DockerOrchestrator
        # truncates ordinary output beyond ~10,000 characters, and 260 report
        # hashes is ~34KB. Live p8a-kafka: the settled receipt claimed exactly
        # 50 of 260 on-disk reports — and the campaign's kafka receipt claimed
        # exactly 50 too, because the clamp, not the run, decided the claim
        # set. The bracketing that receipt-scoping stands on must read every
        # line. The TypeError fallback keeps small doubles working, the same
        # pattern container_io's _execute_untruncated uses.
        try:
            result = control_execute(command, truncate_output=False) or {}
        except TypeError as exc:
            if "truncate_output" not in str(exc):
                raise
            result = control_execute(command) or {}
    except Exception as exc:  # evidence collection never breaks the runner
        logger.debug(f"report snapshot skipped: {exc}")
        return ReportSnapshot(complete=False)
    if (
        not isinstance(result, Mapping)
        or result.get("success") is False
        or result.get("dispatch_status")
        or result.get("exit_code") != 0
    ):
        logger.debug("report snapshot skipped: clean transport did not complete")
        return ReportSnapshot(complete=False)
    return ReportSnapshot(_parse_sha256sum(result.get("output") or ""), complete=True)


def report_delta(
    before: Mapping[str, str],
    after: Mapping[str, str],
    cached_roots: Optional[Iterable[str]] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """What THIS invocation produced: written, rewritten, or vouched for.

    `new` and `changed` are what the dispatch physically wrote. A byte-identical
    XML from an earlier attempt is not this invocation's evidence and appears in
    neither; both keys are always present, so an empty list states "this
    invocation wrote no reports", which is what the primary rollup needs.

    `cached` is the third case, and it is not a weaker one. Live kafka:
    `--build-cache` served most test tasks FROM-CACHE, so their reports were
    never rewritten, the hashes did not move, and 4,686 passing tests could be
    claimed by nothing — they sat in auxiliary while the main count read 546.
    A cache hit is Gradle stating that the report on disk IS this build's
    result for that task, which is a stronger guarantee than a file merely
    existing. `cached_roots` are the directories the build system vouched for;
    reports under them are claimable, and kept in their own bucket so a reader
    can always tell what ran from what was vouched for. The key is absent when
    nothing was vouched for.
    """
    if not report_snapshot_complete(before) or not report_snapshot_complete(after):
        # Unknown is not empty.  The runner may still return its physical exit
        # result, but no report path is attributed without two complete
        # machine snapshots.
        return {"new": [], "changed": []}
    new: List[Dict[str, str]] = []
    changed: List[Dict[str, str]] = []
    cached: List[Dict[str, str]] = []
    roots = [str(root).rstrip("/") for root in (cached_roots or ()) if str(root or "").strip()]
    for path in sorted(after):
        digest = after[path]
        if path not in before:
            new.append({"path": path, "sha256": digest})
        elif before[path] != digest:
            changed.append({"path": path, "sha256": digest})
        elif any(path == root or path.startswith(root + "/") for root in roots):
            cached.append({"path": path, "sha256": digest})
    delta: Dict[str, List[Dict[str, str]]] = {"new": new, "changed": changed}
    if cached:
        delta["cached"] = cached
    return delta


def target_sha(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    working_directory: str,
) -> Optional[str]:
    """The checkout SHA of the tree this invocation ran in, or None.

    Same probe python_tool._target_sha uses for the native-smoke capability
    receipt, with one extra rule: the answer must LOOK like a git object name.
    A container that replies to every command with build-log text has stated
    no sha, and recording that text as provenance would be a fabrication.
    """
    directory = str(working_directory or "").strip()
    if not directory:
        return None
    try:
        result = execute(f"git -C {shlex.quote(directory)} rev-parse HEAD") or {}
    except Exception as exc:  # evidence collection never breaks the runner
        logger.debug(f"target sha unavailable: {exc}")
        return None
    if not _succeeded(result):
        return None
    candidate = _first_line(result.get("output"))
    return candidate if _OBJECT_NAME_RE.fullmatch(candidate) else None


def survey_pins(manifest: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """The survey/config fingerprints AS THE SURVEY RECORDED THEM.

    Read-through only, and only from a manifest the caller ALREADY holds: the
    survey handoff manifest is read exactly once per build by the layer that
    owns the pre-flight, and a receipt must not turn that into a second probe
    (tests/test_build_tool_preflight_integration.py). The survey stamp is the
    single existing producer of these pins, so a pin it does not carry stays an
    absent key — Stage A introduces the survey/document-map fingerprint, and
    nothing is invented in the meantime.
    """
    stamp = manifest.get("survey") if isinstance(manifest, Mapping) else None
    pins: Dict[str, str] = {}
    for key in ("survey_fingerprint", "config_fingerprint", "document_map_fingerprint"):
        value = stamp.get(key) if isinstance(stamp, Mapping) else None
        if value is None and isinstance(manifest, Mapping):
            value = manifest.get(key)
        text = str(value or "").strip()
        if text:
            pins[key] = text
    return pins


def python_import_targets(
    manifest: Optional[Mapping[str, Any]],
) -> Optional[List[str]]:
    """Return the one canonical survey-owned Python import target set.

    Producer, assessor, and physical judge must compare against the same
    manifest projection. ``None`` means the projection is malformed/too large;
    an empty list is the honest statement that the survey named no targets.
    No package discovery or import is performed here.
    """

    if not isinstance(manifest, Mapping):
        return None
    raw: List[Any] = []
    package_paths = manifest.get("python_package_paths") or ()
    if package_paths:
        if not isinstance(package_paths, (list, tuple)):
            return None
        raw = [
            item.get("import_name")
            for item in package_paths
            if isinstance(item, Mapping) and item.get("import_name")
        ]
    if not raw:
        packages = manifest.get("python_packages") or ()
        if not isinstance(packages, (list, tuple)):
            return None
        raw = list(packages)
    targets: List[str] = []
    for value in raw:
        target = str(value or "").strip()
        if not _PYTHON_IMPORT_RE.fullmatch(target):
            return None
        if target not in targets:
            targets.append(target)
    if len(targets) > PRODUCER_OBSERVATION_TARGET_CAP:
        return None
    return targets


def nearest_domain_root(
    manifest: Optional[Mapping[str, Any]],
    working_directory: str,
) -> Optional[str]:
    """The surveyed build domain this invocation belongs to, or None.

    One invocation belongs to ONE domain: the NEAREST containing root wins, the
    same rule the phase gate's domain rollup applies. A run outside every
    surveyed domain — and a project with no surveyed domains at all — has no
    domain fact, so the key stays absent.
    """
    directory = _normalized_root(working_directory)
    if not directory:
        return None
    containing = [
        root
        for root in _surveyed_domain_roots(manifest)
        if directory == root or directory.startswith(f"{root}/")
    ]
    return max(containing, key=len) if containing else None


def nearest_domain_fact_epoch(
    manifest: Optional[Mapping[str, Any]],
    working_directory: str,
) -> Optional[int]:
    """The positive fact epoch of the same nearest surveyed domain root."""

    root = nearest_domain_root(manifest, working_directory)
    facts = manifest.get("domain_facts") if isinstance(manifest, Mapping) else None
    if not root or not isinstance(facts, (list, tuple)):
        return None
    for fact in facts:
        if not isinstance(fact, Mapping) or posixpath.normpath(str(fact.get("root") or "")) != root:
            continue
        epoch = fact.get("fact_epoch")
        return epoch if type(epoch) is int and epoch > 0 else None
    return None


def toolchain_fingerprint(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    executable: Optional[str],
    version_flag: str,
    working_directory: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Which runner binary this invocation actually launched, and its version.

    ONE round trip: the resolved path (`command -v`) and the FIRST line of the
    runner's own version output, separated by a marker so an unresolved path
    can never shift into the version slot. A wrapper (`./gradlew`, `./mvnw`) is
    resolved from the invocation's own cwd, which is where it ran.
    """
    runner = str(executable or "").strip()
    if not runner:
        return None
    directory = str(working_directory or "").strip()
    prefix = f"cd {shlex.quote(directory)} 2>/dev/null; " if directory else ""
    command = (
        f"{prefix}command -v {shlex.quote(runner)} 2>/dev/null; "
        f"echo {shlex.quote(TOOLCHAIN_MARKER)}; "
        f"{shlex.quote(runner)} {version_flag} 2>&1 | head -n 1"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"toolchain fingerprint unavailable: {exc}")
        return None
    resolved, _, version = str(result.get("output") or "").partition(TOOLCHAIN_MARKER)
    fingerprint: Dict[str, str] = {}
    path = _first_line(resolved)
    if path:
        fingerprint["executable"] = path
    line = _first_line(version)[:VERSION_LINE_MAX_CHARS]
    if line:
        fingerprint["version"] = line
    return fingerprint or None


def output_content_hash(output: Optional[str]) -> Optional[str]:
    """sha256 of the output the tool already holds; None when there is none."""
    if output is None:
        return None
    return hashlib.sha256(str(output).encode("utf-8", "replace")).hexdigest()


def read_testcase_outcomes(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    delta: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Bounded per-testcase outcomes from THIS invocation's own reports.

    Review binding note (b): `{node_id, status, reason?}` per node, capped at
    ``TESTCASE_OUTCOME_CAP`` with the truncation recorded. Failures and errors
    sort first, so a truncated list still carries the diagnostic signal and the
    order never depends on which file the container listed first.

    Only the reports named by the delta are read — never a tree scan — and only
    their TAGS cross the transport. A report nobody could read is UNKNOWN, not
    "this invocation ran no tests", so the key stays absent entirely.
    """
    paths: List[str] = []
    for bucket in ("new", "changed"):
        for entry in (delta or {}).get(bucket) or ():
            path = str((entry or {}).get("path") or "").strip()
            if path and path not in paths:
                paths.append(path)
    if not paths:
        return None
    truncated = len(paths) > TESTCASE_FILE_CAP
    command = "; ".join(
        f"grep -oE {shlex.quote(TESTCASE_TAG_PATTERN)} {shlex.quote(path)} 2>/dev/null "
        f"| head -n {TESTCASE_TAG_CAP}"
        for path in paths[:TESTCASE_FILE_CAP]
    )
    try:
        # `grep` exits nonzero when a report simply has no matching tag, so the
        # command status says nothing here; only the tokens do.
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"testcase outcomes unavailable: {exc}")
        return None
    nodes, seen = _parse_testcase_tags(str(result.get("output") or ""))
    if not nodes:
        return None
    if seen > TESTCASE_OUTCOME_CAP:
        truncated = True
    outcomes: Dict[str, Any] = {"nodes": nodes[:TESTCASE_OUTCOME_CAP]}
    if truncated:
        outcomes["truncated"] = True
    return outcomes


def report_tag_command(path: str, *, tag_cap: int = TESTCASE_TAG_CAP) -> str:
    """One report's bounded tag read, headed by the digest of the bytes read.

    The digest is the point. `read_testcase_outcomes` reads tags and can say
    nothing about WHICH version of a report produced them, so its stream cannot
    be bound to an invocation. Hashing and grepping the same path inside one
    shell command means the caller can compare the digest against the receipt's
    own `report_delta` claim and refuse a report the delta does not name.

    Nothing here reads a whole report into anything: `grep -oE` streams, and
    the per-file bound cuts the token list at `tag_cap`. That is what makes it
    usable on kafka's 137.8 MB single XML, which no receipt-bound transport can
    ever slurp against a 16 MB canonical budget.
    """

    quoted = shlex.quote(str(path))
    return (
        f"{{ printf %s {shlex.quote(REPORT_TAG_MARKER)}; sha256sum {quoted} 2>/dev/null; echo; "
        f"grep -oE {shlex.quote(TESTCASE_TAG_PATTERN)} {quoted} 2>/dev/null "
        f"| head -n {int(tag_cap)}; }}"
    )


def parse_report_tag_rows(
    output: str,
    *,
    report_claims: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """A marked, per-report tag stream -> one row per testcase, attributed.

    Rows carry the same keys the in-container exact parser emits, so the fold
    that orders and bounds them, the diagnostic projection and the row contract
    all keep working on one row shape.

    `report_claims` is `path -> sha256` as the receipt's report delta states it.
    When given, a report whose header digest does not match its claim
    contributes NO rows: bytes that are not the bytes this invocation is
    accountable for are not this invocation's evidence, and a mismatch is
    silence rather than a guess. A report the claims do not name at all is
    likewise dropped — discovery is wider than the delta on purpose, and only
    the delta can bind an identity to this receipt.
    """

    rows: List[Dict[str, Any]] = []
    for chunk in str(output or "").split(REPORT_TAG_MARKER)[1:]:
        header, _, body = chunk.partition("\n")
        match = _REPORT_TAG_HEADER_RE.match(header.strip())
        if match is None:
            continue
        digest, path = match.group(1), match.group(2).strip()
        if report_claims is not None and str(report_claims.get(path) or "").lower() != digest:
            continue
        rows.extend(_report_tag_rows(body, path=path, digest=digest))
    return rows


def _report_tag_rows(body: str, *, path: str, digest: str) -> List[Dict[str, Any]]:
    """One report's token stream -> its rows, in the order the file states them.

    A node whose closing tag the per-file bound cut still closes when the NEXT
    testcase opens — by then the whole node was read, so its outcome is
    observed. A node still pending when the stream ENDS is different: the bound
    cut this report somewhere inside it, and every child that would have stated
    a failure may be on the other side of the cut. Publishing it would publish
    the parser's default (`passed`) as an observation, which is exactly how a
    red identity becomes a green row. So it is dropped, and the file's declared
    total is what discloses the loss. Fewer rows, never a wrong one.

    Unlike the diagnostic parser this does NOT deduplicate by identity — a
    report may legitimately repeat a testcase element, and each one is an
    execution.
    """

    rows: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None

    def close(row: Optional[Dict[str, Any]]) -> None:
        if row is not None:
            rows.append(row)

    for match in _TESTCASE_TAG_RE.finditer(body or ""):
        closing, tag, attributes = match.group(1), match.group(2), match.group(3)
        self_closing = attributes.rstrip().endswith("/")
        if tag == "testcase":
            close(pending)
            pending = None
            if closing or len(rows) >= TESTCASE_PARSE_CAP:
                continue
            name = _tag_attribute(attributes, "name")
            if not name:
                continue
            pending = {
                "report_path": path,
                "report_sha256": digest,
                "classname": _tag_attribute(attributes, "classname"),
                "name": name,
                "source_file": None,
                "outcome": "passed",
                "reason": None,
                "execution_ordinal": len(rows) + 1,
            }
            if self_closing:
                close(pending)
                pending = None
        elif pending is not None:
            pending["outcome"] = {"skipped": "skipped", "failure": "failed"}.get(tag, "error")
            reason = _tag_attribute(attributes, "message")
            if reason:
                pending["reason"] = reason[:SKIP_REASON_MAX_CHARS]
    # `pending` is deliberately NOT closed here: an unterminated trailing node
    # is a cut read, not a passing test.
    return rows[:TESTCASE_PARSE_CAP]


def _receipt_text(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    lowercase: bool = False,
    maximum_bytes: int = RECEIPT_TEXT_MAX_BYTES,
    allow_linebreaks: bool = False,
) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"receipt {field} must be canonical text")
    if not value and not allow_empty:
        raise ValueError(f"receipt {field} must be non-empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"receipt {field} exceeds its length limit")
    if "\x00" in value or (not allow_linebreaks and ("\n" in value or "\r" in value)):
        raise ValueError(f"receipt {field} contains unsafe control text")
    if lowercase and value != value.lower():
        raise ValueError(f"receipt {field} must be lowercase")
    return value


def _receipt_text_list(value: Any, field: str, *, cap: int = RECEIPT_SEQUENCE_MAX_ITEMS):
    if not isinstance(value, list) or len(value) > cap:
        raise ValueError(f"receipt {field} must be a bounded list")
    items = [_receipt_text(item, f"{field} item") for item in value]
    if len(items) != len(set(items)):
        raise ValueError(f"receipt {field} contains duplicates")
    return items


def _validate_report_delta(value: Any) -> set[tuple[str, str]]:
    if not isinstance(value, Mapping) or not {"new", "changed"}.issubset(value):
        raise ValueError("receipt report_delta must contain new and changed lists")
    if set(value) - {"new", "changed", "cached"}:
        raise ValueError("receipt report_delta contains unknown buckets")
    claims: set[tuple[str, str]] = set()
    for bucket in ("new", "changed", "cached"):
        entries = value.get(bucket, [])
        if not isinstance(entries, list):
            raise ValueError(f"receipt report_delta.{bucket} must be a list")
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256"}:
                raise ValueError("receipt report_delta entry shape is invalid")
            path = _receipt_text(entry.get("path"), "report_delta.path")
            digest = _receipt_text(entry.get("sha256"), "report_delta.sha256", lowercase=True)
            if not path.startswith("/") or posixpath.normpath(path) != path:
                raise ValueError("receipt report_delta path must be absolute and canonical")
            if _SHA256_RE.fullmatch(digest) is None:
                raise ValueError("receipt report_delta hash is invalid")
            claim = (path, digest)
            if claim in claims or any(existing[0] == path for existing in claims):
                raise ValueError("receipt report_delta contains duplicate paths")
            claims.add(claim)
    return claims


# A refusal has to be actionable. Live camel (an over-cap reactor list) and
# live kafka (a node id carrying raw newlines) failed for two unrelated
# reasons and reported the same word: `invalid_arguments`. Both the dropped
# evidence field's stated reason and the persistence code below carry the
# refusal itself, so the failing ARGUMENT is named at both boundaries.
OMISSION_REASON_MAX_CHARS = 200
_RECEIPT_MESSAGE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
# Receipt refusals name their field in the singular when they speak about one
# entry ("testcase_outcome.node_id"); the receipt carries the plural.
_RECEIPT_FIELD_ALIASES = {
    "id": "receipt_id",
    "testcase_outcome": "testcase_outcomes",
    "capability_observation": "capability_observations",
    "module_outcome": "module_outcomes",
    "evidence_omission": "evidence_omissions",
}


def _omission_reason(exc: BaseException) -> str:
    """One canonical line stating why a field could not be carried."""

    text = " ".join(str(exc).split())[:OMISSION_REASON_MAX_CHARS].strip()
    return text or "receipt evidence field is unrepresentable"


def receipt_refusal_code(exc: BaseException) -> str:
    """`invalid_arguments:<argument>` for one schema refusal.

    The field is read from the refusal's own text, which every raise in this
    module writes in the canonical `receipt <field> ...` shape. A refusal that
    names no field still carries its reason rather than the bare word, because
    a caller who cannot tell WHICH argument was refused cannot repair it.
    """

    message = " ".join(str(exc).split())
    for token in _RECEIPT_MESSAGE_TOKEN_RE.findall(message):
        root = token.split(".", 1)[0]
        field = _RECEIPT_FIELD_ALIASES.get(root, root)
        if field in _RECEIPT_V2_FIELDS:
            return f"{WRITE_INVALID_ARGUMENTS}:{field}"
    detail = re.sub(r"_+", "_", _slug(message)).strip("_")[:96]
    return f"{WRITE_INVALID_ARGUMENTS}:{detail or 'unnamed_argument'}"


def _validate_testcase_outcomes(value: Any) -> None:
    if not isinstance(value, Mapping) or not set(value).issubset({"nodes", "truncated"}):
        raise ValueError("receipt testcase_outcomes shape is invalid")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes or len(nodes) > TESTCASE_OUTCOME_CAP:
        raise ValueError("receipt testcase_outcomes.nodes is invalid")
    seen = set()
    for node in nodes:
        if not isinstance(node, Mapping) or not {"node_id", "status"}.issubset(node):
            raise ValueError("receipt testcase_outcomes node is invalid")
        if set(node) - {"node_id", "status", "reason"}:
            raise ValueError("receipt testcase_outcomes node has unknown fields")
        identifier = _receipt_text(node.get("node_id"), "testcase_outcome.node_id")
        status = _receipt_text(node.get("status"), "testcase_outcome.status", lowercase=True)
        if status not in _STATUS_PRIORITY:
            raise ValueError("receipt testcase_outcome.status is invalid")
        if "reason" in node:
            reason = _receipt_text(node.get("reason"), "testcase_outcome.reason")
            if len(reason) > SKIP_REASON_MAX_CHARS:
                raise ValueError("receipt testcase_outcome.reason is oversized")
        if identifier in seen:
            raise ValueError("receipt testcase_outcomes contains duplicate nodes")
        seen.add(identifier)
    if "truncated" in value and value.get("truncated") is not True:
        raise ValueError("receipt testcase_outcomes.truncated must be true when present")


def _validate_capability_observations(value: Any) -> None:
    if not isinstance(value, list) or not value or len(value) > RECEIPT_SEQUENCE_MAX_ITEMS:
        raise ValueError("receipt capability_observations is invalid")
    features = set()
    for observation in value:
        if not isinstance(observation, Mapping) or not {"feature", "probe"}.issubset(observation):
            raise ValueError("receipt capability_observations entry shape is invalid")
        if set(observation) - {"feature", "probe", "probe_exit_code", "observation"}:
            raise ValueError("receipt capability_observations entry has unknown fields")
        for key, item in observation.items():
            _receipt_text(item, f"capability_observation.{key}")
        if observation["feature"] in features:
            raise ValueError("receipt capability_observations duplicate a feature")
        features.add(observation["feature"])


def _validate_module_outcomes(value: Any) -> None:
    """The reactor summary, bounded by what a reactor can actually print.

    This list is the coverage DENOMINATOR, so it is never clipped to fit: a
    module the build never tried is untried, not missing, and a truncated list
    would state a smaller reactor than the one that ran.

    `tests_reported` is the one OPTIONAL addition and it closes MS-1 §14.2's
    ambiguity for the modules that can close it. Gradle states no per-module
    verdict, so `attempted` has to cover both "ran its tests" and "ran a task
    and produced nothing" — and a whole reactor of `attempted` is why kafka's
    19 modules and 27,219 tests read as unknown. A module whose report files
    declare their own suite totals has PROVEN it executed tests; the count is
    that proof, written by the harvest, absent when nothing proved it. It never
    replaces `status`: an executed module can still have failed.
    """

    if not isinstance(value, list) or not value or len(value) > RECEIPT_MODULE_OUTCOMES_MAX_ITEMS:
        raise ValueError("receipt module_outcomes is invalid")
    for module in value:
        if (
            not isinstance(module, Mapping)
            or not {"module", "status"}.issubset(module)
            or set(module) - {"module", "status", "tests_reported"}
        ):
            raise ValueError("receipt module_outcomes entry shape is invalid")
        _receipt_text(module.get("module"), "module_outcomes.module")
        _receipt_text(module.get("status"), "module_outcomes.status")
        if "tests_reported" in module:
            _receipt_count(module.get("tests_reported"), "module_outcomes.tests_reported")


def _receipt_count(value: Any, field: str, *, minimum: int = 0) -> int:
    """One non-negative integer count. `True` is not 1 and never was."""

    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"receipt {field} must be an integer >= {minimum}")
    return value


def _validate_gradle_suite_summaries(value: Any, *, receipt: Mapping[str, Any]) -> None:
    """Per (project, task-dir) totals summed from the `<testsuite>` roots.

    This is the COUNT side of Gradle test evidence and it is deliberately not
    the same measurement as `testcase_execution_rows`: the totals here come
    from every harvested report's own declared attributes, while the rows are a
    bounded identity sample. A reader that conflates the two would turn a
    2048-row sample into a project-wide rollup, so they stay separate fields
    with separate names.

    Nothing here is summed by a model, and a summary is only meaningful for the
    runner whose layout defines the (project, task) pair — a maven or pytest
    receipt carrying one would be stating evidence it never harvested.
    """

    if str(receipt.get("tool") or "").strip().lower() != "gradle":
        raise ValueError("receipt gradle_suite_summaries requires the gradle runner")
    if not isinstance(value, Mapping) or not set(value).issubset(
        {"suites", "truncated", "dropped_suites", "unreadable_suites", "unsummarized_files"}
    ):
        raise ValueError("receipt gradle_suite_summaries shape is invalid")
    suites = value.get("suites")
    if not isinstance(suites, list) or not suites or len(suites) > GRADLE_SUITE_SUMMARY_CAP:
        raise ValueError("receipt gradle_suite_summaries.suites is invalid")
    seen: set[tuple[str, str]] = set()
    for suite in suites:
        if not isinstance(suite, Mapping) or set(suite) != _GRADLE_SUITE_FIELDS:
            raise ValueError("receipt gradle_suite_summaries entry shape is invalid")
        module = _receipt_text(suite.get("module"), "gradle_suite_summaries.module")
        task = _receipt_text(suite.get("task"), "gradle_suite_summaries.task")
        _receipt_count(suite.get("xml_files"), "gradle_suite_summaries.xml_files", minimum=1)
        for field in _GRADLE_SUITE_COUNT_FIELDS:
            _receipt_count(suite.get(field), f"gradle_suite_summaries.{field}")
        if (module, task) in seen:
            raise ValueError("receipt gradle_suite_summaries repeats a module task pair")
        seen.add((module, task))
    # A cap that fired and a cap that did not are different facts, and the
    # count of what it dropped is the fact — `truncated` alone would say a
    # summary is partial without saying by how much.
    truncated = "truncated" in value
    if truncated and value.get("truncated") is not True:
        raise ValueError("receipt gradle_suite_summaries.truncated must be true when present")
    if truncated != ("dropped_suites" in value):
        raise ValueError("receipt gradle_suite_summaries truncation must state what it dropped")
    if truncated:
        _receipt_count(
            value.get("dropped_suites"), "gradle_suite_summaries.dropped_suites", minimum=1
        )
    if "unreadable_suites" in value:
        _receipt_count(
            value.get("unreadable_suites"), "gradle_suite_summaries.unreadable_suites", minimum=1
        )
    # Reports discovery found and the FILE bound never summed — a different
    # fact from `unreadable_suites` (a report whose head yielded no parsable
    # `<testsuite>` root) and from `dropped_suites` (a (project, task) pair the
    # pair bound dropped). Without it a totals section over 2,048 of 5,000
    # reports would read as the whole project.
    if "unsummarized_files" in value:
        _receipt_count(
            value.get("unsummarized_files"),
            "gradle_suite_summaries.unsummarized_files",
            minimum=1,
        )


def _validate_gradle_row_disclosure(value: Any, *, receipt: Mapping[str, Any]) -> None:
    """What the bounded identity harvest kept, and what it had to drop.

    `red_rows_complete` is the only claim a consumer may lean on when the rows
    are a sample: it says every failure/error identity the harvested summaries
    account for is present in the rows. It is a claim, so it is refused when it
    contradicts the truncation record beside it.
    """

    if str(receipt.get("tool") or "").strip().lower() != "gradle":
        raise ValueError("receipt gradle_row_disclosure requires the gradle runner")
    if not isinstance(value, Mapping) or set(value) - {
        "rows_source",
        "red_rows_complete",
        "rows_truncated",
    }:
        raise ValueError("receipt gradle_row_disclosure shape is invalid")
    if not {"rows_source", "red_rows_complete"}.issubset(value):
        raise ValueError("receipt gradle_row_disclosure must state its source and red completeness")
    source = _receipt_text(value.get("rows_source"), "gradle_row_disclosure.rows_source")
    if source != GRADLE_ROWS_SOURCE:
        raise ValueError("receipt gradle_row_disclosure.rows_source is not a known transport")
    complete = value.get("red_rows_complete")
    if complete is not True and complete is not False:
        raise ValueError("receipt gradle_row_disclosure.red_rows_complete must be a boolean")
    if "rows_truncated" not in value:
        return
    truncation = value.get("rows_truncated")
    if not isinstance(truncation, Mapping) or set(truncation) - {
        "dropped_green",
        "dropped_files",
        "dropped_red",
        "unread_rows",
    }:
        raise ValueError("receipt gradle_row_disclosure.rows_truncated shape is invalid")
    if not {"dropped_green", "dropped_files"}.issubset(truncation):
        raise ValueError("receipt gradle_row_disclosure.rows_truncated is incomplete")
    green = _receipt_count(truncation.get("dropped_green"), "gradle_row_disclosure.dropped_green")
    files = _receipt_count(truncation.get("dropped_files"), "gradle_row_disclosure.dropped_files")
    red = 0
    if "dropped_red" in truncation:
        red = _receipt_count(
            truncation.get("dropped_red"), "gradle_row_disclosure.dropped_red", minimum=1
        )
    # Identities a report declared and its bounded read never delivered. A
    # per-file bound is as real a cap as the row cap, and a sample that reported
    # 130 of a file's 1,000 tests while counting nothing dropped would be the
    # silent cap in its purest form.
    unread = 0
    if "unread_rows" in truncation:
        unread = _receipt_count(
            truncation.get("unread_rows"), "gradle_row_disclosure.unread_rows", minimum=1
        )
    # A file-level bound is a loss even when no row it would have produced was
    # ever built: kafka's 50-file tag bound leaves 1,126 reports unspoken for,
    # and a truncation record that refused to say so would be the silent cap
    # this schema exists to forbid.
    if not green and not red and not files and not unread:
        raise ValueError("receipt gradle_row_disclosure.rows_truncated dropped nothing")
    if red and complete is not False:
        raise ValueError("receipt gradle_row_disclosure cannot drop a red and claim completeness")


def _gradle_evidence_text(value: Any) -> str:
    """Collapse harvested text to one receipt-safe line.

    The kafka lesson: a JUnit display name may legally carry a newline, and
    `_receipt_text` refuses control characters. Collapsing HERE means the field
    is carried; collapsing nowhere means the field is dropped to an omission.
    """

    return " ".join(str(value if value is not None else "").split())


def _gradle_summary_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _gradle_row_ordinal(row: Mapping[str, Any]) -> int:
    value = row.get("execution_ordinal")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        # Sorts after every stated ordinal without changing tier: an unstated
        # ordinal cannot be sealed anyway, so it is the first row to lose a
        # contested slot inside its own tier.
        return 1 << 62
    return value


def _gradle_row_sort_key(row: Mapping[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(row.get("report_path") or ""),
        _gradle_row_ordinal(row),
        _gradle_evidence_text(row.get("classname")),
        _gradle_evidence_text(row.get("name")),
    )


def assemble_gradle_test_rows(
    suite_summaries: Sequence[Mapping[str, Any]],
    testcase_rows: Sequence[Mapping[str, Any]],
    *,
    summary_cap: int = GRADLE_SUITE_SUMMARY_CAP,
    row_cap: int = GRADLE_TESTCASE_ROW_CAP,
    harvested_files: Optional[Sequence[str]] = None,
    unsummarized_files: int = 0,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Fold one Gradle harvest into `(summary_section, rows, disclosures)`.

    Pure: no container, no clock, no receipt identity. `suite_summaries` is one
    entry PER REPORT FILE — `{module, task, tests, failures, errors, skipped}`
    read from that file's `<testsuite>` root — and this folds them into the
    (module, task) totals the receipt carries, counting the files it summed.
    `testcase_rows` are the parser's raw rows; the rows returned here are the
    same objects in the same shape, only ordered and bounded, so identity
    sealing and `validate_testcase_execution_row` stay the single row contract.

    `unsummarized_files` is what a discovery bound left out entirely — reports
    that exist and were never summed. It is carried on the summary section and
    it withdraws the red-completeness claim, because a red can be hiding in a
    report nobody read.

    `harvested_files` is every report the identity pass was ASKED to cover.
    Without it `dropped_files` can only count reports whose rows the cap threw
    away, which understates the loss whenever a file-level bound stopped a
    report from being read at all — kafka's 1,176 reports against a 50-file tag
    bound is exactly that case, and a disclosure that reported 0 dropped files
    there would be false. With it, the count means what it says: reports whose
    identities this sample does not carry.

    Truncation is red-first: a failure or error identity keeps its slot while
    passing and skipped rows lose theirs, and every drop is counted. A loss
    that happened INSIDE a file — its bounded read ended before its last
    testcase — is counted too, as `unread_rows`, against the total that file's
    own `<testsuite>` root declares; it needs `path` on the summary entries,
    which is how the harvest states them.

    Absence is absence — a harvest that found nothing returns `(None, [], None)`
    and the caller records an evidence omission rather than a zero.
    """

    summary_cap = (
        summary_cap
        if isinstance(summary_cap, int) and not isinstance(summary_cap, bool) and summary_cap > 0
        else GRADLE_SUITE_SUMMARY_CAP
    )
    row_cap = (
        row_cap
        if isinstance(row_cap, int) and not isinstance(row_cap, bool) and row_cap > 0
        else GRADLE_TESTCASE_ROW_CAP
    )

    unsummarized = (
        unsummarized_files
        if isinstance(unsummarized_files, int)
        and not isinstance(unsummarized_files, bool)
        and unsummarized_files > 0
        else 0
    )
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    # `path -> tests the file's own <testsuite> root declares`, kept per FILE
    # because that is the unit the per-file read bound cuts. Callers that state
    # no path (the summaries alone are the measurement) simply never appear.
    declared_by_file: Dict[str, int] = {}
    unreadable = 0
    for entry in suite_summaries or ():
        counts = (
            {field: _gradle_summary_count(entry.get(field)) for field in _GRADLE_SUITE_COUNT_FIELDS}
            if isinstance(entry, Mapping)
            else {}
        )
        module = _gradle_evidence_text(entry.get("module")) if isinstance(entry, Mapping) else ""
        task = _gradle_evidence_text(entry.get("task")) if isinstance(entry, Mapping) else ""
        if not module or not task or any(count is None for count in counts.values()) or not counts:
            unreadable += 1
            continue
        bucket = groups.setdefault(
            (module, task),
            {"module": module, "task": task, "xml_files": 0, **{f: 0 for f in counts}},
        )
        bucket["xml_files"] += 1
        for field, count in counts.items():
            bucket[field] += count
        report_path = _gradle_evidence_text(entry.get("path"))
        if report_path:
            declared_by_file[report_path] = declared_by_file.get(report_path, 0) + counts["tests"]

    declared_red = sum(group["failures"] + group["errors"] for group in groups.values())
    # Red-bearing pairs are the ones a truncated summary must keep; ties break
    # on the pair itself so the selection never depends on harvest order.
    ranked = sorted(
        groups.values(),
        key=lambda group: (
            0 if group["failures"] + group["errors"] else 1,
            group["module"],
            group["task"],
        ),
    )
    kept_groups = ranked[:summary_cap]
    dropped_suites = len(ranked) - len(kept_groups)
    summary_section: Optional[Dict[str, Any]] = None
    if kept_groups:
        summary_section = {
            "suites": sorted(kept_groups, key=lambda group: (group["module"], group["task"]))
        }
        if dropped_suites:
            summary_section["truncated"] = True
            summary_section["dropped_suites"] = dropped_suites
        if unreadable:
            summary_section["unreadable_suites"] = unreadable
        if unsummarized:
            summary_section["unsummarized_files"] = unsummarized

    red: List[Mapping[str, Any]] = []
    green: List[Mapping[str, Any]] = []
    unclassified: List[Any] = []
    for row in testcase_rows or ():
        if not isinstance(row, Mapping):
            unclassified.append(row)
            continue
        outcome = _gradle_evidence_text(row.get("outcome")).lower()
        if outcome in _GRADLE_RED_OUTCOMES:
            red.append(row)
        elif outcome in _GRADLE_GREEN_OUTCOMES:
            green.append(row)
        else:
            unclassified.append(row)
    red.sort(key=_gradle_row_sort_key)
    green.sort(key=_gradle_row_sort_key)
    ordered = [*red, *green, *unclassified]
    kept_rows = list(ordered[:row_cap])
    dropped_red = max(0, len(red) - row_cap)
    # Every dropped row that is not a red: passing, skipped, and any row whose
    # outcome the parser did not state (which could never have been sealed).
    dropped_green = len(ordered) - len(kept_rows) - dropped_red
    covered_files = {
        str(row.get("report_path") or "") for row in ordered if isinstance(row, Mapping)
    } - {""}
    # A report the identity pass never reached is a report this sample does not
    # speak for, exactly like one whose every row lost its slot.
    covered_files |= {str(path or "").strip() for path in harvested_files or ()} - {""}
    kept_files = {
        str(row.get("report_path") or "") for row in kept_rows if isinstance(row, Mapping)
    } - {""}
    dropped_files = len(covered_files - kept_files)

    # Identities lost INSIDE a file the sample does speak for: the per-file tag
    # bound stops the read partway (kafka's 400-token bound against a report
    # declaring 1,000 tests), so rows the fold never saw can neither be dropped
    # greens — nothing built them — nor a dropped file, since the report is
    # represented. Without this they are a silent cap, which the receipt schema
    # exists to forbid. Measured against the file's own declared total, and only
    # for files that DID contribute rows: a file that contributed none is
    # already wholly disclosed by `dropped_files`.
    parsed_by_file: Dict[str, int] = {}
    for row in ordered:
        if not isinstance(row, Mapping):
            continue
        report_path = str(row.get("report_path") or "").strip()
        if report_path:
            parsed_by_file[report_path] = parsed_by_file.get(report_path, 0) + 1
    unread_rows = sum(
        max(0, declared_by_file[report_path] - parsed)
        for report_path, parsed in parsed_by_file.items()
        if report_path in declared_by_file
    )

    disclosures: Optional[Dict[str, Any]] = None
    if groups or ordered:
        disclosures = {
            "rows_source": GRADLE_ROWS_SOURCE,
            # Completeness is a claim about reds, and it needs a witness: the
            # summaries state how many reds the reports declared. With no
            # readable summary — or with one the fold could not read whole —
            # there is no witness, so no completeness is claimed.
            "red_rows_complete": bool(groups)
            and not unreadable
            and not unsummarized
            and not dropped_red
            and min(len(red), row_cap) >= declared_red,
        }
        if dropped_green or dropped_red or dropped_files or unread_rows:
            disclosures["rows_truncated"] = {
                "dropped_green": dropped_green,
                "dropped_files": dropped_files,
                **({"dropped_red": dropped_red} if dropped_red else {}),
                **({"unread_rows": unread_rows} if unread_rows else {}),
            }
    return summary_section, kept_rows, disclosures


def _validate_evidence_omissions(value: Any, *, receipt: Mapping[str, Any]) -> None:
    """Each entry names one observability field this receipt could not carry.

    An omission is a STATEMENT, not a shrug: it names the field, marks it
    unavailable, and gives the refusal that produced it. An omission naming a
    field the receipt does carry is a contradiction and is refused.
    """

    if not isinstance(value, list) or not value or len(value) > RECEIPT_SEQUENCE_MAX_ITEMS:
        raise ValueError("receipt evidence_omissions is invalid")
    named = set()
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"field", "status", "reasons"}:
            raise ValueError("receipt evidence_omissions entry shape is invalid")
        field = _receipt_text(entry.get("field"), "evidence_omissions.field")
        if field not in _RECEIPT_OMITTABLE_EVIDENCE_FIELDS:
            raise ValueError("receipt evidence_omissions names a field that cannot be omitted")
        if field in receipt:
            raise ValueError("receipt evidence_omissions contradicts a field the receipt carries")
        if entry.get("status") != "unavailable":
            raise ValueError("receipt evidence_omissions status must be unavailable")
        reasons = entry.get("reasons")
        if not isinstance(reasons, list) or not reasons:
            raise ValueError("receipt evidence_omissions states no reason")
        _receipt_text_list(reasons, "evidence_omissions.reasons")
        if field in named:
            raise ValueError("receipt evidence_omissions duplicate a field")
        named.add(field)


def _validate_testcase_envelope(
    value: Any,
    *,
    receipt: Mapping[str, Any],
    report_claims: set[tuple[str, str]],
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("receipt testcase_execution_rows must be an object")
    if set(value) - {"schema_version", "status", "report_count", "rows", "reasons"}:
        raise ValueError("receipt testcase_execution_rows has unknown fields")
    if value.get("schema_version") != 2:
        raise ValueError("receipt testcase_execution_rows schema is invalid")
    status = value.get("status")
    rows = value.get("rows")
    count = value.get("report_count")
    if status not in {"complete", "unavailable"} or not isinstance(rows, list):
        raise ValueError("receipt testcase_execution_rows status/rows are invalid")
    if type(count) is not int or count < 0:
        raise ValueError("receipt testcase_execution_rows report_count is invalid")
    reasons = value.get("reasons", [])
    _receipt_text_list(reasons, "testcase_execution_rows.reasons")
    if status == "unavailable":
        if rows or not reasons:
            raise ValueError(
                "receipt testcase_execution_rows unavailable envelope needs stated reasons"
            )
        return
    if reasons:
        raise ValueError("receipt testcase_execution_rows complete envelope cannot state reasons")
    for raw in rows:
        try:
            normalized = validate_testcase_execution_row(
                raw,
                receipt_id=receipt.get("receipt_id"),
                run_id=receipt.get("run_id"),
                target_sha=receipt.get("target_sha"),
                domain_id=receipt.get("domain_id"),
                report_claims=report_claims,
            )
        except TestcaseRowContractError as exc:
            raise ValueError(f"receipt testcase_execution_rows row is invalid: {exc}") from exc
        if normalized != raw:
            raise ValueError("receipt testcase_execution_rows row is not canonical")


def validate_receipt_v2(
    payload: Mapping[str, Any],
    *,
    expected_id: Optional[str] = None,
    live: bool = True,
) -> Dict[str, Any]:
    """Return one strict canonical receipt or raise ``ValueError``.

    Hash recomputation and JSON validity are not schema validation. Every live
    reader and writer calls this function so malformed nested evidence cannot
    become a positive assessment merely because it is truthy.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("receipt must be an object")
    receipt = dict(payload)
    schema = receipt.get("schema_version")
    if schema == 1 and not live:
        if set(receipt) != _RECEIPT_V1_FIELDS:
            raise ValueError("historical receipt fields are not exact")
        identifier = _receipt_text(receipt.get("receipt_id"), "receipt_id")
        if _SAFE_RECEIPT_ID_RE.fullmatch(identifier) is None:
            raise ValueError("historical receipt id is unsafe")
        if expected_id is not None and identifier != expected_id:
            raise ValueError("receipt filename does not match receipt_id")
        tool = _receipt_text(receipt.get("tool"), "tool", lowercase=True)
        if tool not in RUNNER_DEFAULTS and tool != "bash":
            raise ValueError("historical receipt tool is not registered")
        _receipt_text(receipt.get("requested_action"), "requested_action", allow_empty=True)
        _receipt_text(receipt.get("effective_action"), "effective_action")
        _receipt_text(
            receipt.get("argv"),
            "argv",
            maximum_bytes=RECEIPT_ARGV_MAX_BYTES,
            allow_linebreaks=True,
        )
        root = _receipt_text(receipt.get("working_directory"), "working_directory")
        if not root.startswith("/") or posixpath.normpath(root) != root:
            raise ValueError("historical receipt working_directory is not canonical")
        exit_code = receipt.get("exit_code")
        if type(exit_code) is not int:
            raise ValueError("historical receipt exit_code must be an integer")
        outcome = receipt.get("outcome")
        if outcome != ("completed" if exit_code == 0 else "failed"):
            raise ValueError("historical receipt outcome contradicts exit_code")
        _validate_report_delta(receipt.get("report_delta"))
        canonical = json.dumps(
            receipt,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(canonical.encode("utf-8")) > RECEIPT_MAX_CANONICAL_BYTES:
            raise ValueError("historical receipt exceeds its canonical byte limit")
        return receipt
    if type(schema) is not int or schema != RECEIPT_SCHEMA_VERSION:
        raise ValueError("receipt schema_version must be v2 when live")
    unknown = set(receipt) - _RECEIPT_V2_FIELDS
    missing = _RECEIPT_V2_REQUIRED_FIELDS - set(receipt)
    if unknown or missing:
        raise ValueError(f"receipt v2 fields are not exact: unknown={unknown} missing={missing}")

    identifier = _receipt_text(receipt.get("receipt_id"), "receipt_id")
    if _SAFE_RECEIPT_ID_RE.fullmatch(identifier) is None:
        raise ValueError("receipt id is unsafe")
    if expected_id is not None:
        expected = _receipt_text(expected_id, "expected receipt_id")
        if _SAFE_RECEIPT_ID_RE.fullmatch(expected) is None or identifier != expected:
            raise ValueError("receipt filename does not match receipt_id")
    _receipt_text(receipt.get("run_id"), "run_id")
    tool = _receipt_text(receipt.get("tool"), "tool", lowercase=True)
    if tool not in RUNNER_DEFAULTS and tool != "bash":
        raise ValueError("receipt tool is not registered")
    _receipt_text(receipt.get("requested_action"), "requested_action", allow_empty=True)
    _receipt_text(receipt.get("effective_action"), "effective_action")
    _receipt_text(
        receipt.get("argv"),
        "argv",
        maximum_bytes=RECEIPT_ARGV_MAX_BYTES,
        allow_linebreaks=True,
    )
    for field in ("working_directory", "actual_cwd"):
        root = _receipt_text(receipt.get(field), field)
        if not root.startswith("/") or posixpath.normpath(root) != root:
            raise ValueError(f"receipt {field} must be absolute and canonical")
    outcome = receipt.get("outcome")
    if outcome not in {"completed", "failed"}:
        raise ValueError("receipt outcome is invalid")
    if "exit_code" in receipt:
        exit_code = receipt.get("exit_code")
        if type(exit_code) is not int:
            raise ValueError("receipt exit_code must be an integer")
        expected_outcome = "completed" if exit_code == 0 else "failed"
        if outcome != expected_outcome:
            raise ValueError("receipt outcome contradicts exit_code")

    report_claims = _validate_report_delta(receipt.get("report_delta"))
    for field in (
        "lifecycle_state",
        "termination_reason",
        "contract_id",
        "contract_hash",
        "execution_binding",
        "compliance",
        "survey_fingerprint",
        "config_fingerprint",
        "document_map_fingerprint",
        "domain_id",
        "output_content_hash",
    ):
        if field in receipt:
            _receipt_text(receipt.get(field), field)
    if (
        "contract_id" in receipt
        and re.fullmatch(r"ic-[0-9a-f]{12}", receipt["contract_id"]) is None
    ):
        raise ValueError("receipt contract_id is invalid")
    for field in ("contract_hash", "output_content_hash"):
        if field in receipt and _SHA256_RE.fullmatch(receipt[field]) is None:
            raise ValueError(f"receipt {field} is not a sha256 digest")
    binding_fields = {"contract_id", "contract_hash", "execution_binding"}
    if set(receipt).intersection(binding_fields) not in (set(), binding_fields):
        raise ValueError("receipt contract binding must be one complete tuple")
    if "execution_binding" in receipt and receipt["execution_binding"] not in {
        "argv_v1",
        "python_facade_v1",
    }:
        raise ValueError("receipt execution_binding is invalid")
    if "compliance" in receipt:
        if receipt.get("execution_binding") != "argv_v1" or receipt["compliance"] not in {
            "exact",
            "equivalent",
            "deviated",
        }:
            raise ValueError("receipt compliance is invalid for its binding")
    if "target_sha" in receipt:
        target = _receipt_text(receipt.get("target_sha"), "target_sha", lowercase=True)
        if _OBJECT_NAME_RE.fullmatch(target) is None:
            raise ValueError("receipt target_sha is invalid")
    if "fact_epoch" in receipt:
        epoch = receipt.get("fact_epoch")
        if type(epoch) is not int or epoch <= 0:
            raise ValueError("receipt fact_epoch must be a positive integer")

    if "toolchain_fingerprint" in receipt:
        fingerprint = receipt.get("toolchain_fingerprint")
        if not isinstance(fingerprint, Mapping) or not fingerprint:
            raise ValueError("receipt toolchain_fingerprint is invalid")
        if set(fingerprint) - {"executable", "version"}:
            raise ValueError("receipt toolchain_fingerprint has unknown fields")
        for key, value in fingerprint.items():
            _receipt_text(value, f"toolchain_fingerprint.{key}")
    if "effective_jdk" in receipt:
        runtime = receipt.get("effective_jdk")
        if not isinstance(runtime, Mapping) or not runtime:
            raise ValueError("receipt effective_jdk is invalid")
        if set(runtime) - {
            "major",
            "requirement_major",
            "requirement_authority",
            "runtime_authority",
            "provenance",
        }:
            raise ValueError("receipt effective_jdk has unknown fields")
        for key, value in runtime.items():
            if key == "provenance":
                if not isinstance(value, Mapping) or not value:
                    raise ValueError("receipt effective_jdk provenance is invalid")
                json.dumps(value, allow_nan=False, sort_keys=True)
            else:
                _receipt_text(value, f"effective_jdk.{key}")

    producer_fields = {
        "producer_sequence",
        "producer_observations",
        "producer_observations_sha256",
    }
    present_producer = set(receipt).intersection(producer_fields)
    if present_producer not in (set(), producer_fields):
        raise ValueError("receipt producer observation binding is incomplete")
    if present_producer:
        sequence = receipt.get("producer_sequence")
        if type(sequence) is not int or sequence != producer_sequence_from_receipt_id(identifier):
            raise ValueError("receipt producer_sequence is invalid")
        normalized = normalize_producer_observations(receipt.get("producer_observations"))
        if normalized != receipt.get("producer_observations"):
            raise ValueError("receipt producer observations are not canonical")
        digest = _receipt_text(
            receipt.get("producer_observations_sha256"),
            "producer_observations_sha256",
            lowercase=True,
        )
        if digest != producer_observations_sha256(normalized):
            raise ValueError("receipt producer observation hash is invalid")

    if "testcase_outcomes" in receipt:
        _validate_testcase_outcomes(receipt.get("testcase_outcomes"))
    if "testcase_execution_rows" in receipt:
        _validate_testcase_envelope(
            receipt.get("testcase_execution_rows"),
            receipt=receipt,
            report_claims=report_claims,
        )
    if "gradle_suite_summaries" in receipt:
        _validate_gradle_suite_summaries(receipt.get("gradle_suite_summaries"), receipt=receipt)
    if "gradle_row_disclosure" in receipt:
        _validate_gradle_row_disclosure(receipt.get("gradle_row_disclosure"), receipt=receipt)
    if "capability_observations" in receipt:
        _validate_capability_observations(receipt.get("capability_observations"))
    if "module_outcomes" in receipt:
        _validate_module_outcomes(receipt.get("module_outcomes"))
    if "excluded_claimed_paths" in receipt:
        excluded = receipt.get("excluded_claimed_paths")
        if type(excluded) is not int or excluded <= 0:
            raise ValueError("receipt excluded_claimed_paths must be positive")
    if "evidence_omissions" in receipt:
        _validate_evidence_omissions(receipt.get("evidence_omissions"), receipt=receipt)

    canonical = json.dumps(
        receipt, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    if len(canonical.encode("utf-8")) > RECEIPT_MAX_CANONICAL_BYTES:
        raise ValueError("receipt exceeds its canonical byte limit")
    return receipt


def receipt_record_scope(
    payload: Mapping[str, Any],
    current_run_id: Optional[str],
) -> str:
    """Classify only a fully validated old-run receipt outside live authority.

    Unknown, malformed and future schemas intentionally remain ``current`` so
    the strict live reader rejects the ledger.  A valid schema-v1 receipt is
    forensic-only; a valid v2 receipt is foreign only when both the host and
    the record state distinct non-empty run identities.
    """

    if not isinstance(payload, Mapping):
        return "current"
    schema = payload.get("schema_version")
    if schema == 1:
        try:
            validate_receipt_v2(payload, live=False)
        except (TypeError, ValueError):
            return "current"
        return "forensic"
    if schema != RECEIPT_SCHEMA_VERSION:
        return "current"
    try:
        validated = validate_receipt_v2(payload)
    except (TypeError, ValueError):
        return "current"
    current = str(current_run_id or "").strip()
    recorded = str(validated.get("run_id") or "").strip()
    if current and recorded and recorded != current:
        return "foreign"
    return "current"


def build_receipt(
    *,
    receipt_id: str,
    run_id: Optional[str] = None,
    tool: str,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    exit_code: Optional[int],
    before: Mapping[str, str],
    after: Mapping[str, str],
    lifecycle_state: Optional[str] = None,
    termination_reason: Optional[str] = None,
    target_sha: Optional[str] = None,
    survey_fingerprint: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
    document_map_fingerprint: Optional[str] = None,
    fact_epoch: Optional[int] = None,
    domain_id: Optional[str] = None,
    actual_cwd: Optional[str] = None,
    toolchain_fingerprint: Optional[Mapping[str, str]] = None,
    output_content_hash: Optional[str] = None,
    testcase_outcomes: Optional[Mapping[str, Any]] = None,
    testcase_execution_rows: Optional[Mapping[str, Any]] = None,
    gradle_suite_summaries: Optional[Mapping[str, Any]] = None,
    gradle_row_disclosure: Optional[Mapping[str, Any]] = None,
    declared_omissions: Optional[Sequence[Mapping[str, Any]]] = None,
    contract_id: Optional[str] = None,
    contract_hash: Optional[str] = None,
    execution_binding: Optional[str] = None,
    compliance: Optional[str] = None,
    capability_observations: Optional[Sequence[Mapping[str, Any]]] = None,
    module_outcomes: Optional[Sequence[Mapping[str, Any]]] = None,
    cached_report_roots: Optional[Iterable[str]] = None,
    excluded_claimed_paths: Optional[int] = None,
    effective_jdk: Optional[Mapping[str, Any]] = None,
    producer_observations: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a schema-v2 receipt. Absent facts serialize as absent keys.

    The v1 block below is frozen: names, shapes and order stay exactly as
    Plan 5 wrote them, because the validator and the phase gate read them.
    """
    receipt: Dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "run_id": str(run_id or "").strip() or active_receipt_run_id(),
        "tool": tool,
        "requested_action": requested_action,
        "effective_action": effective_action,
        "argv": argv,
        "working_directory": working_directory,
    }
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        receipt["exit_code"] = exit_code
    receipt["outcome"] = "completed" if exit_code == 0 else "failed"
    receipt["report_delta"] = report_delta(before, after, cached_report_roots)
    # v2 (spec §C4). `actual_cwd` is the directory the dispatch physically
    # used; `contract_id`/`contract_hash` bind this receipt to the contract the
    # facade froze BEFORE the dispatch, and `compliance` is that comparison's
    # verdict (invocation_contracts.compliance_class). A dispatch with no
    # frozen contract states none of the three — a receipt never claims
    # compliance with a contract that does not exist.
    # HOW the dispatch ended, which the exit code alone cannot say: a detached
    # job whose process vanished carries a SYNTHESIZED exit code (orch's
    # `collect_detached_result`) and a log truncated at the kill, and a dispatch
    # a timeout monitor stopped states its reason. Both were invisible on the
    # receipt, so a reader could not tell a recorded terminal status from a
    # manufactured one — and `receipt_structure` promoted a partial module list
    # as receipt-proven fact. Absent when the dispatch returned in band, which
    # IS the process's own status.
    for key, value in (
        ("lifecycle_state", lifecycle_state),
        ("termination_reason", termination_reason),
        ("actual_cwd", actual_cwd or working_directory),
        ("contract_id", contract_id),
        ("contract_hash", contract_hash),
        ("execution_binding", execution_binding),
        ("compliance", compliance),
        ("target_sha", target_sha),
        ("survey_fingerprint", survey_fingerprint),
        ("config_fingerprint", config_fingerprint),
        ("document_map_fingerprint", document_map_fingerprint),
        ("domain_id", domain_id),
        ("output_content_hash", output_content_hash),
    ):
        text = str(value).strip() if value is not None else ""
        if text:
            receipt[key] = text
    if fact_epoch is not None:
        if type(fact_epoch) is not int or fact_epoch <= 0:
            raise ValueError("receipt fact_epoch must be a positive integer")
        receipt["fact_epoch"] = fact_epoch
    if toolchain_fingerprint:
        receipt["toolchain_fingerprint"] = dict(toolchain_fingerprint)
    if isinstance(effective_jdk, Mapping) and effective_jdk:
        receipt["effective_jdk"] = dict(effective_jdk)
    if producer_observations is not None:
        producer_sequence = producer_sequence_from_receipt_id(receipt_id)
        if producer_sequence is None:
            raise ValueError("producer receipt id has no explicit monotonic sequence")
        normalized_observations = normalize_producer_observations(producer_observations)
        receipt["producer_sequence"] = producer_sequence
        receipt["producer_observations"] = normalized_observations
        receipt["producer_observations_sha256"] = producer_observations_sha256(
            normalized_observations
        )
    # Every OPTIONAL evidence field below is attached through one gate that
    # validates it FIRST. A field the receipt cannot carry is dropped alone and
    # its refusal recorded in `evidence_omissions` — never allowed to reach
    # `write_receipt_result`, where a single raise voids the whole receipt and
    # takes the exit code, the argv, the contract binding and the report delta
    # down with it (live camel and kafka, both on the detached path, both
    # reported as a bare `invalid_arguments`).
    omissions: List[Dict[str, Any]] = []
    # The row envelope is checked against THIS receipt's own claims. A delta
    # the writer will refuse anyway is not attributed to the envelope: the
    # write-time refusal already names `report_delta`.
    try:
        attached_report_claims = _validate_report_delta(receipt["report_delta"])
    except (TypeError, ValueError):
        attached_report_claims = set()

    def _attach(field: str, value: Any, validator: Callable[[Any], Any]) -> None:
        try:
            validator(value)
        except (TypeError, ValueError) as exc:
            logger.debug(f"receipt evidence field {field} is unrepresentable: {exc}")
            omissions.append(
                {
                    "field": field,
                    "status": "unavailable",
                    "reasons": [_omission_reason(exc)],
                }
            )
            return
        receipt[field] = value

    if testcase_outcomes:
        _attach("testcase_outcomes", dict(testcase_outcomes), _validate_testcase_outcomes)
    if testcase_execution_rows:
        # Exact, module-qualified physical rows parsed while the report bytes
        # still match this receipt's delta.  This is separate from the bounded
        # diagnostic ``testcase_outcomes`` list above: metrics must never turn
        # a 50-row diagnostic sample into a project-wide identity rollup.
        _attach(
            "testcase_execution_rows",
            dict(testcase_execution_rows),
            lambda value: _validate_testcase_envelope(
                value,
                receipt=receipt,
                report_claims=attached_report_claims,
            ),
        )
    # Gradle test evidence (evidence study 2026-08-30): the complete per
    # (project, task-dir) totals, and what the bounded identity harvest beside
    # them had to drop. Both are attached through the same gate as every other
    # observability field — a summary the receipt cannot carry becomes a stated
    # omission, never a silent absence and never a voided receipt.
    # The copy is conditional so that a value which is not a mapping at all
    # reaches the VALIDATOR (and becomes a stated omission) instead of raising
    # out of `dict()` here, where nothing would catch it and the whole receipt
    # would be lost to a field that only describes it.
    summaries = (
        dict(gradle_suite_summaries)
        if isinstance(gradle_suite_summaries, Mapping)
        else gradle_suite_summaries
    )
    if summaries:
        _attach(
            "gradle_suite_summaries",
            summaries,
            lambda value: _validate_gradle_suite_summaries(value, receipt=receipt),
        )
    disclosure = (
        dict(gradle_row_disclosure)
        if isinstance(gradle_row_disclosure, Mapping)
        else gradle_row_disclosure
    )
    if disclosure:
        _attach(
            "gradle_row_disclosure",
            disclosure,
            lambda value: _validate_gradle_row_disclosure(value, receipt=receipt),
        )
    # Spec §C8: what a PHYSICAL probe observed about a resolved capability.
    # A dispatch that probed nothing states nothing — the key is absent, never
    # an empty list, because "no capability was probed" and "a probe found
    # nothing" are different facts.
    observations = [
        {str(key): str(value) for key, value in dict(entry).items()}
        for entry in capability_observations or ()
        if isinstance(entry, Mapping) and entry
    ]
    if observations:
        _attach("capability_observations", observations, _validate_capability_observations)
    # What THIS invocation attempted, module by module, in the build system's
    # own words (Maven's reactor summary; the modules whose tasks Gradle ran).
    # The coverage denominator is built from this: a module the build never
    # tried is untried, not missing, and counting it as missing is how a
    # scoped build (`-pl`) or a reactor that stopped early looks like a
    # catastrophe. Absent when the runner stated nothing.
    # `tests_reported` rides along when the runner PROVED the module executed
    # tests (its reports declare their own totals); Gradle's task stream alone
    # can never say that, and `attempted` had to stand in for both.
    modules = [
        {
            **{str(key): entry[key] for key in ("module", "status") if key in entry},
            **({"tests_reported": entry["tests_reported"]} if "tests_reported" in entry else {}),
        }
        for entry in module_outcomes or ()
        if isinstance(entry, Mapping) and entry.get("module")
    ]
    if modules:
        _attach("module_outcomes", modules, _validate_module_outcomes)
    # Plan 8 §3.2. A dispatch that settled LATE states how much of its own
    # write window an intervening receipt had already claimed — first claim
    # wins, and the loss is counted rather than hidden. Absent (never zero) on
    # every synchronous receipt and on every settlement with no interleaving,
    # so the settled path stays field-for-field the synchronous one.
    if isinstance(excluded_claimed_paths, int) and not isinstance(excluded_claimed_paths, bool):
        if excluded_claimed_paths > 0:
            receipt["excluded_claimed_paths"] = excluded_claimed_paths
    # An omission the RUNNER states, for evidence the schema never got the
    # chance to refuse. ofbiz-plugins ran its test task and left no report XML
    # at all: nothing reached `_attach`, so nothing above can speak, and a
    # receipt that simply lacks the field is indistinguishable from one whose
    # harvest never ran. The vocabulary is closed and the field must be an
    # omittable one, so this stays engine evidence — a runner may name which
    # measurement it could not make, never write prose into the receipt.
    stated = {entry["field"] for entry in omissions}
    for entry in declared_omissions or ():
        if not isinstance(entry, Mapping):
            continue
        field = str(entry.get("field") or "").strip()
        reasons = sorted(
            {
                str(reason).strip()
                for reason in entry.get("reasons") or ()
                if str(reason).strip() in DECLARED_OMISSION_REASONS
            }
        )
        if not reasons or field in stated or field in receipt:
            continue
        if field not in _RECEIPT_OMITTABLE_EVIDENCE_FIELDS:
            continue
        omissions.append({"field": field, "status": "unavailable", "reasons": reasons})
        stated.add(field)
    if omissions:
        receipt["evidence_omissions"] = sorted(omissions, key=lambda entry: entry["field"])
    return receipt


def write_receipt(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> bool:
    """Compatibility wrapper for callers that only need a persistence bool."""
    return write_receipt_result(execute, receipt).persisted


def write_receipt_result(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> ContainerWriteResult:
    """Persist one complete receipt through the bounded atomic transport.

    Never raises: the caller is mid-invocation and owes the model a result.
    The typed code describes evidence persistence only; it never rewrites the
    already-observed runner outcome.
    """
    try:
        validated = validate_receipt_v2(receipt or {})
        receipt_id = validated["receipt_id"]
        body = json.dumps(validated, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"invocation receipt is invalid: {exc}")
        return ContainerWriteResult(False, receipt_refusal_code(exc))
    final = f"{RECEIPT_DIR}/{receipt_id}.json"
    try:
        result = compare_publish_container_text_atomic(
            execute,
            final,
            body,
            expected_content=None,
            validate_json=True,
        )
    except Exception as exc:
        logger.debug(f"invocation receipt {receipt_id} not persisted: {exc}")
        return ContainerWriteResult(False, "transport_write_failed")
    if result.code != WRITE_COMPARE_CONFLICT:
        return _publish_receipt_result(execute, validated, body, result)

    # Another writer won the absent->present race. Receipt ids are immutable:
    # the only legal replay is byte-identical canonical JSON. Revalidate the
    # complete target bytes rather than reading/truncating a potentially huge
    # receipt through the host transport.
    payload = body.encode("utf-8")
    expected_bytes = len(payload)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    program = (
        'import hashlib,sys;data=open(sys.argv[1],"rb").read();'
        "raise SystemExit(len(data)!=int(sys.argv[2]) or "
        "hashlib.sha256(data).hexdigest()!=sys.argv[3])"
    )
    command = (
        f"python3 -c {shlex.quote(program)} {shlex.quote(final)} "
        f"{expected_bytes} {expected_sha256}"
    )
    executor = getattr(execute, "execute_command", execute)
    try:
        verification = executor(command) or {}
    except Exception as exc:
        logger.debug(f"invocation receipt {receipt_id} collision is unreadable: {exc}")
        return ContainerWriteResult(False, RECEIPT_ID_COLLISION)
    exit_code = verification.get("exit_code") if isinstance(verification, Mapping) else None
    succeeded = (
        not verification.get("dispatch_status")
        and verification.get("success") is not False
        and (
            (type(exit_code) is int and exit_code == 0)
            or (exit_code is None and verification.get("success") is True)
        )
    )
    if succeeded:
        identical = ContainerWriteResult(
            True,
            "persisted",
            bytes_written=expected_bytes,
            sha256=expected_sha256,
        )
        return _publish_receipt_result(execute, validated, body, identical)
    return ContainerWriteResult(False, RECEIPT_ID_COLLISION)


def _publish_receipt_result(
    execute: Any,
    receipt: Mapping[str, Any],
    body: str,
    result: ContainerWriteResult,
) -> ContainerWriteResult:
    """Turn a container commit into live evidence only after host publication."""

    if not result.persisted:
        return result
    from sag.agent.evidence_publications import (
        evidence_publication_authority_for,
        publish_evidence_bytes,
    )

    authority = evidence_publication_authority_for(execute)
    if str(getattr(authority, "run_id", None) or "") != str(receipt.get("run_id") or ""):
        logger.warning(
            f"invocation receipt {receipt['receipt_id']} reached the container under a "
            "different host publication run"
        )
        return ContainerWriteResult(
            False,
            HOST_PUBLICATION_FAILED,
            bytes_written=result.bytes_written,
            sha256=result.sha256,
        )

    publication = publish_evidence_bytes(
        execute,
        record_kind="invocation_receipt",
        record_id=str(receipt["receipt_id"]),
        raw=body.encode("utf-8"),
        contract_id=receipt.get("contract_id"),
        contract_hash=receipt.get("contract_hash"),
    )
    if publication.published:
        return result
    logger.warning(
        f"invocation receipt {receipt['receipt_id']} reached the container but host "
        f"publication failed: {publication.status}"
    )
    return ContainerWriteResult(
        False,
        HOST_PUBLICATION_FAILED,
        bytes_written=result.bytes_written,
        sha256=result.sha256,
    )


def record_invocation(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    receipt_id: Optional[str] = None,
    run_id: Optional[str] = None,
    tool: str,
    attempt: Any,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    exit_code: Optional[int],
    before: Mapping[str, str],
    after: Mapping[str, str],
    lifecycle_state: Optional[str] = None,
    termination_reason: Optional[str] = None,
    output: Optional[str] = None,
    requirements: Optional[Mapping[str, Any]] = None,
    contract_id: Optional[str] = None,
    contract_hash: Optional[str] = None,
    execution_binding: Optional[str] = None,
    compliance: Optional[str] = None,
    capability_observations: Optional[Sequence[Mapping[str, Any]]] = None,
    module_outcomes: Optional[Sequence[Mapping[str, Any]]] = None,
    gradle_suite_summaries: Optional[Mapping[str, Any]] = None,
    gradle_row_disclosure: Optional[Mapping[str, Any]] = None,
    harvested_testcase_outcomes: Optional[Mapping[str, Any]] = None,
    declared_omissions: Optional[Sequence[Mapping[str, Any]]] = None,
    cached_report_roots: Optional[Iterable[str]] = None,
    excluded_claimed_paths: Optional[int] = None,
    effective_jdk: Optional[Mapping[str, Any]] = None,
    producer_observations: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist the receipt for one runner call; return its ToolResult metadata.

    The v2 facts are collected HERE, after the dispatch the caller already
    completed: the tree's sha, the runner's toolchain and the per-testcase
    outcomes of the reports this invocation wrote are probed; the survey pins
    and the domain are read from the manifest the CALLER already holds, never
    from a second manifest probe. The contract binding
    (`contract_id`/`contract_hash`/`compliance`) is passed in by the caller,
    which is the only layer that knows both the frozen contract and the argv it
    physically ran. Every fact degrades to an absent key — none of them can
    fail the build the model is waiting for.

    A persisted receipt adds `receipt_id`. A failed write preserves the
    already-observed runner result and adds an evidence-only persistence fact
    plus a typed failure code.
    """
    normalized_producer_observations: Optional[Dict[str, Any]] = None
    if producer_observations is not None:
        try:
            normalized_producer_observations = normalize_producer_observations(
                producer_observations
            )
        except (TypeError, ValueError) as exc:
            logger.debug(f"producer observations refused before receipt I/O: {exc}")
            return {
                "receipt_persisted": False,
                "receipt_persistence_code": "producer_observations_invalid",
            }
    resolved_receipt_id = str(receipt_id or "").strip() or next_receipt_id(tool, attempt)
    resolved_run_id = str(run_id or "").strip() or active_receipt_run_id()
    resolved_target_sha = target_sha(execute, working_directory)
    resolved_domain_id = nearest_domain_root(requirements, working_directory)
    resolved_fact_epoch = nearest_domain_fact_epoch(requirements, working_directory)
    resolved_delta = report_delta(before, after, cached_report_roots)
    parsed_rows = read_delta_testcase_rows(
        execute,
        receipt_id=resolved_receipt_id,
        delta=resolved_delta,
    )
    gradle_project_map = (
        read_gradle_project_map(execute, resolved_domain_id) if tool == "gradle" else None
    )
    sealed_rows = seal_testcase_execution_rows(
        parsed_rows,
        run_id=resolved_run_id,
        receipt_id=resolved_receipt_id,
        tool=tool,
        target_sha=resolved_target_sha,
        domain_id=resolved_domain_id,
        working_directory=working_directory,
        module_outcomes=module_outcomes or (),
        gradle_project_map=gradle_project_map,
    )
    diagnostic_rows = diagnostic_testcase_outcomes(parsed_rows)
    receipt = build_receipt(
        receipt_id=resolved_receipt_id,
        run_id=resolved_run_id,
        tool=tool,
        requested_action=requested_action,
        effective_action=effective_action,
        argv=argv,
        working_directory=working_directory,
        exit_code=exit_code,
        before=before,
        after=after,
        lifecycle_state=lifecycle_state,
        termination_reason=termination_reason,
        target_sha=resolved_target_sha,
        domain_id=resolved_domain_id,
        fact_epoch=resolved_fact_epoch,
        actual_cwd=working_directory,
        toolchain_fingerprint=toolchain_fingerprint(
            execute,
            executable=runner_executable(argv, tool),
            version_flag=VERSION_FLAGS.get(tool, "--version"),
            working_directory=working_directory,
        ),
        output_content_hash=output_content_hash(output),
        # Precedence, weakest transport last. A harvest that ran states BOTH
        # this list and the `gradle_row_disclosure` beside it, and the two are
        # one measurement: letting a different transport win the list here
        # would leave the receipt disclosing drops from a sample it does not
        # carry. So a stated disclosure BINDS the list to the harvest — even
        # when the harvest folded to no usable row at all. That case (a failed
        # tag transport, empty claims, rows with no name) is an absent key, not
        # an opening for another transport to fill: a truncation record beside
        # a list it never described is contradictory evidence, and unknown is
        # absent here as everywhere else. With no disclosure there is no sample
        # to contradict — the exact hash-verified parse, then the historical
        # unattributed tag read.
        testcase_outcomes=(
            harvested_testcase_outcomes
            if gradle_row_disclosure is not None
            else (diagnostic_rows or read_testcase_outcomes(execute, resolved_delta))
        ),
        testcase_execution_rows=sealed_rows,
        contract_id=contract_id,
        contract_hash=contract_hash,
        execution_binding=execution_binding,
        compliance=compliance,
        capability_observations=capability_observations,
        module_outcomes=module_outcomes,
        # Harvested by the runner that knows its own report layout (the Gradle
        # tool synchronously, the obligation on settlement) and carried through
        # this ONE assembly point, so the detached path cannot diverge from the
        # synchronous one by lacking a field only the caller could supply.
        gradle_suite_summaries=gradle_suite_summaries,
        gradle_row_disclosure=gradle_row_disclosure,
        declared_omissions=declared_omissions,
        cached_report_roots=cached_report_roots,
        excluded_claimed_paths=excluded_claimed_paths,
        effective_jdk=effective_jdk,
        producer_observations=normalized_producer_observations,
        **survey_pins(requirements),
    )
    persistence = write_receipt_result(execute, receipt)
    if persistence.persisted:
        # Plan 8 §3.6: a terminal receipt that named its modules has PROVEN the
        # project's structure, and the survey only proposed it. Promoting here
        # means one writer for both facts — a receipt settled late (§3.2)
        # promotes exactly like a synchronous one, with no second bookkeeping
        # system to keep in step. A receipt that named no modules writes
        # nothing at all.
        promote_structure(execute, receipt)
        return {"receipt_id": receipt["receipt_id"]}
    return {
        "receipt_persisted": False,
        "receipt_persistence_code": persistence.code,
    }


def runner_executable(argv: str, tool: Optional[str] = None) -> Optional[str]:
    """The binary the argv actually launches (`./gradlew`, a venv python, mvn)."""
    text = str(argv or "").strip()
    if text:
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        if tokens:
            return tokens[0]
    return RUNNER_DEFAULTS.get(str(tool or ""))


def _unique_roots(scan_roots: Iterable[str]) -> List[str]:
    roots: List[str] = []
    for raw in scan_roots or ():
        root = str(raw or "").strip()
        if not root:
            continue
        root = root.rstrip("/") or "/"
        if root not in roots:
            roots.append(root)
    return roots


def _parse_sha256sum(output: str) -> Dict[str, str]:
    """`<hash>  <path>` lines; anything else (stderr noise) is ignored."""
    snapshot: Dict[str, str] = {}
    for line in (output or "").splitlines():
        digest, separator, path = line.partition("  ")
        if not separator:
            continue
        # GNU sha256sum escapes newline/backslash filenames with a leading '\'.
        digest = digest.strip().lstrip("\\")
        path = path.strip()
        if not path or len(digest) != 64:
            continue
        try:
            int(digest, 16)
        except ValueError:
            continue
        snapshot[path] = digest
    return snapshot


def _parse_testcase_tags(output: str) -> Tuple[List[Dict[str, str]], int]:
    """The container's tag token stream -> (sorted nodes, nodes seen).

    A tag stream is enough: JUnit puts the outcome in the testcase's child tag
    and the skip reason in that child's `message` attribute, so the report
    bodies never have to cross the transport. A node whose closing tag was cut
    by the per-file bound still closes when the next testcase opens — a
    truncated read reports fewer nodes, never a wrong one.
    """
    nodes: Dict[str, Dict[str, str]] = {}
    pending: Optional[Dict[str, str]] = None

    def close(node: Optional[Dict[str, str]]) -> None:
        if node and node["node_id"] not in nodes:
            nodes[node["node_id"]] = node

    for match in _TESTCASE_TAG_RE.finditer(output or ""):
        closing, tag, attributes = match.group(1), match.group(2), match.group(3)
        self_closing = attributes.rstrip().endswith("/")
        if tag == "testcase":
            close(pending)
            pending = None
            if closing:
                continue
            node_id = _testcase_node_id(attributes)
            if not node_id:
                continue
            pending = {"node_id": node_id, "status": "passed"}
            if self_closing:
                close(pending)
                pending = None
            if len(nodes) >= TESTCASE_PARSE_CAP:
                break
        elif pending is not None:
            if tag == "skipped":
                pending["status"] = "skipped"
                reason = _tag_attribute(attributes, "message")
                if reason:
                    pending["reason"] = reason[:SKIP_REASON_MAX_CHARS]
            elif tag == "failure":
                pending["status"] = "failed"
                # Spec §5 S2: the FAILURE's own message is the distinct typed
                # evidence (live TVM: the NumPy dtype error) — a failed node
                # with no reason cannot emit a distinct failure code.
                reason = _tag_attribute(attributes, "message")
                if reason:
                    pending["reason"] = reason[:SKIP_REASON_MAX_CHARS]
            elif tag == "error":
                pending["status"] = "error"
                reason = _tag_attribute(attributes, "message")
                if reason:
                    pending["reason"] = reason[:SKIP_REASON_MAX_CHARS]
    close(pending)
    ordered = sorted(
        nodes.values(),
        key=lambda node: (_STATUS_PRIORITY.get(node["status"], 9), node["node_id"]),
    )
    return ordered, len(ordered)


def _testcase_node_id(attributes: str) -> str:
    """`<classname>#<name>`, or the bare name when the report has no class."""
    name = _tag_attribute(attributes, "name")
    if not name:
        return ""
    classname = _tag_attribute(attributes, "classname")
    return f"{classname}#{name}" if classname else name


def _tag_attribute(attributes: str, name: str) -> str:
    match = re.search(rf"""\b{name}=(?:"([^"]*)"|'([^']*)')""", attributes or "")
    if not match:
        return ""
    return " ".join(html.unescape(match.group(1) or match.group(2) or "").split())


def _surveyed_domain_roots(manifest: Optional[Mapping[str, Any]]) -> List[str]:
    """Every surveyed execution root, read without inventing coordinates.

    The manifest projects the recommendation's keys at top level; a manifest
    written before that projection existed carries only the nested
    ``build_recommendation`` (same dual read the phase gate performs).  A
    single-module test survey has no island list, so its explicit ``test_root``
    is still a surveyed domain; otherwise testcase rows from that exact root
    lose their module identity at the receipt boundary.
    """
    if not isinstance(manifest, Mapping):
        return []
    roots: List[str] = []

    def add(value: Any) -> None:
        root = _normalized_root(value)
        if root and root not in roots:
            roots.append(root)

    recommendation = manifest.get("build_recommendation")
    nested = recommendation if isinstance(recommendation, Mapping) else {}
    raw_domains = manifest.get("build_domains")
    if raw_domains is None:
        raw_domains = nested.get("build_domains")
    if isinstance(raw_domains, (list, tuple)):
        for item in raw_domains:
            if isinstance(item, Mapping):
                add(item.get("root"))

    raw_test_islands = manifest.get("test_islands")
    if raw_test_islands is None:
        raw_test_islands = nested.get("test_islands")
    if isinstance(raw_test_islands, (list, tuple)):
        for item in raw_test_islands:
            if isinstance(item, Mapping):
                add(item.get("root"))

    test_system = str(manifest.get("test_system") or nested.get("test_system") or "").strip()
    if test_system in {"maven", "gradle"}:
        add(manifest.get("test_root") or nested.get("test_root"))
    return roots


def _normalized_root(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return posixpath.normpath(raw).rstrip("/") or "/"


def _first_line(output: Any) -> str:
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


def _slug(value: Any) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in str(value or "").strip()
    ).strip("_")
