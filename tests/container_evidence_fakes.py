"""Shared in-memory container doubles for evidence persistence tests."""

import base64
import hashlib
import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from sag.agent.control_events import RunPin
from sag.agent.evidence_publications import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    RUN_PIN_LOGICAL_ARTIFACT_ID,
    EvidencePublicationAuthority,
    evidence_publication_authority_for,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.evidence_records import frame_json_record_stream, frame_named_json_record_stream
from sag.agent.invocation_receipts import RECEIPT_DIR, validate_receipt_v2

RUN_PIN_PATH = "/workspace/.setup_agent/run-pin.json"


@dataclass
class _MemoryControlSink:
    """Small host-owned sink used by strict evidence consumer unit tests."""

    path: str = "/host/test-control-events.jsonl"
    events: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    def emit(self, kind, payload, *, source=None):
        del source
        self.events.append((str(kind), dict(payload)))
        return None


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Return stable exact bytes for a test evidence artifact."""

    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))


def complete_run_pin(run_id: str, target_sha: str, **overrides: Any) -> dict[str, Any]:
    """Build the complete current RunPin shape required by strict readers."""

    payload: dict[str, Any] = {
        "run_id": run_id,
        "target_repo_sha": target_sha,
        "container_image_digest": f"sha256:{'1' * 64}",
        "sag_git_sha": "2" * 40,
        "thinking_model": "test-thinking-model",
        "action_model": "test-action-model",
        "sanitized_config": {},
        "prompt_bundle_sha256": "3" * 64,
        "feature_flags": {},
        "run_order_index": 1,
        "random_seed_or_null": 0,
        "dependency_cache_state": "test-cache",
        "host_arch": "test-arch",
        "advisor": None,
    }
    payload.update(overrides)
    return RunPin.model_validate(payload).model_dump(mode="json")


def complete_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fill only fields that legacy test receipts omitted from canonical v2."""

    normalized = dict(payload)
    normalized.setdefault(
        "argv",
        f"{normalized.get('tool', 'runner')} {normalized.get('effective_action', 'run')}",
    )
    if not normalized.get("report_delta"):
        normalized["report_delta"] = {"new": [], "changed": []}
    if isinstance(normalized.get("toolchain_fingerprint"), str):
        normalized["toolchain_fingerprint"] = {"version": normalized["toolchain_fingerprint"]}
    output_hash = normalized.get("output_content_hash")
    if isinstance(output_hash, str) and len(output_hash) != 64:
        normalized.pop("output_content_hash")
    binding_fields = {"contract_id", "contract_hash", "execution_binding"}
    if set(normalized).intersection(binding_fields) not in (set(), binding_fields):
        for field_name in binding_fields | {"compliance"}:
            normalized.pop(field_name, None)
    if normalized.get("execution_binding") != "argv_v1":
        normalized.pop("compliance", None)
    if normalized.get("testcase_outcomes") == []:
        normalized.pop("testcase_outcomes")
    return validate_receipt_v2(normalized, expected_id=str(normalized.get("receipt_id") or ""))


def add_published_receipt(
    source: Any,
    filesystem: "ContainerFS",
    payload: Mapping[str, Any],
    *,
    publish: bool = True,
) -> dict[str, Any]:
    """Persist and optionally host-publish one canonical v2 receipt."""

    candidate = complete_receipt(payload)
    receipt_id = str(candidate["receipt_id"])
    raw = canonical_json(candidate)
    filesystem.files[f"{RECEIPT_DIR}/{receipt_id}.json"] = raw
    if publish:
        authority = evidence_publication_authority_for(source)
        authority.publish_bytes(
            record_kind="invocation_receipt",
            record_id=receipt_id,
            raw=raw.encode("utf-8"),
        )
    return candidate


def add_published_mutable_json(
    source: Any,
    filesystem: "ContainerFS",
    *,
    path: str,
    record_kind: str,
    record_id: str,
    logical_artifact_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Persist and advance one exact mutable JSON head in a fake store."""

    raw = canonical_json(payload)
    filesystem.files[path] = raw
    authority = evidence_publication_authority_for(source)
    prior = authority.latest_head(logical_artifact_id)
    authority.publish_revision(
        record_kind=record_kind,
        record_id=record_id,
        logical_artifact_id=logical_artifact_id,
        raw=raw.encode("utf-8"),
        expected_previous_raw_sha256=(
            prior.raw_sha256 if prior is not None else EVIDENCE_PUBLICATION_GENESIS_SHA256
        ),
    )
    return raw


def strict_published_evidence(
    source: Any,
    *,
    run_id: str,
    target_sha: str,
    receipts: Sequence[Mapping[str, Any]] = (),
    run_pin: Mapping[str, Any] | str | bool | None = None,
) -> "ContainerFS":
    """Bind one fake container store to its exact host publication authority.

    ``run_pin=False`` means a genuinely absent artifact. String pins and pins
    for another run remain container bytes but are deliberately not authorized,
    preserving malformed/foreign negative cases without weakening production.
    """

    files: dict[str, str] = {}
    if run_pin is None:
        pin_payload: Mapping[str, Any] | str | bool = complete_run_pin(run_id, target_sha)
    elif isinstance(run_pin, Mapping):
        candidate = dict(run_pin)
        if candidate.get("run_id") == run_id:
            candidate = complete_run_pin(
                run_id,
                str(candidate.get("target_repo_sha") or target_sha),
                **{
                    key: value
                    for key, value in candidate.items()
                    if key not in {"run_id", "target_repo_sha"}
                },
            )
        pin_payload = candidate
    else:
        pin_payload = run_pin

    pin_raw: str | None = None
    publish_pin = isinstance(pin_payload, Mapping) and pin_payload.get("run_id") == run_id
    if pin_payload is not False:
        pin_raw = (
            str(pin_payload) if isinstance(pin_payload, str) else canonical_json(dict(pin_payload))
        )
        files[RUN_PIN_PATH] = pin_raw

    normalized_receipts: list[tuple[str, str, bool]] = []
    for receipt in receipts:
        candidate = complete_receipt(receipt)
        receipt_id = str(candidate["receipt_id"])
        raw = canonical_json(candidate)
        files[f"{RECEIPT_DIR}/{receipt_id}.json"] = raw
        normalized_receipts.append((receipt_id, raw, candidate.get("run_id") == run_id))

    filesystem = ContainerFS(files=files)
    authority = EvidencePublicationAuthority(run_id=run_id, sink=_MemoryControlSink())
    token = install_evidence_publication_authority(authority, orchestrator=source)
    reset_evidence_publication_authority(token)
    if publish_pin and pin_raw is not None:
        authority.publish_revision(
            record_kind="run_pin",
            record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            raw=pin_raw.encode("utf-8"),
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
    for receipt_id, raw, current in normalized_receipts:
        if current:
            authority.publish_bytes(
                record_kind="invocation_receipt",
                record_id=receipt_id,
                raw=raw.encode("utf-8"),
            )
    return filesystem


def ok(output=""):
    return {"success": True, "output": output}


def fail(output=""):
    return {"success": False, "output": output}


class ContainerFS:
    """Execute double with a file layer, so atomic writes are observable."""

    def __init__(self, files=None, writable=True):
        self.files = dict(files or {})
        self.writable = writable
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("file=") and "SAG_NAMED_JSON_RECORD_V1" in command:
            assignment = command.partition(";")[0]
            target = shlex.split(assignment[len("file=") :])[0]
            records = (
                [(target.rsplit("/", 1)[-1], self.files[target])] if target in self.files else []
            )
            return ok(frame_named_json_record_stream(records))
        if "for file in " in command and "/*.json; do " in command:
            quoted_glob = command.partition(" in ")[2].partition("; do")[0]
            target = shlex.split(quoted_glob)[0]
            prefix = target[: -len("*.json")]
            records = [
                (path.rsplit("/", 1)[-1], body)
                for path, body in sorted(self.files.items())
                if path.startswith(prefix) and path.endswith(".json")
            ]
            # Source files remain raw JSON.  Only command output is framed.
            if "SAG_NAMED_JSON_RECORD_V1" in command:
                return ok(frame_named_json_record_stream(records))
            return ok(frame_json_record_stream([body for _name, body in records]))
        if command.startswith("cat ") and "\n" not in command:
            target = shlex.split(command)[1]
            if target.endswith("/*.json"):
                prefix = target[: -len("*.json")]
                bodies = [
                    body
                    for path, body in sorted(self.files.items())
                    if path.startswith(prefix) and path.endswith(".json")
                ]
                return ok("\n".join(bodies))
            if target in self.files:
                return ok(self.files[target])
            return fail(f"cat: {target}: No such file or directory")
        tokens = (
            shlex.split(command) if "\n" not in command or command.startswith("python3 -c ") else []
        )
        if tokens[:3] == ["mkdir", "-p", "--"]:
            return ok("") if self.writable else fail("Read-only file system")
        if tokens[:2] == ["rm", "-f"]:
            targets = tokens[3:] if tokens[2:3] == ["--"] else tokens[2:]
            for target in targets:
                self.files.pop(target, None)
            return ok("")
        if len(tokens) == 3 and tokens[:2] == [":", ">"]:
            if not self.writable:
                return fail("Read-only file system")
            self.files[tokens[2]] = ""
            return ok("")
        if len(tokens) == 5 and tokens[:2] == ["printf", "%s"] and tokens[3] == ">>":
            if not self.writable:
                return fail("Read-only file system")
            self.files[tokens[4]] = self.files.get(tokens[4], "") + tokens[2]
            return ok("")
        if tokens[:2] == ["base64", "--decode"] and tokens[-2:-1] == [">"]:
            if not self.writable:
                return fail("Read-only file system")
            self.files[tokens[-1]] = base64.b64decode(self.files.get(tokens[2], "")).decode("utf-8")
            return ok("")
        if tokens[:2] == ["python3", "-c"] and "fcntl.flock" in tokens[2]:
            target, candidate, _lock_path, expected, expected_bytes, expected_sha = tokens[3:9]
            candidate_bytes = self.files.get(candidate, "").encode("utf-8")
            if (
                len(candidate_bytes) != int(expected_bytes)
                or hashlib.sha256(candidate_bytes).hexdigest() != expected_sha
            ):
                return {
                    "success": False,
                    "exit_code": 76,
                    "output": "SAG_CAS_CANDIDATE_INVALID\n",
                }
            current = self.files.get(target)
            actual = (
                "absent"
                if current is None
                else "sha256:" + hashlib.sha256(current.encode("utf-8")).hexdigest()
            )
            if actual != expected:
                return {
                    "success": False,
                    "exit_code": 75,
                    "output": "SAG_CAS_CONFLICT\n",
                }
            self.files[target] = self.files.pop(candidate)
            return {"success": True, "exit_code": 0, "output": ""}
        if tokens[:2] == ["python3", "-c"] and "hashlib.sha256" in tokens[2]:
            path, expected_bytes, expected_sha = tokens[3:6]
            payload = self.files.get(path, "").encode("utf-8")
            valid = (
                len(payload) == int(expected_bytes)
                and hashlib.sha256(payload).hexdigest() == expected_sha
            )
            return ok("") if valid else fail("validation failed")
        if tokens[:2] == ["python3", "-c"] and "json.load" in tokens[2]:
            try:
                json.loads(self.files.get(tokens[3], ""))
            except (TypeError, json.JSONDecodeError):
                return fail("invalid json")
            return ok("")
        if tokens[:3] == ["mv", "-f", "--"]:
            if not self.writable:
                return fail("Read-only file system")
            source, target = tokens[3:5]
            self.files[target] = self.files.pop(source)
            return ok("")
        if "mv -f " in command and "\n" in command:
            if not self.writable:
                return fail("Read-only file system")
            header, _, rest = command.partition("\n")
            heredoc = header.rsplit("<<'", 1)[1].split("'", 1)[0]
            body, _, _ = rest.partition(f"\n{heredoc}")
            final = header.rsplit("mv -f ", 1)[1].split()[1]
            self.files[final] = body
            return ok("")
        return ok("")

    def writes(self):
        return [command for command in self.commands if "mv -f " in command]


class ScriptedOrchestrator:
    """Expose a ``ContainerFS`` through the production execute-command surface."""

    def __init__(self, files=None):
        self.filesystem = ContainerFS(files=files)

    def execute_command(self, command, **kwargs):
        return self.filesystem(command)

    def execute_control_command(self, command, **kwargs):
        return self.filesystem(command)
