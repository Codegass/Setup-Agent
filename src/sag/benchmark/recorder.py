"""Trusted portable recording of a frozen task, before and after any agent.

Use separate ``start``, ordered ``step`` and ``close`` commands. Start must run
before the agent is launched. This recorder cannot observe human input between
commands, so it never invents an autonomous-run telemetry record.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid

from sag.agent.worktree_evidence import WorktreeEvidenceRecorder
from .requirements import bound_file, canonical_digest, evaluation_identity, file_digest, load_json, validate_requirements, normalize_task, repository_artifact_path, report_directories, requires_native_image
from .native_evidence import parse_local_repository, launcher_java_home
from . import jvm_inputs
from . import compilation_evidence
from .wrapper_review import (review_for, capture_request, capture_wrapper_inputs, snapshot_inputs, certify_launcher)

RECORDER_VERSION = "portable-requirements-v2"


def now():
    return datetime.now(timezone.utc).isoformat()


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise ValueError("Symlink path is not a trusted recorder location: " + str(item))
    return path


def beneath(root, relative):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Path escapes its declared root")
    candidate = root / rel
    no_symlinks(candidate)
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Path escapes its declared root")
    return candidate


def write_json(path, value):
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n")
    temporary.replace(path)


def reference(base, path, **extra):
    no_symlinks(path)
    return {"path": str(path.relative_to(base)), "sha256": file_digest(path), "bytes": path.stat().st_size, **extra}


def _file_fingerprint(path):
    # Match the native observer: replacing a file can preserve bytes and mtime.
    # Device/inode distinguish that replacement from an untouched old artifact.
    fields = lambda value: (value.st_mtime_ns, value.st_size, value.st_ino, value.st_dev)
    before = path.stat()
    digest = file_digest(path)
    after = path.stat()
    if fields(before) != fields(after):
        raise ValueError("Evidence changed during fingerprinting: " + str(path))
    return (digest, *fields(after))


@contextmanager
def recording_lock(base):
    no_symlinks(base)
    base.mkdir(parents=True, exist_ok=True)
    lock = base / ".recorder.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        lock.unlink()


class Recorder:
    def __init__(self, *, task, requirements, repo_root, records, run_id):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("A trusted runner must assign a run ID")
        self.task = normalize_task(task)
        self.spec = validate_requirements(requirements, self.task)
        self.root = no_symlinks(repo_root).resolve()
        self.base = no_symlinks(records).resolve()
        if self.base.is_relative_to(self.root) or self.root.is_relative_to(self.base):
            raise ValueError("Records and tested checkout must be separate trees")
        if not self.root.is_dir():
            raise ValueError("Missing tested checkout")
        self.run_id = run_id
        self.identity = evaluation_identity(self.spec)
        self.worktree = WorktreeEvidenceRecorder(self.base, run_id=run_id, project_root=str(self.root), task_definition=self.task)

    def _state(self, *, require_ready=True):
        state = load_json(self.base / "run-open.json")
        if state.get("run_id") != self.run_id or state.get("evaluation_identity") != self.identity or state.get("project_root") != str(self.root):
            raise ValueError("Recorder identity changed after task start")
        if (self.base / "run.json").exists():
            raise ValueError("Attempt is already closed")
        if require_ready and state.get("admission") != "ready":
            raise ValueError("Task-start source integrity was not established")
        return state

    def _boundary(self, name, invocation_id=None):
        snapshot = self.worktree.capture(name, invocation_id=invocation_id)
        path = self.base / "worktree-evidence" / (snapshot["snapshot_id"] + ".json")
        return snapshot, reference(self.base, path)

    def start(self, *, agent="unspecified", execution_origin="independent_replay"):
        if execution_origin not in {"agent", "independent_replay"}:
            raise ValueError("Unknown execution origin")
        with recording_lock(self.base):
            if (self.base / "run-open.json").exists() or (self.base / "run.json").exists() or (self.base / "invocations").exists():
                raise ValueError("Start requires a new attempt directory; do not fabricate a late task_start")
            snapshot, ref = self._boundary("task_start")
            admission = "ready" if snapshot.get("head") == self.task["sha"] and snapshot.get("tracked_clean") is True and snapshot.get("status") == "observed" else "failed" if snapshot.get("head") not in {None, self.task["sha"]} or snapshot.get("tracked_clean") is False else "unavailable"
            state = {"schema_version": 2, "recorder_version": RECORDER_VERSION, "run_id": self.run_id,
                     "execution_origin": execution_origin,
                     "evaluation_identity": self.identity, "repo": self.task["repo"], "commit": self.task["sha"],
                     "project_root": str(self.root), "agent": agent, "started_at": now(),
                     "admission": admission, "worktree": [ref], "invocations": [],
                     "input_policy": {"step_stdin": "DEVNULL", "between_commands": "not_observed"}}
            write_json(self.base / "task.json", self.task)
            write_json(self.base / "requirements.json", self.spec)
            write_json(self.base / "run-open.json", state)
            return state

    @staticmethod
    def _repository_path(item):
        return repository_artifact_path(item)

    def _files(self, step, local_repository=None):
        """Only declared native report directories/artifacts; never scan .m2."""
        paths = {}
        for row in self.spec["requirements"]:
            if row["step_id"] != step["id"]:
                continue
            module_path = row.get("scope", {}).get("module_path")
            if row["kind"] == "test" and module_path is not None and row.get("validation", {}).get("rule") in {"junit", "maven_invoker"}:
                module = beneath(self.root, module_path)
                declared = report_directories(row)
                directories = [beneath(self.root, directory) for directory in declared] if declared is not None else [module / "target" / ("failsafe-reports" if row.get("subtype") == "integration" else "surefire-reports")] if step["runner"] == "maven" else [module / "build/test-results"]
                for directory in directories:
                    no_symlinks(directory)
                    if not directory.exists():
                        continue
                    def walk_error(error):
                        raise error
                    for current, dirs, files in os.walk(directory, followlinks=False, onerror=walk_error):
                        for name in dirs:
                            no_symlinks(Path(current) / name)
                        for name in files:
                            path = Path(current) / name
                            prefix = "BUILD-" if row["validation"]["rule"] == "maven_invoker" else "TEST-"
                            if not name.startswith(prefix) or not name.endswith(".xml"):
                                continue
                            no_symlinks(path)
                            if path.is_file():
                                rel = str(path.relative_to(self.root))
                                paths[("report", rel)] = (path, {"module": row.get("module"), "test_kind": row.get("subtype", "unit")})
            for item in row.get("expectations", {}).get("artifacts", []):
                if row.get("validation", {}).get("rule") == "install" and item.get("repository_relative_path"):
                    if not local_repository:
                        continue
                    relative = self._repository_path(item)
                    path = beneath(Path(local_repository["path"]), relative)
                    if path.is_file():
                        metadata = {key: item[key] for key in ("role", "module", "coordinates", "extension", "classifier", "format") if key in item}
                        metadata["repository_relative_path"] = relative
                        paths[("installed", relative, canonical_digest(metadata))] = (path, metadata)
                    if item.get("install_source_path"):
                        from .installed_evidence import validate_source
                        validate_source(item)
                        source = beneath(self.root, item["install_source_path"])
                        if source.is_file():
                            metadata = {key: item[key] for key in ("role", "module", "coordinates", "extension", "classifier", "format") if key in item}
                            metadata["installation_source_for"] = relative
                            paths[("install_source", item["install_source_path"], canonical_digest(metadata))] = (source, metadata)
                    continue
                location = item.get("path")
                if not isinstance(location, str):
                    continue
                if PurePosixPath(location).is_absolute():
                    # Installation repositories need a captured runtime mapping.
                    # Do not read arbitrary absolute paths supplied in metadata.
                    continue
                path = beneath(self.root, location)
                if path.is_file():
                    metadata = {key: item[key] for key in ("role", "module", "coordinates", "format", "extension", "classifier") if key in item}
                    # One output can fulfill multiple declared roles (for example,
                    # a main JAR augmented with module-info). Preserve each role.
                    paths[("artifact", location, canonical_digest(metadata))] = (path, metadata)
        return paths

    def _snapshot(self, step, local_repository=None):
        return {key: _file_fingerprint(path)
                for key, (path, _) in self._files(step, local_repository).items()}

    def _compilation_snapshot(self, step, invocation_id, out, boundary):
        plans = compilation_evidence.plans_for(self.spec, step["id"])
        if not plans:
            return None
        path = out / ("compilation-" + boundary + ".json")
        write_json(path, {"run_id": self.run_id, "invocation_id": invocation_id, "boundary": boundary,
                          "observations": compilation_evidence.capture(self.root, plans)})
        return reference(self.base, path)

    def _output_inventory(self, step, invocation_id, out, boundary):
        from .artifact_inventory import capture, inventory_plan

        if not self.spec.get("steps"):
            return None  # Old/minimal definitions declare no inventory scope.
        path = out / ("outputs-" + boundary + ".json")
        write_json(path, capture(self.root, inventory_plan(self.spec, step["id"]),
                                run_id=self.run_id, invocation_id=invocation_id, boundary=boundary))
        return reference(self.base, path)

    def _probe(self, argv, cwd, env, out, *, name="launcher-probe", timeout=30):
        path = out / (name + ".log")
        status, code = "unavailable", None
        started = now()
        with path.open("wb") as log:
            try:
                result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
                code, status = result.returncode, "completed"
            except OSError as exc:
                status = "launch_failed"
                log.write((type(exc).__name__ + ": " + str(exc)).encode())
            except subprocess.TimeoutExpired as exc:
                status = "timeout"
                log.write((type(exc).__name__ + ": " + str(exc)).encode())
        executable = shutil.which(argv[0], path=env.get("PATH")) if "/" not in argv[0] else str((cwd / argv[0]).resolve())
        return reference(self.base, path, exit_code=code, status=status, argv=argv, timeout_seconds=timeout,
                         executable=executable, started_at=started, finished_at=now())

    def _native_image_probe(self, launcher_probe, invocation_id, cwd, env, out, timeout):
        observed = {"source": "launcher_jdk_capability", "run_id": self.run_id,
                    "invocation_id": invocation_id, "java_home_probe": launcher_probe,
                    "launcher_java_home": None}
        if launcher_probe.get("exit_code") != 0:
            return observed
        home = launcher_java_home(bound_file(self.base, launcher_probe).read_text(errors="replace"))
        observed["launcher_java_home"] = home
        if home is not None:
            observed["probe"] = self._probe([str(Path(home) / "bin/native-image"), "--version"],
                                            cwd, env, out, name="native-image-probe", timeout=timeout)
        return observed

    def _local_repository(self, step, argv, cwd, env, out, timeout, inputs):
        if step["runner"] != "maven" or not any(r["step_id"] == step["id"] and r.get("validation", {}).get("rule") == "install" for r in self.spec["requirements"]):
            return None
        # Preserve configuration/profile/property options, replace lifecycle
        # goals with a metadata-only goal. Never execute the recorded task here.
        value_options = {"-f", "--file", "-pl", "--projects", "-P", "--activate-profiles", "-s", "--settings", "-gs", "--global-settings", "-t", "--toolchains", "-gt", "--global-toolchains", "-T", "--threads", "-rf", "--resume-from", "-D", "--define", "-l", "--log-file"}
        if inputs.get("inputs_complete") is not True or inputs.get("wrapper_reviewed") is not True:
            return None
        value = False
        for arg in inputs["maven_config"] + inputs["maven_args"]:
            if value:
                value = False
            elif arg in value_options:
                value = True
            elif not arg.startswith("-"):
                # Effective configuration could append lifecycle goals to a
                # metadata probe. Preserve unknown instead of running a build.
                return None
        options, value = [], False
        for arg in argv[1:]:
            if value:
                options.append(arg); value = False
            elif arg.startswith("-"):
                options.append(arg); value = arg in value_options
        probe = self._probe([argv[0], *options, "help:evaluate", "-Dexpression=settings.localRepository", "-q", "-DforceStdout"], cwd, env, out, name="local-repository-probe", timeout=max(.001, timeout))
        raw = bound_file(self.base, probe).read_text(errors="replace")
        path = parse_local_repository(raw)
        if probe["exit_code"] != 0 or path is None:
            return {"path": None, "probe": probe, "exit_code": probe["exit_code"], "source": "maven_settings_probe", "status": "unavailable"}
        try:
            path = str(no_symlinks(path).resolve())
        except ValueError:
            return {"path": None, "probe": probe, "exit_code": probe["exit_code"], "source": "maven_settings_probe", "status": "unavailable"}
        return {"path": path, "probe": probe, "exit_code": probe["exit_code"], "source": "maven_settings_probe", "status": "observed"}

    def _native_input(self, step, cwd, out, state):
        if step["runner"] != "native":
            return None
        executable = beneath(self.root, str((cwd / step["argv"][0]).relative_to(self.root)))
        if not executable.is_file():
            return None
        path = out / "native-executable-input"
        shutil.copyfile(executable, path)
        relative = str(executable.relative_to(self.root))
        proof = reference(self.base, path, producer_relative_path=relative, producer_invocation_id=None)
        if file_digest(executable) != proof["sha256"]:
            raise ValueError("Native binary changed during prelaunch capture")
        for ref in reversed(state["invocations"]):
            producer = load_json(bound_file(self.base, ref))
            if producer.get("run_id") != self.run_id or producer.get("status") != "completed" or producer.get("exit_code") != 0:
                continue
            for artifact in producer.get("artifacts", []):
                if artifact.get("fresh") is True and artifact.get("producer_relative_path") == relative and artifact.get("sha256") == proof["sha256"]:
                    bound_file(self.base, artifact)
                    proof["producer_invocation_id"] = producer["invocation_id"]
                    proof["producer_artifact"] = artifact
                    return proof
        return proof

    def _frozen_pom_selector(self, step, extra_args, out):
        """Admit a literal frozen cwd/pom.xml after a tracked byte comparison.

        A known -f pom.xml is the original benchmark command, not an opaque
        alternate project. Extra selectors from runtime configuration remain
        unsupported because they can change the frozen task's project base.
        """
        def selectors(args):
            found = []
            for index, arg in enumerate(args):
                if arg in {"-f", "--file"}:
                    found.append(args[index + 1] if index + 1 < len(args) else None)
                elif arg.startswith(("--file=", "-f=")):
                    found.append(arg.partition("=")[2])
                elif arg.startswith("-f") and arg not in {"-fae", "-fn", "-ff"}:
                    found.append(None)  # Unreviewed compact selector, e.g. -fpom.xml.
            return found
        if selectors(extra_args):
            return False, [], "Runtime configuration overrides the frozen Maven project selector"
        requested = selectors(step["argv"][1:])
        if not requested:
            return True, [], None
        if len(requested) != 1 or requested[0] not in {"pom.xml", "./pom.xml"}:
            return False, [], "Alternate Maven project configuration is not reviewed"
        refs, probes = [], []
        try:
            relative = str(PurePosixPath(step.get("cwd", ".")) / "pom.xml")
            path = beneath(self.root, relative)
            if not path.is_file():
                raise ValueError("Frozen selected POM is missing")
            raw = path.read_bytes()
            captured = out / "selected-pom.xml"
            captured.write_bytes(raw)
            refs.append(reference(self.base, captured, source=relative))
            commands = [
                ["git", "-C", str(self.root), "ls-files", "--error-unmatch", "-z", "--", relative],
                ["git", "-C", str(self.root), "show", self.task["sha"] + ":" + relative],
            ]
            outputs = []
            for index, argv in enumerate(commands):
                completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
                probe = {"argv": argv, "exit_code": completed.returncode}
                for name, data in (("stdout", completed.stdout), ("stderr", completed.stderr)):
                    destination = out / f"selected-pom-{index}.{name}"
                    destination.write_bytes(data)
                    probe[name] = reference(self.base, destination)
                probes.append(probe)
                outputs.append(completed.stdout)
            proof = out / "selected-pom-proof.json"
            write_json(proof, {"source": relative, "commit": self.task["sha"], "probes": probes})
            refs.append(reference(self.base, proof, source="selected_pom_tracked_revision_proof"))
            if any(probe["exit_code"] != 0 for probe in probes) or outputs[0] != (relative + "\0").encode() or outputs[1] != raw:
                raise ValueError("Frozen selected POM is untracked or differs from its pinned bytes")
            return True, refs, None
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return False, refs, str(exc)

    def _wrapper_snapshot(self, step, env, out, invocation_id, boundary):
        review = review_for(self.spec, step)
        try:
            request = capture_request(self.task, step, review, self.root, self.run_id, invocation_id, boundary, env,
                                      environment_review=jvm_inputs.environment_review_for(self.spec, step))
            request['maven_observer'] = self.spec.get('maven_observer')
            snapshot = capture_wrapper_inputs(request)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            snapshot = {"errors": [str(exc)]}
        path = out / ("wrapper-" + boundary + ".json")
        write_json(path, snapshot)
        return snapshot, reference(self.base, path)

    def _effective_execution(self, step, env, out):
        if step["runner"] == "maven" and step["argv"][0] != "mvn":
            # Wrapper proof is separate; never archive opaque environment values.
            return {"inputs_complete": False, "serial": False, "wrapper_reviewed": False,
                    "wrapper_status": "unreviewed", "argv": step["argv"], "errors": []}
        maven_config, maven_args, jvm_config, refs, errors, complete = [], [], [], [], [], True
        jvm_raw = None
        try:
            for name in ("maven.config", "jvm.config"):
                path = beneath(self.root, ".mvn/" + name)
                if path.exists():
                    raw = path.read_bytes()
                    copied = out / name
                    copied.write_bytes(raw)
                    refs.append(reference(self.base, copied, source=".mvn/" + name))
                    # Both readers parse the retained bytes, not a second read
                    # that could observe a different configuration version.
                    parsed = shlex.split(raw.decode(), comments=True)
                    if name == "maven.config":
                        maven_config = parsed
                    else:
                        jvm_raw = raw
                        jvm_config = parsed
                        if any(arg.startswith(("-D", "@")) for arg in parsed) and not jvm_inputs.matches(self.spec, self.task, step, raw):
                            errors.append("JVM configuration may alter Maven execution")
            if jvm_inputs.review_for(self.spec, step) is not None and not jvm_inputs.matches(self.spec, self.task, step, jvm_raw):
                errors.append("JVM configuration differs from its frozen source review")
            maven_args = shlex.split(env.get("MAVEN_ARGS", ""))
            errors.extend(jvm_inputs.environment_errors(self.spec, self.task, step, env))
            retained_env = {key: env.get(key, "") for key in (
                "MAVEN_ARGS", "MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
                "MAVEN_CONFIG", "MAVEN_PROJECTBASEDIR", "MAVEN_SKIP_RC")}
            write_json(out / "runtime-inputs.json", retained_env)
            refs.append(reference(self.base, out / "runtime-inputs.json", source="recorder_process_environment"))
        except (OSError, ValueError, UnicodeError) as exc:
            complete = False
            errors.append("Execution inputs unavailable: " + str(exc))
        rc_unobserved = env.get("MAVEN_SKIP_RC") != "true" and any(path.exists() for path in [Path("/etc/mavenrc"), Path("/usr/local/etc/mavenrc"), Path(env.get("HOME", str(Path.home()))) / ".mavenrc"])
        if rc_unobserved and self.spec.get('maven_observer'):
            try:
                paths=[Path('/etc/mavenrc'),Path('/usr/local/etc/mavenrc'),Path(env.get('HOME', str(Path.home())))/'.mavenrc',
                       Path('/opt/sag-maven-witness/launch.py'),Path('/opt/sag-maven-witness/witness.jar')]
                observed={str(p):file_digest(p) if not p.is_symlink() else 'symlink' for p in paths if p.exists() or p.is_symlink()}
                rc_unobserved = not jvm_inputs.observer_rc_known(self.spec, observed)
                write_json(out/'observer-startup.json', observed)
                refs.append(reference(self.base,out/'observer-startup.json',source='frozen_passive_observer'))
            except (OSError,ValueError):
                pass
        base_overridden = bool(env.get("MAVEN_PROJECTBASEDIR")) and Path(env["MAVEN_PROJECTBASEDIR"]).resolve() != self.root
        if rc_unobserved:
            errors.append("Maven startup rc is not reviewed")
        if base_overridden:
            errors.append("Maven project base was overridden")
        if env.get("MAVEN_CONFIG"):
            errors.append("MAVEN_CONFIG may supply additional execution inputs")
        args = step["argv"] + maven_config + maven_args
        if any(arg.startswith("@") for arg in args):
            errors.append("Alternate Maven argfile configuration is not reviewed")
        selected_pom_known, selected_refs, selected_error = self._frozen_pom_selector(step, maven_config + maven_args, out)
        refs.extend(selected_refs)
        if not selected_pom_known:
            errors.append(selected_error)
        direct = step["runner"] == "maven" and step["argv"][0] == "mvn"
        complete = complete and direct and not errors
        serial = complete and not any(a.startswith("-T") or a.startswith("--threads") for a in args)
        # Wrapper scripts can rewrite args; file existence is not a review.
        return {"inputs_complete": complete, "serial": serial,
                "wrapper_reviewed": direct, "argv": step["argv"], "maven_config": maven_config,
                "maven_args": maven_args, "jvm_config": jvm_config, "configuration_evidence": refs,
                "maven_args_source": "recorder_process_environment", "wrapper_status": "direct_maven" if direct else "unreviewed",
                "startup_rc_unobserved": rc_unobserved, "project_base_overridden": base_overridden, "errors": errors}

    def _quiesce_gradle(self, launcher, cwd, env, out, timeout):
        """Explicit container experiment preparation, never a build obligation."""
        def daemons():
            proc = Path('/proc')
            if not proc.is_dir():
                raise ValueError('Gradle process observation requires container procfs')
            found = []
            for entry in proc.glob('[0-9]*'):
                try:
                    args = (entry/'cmdline').read_bytes().split(b'\0')
                    marker = b'org.gradle.launcher.daemon.bootstrap.GradleDaemon'
                    if marker in args and (entry/'exe').resolve().name == 'java':
                        at = args.index(marker)
                        found.append({'pid': int(entry.name),
                            'version': args[at+1].decode(errors='replace') if at+1 < len(args) else None})
                except FileNotFoundError:
                    continue  # A process exited during this read-only scan.
            return sorted(found, key=lambda row: row['pid'])

        deadline = time.monotonic() + timeout
        result = {'policy': 'stop_gradle_daemons_before_independent_step',
                  'status': 'unavailable', 'command_unchanged': True}
        try:
            result['before'] = daemons()
            if result['before']:
                result['stop_command'] = self._probe([launcher, '--stop'], cwd, env, out,
                    name='gradle-daemon-stop', timeout=max(.001, deadline-time.monotonic()))
                if result['stop_command'].get('exit_code') != 0:
                    raise ValueError('Gradle daemon stop did not complete successfully')
            result['after'] = daemons()
            while result['after'] and time.monotonic() < deadline:
                time.sleep(min(.1, max(0, deadline-time.monotonic())))
                result['after'] = daemons()
            if result['after']:
                raise ValueError('Gradle daemons remain; do not overlap acceptance memory budgets')
            result['status'] = 'quiescent'
        except (OSError, ValueError) as exc:
            result['reason'] = str(exc)
        path = out/'gradle-daemon-quiescence.json'
        write_json(path, result)
        return reference(self.base, path, status=result['status'])

    def step(self, step_id, *, timeout, java_home=None, maven_bin=None, quiesce_gradle=False, maven_witness=False):
        if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A finite positive remaining budget is required")
        with recording_lock(self.base):
            preparation_tick = time.monotonic()
            state = self._state()
            index = len(state["invocations"])
            if index >= len(self.task["steps"]) or self.task["steps"][index]["id"] != step_id:
                raise ValueError("Execute each frozen step once, in order; retries require another attempt")
            # Validate prior record bytes before extending their lineage.
            for ref in state["invocations"]:
                bound_file(self.base, ref)
            step = self.task["steps"][index]
            cwd = beneath(self.root, step["cwd"])
            invocation_id = "inv-" + uuid.uuid4().hex
            out = self.base / "invocations" / invocation_id
            out.mkdir(parents=True, exist_ok=False)
            _, before_ref = self._boundary("acceptance_before", invocation_id)
            env = dict(os.environ)
            frozen = next((s for s in self.spec.get("steps", []) if s.get("step_id") == step_id), {})
            env.update(frozen.get("environment", {}))
            if java_home:
                env["JAVA_HOME"] = str(Path(java_home).resolve())
                env["PATH"] = str(Path(java_home).resolve() / "bin") + os.pathsep + env.get("PATH", "")
            effective_argv = list(step["argv"])
            if maven_bin:
                if step["runner"] != "maven" or step["argv"][0] != "mvn" or not Path(maven_bin).is_absolute():
                    raise ValueError("maven-bin only resolves a literal mvn executable")
                effective_argv[0] = str(Path(maven_bin).resolve())
            wrapper = step["runner"] == "maven" and step["argv"][0] != "mvn"
            wrapper_before, wrapper_before_ref, wrapper_ready, wrapper_failure = None, None, False, None
            if wrapper:
                wrapper_before, wrapper_before_ref = self._wrapper_snapshot(step, env, out, invocation_id, "acceptance_before")
                try:
                    snapshot_inputs(review_for(self.spec, step), self.task, step, wrapper_before,
                                    self.run_id, invocation_id, "acceptance_before",
                                    environment_review=jvm_inputs.environment_review_for(self.spec, step))
                    wrapper_ready = True
                except (ValueError, KeyError, TypeError) as exc:
                    wrapper_failure = str(exc)
            runtime = {}
            native_capability = None
            needs_native_image = requires_native_image(self.spec, step_id)
            if step["runner"] in {"maven", "gradle"} and (not wrapper or wrapper_ready):
                probe_env = dict(env)
                if needs_native_image:
                    # Ask the actual launcher JVM to print its home. This probe-only
                    # observation flag never changes the frozen build environment.
                    option = "MAVEN_OPTS" if step["runner"] == "maven" else "JAVA_OPTS"
                    probe_env[option] = (probe_env.get(option, "") + " -XshowSettings:properties").strip()
                runtime["launcher_probe"] = self._probe([effective_argv[0], "--version"], cwd, probe_env, out, timeout=max(.001, min(30, timeout - (time.monotonic() - preparation_tick))))
                if needs_native_image:
                    runtime["launcher_probe"]["observation_option"] = {"environment": option, "append": "-XshowSettings:properties"}
                    native_capability = self._native_image_probe(runtime["launcher_probe"], invocation_id, cwd, env, out,
                                                                max(.001, min(30, timeout - (time.monotonic() - preparation_tick))))
            elif not wrapper and step.get("java_major") is not None:
                runtime["launcher_probe"] = self._probe(["java", "-version"], cwd, env, out)
            if wrapper and not wrapper_ready:
                runtime["launcher_probe"] = {"exit_code": None, "status": "not_observed",
                                              "reason": "Wrapper source or environment inputs are unreviewed"}
            inputs = self._effective_execution(step, env, out)
            if wrapper and wrapper_failure:
                inputs["errors"].append(wrapper_failure)
            if wrapper and wrapper_ready:
                try:
                    probe = runtime["launcher_probe"]
                    inputs = certify_launcher(review_for(self.spec, step), self.task, step, wrapper_before,
                                              probe, bound_file(self.base, probe).read_bytes(), self.run_id, invocation_id)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    inputs["errors"].append(str(exc))
            preparation_error = None
            if quiesce_gradle and step['runner'] == 'gradle':
                runtime['daemon_quiescence'] = self._quiesce_gradle(effective_argv[0], cwd, env, out,
                    max(.001, min(30, timeout-(time.monotonic()-preparation_tick))))
                if runtime['daemon_quiescence']['status'] != 'quiescent':
                    preparation_error = 'Gradle daemon quiescence unavailable'
            # help:evaluate loads the project's extensions and can resolve cold
            # dependencies. Charge it to the remaining step budget, not the
            # short --version probe budget; the command receives only what remains.
            local_repository = self._local_repository(step, effective_argv, cwd, env, out, max(.001, timeout - (time.monotonic() - preparation_tick)), inputs)
            observed_repository = local_repository if local_repository and local_repository.get("status") == "observed" else None
            native_input = self._native_input(step, cwd, out, state)
            collection_errors = []
            try:
                before = self._snapshot(step, observed_repository)
            except (OSError, ValueError) as exc:
                before = None
                collection_errors.append("before inventory: " + str(exc))
            compilation_before = self._compilation_snapshot(step, invocation_id, out, "before")
            outputs_before = self._output_inventory(step, invocation_id, out, "before")
            started, tick = now(), time.monotonic()
            status, code = "unavailable", None
            log_path = out / "command.log"
            witness_request, witness_ref, witness_error = None, None, None
            if maven_witness and step['runner'] == 'maven':
                from . import maven_observation
                try:
                    witness_request = maven_observation.prepare(self.base, self.root, self.run_id,
                        invocation_id, self.task['sha'], log_path)
                    env.update(witness_request['environment'])
                except (OSError, ValueError, KeyError) as exc:
                    witness_error = str(exc)
            with log_path.open("wb") as log:
                try:
                    if preparation_error:
                        raise OSError(preparation_error)
                    proc = subprocess.Popen(effective_argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        code, status = proc.wait(timeout=max(.001, timeout - (time.monotonic() - preparation_tick))), "completed"
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        code, status = proc.wait(), "timeout"
                except OSError as exc:
                    log.write((type(exc).__name__ + ": " + str(exc)).encode())
            finished, elapsed = now(), time.monotonic() - tick
            if witness_request:
                try:
                    witness_ref = maven_observation.collect(self.base, out, witness_request)
                except (OSError, ValueError, KeyError) as exc:
                    witness_error = str(exc)
            compilation_after = self._compilation_snapshot(step, invocation_id, out, "after")
            outputs_after = self._output_inventory(step, invocation_id, out, "after")
            _, after_ref = self._boundary("acceptance_after", invocation_id)
            if not wrapper:
                final_out = out / "inputs-after"
                final_out.mkdir()
                final_inputs = self._effective_execution(step, env, final_out)
                comparable = lambda value: {k: v for k, v in value.items() if k != "configuration_evidence"}
                if comparable(inputs) != comparable(final_inputs):
                    inputs.update(inputs_complete=False, serial=False)
                    inputs.setdefault("errors", []).append("Effective execution inputs changed during invocation")
                inputs["configuration_evidence"].extend(final_inputs.get("configuration_evidence", []))
            if wrapper:
                wrapper_after, wrapper_after_ref = self._wrapper_snapshot(step, env, out, invocation_id, "acceptance_after")
                wrapper_evidence = {"before": wrapper_before_ref, "after": wrapper_after_ref,
                                    "launcher_probe": runtime.get("launcher_probe")}
                try:
                    snapshot_inputs(review_for(self.spec, step), self.task, step, wrapper_before,
                                    self.run_id, invocation_id, "acceptance_before",
                                    environment_review=jvm_inputs.environment_review_for(self.spec, step))
                    snapshot_inputs(review_for(self.spec, step), self.task, step, wrapper_after,
                                    self.run_id, invocation_id, "acceptance_after",
                                    environment_review=jvm_inputs.environment_review_for(self.spec, step))
                    probe = runtime["launcher_probe"]
                    inputs = certify_launcher(review_for(self.spec, step), self.task, step, wrapper_before,
                                              probe, bound_file(self.base, probe).read_bytes(), self.run_id,
                                              invocation_id, wrapper_after,
                                              environment_review=jvm_inputs.environment_review_for(self.spec, step))
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    inputs.update(inputs_complete=False, serial=False, wrapper_reviewed=False)
                    inputs.setdefault("errors", []).append(str(exc))
                inputs["errors"] = list(dict.fromkeys(inputs["errors"]))
                inputs["wrapper_evidence"] = wrapper_evidence
            reports, artifacts = [], []
            try:
                files = self._files(step, observed_repository)
                for key, (path, metadata) in files.items():
                    if before is None:
                        continue  # Unknown before evidence cannot certify freshness.
                    fingerprint = _file_fingerprint(path)
                    fresh = before.get(key) != fingerprint
                    if not fresh and key[0] != "installed":
                        continue
                    destination = out / "collected" / key[0] / key[1]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
                    if file_digest(destination) != fingerprint[0] or _file_fingerprint(path) != fingerprint:
                        raise ValueError("Evidence changed while it was being archived: " + key[1])
                    ref = reference(self.base, destination, producer_relative_path=key[1], fresh=fresh,
                                    freshness={"before": before.get(key), "after": fingerprint}, **metadata)
                    (reports if key[0] == "report" else artifacts).append(ref)
            except (OSError, ValueError) as exc:
                collection_errors.append(str(exc))
            record = {"schema_version": 2, "recorder_version": RECORDER_VERSION, "run_id": self.run_id,
                      "execution_origin": state.get("execution_origin"),
                      "evaluation_identity": self.identity, "commit": self.task["sha"], "repo": self.task["repo"],
                      "invocation_id": invocation_id, "step_id": step_id, "sequence": index,
                      "project_root": str(self.root),
                      "runner": step["runner"], "argv": step["argv"], "effective_argv": effective_argv,
                      "cwd": step["cwd"], "status": status, "exit_code": code,
                      "started_at": started, "finished_at": finished, "command_seconds": elapsed,
                      "log": reference(self.base, log_path), "log_complete": status in {"completed", "timeout"},
                      "runtime": runtime, "effective_execution": inputs, "reports": reports, "artifacts": artifacts,
                      "native_image_probe": native_capability,
                      "local_repository": local_repository, "native_executable": native_input, "collection_errors": collection_errors,
                      "reports_collection_complete": not collection_errors,
                      "preparation_seconds": tick - preparation_tick, "step_seconds": time.monotonic() - preparation_tick,
                      "predecessor_sha256": state["invocations"][-1]["sha256"] if state["invocations"] else None,
                      "input_policy": {"stdin": "DEVNULL", "quiesce_gradle": quiesce_gradle}}
            if compilation_before or compilation_after:
                record["compilation_evidence"] = {"before": compilation_before, "after": compilation_after}
            if maven_witness:
                record['input_policy']['maven_witness'] = True
                record['maven_observation'] = witness_ref
                record['maven_observation_error'] = witness_error
            if outputs_before or outputs_after:
                record["output_inventory"] = {"before": outputs_before, "after": outputs_after}
            record_path = out / "invocation.json"
            write_json(record_path, record)
            state["invocations"].append(reference(self.base, record_path))
            state["worktree"].extend([before_ref, after_ref])
            write_json(self.base / "run-open.json", state)
            return record

    def close(self):
        with recording_lock(self.base):
            state = self._state(require_ready=False)
            _, close_ref = self._boundary("evidence_close")
            state["worktree"].append(close_ref)
            state["finished_at"] = now()
            state["status"] = "closed"
            state["telemetry"] = None  # No observation of the agent's human-input channels.
            for ref in state["invocations"] + state["worktree"]:
                bound_file(self.base, ref)
            write_json(self.base / "run.json", state)
            return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "step", "close"])
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--agent", default="unspecified")
    parser.add_argument("--step-id")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--maven-bin", type=Path)
    parser.add_argument("--quiesce-gradle", action="store_true",
                        help="Opt-in container experiment preparation: stop Gradle daemons before replay")
    parser.add_argument('--maven-witness', action='store_true',
                        help='Use the prospectively frozen common Maven observer')
    args = parser.parse_args()
    recorder = Recorder(task=load_json(args.task), requirements=load_json(args.requirements), repo_root=args.repo_root,
                        records=args.records, run_id=args.run_id)
    if args.action == "start":
        result = recorder.start(agent=args.agent)
    elif args.action == "close":
        result = recorder.close()
    else:
        if not args.step_id or args.timeout is None:
            parser.error("step requires --step-id and --timeout")
        result = recorder.step(args.step_id, timeout=args.timeout, java_home=args.java_home,
                               maven_bin=args.maven_bin, quiesce_gradle=args.quiesce_gradle,
                               maven_witness=args.maven_witness)
    response = {"run_id": args.run_id, "action": args.action,
                "status": result.get("status", result.get("admission")), "records": str(args.records)}
    if args.action == "step":
        # The caller uses the command's outcome to dispatch subsequent steps;
        # the recorder process exiting zero only means recording succeeded.
        response["exit_code"] = result.get("exit_code")
    print(json.dumps(response))


if __name__ == "__main__":
    main()
