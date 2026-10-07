"""Host-owned passive native-tool evidence, separate from execution authority.

Native hooks deliver observations and wait only for readonly collection. No
hook response changes tools, prompts, commands, environments or agent decisions.
The Maven launcher supplies resolved argv/environment: shell programs are not
parsed or rewritten. Unqualified launchers remain explicitly unmeasurable.
"""

import json
import posixpath
import re
import secrets
import shlex
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from sag.agent.worktree_evidence import WorktreeEvidenceRecorder
from sag.benchmark.native_hooks import process_terminal
from sag.benchmark.recorder import reference, write_json
from sag.benchmark.requirements import bound_file, canonical_digest, evaluation_identity, load_json
from sag.benchmark.sag_observer import SAGRequirementObserver

POLICY = "native-tool-hooks-v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def _truncated_native_text(text):
    return "<persisted-output>" in text or bool(
        re.search(r"\[\d+ (?:characters|lines) truncated\]", text)
    )


def collect_native_log(native, *, terminal, read_file, launch_path=None):
    """Prefer full native files, then a complete terminal native hook payload.

    Claude can delete its transient stdout file before posting the terminal
    hook. That does not invalidate a complete hook payload. Truncated or
    pending payloads never become complete through this fallback.
    """
    warnings = []
    if terminal:
        for path in dict.fromkeys([launch_path, native.get("output_path")]):
            if not path:
                continue
            try:
                return read_file(path), True, {"kind": "native_file", "path": path}, warnings
            except Exception as exc:
                warnings.append({"path": path, "error": str(exc)})
    text = native.get("text") or ""
    complete = bool(
        terminal and native.get("status") == "completed"
        and type(native.get("exit_code")) is int and text
        and not native.get("truncated") and not _truncated_native_text(text)
    )
    return text.encode(), complete, {"kind": "native_hook"}, warnings


def native_result(arm, event):
    """Normalize the *native* hook schema; a background acknowledgement is pending."""
    response = event.get("tool_response") or {}
    if arm == "opencode":
        metadata = response.get("metadata") or {}
        code = metadata.get("exit")
        return {
            "status": "completed" if type(code) is int else "unavailable",
            "exit_code": code,
            "text": response.get("output", ""),
            "output_path": metadata.get("outputPath"),
            "truncated": metadata.get("truncated") is True,
        }
    if event["hook_event_name"] == "PostToolUseFailure":
        error = event.get("error", "")
        match = re.match(r"\AExit code (\d+)(?:\n|\Z)", error)
        return {
            "status": "completed" if match and not event.get("is_interrupt") else "unavailable",
            "exit_code": int(match[1]) if match else None,
            "text": error[match.end() :] if match else error,
            "truncated": _truncated_native_text(error),
        }
    if response.get("backgroundTaskId"):
        return {
            "status": "pending",
            "exit_code": None,
            "text": response.get("stdout", ""),
            "background_task_id": response["backgroundTaskId"],
            "truncated": False,
        }
    terminal = event["hook_event_name"] == "PostToolUse" and response.get("interrupted") is False
    # The pinned Claude native Bash success hook has no numeric exit field;
    # nonzero exits use PostToolUseFailure (qualified against the frozen CLI).
    text = response.get("stdout", "") + response.get("stderr", "")
    return {
        "status": "completed" if terminal else "unavailable",
        "exit_code": 0 if terminal else None,
        "text": text,
        "output_path": response.get("persistedOutputPath"),
        "truncated": bool(response.get("isOutputTruncated")) or _truncated_native_text(text),
    }


def expected_shell_calls(arm, path):
    """Independent native CLI transcript, used to detect silently missing hooks."""
    calls = set()
    for line in Path(path).read_text().splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if arm == "claude":
            for part in item.get("message", {}).get("content", []):
                # TaskOutput is a read-only wait. Native argument validation can
                # reject it before hooks fire; that cannot hide a build launch.
                # A successful wait still needs a bound hook to close a job.
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool_use"
                    and part.get("name") == "Bash"
                ):
                    calls.add(part["id"])
        elif item.get("type") == "tool_use" and item.get("part", {}).get("tool") == "bash":
            calls.add(item["part"]["callID"])
    return calls


class NativeObserver:
    def __init__(
        self,
        *,
        base,
        arm,
        run_id,
        root,
        task,
        spec,
        execute,
        read_file,
        list_traces,
        copy_trace,
        start_process_trace=None,
    ):
        self.base, self.arm, self.run_id, self.root = Path(base), arm, run_id, root
        self.task, self.spec = task, spec
        self.execute_raw, self.read_file = execute, read_file
        self.list_traces, self.copy_trace = list_traces, copy_trace
        self.start_process_trace = start_process_trace
        self.environment = {}
        self.lock = threading.RLock()
        self.events, self.calls, self.invocations, self.errors = [], {}, [], []
        self.closed = False
        self.base.mkdir(parents=True, exist_ok=False)
        self.observer = SAGRequirementObserver(self.base, run_id, root, task, spec, self.execute)
        self.worktree = WorktreeEvidenceRecorder(
            self.base, run_id=run_id, project_root=root, execute=self.execute, task_definition=task
        )
        self.worktree_refs = [self._boundary("task_start")]

    def _default_exec_environment(self):
        return self.environment

    def execute(self, command, **kwargs):
        return self.execute_raw(command, **kwargs)

    def _boundary(self, kind, identity=None):
        snapshot = self.worktree.capture(kind, invocation_id=identity)
        return reference(self.base, self.worktree.directory / (snapshot["snapshot_id"] + ".json"))

    def event(self, event):
        with self.lock:
            if self.closed:
                return  # Do not mutate sealed files, even for late notifications.
            number = len(self.events)
            path = self.base / "events" / f"{number:06d}.json"
            write_json(path, {"received_at": now(), "native": event})
            event_ref = reference(self.base, path)
            self.events.append(event_ref)
            tool_id = event.get("tool_use_id")
            if event.get("hook_event_name") == "MavenLaunch":
                try:
                    self._launch(event, event_ref)
                except Exception as exc:
                    self.errors.append({"event": event_ref, "error": str(exc)})
                    write_json(self.base / "collection-errors.json", self.errors)
                return
            if event.get("tool_name") not in {"bash", "Bash", "TaskOutput"}:
                return
            try:
                if event["hook_event_name"] == "PreToolUse":
                    self._before(tool_id, event, event_ref)
                else:
                    self._after(tool_id, event, event_ref)
            except Exception as exc:
                self.errors.append({"tool_use_id": tool_id, "event": event_ref, "error": str(exc)})
                write_json(self.base / "collection-errors.json", self.errors)

    def _before(self, tool_id, event, ref):
        if not isinstance(tool_id, str) or tool_id in self.calls:
            raise ValueError("Missing or reused native tool identity")
        call = {
            "before": ref,
            "after": None,
            "started_at": now(),
            "sequence": len(self.calls),
            "session_id": event.get("session_id"),
            "selected_step": None,
            "tool_name": event.get("tool_name"),
        }
        self.calls[tool_id] = call
        call["native_input"] = event.get("tool_input", {})

    def _launch(self, event, ref):
        launch = event["launch"]
        if launch.get("root") != self.root or launch.get("commit") != self.task["sha"]:
            return
        argv = launch.get("argv") or []
        matches = [
            step
            for step in self.task["steps"]
            if argv
            and (
                argv[0] == step["argv"][0]
                or (
                    step["argv"][0] == "mvn"
                    and PurePosixPath(argv[0]).is_absolute()
                    and PurePosixPath(argv[0]).name == "mvn"
                )
            )
            and argv[1:] == step["argv"][1:]
            and launch.get("cwd") == posixpath.normpath(posixpath.join(self.root, step["cwd"]))
        ]
        if not matches:
            return  # Version probes/exploration are not frozen build launches.
        active = [
            c
            for c in self.calls.values()
            if c.get("tool_name") in {"Bash", "bash"}
            and (
                c.get("after") is None
                or c.get("native_result", {}).get("status") == "pending"
            )
        ]
        if len(active) != 1:
            raise ValueError("Native Maven launch has no unique active shell tool")
        call = active[0]
        if call.get("selected_step") is not None:
            raise ValueError(
                "Multiple build launches in one tool need separate physical boundaries"
            )
        if type(launch.get("launcher_pid")) is not int or launch["launcher_pid"] <= 0:
            raise ValueError("Native Maven launcher process is unavailable")
        parsed = {
            "argv": launch["argv"],
            "cwd": launch["cwd"],
            "environment": event["observer_environment"],
            "log_path": event.get("output_path"),
            "background": False,
        }
        call.update(
            launch_event=ref, process_id=launch["launcher_pid"], namespace=event["namespace"]
        )
        if len(matches) != 1:
            raise ValueError("Frozen step identity is ambiguous for this native launch")
        call["selected_step"] = matches[0]
        step = call["selected_step"]
        if self.start_process_trace:
            self.start_process_trace(call["process_id"])
        identity = "ic-" + uuid.uuid4().hex[:12]
        out = self.base / "invocations" / identity
        out.mkdir(parents=True)
        call.update(parsed=parsed, invocation_id=identity, out=out)
        # Any overlapping shell tool can mutate build inputs/outputs. Observe it
        # without serializing the actor, but do not certify shared provenance.
        for other in self.calls.values():
            if (
                other is not call
                and other.get("tool_name") in {"Bash", "bash"}
                and (
                    other.get("after") is None
                    or other.get("native_result", {}).get("status") == "pending"
                )
            ):
                call["overlap"] = other["overlap"] = True
        self.environment = parsed["environment"]
        contract = {
            "run_id": self.run_id,
            "target_sha": self.task["sha"],
            "contract_id": identity,
            "effective_tool": step["runner"],
            "expected_argv": shlex.join(step["argv"][1:]),
            "observed_launcher": parsed["argv"][0],
            "expected_cwd": parsed["cwd"],
            "requested_call": {"params": {"timeout": 60}},
        }
        contract["contract_hash"] = canonical_digest(contract)
        call["contract"] = contract
        self.worktree_refs.append(self._boundary("acceptance_before", identity))
        call["prior_traces"] = set(self.list_traces())
        self.observer.before_contract(contract)
        if step["runner"] in {"maven", "gradle"}:
            call["probe"] = self.observer._version_probe(
                [parsed["argv"][0], "--version"], step, out, "launcher"
            )

    def _after(self, tool_id, event, ref):
        if tool_id not in self.calls or self.calls[tool_id]["after"] is not None:
            raise ValueError("Native end observation has no unique start")
        call = self.calls[tool_id]
        if call["session_id"] != event.get("session_id") or call["tool_name"] != event.get(
            "tool_name"
        ):
            raise ValueError("Native tool end belongs to another session or tool")
        call["after"] = ref
        if event.get("tool_name") == "TaskOutput" and self.arm == "claude":
            self._background_result(event, ref)
            return
        native = native_result(self.arm, event)
        if call.get("parsed", {}).get("background"):
            native.update(status="pending", exit_code=None)
        # A background acknowledgement may arrive before Maven's launch hook.
        # Keep the task identity alive until its actual terminal observation.
        call["native_result"] = native
        if call["selected_step"] is None:
            return
        if native["status"] != "completed":
            return  # No post-execution inventory while a producer may be alive.
        self._finish_process(call, native)

    def _finish_process(self, call, native):
        try:
            raw = self.read_file("/opt/benchmark-process-traces/process." + str(call["process_id"]))
            terminal = process_terminal(raw)
            path = call["out"] / "process.trace"
            path.write_bytes(raw)
            call["process_observation"] = reference(
                self.base,
                path,
                process_id=call["process_id"],
                namespace=call["namespace"],
                **terminal,
            )
            native = {
                **native,
                "native_tool_exit_code": native.get("exit_code"),
                "exit_code": terminal["exit_code"],
                "status": "completed",
            }
            self._record(call, native, terminal=True)
        except (KeyError, OSError, ValueError) as exc:
            call["process_observation_error"] = str(exc)
            call["native_result"] = {**native, "status": "unavailable", "exit_code": None}

    def _background_result(self, event, ref):
        response = event.get("tool_response") or {}
        task = response.get("task") or {}
        task_id = event.get("tool_input", {}).get("task_id")
        matches = [
            c
            for c in self.calls.values()
            if c.get("tool_name") in {"Bash", "bash"}
            and "record" not in c
            and c.get("native_result", {}).get("status") == "pending"
            and c.get("session_id") == event.get("session_id")
            and c.get("native_result", {}).get("background_task_id") == task_id
        ]
        if (
            event["hook_event_name"] != "PostToolUse"
            or response.get("retrieval_status") != "success"
            or task.get("task_type") != "local_bash"
            or task.get("task_id") != task_id
            or task.get("status") not in {"completed", "failed"}
            or type(task.get("exitCode")) is not int
        ):
            return
        if len(matches) != 1:
            raise ValueError("Native background completion has no unique task binding")
        call = matches[0]
        call["terminal_after"] = ref
        native = {
            "status": "completed",
            "exit_code": task["exitCode"],
            "text": task.get("output", ""),
            "truncated": bool(task.get("truncated")),
            "background_task_id": task_id,
        }
        call["native_result"] = native
        if call.get("selected_step"):
            self._finish_process(call, native)

    def _record(self, call, native, *, terminal):
        step, identity, out = call["selected_step"], call["invocation_id"], call["out"]
        self.environment = call["parsed"]["environment"]
        log, complete, log_source, log_warnings = collect_native_log(
            native, terminal=terminal, read_file=self.read_file,
            launch_path=call["parsed"].get("log_path"),
        )
        log_error = None if complete else "Complete terminal native output is unavailable"
        (out / "command.log").write_bytes(log)
        observation = {}
        if terminal:
            receipt = {
                **call["contract"],
                "receipt_id": identity + "-native",
                "actual_cwd": call["parsed"]["cwd"],
                "exit_code": native["exit_code"],
                "lifecycle_state": "finished",
            }
            self.observer.after_receipt(receipt)
            observation = self.observer.export_invocation(identity) or {}
            self.worktree_refs.append(self._boundary("acceptance_after", identity))
        record = {
            "schema_version": 2,
            "recorder_version": POLICY,
            "execution_origin": "agent",
            "run_id": self.run_id,
            "repo": self.task["repo"],
            "commit": self.task["sha"],
            "evaluation_identity": evaluation_identity(self.spec),
            "project_root": self.root,
            "invocation_id": identity,
            "step_id": step["id"],
            "sequence": call["sequence"],
            "runner": step["runner"],
            "argv": step["argv"],
            "effective_argv": call["parsed"]["argv"],
            "cwd": step["cwd"],
            "status": native["status"],
            "exit_code": native.get("exit_code"),
            "started_at": call["started_at"],
            "finished_at": now() if terminal else None,
            "log": reference(self.base, out / "command.log"),
            "log_complete": complete,
            "log_source": log_source,
            "log_collection_warnings": log_warnings,
            "runtime": {"launcher_probe": call.get("probe")},
            "native_hook": {
                "before": call["before"],
                "after": call.get("terminal_after", call["after"]),
                "background_acknowledgement": call["after"] if call.get("terminal_after") else None,
            },
            "native_launcher_event": call.get("launch_event"),
            "native_process": call.get("process_observation"),
            "input_policy": {"native_tools_unchanged": True},
            **observation,
        }
        record["collection_errors"] = [
            e
            for e in (log_error, "Overlapping native shell tools" if call.get("overlap") else None)
            if e
        ]
        # A hook observes the parent environment. Require the independently
        # observed actual Maven launch before certifying environment/argv.
        record["native_launch"] = (
            self._native_launch(call) if terminal and step["runner"] == "maven" else None
        )
        if not record["native_launch"] or call.get("overlap"):
            record["effective_execution"] = {
                **record.get("effective_execution", {}),
                "inputs_complete": False,
                "serial": False,
                "errors": ["Native launch environment or exclusive observation unavailable"],
            }
            record["runtime"] = (
                {}
            )  # Do not turn a parent-environment probe into actual runtime truth.
        else:
            record["runtime"]["native_launch"] = record["native_launch"]
        write_json(out / "invocation.json", record)
        call["record"] = reference(self.base, out / "invocation.json")
        self.invocations.append(call["record"])

    def _native_launch(self, call):
        selected = []
        policy = self.spec.get("maven_observer") or {}
        for name in set(self.list_traces()) - call["prior_traces"]:
            if not re.fullmatch(r"[a-f0-9-]{36}", name):
                continue
            target = self.base / "native-traces" / name
            self.copy_trace(name, target)
            events = target / "events.jsonl"
            rows = [json.loads(line) for line in events.read_text().splitlines()]
            if (
                not rows
                or rows[0].get("event") != "ObserverStarted"
                or rows[-1].get("event") != "ObserverClosed"
            ):
                continue
            launch = json.loads(rows[0]["launcher_observation"])
            if (
                launch.get("cwd") != call["parsed"]["cwd"]
                or launch.get("argv", [])[1:] != call["parsed"]["argv"][1:]
                or rows[0].get("process_id") != str(call["process_id"])
                or rows[0].get("run_id") != call["namespace"]
                or launch.get("commit") != self.task["sha"]
                or launch.get("root") != self.root
                or launch.get("tracked_clean") is not True
                or any(
                    launch.get(k) != policy.get(k) for k in ("observer_sha256", "activation_sha256")
                )
            ):
                continue
            actual_env = launch.get("runtime_inputs")
            keys = {
                "MAVEN_ARGS",
                "MAVEN_OPTS",
                "MAVEN_CONFIG",
                "MAVEN_PROJECTBASEDIR",
                "MAVEN_SKIP_RC",
                "JAVA_TOOL_OPTIONS",
                "_JAVA_OPTIONS",
                "JDK_JAVA_OPTIONS",
            }
            if (
                not isinstance(actual_env, dict)
                or set(actual_env) != keys
                or any(call["parsed"]["environment"].get(k, "") != v for k, v in actual_env.items())
            ):
                continue
            sessions = [r for r in rows if r.get("event") == "SessionStarted"]
            if len(sessions) != 1 or sessions[0].get("concurrency", 1) != 1:
                continue
            selected.append(reference(self.base, events))
        return selected[0] if len(selected) == 1 else None

    def close(self, transcript):
        with self.lock:
            if self.closed:
                return load_json(self.base / "run.json")
            self.closed = True
            missing = expected_shell_calls(self.arm, transcript) - self.calls.keys()
            for call in self.calls.values():
                if call.get("selected_step") and "record" not in call and "invocation_id" in call:
                    self._record(
                        call,
                        call.get("native_result", {"status": "unavailable", "text": ""}),
                        terminal=False,
                    )
            self.worktree_refs.append(self._boundary("evidence_close"))
            # A restart of step i invalidates all previously selected steps at
            # i or later. This is a final ordered suffix, never a best-of merge.
            selected = {}
            order = {s["id"]: i for i, s in enumerate(self.task["steps"])}
            for ref in sorted(
                self.invocations, key=lambda ref: load_json(bound_file(self.base, ref))["sequence"]
            ):
                record = load_json(bound_file(self.base, ref))
                index = order[record["step_id"]]
                selected = {i: value for i, value in selected.items() if i < index}
                selected[index] = ref
            errors = [*self.errors]
            if missing:
                errors.append(
                    {
                        "error": "Native CLI calls have no pre-tool observation",
                        "tool_use_ids": sorted(missing),
                    }
                )
            run = {
                "schema_version": 2,
                "producer": "native_tool_observer_v1",
                "execution_origin": "agent",
                "agent": self.arm,
                "run_id": self.run_id,
                "repo": self.task["repo"],
                "commit": self.task["sha"],
                "project_root": self.root,
                "evaluation_identity": evaluation_identity(self.spec),
                "invocations": [selected[i] for i in sorted(selected)] if not errors else [],
                "all_invocations": self.invocations,
                "worktree": self.worktree_refs,
                "events": self.events,
                "capture_errors": errors,
                "capture_policy": POLICY,
                "capture_closed": True,
                "finished_at": now(),
                "telemetry": None,
            }
            write_json(
                self.base / "native-calls.json",
                {
                    key: {k: v for k, v in call.items() if k not in {"out", "prior_traces"}}
                    for key, call in self.calls.items()
                },
            )
            write_json(self.base / "run.json", run)
            return run


class ObserverServer:
    def __init__(self, observer, port):
        self.token = secrets.token_urlsafe(32)
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/event" or self.headers.get("Authorization") != "Bearer " + token:
                    self.send_error(403)
                    return
                try:
                    event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    observer.event(event)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                except Exception:
                    self.send_error(500)

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
