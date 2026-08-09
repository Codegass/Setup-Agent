"""Controller-owned diagnostics and cleanup for detached job process groups.

This module deliberately knows nothing about Maven, Gradle, Samza, or any
other project identity.  It receives one already-registered detached-job
handle, records bounded physical observations, and can signal only that
handle's process group after the diagnostic evidence has been sealed.

The model is not involved in any operation here.  The controller barrier is
the caller: it may invoke :func:`control_stalled_job` while model/advisor turns
remain suspended.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from sag.agent.evidence_publications import (
    evidence_publication_authority_for,
    latest_publication_raw_sha256,
    publish_evidence_bytes,
    publish_evidence_revision,
    verify_evidence_bytes,
    verify_latest_evidence_bytes,
)
from sag.runtime.container_io import read_container_text
from sag.utils.container_io import (
    WRITE_COMPARE_CONFLICT,
    compare_publish_container_text_atomic,
    write_container_text_atomic,
)

DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_ROOT = "/workspace/.setup_agent/job_diagnostics"
STALL_CONFIRMATION_GRACE_SECONDS = 30
STALL_CLEANUP_GRACE_SECONDS = 120
STALL_CONFIRMATION_TRIGGER = "stall_confirmation"
WALL_GUARD_TRIGGER = "wall_guard"
_STALL_SEAL_REASON = "repeated_no_progress"

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBSERVATIONS = frozenset(
    {
        "cpu_active",
        "io_wait",
        "thread_join_wait",
        "deadlock_signature",
        "unknown",
    }
)
_RAW_NAMES = ("process", "fd", "socket", "artifact", "report", "jvm", "log_tail")
_RAW_MAX_BYTES = {
    "process": 131072,
    "fd": 131072,
    "socket": 131072,
    "artifact": 131072,
    "report": 131072,
    "jvm": 262144,
    "log_tail": 65536,
}
_MAX_DIAGNOSTIC_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_PROGRESS_OUTPUT_BYTES = 65536


@dataclass(frozen=True)
class JobProgressSnapshot:
    """Bounded physical signals used to compare two controller observations."""

    process_state: str = "unknown"
    log_size: int = 0
    process_identity_sha256: str = ""
    artifact_sha256: str = ""
    report_sha256: str = ""
    cpu_ticks_delta: int = 0
    process_count: int = 0
    child_count: int = 0

    @property
    def cpu_active(self) -> bool:
        return self.cpu_ticks_delta > 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "process_state": self.process_state,
            "log_size": self.log_size,
            "process_identity_sha256": self.process_identity_sha256,
            "artifact_sha256": self.artifact_sha256,
            "report_sha256": self.report_sha256,
            "cpu_ticks_delta": self.cpu_ticks_delta,
            "process_count": self.process_count,
            "child_count": self.child_count,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "JobProgressSnapshot":
        return cls(
            process_state=_text(value.get("process_state")) or "unknown",
            log_size=_nonnegative_int(value.get("log_size")),
            process_identity_sha256=_digest(value.get("process_identity_sha256")),
            artifact_sha256=_digest(value.get("artifact_sha256")),
            report_sha256=_digest(value.get("report_sha256")),
            cpu_ticks_delta=_nonnegative_int(value.get("cpu_ticks_delta")),
            process_count=_nonnegative_int(value.get("process_count")),
            child_count=_nonnegative_int(value.get("child_count")),
        )


@dataclass(frozen=True)
class DiagnosticBundle:
    job_id: str
    pid: int
    pgid: int
    process_identity_token: str
    evidence_ref: str
    diagnostic_fingerprint: str
    observation: str
    snapshot: JobProgressSnapshot
    report_delta_count: int = 0
    content_hashes: Mapping[str, str] = field(default_factory=dict)
    persisted: bool = False
    reused: bool = False
    code: str = "capture_failed"


@dataclass(frozen=True)
class ProgressObservation:
    snapshot: JobProgressSnapshot
    signals: Tuple[str, ...] = ()
    code: str = "observed"

    @property
    def progressing(self) -> bool:
        return bool(self.signals)


@dataclass(frozen=True)
class EvidenceSeal:
    job_id: str
    pgid: int
    process_identity_token: str
    evidence_ref: str
    diagnostic_ref: str
    diagnostic_fingerprint: str
    persisted: bool
    code: str


@dataclass(frozen=True)
class CleanupResult:
    job_id: str
    pgid: int
    code: str
    term_sent: bool = False
    kill_sent: bool = False
    group_live: bool = False
    evidence_ref: str = ""

    @property
    def process_group_terminal(self) -> bool:
        """Whether the registered group is physically gone, not ledger-settled."""

        return not self.group_live and (
            self.term_sent
            or self.kill_sent
            or self.code == "already_terminal"
            or self.code.startswith("cleanup_record_")
        )

    @property
    def marker_reconciliation_required(self) -> bool:
        """A dead group still needs the supervisor marker and WS2 settlement."""

        return self.process_group_terminal


@dataclass(frozen=True)
class StallControlResult:
    job_id: str
    code: str
    diagnostic: Optional[DiagnosticBundle] = None
    progress: Optional[ProgressObservation] = None
    seal: Optional[EvidenceSeal] = None
    cleanup: Optional[CleanupResult] = None
    trigger: str = STALL_CONFIRMATION_TRIGGER

    @property
    def confirmed_stall(self) -> bool:
        """Repeated complete no-progress samples reached a durable seal."""

        return bool(
            self.trigger == STALL_CONFIRMATION_TRIGGER
            and self.seal
            and self.seal.persisted
            and self.cleanup is not None
        )

    @property
    def marker_reconciliation_required(self) -> bool:
        return bool(self.cleanup and self.cleanup.marker_reconciliation_required)


def diagnostic_bundle_ref(job_id: str) -> str:
    return f"{DIAGNOSTIC_ROOT}/{job_id}/bundle.json"


def diagnostic_seal_ref(job_id: str) -> str:
    return f"{DIAGNOSTIC_ROOT}/{job_id}/seal.json"


def diagnostic_cleanup_ref(job_id: str) -> str:
    return f"{DIAGNOSTIC_ROOT}/{job_id}/cleanup.json"


def collect_stall_diagnostic(
    execute: Callable[..., Any],
    job: Mapping[str, Any],
    *,
    captured_at: Optional[str] = None,
) -> DiagnosticBundle:
    """Read or create the one bounded diagnostic bundle for ``job``.

    A pre-existing valid bundle is returned byte-semantically rather than
    sampled again.  This is what makes "one first-stall bundle" durable across
    controller retries and restarts.
    """

    identity = _job_identity(job)
    if identity is None:
        return _invalid_bundle(job, "invalid_registered_job_identity")
    job_id, pid, pgid = identity
    process_identity_token = _digest(job.get("process_identity_token"))
    ref = diagnostic_bundle_ref(job_id)
    existing_state, existing, existing_raw = _read_json_state(execute, ref)
    if existing_state == "present" and existing is not None:
        reused = _bundle_from_payload(existing, ref=ref, reused=True)
        if (
            reused is not None
            and reused.pid == pid
            and reused.pgid == pgid
            and reused.process_identity_token == process_identity_token
        ):
            canonical = json.dumps(existing, sort_keys=True, separators=(",", ":"))
            if existing_raw != canonical:
                return _invalid_bundle(job, "diagnostic_existing_raw_mismatch")
            publication_check = verify_evidence_bytes(
                execute,
                record_kind="stall_diagnostic",
                record_id=f"stall-diagnostic-{job_id}",
                raw=existing_raw.encode("utf-8"),
            )
            if not publication_check.authorized:
                return _invalid_bundle(job, f"diagnostic_existing_{publication_check.status}")
            return reused
        return _invalid_bundle(job, "diagnostic_identity_conflict")
    if existing_state != "absent":
        return _invalid_bundle(job, f"diagnostic_existing_{existing_state}")

    command = _diagnostic_command(job_id=job_id, pid=pid, pgid=pgid, job=job)
    probe = _execute(execute, command, timeout=30, truncate_output=False)
    if not _succeeded(probe):
        return _invalid_bundle(job, "diagnostic_probe_failed")
    probe_output = _text(probe.get("output"))
    if len(probe_output.encode("utf-8", "replace")) > _MAX_DIAGNOSTIC_OUTPUT_BYTES:
        return _invalid_bundle(job, "diagnostic_probe_oversized")
    parsed = _parse_probe_output(probe_output)
    if parsed is None:
        return _invalid_bundle(job, "diagnostic_probe_malformed")
    metrics, raw = parsed
    if metrics.get("job_id") != job_id or _as_int(metrics.get("pgid")) != pgid:
        return _invalid_bundle(job, "diagnostic_probe_identity_mismatch")
    if not _valid_snapshot_metrics(metrics):
        return _invalid_bundle(job, "diagnostic_probe_malformed")

    hashes: Dict[str, str] = {}
    for name in _RAW_NAMES:
        content = raw.get(name, "")
        if len(content.encode("utf-8", "replace")) > _RAW_MAX_BYTES[name]:
            return _invalid_bundle(job, f"diagnostic_{name}_oversized")
        actual = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        expected = _digest(metrics.get(f"{name}_sha256"))
        if not expected or actual != expected:
            return _invalid_bundle(job, f"diagnostic_{name}_hash_mismatch")
        hashes[name] = actual

    snapshot = JobProgressSnapshot(
        process_state=_text(metrics.get("process_state")) or "unknown",
        log_size=_nonnegative_int(metrics.get("log_size")),
        process_identity_sha256=_digest(metrics.get("process_identity_sha256")),
        artifact_sha256=hashes["artifact"],
        report_sha256=hashes["report"],
        cpu_ticks_delta=_nonnegative_int(metrics.get("cpu_ticks_delta")),
        process_count=_nonnegative_int(metrics.get("process_count")),
        child_count=_nonnegative_int(metrics.get("child_count")),
    )
    raw_before = job.get("before")
    before: Mapping[str, Any] = raw_before if isinstance(raw_before, Mapping) else {}
    report_delta_count = _report_delta_count(before, raw["report"])
    observation = _typed_observation(snapshot, raw["process"], raw["jvm"])
    payload: Dict[str, Any] = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "job_id": job_id,
        "pid": pid,
        "pgid": pgid,
        "process_identity_token": process_identity_token,
        "captured_at": captured_at or _utc_now(),
        "observation": observation,
        "snapshot": snapshot.as_dict(),
        "report_delta_count": report_delta_count,
        "content_hashes": hashes,
        "raw": raw,
    }
    fingerprint = _canonical_sha256(payload)
    payload["diagnostic_fingerprint"] = fingerprint
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    write = write_container_text_atomic(execute, ref, body, validate_json=True)
    if not write.persisted:
        return DiagnosticBundle(
            job_id=job_id,
            pid=pid,
            pgid=pgid,
            process_identity_token=process_identity_token,
            evidence_ref=ref,
            diagnostic_fingerprint=fingerprint,
            observation=observation,
            snapshot=snapshot,
            report_delta_count=report_delta_count,
            content_hashes=hashes,
            persisted=False,
            code=write.code,
        )
    publication_attempt = publish_evidence_bytes(
        execute,
        record_kind="stall_diagnostic",
        record_id=f"stall-diagnostic-{job_id}",
        raw=body.encode("utf-8"),
    )
    if not publication_attempt.published:
        return DiagnosticBundle(
            job_id=job_id,
            pid=pid,
            pgid=pgid,
            process_identity_token=process_identity_token,
            evidence_ref=ref,
            diagnostic_fingerprint=fingerprint,
            observation=observation,
            snapshot=snapshot,
            report_delta_count=report_delta_count,
            content_hashes=hashes,
            persisted=False,
            code=publication_attempt.status,
        )
    return DiagnosticBundle(
        job_id=job_id,
        pid=pid,
        pgid=pgid,
        process_identity_token=process_identity_token,
        evidence_ref=ref,
        diagnostic_fingerprint=fingerprint,
        observation=observation,
        snapshot=snapshot,
        report_delta_count=report_delta_count,
        content_hashes=hashes,
        persisted=True,
        code="captured",
    )


def probe_job_progress(
    execute: Callable[..., Any],
    job: Mapping[str, Any],
    *,
    previous: Optional[JobProgressSnapshot] = None,
) -> ProgressObservation:
    """Take one lightweight physical progress sample for a registered PGID."""

    identity = _job_identity(job)
    if identity is None:
        return ProgressObservation(JobProgressSnapshot(), code="invalid_registered_job_identity")
    job_id, pid, pgid = identity
    result = _execute(
        execute,
        _progress_command(job_id=job_id, pid=pid, pgid=pgid, job=job),
        timeout=20,
        truncate_output=False,
    )
    if not _succeeded(result):
        return ProgressObservation(JobProgressSnapshot(), code="progress_probe_failed")
    output = _text(result.get("output"))
    if len(output.encode("utf-8", "replace")) > _MAX_PROGRESS_OUTPUT_BYTES:
        return ProgressObservation(JobProgressSnapshot(), code="progress_probe_oversized")
    metrics = _parse_metrics(output)
    if metrics.get("job_id") != job_id or _as_int(metrics.get("pgid")) != pgid:
        return ProgressObservation(JobProgressSnapshot(), code="progress_probe_identity_mismatch")
    if any(
        metrics.get(name) != "1"
        for name in ("identity_complete", "artifact_complete", "report_complete")
    ):
        return ProgressObservation(JobProgressSnapshot(), code="progress_probe_incomplete")
    if not _valid_snapshot_metrics(metrics):
        return ProgressObservation(JobProgressSnapshot(), code="progress_probe_malformed")
    snapshot = JobProgressSnapshot(
        process_state=_text(metrics.get("process_state")) or "unknown",
        log_size=_nonnegative_int(metrics.get("log_size")),
        process_identity_sha256=_digest(metrics.get("process_identity_sha256")),
        artifact_sha256=_digest(metrics.get("artifact_sha256")),
        report_sha256=_digest(metrics.get("report_sha256")),
        cpu_ticks_delta=_nonnegative_int(metrics.get("cpu_ticks_delta")),
        process_count=_nonnegative_int(metrics.get("process_count")),
        child_count=_nonnegative_int(metrics.get("child_count")),
    )
    signals = _progress_signals(previous, snapshot)
    return ProgressObservation(snapshot=snapshot, signals=signals)


def seal_stall_evidence(
    execute: Callable[..., Any],
    diagnostic: DiagnosticBundle,
    *,
    reason: str = "repeated_no_progress",
    sealed_at: Optional[str] = None,
) -> EvidenceSeal:
    """Seal one persisted diagnostic before any process signal is legal."""

    ref = diagnostic_seal_ref(diagnostic.job_id)
    normalized_reason = _text(reason) or _STALL_SEAL_REASON
    if normalized_reason != _STALL_SEAL_REASON:
        return EvidenceSeal(
            job_id=diagnostic.job_id,
            pgid=diagnostic.pgid,
            process_identity_token=diagnostic.process_identity_token,
            evidence_ref=ref,
            diagnostic_ref=diagnostic.evidence_ref,
            diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
            persisted=False,
            code="invalid_seal_reason",
        )
    if not diagnostic.persisted or not _digest(diagnostic.diagnostic_fingerprint):
        return EvidenceSeal(
            job_id=diagnostic.job_id,
            pgid=diagnostic.pgid,
            process_identity_token=diagnostic.process_identity_token,
            evidence_ref=ref,
            diagnostic_ref=diagnostic.evidence_ref,
            diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
            persisted=False,
            code="diagnostic_not_persisted",
        )
    payload = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "job_id": diagnostic.job_id,
        "pgid": diagnostic.pgid,
        "process_identity_token": diagnostic.process_identity_token,
        "diagnostic_ref": diagnostic.evidence_ref,
        "diagnostic_fingerprint": diagnostic.diagnostic_fingerprint,
        "reason": normalized_reason,
        "sealed_at": sealed_at or _utc_now(),
    }
    existing_state, existing, existing_raw = _read_json_state(execute, ref)
    if existing_state == "present" and existing is not None:
        if all(existing.get(key) == value for key, value in payload.items() if key != "sealed_at"):
            canonical = json.dumps(existing, sort_keys=True, separators=(",", ":"))
            if existing_raw != canonical:
                return EvidenceSeal(
                    job_id=diagnostic.job_id,
                    pgid=diagnostic.pgid,
                    process_identity_token=diagnostic.process_identity_token,
                    evidence_ref=ref,
                    diagnostic_ref=diagnostic.evidence_ref,
                    diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
                    persisted=False,
                    code="seal_existing_raw_mismatch",
                )
            publication_check = verify_evidence_bytes(
                execute,
                record_kind="stall_seal",
                record_id=f"stall-seal-{diagnostic.job_id}",
                raw=existing_raw.encode("utf-8"),
            )
            return EvidenceSeal(
                job_id=diagnostic.job_id,
                pgid=diagnostic.pgid,
                process_identity_token=diagnostic.process_identity_token,
                evidence_ref=ref,
                diagnostic_ref=diagnostic.evidence_ref,
                diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
                persisted=publication_check.authorized,
                code=(
                    "already_sealed"
                    if publication_check.authorized
                    else f"seal_existing_{publication_check.status}"
                ),
            )
        return EvidenceSeal(
            job_id=diagnostic.job_id,
            pgid=diagnostic.pgid,
            process_identity_token=diagnostic.process_identity_token,
            evidence_ref=ref,
            diagnostic_ref=diagnostic.evidence_ref,
            diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
            persisted=False,
            code="seal_identity_conflict",
        )
    if existing_state != "absent":
        return EvidenceSeal(
            job_id=diagnostic.job_id,
            pgid=diagnostic.pgid,
            process_identity_token=diagnostic.process_identity_token,
            evidence_ref=ref,
            diagnostic_ref=diagnostic.evidence_ref,
            diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
            persisted=False,
            code=f"seal_existing_{existing_state}",
        )
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    write = write_container_text_atomic(execute, ref, body, validate_json=True)
    publication_attempt = (
        publish_evidence_bytes(
            execute,
            record_kind="stall_seal",
            record_id=f"stall-seal-{diagnostic.job_id}",
            raw=body.encode("utf-8"),
        )
        if write.persisted
        else None
    )
    persisted = bool(write.persisted and publication_attempt and publication_attempt.published)
    return EvidenceSeal(
        job_id=diagnostic.job_id,
        pgid=diagnostic.pgid,
        process_identity_token=diagnostic.process_identity_token,
        evidence_ref=ref,
        diagnostic_ref=diagnostic.evidence_ref,
        diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
        persisted=persisted,
        code=(
            "sealed"
            if persisted
            else publication_attempt.status if publication_attempt else write.code
        ),
    )


def cleanup_registered_process_group(
    execute: Callable[..., Any],
    job: Mapping[str, Any],
    seal: EvidenceSeal,
    *,
    now: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    grace_seconds: int = STALL_CLEANUP_GRACE_SECONDS,
    poll_seconds: int = 5,
) -> CleanupResult:
    """TERM -> 120s -> KILL, restricted to the sealed registered negative PGID."""

    identity = _job_identity(job)
    if identity is None:
        return CleanupResult(_text(job.get("job_id")), 0, "invalid_registered_job_identity")
    job_id, _pid, pgid = identity
    expected_token = _digest(job.get("process_identity_token"))
    if (
        not seal.persisted
        or seal.job_id != job_id
        or seal.pgid != pgid
        or seal.process_identity_token != expected_token
        or seal.evidence_ref != diagnostic_seal_ref(job_id)
        or seal.diagnostic_ref != diagnostic_bundle_ref(job_id)
    ):
        return CleanupResult(job_id, pgid, "unsealed_or_mismatched_evidence")
    seal_state, durable, seal_raw = _read_json_state(execute, seal.evidence_ref)
    if (
        seal_state != "present"
        or durable is None
        or seal_raw is None
        or not _seal_matches(durable, seal)
        or not verify_evidence_bytes(
            execute,
            record_kind="stall_seal",
            record_id=f"stall-seal-{job_id}",
            raw=seal_raw.encode("utf-8"),
        ).authorized
    ):
        return CleanupResult(job_id, pgid, "durable_seal_unavailable")
    diagnostic_state, diagnostic_payload, diagnostic_raw = _read_json_state(
        execute, seal.diagnostic_ref
    )
    durable_diagnostic = (
        _bundle_from_payload(diagnostic_payload, ref=seal.diagnostic_ref, reused=True)
        if diagnostic_state == "present" and diagnostic_payload is not None
        else None
    )
    if (
        durable_diagnostic is None
        or durable_diagnostic.job_id != job_id
        or durable_diagnostic.pid != _pid
        or durable_diagnostic.pgid != pgid
        or durable_diagnostic.process_identity_token != seal.process_identity_token
        or durable_diagnostic.diagnostic_fingerprint != seal.diagnostic_fingerprint
        or diagnostic_raw is None
        or not verify_evidence_bytes(
            execute,
            record_kind="stall_diagnostic",
            record_id=f"stall-diagnostic-{job_id}",
            raw=diagnostic_raw.encode("utf-8"),
        ).authorized
    ):
        return CleanupResult(job_id, pgid, "durable_diagnostic_unavailable")

    now = now or time.monotonic
    sleep = sleep or time.sleep
    grace = max(0, int(grace_seconds))
    interval = max(1, int(poll_seconds))
    if not _process_group_live(execute, pgid):
        return _persist_cleanup(
            execute,
            CleanupResult(job_id, pgid, "already_terminal", group_live=False),
            seal,
        )
    if not _registered_identity_matches(execute, job, job_id=job_id, pid=_pid, pgid=pgid):
        return CleanupResult(job_id, pgid, "registered_identity_unverified", group_live=True)

    # The double dash and explicit leading minus are the safety boundary.  No
    # caller-owned command text or process name enters either signal command.
    term = _execute(execute, f"kill -TERM -- -{pgid}", timeout=15)
    if not _succeeded(term):
        return _persist_cleanup(
            execute,
            CleanupResult(job_id, pgid, "term_failed", group_live=True),
            seal,
        )
    deadline = now() + grace
    while now() < deadline:
        if not _process_group_live(execute, pgid):
            return _persist_cleanup(
                execute,
                CleanupResult(
                    job_id,
                    pgid,
                    "terminated_after_term",
                    term_sent=True,
                    group_live=False,
                ),
                seal,
            )
        sleep(min(float(interval), max(0.0, deadline - now())))

    if not _process_group_live(execute, pgid):
        return _persist_cleanup(
            execute,
            CleanupResult(job_id, pgid, "terminated_after_term", term_sent=True, group_live=False),
            seal,
        )
    if not _registered_identity_matches(execute, job, job_id=job_id, pid=_pid, pgid=pgid):
        return _persist_cleanup(
            execute,
            CleanupResult(
                job_id,
                pgid,
                "identity_changed_before_kill",
                term_sent=True,
                group_live=True,
            ),
            seal,
        )
    killed = _execute(execute, f"kill -KILL -- -{pgid}", timeout=15)
    live = _process_group_live(execute, pgid)
    result = CleanupResult(
        job_id,
        pgid,
        "killed" if _succeeded(killed) and not live else "job_live_at_close",
        term_sent=True,
        kill_sent=_succeeded(killed),
        group_live=live,
    )
    return _persist_cleanup(execute, result, seal)


def control_stalled_job(
    execute: Callable[..., Any],
    job: Mapping[str, Any],
    *,
    now: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    trigger: str = STALL_CONFIRMATION_TRIGGER,
    previous: Optional[JobProgressSnapshot] = None,
    confirmation_grace_seconds: int = STALL_CONFIRMATION_GRACE_SECONDS,
    cleanup_grace_seconds: int = STALL_CLEANUP_GRACE_SECONDS,
) -> StallControlResult:
    """One controller-barrier hook for a quiet-window or wall-guard event.

    ``stall_confirmation`` may diagnose and clean only after two complete
    no-progress samples.  ``wall_guard`` is a close-budget observation: it
    never upgrades elapsed time to a stall or signal authority.  In either
    mode, a progressing job returns before a diagnostic bundle is created.
    A terminal or terminal-unpersisted ledger state spends no process wait.
    """

    job_id = _text(job.get("job_id"))
    if trigger not in {STALL_CONFIRMATION_TRIGGER, WALL_GUARD_TRIGGER}:
        return StallControlResult(job_id, "invalid_trigger", trigger=trigger)
    if _text(job.get("process_state")) == "terminal":
        return StallControlResult(job_id, "terminal_no_wait", trigger=trigger)

    first = probe_job_progress(execute, job, previous=previous)
    if first.code != "observed":
        return StallControlResult(job_id, "progress_unobservable", progress=first, trigger=trigger)
    if first.snapshot.process_state == "terminal":
        return StallControlResult(job_id, "terminal_no_wait", progress=first, trigger=trigger)
    if first.progressing:
        code = "wall_guard_progressing" if trigger == WALL_GUARD_TRIGGER else "progress_observed"
        return StallControlResult(job_id, code, progress=first, trigger=trigger)
    if trigger == WALL_GUARD_TRIGGER:
        # Elapsed budget plus one quiet sample is not a repeated stall
        # signature.  The barrier caller may record an honest live-at-close,
        # but it receives no evidence seal and no signal authority here.
        return StallControlResult(
            job_id,
            "wall_guard_no_progress_unconfirmed",
            progress=first,
            trigger=trigger,
        )

    diagnostic = collect_stall_diagnostic(execute, job)
    if not diagnostic.persisted:
        return StallControlResult(
            job_id,
            "diagnostic_unpersisted",
            diagnostic=diagnostic,
            progress=first,
            trigger=trigger,
        )
    if diagnostic.snapshot.cpu_active:
        observed = ProgressObservation(
            diagnostic.snapshot,
            ("cpu_active",),
            "observed",
        )
        return StallControlResult(
            job_id,
            "progress_observed",
            diagnostic=diagnostic,
            progress=observed,
            trigger=trigger,
        )

    sleeper = sleep or time.sleep
    sleeper(max(0.0, float(confirmation_grace_seconds)))
    second = probe_job_progress(execute, job, previous=first.snapshot)
    if second.code != "observed":
        return StallControlResult(
            job_id,
            "progress_unobservable",
            diagnostic=diagnostic,
            progress=second,
            trigger=trigger,
        )
    if second.snapshot.process_state == "terminal":
        return StallControlResult(
            job_id,
            "terminal_no_wait",
            diagnostic=diagnostic,
            progress=second,
            trigger=trigger,
        )
    if second.progressing:
        return StallControlResult(
            job_id,
            "progress_observed",
            diagnostic=diagnostic,
            progress=second,
            trigger=trigger,
        )

    seal = seal_stall_evidence(execute, diagnostic)
    if not seal.persisted:
        return StallControlResult(
            job_id,
            "seal_unpersisted",
            diagnostic=diagnostic,
            progress=second,
            seal=seal,
            trigger=trigger,
        )
    cleanup = cleanup_registered_process_group(
        execute,
        job,
        seal,
        now=now,
        sleep=sleep,
        grace_seconds=cleanup_grace_seconds,
    )
    return StallControlResult(
        job_id,
        cleanup.code,
        diagnostic=diagnostic,
        progress=second,
        seal=seal,
        cleanup=cleanup,
        trigger=trigger,
    )


def _diagnostic_command(*, job_id: str, pid: int, pgid: int, job: Mapping[str, Any]) -> str:
    log_path = shlex.quote(_text(job.get("log_path")))
    workdir = shlex.quote(_text(job.get("working_directory")) or "/workspace")
    prefix = _probe_prefix(job_id=job_id, pid=pid, pgid=pgid, log_path=log_path, workdir=workdir)
    raw_emit = " ".join(
        f'printf "RAW_{name.upper()}_B64:%s\\n" "$(base64 -w 0 "$tmp/{name}")";'
        for name in _RAW_NAMES
    )
    hash_emit = " ".join(
        f'printf "{name.upper()}_SHA256:%s\\n" "$(sha256sum "$tmp/{name}" | awk \'{{print $1}}\')";'
        for name in _RAW_NAMES
    )
    return (
        "set +e; tmp=$(mktemp -d /tmp/sag-job-diagnostic.XXXXXX) || exit 70; "
        "trap 'rm -r -- \"$tmp\"' EXIT; "
        + prefix
        + f"ps -eo pid=,ppid=,pgid=,sid=,etimes=,pcpu=,stat=,wchan=,comm=,args= "
        f"| awk -v g={pgid} '$3 == g' | head -n 128 | head -c 131072 "
        '> "$tmp/process"; '
        f"awk -v g={pgid} '$3 == g {{print $1, $2, $3, $4, $9, substr($0,index($0,$10))}}' "
        '"$tmp/process" | sha256sum | awk \'{print $1}\' > "$tmp/identity.sha"; '
        "pids=$(awk '{print $1}' \"$tmp/process\"); "
        "bounded_pids=$(printf '%s\\n' $pids | head -n 32); "
        "{ for p in $bounded_pids; do printf 'PID %s\\n' \"$p\"; "
        'ls -l "/proc/$p/fd" 2>/dev/null | head -n 32; done; } '
        '| head -n 128 | head -c 131072 > "$tmp/fd"; '
        '{ if command -v ss >/dev/null 2>&1 && [ -n "$bounded_pids" ]; then '
        "pid_regex=$(printf '%s|' $bounded_pids); pid_regex=${pid_regex%|}; "
        'timeout --kill-after=1 3 ss -tanp 2>/dev/null | grep -E "pid=($pid_regex),"; '
        'fi; } | head -n 128 | head -c 131072 > "$tmp/socket"; '
        f"timeout --kill-after=2 4 find {workdir} \\( -path '*/target/*' "
        "-o -path '*/build/*' "
        "-o -path '*/.setup_agent/pytest-reports/*' \\) -type f "
        "-printf '%T@\\t%s\\t%p\\n' 2>/dev/null | head -n 2048 | sort -nr "
        '| head -n 512 | head -c 131072 > "$tmp/artifact"; '
        f"timeout --kill-after=2 6 find {workdir} -type f "
        "\\( -path '*/surefire-reports/*.xml' "
        "-o -path '*/failsafe-reports/*.xml' -o -path '*/test-results/*/*.xml' "
        "-o -path '*/pytest-reports/*.xml' \\) -exec sha256sum {} + 2>/dev/null "
        '| head -n 512 | head -c 131072 > "$tmp/report"; '
        "java_pid=$(awk '$9 ~ /(^|\\/)java/ {print $1; exit}' \"$tmp/process\"); "
        'jvm_attempted=0; : > "$tmp/jvm"; '
        'if [ -n "$java_pid" ]; then if command -v jcmd >/dev/null 2>&1; then '
        'jvm_attempted=1; timeout --kill-after=2 10 jcmd "$java_pid" Thread.print '
        '> "$tmp/jvm" 2>&1; '
        "elif command -v jstack >/dev/null 2>&1; then jvm_attempted=1; "
        'timeout --kill-after=2 10 jstack "$java_pid" > "$tmp/jvm" 2>&1; fi; fi; '
        'head -n 400 "$tmp/jvm" | head -c 262144 > "$tmp/jvm.bounded"; '
        'mv "$tmp/jvm.bounded" "$tmp/jvm"; '
        f'tail -n 120 {log_path} 2>/dev/null | head -c 65536 > "$tmp/log_tail"; '
        f"printf 'JOB_ID:{job_id}\\nPGID:{pgid}\\n'; "
        "printf 'PROCESS_STATE:%s\\n' \"$process_state\"; "
        "printf 'LOG_SIZE:%s\\n' \"$log_size\"; "
        "printf 'CPU_TICKS_DELTA:%s\\n' \"$cpu_delta\"; "
        "printf 'PROCESS_COUNT:%s\\n' \"$process_count\"; "
        "printf 'CHILD_COUNT:%s\\n' \"$child_count\"; "
        'printf \'PROCESS_IDENTITY_SHA256:%s\\n\' "$(cat "$tmp/identity.sha")"; '
        "printf 'JVM_ATTEMPTED:%s\\n' \"$jvm_attempted\"; " + hash_emit + raw_emit
    )


def _progress_command(*, job_id: str, pid: int, pgid: int, job: Mapping[str, Any]) -> str:
    log_path = shlex.quote(_text(job.get("log_path")))
    workdir = shlex.quote(_text(job.get("working_directory")) or "/workspace")
    prefix = _probe_prefix(job_id=job_id, pid=pid, pgid=pgid, log_path=log_path, workdir=workdir)
    return (
        "set +e; set -o pipefail; tmp=$(mktemp -d /tmp/sag-job-progress.XXXXXX) || exit 70; "
        "trap 'rm -r -- \"$tmp\"' EXIT; "
        + prefix
        + f"timeout --kill-after=1 3 ps -eo pid=,ppid=,pgid=,sid=,comm=,args= "
        f"| awk -v g={pgid} '$3 == g' | sha256sum | awk '{{print $1}}' "
        '> "$tmp/identity.sha"; identity_rc=$?; '
        f"timeout --kill-after=2 8 find {workdir} \\( -path '*/target/*' "
        "-o -path '*/build/*' "
        "-o -path '*/.setup_agent/pytest-reports/*' \\) -type f "
        "-printf '%T@\\t%s\\t%p\\n' 2>/dev/null | sha256sum | awk '{print $1}' "
        '> "$tmp/artifact.sha"; artifact_rc=$?; '
        f"timeout --kill-after=2 4 find {workdir} -type f "
        "\\( -path '*/surefire-reports/*.xml' "
        "-o -path '*/failsafe-reports/*.xml' -o -path '*/test-results/*/*.xml' "
        "-o -path '*/pytest-reports/*.xml' \\) -printf '%T@\\t%s\\t%p\\n' 2>/dev/null "
        "| sha256sum | awk '{print $1}' > \"$tmp/report.sha\"; report_rc=$?; "
        f"printf 'JOB_ID:{job_id}\\nPGID:{pgid}\\n'; "
        "printf 'PROCESS_STATE:%s\\n' \"$process_state\"; "
        "printf 'LOG_SIZE:%s\\n' \"$log_size\"; "
        "printf 'CPU_TICKS_DELTA:%s\\n' \"$cpu_delta\"; "
        "printf 'PROCESS_COUNT:%s\\n' \"$process_count\"; "
        "printf 'CHILD_COUNT:%s\\n' \"$child_count\"; "
        '[ "$identity_rc" -eq 0 ] && identity_complete=1 || identity_complete=0; '
        '[ "$artifact_rc" -eq 0 ] && artifact_complete=1 || artifact_complete=0; '
        '[ "$report_rc" -eq 0 ] && report_complete=1 || report_complete=0; '
        "printf 'IDENTITY_COMPLETE:%s\\n' \"$identity_complete\"; "
        "printf 'ARTIFACT_COMPLETE:%s\\n' \"$artifact_complete\"; "
        "printf 'REPORT_COMPLETE:%s\\n' \"$report_complete\"; "
        'printf \'PROCESS_IDENTITY_SHA256:%s\\n\' "$(cat "$tmp/identity.sha")"; '
        'printf \'ARTIFACT_SHA256:%s\\n\' "$(cat "$tmp/artifact.sha")"; '
        'printf \'REPORT_SHA256:%s\\n\' "$(cat "$tmp/report.sha")"'
    )


def _probe_prefix(*, job_id: str, pid: int, pgid: int, log_path: str, workdir: str) -> str:
    del pid, workdir
    # `/proc/<pid>/stat` has a parenthesized comm field which may contain
    # spaces.  Strip through the final `)` before selecting original fields
    # 14/15 (now positions 12/13) so CPU activity is based on tick DELTA, not
    # lifetime-average `%CPU`.
    return (
        f"job_id={shlex.quote(job_id)}; pgid={pgid}; "
        f"log_size=$(wc -c < {log_path} 2>/dev/null || printf 0); "
        f"pids=$(ps -eo pid=,pgid= | awk -v g={pgid} '$2 == g {{print $1}}'); "
        "sum_ticks() { total=0; for p in $pids; do "
        'line=$(cat "/proc/$p/stat" 2>/dev/null) || continue; '
        "rest=${line##*) }; set -- $rest; u=${12:-0}; s=${13:-0}; "
        "total=$((total + u + s)); done; printf '%s' \"$total\"; }; "
        "ticks_before=$(sum_ticks); sleep 1; ticks_after=$(sum_ticks); "
        'cpu_delta=$((ticks_after - ticks_before)); [ "$cpu_delta" -ge 0 ] || cpu_delta=0; '
        "process_count=$(printf '%s\\n' $pids | awk 'NF {n++} END {print n+0}'); "
        "child_count=$((process_count > 0 ? process_count - 1 : 0)); "
        'if [ "$process_count" -gt 0 ]; then process_state=running; '
        "else process_state=terminal; fi; "
    )


def _parse_probe_output(output: str) -> Optional[Tuple[Dict[str, str], Dict[str, str]]]:
    metrics = _parse_metrics(output)
    raw: Dict[str, str] = {}
    for name in _RAW_NAMES:
        encoded = metrics.get(f"raw_{name}_b64")
        if encoded is None:
            return None
        try:
            raw[name] = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
        except (ValueError, UnicodeError):
            return None
    return metrics, raw


def _parse_metrics(output: str) -> Dict[str, str]:
    metrics: Dict[str, str] = {}
    allowed = {
        "job_id",
        "pgid",
        "process_state",
        "log_size",
        "cpu_ticks_delta",
        "process_count",
        "child_count",
        "process_identity_sha256",
        "artifact_sha256",
        "report_sha256",
        "jvm_attempted",
        "identity_complete",
        "artifact_complete",
        "report_complete",
        *(f"{name}_sha256" for name in _RAW_NAMES),
        *(f"raw_{name}_b64" for name in _RAW_NAMES),
    }
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        normalized = key.strip().lower()
        if separator and normalized in allowed and normalized not in metrics:
            metrics[normalized] = value.strip()
    return metrics


def _valid_snapshot_metrics(metrics: Mapping[str, Any]) -> bool:
    state = _text(metrics.get("process_state"))
    if state not in {"running", "terminal"}:
        return False
    numbers = {
        name: _as_int(metrics.get(name))
        for name in (
            "log_size",
            "cpu_ticks_delta",
            "process_count",
            "child_count",
        )
    }
    if any(value is None or value < 0 for value in numbers.values()):
        return False
    process_count = numbers["process_count"]
    child_count = numbers["child_count"]
    if process_count is None or child_count is None or child_count > process_count:
        return False
    if (state == "running") != (process_count > 0):
        return False
    return all(
        _digest(metrics.get(name))
        for name in (
            "process_identity_sha256",
            "artifact_sha256",
            "report_sha256",
        )
    )


def _typed_observation(snapshot: JobProgressSnapshot, process: str, jvm: str) -> str:
    """Classify only physical bundle content; project identity is unavailable."""

    jvm_lower = jvm.casefold()
    process_lower = process.casefold()
    if "deadlock" in jvm_lower:
        return "deadlock_signature"
    if snapshot.cpu_active:
        return "cpu_active"
    if "thread.join" in jvm_lower and (" waiting" in jvm_lower or "waiting " in jvm_lower):
        return "thread_join_wait"
    if any(marker in process_lower for marker in ("io_schedule", "wait_on_page", "blk_mq")):
        return "io_wait"
    return "unknown"


def _progress_signals(
    previous: Optional[JobProgressSnapshot], current: JobProgressSnapshot
) -> Tuple[str, ...]:
    signals = []
    if current.cpu_active:
        signals.append("cpu_active")
    if previous is not None:
        if current.log_size > previous.log_size:
            signals.append("log_growth")
        if _changed(previous.artifact_sha256, current.artifact_sha256):
            signals.append("artifact_delta")
        if _changed(previous.report_sha256, current.report_sha256):
            signals.append("report_delta")
        if _changed(previous.process_identity_sha256, current.process_identity_sha256):
            signals.append("child_process_transition")
    return tuple(dict.fromkeys(signals))


def _changed(before: str, after: str) -> bool:
    return bool(before and after and before != after)


def _report_delta_count(before: Mapping[str, Any], report_raw: str) -> int:
    current: Dict[str, str] = {}
    for line in report_raw.splitlines():
        digest, separator, path = line.partition("  ")
        digest = digest.strip().lstrip("\\")
        path = path.strip()
        if separator and _digest(digest) and path:
            current[path] = digest
    return sum(1 for path, digest in current.items() if _text(before.get(path)) != digest)


def _bundle_from_payload(
    payload: Mapping[str, Any], *, ref: str, reused: bool
) -> Optional[DiagnosticBundle]:
    try:
        if payload.get("schema_version") != DIAGNOSTIC_SCHEMA_VERSION:
            return None
        job_id = _text(payload.get("job_id"))
        pid = _positive_int(payload.get("pid"))
        pgid = _positive_int(payload.get("pgid"))
        process_identity_token = _digest(payload.get("process_identity_token"))
        fingerprint = _digest(payload.get("diagnostic_fingerprint"))
        observation = _text(payload.get("observation"))
        raw_payload = dict(payload)
        raw_payload.pop("diagnostic_fingerprint", None)
        if (
            not job_id
            or not pid
            or not pgid
            or not process_identity_token
            or observation not in _OBSERVATIONS
            or not _JOB_ID.fullmatch(job_id)
            or pid != pgid
            or ref != diagnostic_bundle_ref(job_id)
        ):
            return None
        if fingerprint != _canonical_sha256(raw_payload):
            return None
        snapshot_value = payload.get("snapshot")
        if not isinstance(snapshot_value, Mapping) or not _valid_snapshot_metrics(snapshot_value):
            return None
        hashes = payload.get("content_hashes")
        if not isinstance(hashes, Mapping) or set(hashes) != set(_RAW_NAMES):
            return None
        raw = payload.get("raw")
        if not isinstance(raw, Mapping) or set(raw) != set(_RAW_NAMES):
            return None
        for name in _RAW_NAMES:
            content = raw.get(name)
            expected = _digest(hashes.get(name))
            if not isinstance(content, str) or not expected:
                return None
            if len(content.encode("utf-8", "replace")) > _RAW_MAX_BYTES[name]:
                return None
            if hashlib.sha256(content.encode("utf-8", "replace")).hexdigest() != expected:
                return None
        return DiagnosticBundle(
            job_id=job_id,
            pid=pid,
            pgid=pgid,
            process_identity_token=process_identity_token,
            evidence_ref=ref,
            diagnostic_fingerprint=fingerprint,
            observation=observation,
            snapshot=JobProgressSnapshot.from_mapping(snapshot_value),
            report_delta_count=_nonnegative_int(payload.get("report_delta_count")),
            content_hashes={str(key): str(value) for key, value in hashes.items()},
            persisted=True,
            reused=reused,
            code="already_captured" if reused else "captured",
        )
    except (TypeError, ValueError):
        return None


def _invalid_bundle(job: Mapping[str, Any], code: str) -> DiagnosticBundle:
    return DiagnosticBundle(
        job_id=_text(job.get("job_id")),
        pid=_positive_int(job.get("pid")) or 0,
        pgid=_positive_int(job.get("pgid")) or 0,
        process_identity_token=_digest(job.get("process_identity_token")),
        evidence_ref=(
            diagnostic_bundle_ref(_text(job.get("job_id")))
            if _JOB_ID.fullmatch(_text(job.get("job_id")))
            else ""
        ),
        diagnostic_fingerprint="",
        observation="unknown",
        snapshot=JobProgressSnapshot(),
        persisted=False,
        code=code,
    )


def _persist_cleanup(
    execute: Callable[..., Any], result: CleanupResult, seal: EvidenceSeal
) -> CleanupResult:
    ref = diagnostic_cleanup_ref(result.job_id)
    payload = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "job_id": result.job_id,
        "pgid": result.pgid,
        "seal_ref": seal.evidence_ref,
        "code": result.code,
        "term_sent": result.term_sent,
        "kill_sent": result.kill_sent,
        "group_live": result.group_live,
        "recorded_at": _utc_now(),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    logical_artifact_id = f"stall-cleanup-{result.job_id}"
    raw = body.encode("utf-8")
    for _attempt in range(3):
        authority = evidence_publication_authority_for(execute)
        head = authority.latest_head(logical_artifact_id)
        expected_publication = latest_publication_raw_sha256(execute, logical_artifact_id)
        try:
            current = read_container_text(_ExecuteOnly(execute), ref, exact_bytes=True)
        except Exception:
            return _cleanup_persistence_failure(result, ref, "read_failed")
        if head is not None:
            if head.publication_state == "revoked":
                return _cleanup_persistence_failure(result, ref, "revoked")
            current_is_host_head = bool(
                current is not None
                and verify_latest_evidence_bytes(
                    execute,
                    record_kind="stall_cleanup",
                    record_id=logical_artifact_id,
                    logical_artifact_id=logical_artifact_id,
                    raw=current.encode("utf-8"),
                ).authorized
            )
            if not current_is_host_head:
                # Complete only the exact host half of this independently
                # computed write.  Any other unverified body is container
                # tampering, not a base from which a trusted revision may grow.
                if current == body:
                    publication = publish_evidence_revision(
                        execute,
                        record_kind="stall_cleanup",
                        record_id=logical_artifact_id,
                        logical_artifact_id=logical_artifact_id,
                        raw=raw,
                        expected_previous_raw_sha256=expected_publication,
                    )
                    if publication.published:
                        return _cleanup_persisted(result, ref)
                    return _cleanup_persistence_failure(result, ref, publication.status)
                return _cleanup_persistence_failure(result, ref, "not_current")
        write = compare_publish_container_text_atomic(
            execute,
            ref,
            body,
            expected_content=current,
            validate_json=True,
        )
        if not write.persisted:
            if write.code == WRITE_COMPARE_CONFLICT:
                continue
            return _cleanup_persistence_failure(result, ref, write.code)
        publication = publish_evidence_revision(
            execute,
            record_kind="stall_cleanup",
            record_id=logical_artifact_id,
            logical_artifact_id=logical_artifact_id,
            raw=raw,
            expected_previous_raw_sha256=expected_publication,
        )
        if publication.published:
            return _cleanup_persisted(result, ref)
        return _cleanup_persistence_failure(result, ref, publication.status)
    return _cleanup_persistence_failure(result, ref, "compare_retry_exhausted")


def _cleanup_persisted(result: CleanupResult, ref: str) -> CleanupResult:
    return CleanupResult(
        result.job_id,
        result.pgid,
        result.code,
        result.term_sent,
        result.kill_sent,
        result.group_live,
        ref,
    )


def _cleanup_persistence_failure(result: CleanupResult, ref: str, code: str) -> CleanupResult:
    return CleanupResult(
        result.job_id,
        result.pgid,
        f"cleanup_record_{code}",
        result.term_sent,
        result.kill_sent,
        result.group_live,
        ref,
    )


class _ExecuteOnly:
    """Lossless-reader adapter for writers that receive an execute callback."""

    def __init__(self, execute: Callable[..., Any]) -> None:
        self._execute = execute
        owner = getattr(execute, "__self__", None)
        files = getattr(owner, "files", None)
        if isinstance(files, dict):
            self.files = files

    def execute_command(self, command: str, **kwargs: Any) -> Any:
        return self._execute(command, **kwargs)


def _process_group_live(execute: Callable[..., Any], pgid: int) -> bool:
    if pgid <= 1:
        return False
    # `kill -0 -- -PGID` is the only liveness operation and carries no signal.
    result = _execute(execute, f"kill -0 -- -{pgid}", timeout=15)
    return _succeeded(result)


def _registered_identity_matches(
    execute: Callable[..., Any],
    job: Mapping[str, Any],
    *,
    job_id: str,
    pid: int,
    pgid: int,
) -> bool:
    """Re-read launcher identity files and the live kernel session tuple."""

    expected_pid_path = f"/tmp/sag_jobs/{job_id}.pid"
    expected_pgid_path = f"/tmp/sag_jobs/{job_id}.pgid"
    expected_identity_path = f"/tmp/sag_jobs/{job_id}.identity"
    expected_token = _digest(job.get("process_identity_token"))
    if _text(job.get("pid_path")) != expected_pid_path:
        return False
    if _text(job.get("pgid_path")) != expected_pgid_path:
        return False
    if _text(job.get("identity_path")) != expected_identity_path or not expected_token:
        return False
    if _read_positive_integer(execute, expected_pid_path) != pid:
        return False
    if _read_positive_integer(execute, expected_pgid_path) != pgid:
        return False
    if _read_digest(execute, expected_identity_path) != expected_token:
        return False
    result = _execute(
        execute,
        f"ps -o pid=,pgid=,sid= -p {pid}",
        timeout=15,
    )
    if not _succeeded(result):
        return False
    rows = [line.split() for line in _text(result.get("output")).splitlines() if line.strip()]
    if rows != [[str(pid), str(pgid), str(pid)]]:
        return False
    current = _execute(
        execute,
        (
            f'stat_line="$(cat /proc/{pid}/stat 2>/dev/null)" || exit 1; '
            "stat_rest=${stat_line##*) }; set -- $stat_rest; start_ticks=${20:-}; "
            'boot_id="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" || exit 1; '
            f'printf \'%s:%s:%s:%s\' "$boot_id" {pid} {pgid} "$start_ticks" '
            "| sha256sum | awk '{print $1}'"
        ),
        timeout=15,
    )
    return _succeeded(current) and _digest(current.get("output")) == expected_token


def _read_positive_integer(execute: Callable[..., Any], path: str) -> Optional[int]:
    result = _execute(execute, f"cat {shlex.quote(path)} 2>/dev/null", timeout=15)
    if not _succeeded(result):
        return None
    lines = [line.strip() for line in _text(result.get("output")).splitlines() if line.strip()]
    return _positive_int(lines[0]) if len(lines) == 1 else None


def _read_digest(execute: Callable[..., Any], path: str) -> str:
    result = _execute(execute, f"cat {shlex.quote(path)} 2>/dev/null", timeout=15)
    if not _succeeded(result):
        return ""
    lines = [line.strip() for line in _text(result.get("output")).splitlines() if line.strip()]
    return _digest(lines[0]) if len(lines) == 1 else ""


def _seal_matches(payload: Optional[Mapping[str, Any]], seal: EvidenceSeal) -> bool:
    return bool(
        payload
        and payload.get("schema_version") == DIAGNOSTIC_SCHEMA_VERSION
        and _text(payload.get("job_id")) == seal.job_id
        and _as_int(payload.get("pgid")) == seal.pgid
        and _digest(payload.get("process_identity_token")) == seal.process_identity_token
        and _text(payload.get("diagnostic_ref")) == seal.diagnostic_ref
        and _text(payload.get("diagnostic_fingerprint")) == seal.diagnostic_fingerprint
        and _text(payload.get("reason")) == _STALL_SEAL_REASON
    )


def _job_identity(job: Mapping[str, Any]) -> Optional[Tuple[str, int, int]]:
    job_id = _text(job.get("job_id"))
    pid = _positive_int(job.get("pid"))
    pgid = _positive_int(job.get("pgid"))
    token = _digest(job.get("process_identity_token"))
    if not _JOB_ID.fullmatch(job_id) or pid is None or pgid is None or not token:
        return None
    # The launcher creates a new session whose leader is both PID and PGID.
    # Refusing any other shape prevents a forged ledger from redirecting a
    # cleanup signal at an unrelated group.
    if pid <= 1 or pgid <= 1 or pid != pgid:
        return None
    if _text(job.get("pid_path")) != f"/tmp/sag_jobs/{job_id}.pid":
        return None
    if _text(job.get("pgid_path")) != f"/tmp/sag_jobs/{job_id}.pgid":
        return None
    if _text(job.get("identity_path")) != f"/tmp/sag_jobs/{job_id}.identity":
        return None
    return job_id, pid, pgid


def _read_json_state(
    execute: Callable[..., Any], path: str
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    quoted = shlex.quote(path)
    result = _execute(
        execute,
        f"if [ ! -e {quoted} ]; then exit 44; fi; cat {quoted}",
        timeout=15,
    )
    if not _succeeded(result):
        if result.get("exit_code") == 44 and not result.get("dispatch_status"):
            return "absent", None, None
        return "unreadable", None, None
    raw = result.get("output")
    if not isinstance(raw, str) or not raw:
        return "invalid", None, None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return "invalid", None, raw
    if not isinstance(payload, Mapping):
        return "invalid", None, raw
    return "present", dict(payload), raw


def _execute(execute: Callable[..., Any], command: str, **kwargs: Any) -> Mapping[str, Any]:
    try:
        try:
            result = execute(command, **kwargs)
        except TypeError as exc:
            if not any(name in str(exc) for name in kwargs):
                raise
            result = execute(command)
    except Exception as exc:
        return {"exit_code": -1, "output": type(exc).__name__}
    return result if isinstance(result, Mapping) else {}


def _succeeded(result: Mapping[str, Any]) -> bool:
    return bool(result.get("exit_code") == 0 or result.get("success") is True)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> Optional[int]:
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_int(value: Any) -> int:
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed >= 0 else 0


def _digest(value: Any) -> str:
    text = _text(value).lower()
    return text if _SHA256.fullmatch(text) else ""


__all__ = [
    "CleanupResult",
    "DiagnosticBundle",
    "DIAGNOSTIC_ROOT",
    "EvidenceSeal",
    "JobProgressSnapshot",
    "ProgressObservation",
    "STALL_CLEANUP_GRACE_SECONDS",
    "STALL_CONFIRMATION_GRACE_SECONDS",
    "StallControlResult",
    "cleanup_registered_process_group",
    "collect_stall_diagnostic",
    "control_stalled_job",
    "diagnostic_bundle_ref",
    "diagnostic_cleanup_ref",
    "diagnostic_seal_ref",
    "probe_job_progress",
    "seal_stall_evidence",
]
