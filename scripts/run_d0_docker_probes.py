#!/usr/bin/env python3
"""Run the seven pre-registered D0 Docker probes without invoking a model.

The runner is deliberately separate from the model campaign machinery.  Each
probe uses production persistence/controller functions against one or more
fresh SAG containers, seals its artifacts before cleanup, and stops the whole
campaign on the first failed assertion or registered campaign stop condition.

Examples::

    uv run python scripts/run_d0_docker_probes.py --list-probes
    uv run python scripts/run_d0_docker_probes.py \
      --prepare-image --parent-image ubuntu:24.04 \
      --base-image sag-d0-probe-base:20260808
    uv run python scripts/run_d0_docker_probes.py \
      --campaign-dir logs/d0-20260808 --dry-run
    uv run python scripts/run_d0_docker_probes.py \
      --campaign-dir logs/d0-20260808 --probe sync-large-evidence

``--dry-run`` validates and writes the immutable campaign lock, but creates no
container and runs no probe.  It is therefore safe to use before authorizing a
real Docker acceptance run.  The script never calls ``sag project`` and never
constructs an LLM client.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOCK_SCHEMA_VERSION = 1
PROBE_RESULT_SCHEMA_VERSION = 1
MIN_LARGE_EVIDENCE_BYTES = 3 * 1024 * 1024
EXPECTED_TESTCASE_ROWS = 24_000
MAX_CONTAINER_COMMAND_CHARS = 60_200
NOT_APPLICABLE_SHA256 = hashlib.sha256(b"not-applicable:no-model").hexdigest()
PREPARED_IMAGE_VERSION = "1"
PREPARED_IMAGE_LABEL = "org.setup-agent.d0.prepared-version"
PREPARED_SOURCE_LABEL = "org.setup-agent.d0.source-tree-sha256"
DEFAULT_PREPARED_IMAGE = "sag-d0-probe-base:20260808"
MAVEN_VERSION = "3.9.11"
MAVEN_ZIP_NAME = f"apache-maven-{MAVEN_VERSION}-bin.zip"
MAVEN_ZIP_SHA256 = "0d7125e8c91097b36edb990ea5934e6c68b4440eef4ea96510a0f6815e7eeadb"
GRADLE_VERSION = "8.10"
GRADLE_ZIP_SHA256 = "5b9c5eb3f9fc2c94abaea57d90bd78747ca117ddbbf96c859d3741181a12bf2a"
HTTP_REPOSITORY_URL = "https://github.com/apache/httpcomponents-client.git"
HTTP_REPOSITORY_REF = "rel/v5.6.1"
HTTP_TARGET_SHA = "4f86ca6a5eb528613edb892a4f7161e23dce15d7"
D0_ROW_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>invalid.example.d0</groupId>
  <artifactId>detached-row-probe</artifactId>
  <version>1.0.0</version>
</project>
"""
TERMINAL_REPLAY_KINDS = frozenset(
    {
        "job_terminal_observed",
        "job_terminal_unpersisted",
        "job_settled",
        "job_unsettled",
        "job_live_at_close",
        "job_barrier_integrity_failure",
        "job_stall_observed",
    }
)
TERMINAL_REPLAY_FORBIDDEN_KINDS = frozenset(
    {
        "job_settled",
        "job_unsettled",
        "job_live_at_close",
        "job_barrier_integrity_failure",
        "job_stall_observed",
    }
)
TERMINAL_EXPECTED_LIFECYCLE = (
    "job_terminal_observed",
    "job_terminal_unpersisted",
)

# These are copied from the implementation plan verbatim enough to make the
# lock an executable pre-registration.  A future edit changes the registry
# hash and cannot silently resume an already-started campaign.
STOP_CONDITIONS = (
    "exit marker exists but job_unsettled is emitted",
    "receipt or obligation command exceeds 60,200 characters",
    "argument list too long appears",
    "byte hash JSON validation differs or a temporary file survives",
    "completion claim exceeds the pre-registered no-op cap",
    "wrapper guidance proposes checksum mutation",
    "runner-observed JDK is overwritten by older survey state",
    "one runner success is failed by another ecosystem analyzer",
    "same root or job is dispatched twice without new authority",
    "prompt model image revision or campaign pins drift",
    "claimed metrics combine quarantined unattributed or stale observations",
    "a physical probe lacks a frozen controller intent envelope contract chain",
    "a stable control develops an unregistered red",
)


class D0Error(RuntimeError):
    """The D0 runner could not establish an acceptance fact."""


class D0Stop(D0Error):
    """A pre-registered campaign stop condition fired."""


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    order: int
    description: str
    container_count: int = 1
    no_model: bool = True


PROBE_SPECS: dict[str, ProbeSpec] = {
    spec.name: spec
    for spec in (
        ProbeSpec(
            "sync-large-evidence",
            0,
            "3 MiB synchronous receipt and large obligation persist exactly",
        ),
        ProbeSpec(
            "detached-large-settlement",
            1,
            "3 MiB detached settlement receipt persists and settles once",
        ),
        ProbeSpec(
            "terminal-receipt-failure",
            2,
            "terminal process plus injected receipt failure stays terminal-unpersisted",
        ),
        ProbeSpec(
            "http-wrapper-unzip-ablation",
            3,
            "HTTP Maven-wrapper control and treatment differ only by unzip",
            container_count=2,
        ),
        ProbeSpec(
            "dynamic-jdk-authority",
            4,
            "static Java 11 then observed Java 17 leaves the next dispatch on 17",
        ),
        ProbeSpec(
            "gradle-runner-classification",
            5,
            "Gradle exit zero with Errors in a task name remains success",
        ),
        ProbeSpec(
            "multi-job-progress-barrier",
            6,
            "progressing jobs hold one controller barrier and release exactly once",
        ),
    )
}


@dataclass(frozen=True)
class HostFacts:
    sag_git_sha: str
    source_tree_sha256: str
    source_dirty: bool
    base_image: str
    image_id: str
    image_repo_digests: tuple[str, ...]
    image_labels: tuple[tuple[str, str], ...]
    docker_server_version: str
    host_arch: str
    python_version: str


@dataclass(frozen=True)
class ProbeObservation:
    passed: bool
    facts: Mapping[str, Any] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    output_excerpt: str = ""
    max_command_chars: int = 0
    exit_marker_job_ids: tuple[str, ...] = ()
    job_unsettled_ids: tuple[str, ...] = ()
    byte_hash_json_mismatch: bool = False
    temporary_files: tuple[str, ...] = ()
    no_op_claim_count: int = 0
    no_op_claim_cap: int = 3
    wrapper_checksum_mutated: bool = False
    jdk_authority_regressed: bool = False
    cross_runner_false_failure: bool = False
    duplicate_dispatches: tuple[str, ...] = ()
    evidence_grains_combined: bool = False
    unregistered_control_red: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def probe_evaluator_disposition(spec: ProbeSpec, observation: ProbeObservation) -> str:
    if not observation.passed:
        return "failed"
    if spec.name == "sync-large-evidence":
        return "synthetic_non_claimable"
    return "claimable"


def probe_evaluator_grain(spec: ProbeSpec) -> str:
    return "synthetic-transport-fixture" if spec.name == "sync-large-evidence" else "probe-run"


@dataclass(frozen=True)
class ControllerActionBinding:
    """One D0 controller-owned public action and its emitted envelope."""

    envelope_id: str
    tool_call_id: str
    tool: str
    domain_id: str
    exact_params: Mapping[str, Any]
    intent_id: str
    intent_source: str
    action_fingerprint: str
    predecessor_contract_id: str | None = None


@dataclass(frozen=True)
class ControllerDispatchBinding:
    """One controller action with the contract frozen for its dispatch."""

    action: ControllerActionBinding
    contract: Mapping[str, Any]

    @property
    def envelope_id(self) -> str:
        return self.action.envelope_id

    @property
    def intent_id(self) -> str:
        return self.action.intent_id

    @property
    def intent_source(self) -> str:
        return self.action.intent_source

    @property
    def action_fingerprint(self) -> str:
        return self.action.action_fingerprint


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(value) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(canonical_json(row) + "\n" for row in rows)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def probe_registry_material() -> list[dict[str, Any]]:
    return [asdict(spec) for spec in PROBE_SPECS.values()]


def select_probes(names: Sequence[str] | None) -> list[ProbeSpec]:
    if not names:
        return list(PROBE_SPECS.values())
    requested = {str(name).strip() for name in names if str(name).strip()}
    unknown = sorted(requested.difference(PROBE_SPECS))
    if unknown:
        raise D0Error("unknown D0 probe(s): " + ", ".join(unknown))
    return [spec for spec in PROBE_SPECS.values() if spec.name in requested]


def build_campaign_lock(
    *, campaign_id: str, facts: HostFacts, selected: Sequence[ProbeSpec]
) -> dict[str, Any]:
    registry_sha256 = canonical_sha256(probe_registry_material())
    control_bundle_sha256 = canonical_sha256(
        {
            "source_tree_sha256": facts.source_tree_sha256,
            "probe_registry_sha256": registry_sha256,
            "stop_conditions": list(STOP_CONDITIONS),
        }
    )
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "d0-deterministic-no-model",
        "model_pin": "none:no-model",
        "prompt_bundle_sha256": NOT_APPLICABLE_SHA256,
        "control_bundle_sha256": control_bundle_sha256,
        "source": {
            "git_sha": facts.sag_git_sha,
            "tree_sha256": facts.source_tree_sha256,
            "dirty": facts.source_dirty,
        },
        "image": {
            "reference": facts.base_image,
            "id": facts.image_id,
            "repo_digests": list(facts.image_repo_digests),
            "labels": dict(facts.image_labels),
        },
        "host": {
            "arch": facts.host_arch,
            "docker_server_version": facts.docker_server_version,
            "python_version": facts.python_version,
        },
        "probe_registry_sha256": registry_sha256,
        "probes": [
            {
                **asdict(spec),
                # The selected D0 plan is itself the total order.  A partial
                # run never carries sparse indices from probes it did not run.
                "run_order_index": index,
            }
            for index, spec in enumerate(selected)
        ],
        "stop_conditions": list(STOP_CONDITIONS),
        "cleanup_policy": {
            "archive_before_cleanup": True,
            "retain_failing_container": True,
        },
    }


def ensure_campaign_lock(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    if path.is_file():
        try:
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise D0Stop(f"campaign lock is unreadable: {exc}") from exc
        if canonical_json(observed) != canonical_json(expected):
            raise D0Stop(
                "campaign pin drift: existing campaign-lock.json differs from current "
                "source/image/probe/order/stop-condition pins"
            )
        if not isinstance(observed, dict):
            raise D0Stop("campaign lock must contain a JSON object")
        return cast(dict[str, Any], observed)
    _atomic_json(path, expected)
    return dict(expected)


def enforce_stop_conditions(observation: ProbeObservation) -> None:
    overlap = sorted(
        set(observation.exit_marker_job_ids).intersection(observation.job_unsettled_ids)
    )
    if overlap:
        raise D0Stop(
            "exit marker exists but the job was emitted as job_unsettled: " + ", ".join(overlap)
        )
    if observation.max_command_chars > MAX_CONTAINER_COMMAND_CHARS:
        raise D0Stop(
            f"receipt/obligation command exceeded 60,200 characters: "
            f"{observation.max_command_chars}"
        )
    if "argument list too long" in observation.output_excerpt.casefold():
        raise D0Stop("argument list too long appeared in probe output")
    if observation.byte_hash_json_mismatch:
        raise D0Stop("byte/hash/JSON validation differed")
    if observation.temporary_files:
        raise D0Stop("temporary file survived: " + ", ".join(observation.temporary_files))
    if observation.no_op_claim_count > observation.no_op_claim_cap:
        raise D0Stop("completion claim exceeded the pre-registered no-op cap")
    if observation.wrapper_checksum_mutated:
        raise D0Stop("wrapper checksum mutation was observed or proposed")
    if observation.jdk_authority_regressed:
        raise D0Stop("runner-observed JDK authority was overwritten by older survey state")
    if observation.cross_runner_false_failure:
        raise D0Stop("one runner success was failed by another ecosystem analyzer")
    if observation.duplicate_dispatches:
        raise D0Stop(
            "the same root/job was dispatched twice without new authority: "
            + ", ".join(observation.duplicate_dispatches)
        )
    if observation.evidence_grains_combined:
        raise D0Stop("claimed and non-claimable evidence grains were combined")
    if observation.unregistered_control_red:
        raise D0Stop("stable control developed an unregistered red")
    if not observation.passed:
        detail = "; ".join(observation.failures) or "unspecified assertion"
        raise D0Stop(f"probe assertion failed: {detail}")


def archive_tree(source: Path, destination: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    if not source.is_dir():
        return checksums
    for child in sorted(source.rglob("*")):
        if not child.is_file():
            continue
        relative = child.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, target)
        checksums[relative.as_posix()] = _sha256_file(target)
    return checksums


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _required_command(command: Sequence[str], *, cwd: Path | None = None) -> str:
    result = _run(command, cwd=cwd)
    if result.returncode != 0:
        rendered = " ".join(shlex.quote(part) for part in command)
        raise D0Error(f"{rendered} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _source_tree_digest(repo: Path) -> str:
    """Hash the executable source, config, lock, and this runner by path+bytes.

    The D0 campaign may intentionally run before a commit.  Pinning only HEAD
    would then misidentify the code under test, so the content digest is the
    authority and the committed SHA remains provenance.
    """
    candidates: list[Path] = []
    for root in (
        repo / "src" / "sag",
        repo / "scripts",
        repo / "tests" / "fixtures" / "d0",
    ):
        if root.is_dir():
            candidates.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and (
                    path.suffix in {".json", ".py", ".toml", ".yaml", ".yml", ".xml"}
                    or path.name == "Dockerfile"
                )
                and "__pycache__" not in path.parts
            )
    for name in ("pyproject.toml", "uv.lock"):
        path = repo / name
        if path.is_file():
            candidates.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(candidates)):
        relative = path.relative_to(repo).as_posix().encode("utf-8")
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def load_host_facts(repo: Path, base_image: str) -> HostFacts:
    if not shutil.which("docker"):
        raise D0Error("docker CLI is unavailable")
    if not shutil.which("uv"):
        raise D0Error("uv is unavailable")
    docker_version = _required_command(["docker", "version", "--format", "{{.Server.Version}}"])
    image_text = _required_command(["docker", "image", "inspect", base_image])
    try:
        image = json.loads(image_text)[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise D0Error(f"cannot parse docker image inspect for {base_image}") from exc
    git_sha = _required_command(["git", "rev-parse", "HEAD"], cwd=repo)
    status = _required_command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo)
    labels = {
        str(key): str(value)
        for key, value in ((image.get("Config") or {}).get("Labels") or {}).items()
    }
    source_digest = _source_tree_digest(repo)
    if labels.get(PREPARED_IMAGE_LABEL) != PREPARED_IMAGE_VERSION:
        raise D0Error(f"{base_image} is not a D0 prepared image; run --prepare-image first")
    if labels.get(PREPARED_SOURCE_LABEL) != source_digest:
        raise D0Error(
            "prepared image source pin differs from the executable D0 source tree; "
            "rebuild it with --prepare-image"
        )
    return HostFacts(
        sag_git_sha=git_sha,
        source_tree_sha256=source_digest,
        source_dirty=bool(status),
        base_image=base_image,
        image_id=str(image.get("Id") or ""),
        image_repo_digests=tuple(sorted(str(value) for value in image.get("RepoDigests") or ())),
        image_labels=tuple(sorted(labels.items())),
        docker_server_version=docker_version,
        host_arch=platform.machine() or "unknown",
        python_version=platform.python_version(),
    )


def prepare_d0_image(repo: Path, *, parent_image: str, target_image: str) -> dict[str, Any]:
    """Build the immutable D0 snapshot; this is never implicit in a campaign."""

    repo = repo.resolve()
    if not shutil.which("docker"):
        raise D0Error("docker CLI is unavailable")
    dockerfile = repo / "tests" / "fixtures" / "d0" / "Dockerfile"
    if not dockerfile.is_file():
        raise D0Error(f"D0 Dockerfile is missing: {dockerfile}")
    source_digest = _source_tree_digest(repo)
    command = [
        "docker",
        "build",
        "--build-arg",
        f"BASE_IMAGE={parent_image}",
        "--build-arg",
        f"MAVEN_VERSION={MAVEN_VERSION}",
        "--build-arg",
        f"MAVEN_ZIP_SHA256={MAVEN_ZIP_SHA256}",
        "--build-arg",
        f"GRADLE_VERSION={GRADLE_VERSION}",
        "--build-arg",
        f"GRADLE_ZIP_SHA256={GRADLE_ZIP_SHA256}",
        "--build-arg",
        f"HTTP_REPOSITORY_URL={HTTP_REPOSITORY_URL}",
        "--build-arg",
        f"HTTP_REPOSITORY_REF={HTTP_REPOSITORY_REF}",
        "--build-arg",
        f"HTTP_TARGET_SHA={HTTP_TARGET_SHA}",
        "--label",
        f"{PREPARED_SOURCE_LABEL}={source_digest}",
        "--file",
        str(dockerfile),
        "--tag",
        target_image,
        str(dockerfile.parent),
    ]
    built = _run(command, cwd=repo, timeout=1_800)
    if built.returncode != 0:
        raise D0Error(
            "D0 prepared image build failed: " + (built.stderr or built.stdout)[-8_000:].strip()
        )
    image_text = _required_command(["docker", "image", "inspect", target_image])
    image = json.loads(image_text)[0]
    labels = (image.get("Config") or {}).get("Labels") or {}
    if labels.get(PREPARED_IMAGE_LABEL) != PREPARED_IMAGE_VERSION:
        raise D0Error("prepared D0 image is missing its version label")
    if labels.get(PREPARED_SOURCE_LABEL) != source_digest:
        raise D0Error("prepared D0 image source label did not bind the current source")
    return {
        "status": "prepared",
        "image": target_image,
        "image_id": str(image.get("Id") or ""),
        "source_tree_sha256": source_digest,
    }


@dataclass
class CommandAudit:
    """Audit every sync/detached production call into content-addressed blobs."""

    orchestrator: Any
    records: list[dict[str, Any]] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)
    output_fragments: list[str] = field(default_factory=list)
    _next_index: int = 0
    _sync_impl: Callable[..., Any] = field(init=False, repr=False)
    _detached_impl: Callable[..., Any] = field(init=False, repr=False)
    _poll_impl: Callable[..., Any] = field(init=False, repr=False)
    _collect_impl: Callable[..., Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._sync_impl = self.orchestrator.execute_command
        self._detached_impl = self.orchestrator.execute_command_detached
        self._poll_impl = self.orchestrator.poll_detached_command
        self._collect_impl = self.orchestrator.collect_detached_result
        # Internal detached helpers call ``self.execute_command``.  Installing
        # the proxy on the production object ensures those nested commands are
        # audited too, while the saved bound implementations prevent recursion.
        self.orchestrator.execute_command = self.execute_command
        self.orchestrator.execute_command_detached = self.execute_command_detached
        self.orchestrator.poll_detached_command = self.poll_detached_command
        self.orchestrator.collect_detached_result = self.collect_detached_result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.orchestrator, name)

    def evidence_store_identity(self) -> str:
        """Report the orchestrator's store identity, never a proxy identity.

        The audit is a transparent proxy for exactly one container, and one
        epoch authority is installed on both objects.  Deriving identity from
        the proxy would make the same container look like a second store.
        """

        resolver = getattr(self.orchestrator, "evidence_store_identity", None)
        if callable(resolver):
            return str(resolver())
        container_id = str(getattr(self.orchestrator, "container_id", "") or "").strip()
        if container_id:
            return f"container_id:{container_id}"
        return f"object:{id(self.orchestrator)}"

    def _allocate_index(self) -> int:
        index = self._next_index
        self._next_index += 1
        return index

    def _blob(self, value: str | bytes) -> str:
        body = value if isinstance(value, bytes) else value.encode("utf-8", "replace")
        digest = hashlib.sha256(body).hexdigest()
        self.blobs.setdefault(digest, body)
        return f"sha256:{digest}"

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, sort_keys=True, default=str))

    def _record_result(
        self,
        *,
        index: int,
        kind: str,
        command: str | None,
        result: Any,
    ) -> None:
        safe = self._json_safe(result)
        output = str((result or {}).get("output") or "") if isinstance(result, Mapping) else ""
        full_output = (
            str((result or {}).get("full_output") or output)
            if isinstance(result, Mapping)
            else output
        )
        record = {
            "command_index": index,
            "kind": kind,
            "chars": len(command or ""),
            "command_ref": self._blob(command or ""),
            "response_ref": self._blob(canonical_json(safe)),
            "output_ref": self._blob(output),
            "full_output_ref": self._blob(full_output),
            "exit_code": (result or {}).get("exit_code") if isinstance(result, Mapping) else None,
            "success": (result or {}).get("success") if isinstance(result, Mapping) else None,
        }
        self.records.append(record)
        if full_output:
            self.output_fragments.append(full_output[-2_000:])

    def _record_exception(
        self, *, index: int, kind: str, command: str | None, error: BaseException
    ) -> None:
        body = f"{type(error).__name__}: {error}"
        self.records.append(
            {
                "command_index": index,
                "kind": kind,
                "chars": len(command or ""),
                "command_ref": self._blob(command or ""),
                "response_ref": self._blob(canonical_json({"exception": body})),
                "output_ref": self._blob(body),
                "full_output_ref": self._blob(body),
                "exit_code": None,
                "success": False,
            }
        )
        self.output_fragments.append(body[-2_000:])

    def execute_command(self, command: str, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        index = self._allocate_index()
        text = str(command)
        try:
            result = self._sync_impl(command, *args, **kwargs)
        except BaseException as exc:
            self._record_exception(index=index, kind="execute_command", command=text, error=exc)
            raise
        self._record_result(
            index=index,
            kind="execute_command",
            command=text,
            result=result,
        )
        return cast(Mapping[str, Any], result)

    def execute_control_command(self, command: str, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        """Run bounded host-control I/O on the clean channel.

        Bootstrap, verification, cleanup and archive reads are not project
        evidence: they must neither require nor consume the project runtime
        overlay, which ``DockerOrchestrator.execute_command`` now refuses to
        build without an installed host publication authority.  The production
        clean channel re-enters this audit through the ``execute_command``
        proxy installed on the orchestrator, so every control command is still
        content-addressed into the sealed archive exactly once.
        """

        clean = getattr(self.orchestrator, "execute_control_command", None)
        if not callable(clean):
            # Narrow unit-double fallback, matching ``resolve_control_execute``:
            # an in-memory test orchestrator has no separate clean channel.
            return self.execute_command(command, *args, **kwargs)
        return cast(Mapping[str, Any], clean(command, *args, **kwargs))

    def execute_command_detached(
        self, command: str, *args: Any, **kwargs: Any
    ) -> Mapping[str, Any]:
        index = self._allocate_index()
        text = str(command)
        try:
            result = self._detached_impl(command, *args, **kwargs)
        except BaseException as exc:
            self._record_exception(
                index=index,
                kind="execute_command_detached",
                command=text,
                error=exc,
            )
            raise
        self._record_result(
            index=index,
            kind="execute_command_detached",
            command=text,
            result=result,
        )
        return cast(Mapping[str, Any], result)

    def poll_detached_command(self, handle: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
        index = self._allocate_index()
        try:
            result = self._poll_impl(handle, *args, **kwargs)
        except BaseException as exc:
            self._record_exception(
                index=index,
                kind="poll_detached_command",
                command=canonical_json(self._json_safe(handle)),
                error=exc,
            )
            raise
        self._record_result(
            index=index,
            kind="poll_detached_command",
            command=canonical_json(self._json_safe(handle)),
            result=result,
        )
        return result

    def collect_detached_result(
        self,
        handle: Mapping[str, Any],
        poll_result: Mapping[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        index = self._allocate_index()
        command = canonical_json(
            {"handle": self._json_safe(handle), "poll_result": self._json_safe(poll_result)}
        )
        try:
            result = self._collect_impl(handle, poll_result, *args, **kwargs)
        except BaseException as exc:
            self._record_exception(
                index=index,
                kind="collect_detached_result",
                command=command,
                error=exc,
            )
            raise
        self._record_result(
            index=index,
            kind="collect_detached_result",
            command=command,
            result=result,
        )
        return cast(Mapping[str, Any], result)

    def record_injected_failure(self, command: str, output: str, *, exit_code: int) -> None:
        index = self._allocate_index()
        self._record_result(
            index=index,
            kind="injected_receipt_failure",
            command=command,
            result={"success": False, "exit_code": exit_code, "output": output},
        )

    @property
    def max_command_chars(self) -> int:
        return max(
            (
                int(item["chars"])
                for item in self.records
                if item.get("kind")
                in {"execute_command", "execute_command_detached", "injected_receipt_failure"}
            ),
            default=0,
        )

    @property
    def output_excerpt(self) -> str:
        return "\n".join(self.output_fragments)[-8_000:]

    @property
    def command_texts(self) -> tuple[str, ...]:
        rows = sorted(self.records, key=lambda item: int(item["command_index"]))
        values = []
        for row in rows:
            reference = str(row.get("command_ref") or "").removeprefix("sha256:")
            body = self.blobs.get(reference)
            if body is not None:
                values.append(body.decode("utf-8", "replace"))
        return tuple(values)


@dataclass(frozen=True)
class ContainerEvidenceEpoch:
    """One host publication epoch bound to exactly one fresh container store."""

    run_id: str
    control_event_path: Path
    sink: Any
    authority: Any


def _install_epoch_authority(authority: Any, audit: CommandAudit) -> Any:
    """Install one epoch authority on both the audit proxy and its orchestrator.

    Production installs the authority on the orchestrator object itself
    (``SetupAgent._initialize_control_recording``), and
    ``DockerOrchestrator._default_exec_environment`` resolves it from ``self``
    before it will build a project runtime overlay.  Binding only the audit
    proxy therefore leaves the project lane with "runtime environment has no
    host publication authority" for every probe command and every detached
    dispatch.  The audit keeps its own binding because evidence writers receive
    ``audit.execute_command`` and resolve authority from that owner; both
    objects report the same immutable container store identity.
    """

    from sag.agent.evidence_publications import install_evidence_publication_authority

    orchestrator = getattr(audit, "orchestrator", None)
    if orchestrator is not None:
        install_evidence_publication_authority(authority, orchestrator=orchestrator)
    return install_evidence_publication_authority(authority, orchestrator=audit)


@dataclass
class DockerProbeRuntime:
    repo: Path
    campaign_dir: Path
    campaign_id: str
    base_image: str
    spec: ProbeSpec
    run_pin: Mapping[str, Any]
    keep_containers: bool
    containers: list[Any] = field(default_factory=list)
    audits: list[CommandAudit] = field(default_factory=list)
    extra_evidence: list[tuple[CommandAudit, str, str]] = field(default_factory=list)
    _epochs_by_audit_id: dict[int, ContainerEvidenceEpoch] = field(
        default_factory=dict, init=False, repr=False
    )
    _epoch_audits: dict[int, CommandAudit] = field(default_factory=dict, init=False, repr=False)
    _sinks_by_path: dict[Path, Any] = field(default_factory=dict, init=False, repr=False)
    _output_storage_by_audit_id: dict[int, Any] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def artifact_dir(self) -> Path:
        return self.campaign_dir / self.spec.name

    @property
    def scratch_dir(self) -> Path:
        return self.campaign_dir / ".scratch" / self.spec.name

    @property
    def control_event_path(self) -> Path:
        """Legacy one-container view; multi-container callers must select an epoch."""

        if len(self._epochs_by_audit_id) == 1:
            return next(iter(self._epochs_by_audit_id.values())).control_event_path
        return self.scratch_dir / "control-events.jsonl"

    @staticmethod
    def _epoch_slug(value: str, *, fallback: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]", "-", str(value)).strip("-._")
        return normalized or fallback

    def _epoch_run_id(self, *, suffix: str, ordinal: int) -> str:
        campaign = self._epoch_slug(self.campaign_id, fallback="campaign")
        probe = self._epoch_slug(self.spec.name, fallback="probe")
        leaf = self._epoch_slug(suffix, fallback="container")
        material = f"{self.campaign_id}\0{self.spec.name}\0{suffix}\0{ordinal}"
        token = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        return f"d0-{campaign[:64]}-{probe[:64]}-{leaf[:48]}-{ordinal}-{token}"[:256]

    def _register_epoch(self, audit: CommandAudit, *, suffix: str) -> ContainerEvidenceEpoch:
        from sag.agent.control_events import ControlEventSink
        from sag.agent.evidence_publications import (
            EvidencePublicationAuthority,
            reset_evidence_publication_authority,
        )

        key = id(audit)
        if key in self._epochs_by_audit_id:
            return self._epochs_by_audit_id[key]
        ordinal = len(self._epochs_by_audit_id) + 1
        path = self.scratch_dir / "epochs" / f"container-{ordinal}" / "control-events.jsonl"
        if path.exists():
            raise D0Stop(f"fresh-container host publication stream already exists: {path}")
        sink = ControlEventSink(path)
        self._register_control_sink(path, sink)
        authority = EvidencePublicationAuthority.for_live_run(
            run_id=self._epoch_run_id(suffix=suffix, ordinal=ordinal),
            sink=sink,
        )
        # Writers normally receive ``audit.execute_command``, while the project
        # lane resolves authority from the orchestrator itself; bind both.
        token = _install_epoch_authority(authority, audit)
        reset_evidence_publication_authority(token)
        epoch = ContainerEvidenceEpoch(
            run_id=authority.run_id,
            control_event_path=path,
            sink=sink,
            authority=authority,
        )
        self._epochs_by_audit_id[key] = epoch
        self._epoch_audits[key] = audit
        return epoch

    def _register_control_sink(self, path: Path, sink: Any) -> None:
        resolved = path.resolve()
        prior = self._sinks_by_path.get(resolved)
        if prior is not None and prior is not sink:
            raise D0Stop("one host control-event path cannot own multiple live sinks")
        self._sinks_by_path[resolved] = sink

    def epoch_for(self, audit: CommandAudit) -> ContainerEvidenceEpoch:
        epoch = self._epochs_by_audit_id.get(id(audit))
        if epoch is None or self._epoch_audits.get(id(audit)) is not audit:
            raise D0Error("command audit has no container evidence epoch")
        epoch.authority.assert_store(audit)
        return epoch

    def audit_for_source(self, source: Any) -> CommandAudit:
        if isinstance(source, CommandAudit) and id(source) in self._epochs_by_audit_id:
            return source
        owner = getattr(source, "__self__", None) if callable(source) else None
        if isinstance(owner, CommandAudit) and id(owner) in self._epochs_by_audit_id:
            return owner
        audit = getattr(source, "audit", None)
        if isinstance(audit, CommandAudit) and id(audit) in self._epochs_by_audit_id:
            return audit
        raise D0Error("controller action is not bound to a registered container epoch")

    @contextmanager
    def evidence_epoch(self, audit: CommandAudit) -> Iterator[ContainerEvidenceEpoch]:
        from sag.agent.evidence_publications import (
            current_evidence_publication_authority,
            install_evidence_publication_authority,
        )
        from sag.agent.invocation_receipts import active_receipt_run_id, set_active_receipt_run_id

        epoch = self.epoch_for(audit)
        previous_authority = current_evidence_publication_authority()
        previous_run_id = active_receipt_run_id()
        _install_epoch_authority(epoch.authority, audit)
        set_active_receipt_run_id(epoch.run_id)
        try:
            yield epoch
        finally:
            install_evidence_publication_authority(previous_authority)
            set_active_receipt_run_id(previous_run_id)

    def output_storage_for(self, audit: CommandAudit) -> Any:
        """One production OutputStorageManager per container, as the engine builds it.

        Production tool calls always run inside
        ``ToolExecutor.execute`` -> ``bind_tool_result_output_storage``.  A
        canonical FAILED ToolResult refuses to be constructed without that
        durable binding ("canonical failed results require durable output
        storage before construction"), so a D0 probe that drives a public tool
        directly must supply the same ambient storage the engine would.
        """

        from sag.agent.output_storage import OutputStorageManager

        key = id(audit)
        storage = self._output_storage_by_audit_id.get(key)
        if storage is None:
            storage = OutputStorageManager(
                Path("/workspace/.setup_agent/contexts"),
                orchestrator=audit,
            )
            self._output_storage_by_audit_id[key] = storage
        return storage

    def _ensure_all_epochs(self) -> None:
        for index, audit in enumerate(self.audits, start=1):
            if id(audit) not in self._epochs_by_audit_id:
                self._register_epoch(audit, suffix=f"archive-{index}")

    def new_container(self, suffix: str = "main") -> CommandAudit:
        from sag.docker_orch.orch import DockerOrchestrator

        readable_campaign = re.sub(r"[^a-z0-9-]", "-", self.campaign_id.lower()).strip("-")
        token = (
            f"{hashlib.sha256(self.campaign_id.encode()).hexdigest()[:8]}-"
            f"{readable_campaign[-8:] or 'campaign'}"
        )
        probe = re.sub(r"[^a-z0-9-]", "-", self.spec.name.lower()).strip("-")
        leaf = re.sub(r"[^a-z0-9-]", "-", suffix.lower()).strip("-")
        project_name = f"d0-{token}-{probe}-{leaf}"[:58].rstrip("-")
        container_name = f"sag-{project_name}"
        existing = _run(
            [
                "docker",
                "container",
                "ls",
                "-a",
                "--filter",
                f"name=^{container_name}$",
                "--format",
                "{{.Names}}",
            ]
        )
        if existing.returncode != 0:
            raise D0Error(f"cannot inspect fresh container name {container_name}")
        if container_name in existing.stdout.splitlines():
            raise D0Stop(f"fresh-container requirement failed: {container_name} already exists")
        expected_image = str((self.run_pin.get("image") or {}).get("id") or "")
        if not expected_image:
            raise D0Stop("run pin has no prepared image id")
        # Do not call DockerOrchestrator.create_and_start_container here: its
        # production setup deliberately apt-updates each new container, which
        # would destroy the HTTP A/B single-variable proof.  Both arms instead
        # start byte-for-byte from the one separately prepared and pinned image;
        # DockerOrchestrator remains the production executor after creation.
        created = _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container_name,
                "--workdir",
                "/workspace",
                "--env",
                "LANG=C.UTF-8",
                "--env",
                "LC_ALL=C.UTF-8",
                "--env",
                "DEBIAN_FRONTEND=noninteractive",
                "--label",
                f"setup-agent.project={project_name}",
                expected_image,
                "/bin/bash",
                "-c",
                "while true; do sleep 30; done",
            ],
            timeout=60,
        )
        if created.returncode != 0:
            raise D0Error(
                f"failed to create fresh container {container_name}: " f"{created.stderr.strip()}"
            )
        orchestrator = DockerOrchestrator(
            base_image=expected_image,
            project_name=project_name,
        )
        observed_image = _required_command(
            ["docker", "inspect", "--format", "{{.Image}}", container_name]
        )
        if observed_image != expected_image:
            raise D0Stop(f"container image pin drift: {observed_image!r} != {expected_image!r}")
        audit = CommandAudit(orchestrator)
        self._register_epoch(audit, suffix=suffix)
        # Bootstrap I/O belongs on the clean control channel: the project lane
        # (execute_command) refuses to run before the epoch authority is
        # installed below, and directory creation is not project evidence.
        evidence_dirs = audit.execute_control_command(
            "mkdir -p /workspace/.setup_agent /tmp/sag_jobs"
        )
        if evidence_dirs.get("exit_code") != 0:
            raise D0Error(f"fresh container {container_name} cannot create D0 evidence directories")
        self.containers.append(orchestrator)
        self.audits.append(audit)
        # The newest fresh container becomes the ambient epoch for immediate
        # producer calls. Controller scopes still switch explicitly and restore.
        from sag.agent.invocation_receipts import set_active_receipt_run_id

        epoch = self.epoch_for(audit)
        _install_epoch_authority(epoch.authority, audit)
        set_active_receipt_run_id(epoch.run_id)
        return audit

    def register_container_evidence(
        self, audit: CommandAudit, source: str, *, archive_name: str
    ) -> None:
        if audit not in self.audits:
            raise D0Error("extra evidence must belong to this probe runtime")
        if not source.startswith("/") or "/" in archive_name or archive_name.startswith("."):
            raise D0Error("extra evidence path/name is not a bounded D0 fixture")
        self.extra_evidence.append((audit, source, archive_name))

    def archive(self, observation: ProbeObservation) -> dict[str, str]:
        self._ensure_all_epochs()
        destination = self.artifact_dir
        if destination.exists():
            raise D0Stop(f"probe archive already exists; D0 never resumes: {destination}")
        stage = self.campaign_dir / f".{self.spec.name}.{uuid.uuid4().hex}.stage"
        stage.mkdir(parents=True, exist_ok=False)
        try:
            _atomic_json(stage / "run-pin.json", self.run_pin)
            _atomic_json(stage / "result.json", observation.to_json())
            _atomic_json(
                stage / "evaluator-row.json",
                {
                    "schema_version": 2,
                    "evaluation_kind": "d0-deterministic-probe",
                    "probe": self.spec.name,
                    "identity_version": 1,
                    "disposition": probe_evaluator_disposition(self.spec, observation),
                    "evidence_grain": probe_evaluator_grain(self.spec),
                    "passed": observation.passed,
                    "failures": list(observation.failures),
                    "result_sha256": canonical_sha256(observation.to_json()),
                },
            )
            archive_tree(self.scratch_dir, stage / "host-evidence")
            for index, audit in enumerate(self.audits):
                label = f"container-{index + 1}"
                container_name = audit.orchestrator.container_name
                epoch = self.epoch_for(audit)
                epoch_dir = stage / label
                epoch_dir.mkdir(parents=True, exist_ok=True)
                if epoch.control_event_path.is_file():
                    shutil.copy2(epoch.control_event_path, epoch_dir / "control-events.jsonl")
                else:
                    (epoch_dir / "control-events.jsonl").touch()
                _atomic_json(
                    epoch_dir / "evidence-epoch.json",
                    {
                        "schema_version": 1,
                        "run_id": epoch.run_id,
                        "container_name": container_name,
                        "host_control_event_path": str(epoch.control_event_path),
                        "control_event_path": f"{label}/control-events.jsonl",
                    },
                )
                inspect = _run(["docker", "inspect", container_name])
                if inspect.returncode != 0:
                    raise D0Error(
                        f"cannot inspect {container_name} before cleanup: {inspect.stderr.strip()}"
                    )
                (stage / f"{label}-inspect.json").write_text(inspect.stdout, encoding="utf-8")
                logs = _run(["docker", "logs", container_name])
                if logs.returncode != 0:
                    raise D0Error(f"cannot read logs for {container_name}: {logs.stderr.strip()}")
                (stage / f"{label}-docker.log").write_text(
                    (logs.stdout or "") + (logs.stderr or ""), encoding="utf-8"
                )
                with (stage / f"{label}-commands.jsonl").open("x", encoding="utf-8") as handle:
                    for row in sorted(audit.records, key=lambda item: int(item["command_index"])):
                        handle.write(canonical_json(row) + "\n")
                blob_dir = epoch_dir / "command-blobs"
                blob_dir.mkdir(parents=True, exist_ok=True)
                for digest, body in sorted(audit.blobs.items()):
                    (blob_dir / digest).write_bytes(body)
                for source, leaf in (
                    ("/workspace/.setup_agent", ".setup_agent"),
                    ("/tmp/sag_jobs", "sag_jobs"),
                ):
                    target = epoch_dir / leaf
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copied = _run(["docker", "cp", f"{container_name}:{source}", str(target)])
                    if copied.returncode != 0:
                        raise D0Error(
                            f"cannot archive {source} from {container_name}: "
                            f"{copied.stderr.strip()}"
                        )
                for evidence_audit, source, leaf in self.extra_evidence:
                    if evidence_audit is not audit:
                        continue
                    target = epoch_dir / "extra-evidence" / leaf
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copied = _run(["docker", "cp", f"{container_name}:{source}", str(target)])
                    if copied.returncode != 0:
                        raise D0Error(
                            f"cannot archive D0 evidence {source} from {container_name}: "
                            f"{copied.stderr.strip()}"
                        )
            # Recover and validate each epoch independently before producing a
            # derived campaign event aggregation used by the existing gates.
            events = _verify_archived_evidence_epochs(stage)
            with (stage / "control-events.jsonl").open("x", encoding="utf-8") as handle:
                for event in events:
                    handle.write(canonical_json(event) + "\n")
            stop_audit = recompute_stop_audit(
                artifact_dir=stage,
                observation=observation,
                spec=self.spec,
                run_pin=self.run_pin,
            )
            _atomic_json(stage / "stop-audit.json", stop_audit)
            checksums: dict[str, str] = {}
            for path in sorted(stage.rglob("*")):
                if path.is_file() and path.name not in {"checksums.json", "seal.json"}:
                    checksums[path.relative_to(stage).as_posix()] = _sha256_file(path)
            _atomic_json(stage / "checksums.json", checksums)
            _atomic_json(
                stage / "seal.json",
                {
                    "schema_version": 1,
                    "status": "sealed",
                    "probe": self.spec.name,
                    "checksums_sha256": _sha256_file(stage / "checksums.json"),
                    "file_count": len(checksums),
                },
            )
            os.replace(stage, destination)
            self.verify_archive_seal()
            return checksums
        except BaseException as exc:
            failure_dir = self.campaign_dir / ".archive-failures"
            failure_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(
                failure_dir / f"{self.spec.name}-{uuid.uuid4().hex}.json",
                {
                    "schema_version": 1,
                    "probe": self.spec.name,
                    "archive_integrity": "failed",
                    "stage": str(stage),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise

    def verify_archive_seal(self) -> None:
        destination = self.artifact_dir
        try:
            seal = json.loads((destination / "seal.json").read_text(encoding="utf-8"))
            checksums_path = destination / "checksums.json"
            checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise D0Stop(f"probe archive is not sealed: {exc}") from exc
        if seal.get("status") != "sealed" or seal.get("probe") != self.spec.name:
            raise D0Stop("probe archive seal identity is invalid")
        if seal.get("checksums_sha256") != _sha256_file(checksums_path):
            raise D0Stop("probe archive checksum manifest does not match its seal")
        actual_files = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file() and path.name not in {"checksums.json", "seal.json"}
        }
        if actual_files != set(str(key) for key in checksums):
            raise D0Stop("probe archive file set differs from the sealed checksum manifest")
        for relative, expected in checksums.items():
            path = destination / str(relative)
            if not path.is_file() or _sha256_file(path) != expected:
                raise D0Stop(f"probe archive byte mismatch after seal: {relative}")

    def cleanup(self) -> None:
        if self.keep_containers:
            return
        for audit in self.audits:
            if not audit.orchestrator.remove_project():
                raise D0Error(
                    f"failed to clean passing container {audit.orchestrator.container_name}"
                )

    def aggregate_observation(
        self,
        *,
        passed: bool,
        facts: Mapping[str, Any],
        failures: Iterable[str] = (),
        exit_marker_job_ids: tuple[str, ...] = (),
        job_unsettled_ids: tuple[str, ...] = (),
        byte_hash_json_mismatch: bool = False,
        no_op_claim_count: int = 0,
        no_op_claim_cap: int = 3,
        wrapper_checksum_mutated: bool = False,
        jdk_authority_regressed: bool = False,
        cross_runner_false_failure: bool = False,
        duplicate_dispatches: tuple[str, ...] = (),
        evidence_grains_combined: bool = False,
        unregistered_control_red: bool = False,
    ) -> ProbeObservation:
        temporary: list[str] = []
        for audit in self.audits:
            # Post-probe verification sweep: control-plane, never a project
            # command.  It must also stay runnable for a probe that failed
            # before its epoch installed anything on the project lane.
            result = audit.execute_control_command(
                "find /workspace/.setup_agent -type f "
                "\\( -name '*.tmp' -o -name '*.b64.*.tmp' \\) -print 2>/dev/null"
            )
            temporary.extend(
                line.strip()
                for line in str(result.get("output") or "").splitlines()
                if line.strip()
            )
        return ProbeObservation(
            passed=passed,
            facts=dict(facts),
            failures=tuple(str(item) for item in failures),
            output_excerpt="\n".join(audit.output_excerpt for audit in self.audits)[-8_000:],
            max_command_chars=max((audit.max_command_chars for audit in self.audits), default=0),
            exit_marker_job_ids=exit_marker_job_ids,
            job_unsettled_ids=job_unsettled_ids,
            byte_hash_json_mismatch=byte_hash_json_mismatch,
            temporary_files=tuple(sorted(set(temporary))),
            no_op_claim_count=no_op_claim_count,
            no_op_claim_cap=no_op_claim_cap,
            wrapper_checksum_mutated=wrapper_checksum_mutated,
            jdk_authority_regressed=jdk_authority_regressed,
            cross_runner_false_failure=cross_runner_false_failure,
            duplicate_dispatches=duplicate_dispatches,
            evidence_grains_combined=evidence_grains_combined,
            unregistered_control_red=unregistered_control_red,
        )


PROBE_CONTROLLER_LINEAGE_COUNTS: dict[str, tuple[int, int, int]] = {
    # controller actions, frozen contracts, physical receipts
    "detached-large-settlement": (1, 1, 1),
    "terminal-receipt-failure": (1, 1, 0),
    "http-wrapper-unzip-ablation": (4, 4, 3),
    "dynamic-jdk-authority": (2, 3, 3),
    "gradle-runner-classification": (1, 1, 1),
    "multi-job-progress-barrier": (2, 2, 2),
}


PROBE_REQUIRED_FACTS: dict[str, tuple[str, ...]] = {
    "sync-large-evidence": (
        "expected",
        "observed",
        "evidence_disposition",
        "physical_dispatches",
        "production_entrypoints",
    ),
    "detached-large-settlement": (
        "job_id",
        "ledger",
        "receipt",
        "receipt_testcase_rows",
        "row_metrics",
        "evaluator_metrics",
        "settlement_emissions",
        "controller_lineage",
        "production_entrypoints",
    ),
    "terminal-receipt-failure": (
        "job_id",
        "ledger",
        "event_kinds",
        "replay_event_kinds",
        "terminal_receipt_unpersisted",
        "model_turns",
        "replay_snapshot_sha256",
        "replay_event_digest",
        "replay_expected_event_digest",
        "replay_conflicts",
        "replay_unconsumed_events",
        "replay_external_calls",
        "controller_lineage",
        "production_entrypoints",
    ),
    "http-wrapper-unzip-ablation": (
        "image_id",
        "control",
        "treatment_repetitions",
        "properties_sha256",
        "registered_treatment_mutation",
        "target_sha",
        "checkout_sha256",
        "dispatch_receipt_ids",
        "wrapper_sha256",
        "evidence_run_ids",
        "controller_lineage",
        "production_entrypoints",
    ),
    "dynamic-jdk-authority": (
        "first_call",
        "later_call",
        "runtime_requirement",
        "runtime_before",
        "runtime_after_first",
        "dispatch_receipt_ids",
        "controller_lineage",
        "production_entrypoints",
    ),
    "gradle-runner-classification": (
        "physical_exit_code",
        "operation_outcome",
        "runner",
        "dispatch_receipt_ids",
        "controller_lineage",
        "production_entrypoints",
    ),
    "multi-job-progress-barrier": (
        "job_ids",
        "loop_result",
        "sentinel_turns",
        "intervening_model_turns",
        "model_ledger_states",
        "progress_by_job",
        "progress_files",
        "signal_commands",
        "job_settled_events",
        "controller_lineage",
        "production_entrypoints",
    ),
}

PROBE_REQUIRED_ENTRYPOINTS: dict[str, tuple[str, ...]] = {
    "sync-large-evidence": (
        "invocation_receipts.write_receipt_result",
        "job_obligations.write_obligation",
    ),
    "detached-large-settlement": (
        "DockerOrchestrator.execute_command_detached",
        "job_obligations.reconcile_job_obligations",
        "ReportTool.finalize_metrics_v2",
        "evaluate_golden_battery.evaluate_v2_campaign",
    ),
    "terminal-receipt-failure": (
        "ReActEngine.run_react_loop",
        "job_obligations.reconcile_job_obligations",
        "ControlReplayRunner.offline",
    ),
    "http-wrapper-unzip-ablation": ("MavenTool.execute",),
    "dynamic-jdk-authority": ("BuildTool.execute",),
    "gradle-runner-classification": ("BuildTool.execute", "GradleTool.execute"),
    "multi-job-progress-barrier": (
        "ReActEngine.run_react_loop",
        "job_obligations.reconcile_job_obligations",
    ),
}


def required_probe_evidence_errors(spec: ProbeSpec, facts: Mapping[str, Any]) -> list[str]:
    missing_facts = [key for key in PROBE_REQUIRED_FACTS[spec.name] if key not in facts]
    actual_entrypoints = set(str(item) for item in facts.get("production_entrypoints") or ())
    missing_entrypoints = sorted(
        set(PROBE_REQUIRED_ENTRYPOINTS[spec.name]).difference(actual_entrypoints)
    )
    errors = []
    if missing_facts:
        errors.append("required evaluator facts absent: " + ", ".join(missing_facts))
    if missing_entrypoints:
        errors.append("production entrypoint evidence absent: " + ", ".join(missing_entrypoints))
    return errors


def semantic_probe_evidence_errors(
    spec: ProbeSpec,
    facts: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Pure negative gate for each probe's required controller/evaluator shape."""

    errors: list[str] = []
    kinds = [str(event.get("kind") or "") for event in events]
    if spec.name == "sync-large-evidence":
        if (
            facts.get("evidence_disposition") != "synthetic_non_claimable"
            or int(facts.get("physical_dispatches") or 0) != 0
        ):
            errors.append(
                "synchronous large transport fixture was not explicit synthetic non-claimable"
            )
    else:
        expected = PROBE_CONTROLLER_LINEAGE_COUNTS.get(spec.name)
        lineage = facts.get("controller_lineage") or {}
        if expected is not None:
            action_count, contract_count, receipt_count = expected
            intent_ids = [str(value) for value in lineage.get("intent_ids") or ()]
            envelope_ids = [str(value) for value in lineage.get("envelope_ids") or ()]
            contract_ids = [str(value) for value in lineage.get("contract_ids") or ()]
            receipt_contract_ids = [
                str(value) for value in lineage.get("receipt_contract_ids") or ()
            ]
            if (
                len(intent_ids) != action_count
                or len(set(intent_ids)) != action_count
                or len(envelope_ids) != action_count
                or len(set(envelope_ids)) != action_count
                or len(contract_ids) != contract_count
                or len(set(contract_ids)) != contract_count
                or len(receipt_contract_ids) != receipt_count
                or len(set(receipt_contract_ids)) != receipt_count
            ):
                errors.append(
                    "controller intent/envelope/contract lineage counts do not match the "
                    "pre-registered physical dispatch shape"
                )

    if spec.name == "terminal-receipt-failure":
        lifecycle_sequence = [kind for kind in kinds if kind in TERMINAL_REPLAY_KINDS]
        if lifecycle_sequence != list(TERMINAL_EXPECTED_LIFECYCLE):
            errors.append("terminal receipt lifecycle was not observed then terminal-unpersisted")
        if kinds.count("job_terminal_unpersisted") != 1:
            errors.append("terminal receipt failure did not emit exactly one terminal event")
        if kinds.count("job_terminal_observed") != 1:
            errors.append("terminal receipt failure did not emit exactly one observation")
        live_forbidden = sorted(TERMINAL_REPLAY_FORBIDDEN_KINDS.intersection(kinds))
        if live_forbidden:
            errors.append(
                "terminal receipt failure emitted contradictory lifecycle events: "
                + ", ".join(live_forbidden)
            )
        replay = [str(item) for item in facts.get("replay_event_kinds") or ()]
        replay_forbidden = TERMINAL_REPLAY_FORBIDDEN_KINDS.intersection(replay)
        if replay != list(TERMINAL_EXPECTED_LIFECYCLE) or replay_forbidden:
            errors.append("terminal receipt replay did not preserve terminal-unpersisted")
        if int(facts.get("model_turns") or 0) != 0:
            errors.append("terminal receipt barrier allowed a model turn")
        if int(facts.get("replay_external_calls") or 0) != 0:
            errors.append("terminal receipt replay made an external call")
        if facts.get("replay_unconsumed_events"):
            errors.append("terminal receipt replay left unconsumed controller state")
        if facts.get("replay_event_digest") != facts.get("replay_expected_event_digest"):
            errors.append("terminal receipt replay differs from its frozen event digest")
        expected_conflict = f"job_terminal_unpersisted:{facts.get('job_id')}"
        if expected_conflict not in (facts.get("replay_conflicts") or ()):
            errors.append("terminal receipt replay did not reconstruct its typed conflict")
    elif spec.name == "http-wrapper-unzip-ablation":
        control = facts.get("control") or {}
        if control.get("error_code") != "prerequisite_executable_missing:unzip":
            errors.append("HTTP control did not reproduce typed missing-unzip failure")
        if control.get("runner_dispatched") is not False:
            errors.append("HTTP control dispatched a wrapper")
        repetitions = facts.get("treatment_repetitions") or []
        if len(repetitions) != 3 or any(not item.get("maven_proper") for item in repetitions):
            errors.append("three HTTP treatment repetitions did not reach Maven proper")
        if any(item.get("checksum_changed") for item in repetitions):
            errors.append("HTTP treatment changed the wrapper checksum")
        receipt_ids = [str(item) for item in facts.get("dispatch_receipt_ids") or ()]
        if len(receipt_ids) != 3 or len(set(receipt_ids)) != 3:
            errors.append("HTTP treatment did not persist exactly three build dispatch receipts")
        control_contract_id = str(control.get("contract_id") or "")
        treatment_contract_ids = [str(item.get("contract_id") or "") for item in repetitions]
        receipt_contract_ids = [
            str(value)
            for value in ((facts.get("controller_lineage") or {}).get("receipt_contract_ids") or ())
        ]
        if (
            not control_contract_id
            or len(set(treatment_contract_ids)) != 3
            or set(receipt_contract_ids) != set(treatment_contract_ids)
            or control_contract_id in receipt_contract_ids
        ):
            errors.append(
                "HTTP control/treatment contracts do not prove pre-dispatch refusal and "
                "three exact treatment receipts"
            )
        if facts.get("registered_treatment_mutation") != ("ln -s /opt/d0/unzip /usr/bin/unzip"):
            errors.append("HTTP treatment mutation was not exactly the unzip executable")
        epoch_ids = facts.get("evidence_run_ids") or {}
        if (
            set(epoch_ids) != {"control", "treatment"}
            or not all(str(value) for value in epoch_ids.values())
            or len(set(str(value) for value in epoch_ids.values())) != 2
        ):
            errors.append("HTTP control/treatment do not have distinct evidence epochs")
    elif spec.name == "dynamic-jdk-authority":
        first = facts.get("first_call") or {}
        later = facts.get("later_call") or {}
        runtime = facts.get("runtime_requirement") or {}
        first_contract_ids = [str(value) for value in first.get("contract_ids") or ()]
        if first.get("jdk_retry") != {"from": "11", "to": "17"}:
            errors.append("public build call did not perform one Java 11 to 17 retry")
        if int(first.get("physical_dispatches") or 0) != 2:
            errors.append("public Java repair was not bounded to one retry")
        if not first.get("succeeded") or not later.get("succeeded"):
            errors.append("public Java repair calls did not both complete successfully")
        if int(later.get("physical_dispatches") or 0) != 1:
            errors.append("later public Java call did not remain a single dispatch")
        if runtime.get("required_major") != "17" or later.get("effective_major") != "17":
            errors.append("later public build call did not retain dynamic Java 17")
        if later.get("requirement_authority") != "persisted_dynamic":
            errors.append("later Java contract lost persisted-dynamic authority")
        receipt_ids = [str(item) for item in facts.get("dispatch_receipt_ids") or ()]
        if len(receipt_ids) != 3 or len(set(receipt_ids)) != 3:
            errors.append("Java repair path did not persist exactly three dispatch receipts")
        if (
            len(first_contract_ids) != 2
            or len(set(first_contract_ids)) != 2
            or not later.get("contract_id")
            or later.get("predecessor_contract_id") != first_contract_ids[-1]
        ):
            errors.append("Java repair did not preserve its controller contract chain")
    elif spec.name == "gradle-runner-classification":
        if facts.get("physical_exit_code") != 0:
            errors.append("Gradle build dispatch did not exit zero")
        if facts.get("operation_outcome") != "success" or facts.get("runner") != "gradle":
            errors.append("Gradle success was failed by a foreign runner classifier")
        receipt_ids = [str(item) for item in facts.get("dispatch_receipt_ids") or ()]
        if len(receipt_ids) != 1 or len(set(receipt_ids)) != 1:
            errors.append("Gradle public path did not persist exactly one dispatch receipt")
    elif spec.name == "multi-job-progress-barrier":
        job_ids = [str(item) for item in facts.get("job_ids") or ()]
        settled = [
            str((event.get("payload") or {}).get("job_id") or "")
            for event in events
            if event.get("kind") == "job_settled"
        ]
        if sorted(settled) != sorted(job_ids) or len(settled) != len(job_ids):
            errors.append("multi-job barrier did not emit exactly one settlement per job")
        if any(kind in {"job_stall_observed", "job_live_at_close"} for kind in kinds):
            errors.append("progressing multi-job barrier entered stall/live cleanup")
        if int(facts.get("sentinel_turns") or 0) != 1:
            errors.append("production loop did not make exactly one post-settlement sentinel turn")
        if int(facts.get("intervening_model_turns") or 0) != 0:
            errors.append("production loop made a turn while the job barrier was live")
        expected_states = [{job_id: "terminal/settled" for job_id in job_ids}]
        if facts.get("model_ledger_states") != expected_states:
            errors.append("sentinel turn did not observe all jobs terminal/settled")
        progress = facts.get("progress_by_job") or {}
        if any(int(progress.get(job_id) or 0) < 2 for job_id in job_ids):
            errors.append("each job did not prove progress across multiple barrier samples")
        if int(facts.get("signal_commands") or 0) != 0:
            errors.append("progressing multi-job barrier issued a signal")
    return errors


def _blob_bytes(artifact_dir: Path, label: str, reference: Any) -> bytes:
    text = str(reference or "")
    if not text.startswith("sha256:"):
        raise D0Stop(f"command audit reference is not content addressed: {text!r}")
    digest = text.removeprefix("sha256:")
    path = artifact_dir / label / "command-blobs" / digest
    if not path.is_file():
        raise D0Stop(f"command audit blob is missing: {label}/{digest}")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != digest:
        raise D0Stop(f"command audit blob hash differs: {label}/{digest}")
    return body


def _archived_contract_lineage_errors(
    artifact_dir: Path,
    spec: ProbeSpec,
    events: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any] | None = None,
) -> list[str]:
    """Verify the sealed controller envelope -> contract -> evidence chain.

    The synchronous large fixture exercises only bounded persistence.  It has
    no physical runner and is deliberately contractless/non-claimable.  Every
    other D0 probe physically dispatches, so neither a runner-authored fact nor
    a receipt may stand in for the pre-dispatch controller authority.
    """

    if spec.name == "sync-large-evidence":
        return []

    expected = PROBE_CONTROLLER_LINEAGE_COUNTS.get(spec.name)
    if expected is None:
        return [f"no controller-lineage registry exists for physical probe {spec.name}"]
    expected_actions, expected_contracts, expected_receipts = expected
    errors: list[str] = []

    controller_envelopes: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "action_envelope":
            continue
        payload = dict(event.get("payload") or {})
        if payload.get("intent_source") != "controller":
            continue
        envelope_id = str(payload.get("envelope_id") or "")
        if not envelope_id:
            errors.append("controller action envelope has no envelope_id")
            continue
        if envelope_id in controller_envelopes:
            errors.append(f"controller action envelope is duplicated: {envelope_id}")
            continue
        controller_envelopes[envelope_id] = payload
    if len(controller_envelopes) != expected_actions:
        errors.append(
            "controller action envelope count differs: "
            f"{len(controller_envelopes)} != {expected_actions}"
        )

    contracts: dict[str, dict[str, Any]] = {}
    for path in sorted(artifact_dir.glob("container-*/.setup_agent/invocation_contracts/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"archived frozen invocation contract is unreadable: {path.name}")
            continue
        if not isinstance(value, dict):
            errors.append(f"archived frozen invocation contract is not an object: {path.name}")
            continue
        contract_id = str(value.get("contract_id") or "")
        if not contract_id:
            errors.append(f"archived frozen invocation contract has no id: {path.name}")
            continue
        if contract_id != path.stem:
            errors.append(f"frozen invocation contract filename differs: {contract_id}")
        if contract_id in contracts:
            errors.append(f"frozen invocation contract id is duplicated: {contract_id}")
            continue
        contracts[contract_id] = value
    if len(contracts) != expected_contracts:
        errors.append(
            "frozen invocation contract count differs: " f"{len(contracts)} != {expected_contracts}"
        )

    linked_envelopes: set[str] = set()
    for contract_id, contract in contracts.items():
        expected_hash = canonical_sha256(
            {key: value for key, value in contract.items() if key != "contract_hash"}
        )
        if contract.get("contract_hash") != expected_hash:
            errors.append(f"frozen invocation contract hash differs: {contract_id}")
        lineage = (
            str(contract.get("intent_id") or ""),
            str(contract.get("action_fingerprint") or ""),
            str(contract.get("envelope_id") or ""),
        )
        if contract.get("intent_source") != "controller" or not all(lineage):
            errors.append(
                f"frozen invocation contract lacks complete controller intent: {contract_id}"
            )
            continue
        intent_id, fingerprint, envelope_id = lineage
        envelope = controller_envelopes.get(envelope_id)
        if envelope is None:
            errors.append(
                f"frozen invocation contract {contract_id} has no matching action envelope "
                f"{envelope_id}"
            )
            continue
        linked_envelopes.add(envelope_id)
        if (
            envelope.get("intent_id") != intent_id
            or envelope.get("intent_source") != "controller"
            or envelope.get("action_fingerprint") != fingerprint
        ):
            errors.append(f"controller intent differs across envelope/contract: {contract_id}")
        requested = contract.get("requested_call") or {}
        if envelope.get("tool") != requested.get("tool") or canonical_json(
            envelope.get("exact_params") or {}
        ) != canonical_json((requested.get("params") or {})):
            errors.append(f"requested call differs across envelope/contract: {contract_id}")
        predecessor = str(contract.get("predecessor_contract_id") or "")
        if predecessor and predecessor not in contracts:
            errors.append(
                f"frozen invocation contract predecessor is absent: {contract_id}->{predecessor}"
            )

    for envelope_id in sorted(set(controller_envelopes).difference(linked_envelopes)):
        errors.append(
            f"controller action envelope has no frozen invocation contract: {envelope_id}"
        )

    for contract_id in contracts:
        seen: set[str] = set()
        cursor = contract_id
        while cursor:
            if cursor in seen:
                errors.append(f"frozen invocation contract predecessor cycle: {contract_id}")
                break
            seen.add(cursor)
            cursor = str((contracts.get(cursor) or {}).get("predecessor_contract_id") or "")

    if spec.name == "dynamic-jdk-authority" and len(controller_envelopes) == 2:
        envelope_order = list(controller_envelopes)
        first_ids = [
            contract_id
            for contract_id, contract in contracts.items()
            if contract.get("envelope_id") == envelope_order[0]
        ]
        later_ids = [
            contract_id
            for contract_id, contract in contracts.items()
            if contract.get("envelope_id") == envelope_order[1]
        ]
        roots = [
            contract_id
            for contract_id in first_ids
            if not contracts[contract_id].get("predecessor_contract_id")
        ]
        retries = [
            contract_id
            for contract_id in first_ids
            if str(contracts[contract_id].get("predecessor_contract_id") or "") in roots
        ]
        later_predecessor = (
            str(contracts[later_ids[0]].get("predecessor_contract_id") or "")
            if len(later_ids) == 1
            else ""
        )
        if (
            len(first_ids) != 2
            or len(roots) != 1
            or len(retries) != 1
            or len(later_ids) != 1
            or later_predecessor != (retries[0] if len(retries) == 1 else "")
        ):
            errors.append(
                "Java controller contract chain is not original -> bounded retry -> later call"
            )

    receipts: dict[str, dict[str, Any]] = {}
    for path in sorted(artifact_dir.glob("container-*/.setup_agent/invocation_receipts/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"archived invocation receipt is unreadable: {path.name}")
            continue
        if not isinstance(value, dict):
            errors.append(f"archived invocation receipt is not an object: {path.name}")
            continue
        receipt_id = str(value.get("receipt_id") or path.stem)
        receipts[receipt_id] = value
        contract_id = str(value.get("contract_id") or "")
        linked_contract = contracts.get(contract_id)
        if linked_contract is None:
            errors.append(
                f"invocation receipt {receipt_id} references absent frozen invocation "
                f"contract {contract_id or '<missing>'}"
            )
        elif value.get("contract_hash") != linked_contract.get("contract_hash"):
            errors.append(f"invocation receipt contract hash differs: {receipt_id}")
    if len(receipts) != expected_receipts:
        errors.append(
            f"physical invocation receipt count differs: {len(receipts)} != {expected_receipts}"
        )

    obligations: list[dict[str, Any]] = []
    for path in sorted(artifact_dir.glob("container-*/.setup_agent/job_obligations/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"archived job obligation is unreadable: {path.name}")
            continue
        if not isinstance(value, dict):
            errors.append(f"archived job obligation is not an object: {path.name}")
            continue
        obligations.append(value)
        job_id = str(value.get("job_id") or path.stem)
        contract_id = str(value.get("contract_id") or "")
        linked_contract = contracts.get(contract_id)
        if linked_contract is None:
            errors.append(
                f"job obligation {job_id} references absent frozen invocation contract "
                f"{contract_id or '<missing>'}"
            )
        elif value.get("contract_hash") != linked_contract.get("contract_hash"):
            errors.append(f"job obligation contract hash differs: {job_id}")
        settled_receipt_id = str(value.get("settled_receipt_id") or "")
        if settled_receipt_id and settled_receipt_id not in receipts:
            errors.append(
                f"settled job obligation {job_id} references absent receipt "
                f"{settled_receipt_id}"
            )
    expected_obligations = {
        "detached-large-settlement": 1,
        "terminal-receipt-failure": 1,
        "multi-job-progress-barrier": 2,
    }.get(spec.name, 0)
    if len(obligations) != expected_obligations:
        errors.append(f"job obligation count differs: {len(obligations)} != {expected_obligations}")

    if facts is not None:
        fact_lineage = facts.get("controller_lineage") or {}
        fact_intents = {str(value) for value in fact_lineage.get("intent_ids") or ()}
        fact_envelopes = {str(value) for value in fact_lineage.get("envelope_ids") or ()}
        fact_contracts = {str(value) for value in fact_lineage.get("contract_ids") or ()}
        fact_receipt_contracts = {
            str(value) for value in fact_lineage.get("receipt_contract_ids") or ()
        }
        archived_intents = {
            str(payload.get("intent_id") or "") for payload in controller_envelopes.values()
        }
        archived_receipt_contracts = {
            str(receipt.get("contract_id") or "") for receipt in receipts.values()
        }
        if fact_intents != archived_intents:
            errors.append("controller intent facts differ from archived action envelopes")
        if fact_envelopes != set(controller_envelopes):
            errors.append("controller envelope facts differ from archived action envelopes")
        if fact_contracts != set(contracts):
            errors.append("controller contract facts differ from archived frozen contracts")
        if fact_receipt_contracts != archived_receipt_contracts:
            errors.append("receipt contract facts differ from archived physical receipts")
    return errors


def recompute_stop_audit(
    *,
    artifact_dir: Path,
    observation: ProbeObservation,
    spec: ProbeSpec,
    run_pin: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive every applicable D0 stop from sealed bytes, not boolean defaults."""

    reasons: list[str] = []
    for required in (
        "run-pin.json",
        "result.json",
        "evaluator-row.json",
        "control-events.jsonl",
    ):
        if not (artifact_dir / required).is_file():
            reasons.append(f"required archive evidence is absent: {required}")
    try:
        archived_pin = json.loads((artifact_dir / "run-pin.json").read_text(encoding="utf-8"))
        archived_result = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
        evaluator = json.loads((artifact_dir / "evaluator-row.json").read_text(encoding="utf-8"))
        if canonical_json(archived_pin) != canonical_json(run_pin):
            reasons.append("archived run pin differs from the dispatch pin")
        if canonical_json(archived_result) != canonical_json(observation.to_json()):
            reasons.append("archived evaluator input differs from the probe result")
        if evaluator.get("result_sha256") != canonical_sha256(archived_result):
            reasons.append("evaluator row is not bound to the archived result")
        if evaluator.get("evidence_grain") != probe_evaluator_grain(spec):
            reasons.append("evaluator row uses an unregistered evidence grain")
        if evaluator.get("disposition") != probe_evaluator_disposition(spec, observation):
            reasons.append("evaluator row disposition differs from the probe evidence class")
        if archived_pin.get("model_pin") != "none:no-model":
            reasons.append("D0 model pin is not the no-model sentinel")
        if archived_pin.get("prompt_bundle_sha256") != NOT_APPLICABLE_SHA256:
            reasons.append("D0 prompt pin is not the preregistered no-model hash")
    except (OSError, json.JSONDecodeError) as exc:
        reasons.append(f"required archive JSON could not be evaluated: {exc}")
    command_rows = 0
    command_texts: list[str] = []
    output_texts: list[str] = []
    command_outputs: list[tuple[str, str]] = []
    max_chars = 0
    for journal in sorted(artifact_dir.glob("container-*-commands.jsonl")):
        label = journal.name.removesuffix("-commands.jsonl")
        for line_number, line in enumerate(
            journal.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                reasons.append(f"invalid command journal JSON {journal.name}:{line_number}")
                continue
            command_rows += 1
            try:
                command = _blob_bytes(artifact_dir, label, row.get("command_ref")).decode(
                    "utf-8", "replace"
                )
                _blob_bytes(artifact_dir, label, row.get("output_ref"))
                full_output = _blob_bytes(
                    artifact_dir,
                    label,
                    row.get("full_output_ref") or row.get("output_ref"),
                ).decode("utf-8", "replace")
                _blob_bytes(artifact_dir, label, row.get("response_ref"))
            except D0Stop as exc:
                reasons.append(str(exc))
                continue
            if int(row.get("chars") or 0) != len(command):
                reasons.append(f"command length differs at {journal.name}:{line_number}")
            if row.get("kind") in {
                "execute_command",
                "execute_command_detached",
                "injected_receipt_failure",
            }:
                max_chars = max(max_chars, len(command))
            command_texts.append(command)
            output_texts.append(full_output)
            command_outputs.append((command, full_output))
    if command_rows == 0:
        reasons.append("no production command audit rows were archived")
    if max_chars > MAX_CONTAINER_COMMAND_CHARS:
        reasons.append(f"receipt/obligation command exceeded 60,200 characters: {max_chars}")
    if "argument list too long" in "\n".join(output_texts).casefold():
        reasons.append("argument list too long appeared in a full command output")
    complete_output = "\n".join(output_texts)
    complete_commands = "\n".join(command_texts)

    invalid_json: list[str] = []
    temporary_files: list[str] = []
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(artifact_dir).as_posix()
        if path.name.endswith(".tmp") or ".b64." in path.name and path.name.endswith(".tmp"):
            temporary_files.append(relative)
        if path.suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                invalid_json.append(relative)
        elif path.suffix == ".jsonl":
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        json.loads(line)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                invalid_json.append(relative)
    if invalid_json:
        reasons.append("invalid archived JSON: " + ", ".join(invalid_json))
    if temporary_files:
        reasons.append("temporary file survived: " + ", ".join(temporary_files))

    events: list[dict[str, Any]] = []
    control_path = artifact_dir / "control-events.jsonl"
    if not control_path.is_file():
        reasons.append("control stream is absent")
    else:
        from sag.agent.control_events import ControlEvent

        for line_number, line in enumerate(
            control_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                event = ControlEvent.model_validate_json(line)
            except Exception as exc:
                reasons.append(
                    f"invalid typed control event control-events.jsonl:{line_number}: "
                    f"{type(exc).__name__}"
                )
                continue
            events.append(event.model_dump(mode="json"))
    unsettled_ids = {
        str((event.get("payload") or {}).get("job_id") or "")
        for event in events
        if event.get("kind") == "job_unsettled"
    }
    terminal_ids: set[str] = set()
    obligation_ids: list[str] = []
    for path in artifact_dir.glob("container-*/.setup_agent/job_obligations/*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        job_id = str(value.get("job_id") or "")
        if job_id:
            obligation_ids.append(job_id)
        if value.get("process_state") == "terminal" and job_id:
            terminal_ids.add(job_id)
    overlap = sorted(terminal_ids.intersection(unsettled_ids))
    if overlap:
        reasons.append("terminal job emitted as job_unsettled: " + ", ".join(overlap))
    if len(obligation_ids) != len(set(obligation_ids)):
        reasons.append("duplicate job identity in archived obligation ledgers")

    expected_image = str((run_pin.get("image") or {}).get("id") or "")
    observed_images: list[str] = []
    for inspect_path in sorted(artifact_dir.glob("container-*-inspect.json")):
        payload = json.loads(inspect_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not payload:
            reasons.append(f"container inspect is empty: {inspect_path.name}")
            continue
        observed_images.append(str(payload[0].get("Image") or ""))
    if len(observed_images) != spec.container_count:
        reasons.append(
            f"fresh-container evidence count differs: {len(observed_images)} != "
            f"{spec.container_count}"
        )
    if any(image != expected_image for image in observed_images):
        reasons.append("container image revision drifted from the run pin")

    facts = dict(observation.facts)
    reasons.extend(required_probe_evidence_errors(spec, facts))
    reasons.extend(_archived_contract_lineage_errors(artifact_dir, spec, events, facts))

    kinds = [str(event.get("kind") or "") for event in events]
    reasons.extend(semantic_probe_evidence_errors(spec, facts, events))
    if spec.name == "sync-large-evidence":
        receipts = list(artifact_dir.glob("container-*/.setup_agent/invocation_receipts/*.json"))
        obligations = list(artifact_dir.glob("container-*/.setup_agent/job_obligations/*.json"))
        if len(receipts) != 1 or receipts[0].stat().st_size < MIN_LARGE_EVIDENCE_BYTES:
            reasons.append("archived synchronous receipt is absent or smaller than 3 MiB")
        if len(obligations) != 1 or obligations[0].stat().st_size < MIN_LARGE_EVIDENCE_BYTES:
            reasons.append("archived synchronous obligation is absent or smaller than 3 MiB")
    if spec.name == "detached-large-settlement":
        receipts = list(artifact_dir.glob("container-*/.setup_agent/invocation_receipts/*.json"))
        if len(receipts) != 1 or receipts[0].stat().st_size < MIN_LARGE_EVIDENCE_BYTES:
            reasons.append("archived detached settlement receipt is absent or smaller than 3 MiB")
        if not terminal_ids:
            reasons.append("detached settlement has no terminal obligation evidence")
        row_metrics = facts.get("row_metrics") or {}
        evaluator_metrics = facts.get("evaluator_metrics") or {}
        if any(
            int(value or 0) != EXPECTED_TESTCASE_ROWS
            for value in (
                facts.get("receipt_testcase_rows"),
                row_metrics.get("receipt_executions"),
                evaluator_metrics.get("receipt_executions"),
            )
        ):
            reasons.append("24k production row writer/reader/evaluator proof did not agree")
        metrics_paths = list(artifact_dir.glob("container-*/.setup_agent/report_metrics.json"))
        if len(metrics_paths) != 1:
            reasons.append("production metrics-v2 artifact is absent from detached row probe")
    if spec.name == "terminal-receipt-failure":
        lifecycle_sequence = [kind for kind in kinds if kind in TERMINAL_REPLAY_KINDS]
        if lifecycle_sequence != list(TERMINAL_EXPECTED_LIFECYCLE):
            reasons.append("terminal receipt lifecycle was not observed then terminal-unpersisted")
        if kinds.count("job_terminal_unpersisted") != 1:
            reasons.append("terminal receipt failure did not emit exactly one terminal event")
        if kinds.count("job_terminal_observed") != 1:
            reasons.append("terminal receipt failure did not emit exactly one observation")
        if TERMINAL_REPLAY_FORBIDDEN_KINDS.intersection(kinds):
            reasons.append("terminal receipt failure emitted contradictory lifecycle events")
        terminal_ledgers = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in artifact_dir.glob("container-*/.setup_agent/job_obligations/*.json")
        ]
        if len(terminal_ledgers) != 1 or (
            terminal_ledgers[0].get("process_state") != "terminal"
            or terminal_ledgers[0].get("settlement_state") != "unpersisted"
        ):
            reasons.append("terminal receipt failure ledger is not terminal/unpersisted")
        replay_paths = list(artifact_dir.glob("host-evidence/replay-transcript.jsonl"))
        replay_events: list[dict[str, Any]] = []
        replay_header: dict[str, Any] = {}
        if len(replay_paths) == 1:
            replay_rows = [
                json.loads(line)
                for line in replay_paths[0].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if replay_rows:
                replay_header = replay_rows[0]
                replay_events = replay_rows[1:]
        replay_terminal = [
            event for event in replay_events if event.get("kind") == "job_terminal_unpersisted"
        ]
        replay_observed = [
            event for event in replay_events if event.get("kind") == "job_terminal_observed"
        ]
        main_terminal = [
            event for event in events if event.get("kind") == "job_terminal_unpersisted"
        ]
        replay_kinds = [str(event.get("kind") or "") for event in replay_events]
        replay_lifecycle_kinds = [kind for kind in replay_kinds if kind in TERMINAL_REPLAY_KINDS]
        if replay_lifecycle_kinds != list(
            TERMINAL_EXPECTED_LIFECYCLE
        ) or TERMINAL_REPLAY_FORBIDDEN_KINDS.intersection(replay_kinds):
            reasons.append("archived replay stream is not exactly terminal-unpersisted")
        elif canonical_json(replay_terminal[0].get("payload") or {}) != canonical_json(
            main_terminal[0].get("payload") if main_terminal else {}
        ):
            reasons.append("archived replay terminal payload differs from the live barrier")
        if replay_header.get("expected_event_digest") != facts.get("replay_event_digest"):
            reasons.append("archived replay lock differs from the verified event digest")
        if facts.get("replay_event_digest") != facts.get("replay_expected_event_digest"):
            reasons.append("verified replay event digest differs from its frozen expectation")
        if facts.get("replay_unconsumed_events") or int(facts.get("replay_external_calls") or 0):
            reasons.append("offline replay consumed external work or left controller state")
    if spec.name == "multi-job-progress-barrier":
        job_ids = [str(item) for item in facts.get("job_ids") or ()]
        settled = [
            str((event.get("payload") or {}).get("job_id") or "")
            for event in events
            if event.get("kind") == "job_settled"
        ]
        if sorted(settled) != sorted(job_ids) or len(settled) != len(job_ids):
            reasons.append("multi-job barrier did not emit exactly one settlement per job")
        if any(kind in {"job_stall_observed", "job_live_at_close"} for kind in kinds):
            reasons.append("progressing multi-job barrier entered stall/live cleanup")
        if int(facts.get("sentinel_turns") or 0) != 1:
            reasons.append("production loop did not make exactly one post-settlement sentinel turn")
        if int(facts.get("intervening_model_turns") or 0) != 0:
            reasons.append("production loop made an intervening turn before settlement")
        progress = facts.get("progress_by_job") or {}
        if any(int(progress.get(job_id) or 0) < 2 for job_id in job_ids):
            reasons.append("each job did not prove progress across multiple barrier samples")
        archived_progress = progress_response_counts(command_outputs, job_ids=job_ids)
        if archived_progress != {job_id: int(progress.get(job_id) or 0) for job_id in job_ids}:
            reasons.append("archived progress-probe responses differ from barrier facts")
        roots = {
            str(value.get("working_directory") or "")
            for path in artifact_dir.glob("container-*/.setup_agent/job_obligations/*.json")
            for value in (json.loads(path.read_text(encoding="utf-8")),)
            if str(value.get("job_id") or "") in job_ids
        }
        if len(roots) != len(job_ids):
            reasons.append("multi-job progress identities did not use independent roots")
        progress_files = facts.get("progress_files") or {}
        archived_progress_counts = [
            len(list(root.glob("target/progress/*.txt")))
            for root in sorted(artifact_dir.glob("container-*/extra-evidence/barrier-job-*"))
        ]
        expected_progress_counts = sorted(int(value) for value in progress_files.values())
        if sorted(archived_progress_counts) != expected_progress_counts or len(
            archived_progress_counts
        ) != len(job_ids):
            reasons.append("archived independent progress artifacts differ from barrier facts")
        if "kill -TERM -- -" in complete_commands or "kill -KILL -- -" in complete_commands:
            reasons.append("progressing barrier issued a process-group signal")
    if spec.name == "http-wrapper-unzip-ablation":
        repetitions = facts.get("treatment_repetitions") or []
        if len(repetitions) != 3 or any(not item.get("maven_proper") for item in repetitions):
            reasons.append("three HTTP treatment repetitions did not reach Maven proper")
        if any(item.get("checksum_changed") for item in repetitions):
            reasons.append("HTTP wrapper checksum changed")
        control = facts.get("control") or {}
        if control.get("error_code") != "prerequisite_executable_missing:unzip":
            reasons.append("HTTP control did not reproduce typed missing-unzip failure")
        property_paths = sorted(
            artifact_dir.glob(
                "container-*/extra-evidence/http-*-checkout/.mvn/wrapper/"
                "maven-wrapper.properties"
            )
        )
        property_facts = facts.get("properties_sha256") or {}
        expected_property_sha = str(property_facts.get("expected") or "")
        if len(property_paths) != 2 or any(
            _sha256_file(path) != expected_property_sha for path in property_paths
        ):
            reasons.append("archived HTTP wrapper properties differ from the original checkout")
        wrapper_paths = sorted(artifact_dir.glob("container-*/extra-evidence/http-*-checkout/mvnw"))
        if len(wrapper_paths) != 2 or any(
            _sha256_file(path) != facts.get("wrapper_sha256") for path in wrapper_paths
        ):
            reasons.append("archived HTTP wrapper script differs from the original checkout")
        if facts.get("target_sha") != HTTP_TARGET_SHA:
            reasons.append("archived HTTP checkout does not bind the preregistered target SHA")
        checkout_roots = sorted(artifact_dir.glob("container-*/extra-evidence/http-*-checkout"))
        checkout_maps: list[dict[str, str]] = []
        for checkout in checkout_roots:
            checkout_maps.append(
                {
                    path.relative_to(checkout).as_posix(): _sha256_file(path)
                    for path in checkout.rglob("*")
                    if path.is_file()
                    and ".git" not in path.relative_to(checkout).parts
                    and "target" not in path.relative_to(checkout).parts
                }
            )
        if len(checkout_maps) != 2 or checkout_maps[0] != checkout_maps[1]:
            reasons.append("HTTP control/treatment checkout bytes differ outside the unzip arm")
        try:
            archived_receipts = _archived_receipt_records(artifact_dir)
        except D0Stop as exc:
            reasons.append(str(exc))
            archived_receipts = []
        wrapper_dispatches = dispatch_receipts(
            archived_receipts,
            tool="maven",
            working_directory="/workspace/httpcomponents-client",
            effective_action="validate",
        )
        if len(wrapper_dispatches) != 3:
            reasons.append(
                f"public HTTP build receipts recorded {len(wrapper_dispatches)} dispatches instead of 3"
            )
        if complete_output.count("BUILD SUCCESS") < 3:
            reasons.append("full outputs do not prove three Maven proper successes")
        if "apt-get remove" in complete_commands:
            reasons.append("HTTP arm mutated package state beyond adding unzip")
        mutation_guidance = re.search(
            r"(?:edit|change|replace|remove|disable).{0,80}distributionSha256Sum",
            complete_output,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if mutation_guidance:
            reasons.append("wrapper guidance proposed checksum mutation")
    if spec.name == "dynamic-jdk-authority":
        first = facts.get("first_call") or {}
        later = facts.get("later_call") or {}
        runtime = facts.get("runtime_requirement") or {}
        if first.get("jdk_retry") != {"from": "11", "to": "17"}:
            reasons.append("public build call did not perform one Java 11 to 17 retry")
        if not first.get("succeeded") or not later.get("succeeded"):
            reasons.append("public Java repair calls did not both complete successfully")
        if runtime.get("required_major") != "17" or later.get("effective_major") != "17":
            reasons.append("later public build call did not retain dynamic Java 17")
        try:
            archived_receipts = _archived_receipt_records(artifact_dir)
        except D0Stop as exc:
            reasons.append(str(exc))
            archived_receipts = []
        wrapper_dispatches = dispatch_receipts(
            archived_receipts,
            tool="maven",
            working_directory="/workspace/d0-jdk",
            effective_action="compile",
        )
        if len(wrapper_dispatches) != 3:
            reasons.append(
                "Java public BuildTool evidence did not contain original, retry, and later Maven calls"
            )
        if "Required Java version 17" not in complete_output and not re.search(
            r"RequireJavaVersion.*allowed(?:\s+version)?\s+range\s*\[?17",
            complete_output,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            reasons.append("full Maven output lacks the runner-observed Java-17 failure")
        overlay_paths = list(artifact_dir.glob("container-*/.setup_agent/env_overlay.json"))
        runtime_rows: list[dict[str, Any]] = []
        if len(overlay_paths) == 1:
            overlay = json.loads(overlay_paths[0].read_text(encoding="utf-8"))
            runtime_rows = list(
                (((overlay.get("tools") or {}).get("java") or {}).get("runtime_requirements")) or []
            )
        if not any(
            row.get("required_major") == "17"
            and (row.get("active_runtime") or {}).get("major") == "17"
            and row.get("source_ref")
            for row in runtime_rows
        ):
            reasons.append("archived dynamic runtime store lacks confirmed Java 17 evidence")
        later_contract_id = str((facts.get("later_call") or {}).get("contract_id") or "")
        later_contract_paths = list(
            artifact_dir.glob(
                f"container-*/.setup_agent/invocation_contracts/{later_contract_id}.json"
            )
        )
        if len(later_contract_paths) != 1:
            reasons.append("later Java invocation contract is absent from the archive")
        else:
            binding = (
                json.loads(later_contract_paths[0].read_text(encoding="utf-8")).get("effective_jdk")
                or {}
            )
            if (
                binding.get("major") != "17"
                or binding.get("requirement_authority") != "persisted_dynamic"
            ):
                reasons.append("archived later contract does not bind persisted-dynamic Java 17")
    if spec.name == "gradle-runner-classification":
        if facts.get("operation_outcome") != "success" or facts.get("runner") != "gradle":
            reasons.append("Gradle success was failed by a foreign runner classifier")
        try:
            archived_receipts = _archived_receipt_records(artifact_dir)
        except D0Stop as exc:
            reasons.append(str(exc))
            archived_receipts = []
        gradle_dispatches = dispatch_receipts(
            archived_receipts,
            tool="gradle",
            working_directory="/workspace/d0-gradle",
            effective_action="test",
        )
        if len(gradle_dispatches) != 1 or gradle_dispatches[0].get("exit_code") != 0:
            reasons.append("archive lacks one successful public Gradle dispatch receipt")
        if "BUILD SUCCESSFUL" not in complete_output or "Errors" not in complete_output:
            reasons.append("full Gradle output lacks the preregistered success/error-name shape")

    if not observation.passed:
        reasons.append("probe implementation reported failure: " + "; ".join(observation.failures))
    if observation.evidence_grains_combined:
        reasons.append("claimed and non-claimable evidence grains were combined")
    return {
        "schema_version": 1,
        "status": "passed" if not reasons else "failed",
        "probe": spec.name,
        "reasons": list(dict.fromkeys(reasons)),
        "derived": {
            "command_rows": command_rows,
            "max_command_chars": max_chars,
            "full_output_blobs": len(output_texts),
            "control_event_kinds": kinds,
            "terminal_job_ids": sorted(terminal_ids),
            "unsettled_job_ids": sorted(unsettled_ids),
            "image_ids": observed_images,
            "temporary_files": temporary_files,
            "invalid_json": invalid_json,
        },
    }


def enforce_stop_audit(audit: Mapping[str, Any]) -> None:
    if audit.get("status") != "passed":
        reasons = [str(item) for item in audit.get("reasons") or ()]
        raise D0Stop("independent stop audit failed: " + "; ".join(reasons))


def _large_report_map(minimum_json_bytes: int = MIN_LARGE_EVIDENCE_BYTES) -> dict[str, str]:
    """A deterministic, valid receipt delta source larger than the target."""
    reports: dict[str, str] = {}
    digest = hashlib.sha256(b"<testsuite/>").hexdigest()
    index = 0
    while True:
        for _ in range(2_000):
            leaf = f"TEST-d0-{index:06d}-{'x' * 72}.xml"
            reports[f"/workspace/d0/target/surefire-reports/{leaf}"] = digest
            index += 1
        # The receipt contains these as object rows and is slightly larger
        # than this mapping.  Using the mapping size as the loop floor keeps
        # both receipt and obligation comfortably over 3 MiB.
        if len(json.dumps(reports, sort_keys=True).encode("utf-8")) >= minimum_json_bytes:
            return reports


def _publish_d0_build_requirements(
    audit: CommandAudit,
    root: str,
    *,
    system: str = "maven",
    java_version: str | None = None,
    java_version_source: str | None = None,
    target_sha: str | None = None,
) -> dict[str, Any]:
    """Host-publish one strict build-requirements v1 for a single-module fixture.

    Every public runner refuses to dispatch — probe, pre-flight and all — until
    the manifest is a complete current host publication
    (``BUILD_REQUIREMENTS_UNAVAILABLE``).  In a real run the survey writes it;
    D0 drives the tools directly, so the probe owns the same write.

    ``build_domains``/``domain_facts`` describe a MULTI-domain graph and are
    refused for a single root, so a one-module fixture omits them and
    production scopes the runtime to the invocation's own root.  The survey
    fingerprint is a pure function of the derived facts; ``write_build_requirements``
    stamps it at the persistence boundary, and computing it here keeps the
    body self-consistent before it leaves the runner.
    """

    from sag.tools.internal.build_preflight import (
        BUILD_REQUIREMENTS_SCHEMA_VERSION,
        survey_facts_fingerprint,
        validate_build_requirements_v1,
        write_build_requirements,
    )
    from sag.tools.internal.project_analyzer import SURVEY_FACTS_VERSION

    manifest: dict[str, Any] = {
        "schema_version": BUILD_REQUIREMENTS_SCHEMA_VERSION,
        "survey": {
            "project_path": root,
            "analyzer_version": SURVEY_FACTS_VERSION,
            "config_fingerprint": None,
            "target_sha": target_sha,
            "document_map_fingerprint": None,
        },
        "java_version": java_version,
        "java_version_source": java_version_source,
        "java_version_enforced": False,
        "root_shape": "single_module",
        "build_root": root,
        "fail_at_end": False,
        "test_root": None,
        "test_system": None,
        "test_fail_at_end": False,
        "build_islands": [{"root": root, "system": system, "goal": "build"}],
        "test_islands": [],
    }
    manifest["survey"] = {
        **manifest["survey"],
        "survey_fingerprint": survey_facts_fingerprint(manifest),
    }
    validate_build_requirements_v1(manifest)
    if not write_build_requirements(audit, manifest):
        raise D0Error(f"D0 build requirements were not host published for {root}")
    return manifest


def _container_file_facts(
    audit: CommandAudit,
    path: str,
    *,
    expect_json: bool = True,
) -> dict[str, Any]:
    """Byte count and digest of one persisted file, JSON-validated on request.

    Evidence records are JSON and must parse.  Some pinned fixtures are not —
    ``maven-wrapper.properties`` is a Java properties file — and demanding JSON
    of them fails the read on the fixture's format rather than on the fact the
    probe is measuring (that its bytes did not change).
    """

    check = "json.loads(b.decode('utf-8'));valid=True;" if expect_json else "valid=None;"
    program = (
        "import hashlib,json,sys;"
        "p=sys.argv[1];b=open(p,'rb').read();"
        f"{check}"
        "print(json.dumps({'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),"
        "'json_valid':valid},sort_keys=True))"
    )
    command = f"python3 -c {shlex.quote(program)} {shlex.quote(path)}"
    # Reading already-persisted evidence bytes is archive I/O, not a project
    # command: it neither produces nor consumes the project runtime overlay.
    result = audit.execute_control_command(command, truncate_output=False)
    if result.get("exit_code") != 0:
        raise D0Error(f"cannot read persisted file {path}: {result.get('output')}")
    try:
        value = json.loads(str(result.get("output") or "").strip().splitlines()[-1])
        if not isinstance(value, dict):
            raise D0Error(f"file-facts response is not an object for {path}")
        return cast(dict[str, Any], value)
    except (json.JSONDecodeError, IndexError) as exc:
        raise D0Error(f"invalid file-facts response for {path}") from exc


def _read_container_json(audit: CommandAudit, path: str) -> dict[str, Any]:
    # Archive read of persisted evidence: clean control channel.
    result = audit.execute_control_command(f"cat {shlex.quote(path)}", truncate_output=False)
    if result.get("exit_code") != 0:
        raise D0Error(f"cannot read container JSON {path}")
    try:
        value = json.loads(str(result.get("output") or ""))
    except json.JSONDecodeError as exc:
        raise D0Error(f"container JSON is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise D0Error(f"container JSON is not an object: {path}")
    return value


def dispatch_receipts(
    records: Iterable[Mapping[str, Any]],
    *,
    tool: str,
    working_directory: str,
    effective_action: str,
) -> list[dict[str, Any]]:
    """Select physical runner dispatches from durable invocation receipts.

    Wrapper discovery, ``--version`` probes, fixture setup, and post-run checks
    can all contain the wrapper executable in their command text.  None is a
    build dispatch.  A production receipt is the dispatch boundary, so D0
    counts only schema-v2 receipts with the exact runner/root/action identity.
    """

    selected: list[dict[str, Any]] = []
    for raw in records:
        record = dict(raw)
        actual_cwd = str(record.get("actual_cwd") or record.get("working_directory") or "")
        if (
            record.get("schema_version") == 2
            and str(record.get("receipt_id") or "").strip()
            and str(record.get("tool") or "") == tool
            and actual_cwd.rstrip("/") == working_directory.rstrip("/")
            and str(record.get("effective_action") or "") == effective_action
            and isinstance(record.get("exit_code"), int)
            and not isinstance(record.get("exit_code"), bool)
        ):
            selected.append(record)
    return selected


def _receipts_in_dispatch_order(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Order durable receipts by the process-global ordinal in their identity.

    Receipt ids carry a monotonic ordinal as their final component, so this is
    dispatch order without trusting the reader's directory listing.
    """

    def ordinal(record: Mapping[str, Any]) -> int:
        tail = str(record.get("receipt_id") or "").rsplit("-", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return -1

    return sorted((dict(record) for record in records), key=ordinal)


_PROGRESS_RESPONSE_FIELDS = (
    "process_state",
    "log_size",
    "process_identity_sha256",
    "artifact_sha256",
    "report_sha256",
    "cpu_ticks_delta",
    "process_count",
    "child_count",
)


def progress_response_counts(
    command_outputs: Iterable[tuple[str, str]],
    *,
    job_ids: Iterable[str],
) -> dict[str, int]:
    """Count changed complete progress-probe responses for each registered job.

    This is intentionally independent of the D0 loop-memory observer.  The
    production progress probe emits a bounded typed response; the command audit
    preserves that complete response in CAS, so the archive can reproduce this
    count without trusting a runner-authored summary field.
    """

    expected = {str(job_id) for job_id in job_ids if str(job_id)}
    previous: dict[str, str] = {}
    counts = {job_id: 0 for job_id in expected}
    for command, output in command_outputs:
        if "mktemp -d /tmp/sag-job-progress." not in command:
            continue
        fields: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip().lower()] = value.strip()
        job_id = fields.get("job_id", "")
        if job_id not in expected:
            continue
        if any(
            fields.get(name) != "1"
            for name in ("identity_complete", "artifact_complete", "report_complete")
        ):
            continue
        if any(name not in fields for name in _PROGRESS_RESPONSE_FIELDS):
            continue
        fingerprint = canonical_sha256({name: fields[name] for name in _PROGRESS_RESPONSE_FIELDS})
        prior = previous.get(job_id)
        if prior is not None and prior != fingerprint:
            counts[job_id] += 1
        previous[job_id] = fingerprint
    return counts


def audited_command_outputs(audit: CommandAudit) -> list[tuple[str, str]]:
    """Return complete command/response pairs from the in-memory CAS journal."""

    pairs: list[tuple[str, str]] = []
    for row in sorted(audit.records, key=lambda item: int(item["command_index"])):
        command_digest = str(row.get("command_ref") or "").removeprefix("sha256:")
        output_digest = str(row.get("full_output_ref") or row.get("output_ref") or "").removeprefix(
            "sha256:"
        )
        command = audit.blobs.get(command_digest)
        output = audit.blobs.get(output_digest)
        if command is None or output is None:
            raise D0Error("in-memory command audit CAS is incomplete")
        if hashlib.sha256(command).hexdigest() != command_digest:
            raise D0Error("in-memory command audit command hash differs")
        if hashlib.sha256(output).hexdigest() != output_digest:
            raise D0Error("in-memory command audit output hash differs")
        pairs.append(
            (
                command.decode("utf-8", "replace"),
                output.decode("utf-8", "replace"),
            )
        )
    return pairs


def _strict_container_receipts(audit: CommandAudit) -> list[dict[str, Any]]:
    """Read the complete production receipt stream, failing on any bad row."""

    from sag.agent.evidence_records import (
        EvidencePublicationBinding,
        read_live_published_json_records,
    )
    from sag.agent.invocation_receipts import (
        RECEIPT_DIR,
        receipt_record_scope,
        validate_receipt_v2,
    )

    read = read_live_published_json_records(
        audit,
        RECEIPT_DIR,
        record_kind="invocation_receipt",
        validator=lambda payload, record_id: validate_receipt_v2(payload, expected_id=record_id),
        publication_binding=lambda payload: EvidencePublicationBinding(
            run_id=str(payload.get("run_id") or ""),
            contract_id=(str(payload["contract_id"]) if "contract_id" in payload else None),
            contract_hash=(str(payload["contract_hash"]) if "contract_hash" in payload else None),
        ),
        record_scope=receipt_record_scope,
    )
    if not read.complete or read.conflict is not None:
        detail = f": {read.detail}" if read.detail else ""
        raise D0Error(f"invocation receipt stream was not host-authorized{detail}")
    return [dict(record.payload) for record in read.records]


_IMMUTABLE_ARCHIVE_DIRS = {
    "invocation_contract": "invocation_contracts",
    "invocation_receipt": "invocation_receipts",
    "receipt_assessment": "evidence_assessments",
    "policy_claim": "claims",
    "repair_context": "repair_contexts",
    "testcase_row_input": ".testcase-row-input",
}
_MUTABLE_ARCHIVE_FILES = {
    "build_requirements": "build_requirements.json",
    "receipt_structure": "build_requirements.json",
    "run_pin": "run-pin.json",
    "document_map": "document_map.json",
    "report_metrics": "report_metrics.json",
    # The runtime env overlay is a host-published mutable artifact too: a
    # dynamic-JDK repair advances its head, and an archive that cannot resolve
    # that record cannot verify the epoch it belongs to.
    "env_overlay": "env_overlay.json",
}


def _archive_publication_path(
    setup_agent_dir: Path,
    *,
    record_kind: str,
    record_id: str,
) -> Path:
    directory = _IMMUTABLE_ARCHIVE_DIRS.get(record_kind)
    if directory is not None:
        return setup_agent_dir / directory / f"{record_id}.json"
    fixed = _MUTABLE_ARCHIVE_FILES.get(record_kind)
    if fixed is not None:
        return setup_agent_dir / fixed
    if record_kind == "job_obligation":
        return setup_agent_dir / "job_obligations" / f"{record_id}.json"
    if record_kind == "stall_diagnostic" and record_id.startswith("stall-diagnostic-"):
        job_id = record_id.removeprefix("stall-diagnostic-")
        return setup_agent_dir / "job_diagnostics" / job_id / "bundle.json"
    if record_kind == "stall_seal" and record_id.startswith("stall-seal-"):
        job_id = record_id.removeprefix("stall-seal-")
        return setup_agent_dir / "job_diagnostics" / job_id / "seal.json"
    if record_kind == "stall_cleanup" and record_id.startswith("stall-cleanup-"):
        job_id = record_id.removeprefix("stall-cleanup-")
        return setup_agent_dir / "job_diagnostics" / job_id / "cleanup.json"
    raise D0Stop(f"archive has no semantic path for published {record_kind}:{record_id}")


def _validate_archived_semantic_identity(
    *,
    record_kind: str,
    record_id: str,
    raw: bytes,
    run_id: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise D0Stop(f"published {record_kind}:{record_id} is not strict JSON") from exc
    if not isinstance(payload, dict):
        raise D0Stop(f"published {record_kind}:{record_id} is not a JSON object")
    identity_fields = {
        "invocation_contract": "contract_id",
        "invocation_receipt": "receipt_id",
        "receipt_assessment": "assessment_id",
        "policy_claim": "claim_id",
        "job_obligation": "job_id",
        "repair_context": "repair_context_id",
    }
    identity_field = identity_fields.get(record_kind)
    if identity_field is not None and payload.get(identity_field) != record_id:
        raise D0Stop(
            f"published {record_kind} semantic identity differs from filename: {record_id}"
        )
    if record_kind in {"invocation_contract", "invocation_receipt", "job_obligation"}:
        if payload.get("run_id") != run_id:
            raise D0Stop(f"published {record_kind}:{record_id} belongs to another epoch")
    if record_kind == "invocation_receipt":
        from sag.agent.invocation_receipts import validate_receipt_v2

        try:
            normalized = validate_receipt_v2(payload, expected_id=record_id)
        except (TypeError, ValueError) as exc:
            raise D0Stop(
                f"published invocation receipt is semantically invalid: {record_id}"
            ) from exc
        if normalized != payload:
            raise D0Stop(f"published invocation receipt is noncanonical: {record_id}")
    elif record_kind == "invocation_contract":
        expected_hash = canonical_sha256(
            {key: value for key, value in payload.items() if key != "contract_hash"}
        )
        if payload.get("contract_hash") != expected_hash:
            raise D0Stop(f"published invocation contract hash differs: {record_id}")
    elif record_kind == "receipt_assessment":
        from sag.agent.evidence_assessments import validate_assessment_v2

        try:
            normalized = validate_assessment_v2(payload, expected_id=record_id)
        except (TypeError, ValueError) as exc:
            raise D0Stop(f"published receipt assessment is invalid: {record_id}") from exc
        if normalized != payload:
            raise D0Stop(f"published receipt assessment is noncanonical: {record_id}")
    elif record_kind == "job_obligation":
        # The live obligation schema is v3 (docker-exec identity envelope).
        from sag.agent.job_obligations import validate_obligation_v3

        try:
            normalized = validate_obligation_v3(payload, expected_id=record_id)
        except (TypeError, ValueError) as exc:
            raise D0Stop(f"published job obligation is invalid: {record_id}") from exc
        if normalized != payload:
            raise D0Stop(f"published job obligation is noncanonical: {record_id}")
    elif record_kind == "run_pin":
        from sag.agent.control_events import RunPin

        try:
            normalized = RunPin.model_validate(payload).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise D0Stop("published run pin is invalid") from exc
        if normalized != payload:
            raise D0Stop("published run pin is noncanonical")
    elif record_kind == "report_metrics":
        from sag.tools.report_metrics import read_report_metrics

        normalized = read_report_metrics(payload)
        if not isinstance(normalized, dict) or normalized != payload:
            raise D0Stop("published report metrics is not canonical metrics v2")
    return payload


def _verify_one_archived_evidence_epoch(
    *,
    container_dir: Path,
    association: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Strictly recover one host stream, then verify that container's exact store."""

    from sag.agent.evidence_publications import EvidencePublicationAuthority

    run_id = str(association.get("run_id") or "")
    if (
        set(association)
        != {
            "schema_version",
            "run_id",
            "container_name",
            "host_control_event_path",
            "control_event_path",
        }
        or association.get("schema_version") != 1
    ):
        raise D0Stop(f"{container_dir.name} evidence epoch association fields are invalid")
    if not str(association.get("container_name") or "") or not str(
        association.get("host_control_event_path") or ""
    ):
        raise D0Stop(f"{container_dir.name} evidence epoch association is incomplete")
    expected_stream = f"{container_dir.name}/control-events.jsonl"
    if association.get("control_event_path") != expected_stream:
        raise D0Stop(f"{container_dir.name} epoch association points to another host stream")
    stream = container_dir / "control-events.jsonl"
    try:
        authority = EvidencePublicationAuthority.recover_from_host_jsonl(stream, run_id=run_id)
    except Exception as exc:
        raise D0Stop(f"{container_dir.name} host publication recovery failed: {exc}") from exc
    setup_agent_dir = container_dir / ".setup_agent"
    snapshot = authority.snapshot()
    expected_present_paths: set[Path] = set()
    expected_absent_paths: set[Path] = set()

    for record_kind, expected_ids in snapshot.immutable_record_ids.items():
        expected = set(expected_ids)
        directory = _IMMUTABLE_ARCHIVE_DIRS.get(record_kind)
        if directory is not None:
            root = setup_agent_dir / directory
            observed = {path.stem for path in root.glob("*.json") if path.is_file()}
            if observed != expected:
                raise D0Stop(
                    f"{container_dir.name} {record_kind} files differ from host expected set"
                )
        for record_id in expected_ids:
            path = _archive_publication_path(
                setup_agent_dir, record_kind=record_kind, record_id=record_id
            )
            if not path.is_file():
                raise D0Stop(
                    f"published evidence file is absent: {path.relative_to(container_dir)}"
                )
            expected_present_paths.add(path)
            raw = path.read_bytes()
            payload = _validate_archived_semantic_identity(
                record_kind=record_kind,
                record_id=record_id,
                raw=raw,
                run_id=run_id,
            )
            contract_id = None
            contract_hash = None
            if record_kind == "invocation_contract":
                contract_id = str(payload.get("contract_id") or "")
                contract_hash = str(payload.get("contract_hash") or "")
            elif record_kind == "invocation_receipt" and "contract_id" in payload:
                contract_id = str(payload.get("contract_id") or "")
                contract_hash = str(payload.get("contract_hash") or "")
            publication = authority.verify_bytes(
                record_kind=record_kind,
                record_id=record_id,
                raw=raw,
                run_id=run_id,
                contract_id=contract_id,
                contract_hash=contract_hash,
            )
            if not publication.authorized:
                raise D0Stop(f"published evidence bytes differ: {record_kind}:{record_id}")

    mutable_paths_seen: dict[Path, str] = {}
    for logical_id, head in snapshot.mutable_heads.items():
        path = _archive_publication_path(
            setup_agent_dir,
            record_kind=head.record_kind,
            record_id=head.record_id,
        )
        prior = mutable_paths_seen.get(path)
        if prior is not None and prior != logical_id:
            raise D0Stop("two mutable host heads resolve to one archived file")
        mutable_paths_seen[path] = logical_id
        if head.publication_state == "revoked":
            expected_absent_paths.add(path)
            if path.exists():
                raise D0Stop(
                    f"tombstoned evidence was resurrected: {path.relative_to(container_dir)}"
                )
            continue
        if not path.is_file():
            raise D0Stop(f"latest mutable evidence is absent: {path.relative_to(container_dir)}")
        expected_present_paths.add(path)
        raw = path.read_bytes()
        check = authority.verify_latest_bytes(
            record_kind=head.record_kind,
            record_id=head.record_id,
            logical_artifact_id=logical_id,
            raw=raw,
            run_id=run_id,
            contract_id=head.contract_id,
            contract_hash=head.contract_hash,
        )
        if not check.authorized:
            raise D0Stop(
                f"latest mutable evidence bytes differ: {head.record_kind}:{head.record_id}"
            )
        _validate_archived_semantic_identity(
            record_kind=head.record_kind,
            record_id=head.record_id,
            raw=raw,
            run_id=run_id,
        )

    known_paths: set[Path] = set()
    for directory in set(_IMMUTABLE_ARCHIVE_DIRS.values()):
        known_paths.update(
            path for path in (setup_agent_dir / directory).glob("*.json") if path.is_file()
        )
    known_paths.update(
        path for path in (setup_agent_dir / "job_obligations").glob("*.json") if path.is_file()
    )
    known_paths.update(
        path
        for leaf in ("bundle.json", "seal.json", "cleanup.json")
        for path in (setup_agent_dir / "job_diagnostics").glob(f"*/{leaf}")
        if path.is_file()
    )
    known_paths.update(
        path
        for leaf in set(_MUTABLE_ARCHIVE_FILES.values())
        if (path := setup_agent_dir / leaf).is_file()
    )
    if known_paths != expected_present_paths:
        extras = sorted(
            path.relative_to(container_dir).as_posix()
            for path in known_paths - expected_present_paths
        )
        missing = sorted(
            path.relative_to(container_dir).as_posix()
            for path in expected_present_paths - known_paths
        )
        raise D0Stop(
            f"{container_dir.name} archived evidence differs from host expected set: "
            f"extra={extras} missing={missing}"
        )
    if any(path.exists() for path in expected_absent_paths):
        raise D0Stop(f"{container_dir.name} contains a tombstoned evidence artifact")

    return _control_events(stream)


def _verify_archived_evidence_epochs(artifact_dir: Path) -> list[dict[str, Any]]:
    """Verify every container epoch before returning a campaign event projection."""

    events: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    streams: set[Path] = set()
    store_names: set[str] = set()
    container_dirs = sorted(path for path in artifact_dir.glob("container-*") if path.is_dir())
    for container_dir in container_dirs:
        association_path = container_dir / "evidence-epoch.json"
        try:
            association = json.loads(association_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise D0Stop(f"{container_dir.name} evidence epoch association is invalid") from exc
        if not isinstance(association, dict):
            raise D0Stop(f"{container_dir.name} evidence epoch association is not an object")
        run_id = str(association.get("run_id") or "")
        store_name = str(association.get("container_name") or "")
        stream = (container_dir / "control-events.jsonl").resolve()
        if not run_id or run_id in run_ids:
            raise D0Stop("fresh containers do not have distinct evidence run ids")
        if stream in streams:
            raise D0Stop("fresh containers do not have distinct host control streams")
        if not store_name or store_name in store_names:
            raise D0Stop("fresh containers do not have distinct store associations")
        run_ids.add(run_id)
        streams.add(stream)
        store_names.add(store_name)
        events.extend(
            _verify_one_archived_evidence_epoch(
                container_dir=container_dir,
                association=association,
            )
        )
    return events


def _archived_receipt_records(artifact_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(artifact_dir.glob("container-*/.setup_agent/invocation_receipts/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise D0Stop(f"archived invocation receipt is unreadable: {path.name}") from exc
        if not isinstance(value, dict):
            raise D0Stop(f"archived invocation receipt is not an object: {path.name}")
        records.append(value)
    return records


def _controller_call_token(
    runtime: DockerProbeRuntime, label: str, sequence: int, *, run_id: str = ""
) -> str:
    material = "\0".join(
        (
            str(runtime.campaign_id),
            str(runtime.spec.name),
            str(run_id),
            str(label),
            str(sequence),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _controller_epoch(
    runtime: DockerProbeRuntime,
    *,
    audit: CommandAudit | None,
) -> tuple[Any, str, Any, CommandAudit]:
    """Return the one pre-registered sink/run/scope/audit for a container."""

    if audit is None:
        if len(runtime.audits) != 1:
            raise D0Error("multi-container controller action must identify its audit")
        audit = runtime.audits[0]
    epoch = runtime.epoch_for(audit)
    return epoch.sink, epoch.run_id, runtime.evidence_epoch(audit), audit


def _controller_evidence_scope(tool: str, params: Mapping[str, Any]) -> str:
    """The registered state scope production would assign this action.

    Mirrors ``ReActEngine._tool_evidence_scope`` for the tools D0 drives: a
    backend runner claims artifacts unless its operation is a test operation,
    and every other D0 action is project analysis (D0 runs no phase machine,
    which is the branch production falls back to when no phase is active).
    """

    operations = " ".join(
        str(params.get(key) or "")
        for key in ("action", "command", "args", "goals", "tasks")
    ).lower()
    if str(tool) in {"build", "maven", "gradle", "python"}:
        if any(token in operations for token in ("test", "verify", "check")):
            return "test_runtime"
        return "artifacts"
    return "project_analysis"


@contextmanager
def _controller_action_scope(
    runtime: DockerProbeRuntime,
    *,
    audit: CommandAudit | None = None,
    label: str,
    domain_id: str,
    tool: str,
    params: Mapping[str, Any],
    next_action_kind: str,
    predecessor_contract_id: str | None = None,
) -> Iterator[ControllerActionBinding]:
    """Emit and scope one real controller-selected D0 action.

    D0 has no model, but its physical calls still cross the same public
    intent/envelope boundary as a normal run.  The envelope is durable before
    the tool is entered, and the thread-local action scope is restored on every
    exit path so one probe cannot lend authority to the next one.

    The scope also ANSWERS its own envelope.  A control stream is a grammar,
    not a log: an ``action_envelope`` is open until a matching ``tool_result``
    closes it, and production's strict recovery
    (``ReActEngine._restore_active_repair_context`` ->
    ``recover_active_repair_context_from_path``) refuses a stream that ends on
    an unanswered envelope.  A D0 direct dispatch has no engine tool loop to
    emit that answer, so this scope emits it on every exit path — including a
    raising one, which is exactly when a half-open envelope would otherwise be
    sealed into the archive.
    """

    from sag.agent.action_intents import (
        EngineActionIntentFactory,
        bounded_exact_params,
    )
    from sag.agent.control_events import (
        ActionEnvelopePayload,
        ToolResultPayload,
        action_envelope_sha256,
    )
    from sag.agent.invocation_contracts import action_context
    from sag.tools.base import bind_tool_result_output_storage

    sink, run_id, epoch_scope, scope_audit = _controller_epoch(runtime, audit=audit)
    with epoch_scope:
        exact_params = bounded_exact_params(params)
        sequence = sink.sequence + 1
        token = _controller_call_token(runtime, label, sequence, run_id=run_id)
        tool_call_id = f"d0-call-{token}"
        envelope_id = f"d0-envelope-{token}"
        intent = EngineActionIntentFactory.for_controller().from_submission(
            {
                "domain_id": domain_id,
                "tool": tool,
                "params": exact_params,
                "next_action_kind": next_action_kind,
            },
            tool_call_id=tool_call_id,
            predecessor_contract_id=predecessor_contract_id,
        )
        envelope = ActionEnvelopePayload(
            envelope_id=envelope_id,
            tool_call_id=tool_call_id,
            tool=intent.tool,
            exact_params=exact_params,
            intent_id=intent.intent_id,
            intent_source=intent.source,
            action_fingerprint=intent.action_fingerprint,
            envelope_sha256=action_envelope_sha256(
                tool_call_id=tool_call_id,
                tool=intent.tool,
                exact_params=exact_params,
                intent_id=intent.intent_id,
                intent_source=intent.source,
                action_fingerprint=intent.action_fingerprint,
            ),
        )
        sink.emit("action_envelope", envelope)
        binding = ControllerActionBinding(
            envelope_id=envelope_id,
            tool_call_id=tool_call_id,
            tool=intent.tool,
            domain_id=intent.domain_id,
            exact_params=exact_params,
            intent_id=intent.intent_id,
            intent_source=intent.source,
            action_fingerprint=intent.action_fingerprint,
            predecessor_contract_id=intent.predecessor_contract_id,
        )
        status = "crashed"
        try:
            with action_context(
                envelope_id=binding.envelope_id,
                intent_source=binding.intent_source,
                intent_id=binding.intent_id,
                intent_domain_id=binding.domain_id,
                intent_exact_params=binding.exact_params,
                action_fingerprint=binding.action_fingerprint,
                predecessor_contract_id=binding.predecessor_contract_id,
            ), bind_tool_result_output_storage(
                runtime.output_storage_for(scope_audit),
                task_id=binding.tool_call_id,
                tool_name=binding.tool,
            ):
                yield binding
            status = "completed"
        finally:
            completed = status == "completed"
            sink.emit(
                "tool_result",
                ToolResultPayload(
                    envelope_id=binding.envelope_id,
                    execution_id=f"d0-exec-{token}",
                    tool=binding.tool,
                    params=exact_params,
                    scope=_controller_evidence_scope(binding.tool, exact_params),
                    result={
                        "invocation_status": status,
                        "operation_outcome": "success" if completed else "failed",
                        "evidence_status": "verified" if completed else "unknown",
                        "evidence_assessment": "success" if completed else "unknown",
                        # The probe archive is the evidence body; a control
                        # event never embeds one.
                        "output": (
                            "d0 controller-owned direct dispatch; "
                            "evidence body is the sealed probe archive"
                        ),
                        "metadata": {"d0_probe": runtime.spec.name, "d0_label": label},
                    },
                ),
            )


@contextmanager
def _controller_dispatch_scope(
    runtime: DockerProbeRuntime,
    execute: Callable[..., Any],
    *,
    label: str,
    domain_id: str,
    tool: str,
    params: Mapping[str, Any],
    next_action_kind: str,
    effective_action: str,
    expected_cwd: str,
    expected_argv: str | None,
    requirements: Mapping[str, Any] | None = None,
    predecessor_contract_id: str | None = None,
    target_sha_value: str | None = None,
    effective_tool: str | None = None,
) -> Iterator[ControllerDispatchBinding]:
    """Freeze one exact direct-runner contract, then expose its dispatch scope.

    ``effective_tool`` is the executor family whose evidence conventions the
    dispatch produces; it defaults to the public action tool.  Production keeps
    the two separate on purpose (``EXECUTOR_ALLOWLIST_V1``), and the settlement
    receipt's row writer reads the executor, not the public tool.
    """

    from sag.agent.invocation_contracts import (
        ARGV_EXECUTION_BINDING,
        dispatch_contract,
        freeze_contract,
    )
    from sag.agent.invocation_receipts import active_receipt_run_id

    audit = runtime.audit_for_source(execute) if isinstance(runtime, DockerProbeRuntime) else None

    with _controller_action_scope(
        runtime,
        audit=audit,
        label=label,
        domain_id=domain_id,
        tool=tool,
        params=params,
        next_action_kind=next_action_kind,
        predecessor_contract_id=predecessor_contract_id,
    ) as action:
        contract = freeze_contract(
            execute,
            run_id=active_receipt_run_id(),
            envelope_id=action.envelope_id,
            tool=action.tool,
            params=action.exact_params,
            effective_tool=effective_tool or tool,
            effective_action=effective_action,
            expected_cwd=expected_cwd,
            expected_argv=expected_argv,
            execution_binding=ARGV_EXECUTION_BINDING,
            intent_source=action.intent_source,
            intent_id=action.intent_id,
            intent_domain_id=action.domain_id,
            intent_exact_params=action.exact_params,
            action_fingerprint=action.action_fingerprint,
            requirements=requirements,
            predecessor_contract_id=action.predecessor_contract_id,
            target_sha_value=target_sha_value,
        )
        if contract is None:
            raise D0Stop(
                f"controller dispatch contract did not persist before {label}; "
                "the physical runner was refused"
            )
        binding = ControllerDispatchBinding(action=action, contract=dict(contract))
        with dispatch_contract(contract):
            yield binding


def _runner_argument_vector(command: str) -> str | None:
    """Contract argv excludes the runner executable, matching production receipts."""

    tokens = shlex.split(command)
    return shlex.join(tokens[1:]) if len(tokens) > 1 else None


def _controller_lineage(
    *,
    actions: Sequence[ControllerActionBinding],
    contract_ids: Sequence[Any],
    receipt_contract_ids: Sequence[Any],
) -> dict[str, list[str]]:
    return {
        "intent_ids": [action.intent_id for action in actions],
        "envelope_ids": [action.envelope_id for action in actions],
        "contract_ids": [str(value) for value in contract_ids if str(value)],
        "receipt_contract_ids": [str(value) for value in receipt_contract_ids if str(value)],
    }


def _wait_for_detached_terminal(
    audit: CommandAudit,
    handle: Mapping[str, Any],
    *,
    timeout_seconds: float = 240.0,
) -> int:
    """Wait on the production terminal authority: Docker's exec record.

    The detached supervisor deliberately writes no container exit marker — a
    same-uid runner could rewrite one — so ``observe_exit_marker`` is now a
    forensic parser for a legacy file that never appears.  D0 therefore takes
    its physical exit code from the audited production poll, which reads
    ``inspect_detached_terminal`` and nothing in the container.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        poll = audit.poll_detached_command(dict(handle))
        if poll.get("probe_success") is not True or poll.get("terminal_probe_success") is not True:
            raise D0Error(
                "detached terminal probe failed: " f"{poll.get('probe_error') or 'unknown'}"
            )
        state = str(poll.get("state") or "")
        if state == "finished":
            exit_code = poll.get("exit_code")
            if type(exit_code) is not int or isinstance(exit_code, bool):
                raise D0Error("detached terminal observation carried no integer exit code")
            return int(exit_code)
        if state not in {"running"}:
            raise D0Error(f"detached job became {state or 'unknown'} before it was terminal")
        time.sleep(0.5)
    raise D0Error(f"detached job did not become terminal within {timeout_seconds:.0f}s")


def _detached_identity_errors(handle: Mapping[str, Any]) -> list[str]:
    """Reject an incomplete docker-exec identity before an obligation states it.

    This is the same acceptance test ``record_dispatch_obligation_result``
    applies to a production handle.  A D0 obligation must never assert a
    started, identity-verified job on anything weaker.
    """

    from sag.agent.job_obligations import DETACHED_TERMINAL_AUTHORITY

    errors: list[str] = []
    if handle.get("started") is not True or handle.get("start_accepted") is not True:
        errors.append("detached identity dispatch was not accepted by Docker")
    if handle.get("startup_identity_verified") is not True:
        errors.append("detached identity dispatch did not verify its startup identity")
    if handle.get("runner_dispatch_state") != "accepted":
        errors.append("detached identity dispatch state is not accepted")
    if handle.get("terminal_authority") != DETACHED_TERMINAL_AUTHORITY:
        errors.append("detached identity dispatch has no docker-exec terminal authority")
    for field_name in ("docker_exec_id", "container_id", "process_identity_token"):
        if re.fullmatch(r"[0-9a-f]{64}", str(handle.get(field_name) or "")) is None:
            errors.append(f"detached identity {field_name} is not a docker-reported sha256")
    pid = handle.get("pid")
    pgid = handle.get("pgid")
    if type(pid) is not int or type(pgid) is not int or pid <= 0 or pid != pgid:
        errors.append("detached identity process is not its own session leader")
    for field_name in ("job_id", "log_path", "exit_code_path", "pid_path", "pgid_path"):
        if not str(handle.get(field_name) or ""):
            errors.append(f"detached identity {field_name} is absent")
    if not str(handle.get("identity_path") or ""):
        errors.append("detached identity identity_path is absent")
    return errors


def _probe_sync_large_evidence(runtime: DockerProbeRuntime) -> ProbeObservation:
    from sag.agent.invocation_receipts import build_receipt, write_receipt_result
    from sag.agent.job_obligations import build_obligation, write_obligation

    audit = runtime.new_container()
    epoch = runtime.epoch_for(audit)
    reports = _large_report_map()
    receipt = build_receipt(
        receipt_id="inv-d0-sync-large-0001",
        tool="maven",
        requested_action="verify",
        effective_action="verify",
        argv="mvn verify",
        working_directory="/workspace/d0",
        exit_code=0,
        before={},
        after=reports,
    )
    receipt_body = json.dumps(receipt, sort_keys=True)
    receipt_result = write_receipt_result(audit.execute_command, receipt)

    # A schema-v3 obligation carries a docker-exec identity envelope, so it can
    # only describe a job that Docker really accepted.  This fixture owns no
    # controller contract and must stay contractless/non-claimable, so the
    # identity comes from one real *control-plane* detached dispatch instead of
    # a project-lane runner dispatch: the envelope is genuine, and the probe
    # still performs zero physical project dispatches.  Nothing here is
    # hand-written 64-hex.
    identity_command = "sh -c 'exit 0'"
    handle = audit.execute_control_command_detached(identity_command, workdir="/workspace")
    identity_failures = _detached_identity_errors(handle)
    if identity_failures:
        return runtime.aggregate_observation(
            passed=False,
            facts={
                "dispatch": {
                    key: handle.get(key)
                    for key in (
                        "started",
                        "job_id",
                        "dispatch_status",
                        "runner_dispatch_state",
                        "start_accepted",
                        "startup_identity_verified",
                    )
                },
                "evidence_disposition": "synthetic_non_claimable",
                "physical_dispatches": 0,
                "expected": {},
                "observed": {},
                "production_entrypoints": [
                    "invocation_receipts.write_receipt_result",
                    "job_obligations.write_obligation",
                ],
            },
            failures=tuple(identity_failures),
        )
    job_id = str(handle.get("job_id") or "")
    obligation = build_obligation(
        job_id=job_id,
        run_id=epoch.run_id,
        tool="bash",
        attempt=1,
        requested_action="run",
        effective_action="run",
        argv=identity_command,
        working_directory="/workspace",
        before=reports,
        log_path=str(handle.get("log_path") or ""),
        exit_code_path=str(handle.get("exit_code_path") or ""),
        terminal_authority=str(handle.get("terminal_authority") or ""),
        docker_exec_id=str(handle.get("docker_exec_id") or ""),
        container_id=str(handle.get("container_id") or ""),
        start_accepted=True,
        startup_identity_verified=True,
        runner_dispatch_state=str(handle.get("runner_dispatch_state") or ""),
        pid=handle.get("pid"),
        pgid=handle.get("pgid"),
        pid_path=str(handle.get("pid_path") or ""),
        pgid_path=str(handle.get("pgid_path") or ""),
        identity_path=str(handle.get("identity_path") or ""),
        process_identity_token=str(handle.get("process_identity_token") or ""),
    )
    obligation_body = json.dumps(obligation, sort_keys=True)
    obligation_ok = write_obligation(audit.execute_command, obligation)

    receipt_path = "/workspace/.setup_agent/invocation_receipts/inv-d0-sync-large-0001.json"
    obligation_path = f"/workspace/.setup_agent/job_obligations/{job_id}.json"
    persisted_receipt = _container_file_facts(audit, receipt_path)
    persisted_obligation = _container_file_facts(audit, obligation_path)
    expected: dict[str, dict[str, Any]] = {
        "receipt": {
            "bytes": len(receipt_body.encode("utf-8")),
            "sha256": hashlib.sha256(receipt_body.encode("utf-8")).hexdigest(),
        },
        "obligation": {
            "bytes": len(obligation_body.encode("utf-8")),
            "sha256": hashlib.sha256(obligation_body.encode("utf-8")).hexdigest(),
        },
    }
    failures: list[str] = []
    if not receipt_result.persisted:
        failures.append(f"receipt persistence returned {receipt_result.code}")
    if not obligation_ok:
        failures.append("large obligation did not persist")
    for name, observed in (
        ("receipt", persisted_receipt),
        ("obligation", persisted_obligation),
    ):
        if expected[name]["bytes"] < MIN_LARGE_EVIDENCE_BYTES:
            failures.append(f"{name} was smaller than 3 MiB")
        if observed.get("bytes") != expected[name]["bytes"]:
            failures.append(f"{name} byte count changed")
        if observed.get("sha256") != expected[name]["sha256"]:
            failures.append(f"{name} hash changed")
        if observed.get("json_valid") is not True:
            failures.append(f"{name} JSON did not validate")
    return runtime.aggregate_observation(
        passed=not failures,
        facts={
            "expected": expected,
            "observed": {
                "receipt": persisted_receipt,
                "obligation": persisted_obligation,
            },
            # This probe exercises the bounded persistence transport only.  It
            # synthesizes no runner claim and therefore owns no action/contract.
            "evidence_disposition": "synthetic_non_claimable",
            "physical_dispatches": 0,
            # Stated explicitly so the sealed archive says where the v3
            # identity envelope came from rather than leaving it implied.
            "obligation_identity": {
                "job_id": job_id,
                "channel": "DockerOrchestrator.execute_control_command_detached",
                "terminal_authority": str(handle.get("terminal_authority") or ""),
                "docker_exec_id": str(handle.get("docker_exec_id") or ""),
                "container_id": str(handle.get("container_id") or ""),
                "runner_dispatch_state": str(handle.get("runner_dispatch_state") or ""),
            },
            "receipt_persistence_code": receipt_result.code,
            "production_entrypoints": [
                "invocation_receipts.write_receipt_result",
                "job_obligations.write_obligation",
            ],
        },
        failures=failures,
        byte_hash_json_mismatch=any("changed" in item or "validate" in item for item in failures),
    )


def _probe_detached_large_settlement(runtime: DockerProbeRuntime) -> ProbeObservation:
    from sag.agent.control_events import RunPin
    from sag.agent.control_events import canonical_json as control_canonical_json
    from sag.agent.evidence_publications import (
        RUN_PIN_LOGICAL_ARTIFACT_ID,
        latest_publication_raw_sha256,
        publish_evidence_revision,
    )
    from sag.agent.job_obligations import (
        read_obligations,
        reconcile_job_obligations,
        record_dispatch_obligation_result,
    )
    from sag.tools.report_tool import ReportTool
    from sag.utils.container_io import write_container_text_atomic
    from scripts.evaluate_golden_battery import evaluate_v2_campaign

    audit = runtime.new_container()
    project_root = "/workspace/d0"
    report_root = f"{project_root}/target/surefire-reports"
    report_path = f"{report_root}/TEST-d0-rows.xml"
    run_id = runtime.epoch_for(audit).run_id
    pom = write_container_text_atomic(
        audit,
        f"{project_root}/pom.xml",
        D0_ROW_POM,
    )
    # Fixture bootstrap for the target checkout: control-plane.  The audited
    # project lane begins at the real detached dispatch below.
    initialized = audit.execute_control_command(
        f"mkdir -p {shlex.quote(report_root)} && "
        f"git -C {shlex.quote(project_root)} init -q && "
        f"git -C {shlex.quote(project_root)} config user.email d0@example.invalid && "
        f"git -C {shlex.quote(project_root)} config user.name D0 && "
        f"git -C {shlex.quote(project_root)} add pom.xml && "
        "GIT_AUTHOR_DATE=2026-08-08T00:00:00Z "
        "GIT_COMMITTER_DATE=2026-08-08T00:00:00Z "
        f"git -C {shlex.quote(project_root)} commit -qm initial && "
        f"git -C {shlex.quote(project_root)} rev-parse HEAD"
    )
    if not pom.persisted or initialized.get("exit_code") != 0:
        return runtime.aggregate_observation(
            passed=False,
            facts={"fixture_output": initialized.get("output")},
            failures=("detached row target could not be pinned",),
        )
    target_sha = str(initialized.get("output") or "").strip().splitlines()[-1]
    domain_id = project_root
    requirements = {
        "build_system": "maven",
        "build_root": project_root,
        # ``nearest_domain_root`` reads ``build_domains`` (or the nested
        # recommendation), not ``domain_facts``.  Without it the dispatch has
        # no surveyed domain, the settlement receipt carries no ``domain_id``,
        # and the production row writer refuses every row as
        # ``domain_id_unavailable``.
        "build_domains": [{"root": project_root}],
        "domain_facts": [{"domain_id": domain_id, "root": project_root, "fact_epoch": 1}],
        "build_islands": [{"root": project_root, "system": "maven", "goal": "test"}],
    }
    production_pin = RunPin(
        run_id=run_id,
        target_repo_sha=target_sha,
        container_image_digest=str((runtime.run_pin.get("image") or {}).get("id") or ""),
        sag_git_sha=str((runtime.run_pin.get("source") or {}).get("git_sha") or ""),
        thinking_model="none:no-model",
        action_model="none:no-model",
        sanitized_config={"campaign_kind": "d0-deterministic-no-model"},
        prompt_bundle_sha256=NOT_APPLICABLE_SHA256,
        feature_flags={},
        run_order_index=int(runtime.run_pin.get("run_order_index") or 0),
        random_seed_or_null=None,
        dependency_cache_state="prepared-d0-image",
        host_arch=platform.machine(),
        advisor={"mode": "off", "calls": []},
    )
    # The metrics reader takes the run pin from the exact current HOST
    # publication, not from whatever bytes sit at the container path, so the
    # pin must be published exactly as the agent publishes it: canonical bytes
    # to the container mirror, then one head revision.  An unpublished mirror
    # reads back as "no pin", which empties run.run_id and also makes the file
    # an unexpected extra during archive epoch verification.
    pin_body = control_canonical_json(production_pin)
    pin_raw = pin_body.encode("utf-8")
    expected_previous_pin = latest_publication_raw_sha256(audit, RUN_PIN_LOGICAL_ARTIFACT_ID)
    pin_write = write_container_text_atomic(
        audit,
        "/workspace/.setup_agent/run-pin.json",
        pin_body,
        validate_json=True,
    )
    pin_publication = (
        publish_evidence_revision(
            audit,
            record_kind="run_pin",
            record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            raw=pin_raw,
            expected_previous_raw_sha256=expected_previous_pin,
        )
        if pin_write.persisted
        else None
    )
    if not pin_write.persisted or pin_publication is None or not pin_publication.published:
        return runtime.aggregate_observation(
            passed=False,
            facts={
                "run_pin_publication": (
                    pin_publication.status if pin_publication is not None else "not_persisted"
                )
            },
            failures=("production metrics run pin was not host published",),
        )
    # One real JUnit report carries 24k distinct runtime testcase rows.  The
    # launcher remains short; the production row parser, receipt writer, strict
    # metrics reader/projector, and evaluator must move the large identity set.
    program = (
        "from pathlib import Path;"
        f"p=Path({report_path!r});"
        f"n={EXPECTED_TESTCASE_ROWS};"
        "h=p.open('w',encoding='utf-8');"
        'h.write(f\'<testsuite name="d0" tests="{n}">\');'
        '[h.write(f\'<testcase classname="d0.Rows" name="case_{i:05d}"/>\') '
        "for i in range(n)];"
        "h.write('</testsuite>');h.close()"
    )
    detached_command = f"python3 -c {shlex.quote(program)}"
    # The executor family, not the public tool, selects the evidence
    # conventions the settlement receipt applies: production seals testcase
    # execution rows only for maven/gradle/python, and `argv_v1` forbids the
    # python executor (it demands the python_facade_v1 build binding).  The
    # report this dispatch writes IS Maven's `target/surefire-reports` JUnit
    # layout, so `maven` is the executor whose conventions actually describe
    # it.  The exact physical argv and toolchain fingerprint stay on the
    # receipt, so the archive never hides what really ran.
    with _controller_dispatch_scope(
        runtime,
        audit.execute_command,
        label="detached-large-generator",
        domain_id=domain_id,
        tool="bash",
        params={"command": detached_command, "working_directory": project_root},
        next_action_kind="bash",
        effective_action="run",
        expected_cwd=project_root,
        expected_argv=_runner_argument_vector(detached_command),
        requirements=requirements,
        target_sha_value=target_sha,
        effective_tool="maven",
    ) as dispatch_binding:
        handle = audit.execute_command_detached(detached_command, workdir=project_root)
        if not handle.get("started"):
            return runtime.aggregate_observation(
                passed=False,
                facts={"dispatch": handle},
                failures=("detached command did not start",),
            )
        recorded = record_dispatch_obligation_result(
            audit.execute_command,
            result={"dispatch": handle, "handoff_reason": "window"},
            tool="maven",
            attempt=1,
            requested_action="run",
            effective_action="run",
            argv=detached_command,
            working_directory=project_root,
            before={},
            requirements=requirements,
        )
    if not recorded.persisted:
        return runtime.aggregate_observation(
            passed=False,
            facts={"obligation": recorded.metadata()},
            failures=(f"dispatch obligation did not persist: {recorded.code}",),
        )
    exit_code = _wait_for_detached_terminal(audit, handle)
    first = reconcile_job_obligations(audit)
    # A first sweep can leave persistence pending only when its first bounded
    # attempt failed.  D0 permits the production retry but not an open barrier.
    second = reconcile_job_obligations(audit)
    records = read_obligations(audit) or []
    matching = [item for item in records if item.get("job_id") == recorded.job_id]
    failures: list[str] = []
    if exit_code != 0:
        failures.append(f"detached generator exited {exit_code}")
    if len(matching) != 1:
        failures.append("detached obligation was not uniquely readable")
        ledger: dict[str, Any] = {}
    else:
        ledger = matching[0]
    receipt_id = str(ledger.get("settled_receipt_id") or "")
    if ledger.get("process_state") != "terminal":
        failures.append("detached process did not become terminal")
    if ledger.get("settlement_state") != "settled" or not receipt_id:
        failures.append("detached obligation did not settle")
    if ledger.get("contract_id") != dispatch_binding.contract.get("contract_id"):
        failures.append("detached obligation did not inherit its controller contract")
    receipt_facts: dict[str, Any] = {}
    receipt: dict[str, Any] = {}
    receipt_testcase_rows = 0
    if receipt_id:
        receipt = _read_container_json(
            audit,
            f"/workspace/.setup_agent/invocation_receipts/{receipt_id}.json",
        )
        receipt_facts = _container_file_facts(
            audit,
            f"/workspace/.setup_agent/invocation_receipts/{receipt_id}.json",
        )
        if int(receipt_facts.get("bytes") or 0) < MIN_LARGE_EVIDENCE_BYTES:
            failures.append("detached settlement receipt was smaller than 3 MiB")
        row_envelope = receipt.get("testcase_execution_rows") or {}
        receipt_testcase_rows = len(row_envelope.get("rows") or [])
        if (
            row_envelope.get("status") != "complete"
            or receipt_testcase_rows != EXPECTED_TESTCASE_ROWS
        ):
            failures.append("production receipt did not seal exactly 24k testcase rows")
        if receipt.get("contract_id") != dispatch_binding.contract.get("contract_id"):
            failures.append("detached settlement receipt lost its controller contract")
    third = reconcile_job_obligations(audit)
    settlement_ids = [item.job_id for item in (*first.settlements, *second.settlements)]
    if settlement_ids.count(recorded.job_id) != 1:
        failures.append("detached job did not release through exactly one settlement")
    if third.settlements:
        failures.append("settled detached job emitted a duplicate settlement")
    snapshot = {
        "run_id": run_id,
        "finalized_at": "2026-08-08T00:00:00Z",
        "verdict": "success",
        "close_reason": "test_terminated",
        "build_evidence": {
            "observed": True,
            "green": True,
            "judgment": "success",
            "outcome": "success",
            "evidence_status": "verified",
            "refs": [receipt_id] if receipt_id else [],
            "source": "observations",
            "domain_states": {domain_id: {"state": "success"}},
        },
        "test_stats": {"judgment": "success"},
        "conflicts": [],
        "input_refs": [receipt_id] if receipt_id else [],
    }
    metrics = ReportTool(audit).finalize_metrics_v2(snapshot)
    persisted_metrics = _read_container_json(
        audit,
        "/workspace/.setup_agent/report_metrics.json",
    )
    if canonical_json(metrics) != canonical_json(persisted_metrics):
        failures.append("production metrics reader did not reproduce its persisted artifact")
    claimed = (persisted_metrics.get("tests") or {}).get("claimed") or {}
    projected_rows = int((claimed.get("receipt_executions") or {}).get("executed") or 0)
    evaluator = evaluate_v2_campaign([persisted_metrics])
    evaluator_rows = int(
        (
            ((evaluator.get("diagnostic_totals") or {}).get("claimed") or {}).get(
                "receipt_executions"
            )
            or {}
        ).get("executed")
        or 0
    )
    if projected_rows != EXPECTED_TESTCASE_ROWS or evaluator_rows != EXPECTED_TESTCASE_ROWS:
        failures.append("production metrics/evaluator did not preserve all 24k receipt rows")
    return runtime.aggregate_observation(
        passed=not failures,
        facts={
            "job_id": recorded.job_id,
            "exit_code": exit_code,
            "ledger": ledger,
            "receipt": receipt_facts,
            "receipt_testcase_rows": receipt_testcase_rows,
            "row_metrics": {"receipt_executions": projected_rows},
            "evaluator_metrics": {"receipt_executions": evaluator_rows},
            "settlement_emissions": settlement_ids.count(recorded.job_id),
            # Stated so the sealed archive describes its own fixture: the
            # physical runner is the argv above, and the executor family is the
            # one whose report conventions that argv produces.
            "dispatch_fixture": {
                "public_tool": "bash",
                "effective_tool": "maven",
                "physical_argv": detached_command,
                "report_path": report_path,
            },
            "controller_lineage": _controller_lineage(
                actions=[dispatch_binding.action],
                contract_ids=[dispatch_binding.contract.get("contract_id")],
                receipt_contract_ids=[receipt.get("contract_id")],
            ),
            "production_entrypoints": [
                "DockerOrchestrator.execute_command_detached",
                "job_obligations.reconcile_job_obligations",
                "ReportTool.finalize_metrics_v2",
                "evaluate_golden_battery.evaluate_v2_campaign",
            ],
        },
        failures=failures,
        exit_marker_job_ids=(recorded.job_id,),
    )


class _ReceiptFailingProxy:
    """Inject only receipt staging failure; process/obligation I/O stays real."""

    def __init__(self, audit: CommandAudit):
        self.audit = audit
        self.failures = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.audit, name)

    def _injects(self, command: str) -> bool:
        return (
            command.startswith(": > ")
            and "/workspace/.setup_agent/invocation_receipts/" in command
            and ".b64." in command
        )

    def _inject(self, command: str) -> Mapping[str, Any]:
        self.failures += 1
        injected_output = "injected receipt staging failure"
        self.audit.record_injected_failure(command, injected_output, exit_code=73)
        return {
            "success": False,
            "exit_code": 73,
            "output": injected_output,
        }

    def execute_command(self, command: str, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        text = str(command)
        if self._injects(text):
            return self._inject(text)
        return self.audit.execute_command(command, *args, **kwargs)

    def execute_control_command(self, command: str, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        """The receipt transport stages through the clean channel, so inject here.

        ``container_io`` resolves ``execute_control_command`` from whatever
        object it is handed, so an injector that only overrides
        ``execute_command`` is bypassed entirely and stages the receipt for
        real.  Obligation and process I/O use different paths and pass through.
        """

        text = str(command)
        if self._injects(text):
            return self._inject(text)
        return self.audit.execute_control_command(command, *args, **kwargs)


def _configure_no_phase_engine(
    *,
    runtime: DockerProbeRuntime,
    audit: CommandAudit,
    orchestrator: Any,
    client: Any,
    loop_memory: Any = None,
    stall_seconds: int = 2,
) -> Any:
    from sag.agent.react_engine import ReActEngine

    epoch = runtime.epoch_for(audit)
    return ReActEngine.for_controller_loop(
        orchestrator=orchestrator,
        llm_client=client,
        control_event_sink=epoch.sink,
        run_id=epoch.run_id,
        loop_memory=loop_memory,
        repository_url="https://invalid.example/d0.git",
        max_wall_clock_seconds=180,
        dispatch_stall_seconds=stall_seconds,
        max_iterations=1,
        obligation_poll_seconds=0.25,
        report_reserve_seconds=0,
    )


def _control_events(path: Path) -> list[dict[str, Any]]:
    from sag.agent.control_events import ControlEvent

    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(ControlEvent.model_validate_json(line).model_dump(mode="json"))
    return events


def _probe_terminal_receipt_failure(runtime: DockerProbeRuntime) -> ProbeObservation:
    from sag.agent.job_obligations import (
        read_obligations,
        record_dispatch_obligation_result,
    )

    audit = runtime.new_container()
    terminal_command = "sh -c 'exit 7'"
    with _controller_dispatch_scope(
        runtime,
        audit.execute_command,
        label="terminal-exit-seven",
        domain_id="d0-terminal",
        tool="bash",
        params={"command": terminal_command, "working_directory": "/workspace"},
        next_action_kind="bash",
        effective_action="run",
        expected_cwd="/workspace",
        expected_argv=_runner_argument_vector(terminal_command),
    ) as dispatch_binding:
        handle = audit.execute_command_detached(terminal_command, workdir="/workspace")
        if not handle.get("started"):
            return runtime.aggregate_observation(
                passed=False,
                facts={"dispatch": handle},
                failures=("terminal-failure injection job did not start",),
            )
        recorded = record_dispatch_obligation_result(
            audit.execute_command,
            result={"dispatch": handle, "handoff_reason": "window"},
            tool="bash",
            attempt=1,
            requested_action="run",
            effective_action="run",
            argv=terminal_command,
            working_directory="/workspace",
            before={},
        )
    if not recorded.persisted:
        return runtime.aggregate_observation(
            passed=False,
            facts={"obligation": recorded.metadata()},
            failures=("terminal-failure obligation did not persist",),
        )
    exit_code = _wait_for_detached_terminal(audit, handle)
    failing = _ReceiptFailingProxy(audit)
    first_client = _NoModelClient()
    engine = _configure_no_phase_engine(
        runtime=runtime,
        audit=audit,
        orchestrator=failing,
        client=first_client,
    )
    first_loop = engine.run_react_loop(
        "settle the terminal job",
        max_iterations=1,
        completion_mode="build",
    )
    records = read_obligations(audit) or []
    matching = [item for item in records if item.get("job_id") == recorded.job_id]
    ledger = matching[0] if len(matching) == 1 else {}
    if ledger.get("contract_id") != dispatch_binding.contract.get("contract_id"):
        return runtime.aggregate_observation(
            passed=False,
            facts={
                "job_id": recorded.job_id,
                "ledger": ledger,
                "controller_lineage": _controller_lineage(
                    actions=[dispatch_binding.action],
                    contract_ids=[dispatch_binding.contract.get("contract_id")],
                    receipt_contract_ids=[],
                ),
            },
            failures=("terminal obligation did not inherit its controller contract",),
            exit_marker_job_ids=(recorded.job_id,),
        )
    control_event_path = runtime.epoch_for(audit).control_event_path
    first_events = _control_events(control_event_path)
    from sag.agent.replay import ControlReplayRunner

    fixture_path = runtime.repo / "tests" / "fixtures" / "control_layer" / "paramiko.jsonl"
    fixture_rows = [
        json.loads(line)
        for line in fixture_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    replay_header = dict(fixture_rows[0])
    # The recorded fixture's own schema version stands.  Raising it applies the
    # schema-2 test-gate contract (source_attempt_id must be the machine's
    # current attempt, plus a candidate resolution) to rows recorded before
    # that contract existed, which fails the replay on the FIXTURE rather than
    # on the live terminal-unpersisted lifecycle this probe is registered to
    # verify.  The lifecycle events D0 inserts carry no version requirement:
    # only `forced_action` needs >= 2 and `repair_context_opened` needs 3.
    replay_header["run_id"] = "d0-terminal-unpersisted-replay"
    replay_header["source_manifest"] = [
        *list(replay_header.get("source_manifest") or ()),
        {
            "path": "control-events.jsonl",
            "sha256": _sha256_file(control_event_path),
        },
    ]
    replay_rows: list[dict[str, Any]] = [replay_header]
    replay_lifecycle_events = [
        event for event in first_events if event.get("kind") in TERMINAL_REPLAY_KINDS
    ]
    inserted = False
    for fixture_event in fixture_rows[1:]:
        if fixture_event.get("kind") == "evidence_close":
            inserted = True
            for live_event in replay_lifecycle_events:
                replay_rows.append(
                    {
                        "kind": live_event["kind"],
                        "payload": dict(live_event.get("payload") or {}),
                        "source": {
                            "path": "control-events.jsonl",
                            "line_ref": f"sequence:{live_event.get('sequence')}",
                            "sha256": canonical_sha256(
                                {
                                    "kind": live_event["kind"],
                                    "payload": live_event.get("payload") or {},
                                }
                            ),
                        },
                    }
                )
        replay_rows.append(dict(fixture_event))
    if not inserted:
        raise D0Stop("terminal replay fixture has no evidence-close boundary")
    for sequence, row in enumerate(replay_rows[1:], 1):
        row["sequence"] = sequence

    replay_path = runtime.scratch_dir / "replay-transcript.jsonl"
    _atomic_jsonl(replay_path, replay_rows)
    live_event_kinds = [str(event.get("kind") or "") for event in first_events]
    live_observed_ids = [
        str((event.get("payload") or {}).get("job_id") or "")
        for event in first_events
        if event.get("kind") == "job_terminal_observed"
    ]
    live_unpersisted_ids = [
        str((event.get("payload") or {}).get("job_id") or "")
        for event in first_events
        if event.get("kind") == "job_terminal_unpersisted"
    ]
    live_forbidden = sorted(TERMINAL_REPLAY_FORBIDDEN_KINDS.intersection(live_event_kinds))
    live_shape_failures: list[str] = []
    live_lifecycle_sequence = [str(event.get("kind") or "") for event in replay_lifecycle_events]
    if live_lifecycle_sequence != list(TERMINAL_EXPECTED_LIFECYCLE):
        live_shape_failures.append(
            "live lifecycle is not terminal-observed then terminal-unpersisted"
        )
    if live_observed_ids != [recorded.job_id]:
        live_shape_failures.append("live stream is not exactly one terminal observation")
    if live_unpersisted_ids != [recorded.job_id]:
        live_shape_failures.append("live stream is not exactly one terminal-unpersisted state")
    if live_forbidden:
        live_shape_failures.append(
            "live stream carried contradictory lifecycle/integrity events: "
            + ", ".join(live_forbidden)
        )
    if live_shape_failures:
        return runtime.aggregate_observation(
            passed=False,
            facts={
                "job_id": recorded.job_id,
                "ledger": ledger,
                "event_kinds": live_event_kinds,
                "replay_event_kinds": [
                    str(event.get("kind") or "") for event in replay_lifecycle_events
                ],
                "terminal_receipt_unpersisted": len(live_unpersisted_ids),
                "model_turns": first_client.requests,
                "replay_snapshot_sha256": "",
                "replay_event_digest": "",
                "replay_expected_event_digest": "",
                "replay_conflicts": [],
                "replay_unconsumed_events": [],
                "replay_external_calls": 0,
                "controller_lineage": _controller_lineage(
                    actions=[dispatch_binding.action],
                    contract_ids=[dispatch_binding.contract.get("contract_id")],
                    receipt_contract_ids=[],
                ),
                "production_entrypoints": [
                    "ReActEngine.run_react_loop",
                    "job_obligations.reconcile_job_obligations",
                ],
            },
            failures=live_shape_failures,
            exit_marker_job_ids=(recorded.job_id,),
            job_unsettled_ids=tuple(
                recorded.job_id for kind in live_event_kinds if kind == "job_unsettled"
            ),
        )
    unlocked_replay = ControlReplayRunner.offline(verify_expected=False).run(replay_path)
    replay_header["expected_snapshot"] = unlocked_replay.snapshot.model_dump(mode="json")
    replay_header["expected_event_digest"] = unlocked_replay.produced_event_digest
    _atomic_jsonl(replay_path, replay_rows)
    verified_replay = ControlReplayRunner.offline().run(replay_path)
    archived_replay_rows = [
        json.loads(line)
        for line in replay_path.read_text(encoding="utf-8").splitlines()[1:]
        if line.strip()
    ]
    replay_events = [
        event for event in archived_replay_rows if event.get("kind") in TERMINAL_REPLAY_KINDS
    ]
    failures: list[str] = []
    if exit_code != 7:
        failures.append(f"terminal marker recorded {exit_code}, expected 7")
    if ledger.get("process_state") != "terminal":
        failures.append("receipt failure rewrote terminal process state")
    if ledger.get("settlement_state") != "unpersisted":
        failures.append("receipt failure did not close as terminal-unpersisted")
    event_kinds = [str(event.get("kind") or "") for event in first_events]
    replay_kinds = [str(event.get("kind") or "") for event in replay_events]
    if first_loop is not False:
        failures.append("terminal-unpersisted live barrier did not stop the controller run")
    if first_client.requests != 0 or verified_replay.compatibility_action_model_calls != 0:
        failures.append("terminal-unpersisted barrier allowed a model turn")
    if event_kinds.count("job_terminal_unpersisted") != 1:
        failures.append("live barrier did not emit exactly one terminal-unpersisted event")
    if event_kinds.count("job_terminal_observed") != 1:
        failures.append("live barrier did not emit exactly one terminal observation")
    if replay_kinds.count("job_terminal_unpersisted") != 1:
        failures.append("replay did not reproduce exactly one terminal-unpersisted event")
    if replay_kinds.count("job_terminal_observed") != 1:
        failures.append("replay did not reproduce exactly one terminal observation")
    if [kind for kind in event_kinds if kind in TERMINAL_REPLAY_KINDS] != list(
        TERMINAL_EXPECTED_LIFECYCLE
    ):
        failures.append("live lifecycle order was not terminal-observed then terminal-unpersisted")
    if replay_kinds != list(TERMINAL_EXPECTED_LIFECYCLE):
        failures.append(
            "replay lifecycle order was not terminal-observed then terminal-unpersisted"
        )
    live_forbidden = sorted(TERMINAL_REPLAY_FORBIDDEN_KINDS.intersection(event_kinds))
    replay_forbidden = sorted(TERMINAL_REPLAY_FORBIDDEN_KINDS.intersection(replay_kinds))
    if live_forbidden:
        failures.append(
            "terminal-unpersisted live stream carried contradictory events: "
            + ", ".join(live_forbidden)
        )
    if replay_forbidden:
        failures.append(
            "terminal-unpersisted replay carried contradictory events: "
            + ", ".join(replay_forbidden)
        )
    observed_ids = [
        str((event.get("payload") or {}).get("job_id") or "")
        for event in first_events
        if event.get("kind") == "job_terminal_observed"
    ]
    first_terminal_ids = [
        str((event.get("payload") or {}).get("job_id") or "")
        for event in first_events
        if event.get("kind") == "job_terminal_unpersisted"
    ]
    replay_terminal_ids = [
        str((event.get("payload") or {}).get("job_id") or "")
        for event in replay_events
        if event.get("kind") == "job_terminal_unpersisted"
    ]
    replay_observed_ids = [
        str((event.get("payload") or {}).get("job_id") or "")
        for event in replay_events
        if event.get("kind") == "job_terminal_observed"
    ]
    if observed_ids != [recorded.job_id] or replay_observed_ids != observed_ids:
        failures.append("terminal observation replay changed the durable job identity")
    if first_terminal_ids != [recorded.job_id] or replay_terminal_ids != first_terminal_ids:
        failures.append("terminal lifecycle replay changed the durable final-state projection")
    first_terminal_payloads = [
        dict(event.get("payload") or {})
        for event in first_events
        if event.get("kind") == "job_terminal_unpersisted"
    ]
    replay_terminal_payloads = [
        dict(event.get("payload") or {})
        for event in replay_events
        if event.get("kind") == "job_terminal_unpersisted"
    ]
    if canonical_json(first_terminal_payloads) != canonical_json(replay_terminal_payloads):
        failures.append("terminal lifecycle replay changed the final event payload")
    expected_conflict = f"job_terminal_unpersisted:{recorded.job_id}"
    if expected_conflict not in verified_replay.snapshot.conflicts:
        failures.append("offline replay did not reconstruct terminal-unpersisted conflict")
    if verified_replay.unconsumed_events:
        failures.append("offline replay left unconsumed controller state")
    if verified_replay.produced_event_digest != verified_replay.expected_event_digest:
        failures.append("offline replay did not match its atomically frozen event digest")
    return runtime.aggregate_observation(
        passed=not failures,
        facts={
            "job_id": recorded.job_id,
            "exit_code": exit_code,
            "ledger": ledger,
            "receipt_failures_injected": failing.failures,
            "event_kinds": event_kinds,
            "replay_event_kinds": replay_kinds,
            "terminal_event_payload": first_terminal_payloads,
            "replay_terminal_event_payload": replay_terminal_payloads,
            "terminal_receipt_unpersisted": event_kinds.count("job_terminal_unpersisted"),
            "model_turns": (
                first_client.requests + verified_replay.compatibility_action_model_calls
            ),
            "replay_snapshot_sha256": canonical_sha256(
                verified_replay.snapshot.model_dump(mode="json")
            ),
            "replay_event_digest": verified_replay.produced_event_digest,
            "replay_expected_event_digest": verified_replay.expected_event_digest,
            "replay_conflicts": list(verified_replay.snapshot.conflicts),
            "replay_unconsumed_events": list(verified_replay.unconsumed_events),
            "replay_external_calls": verified_replay.compatibility_action_model_calls,
            "controller_lineage": _controller_lineage(
                actions=[dispatch_binding.action],
                contract_ids=[dispatch_binding.contract.get("contract_id")],
                receipt_contract_ids=[],
            ),
            "production_entrypoints": [
                "ReActEngine.run_react_loop",
                "job_obligations.reconcile_job_obligations",
                "ControlReplayRunner.offline",
            ],
        },
        failures=failures,
        exit_marker_job_ids=(recorded.job_id,),
        job_unsettled_ids=tuple(recorded.job_id for kind in event_kinds if kind == "job_unsettled"),
    )


def _prepare_http_wrapper(audit: CommandAudit, root: str) -> dict[str, str]:
    properties = f"{root}/.mvn/wrapper/maven-wrapper.properties"
    wrapper = f"{root}/mvnw"
    # Copying the pinned checkout and hashing it is fixture bootstrap plus
    # verification: control-plane on both arms so the A/B stays single-variable.
    initialized = audit.execute_control_command(
        f"test ! -e {shlex.quote(root)} && "
        f"cp -a /opt/d0/httpcomponents-client {shlex.quote(root)} && "
        f"test -x {shlex.quote(wrapper)} && "
        f"git -C {shlex.quote(root)} diff --quiet && "
        f'test -z "$(git -C {shlex.quote(root)} status --porcelain)" && '
        f"git -C {shlex.quote(root)} rev-parse HEAD && "
        f"git -C {shlex.quote(root)} archive --format=tar HEAD | sha256sum && "
        f"sha256sum {shlex.quote(properties)} {shlex.quote(wrapper)}"
    )
    if initialized.get("exit_code") != 0:
        raise D0Error("real HTTP wrapper checkout could not be copied and pinned")
    lines = [line.strip() for line in str(initialized.get("output") or "").splitlines()]
    if len(lines) < 4:
        raise D0Error("HTTP wrapper fixture pin probe returned incomplete output")
    target_sha = lines[-4]
    # MavenTool refuses to dispatch anything — including its wrapper probe —
    # without a host-published manifest, so both arms get the identical one.
    _publish_d0_build_requirements(audit, root, system="maven", target_sha=target_sha)
    return {
        "properties": properties,
        "wrapper": wrapper,
        "target_sha": target_sha,
        "checkout_sha256": lines[-3].split()[0],
        "properties_sha256": lines[-2].split()[0],
        "wrapper_sha256": lines[-1].split()[0],
    }


def _tool_result_summary(result: Any) -> dict[str, Any]:
    return {
        "succeeded": bool(result.succeeded),
        "error_code": result.error_code,
        "operation_outcome": result.operation_outcome.value,
        "runner_dispatched": bool((result.metadata or {}).get("runner_dispatched", True)),
        "runner_choice": dict((result.metadata or {}).get("maven_runner_choice") or {}),
        "output_sha256": hashlib.sha256((result.output or "").encode()).hexdigest(),
    }


def _probe_http_wrapper_unzip_ablation(runtime: DockerProbeRuntime) -> ProbeObservation:
    from sag.tools.internal.maven_tool import MavenTool

    control = runtime.new_container("control")
    treatment = runtime.new_container("treatment")
    root = "/workspace/httpcomponents-client"
    control_fixture = _prepare_http_wrapper(control, root)
    treatment_fixture = _prepare_http_wrapper(treatment, root)
    runtime.register_container_evidence(control, root, archive_name="http-control-checkout")
    runtime.register_container_evidence(treatment, root, archive_name="http-treatment-checkout")
    original_sha = control_fixture["properties_sha256"]
    # Baseline package-manifest verification and the one registered environment
    # mutation are control-plane: neither is a project build command, and both
    # must run identically on the two arms.
    baseline_control = control.execute_control_command(
        "test ! -e /usr/bin/unzip && test -x /opt/d0/unzip && "
        "dpkg-query -W -f='${Package}=${Version}\\n' | sha256sum"
    )
    baseline_treatment = treatment.execute_control_command(
        "test ! -e /usr/bin/unzip && test -x /opt/d0/unzip && "
        "dpkg-query -W -f='${Package}=${Version}\\n' | sha256sum"
    )
    treatment_mutation = treatment.execute_control_command("ln -s /opt/d0/unzip /usr/bin/unzip")
    validate_params = {
        "command": "validate",
        "raw_output": True,
        "working_directory": root,
        "timeout": 180,
        "use_wrapper": True,
    }
    with _controller_dispatch_scope(
        runtime,
        control.execute_command,
        label="http-control-validate",
        domain_id="d0-http-control",
        tool="maven",
        params=validate_params,
        next_action_kind="maven",
        effective_action="validate",
        expected_cwd=root,
        expected_argv="validate",
        target_sha_value=control_fixture["target_sha"],
    ) as control_binding:
        control_result = MavenTool(control).execute(
            command="validate",
            raw_output=True,
            working_directory=root,
            timeout=180,
            use_wrapper=True,
        )
    treatment_results = []
    treatment_bindings: list[ControllerDispatchBinding] = []
    treatment_tool = MavenTool(treatment)
    for repetition in range(1, 4):
        predecessor = (
            str(treatment_bindings[-1].contract.get("contract_id") or "")
            if treatment_bindings
            else None
        )
        with _controller_dispatch_scope(
            runtime,
            treatment.execute_command,
            label=f"http-treatment-validate-{repetition}",
            domain_id="d0-http-treatment",
            tool="maven",
            params=validate_params,
            next_action_kind="maven",
            effective_action="validate",
            expected_cwd=root,
            expected_argv="validate",
            predecessor_contract_id=predecessor,
            target_sha_value=treatment_fixture["target_sha"],
        ) as treatment_binding:
            result = treatment_tool.execute(
                command="validate",
                raw_output=True,
                working_directory=root,
                timeout=180,
                use_wrapper=True,
            )
        treatment_bindings.append(treatment_binding)
        treatment_results.append(
            {
                "repetition": repetition,
                "contract_id": str(treatment_binding.contract.get("contract_id") or ""),
                **_tool_result_summary(result),
                "maven_proper": bool(
                    result.succeeded
                    and "BUILD SUCCESS" in (result.raw_output or result.output or "")
                    and ((result.metadata or {}).get("maven_runner_choice") or {}).get("runner")
                    == "wrapper"
                ),
                "checksum_changed": False,
            }
        )

    # maven-wrapper.properties is a Java properties file: this probe measures
    # whether its bytes changed, not whether it is JSON.
    control_after = _container_file_facts(
        control, control_fixture["properties"], expect_json=False
    )
    treatment_after = _container_file_facts(
        treatment, treatment_fixture["properties"], expect_json=False
    )
    control_dispatches = dispatch_receipts(
        _strict_container_receipts(control),
        tool="maven",
        working_directory=root,
        effective_action="validate",
    )
    treatment_dispatches = dispatch_receipts(
        _strict_container_receipts(treatment),
        tool="maven",
        working_directory=root,
        effective_action="validate",
    )
    # Post-run tracked-byte verification: control-plane.
    post_control = control.execute_control_command(
        f"git -C {shlex.quote(root)} diff --quiet && "
        f'test -z "$(git -C {shlex.quote(root)} status --porcelain)" && '
        f"git -C {shlex.quote(root)} rev-parse HEAD"
    )
    post_treatment = treatment.execute_control_command(
        f"git -C {shlex.quote(root)} diff --quiet && "
        f'test -z "$(git -C {shlex.quote(root)} status --porcelain)" && '
        f"git -C {shlex.quote(root)} rev-parse HEAD"
    )
    failures: list[str] = []
    if baseline_control.get("exit_code") != 0 or baseline_treatment.get("exit_code") != 0:
        failures.append("prepared HTTP arms did not start from missing-unzip state")
    if baseline_control.get("output") != baseline_treatment.get("output"):
        failures.append("prepared HTTP arms had different package manifests")
    if control_fixture != treatment_fixture:
        failures.append("HTTP arms did not have the same target SHA and checkout bytes")
    if treatment_mutation.get("exit_code") != 0:
        failures.append("registered treatment could not add only the unzip executable")
    if control_result.error_code != "prerequisite_executable_missing:unzip":
        failures.append("control did not reproduce typed missing-unzip failure")
    if (control_result.metadata or {}).get("runner_dispatched") is not False:
        failures.append("control dispatched a wrapper despite the missing prerequisite")
    if len(treatment_results) != 3 or any(not item["maven_proper"] for item in treatment_results):
        failures.append("all three treatment repetitions did not reach Maven proper")
    if control_dispatches:
        failures.append("HTTP control persisted a build dispatch receipt")
    if len(treatment_dispatches) != 3:
        failures.append(
            f"HTTP treatment persisted {len(treatment_dispatches)} build dispatch receipts"
        )
    control_contract_id = str(control_binding.contract.get("contract_id") or "")
    treatment_contract_ids = [
        str(binding.contract.get("contract_id") or "") for binding in treatment_bindings
    ]
    treatment_receipt_contract_ids = [
        str(receipt.get("contract_id") or "") for receipt in treatment_dispatches
    ]
    if len(set(treatment_contract_ids)) != 3:
        failures.append("HTTP treatment did not freeze three unique invocation contracts")
    if set(treatment_receipt_contract_ids) != set(treatment_contract_ids):
        failures.append("HTTP treatment receipts did not bind one-to-one to frozen contracts")
    if control_contract_id in treatment_receipt_contract_ids:
        failures.append("HTTP missing-unzip control contract acquired a dispatch receipt")
    if control_fixture["target_sha"] != HTTP_TARGET_SHA:
        failures.append("prepared HTTP checkout does not match the preregistered target SHA")
    if any(
        result.get("exit_code") != 0 or str(result.get("output") or "").strip() != HTTP_TARGET_SHA
        for result in (post_control, post_treatment)
    ):
        failures.append("HTTP arm changed tracked bytes or the original target SHA")
    mutated = any(item.get("sha256") != original_sha for item in (control_after, treatment_after))
    if mutated:
        failures.append("wrapper properties/checksum changed")
    for item in treatment_results:
        item["checksum_changed"] = mutated
    return runtime.aggregate_observation(
        passed=not failures,
        facts={
            "image_id": str((runtime.run_pin.get("image") or {}).get("id") or ""),
            "registered_treatment_mutation": "ln -s /opt/d0/unzip /usr/bin/unzip",
            "baseline_package_manifest_sha256": str(baseline_control.get("output") or "").strip(),
            "target_sha": control_fixture["target_sha"],
            "post_target_sha": {
                "control": str(post_control.get("output") or "").strip(),
                "treatment": str(post_treatment.get("output") or "").strip(),
            },
            "checkout_sha256": control_fixture["checkout_sha256"],
            "control": {
                **_tool_result_summary(control_result),
                "contract_id": control_contract_id,
            },
            "treatment_repetitions": treatment_results,
            "dispatch_receipt_ids": [
                str(receipt.get("receipt_id")) for receipt in treatment_dispatches
            ],
            "controller_lineage": _controller_lineage(
                actions=[control_binding.action]
                + [binding.action for binding in treatment_bindings],
                contract_ids=[control_contract_id, *treatment_contract_ids],
                receipt_contract_ids=treatment_receipt_contract_ids,
            ),
            "wrapper_sha256": control_fixture["wrapper_sha256"],
            "properties_sha256": {
                "expected": original_sha,
                "control": control_after.get("sha256"),
                "treatment": treatment_after.get("sha256"),
            },
            "evidence_run_ids": {
                "control": runtime.epoch_for(control).run_id,
                "treatment": runtime.epoch_for(treatment).run_id,
            },
            "production_entrypoints": ["MavenTool.execute"],
        },
        failures=failures,
        wrapper_checksum_mutated=mutated,
    )


def _probe_dynamic_jdk_authority(runtime: DockerProbeRuntime) -> ProbeObservation:
    from sag.runtime.env_overlay import EnvOverlayStore
    from sag.tools.build.build_tool import BuildTool
    from sag.tools.internal.build_preflight import active_java_runtime
    from sag.tools.internal.maven_tool import MavenTool
    from sag.utils.container_io import write_container_text_atomic

    audit = runtime.new_container()
    root = "/workspace/d0-jdk"
    wrapper = write_container_text_atomic(
        audit,
        f"{root}/mvnw",
        '#!/bin/sh\nexec /opt/d0/jdk-maven/bin/mvn "$@"\n',
    )
    # A compile that leaves no artifact is not a success: production validates
    # `target/classes/**.class` after a BUILD SUCCESS marker and fails the
    # result when the tree is empty.  The pinned fixture pom carries no
    # sources, so this probe supplies exactly one compilable class and the
    # Java-17 dispatch has something real to produce.
    source = write_container_text_atomic(
        audit,
        f"{root}/src/main/java/d0/Probe.java",
        "package d0;\n\npublic final class Probe {\n"
        "    private Probe() {}\n\n"
        "    public static String marker() {\n"
        '        return "d0-jdk-authority";\n'
        "    }\n}\n",
    )
    # A wrapper without readable properties has no stated distribution, so the
    # runner refuses it as `wrapper_properties_unreadable` before any dispatch.
    # These are the exact distribution facts of the Maven this wrapper execs
    # (the image downloads that same zip and unpacks it to /opt/d0/jdk-maven),
    # so the declared archive type is the real one and its extractor — the
    # unzip linked below — is genuinely present.
    wrapper_properties = write_container_text_atomic(
        audit,
        f"{root}/.mvn/wrapper/maven-wrapper.properties",
        "distributionType=only-script\n"
        "distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/"
        f"apache-maven/{MAVEN_VERSION}/{MAVEN_ZIP_NAME}\n"
        f"distributionSha256Sum={MAVEN_ZIP_SHA256}\n",
    )
    # Deterministic checkout bootstrap: control-plane.  The audited project lane
    # starts at the public BuildTool calls below.
    fixture = audit.execute_control_command(
        f"cp /opt/d0/jdk-project/pom.xml {root}/pom.xml && "
        "ln -s /opt/d0/unzip /usr/bin/unzip && "
        f"chmod 755 {root}/mvnw && "
        f"git -C {root} init -q && "
        "git -C /workspace/d0-jdk config user.email d0@example.invalid && "
        "git -C /workspace/d0-jdk config user.name D0 && "
        "git -C /workspace/d0-jdk add . && "
        "GIT_AUTHOR_DATE=2026-08-08T00:00:00Z "
        "GIT_COMMITTER_DATE=2026-08-08T00:00:00Z "
        "git -C /workspace/d0-jdk commit -qm initial"
    )
    if (
        not wrapper.persisted
        or not wrapper_properties.persisted
        or not source.persisted
        or fixture.get("exit_code") != 0
    ):
        return runtime.aggregate_observation(
            passed=False,
            facts={"fixture_output": fixture.get("output")},
            failures=("could not create deterministic JDK target checkout",),
        )
    runtime.register_container_evidence(audit, root, archive_name="jdk-checkout")
    sha_result = audit.execute_control_command(f"git -C {root} rev-parse HEAD")
    target_sha = str(sha_result.get("output") or "").strip()
    domain_id = "d0-jdk-domain"
    # The static survey states Java 11 from the POM's compiler configuration —
    # `java_version_source` is a closed enum in build-requirements v1, so the
    # manifest names the real producer of that fact rather than a D0 label.
    manifest = _publish_d0_build_requirements(
        audit,
        root,
        system="maven",
        java_version="11",
        java_version_source="maven-compiler",
        target_sha=target_sha,
    )
    manifest_ok = manifest.get("java_version") == "11"
    before_runtime = active_java_runtime(audit)
    build = BuildTool(audit, maven_tool=MavenTool(audit))
    build_params = {
        "action": "compile",
        "args": "-o",
        "working_directory": root,
        "timeout": 240,
    }
    with _controller_action_scope(
        runtime,
        audit=audit,
        label="jdk-compile-repair",
        domain_id=domain_id,
        tool="build",
        params=build_params,
        next_action_kind="compile",
    ) as first_action:
        first = build.execute(
            action="compile",
            args="-o",
            working_directory=root,
            timeout=240,
        )
    # The contract a dispatch was frozen against travels on the DURABLE
    # receipt; only the public envelope's own result carries it in metadata, so
    # the inner executions of a bounded retry state nothing.  Reading the
    # receipts is also what the archive lineage audit compares against.
    first_contract_ids = [
        str(record.get("contract_id") or "")
        for record in _receipts_in_dispatch_order(
            dispatch_receipts(
                _strict_container_receipts(audit),
                tool="maven",
                working_directory=root,
                effective_action="compile",
            )
        )
    ]
    first_final_contract_id = first_contract_ids[-1] if first_contract_ids else ""
    after_first_runtime = active_java_runtime(audit)
    store = EnvOverlayStore(audit)
    # A single-module manifest carries no domain projection, so BuildTool
    # scopes the runtime requirement to the invocation's own root with no
    # domain id.  Querying under a D0 label would look in an empty scope.
    runtime_records = store.scoped_runtime_requirements(
        "java",
        target_sha=target_sha,
        domain_id=None,
        domain_root=root,
    )
    dynamic: dict[str, Any] = next(
        (record for record in runtime_records if record.get("required_major") == "17"),
        {},
    )
    with _controller_action_scope(
        runtime,
        audit=audit,
        label="jdk-compile-persisted-runtime",
        domain_id=domain_id,
        tool="build",
        params=build_params,
        next_action_kind="compile",
        predecessor_contract_id=first_final_contract_id or None,
    ) as later_action:
        later = build.execute(
            action="compile",
            args="-o",
            working_directory=root,
            timeout=240,
        )
    later_contract_id = str((later.metadata or {}).get("contract_id") or "")
    later_contract = (
        _read_container_json(
            audit,
            f"/workspace/.setup_agent/invocation_contracts/{later_contract_id}.json",
        )
        if later_contract_id
        else {}
    )
    later_effective = dict(later_contract.get("effective_jdk") or {})
    receipt_records = dispatch_receipts(
        _strict_container_receipts(audit),
        tool="maven",
        working_directory=root,
        effective_action="compile",
    )
    receipt_ids = [str(record.get("receipt_id") or "") for record in receipt_records]
    receipt_contract_ids = [str(record.get("contract_id") or "") for record in receipt_records]
    all_contract_ids = [*first_contract_ids, later_contract_id]
    failures: list[str] = []
    if not manifest_ok or before_runtime.get("major") != "11":
        failures.append("public build did not start from a static Java 11 survey/runtime")
    if not first.succeeded or first.metadata.get("jdk_retry") != {"from": "11", "to": "17"}:
        failures.append("first public BuildTool call did not perform the bounded Java-17 repair")
    if len(first.execution_trace) != 2:
        failures.append("first public BuildTool call did not contain exactly one physical retry")
    if after_first_runtime.get("major") != "17":
        failures.append("repaired dispatch environment did not physically report Java 17")
    if dynamic.get("required_major") != "17" or not dynamic.get("source_ref"):
        failures.append("runner-observed Java 17 requirement did not persist from a receipt")
    if not later.succeeded or len(later.execution_trace) != 1:
        failures.append("later public BuildTool call did not succeed in one dispatch")
    if len(receipt_records) != 3:
        failures.append("Java repair path did not persist exactly three physical dispatch receipts")
    if len(set(all_contract_ids)) != 3 or set(receipt_contract_ids) != set(all_contract_ids):
        failures.append("Java controller actions did not bind three unique physical contracts")
    if (
        later_effective.get("major") != "17"
        or later_effective.get("requirement_authority") != "persisted_dynamic"
    ):
        failures.append("later public contract did not retain persisted-dynamic Java 17")
    authority_regressed = any("later public" in item for item in failures)
    return runtime.aggregate_observation(
        passed=not failures,
        facts={
            "target_sha": target_sha,
            "static_manifest_java": manifest["java_version"],
            "runtime_before": before_runtime,
            "runtime_after_first": after_first_runtime,
            "first_call": {
                "succeeded": first.succeeded,
                "jdk_retry": first.metadata.get("jdk_retry"),
                "physical_dispatches": len(first.execution_trace),
                "contract_ids": first_contract_ids,
            },
            "runtime_requirement": dynamic,
            "dispatch_receipt_ids": receipt_ids,
            "later_call": {
                "succeeded": later.succeeded,
                "physical_dispatches": len(later.execution_trace),
                "contract_id": later_contract_id,
                "effective_major": later_effective.get("major"),
                "requirement_authority": later_effective.get("requirement_authority"),
                "predecessor_contract_id": first_final_contract_id,
            },
            "controller_lineage": _controller_lineage(
                actions=[first_action, later_action],
                contract_ids=all_contract_ids,
                receipt_contract_ids=receipt_contract_ids,
            ),
            "production_entrypoints": ["BuildTool.execute"],
        },
        failures=failures,
        jdk_authority_regressed=authority_regressed,
    )


def _probe_gradle_runner_classification(runtime: DockerProbeRuntime) -> ProbeObservation:
    from sag.tools.build.build_tool import BuildTool
    from sag.tools.internal.gradle_tool import GradleTool
    from sag.utils.container_io import write_container_text_atomic

    audit = runtime.new_container()
    root = "/workspace/d0-gradle"
    # Fixture bootstrap: control-plane.
    audit.execute_control_command(f"mkdir -p {root}")
    build_file = write_container_text_atomic(
        audit,
        f"{root}/build.gradle",
        """plugins { id 'java' }

tasks.register('checkKotlinGradlePluginConfigurationErrors') {
    doLast { println('counterexample task completed') }
}

tasks.named('test') {
    dependsOn tasks.named('checkKotlinGradlePluginConfigurationErrors')
}
""",
    )
    settings_file = write_container_text_atomic(
        audit,
        f"{root}/settings.gradle",
        "rootProject.name = 'd0-gradle'\n",
    )
    if not all(item.persisted for item in (build_file, settings_file)):
        return runtime.aggregate_observation(
            passed=False,
            facts={
                "fixture_writes": [
                    build_file.__dict__,
                    settings_file.__dict__,
                ]
            },
            failures=("Gradle project fixture did not persist atomically",),
        )
    initialized = audit.execute_control_command(
        "git init -q && git config user.email d0@example.invalid && "
        "git config user.name D0 && git add . && "
        "GIT_AUTHOR_DATE=2026-08-08T00:00:00Z "
        "GIT_COMMITTER_DATE=2026-08-08T00:00:00Z git commit -q -m fixture",
        workdir=root,
    )
    if initialized.get("exit_code") != 0:
        return runtime.aggregate_observation(
            passed=False,
            facts={"fixture_init_exit_code": initialized.get("exit_code")},
            failures=("Gradle project fixture did not initialize",),
        )
    # BuildTool refuses every routing decision without a host-published manifest.
    target_sha_result = audit.execute_control_command(f"git -C {root} rev-parse HEAD")
    _publish_d0_build_requirements(
        audit,
        root,
        system="gradle",
        target_sha=str(target_sha_result.get("output") or "").strip() or None,
    )
    with _controller_action_scope(
        runtime,
        audit=audit,
        label="gradle-test",
        domain_id="d0-gradle",
        tool="build",
        params={"action": "test", "working_directory": root, "timeout": 180},
        next_action_kind="test",
    ) as build_action:
        result = BuildTool(
            audit,
            gradle_tool=GradleTool(audit),
        ).execute(
            action="test",
            working_directory=root,
            timeout=180,
        )
    receipt_records = dispatch_receipts(
        _strict_container_receipts(audit),
        tool="gradle",
        working_directory=root,
        effective_action="test",
    )
    output = str(result.raw_output or result.output or "")
    contract_id = str((result.metadata or {}).get("contract_id") or "")
    receipt_contract_ids = [str(receipt.get("contract_id") or "") for receipt in receipt_records]
    failures: list[str] = []
    if len(receipt_records) != 1 or receipt_records[0].get("exit_code") != 0:
        failures.append("public Gradle path did not persist one successful dispatch receipt")
    if not result.succeeded or result.operation_outcome.value != "success":
        failures.append("Gradle success was misclassified by foreign runner vocabulary")
    if receipt_contract_ids != [contract_id] or not contract_id:
        failures.append("Gradle receipt did not inherit its controller invocation contract")
    if not all(
        marker in output
        for marker in (
            "> Task :checkKotlinGradlePluginConfigurationErrors",
            "BUILD SUCCESSFUL",
        )
    ):
        failures.append("public Gradle result did not preserve the classifier counterexample")
    return runtime.aggregate_observation(
        passed=not failures,
        facts={
            "physical_exit_code": (
                receipt_records[0].get("exit_code") if len(receipt_records) == 1 else None
            ),
            "output": output,
            "operation_outcome": result.operation_outcome.value,
            "runner": (receipt_records[0].get("tool") if len(receipt_records) == 1 else None),
            "dispatch_receipt_ids": [
                str(receipt.get("receipt_id") or "") for receipt in receipt_records
            ],
            "controller_lineage": _controller_lineage(
                actions=[build_action],
                contract_ids=[contract_id],
                receipt_contract_ids=receipt_contract_ids,
            ),
            "production_entrypoints": ["BuildTool.execute", "GradleTool.execute"],
        },
        failures=failures,
        cross_runner_false_failure=bool(failures),
    )


class _NoModelClient:
    def __init__(self) -> None:
        self.requests = 0

    def get_native_turn(self, *_args: Any, **_kwargs: Any) -> Any:
        self.requests += 1
        raise AssertionError("D0 barrier attempted a model turn")


class _BarrierSentinelClient:
    def __init__(self, audit: CommandAudit, job_ids: Sequence[str]) -> None:
        self.audit = audit
        self.job_ids = tuple(job_ids)
        self.requests = 0
        self.ledger_states: list[dict[str, str]] = []

    def get_native_turn(self, _messages: Any, **_kwargs: Any) -> Any:
        from sag.agent.job_obligations import read_obligations
        from sag.agent.react_llm import NativeTurn

        self.requests += 1
        records = read_obligations(self.audit) or []
        by_id = {str(item.get("job_id") or ""): item for item in records}
        self.ledger_states.append(
            {
                job_id: (
                    f"{by_id.get(job_id, {}).get('process_state')}/"
                    f"{by_id.get(job_id, {}).get('settlement_state')}"
                )
                for job_id in self.job_ids
            }
        )
        return NativeTurn(text="all jobs settled", tool_calls=(), model_used="d0-sentinel")


class _ProgressRecordingLoopMemory:
    """Production LoopMemory with a read-only transition trace for D0 evidence."""

    def __init__(self) -> None:
        from sag.agent.loop_memory import LoopMemory

        self._delegate = LoopMemory()
        self.transitions: list[tuple[str, str]] = []

    def observe_job_transition(
        self,
        job_id: str,
        lifecycle_state: str,
        **_kwargs: Any,
    ) -> None:
        changed = self._delegate.observe_job_transition(
            job_id,
            lifecycle_state,
            **_kwargs,
        )
        if changed:
            self.transitions.append((str(job_id), str(lifecycle_state)))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _probe_multi_job_progress_barrier(runtime: DockerProbeRuntime) -> ProbeObservation:
    from sag.agent.job_obligations import (
        read_obligations,
        reconcile_job_obligations,
        record_dispatch_obligation_result,
    )

    audit = runtime.new_container()
    obligations = []
    dispatch_bindings: list[ControllerDispatchBinding] = []
    expected_files: dict[str, int] = {}
    for index, steps in ((1, 60), (2, 70)):
        root = f"/workspace/d0-barrier/job-{index}"
        # Fixture bootstrap: control-plane.
        audit.execute_control_command(f"mkdir -p {root}/target/progress")
        runtime.register_container_evidence(audit, root, archive_name=f"barrier-job-{index}")
        program = (
            "import pathlib,time;"
            f"r=pathlib.Path({root + '/target/progress'!r});"
            f"[((r/f'job-{index}-{{i}}.txt').write_text(str(i)), "
            f"print('job-{index} progress',i,flush=True), time.sleep(0.3)) "
            f"for i in range({steps})]"
        )
        detached_command = f"python3 -c {shlex.quote(program)}"
        with _controller_dispatch_scope(
            runtime,
            audit.execute_command,
            label=f"barrier-job-{index}",
            domain_id=f"d0-barrier-job-{index}",
            tool="bash",
            params={"command": detached_command, "working_directory": root},
            next_action_kind="bash",
            effective_action="run",
            expected_cwd=root,
            expected_argv=_runner_argument_vector(detached_command),
        ) as dispatch_binding:
            handle = audit.execute_command_detached(detached_command, workdir=root)
            if not handle.get("started"):
                return runtime.aggregate_observation(
                    passed=False,
                    facts={"dispatch": handle},
                    failures=(f"barrier job {index} did not start",),
                )
            obligation = record_dispatch_obligation_result(
                audit.execute_command,
                result={"dispatch": handle, "handoff_reason": "window"},
                tool="bash",
                attempt=index,
                requested_action="run",
                effective_action="run",
                argv=detached_command,
                working_directory=root,
                before={},
            )
        if not obligation.persisted:
            return runtime.aggregate_observation(
                passed=False,
                facts={"obligation": obligation.metadata()},
                failures=(f"barrier job {index} obligation did not persist",),
            )
        obligations.append(obligation)
        dispatch_bindings.append(dispatch_binding)
        expected_files[obligation.job_id] = steps

    job_ids = tuple(item.job_id for item in obligations)
    memory = _ProgressRecordingLoopMemory()
    client = _BarrierSentinelClient(audit, job_ids)
    engine = _configure_no_phase_engine(
        runtime=runtime,
        audit=audit,
        orchestrator=audit,
        client=client,
        loop_memory=memory,
        stall_seconds=2,
    )
    loop_result = engine.run_react_loop(
        "hold all jobs before the sentinel turn",
        max_iterations=1,
        completion_mode="build",
    )
    final_events = _control_events(runtime.epoch_for(audit).control_event_path)
    final_settlements = [event for event in final_events if event.get("kind") == "job_settled"]
    idempotent_reconciliation = reconcile_job_obligations(audit)
    records = read_obligations(audit) or []
    failures: list[str] = []
    if loop_result is not True:
        failures.append(f"production loop/barrier did not clear cleanly ({loop_result})")
    if client.requests != 1:
        failures.append("production loop did not make one sentinel turn after settlement")
    if client.ledger_states != [{job_id: "terminal/settled" for job_id in job_ids}]:
        failures.append("sentinel model turn occurred before every job was durably settled")
    settled_ids = [
        str((event.get("payload") or {}).get("job_id") or "") for event in final_settlements
    ]
    if sorted(settled_ids) != sorted(job_ids) or len(final_settlements) != 2:
        failures.append("multi-job barrier did not settle each job exactly once")
    if any(
        (
            idempotent_reconciliation.running_job_ids,
            idempotent_reconciliation.settlement_pending_job_ids,
            idempotent_reconciliation.terminal_observations,
            idempotent_reconciliation.settlements,
            idempotent_reconciliation.terminal_unpersisted,
            idempotent_reconciliation.integrity_failures,
        )
    ):
        failures.append("public post-settlement reconciliation was not idempotent")
    by_id = {str(item.get("job_id") or ""): item for item in records}
    if any(by_id.get(job_id, {}).get("settlement_state") != "settled" for job_id in job_ids):
        failures.append("barrier released before every obligation was durably settled")
    contract_ids = [str(binding.contract.get("contract_id") or "") for binding in dispatch_bindings]
    ledger_contract_ids = [
        str(by_id.get(job_id, {}).get("contract_id") or "") for job_id in job_ids
    ]
    receipt_records = _strict_container_receipts(audit)
    receipt_contract_ids = [str(receipt.get("contract_id") or "") for receipt in receipt_records]
    if (
        len(set(contract_ids)) != 2
        or ledger_contract_ids != contract_ids
        or set(receipt_contract_ids) != set(contract_ids)
    ):
        failures.append("multi-job obligations/receipts lost independent controller contracts")
    event_kinds = [str(event.get("kind") or "") for event in final_events]
    if any(
        kind in {"job_unsettled", "job_live_at_close", "job_stall_observed"} for kind in event_kinds
    ):
        failures.append("progressing barrier job was closed as live/unsettled")
    controller_progress_by_job = {
        job_id: sum(
            1 for seen_id, state in memory.transitions if seen_id == job_id and state == "progress"
        )
        for job_id in job_ids
    }
    progress_by_job = progress_response_counts(
        audited_command_outputs(audit),
        job_ids=job_ids,
    )
    if any(count < 2 for count in controller_progress_by_job.values()):
        failures.append("production loop did not observe repeated progress for each job")
    if any(count < 2 for count in progress_by_job.values()):
        failures.append("each independent job did not cross the stall threshold with progress")
    progress_files: dict[str, int] = {}
    for index, job_id in enumerate(job_ids, start=1):
        # Post-barrier artifact verification: control-plane.
        result = audit.execute_control_command(
            f"find /workspace/d0-barrier/job-{index}/target/progress -type f | wc -l"
        )
        progress_files[job_id] = int(str(result.get("output") or "0").strip().splitlines()[-1])
    if progress_files != expected_files:
        failures.append("barrier released before each job wrote all expected progress artifacts")
    diagnostics = audit.execute_control_command(
        "find /workspace/.setup_agent/job_diagnostics -type f -print 2>/dev/null"
    )
    signal_commands = sum(
        1
        for command in audit.command_texts
        if "kill -TERM -- -" in command or "kill -KILL -- -" in command
    )
    if str(diagnostics.get("output") or "").strip() or signal_commands:
        failures.append("progressing jobs entered diagnostic or TERM/KILL cleanup")
    return runtime.aggregate_observation(
        passed=not failures,
        facts={
            "job_ids": list(job_ids),
            "loop_result": loop_result,
            "idempotent_reconciliation": {
                "running": list(idempotent_reconciliation.running_job_ids),
                "pending": list(idempotent_reconciliation.settlement_pending_job_ids),
                "settlements": [
                    settlement.event_payload()
                    for settlement in idempotent_reconciliation.settlements
                ],
                "integrity_failures": list(idempotent_reconciliation.integrity_failures),
            },
            "sentinel_turns": client.requests,
            "intervening_model_turns": sum(
                1
                for state in client.ledger_states
                if state != {job_id: "terminal/settled" for job_id in job_ids}
            ),
            "model_ledger_states": client.ledger_states,
            "progress_by_job": progress_by_job,
            "controller_progress_by_job": controller_progress_by_job,
            "job_settled_events": len(final_settlements),
            "control_event_kinds": event_kinds,
            "progress_files": progress_files,
            "signal_commands": signal_commands,
            "controller_lineage": _controller_lineage(
                actions=[binding.action for binding in dispatch_bindings],
                contract_ids=contract_ids,
                receipt_contract_ids=receipt_contract_ids,
            ),
            "production_entrypoints": [
                "ReActEngine.run_react_loop",
                "job_obligations.reconcile_job_obligations",
            ],
        },
        failures=failures,
        exit_marker_job_ids=job_ids,
        job_unsettled_ids=tuple(
            str((event.get("payload") or {}).get("job_id") or "")
            for event in final_events
            if event.get("kind") == "job_unsettled"
        ),
        duplicate_dispatches=tuple(job_id for job_id in set(job_ids) if job_ids.count(job_id) > 1),
    )


PROBE_IMPLEMENTATIONS: dict[str, Callable[[DockerProbeRuntime], ProbeObservation]] = {
    "sync-large-evidence": _probe_sync_large_evidence,
    "detached-large-settlement": _probe_detached_large_settlement,
    "terminal-receipt-failure": _probe_terminal_receipt_failure,
    "http-wrapper-unzip-ablation": _probe_http_wrapper_unzip_ablation,
    "dynamic-jdk-authority": _probe_dynamic_jdk_authority,
    "gradle-runner-classification": _probe_gradle_runner_classification,
    "multi-job-progress-barrier": _probe_multi_job_progress_barrier,
}


def execute_registered_probe(
    runtime: DockerProbeRuntime,
    spec: ProbeSpec,
    run_pin: Mapping[str, Any],
) -> ProbeObservation:
    from sag.agent.evidence_publications import (
        current_evidence_publication_authority,
        install_evidence_publication_authority,
        unavailable_evidence_publication_authority,
    )
    from sag.agent.invocation_receipts import active_receipt_run_id, set_active_receipt_run_id

    del run_pin  # runtime already carries the exact immutable pin
    implementation = PROBE_IMPLEMENTATIONS.get(spec.name)
    if implementation is None:
        raise D0Error(f"probe implementation missing: {spec.name}")
    # ContextVar state is process-local, while the probes share one host
    # process. Start unavailable, let each fresh container install its own
    # epoch, and restore the caller exactly even on a failed probe.
    previous_authority = current_evidence_publication_authority()
    previous_run_id = active_receipt_run_id()
    install_evidence_publication_authority(
        unavailable_evidence_publication_authority(
            "D0 probe has not created its fresh container epoch"
        )
    )
    try:
        return implementation(runtime)
    finally:
        install_evidence_publication_authority(previous_authority)
        set_active_receipt_run_id(previous_run_id)


def _cause_report(
    *,
    campaign_dir: Path,
    spec: ProbeSpec,
    completed: Sequence[str],
    error: BaseException,
    runtime: DockerProbeRuntime,
) -> None:
    archive_integrity = "failed"
    if runtime.artifact_dir.is_dir():
        try:
            runtime.verify_archive_seal()
        except BaseException:
            archive_integrity = "failed"
        else:
            archive_integrity = "sealed"
    _atomic_json(
        campaign_dir / "cause-report.json",
        {
            "schema_version": 1,
            "status": "stopped",
            "failed_probe": spec.name,
            "completed_probes": list(completed),
            "error_type": type(error).__name__,
            "error": str(error),
            "retained_containers": [audit.orchestrator.container_name for audit in runtime.audits],
            "artifact_dir": str(runtime.artifact_dir),
            "archive_integrity": archive_integrity,
            "archive_failure_records": [
                path.name
                for path in sorted((campaign_dir / ".archive-failures").glob(f"{spec.name}-*.json"))
            ],
        },
    )
    _write_campaign_index(campaign_dir)


def _write_campaign_index(campaign_dir: Path) -> None:
    """Seal campaign-level pins/ledger/results without rehashing raw evidence.

    Each probe's ``checksums.json`` already covers every raw archived byte.
    This index binds those manifests to the campaign lock and terminal summary
    or cause report.
    """
    candidates = [
        campaign_dir / "campaign-lock.json",
        campaign_dir / "campaign-ledger.jsonl",
        campaign_dir / "dry-validation.json",
        campaign_dir / "summary.json",
        campaign_dir / "cause-report.json",
        *(path for path in campaign_dir.glob("*/checksums.json")),
        *(path for path in campaign_dir.glob("*/seal.json")),
        *(path for path in campaign_dir.glob(".archive-failures/*.json")),
    ]
    checksums = {
        path.relative_to(campaign_dir).as_posix(): _sha256_file(path)
        for path in sorted(set(candidates))
        if path.is_file()
    }
    _atomic_json(campaign_dir / "campaign-checksums.json", checksums)


def run_campaign(
    *,
    repo: Path,
    campaign_dir: Path,
    campaign_id: str,
    base_image: str,
    selected: Sequence[ProbeSpec],
    dry_run: bool,
    keep_containers: bool,
    fact_loader: Callable[[Path, str], HostFacts] = load_host_facts,
    probe_executor: Callable[
        [DockerProbeRuntime, ProbeSpec, Mapping[str, Any]], ProbeObservation
    ] = execute_registered_probe,
) -> dict[str, Any]:
    repo = repo.resolve()
    campaign_dir = campaign_dir.resolve()
    if not selected:
        raise D0Error("D0 selection is empty")
    facts = fact_loader(repo, base_image)
    lock = build_campaign_lock(
        campaign_id=campaign_id,
        facts=facts,
        selected=selected,
    )
    campaign_dir.mkdir(parents=True, exist_ok=True)
    ensure_campaign_lock(campaign_dir / "campaign-lock.json", lock)
    if dry_run:
        summary = {
            "schema_version": 1,
            "status": "dry-valid",
            "campaign_id": campaign_id,
            "probes": [spec.name for spec in selected],
            "containers_required": sum(spec.container_count for spec in selected),
            "model_calls": 0,
        }
        _atomic_json(campaign_dir / "dry-validation.json", summary)
        _write_campaign_index(campaign_dir)
        return summary

    ledger = campaign_dir / "campaign-ledger.jsonl"
    if ledger.exists() and ledger.read_text(encoding="utf-8").strip():
        raise D0Stop(
            "campaign ledger is non-empty; D0 does not resume or hot-patch a started campaign"
        )
    existing_probe_archives = [
        spec.name for spec in selected if (campaign_dir / spec.name).exists()
    ]
    if existing_probe_archives:
        raise D0Stop(
            "probe archive already exists; D0 does not resume: "
            + ", ".join(existing_probe_archives)
        )
    completed: list[str] = []
    for index, spec in enumerate(selected):
        run_pin = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "campaign_lock_sha256": canonical_sha256(lock),
            "probe": spec.name,
            "run_order_index": index,
            "model_pin": "none:no-model",
            "prompt_bundle_sha256": NOT_APPLICABLE_SHA256,
            "control_bundle_sha256": lock["control_bundle_sha256"],
            "source": lock["source"],
            "image": lock["image"],
        }
        runtime = DockerProbeRuntime(
            repo=repo,
            campaign_dir=campaign_dir,
            campaign_id=campaign_id,
            base_image=base_image,
            spec=spec,
            run_pin=run_pin,
            keep_containers=keep_containers,
        )
        started = time.monotonic()
        try:
            current_facts = fact_loader(repo, base_image)
            if current_facts != facts:
                raise D0Stop(
                    "campaign pin drift before probe dispatch: source, image, Docker, "
                    "host, or Python facts changed after lock"
                )
            observation = probe_executor(runtime, spec, run_pin)
            runtime.archive(observation)
            runtime.verify_archive_seal()
            stop_audit = json.loads(
                (runtime.artifact_dir / "stop-audit.json").read_text(encoding="utf-8")
            )
            enforce_stop_audit(stop_audit)
            # Retain the typed helper as a secondary guard for fields whose
            # physical re-derivation is probe-specific; it can only tighten
            # the independently reconstructed archive verdict.
            enforce_stop_conditions(observation)
            runtime.cleanup()
        except BaseException as exc:
            # If the probe raised before returning a typed observation, seal a
            # minimal failure row and whatever container diagnostics exist.
            if not (runtime.artifact_dir / "seal.json").is_file():
                try:
                    observation = runtime.aggregate_observation(
                        passed=False,
                        facts={},
                        failures=(f"{type(exc).__name__}: {exc}",),
                    )
                    runtime.archive(observation)
                except Exception as archive_exc:
                    exc = D0Stop(f"{exc}; evidence archive also failed: {archive_exc}")
            _cause_report(
                campaign_dir=campaign_dir,
                spec=spec,
                completed=completed,
                error=exc,
                runtime=runtime,
            )
            if isinstance(exc, D0Stop):
                raise exc
            raise D0Stop(f"probe {spec.name} raised {type(exc).__name__}: {exc}") from exc
        elapsed = time.monotonic() - started
        completed.append(spec.name)
        _append_jsonl(
            ledger,
            {
                "schema_version": 1,
                "probe": spec.name,
                "run_order_index": index,
                "status": "passed",
                "elapsed_seconds": round(elapsed, 3),
                "artifact_dir": spec.name,
                "result_sha256": _sha256_file(runtime.artifact_dir / "result.json"),
                "checksums_sha256": _sha256_file(runtime.artifact_dir / "checksums.json"),
            },
        )
    summary = {
        "schema_version": 1,
        "status": "passed",
        "campaign_id": campaign_id,
        "probes": completed,
        "model_calls": 0,
    }
    _atomic_json(campaign_dir / "summary.json", summary)
    _write_campaign_index(campaign_dir)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT),
        help="Setup-Agent source tree under test (default: repository root)",
    )
    parser.add_argument(
        "--campaign-dir",
        default="logs/d0-deterministic-probes",
        help="new evidence directory carrying the immutable campaign lock",
    )
    parser.add_argument(
        "--campaign-id",
        help="stable campaign id (default: campaign directory basename)",
    )
    parser.add_argument(
        "--base-image",
        default=os.getenv("SAG_D0_PREPARED_IMAGE", DEFAULT_PREPARED_IMAGE),
    )
    parser.add_argument(
        "--prepare-image",
        action="store_true",
        help="build the pinned D0 snapshot image, then exit without a campaign",
    )
    parser.add_argument(
        "--parent-image",
        default="ubuntu:24.04",
        help="parent used only by --prepare-image (pin a digest for publication)",
    )
    parser.add_argument(
        "--probe",
        action="append",
        choices=tuple(PROBE_SPECS),
        help="run only this registered probe (repeatable; registry order still wins)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate Docker/image/source pins and write the lock; create no containers",
    )
    parser.add_argument(
        "--keep-containers",
        action="store_true",
        help="retain passing containers too (failing containers are always retained)",
    )
    parser.add_argument("--list-probes", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_probes:
        for spec in PROBE_SPECS.values():
            print(
                f"{spec.order + 1}. {spec.name} "
                f"(fresh containers={spec.container_count}, no-model): {spec.description}"
            )
        return 0
    if args.prepare_image:
        try:
            summary = prepare_d0_image(
                Path(args.repo),
                parent_image=args.parent_image,
                target_image=args.base_image,
            )
        except D0Error as exc:
            print(f"D0 PREPARE STOP: {exc}", file=sys.stderr)
            return 2
        print(canonical_json(summary))
        return 0
    campaign_dir = Path(args.campaign_dir)
    campaign_id = str(args.campaign_id or campaign_dir.name).strip()
    if not campaign_id:
        parser.error("campaign id cannot be empty")
    try:
        summary = run_campaign(
            repo=Path(args.repo),
            campaign_dir=campaign_dir,
            campaign_id=campaign_id,
            base_image=args.base_image,
            selected=select_probes(args.probe),
            dry_run=args.dry_run,
            keep_containers=args.keep_containers,
        )
    except D0Error as exc:
        print(f"D0 STOP: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
