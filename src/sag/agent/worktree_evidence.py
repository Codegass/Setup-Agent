"""Host-retained worktree boundary evidence, independent of task verdicts.

This module uses only the standard library so the portable recorder can use the
same byte-preserving probe and persistence contract. Snapshots are observations,
not proof that transient changes never happened between the boundaries.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import posixpath
import shlex
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_LOG = logging.getLogger(__name__)
_BOUNDARIES = {"task_start", "acceptance_before", "acceptance_after", "evidence_close"}
_PROBES = {
    "head": ["rev-parse", "HEAD"],
    "tracked": ["diff", "--name-only", "-z", "HEAD", "--"],
    "untracked": ["ls-files", "--others", "--exclude-standard", "-z"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Runs under the clean container-control environment. JSON/base64 retains NULs,
# trailing newlines and arbitrary filename bytes through presentation stripping.
_PROBE_PROGRAM = """import base64,json,subprocess,sys,datetime
out={}
for name,args in json.loads(sys.argv[2]).items():
 argv=['git','-C',sys.argv[1]]+args
 row={'argv':argv,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
 try:
  p=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,check=False)
  row.update(exit_code=p.returncode,stdout=base64.b64encode(p.stdout).decode(),stderr=base64.b64encode(p.stderr).decode())
 except Exception as e:
  row.update(exit_code=None,error=type(e).__name__)
 row['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 out[name]=row
print(json.dumps(out,sort_keys=True))
"""


def probe_worktree(project_root: str) -> dict[str, Any]:
    """Collect the same three probes locally, without creating checkout files."""
    result = {}
    for name, args in _PROBES.items():
        argv = ["git", "-C", project_root, *args]
        row = {"argv": argv, "started_at": _now()}
        try:
            completed = subprocess.run(argv, capture_output=True, timeout=30, check=False)
            row.update(
                exit_code=completed.returncode,
                stdout=base64.b64encode(completed.stdout).decode("ascii"),
                stderr=base64.b64encode(completed.stderr).decode("ascii"),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            row.update(exit_code=None, error=type(exc).__name__)
        row["finished_at"] = _now()
        result[name] = row
    return result


class WorktreeEvidenceRecorder:
    """Record immutable, run-bound observations outside the tested checkout."""

    def __init__(
        self,
        session_dir: str | Path,
        *,
        run_id: str,
        project_root: str,
        execute: Callable[..., Any] | None = None,
        task_definition: Mapping[str, Any] | None = None,
    ):
        if not run_id or not project_root:
            raise ValueError("worktree evidence requires run and project identity")
        self.directory = Path(session_dir).resolve() / "worktree-evidence"
        if self.directory.is_relative_to(Path(project_root).resolve()):
            raise ValueError("worktree evidence must be stored outside the tested checkout")
        self.run_id = run_id
        self.project_root = project_root
        self.execute = execute
        self.task_definition = dict(task_definition or {})
        self._snapshots: dict[tuple[str, str | None], dict[str, Any]] = {}
        self._accepted_contracts: set[str] = set()
        self._lock = threading.RLock()

    def _probe(self) -> Mapping[str, Any]:
        if self.execute is None:
            return probe_worktree(self.project_root)
        command = shlex.join(
            [
                "python3",
                "-c",
                _PROBE_PROGRAM,
                self.project_root,
                json.dumps(_PROBES, sort_keys=True),
            ]
        )
        result = self.execute(command, truncate_output=False, timeout=100)
        if (
            not isinstance(result, Mapping)
            or result.get("success") is False
            or type(result.get("exit_code")) is not int
            or result["exit_code"] != 0
            or result.get("dispatch_status")
        ):
            raise ValueError("worktree probe transport failed")
        payload = json.loads(result.get("output", ""))
        if not isinstance(payload, dict) or set(payload) != set(_PROBES):
            raise ValueError("worktree probe transport incomplete")
        return payload

    def capture(
        self, boundary: str, *, invocation_id: str | None = None, receipt_id: str | None = None
    ) -> dict[str, Any]:
        """Capture once per boundary/invocation; failed reads remain unavailable.

        ``acceptance_after`` is observed at receipt publication. In detached
        execution that can be later than process termination; no isolation or
        write-origin claim follows from that snapshot.
        """
        if boundary not in _BOUNDARIES:
            raise ValueError("unknown worktree boundary")
        if boundary.startswith("acceptance_") and not invocation_id:
            raise ValueError("acceptance boundary requires invocation identity")
        key = boundary, invocation_id
        with self._lock:
            if key in self._snapshots:
                return self._snapshots[key]
            snapshot_id = f"wt-{uuid.uuid4().hex}"
            row: dict[str, Any] = {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "run_id": self.run_id,
                "project_root": self.project_root,
                "boundary": boundary,
                "invocation_id": invocation_id,
                "receipt_id": receipt_id,
                "observed_at": _now(),
                "observation_source": (
                    "receipt_publication" if boundary == "acceptance_after" else boundary
                ),
                "head": None,
                "tracked_clean": None,
                "tracked_paths": None,
                "untracked_paths": None,
                "probes": {},
            }
            try:
                probes = self._probe()
            except Exception as exc:
                probes = {}
                row["transport_error"] = type(exc).__name__
            self.directory.mkdir(parents=True, exist_ok=True)
            for name, args in _PROBES.items():
                probe = probes.get(name)
                saved: dict[str, Any] = {
                    "argv": ["git", "-C", self.project_root, *args],
                    "exit_code": None,
                    "status": "unavailable",
                }
                raw = None
                if isinstance(probe, Mapping) and probe.get("argv") == saved["argv"]:
                    try:
                        code = probe.get("exit_code")
                        saved.update(
                            started_at=probe.get("started_at"),
                            finished_at=probe.get("finished_at"),
                            exit_code=code if type(code) is int else None,
                        )
                        for stream in ("stdout", "stderr"):
                            data = base64.b64decode(probe[stream], validate=True)
                            filename = f"{snapshot_id}.{name}.{stream}"
                            (self.directory / filename).write_bytes(data)
                            saved[stream] = {
                                "path": f"worktree-evidence/{filename}",
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "bytes": len(data),
                            }
                            if stream == "stdout":
                                raw = data
                        saved["status"] = "observed" if type(code) is int else "unavailable"
                        if code == 0 and type(code) is int:
                            if name == "head":
                                head = raw.decode("ascii").strip()
                                if len(head) != 40 or any(
                                    c not in "0123456789abcdef" for c in head
                                ):
                                    raise ValueError("invalid HEAD")
                                row["head"] = head
                            else:
                                if raw and not raw.endswith(b"\x00"):
                                    raise ValueError("incomplete NUL path listing")
                                paths = [
                                    p.decode("utf-8", errors="surrogateescape")
                                    for p in raw.split(b"\x00")
                                    if p
                                ]
                                row[f"{name}_paths"] = paths
                                if name == "tracked":
                                    row["tracked_clean"] = not paths
                    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                        saved["status"] = "unavailable"
                        saved["error"] = type(exc).__name__
                row["probes"][name] = saved
            row["status"] = (
                "observed"
                if all(
                    p["status"] == "observed" and p["exit_code"] == 0
                    for p in row["probes"].values()
                )
                else "unavailable"
            )
            # The record only names raw bytes already on disk. Unique names
            # and atomic rename prevent a reader observing a partial snapshot.
            destination = self.directory / f"{snapshot_id}.json"
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(row, sort_keys=True, indent=2) + "\n")
            temporary.replace(destination)
            self._snapshots[key] = row
            return row

    def before_contract(self, contract: Mapping[str, Any]) -> None:
        if contract.get("run_id") != self.run_id:
            return
        try:
            argv = tuple(shlex.split(contract.get("expected_argv") or ""))
        except ValueError:
            return
        for step in self.task_definition.get("steps", []):
            cwd = posixpath.normpath(posixpath.join(self.project_root, step.get("cwd", ".")))
            runner = step.get("runner")
            effective = contract.get("effective_tool")
            if (
                cwd != contract.get("expected_cwd")
                or tuple(step.get("argv", [])[1:]) != argv
                or (runner in {"maven", "gradle"} and runner != effective)
            ):
                continue
            if runner == "native":
                params = (contract.get("requested_call") or {}).get("params") or {}
                if effective != "bash" or params.get("command") != shlex.join(step["argv"]):
                    continue
            elif runner not in {"maven", "gradle"}:
                continue
            identity = contract.get("contract_id")
            if not isinstance(identity, str) or not identity:
                return
            self.capture("acceptance_before", invocation_id=identity)
            self._accepted_contracts.add(identity)
            return

    def after_receipt(self, receipt: Mapping[str, Any]) -> None:
        identity = receipt.get("contract_id")
        if receipt.get("run_id") == self.run_id and identity in self._accepted_contracts:
            self.capture(
                "acceptance_after", invocation_id=identity, receipt_id=receipt.get("receipt_id")
            )


def record_worktree_boundary(source: Any, boundary: str, **kwargs: Any) -> None:
    """Optional lifecycle hook; never turns failed recording into clean state."""
    recorder = _recorder(source)
    if recorder is not None:
        try:
            recorder.capture(boundary, **kwargs)
        except Exception as exc:
            _LOG.warning("Worktree boundary %s could not be retained: %s", boundary, exc)


def _recorder(source: Any) -> WorktreeEvidenceRecorder | None:
    owner = getattr(source, "__self__", None) if callable(source) else source
    recorder = getattr(owner, "worktree_evidence_recorder", None)
    return recorder if isinstance(recorder, WorktreeEvidenceRecorder) else None


def record_contract_worktree(source: Any, contract: Mapping[str, Any]) -> None:
    recorder = _recorder(source)
    if recorder is not None:
        try:
            recorder.before_contract(contract)
        except Exception as exc:
            _LOG.warning("Worktree acceptance-before could not be retained: %s", exc)
    _record_requirements(source, "before_contract", contract)


def record_receipt_worktree(source: Any, receipt: Mapping[str, Any]) -> None:
    recorder = _recorder(source)
    if recorder is not None:
        try:
            recorder.after_receipt(receipt)
        except Exception as exc:
            _LOG.warning("Worktree acceptance-after could not be retained: %s", exc)
    _record_requirements(source, "after_receipt", receipt)


def _record_requirements(source: Any, method: str, payload: Mapping[str, Any]) -> None:
    owner = getattr(source, "__self__", None) if callable(source) else source
    observer = getattr(owner, "requirement_observer", None)
    callback = getattr(observer, method, None)
    if callable(callback):
        try:
            callback(payload)
        except Exception as exc:
            _LOG.warning("Requirement observation %s could not be retained: %s", method, exc)
