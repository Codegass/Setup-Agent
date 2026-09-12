"""Agent-maintained runtime environment overlay persistence."""

from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import re
import shlex
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, cast

from sag.agent.evidence_publications import (
    ENV_OVERLAY_LOGICAL_ARTIFACT_ID,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    MutablePublicationObservation,
    evidence_publication_authority_for,
    latest_publication_raw_sha256,
    publish_evidence_revision,
)
from sag.runtime.container_io import read_container_text, resolve_control_execute
from sag.utils.container_io import compare_publish_container_text_atomic

DEFAULT_OVERLAY_JSON = "/workspace/.setup_agent/env_overlay.json"
DEFAULT_OVERLAY_SCRIPT = "/workspace/.setup_agent/env_overlay.sh"
ENV_OVERLAY_RECORD_ID = ENV_OVERLAY_LOGICAL_ARTIFACT_ID
ENV_OVERLAY_MAX_BYTES = 1024 * 1024
RUNTIME_EXEC_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_TOOL_ALIASES = {"mvn": "maven"}
_RUNTIME_MAJOR_RE = re.compile(r"^(?:1\.)?(\d+)$")
_TARGET_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_DENIED_OVERLAY_ENV = frozenset(
    {"PATH", "BASH_ENV", "ENV", "CDPATH", "SHELLOPTS", "BASHOPTS", "GLOBIGNORE", "IFS"}
)

# A target/domain has made mutually exclusive statements about the runtime it
# needs.  The resolver returns this code instead of picking the newest record:
# time orders observations, it does not make one physical claim erase another.
RUNTIME_REQUIREMENT_CONFLICT = "java_runtime_requirement_conflict"


class EnvOverlayWarning(UserWarning):
    """Warning marker for recoverable overlay state problems."""


class EnvOverlayUnavailableError(RuntimeError):
    """The project-writable overlay is not one exact current host revision."""

    code = "environment_overlay_unavailable"


class EnvOverlayStore:
    """Persist runtime tool environment overlays inside the workspace."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        overlay_json: str = DEFAULT_OVERLAY_JSON,
        overlay_script: str = DEFAULT_OVERLAY_SCRIPT,
    ):
        self.orchestrator = orchestrator
        self.overlay_json = overlay_json
        self.overlay_script = overlay_script
        self._loaded_raw: Optional[str] = None
        self._loaded_head_sha256: str = "0" * 64
        self._loaded_authorized = False

    def inspect(self) -> dict[str, Any]:
        """Return the current overlay, recovering invalid state to an empty overlay."""
        overlay, warnings = self._load_overlay()
        result = deepcopy(overlay)
        if warnings:
            result["warnings"] = warnings
        return result

    def bootstrap_current_run(self) -> str:
        """Give a new run an honest empty overlay before project-lane I/O.

        A container can outlive the command that published its overlay.  The
        next command owns a new host publication stream, so the old JSON is
        forensic rather than current runtime authority.  Normal project-lane
        commands cannot even load context while those bytes remain unmatched.

        This controller-only bootstrap does not adopt or derive from the old
        body.  It compare-and-replaces the exact bytes with the canonical empty
        overlay and publishes that empty revision as the new run's genesis.
        Installed binaries remain in the container; only stale activation
        claims are discarded and may be re-established by normal preflight.

        Returns ``"present"`` when this run already owns a head, ``"absent"``
        when no overlay exists and no bootstrap is needed, and ``"reset"``
        after replacing a foreign-run overlay.
        """

        authority = evidence_publication_authority_for(self.orchestrator)
        head = authority.latest_head(ENV_OVERLAY_LOGICAL_ARTIFACT_ID)
        if head is not None:
            return "present"

        previous = self._read_file(self.overlay_json)
        if previous is None:
            return "absent"

        payload = self._canonical_overlay_json(self._empty_overlay())
        # Reuse the store's one production/test-double CAS boundary.  The
        # previous bytes are a compare token only; no field is parsed or
        # preserved from the foreign body.
        self._loaded_raw = previous
        if not self._compare_publish_json(payload):
            raise EnvOverlayUnavailableError(
                "new-run overlay bootstrap could not replace the foreign revision"
            )

        publication = publish_evidence_revision(
            self.orchestrator,
            record_kind="env_overlay",
            record_id=ENV_OVERLAY_RECORD_ID,
            logical_artifact_id=ENV_OVERLAY_LOGICAL_ARTIFACT_ID,
            raw=payload.encode("utf-8"),
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
        if not publication.published:
            raise EnvOverlayUnavailableError(
                "new-run empty overlay reached the container but host publication failed: "
                f"{publication.status}: {publication.detail}"
            )

        # The shell projection is forensic only, but keeping it synchronized
        # avoids showing stale activation claims to a human inspecting the
        # container after the reset.
        self._write_file(self.overlay_script, self._render_shell_script(self._empty_overlay()))
        return "reset"

    def authorized_environment(
        self,
        base_environment: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, str]:
        """Derive one runner environment from the exact host-authorized JSON head.

        The generated ``env_overlay.sh`` is deliberately not consulted.  An
        invalid, stale, deleted, mirror-only or otherwise unpublished JSON
        body makes the overlay unavailable rather than partially applying it.
        """

        overlay, warnings = self._load_overlay()
        if warnings:
            raise EnvOverlayUnavailableError("; ".join(warnings))

        resolved = {
            str(key): str(value)
            for key, value in dict(base_environment or {}).items()
            if isinstance(key, str)
        }
        resolved.setdefault("LANG", "C.UTF-8")
        resolved.setdefault("LC_ALL", "C.UTF-8")

        active_candidates: list[tuple[str, Mapping[str, Any]]] = []
        tool_names = sorted(
            overlay.get("tools", {}),
            key=lambda name: (name != "maven", name),
        )
        for tool_name in tool_names:
            entry = overlay["tools"][tool_name]
            active = entry.get("active")
            candidate = entry.get("candidates", {}).get(active) if active else None
            if not active or not isinstance(candidate, Mapping):
                continue
            active_candidates.append((active, candidate))
            for key, value in sorted(candidate.get("env", {}).items()):
                if key != "PATH":
                    resolved[key] = str(value)

        path_entries: list[str] = []
        seen: set[str] = set()
        for executable, _candidate in active_candidates:
            directory = posixpath.dirname(executable)
            if directory and directory not in seen:
                seen.add(directory)
                path_entries.append(directory)
        for _executable, candidate in active_candidates:
            for directory in candidate.get("path_prepend", []):
                if directory not in seen:
                    seen.add(directory)
                    path_entries.append(directory)
        base_path = str(resolved.get("PATH") or RUNTIME_EXEC_BASE_PATH)
        resolved["PATH"] = ":".join([*path_entries, base_path])
        return resolved

    def register(
        self,
        tool: str,
        executable: str,
        *,
        version: Optional[str] = None,
        source: str = "agent_registered",
        env: Optional[dict[str, Any]] = None,
        path_prepend: Optional[list[str] | str] = None,
        activate: bool = False,
        distribution: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Register a candidate executable and optionally make it active."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        executable_path = self._normalize_tool_executable(tool_name, executable)
        entry = self._tool_entry(overlay, tool_name)
        candidates = entry.setdefault("candidates", {})
        existing = candidates.get(executable_path, {})

        normalized_env = self._normalize_env(env if env is not None else existing.get("env", {}))
        normalized_path = self._normalize_path_prepend(
            path_prepend if path_prepend is not None else existing.get("path_prepend"),
            executable_path,
        )

        candidates[executable_path] = {
            "version": str(version) if version is not None else existing.get("version"),
            "source": source,
            "env": normalized_env,
            "path_prepend": normalized_path,
        }
        # Runtime identity belongs to the selected candidate, not to a project
        # exception. Preserve it when a preflight refreshes this same path.
        for key, value in (("distribution", distribution), ("capabilities", capabilities)):
            selected = value if value is not None else existing.get(key)
            if selected is not None:
                candidates[executable_path][key] = deepcopy(selected)

        entry.setdefault("blocked", [])
        if activate:
            self._activate_in_overlay(overlay, tool_name, executable_path)
        return self._write_overlay(overlay)

    def activate(self, tool: str, executable: str) -> dict[str, Any]:
        """Activate a registered executable for a tool."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        executable_path = self._normalize_tool_executable(tool_name, executable)
        self._activate_in_overlay(overlay, tool_name, executable_path)
        return self._write_overlay(overlay)

    def block(
        self,
        tool: str,
        executable: str,
        *,
        version: Optional[str] = None,
        requirement: Optional[str] = None,
        reason: Optional[str] = None,
        source: str = "build_error",
    ) -> dict[str, Any]:
        """Record negative evidence for one exact executable."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        executable_path = self._normalize_tool_executable(tool_name, executable)
        entry = self._tool_entry(overlay, tool_name)
        block_record = {
            "executable": executable_path,
            "version": str(version) if version is not None else None,
            "requirement": requirement,
            "reason": reason,
            "source": source,
        }

        blocked = entry.setdefault("blocked", [])
        blocked[:] = [
            item
            for item in blocked
            if not (
                item.get("executable") == executable_path
                and item.get("version") == block_record["version"]
                and item.get("requirement") == requirement
            )
        ]
        blocked.append(block_record)

        if entry.get("active") == executable_path:
            entry.pop("active", None)

        return self._write_overlay(overlay)

    def record_requirement_failure(
        self,
        tool: str,
        *,
        requirement: str,
        executable: Optional[str] = None,
        version: Optional[str] = None,
        reason: Optional[str] = None,
        source: str = "build_error",
        working_directory: Optional[str] = None,
    ) -> dict[str, Any]:
        """Atomically persist an observed constraint and exact negative evidence."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        raw_requirement = str(requirement or "").strip()
        if not raw_requirement:
            raise ValueError("requirement is required")

        entry = self._tool_entry(overlay, tool_name)
        requirement_record = {
            "raw": raw_requirement,
            "source": str(source or "build_error"),
            "working_directory": (
                str(working_directory).rstrip("/") if working_directory else None
            ),
        }
        requirements = entry.setdefault("requirements", [])
        if not any(
            item.get("raw") == requirement_record["raw"]
            and item.get("working_directory") == requirement_record["working_directory"]
            for item in requirements
        ):
            requirements.append(requirement_record)

        if executable:
            executable_path = self._normalize_tool_executable(tool_name, executable)
            block_record = {
                "executable": executable_path,
                "version": str(version) if version is not None else None,
                "requirement": raw_requirement,
                "reason": reason,
                "source": str(source or "build_error"),
            }
            blocked = entry.setdefault("blocked", [])
            blocked[:] = [
                item
                for item in blocked
                if not (
                    item.get("executable") == executable_path
                    and item.get("version") == block_record["version"]
                    and item.get("requirement") == raw_requirement
                )
            ]
            blocked.append(block_record)
            if entry.get("active") == executable_path:
                entry.pop("active", None)

        return self._write_overlay(overlay)

    def observed_requirements(
        self,
        tool: str,
        *,
        working_directory: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return applicable harness-observed constraints without weakening history."""
        overlay, _warnings = self._load_overlay()
        entry = overlay.get("tools", {}).get(self._normalize_tool(tool), {})
        records = entry.get("requirements", [])
        return [
            deepcopy(record)
            for record in records
            if self._requirement_applies(record, working_directory)
        ]

    def observed_requirement(
        self,
        tool: str,
        *,
        working_directory: Optional[str] = None,
    ) -> dict[str, Any] | None:
        """Backward-compatible first applicable harness-observed constraint."""
        requirements = self.observed_requirements(
            tool,
            working_directory=working_directory,
        )
        return requirements[0] if requirements else None

    def record_runtime_requirement(
        self,
        tool: str,
        *,
        target_sha: str,
        domain_root: str,
        required_major: str,
        source_ref: str,
        domain_id: Optional[str] = None,
        observed_sequence: Optional[int] = None,
        observed_at: Optional[str] = None,
        observed_runtime: Optional[Mapping[str, Any]] = None,
        command_key: Optional[str] = None,
        constraint: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persist one runner-observed runtime requirement at an exact scope.

        These records are deliberately separate from the generic Maven
        version constraints above.  A build-tool runtime fact is applicable
        only to the exact checkout and build domain that emitted it; parent /
        child containment is not authority to leak it into a sibling domain.
        """
        overlay, warnings = self._load_overlay()
        self._require_runtime_state_readable(warnings)
        tool_name = self._normalize_tool(tool)
        scope = self._runtime_scope(
            target_sha=target_sha,
            domain_id=domain_id,
            domain_root=domain_root,
        )
        major = self._runtime_major(required_major)
        reference = str(source_ref or "").strip()
        if not reference:
            raise ValueError("source_ref is required")
        entry = self._tool_entry(overlay, tool_name)
        records = entry.setdefault("runtime_requirements", [])

        identity = {
            **scope,
            "required_major": major,
            "source_ref": reference,
        }
        if command_key is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", command_key) or not constraint:
                raise ValueError(
                    "scoped runtime observations require a command hash and constraint"
                )
            identity.update(command_key=command_key, constraint=str(constraint))
        for existing_record in records:
            if all(existing_record.get(key) == value for key, value in identity.items()):
                # A receipt may be re-assessed after a process restart.  Its
                # observation remains one event, not a fresh latest-wins vote.
                return self._write_overlay(overlay)

        if observed_sequence is None:
            sequence = (
                max(
                    (
                        int(record.get("observed_sequence", 0))
                        for record in records
                        if isinstance(record, Mapping)
                    ),
                    default=0,
                )
                + 1
            )
        elif (
            not isinstance(observed_sequence, int)
            or isinstance(observed_sequence, bool)
            or observed_sequence < 1
        ):
            raise ValueError("observed_sequence must be a positive integer")
        else:
            sequence = observed_sequence

        timestamp = str(observed_at or "").strip() or datetime.now(timezone.utc).isoformat()
        new_record: dict[str, Any] = {
            **identity,
            "observed_sequence": sequence,
            "observed_at": timestamp,
        }
        snapshot = self._normalize_runtime_snapshot(observed_runtime)
        if snapshot:
            new_record["observed_runtime"] = snapshot
        records.append(new_record)
        return self._write_overlay(overlay)

    def confirm_runtime_requirement(
        self,
        tool: str,
        *,
        target_sha: str,
        domain_root: str,
        source_ref: str,
        active_runtime: Mapping[str, Any],
        domain_id: Optional[str] = None,
        activated_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Attach a same-environment provisioning postcondition to its fact."""
        overlay, warnings = self._load_overlay()
        self._require_runtime_state_readable(warnings)
        tool_name = self._normalize_tool(tool)
        scope = self._runtime_scope(
            target_sha=target_sha,
            domain_id=domain_id,
            domain_root=domain_root,
        )
        reference = str(source_ref or "").strip()
        snapshot = self._normalize_runtime_snapshot(active_runtime)
        if not reference:
            raise ValueError("source_ref is required")
        if not snapshot or not snapshot.get("major"):
            raise ValueError("active_runtime must state a major")
        records = overlay.get("tools", {}).get(tool_name, {}).get("runtime_requirements", [])
        matching = [
            record
            for record in records
            if self._runtime_record_matches(record, scope) and record.get("source_ref") == reference
        ]
        if not matching:
            raise ValueError("runtime requirement observation does not exist")
        record = max(matching, key=lambda item: int(item.get("observed_sequence", 0)))
        record["active_runtime"] = snapshot
        record["activated_at"] = (
            str(activated_at or "").strip() or datetime.now(timezone.utc).isoformat()
        )
        return self._write_overlay(overlay)

    def scoped_runtime_requirements(
        self,
        tool: str,
        *,
        target_sha: str,
        domain_root: str,
        domain_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return observations for one exact target/domain and no other."""
        overlay, warnings = self._load_overlay()
        self._require_runtime_state_readable(warnings)
        scope = self._runtime_scope(
            target_sha=target_sha,
            domain_id=domain_id,
            domain_root=domain_root,
        )
        records = (
            overlay.get("tools", {})
            .get(self._normalize_tool(tool), {})
            .get("runtime_requirements", [])
        )
        return [
            deepcopy(record) for record in records if self._runtime_record_matches(record, scope)
        ]

    def resolve_runtime_requirement(
        self,
        tool: str,
        *,
        target_sha: str,
        domain_root: str,
        static_major: Optional[str],
        static_source: Optional[str],
        domain_id: Optional[str] = None,
        runner_observed: Optional[Mapping[str, Any]] = None,
        command_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resolve runner > persisted dynamic > static survey authority.

        Conflicting observations at the same exact scope are returned as a
        typed conflict.  No caller can accidentally turn list order into a
        latest-wins policy.
        """
        records = self.scoped_runtime_requirements(
            tool,
            target_sha=target_sha,
            domain_id=domain_id,
            domain_root=domain_root,
        )
        if command_key is not None:
            # A runner error describes this command. A later CI step may use a
            # different JVM or distribution. Old records without command/bound
            # evidence remain readable, but cannot authorize an automatic switch.
            records = [
                r for r in records if r.get("command_key") == command_key and r.get("constraint")
            ]
        current = dict(runner_observed or {})
        if current:
            scope = self._runtime_scope(
                target_sha=target_sha,
                domain_id=domain_id,
                domain_root=domain_root,
            )
            if not self._runtime_record_matches(current, scope):
                raise ValueError("runner_observed does not match the requested runtime scope")
            current_major = self._runtime_major(current.get("required_major"))
            current["required_major"] = current_major
            if command_key is not None and current.get("command_key") != command_key:
                raise ValueError("runner_observed does not match the command")
            records_for_conflict = records + [current]
        else:
            records_for_conflict = records
        if command_key is not None and records_for_conflict:
            # Preserve all bounded statements. JdkPreflight intersects them
            # using the same range reader as declared JVM requirements.
            newest = current or max(records, key=lambda item: int(item.get("observed_sequence", 0)))
            return {
                "required_major": newest["required_major"],
                "authority": "runner_observed" if current else "persisted_dynamic",
                "provenance": newest,
                "runtime_constraints": list(
                    dict.fromkeys(r["constraint"] for r in records_for_conflict)
                ),
            }
        majors = sorted(
            {
                record.get("required_major")
                for record in records_for_conflict
                if record.get("required_major")
            },
            key=lambda value: int(str(value)),
        )
        if len(majors) > 1:
            source_refs: list[str] = []
            for record in records_for_conflict:
                reference = str(record.get("source_ref") or "").strip()
                if reference and reference not in source_refs:
                    source_refs.append(reference)
            return {
                "conflict": {
                    "typed_code": RUNTIME_REQUIREMENT_CONFLICT,
                    "required_majors": majors,
                    "source_refs": source_refs,
                }
            }
        if current:
            return {
                "required_major": current["required_major"],
                "authority": "runner_observed",
                "provenance": current,
            }
        if records:
            newest = max(records, key=lambda item: int(item.get("observed_sequence", 0)))
            return {
                "required_major": newest["required_major"],
                "authority": "persisted_dynamic",
                "provenance": newest,
            }
        static = str(static_major or "").strip()
        if static:
            return {
                "required_major": self._runtime_major(static),
                "authority": "static_survey",
                "provenance": {"source": str(static_source or "unknown")},
            }
        return {}

    def clear(self, tool: Optional[str] = None) -> dict[str, Any]:
        """Clear the whole overlay or one tool entry."""
        overlay, _warnings = self._load_overlay()
        if tool is None:
            overlay = self._empty_overlay()
        else:
            overlay.setdefault("tools", {}).pop(self._normalize_tool(tool), None)
        return self._write_overlay(overlay)

    def active_candidate(
        self,
        tool: str,
        *,
        overlay: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any] | None:
        """Return the active candidate metadata for a tool, including its executable."""
        if overlay is None:
            overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        entry = overlay.get("tools", {}).get(tool_name, {})
        active = entry.get("active")
        if not active:
            return None
        candidate = entry.get("candidates", {}).get(active)
        if not candidate:
            return None
        return {"executable": active, **deepcopy(candidate)}

    def is_blocked(
        self,
        tool: str,
        executable: str,
        version: Optional[str] = None,
        requirement: Optional[str] = None,
    ) -> bool:
        """Return whether the exact executable has matching negative evidence."""
        overlay, _warnings = self._load_overlay()
        return self._is_blocked_in_overlay(
            overlay,
            self._normalize_tool(tool),
            self._normalize_tool_executable(self._normalize_tool(tool), executable),
            version=version,
            requirement=requirement,
        )

    def _load_overlay(self) -> tuple[dict[str, Any], list[str]]:
        self._loaded_raw = None
        self._loaded_head_sha256 = "0" * 64
        self._loaded_authorized = False
        try:
            raw = self._read_file(self.overlay_json)
            authority = evidence_publication_authority_for(self.orchestrator)
            observed: dict[str, MutablePublicationObservation] = {}
            normalized: dict[str, Any] | None = None
            if raw is not None:
                if len(raw.encode("utf-8")) > ENV_OVERLAY_MAX_BYTES:
                    raise ValueError("env overlay exceeds its raw byte limit")
                loaded = self._strict_json_object(raw)
                normalized = self._validate_overlay_payload(loaded, ENV_OVERLAY_RECORD_ID)
                canonical = self._canonical_overlay_json(normalized)
                if raw != canonical:
                    raise ValueError("env overlay bytes are not canonical")
                observed[ENV_OVERLAY_RECORD_ID] = MutablePublicationObservation(
                    logical_artifact_id=ENV_OVERLAY_LOGICAL_ARTIFACT_ID,
                    raw_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    byte_count=len(raw.encode("utf-8")),
                    run_id=str(getattr(authority, "run_id", "") or "unavailable"),
                )
            check = authority.verify_latest_record_set("env_overlay", observed)
            if not check.authorized:
                raise EnvOverlayUnavailableError(
                    check.detail or f"host publication check was {check.status}"
                )
            self._loaded_raw = raw
            self._loaded_head_sha256 = latest_publication_raw_sha256(
                self.orchestrator,
                ENV_OVERLAY_LOGICAL_ARTIFACT_ID,
            )
            self._loaded_authorized = True
            return deepcopy(normalized or self._empty_overlay()), []
        except Exception as exc:
            return self._empty_overlay(), [f"Env overlay unavailable at {self.overlay_json}: {exc}"]

    def _write_overlay(self, overlay: dict[str, Any]) -> dict[str, Any]:
        if not self._loaded_authorized:
            raise EnvOverlayUnavailableError(
                "env overlay update requires one exact current host-published predecessor"
            )
        normalized = self._validate_overlay_payload(overlay, ENV_OVERLAY_RECORD_ID)
        payload = self._canonical_overlay_json(normalized)
        script = self._render_shell_script(normalized)

        if payload == self._loaded_raw:
            return deepcopy(normalized)

        self._ensure_overlay_dir()
        previous = {
            self.overlay_script: self._read_file(self.overlay_script),
        }

        # The shell file is now forensic/readback only.  JSON is the sole
        # runtime input, and exact host publication is its commit authority.
        try:
            self._write_file(self.overlay_script, script)
            persisted = self._compare_publish_json(payload)
            if not persisted:
                raise RuntimeError("env overlay container compare-and-publish failed")
            observed_json = self._read_file(self.overlay_json)
            observed_script = self._read_file(self.overlay_script)
            if observed_json != payload or observed_script != script:
                raise RuntimeError("env overlay JSON/shell readback did not match pending bytes")
        except Exception as exc:
            rollback_errors = self._restore_persisted_pair(previous)
            if rollback_errors:
                raise RuntimeError(
                    "Env overlay container transaction failed and rollback was incomplete: "
                    f"{exc}; rollback errors: {'; '.join(rollback_errors)}"
                ) from exc
            raise RuntimeError(
                f"Env overlay container transaction failed; forensic shell restored: {exc}"
            ) from exc

        publication = publish_evidence_revision(
            self.orchestrator,
            record_kind="env_overlay",
            record_id=ENV_OVERLAY_RECORD_ID,
            logical_artifact_id=ENV_OVERLAY_LOGICAL_ARTIFACT_ID,
            raw=payload.encode("utf-8"),
            expected_previous_raw_sha256=self._loaded_head_sha256,
        )
        if not publication.published:
            # Do not treat container rollback as host publication.  These exact
            # bytes may remain for forensics, but every runner read will reject
            # them because the mutable host head did not advance.
            self._loaded_authorized = False
            raise EnvOverlayUnavailableError(
                f"env overlay host publication failed: {publication.status}: {publication.detail}"
            )
        assert publication.publication is not None
        self._loaded_raw = payload
        self._loaded_head_sha256 = publication.publication.raw_sha256
        self._loaded_authorized = True
        return deepcopy(normalized)

    def _compare_publish_json(self, payload: str) -> bool:
        files = getattr(self.orchestrator, "files", None)
        writer = getattr(self.orchestrator, "write_file", None)
        # Explicit in-memory unit-double transport. Production always uses the
        # bounded clean control CAS helper below.
        if isinstance(files, dict) and not callable(
            getattr(self.orchestrator, "execute_control_command", None)
        ):
            if files.get(self.overlay_json) != self._loaded_raw:
                return False
            if callable(writer):
                result = writer(self.overlay_json, payload)
                return not (
                    isinstance(result, Mapping)
                    and (result.get("success") is False or result.get("exit_code", 0) != 0)
                )
            self._write_file(self.overlay_json, payload)
            return files.get(self.overlay_json) == payload
        result = compare_publish_container_text_atomic(
            self.orchestrator,
            self.overlay_json,
            payload,
            expected_content=self._loaded_raw,
            validate_json=True,
        )
        return result.persisted

    def _restore_persisted_pair(self, previous: dict[str, Optional[str]]) -> list[str]:
        errors: list[str] = []
        for path in (self.overlay_script,):
            try:
                old_content = previous[path]
                if old_content is None:
                    self._remove_file(path)
                else:
                    self._write_file(path, old_content)
            except Exception as rollback_exc:
                errors.append(f"{path}: {rollback_exc}")
        return errors

    @staticmethod
    def _strict_json_object(raw: str) -> dict[str, Any]:
        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, child in pairs:
                if key in value:
                    raise ValueError(f"duplicate JSON key: {key!r}")
                value[key] = child
            return value

        loaded = json.loads(raw, object_pairs_hook=reject_duplicates)
        if not isinstance(loaded, dict):
            raise ValueError("env overlay must be a JSON object")
        return loaded

    def _validate_overlay_payload(
        self,
        overlay: Mapping[str, Any],
        expected_id: str,
    ) -> dict[str, Any]:
        if expected_id != ENV_OVERLAY_RECORD_ID:
            raise ValueError("env overlay record identity is invalid")
        if not isinstance(overlay, Mapping) or set(overlay) != {"version", "tools"}:
            raise ValueError("env overlay top-level schema is not closed")
        if type(overlay.get("version")) is not int or overlay.get("version") != 1:
            raise ValueError("env overlay version must be strict v1")
        tools = overlay.get("tools")
        if not isinstance(tools, dict) or len(tools) > 64:
            raise ValueError("env overlay tools must be a bounded object")
        normalized = self._normalize_overlay(deepcopy(dict(overlay)))
        if normalized != dict(overlay):
            raise ValueError("env overlay contains noncanonical or unknown fields")
        for tool_name, entry in normalized["tools"].items():
            if _TOOL_NAME_RE.fullmatch(tool_name) is None:
                raise ValueError("env overlay tool name is invalid")
            candidates = entry.get("candidates", {})
            if len(candidates) > 256 or len(entry.get("blocked", [])) > 1024:
                raise ValueError("env overlay tool collections exceed their limits")
            if (
                len(entry.get("requirements", [])) > 1024
                or len(entry.get("runtime_requirements", [])) > 1024
            ):
                raise ValueError("env overlay observation collections exceed their limits")
            active = entry.get("active")
            if active is not None and active not in candidates:
                raise ValueError("env overlay active executable is not a candidate")
            for executable, candidate in candidates.items():
                self._require_strict_absolute_path(executable, "candidate executable")
                required_fields = {"version", "source", "env", "path_prepend"}
                optional_fields = {"distribution", "capabilities"}
                if not required_fields <= set(candidate) <= required_fields | optional_fields:
                    raise ValueError("env overlay candidate schema is not closed")
                if candidate["version"] is not None and not isinstance(candidate["version"], str):
                    raise ValueError("env overlay candidate version must be a string")
                if not isinstance(candidate["source"], str):
                    raise ValueError("env overlay candidate source must be a string")
                if (
                    len(candidate.get("env", {})) > 64
                    or len(candidate.get("path_prepend", [])) > 64
                ):
                    raise ValueError("env overlay candidate environment exceeds its limits")
                for path in candidate.get("path_prepend", []):
                    self._require_strict_absolute_path(path, "PATH entry")
            for block in entry.get("blocked", []):
                if set(block) != {
                    "executable",
                    "version",
                    "requirement",
                    "reason",
                    "source",
                }:
                    raise ValueError("env overlay blocked record schema is not closed")
                self._require_strict_absolute_path(block["executable"], "blocked executable")
                if any(
                    block[name] is not None and not isinstance(block[name], str)
                    for name in ("version", "requirement", "reason")
                ) or not isinstance(block["source"], str):
                    raise ValueError("env overlay blocked record has invalid scalar types")
            for requirement in entry.get("requirements", []):
                if set(requirement) != {"raw", "source", "working_directory"}:
                    raise ValueError("env overlay requirement schema is not closed")
                if not isinstance(requirement["raw"], str) or not isinstance(
                    requirement["source"], str
                ):
                    raise ValueError("env overlay requirement has invalid scalar types")
                working_directory = requirement["working_directory"]
                if working_directory is not None:
                    self._require_strict_absolute_path(
                        working_directory,
                        "requirement working directory",
                    )
            self._require_bounded_strings(entry)
        encoded = self._canonical_overlay_json(normalized).encode("utf-8")
        if len(encoded) > ENV_OVERLAY_MAX_BYTES:
            raise ValueError("env overlay exceeds its canonical byte limit")
        return normalized

    @staticmethod
    def _canonical_overlay_json(overlay: Mapping[str, Any]) -> str:
        return json.dumps(dict(overlay), indent=2, sort_keys=True)

    @staticmethod
    def _require_strict_absolute_path(value: Any, label: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 4096
            or not posixpath.isabs(value)
            or posixpath.normpath(value) != value
            or any(character in value for character in ("\x00", "\n", "\r", ":"))
        ):
            raise ValueError(f"env overlay {label} must be one normalized absolute path")

    @classmethod
    def _require_bounded_strings(cls, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 4096:
                    raise ValueError("env overlay object key exceeds its bound")
                cls._require_bounded_strings(child)
        elif isinstance(value, list):
            for child in value:
                cls._require_bounded_strings(child)
        elif isinstance(value, str) and len(value.encode("utf-8")) > 64 * 1024:
            raise ValueError("env overlay string exceeds its bound")
        elif value is not None and not isinstance(value, (str, int, bool)):
            raise ValueError("env overlay contains an unsupported scalar type")

    def _render_shell_script(self, overlay: dict[str, Any]) -> str:
        """Render the legacy forensic mirror; no runner sources these bytes."""

        lines = ["# Generated by Setup-Agent env overlay."]
        path_entries: list[str] = []
        seen_paths: set[str] = set()
        active_candidates: list[tuple[str, dict[str, Any]]] = []

        # Maven is the public weak-model recovery surface.  Its exact
        # executable directory must win even when another active tool or one of
        # its caller-supplied prefixes would sort ahead of it.
        tool_names = sorted(
            overlay.get("tools", {}),
            key=lambda name: (name != "maven", name),
        )
        for tool_name in tool_names:
            tool_entry = overlay["tools"][tool_name]
            active = tool_entry.get("active")
            if not active:
                continue
            candidate = tool_entry.get("candidates", {}).get(active)
            if not candidate:
                continue
            active_candidates.append((active, candidate))

            for key, value in sorted(candidate.get("env", {}).items()):
                lines.append(f"export {key}={shlex.quote(str(value))}")

        # Exact executable directories precede every optional caller prefix.
        # With Maven ordered first above, bare ``mvn`` and BuildTool's exact
        # resolver observe the same active runtime across working directories.
        for active, _candidate in active_candidates:
            directory = posixpath.dirname(active)
            if directory and directory != "." and directory not in seen_paths:
                seen_paths.add(directory)
                path_entries.append(directory)

        for _active, candidate in active_candidates:
            for path in candidate.get("path_prepend", []):
                if path not in seen_paths:
                    seen_paths.add(path)
                    path_entries.append(path)

        if path_entries:
            quoted_prefix = ":".join(shlex.quote(path) for path in path_entries)
            lines.append(f"export PATH={quoted_prefix}:$PATH")

        return "\n".join(lines) + "\n"

    def _read_file(self, path: str) -> Optional[str]:
        # Exact transport preserves the terminal LF in the generated shell
        # script. DockerOrchestrator's ordinary text output calls `.strip()`,
        # which made every real overlay transaction fail its byte-for-byte
        # readback even though the persisted files were correct.
        return read_container_text(self.orchestrator, path, exact_bytes=True)

    def _write_file(self, path: str, content: str) -> None:
        if hasattr(self.orchestrator, "write_file"):
            result = self.orchestrator.write_file(path, content)
            if isinstance(result, dict) and (
                result.get("success") is False or result.get("exit_code", 0) != 0
            ):
                raise RuntimeError(f"Failed to write {path}: {result.get('output', '')}")
            return

        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        command = f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)}"
        execute = resolve_control_execute(self.orchestrator)
        if execute is None:
            raise RuntimeError("env overlay transport has no clean executor")
        result = execute(command)
        if result.get("exit_code", 0) != 0 or result.get("success") is False:
            raise RuntimeError(f"Failed to write {path}: {result.get('output', '')}")

    def _remove_file(self, path: str) -> None:
        files = getattr(self.orchestrator, "files", None)
        if isinstance(files, dict):
            files.pop(path, None)
            return
        execute = resolve_control_execute(self.orchestrator)
        if execute is None:
            raise RuntimeError("env overlay transport has no clean executor")
        result = execute(f"rm -f {shlex.quote(path)}")
        if result.get("exit_code", 0) != 0 or result.get("success") is False:
            raise RuntimeError(f"Failed to remove {path}: {result.get('output', '')}")

    def _ensure_overlay_dir(self) -> None:
        directory = posixpath.dirname(self.overlay_json)
        execute = resolve_control_execute(self.orchestrator)
        if execute is None:
            raise RuntimeError("env overlay transport has no clean executor")
        result = execute(f"mkdir -p {shlex.quote(directory)}")
        if result.get("exit_code", 0) != 0 or result.get("success") is False:
            raise RuntimeError(f"Failed to create {directory}: {result.get('output', '')}")

    def _normalize_overlay(
        self,
        overlay: dict[str, Any],
        *,
        tolerant: bool = False,
        warnings: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        normalized = self._empty_overlay()
        canonical_tool_entries: dict[str, bool] = {}
        tools = overlay.get("tools", {})
        if not isinstance(tools, dict):
            return normalized

        for raw_tool_name, raw_entry in tools.items():
            if not isinstance(raw_tool_name, str) or not isinstance(raw_entry, dict):
                continue
            try:
                tool_name = self._normalize_tool(raw_tool_name)
            except ValueError as exc:
                if tolerant:
                    self._append_warning(
                        warnings,
                        f"Ignored invalid env overlay tool entry: {exc}",
                    )
                    continue
                raise
            entry: dict[str, Any] = {"candidates": {}, "blocked": []}

            candidates = raw_entry.get("candidates", {})
            if isinstance(candidates, dict):
                for raw_executable, raw_candidate in candidates.items():
                    try:
                        if not isinstance(raw_executable, str) or not raw_executable.strip():
                            continue
                        candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
                        executable = self._normalize_tool_executable(
                            tool_name,
                            raw_executable,
                        )
                        raw_env = candidate.get("env") if candidate.get("env") is not None else {}
                        if not isinstance(raw_env, dict):
                            raise ValueError("env must be an object")
                        entry["candidates"][executable] = {
                            "version": (
                                str(candidate.get("version"))
                                if candidate.get("version") is not None
                                else None
                            ),
                            "source": str(candidate.get("source") or "agent_registered"),
                            "env": self._normalize_env(raw_env),
                            "path_prepend": self._normalize_path_prepend(
                                candidate.get("path_prepend"),
                                executable,
                            ),
                        }
                        if candidate.get("distribution") is not None:
                            distribution = candidate["distribution"]
                            if not isinstance(distribution, str) or not re.fullmatch(
                                r"[a-z][a-z0-9._-]{0,63}", distribution
                            ):
                                raise ValueError("invalid runtime distribution")
                            entry["candidates"][executable]["distribution"] = distribution
                        if candidate.get("capabilities") is not None:
                            capabilities = candidate["capabilities"]
                            if (
                                not isinstance(capabilities, list)
                                or len(capabilities) > 32
                                or any(
                                    not isinstance(item, str)
                                    or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", item)
                                    for item in capabilities
                                )
                            ):
                                raise ValueError("invalid runtime capabilities")
                            entry["candidates"][executable]["capabilities"] = sorted(
                                set(capabilities)
                            )
                    except ValueError as exc:
                        if tolerant:
                            self._append_warning(
                                warnings,
                                (
                                    "Ignored invalid env overlay candidate for "
                                    f"{tool_name}: {exc}"
                                ),
                            )
                            continue
                        raise

            active = raw_entry.get("active")
            if isinstance(active, str) and active in entry["candidates"]:
                entry["active"] = active

            blocked = raw_entry.get("blocked", [])
            if isinstance(blocked, list):
                for raw_block in blocked:
                    if not isinstance(raw_block, dict) or not raw_block.get("executable"):
                        continue
                    try:
                        entry["blocked"].append(
                            {
                                "executable": self._normalize_tool_executable(
                                    tool_name,
                                    raw_block["executable"],
                                ),
                                "version": (
                                    str(raw_block.get("version"))
                                    if raw_block.get("version") is not None
                                    else None
                                ),
                                "requirement": raw_block.get("requirement"),
                                "reason": raw_block.get("reason"),
                                "source": str(raw_block.get("source") or "build_error"),
                            }
                        )
                    except ValueError as exc:
                        if tolerant:
                            self._append_warning(
                                warnings,
                                f"Ignored invalid env overlay block for {tool_name}: {exc}",
                            )
                            continue
                        raise

            raw_requirements = raw_entry.get("requirements")
            if not isinstance(raw_requirements, list):
                legacy_requirement = raw_entry.get("requirement")
                raw_requirements = (
                    [legacy_requirement] if isinstance(legacy_requirement, dict) else []
                )
            normalized_requirements = []
            for raw_requirement in raw_requirements:
                if not isinstance(raw_requirement, dict):
                    continue
                requirement_value = raw_requirement.get("raw")
                if not isinstance(requirement_value, str) or not requirement_value.strip():
                    continue
                normalized_record = {
                    "raw": requirement_value.strip(),
                    "source": str(raw_requirement.get("source") or "build_error"),
                    "working_directory": (
                        str(raw_requirement["working_directory"]).rstrip("/")
                        if raw_requirement.get("working_directory")
                        else None
                    ),
                }
                if normalized_record not in normalized_requirements:
                    normalized_requirements.append(normalized_record)
            if normalized_requirements:
                entry["requirements"] = normalized_requirements

            normalized_runtime_requirements = []
            for raw_runtime in raw_entry.get("runtime_requirements") or []:
                if not isinstance(raw_runtime, Mapping):
                    continue
                try:
                    scope = self._runtime_scope(
                        target_sha=raw_runtime.get("target_sha"),
                        domain_id=raw_runtime.get("domain_id"),
                        domain_root=raw_runtime.get("domain_root"),
                    )
                    sequence = raw_runtime.get("observed_sequence")
                    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                        raise ValueError("observed_sequence must be a positive integer")
                    observed_at = str(raw_runtime.get("observed_at") or "").strip()
                    source_ref = str(raw_runtime.get("source_ref") or "").strip()
                    if not observed_at or not source_ref:
                        raise ValueError("runtime observation time and source_ref are required")
                    record = {
                        **scope,
                        "required_major": self._runtime_major(raw_runtime.get("required_major")),
                        "source_ref": source_ref,
                        "observed_sequence": sequence,
                        "observed_at": observed_at,
                    }
                    if raw_runtime.get("command_key") is not None:
                        command_key = raw_runtime["command_key"]
                        constraint = raw_runtime.get("constraint")
                        if (
                            not isinstance(command_key, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", command_key)
                            or not isinstance(constraint, str)
                            or not constraint
                        ):
                            raise ValueError("invalid command-scoped runtime constraint")
                        record.update(command_key=command_key, constraint=constraint)
                    for key in ("observed_runtime", "active_runtime"):
                        snapshot = self._normalize_runtime_snapshot(raw_runtime.get(key))
                        if snapshot:
                            record[key] = snapshot
                    activated_at = str(raw_runtime.get("activated_at") or "").strip()
                    if activated_at and "active_runtime" in record:
                        record["activated_at"] = activated_at
                    if record not in normalized_runtime_requirements:
                        normalized_runtime_requirements.append(record)
                except ValueError as exc:
                    if tolerant:
                        self._append_warning(
                            warnings,
                            f"Ignored invalid runtime requirement for {tool_name}: {exc}",
                        )
                        continue
                    raise
            if normalized_runtime_requirements:
                entry["runtime_requirements"] = normalized_runtime_requirements

            incoming_is_canonical = raw_tool_name.strip().lower() == tool_name
            existing = normalized["tools"].get(tool_name)
            if existing is None:
                normalized["tools"][tool_name] = entry
                canonical_tool_entries[tool_name] = incoming_is_canonical
                continue

            existing_is_canonical = canonical_tool_entries.get(tool_name, False)
            for executable, candidate in entry["candidates"].items():
                if executable not in existing["candidates"] or (
                    incoming_is_canonical and not existing_is_canonical
                ):
                    existing["candidates"][executable] = candidate

            for block in entry.get("blocked", []):
                if block not in existing["blocked"]:
                    existing["blocked"].append(block)

            existing_requirements = existing.setdefault("requirements", [])
            for requirement in entry.get("requirements", []):
                if requirement not in existing_requirements:
                    existing_requirements.append(requirement)
            if not existing_requirements:
                existing.pop("requirements", None)

            existing_runtime = existing.setdefault("runtime_requirements", [])
            for record in entry.get("runtime_requirements", []):
                if record not in existing_runtime:
                    existing_runtime.append(record)
            if not existing_runtime:
                existing.pop("runtime_requirements", None)

            # A persisted canonical key is authoritative over the legacy
            # ``mvn`` alias, including an intentional lack of activation.  An
            # alias-only v1 overlay remains readable and is normalized to one
            # ``maven`` key without inventing a second active candidate.
            if incoming_is_canonical:
                if "active" in entry:
                    existing["active"] = entry["active"]
                else:
                    existing.pop("active", None)
            elif not existing_is_canonical and "active" in entry:
                existing["active"] = entry["active"]
            canonical_tool_entries[tool_name] = existing_is_canonical or incoming_is_canonical

        return normalized

    def _append_warning(self, warnings: Optional[list[str]], message: str) -> None:
        if warnings is not None:
            warnings.append(message)

    def _requirement_applies(
        self,
        record: dict[str, Any],
        working_directory: Optional[str],
    ) -> bool:
        if working_directory is None:
            return True
        raw_scope = str(record.get("working_directory") or "").strip()
        raw_requested = str(working_directory or "").strip()
        scope = posixpath.normpath(raw_scope) if raw_scope else ""
        if not scope:
            return True
        if not raw_requested:
            return True
        requested = posixpath.normpath(raw_requested)
        # A direct module retry inherits its own constraint, descendants inherit
        # it, and a parent reactor must intersect every constrained module it
        # contains.  A true sibling/disjoint project remains isolated.
        return (
            requested == scope
            or requested.startswith(scope.rstrip("/") + "/")
            or scope.startswith(requested.rstrip("/") + "/")
        )

    def _runtime_scope(
        self,
        *,
        target_sha: Any,
        domain_id: Any,
        domain_root: Any,
    ) -> dict[str, str]:
        sha = str(target_sha or "").strip()
        root = str(domain_root or "").strip()
        if not _TARGET_SHA_RE.fullmatch(sha):
            raise ValueError("target_sha must be a git object id")
        if not root or not root.startswith("/"):
            raise ValueError("domain_root must be an absolute path")
        scope = {
            "target_sha": sha.lower(),
            "domain_root": posixpath.normpath(root).rstrip("/") or "/",
        }
        identifier = str(domain_id or "").strip()
        if identifier:
            scope["domain_id"] = identifier
        return scope

    @staticmethod
    def _require_runtime_state_readable(warnings: list[str]) -> None:
        if warnings:
            raise RuntimeError("dynamic runtime state is not trustworthy: " + "; ".join(warnings))

    @staticmethod
    def _runtime_record_matches(record: Mapping[str, Any], scope: Mapping[str, str]) -> bool:
        return all(record.get(key) == value for key, value in scope.items()) and (
            ("domain_id" in record) == ("domain_id" in scope)
        )

    @staticmethod
    def _runtime_major(value: Any) -> str:
        match = _RUNTIME_MAJOR_RE.fullmatch(str(value or "").strip())
        if not match:
            raise ValueError("runtime major must be a positive Java major")
        major = str(int(match.group(1)))
        if int(major) < 1:
            raise ValueError("runtime major must be a positive Java major")
        return major

    def _normalize_runtime_snapshot(
        self,
        snapshot: Optional[Mapping[str, Any]],
    ) -> dict[str, str]:
        if not isinstance(snapshot, Mapping):
            return {}
        normalized: dict[str, str] = {}
        if snapshot.get("major") is not None:
            normalized["major"] = self._runtime_major(snapshot.get("major"))
        executable = str(snapshot.get("executable") or "").strip()
        if executable:
            normalized["executable"] = executable
        version = str(snapshot.get("version") or "").strip()
        if version:
            normalized["version"] = version
        if snapshot.get("distribution"):
            normalized["distribution"] = str(snapshot["distribution"])
        return normalized

    def _empty_overlay(self) -> dict[str, Any]:
        return {"version": 1, "tools": {}}

    def _tool_entry(self, overlay: dict[str, Any], tool: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            overlay.setdefault("tools", {}).setdefault(
                tool,
                {"candidates": {}, "blocked": []},
            ),
        )

    def _is_blocked_in_overlay(
        self,
        overlay: dict[str, Any],
        tool: str,
        executable: str,
        *,
        version: Optional[str] = None,
        requirement: Optional[str] = None,
    ) -> bool:
        for block in overlay.get("tools", {}).get(tool, {}).get("blocked", []):
            if block.get("executable") != executable:
                continue
            if version is not None and block.get("version") not in (None, str(version)):
                continue
            if requirement is not None and block.get("requirement") not in (None, requirement):
                continue
            return True
        return False

    def _activate_in_overlay(
        self,
        overlay: dict[str, Any],
        tool: str,
        executable: str,
    ) -> None:
        entry = overlay.get("tools", {}).get(tool)
        if not entry or executable not in entry.get("candidates", {}):
            raise ValueError(f"{executable} is not registered for {tool}")
        if self._is_blocked_in_overlay(overlay, tool, executable):
            raise ValueError(f"{executable} is blocked for {tool}")
        entry["active"] = executable

    def _normalize_tool(self, tool: str) -> str:
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool is required")
        normalized = tool.strip().lower()
        return _TOOL_ALIASES.get(normalized, normalized)

    def _normalize_executable(self, executable: str) -> str:
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("executable is required")
        return executable.strip()

    def _normalize_tool_executable(self, tool: str, executable: str) -> str:
        normalized = self._normalize_executable(executable)
        if self._normalize_tool(tool) == "maven" and not posixpath.isabs(normalized):
            raise ValueError("Maven executable must be an absolute container path")
        return normalized

    def _normalize_env(self, env: dict[str, Any]) -> dict[str, str]:
        if not isinstance(env, dict):
            raise ValueError("env must be an object")
        normalized: dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.match(key):
                raise ValueError(f"Invalid env key: {key!r}")
            if key in _DENIED_OVERLAY_ENV:
                raise ValueError(f"Env key must not alter shell control state: {key}")
            normalized[key] = str(value)
        return normalized

    def _normalize_path_prepend(
        self,
        path_prepend: Optional[list[str] | str],
        executable: str,
    ) -> list[str]:
        executable_directory = posixpath.dirname(executable)
        normalized: list[str] = []
        if executable_directory and executable_directory != ".":
            normalized.append(executable_directory)

        if path_prepend is None:
            return normalized
        if isinstance(path_prepend, str):
            entries = [path_prepend]
        elif isinstance(path_prepend, list):
            entries = path_prepend
        else:
            raise ValueError("path_prepend must be a string or list of strings")

        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("path_prepend entries must be non-empty strings")
            cleaned = entry.strip()
            if cleaned not in normalized:
                normalized.append(cleaned)
        return normalized
