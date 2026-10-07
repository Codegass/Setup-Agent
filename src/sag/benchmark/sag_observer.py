"""Observe frozen SAG acceptance outputs without running build/test lifecycles.

The observer shares the native evidence format with the portable recorder. It
reads only frozen report roots and declared artifact paths and retains bytes on
the host. Installation may run a guarded Maven metadata goal, which can load
Maven extensions/plugins; failed inventory never becomes an empty snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import posixpath
import re
import shlex
import threading
import time
from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path, PurePosixPath

from .requirements import (
    canonical_digest,
    evaluation_identity,
    file_digest,
    validate_requirements,
    repository_artifact_path,
    requires_native_image,
)
from .native_evidence import launcher_java_home
from . import jvm_inputs
from . import compilation_evidence
from .wrapper_review import (
    review_for,
    capture_request,
    capture_program,
    snapshot_inputs,
    certify_launcher,
)

# The fixed program inventories and copies bytes. Its separate guarded probe
# branch runs only guarded metadata or version probes, never a build/test lifecycle.
_PROGRAM = r"""
import base64,hashlib,json,os,pathlib,stat,subprocess,sys,shutil,time
request=json.loads(sys.argv[1]);root=pathlib.Path(request['root'])
def safe(relative,location='checkout'):
 base=root if location=='checkout' else pathlib.Path(request['local_repository'])
 p=base/pathlib.PurePosixPath(relative)
 if pathlib.PurePosixPath(relative).is_absolute() or '..' in pathlib.PurePosixPath(relative).parts:raise ValueError('path escape')
 for q in [p,*p.parents]:
  if q.is_symlink():raise ValueError('symlink evidence path')
 if not p.resolve().is_relative_to(base.resolve()):raise ValueError('resolved path escape')
 return p
def fp(p):
 before=p.stat()
 if not stat.S_ISREG(before.st_mode):raise ValueError('nonregular evidence file')
 digest=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
 digest=digest.hexdigest()
 after=p.stat()
 a=[before.st_size,before.st_mtime_ns,before.st_ino,before.st_dev]
 b=[after.st_size,after.st_mtime_ns,after.st_ino,after.st_dev]
 if a!=b:raise ValueError('file changed during inventory')
 return {'sha256':digest,'bytes':after.st_size,'mtime_ns':after.st_mtime_ns,'inode':after.st_ino,'device':after.st_dev}
def walk_error(error):raise error
out={'files':[],'configs':[],'errors':[],'head':None}
try:
 safe('.')
 head=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,timeout=20,check=False)
 if head.returncode!=0:raise ValueError('git revision unavailable')
 out['head']=head.stdout.decode('ascii').strip()
 if out['head']!=request['commit']:raise ValueError('checkout revision differs')
 env={**os.environ,**request.get('runtime_environment',{})}
 names=('MAVEN_ARGS','MAVEN_OPTS','MAVEN_CONFIG','MAVEN_PROJECTBASEDIR','MAVEN_SKIP_RC','JAVA_TOOL_OPTIONS','_JAVA_OPTIONS','JDK_JAVA_OPTIONS')
 out['runtime_inputs']={} if request.get('redact_runtime_inputs') else {name:env.get(name,'') for name in names}
 out['environment_observed']=request.get('environment_observed') is True
 out['startup_rc_present']=any(p.exists() or p.is_symlink() for p in [pathlib.Path('/etc/mavenrc'),pathlib.Path('/usr/local/etc/mavenrc'),pathlib.Path(env.get('HOME',str(pathlib.Path.home())))/'.mavenrc'])
 if request.get('maven_observer'):
  paths=[pathlib.Path('/etc/mavenrc'),pathlib.Path('/usr/local/etc/mavenrc'),pathlib.Path(env.get('HOME',str(pathlib.Path.home())))/'.mavenrc',pathlib.Path('/opt/sag-maven-witness/launch.py'),pathlib.Path('/opt/sag-maven-witness/witness.jar')]
  out['startup_rc_hashes']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() if not p.is_symlink() else 'symlink' for p in paths if p.exists() or p.is_symlink()}
 if request['operation']=='probe':
  started=time.monotonic();out['probe_argv']=request['argv'];executable=str(safe(request['cwd'])/request['argv'][0]) if '/' in request['argv'][0] else shutil.which(request['argv'][0],path=env.get('PATH'));out['probe_executable']=str(pathlib.Path(executable).resolve()) if executable else None
  try:
   probe=subprocess.run(request['argv'],cwd=safe(request['cwd']),env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=request['timeout'],check=False)
   stdout,stderr=probe.stdout,probe.stderr;out['probe_exit_code']=probe.returncode
  except subprocess.TimeoutExpired as e:
   stdout,stderr=e.stdout or b'',e.stderr or b'';out['probe_exit_code']=None;out['probe_status']='timeout'
  except OSError as e:
   stdout,stderr=b'',str(e).encode();out['probe_exit_code']=None;out['probe_status']='launch_failed'
  out['probe_stdout']=base64.b64encode(stdout).decode('ascii');out['probe_stderr']=base64.b64encode(stderr).decode('ascii');out['probe_seconds']=time.monotonic()-started
 elif request['operation']=='chunk':
  p=safe(request['path'],request.get('storage_root','checkout'));expected=request['fingerprint'];before=p.stat()
  observed=[before.st_size,before.st_mtime_ns,before.st_ino,before.st_dev]
  if observed!=[expected[k] for k in ('bytes','mtime_ns','inode','device')]:raise ValueError('file changed before copy')
  with p.open('rb') as f:
   f.seek(request['offset']);data=f.read(request['length'])
  after=p.stat()
  if observed!=[after.st_size,after.st_mtime_ns,after.st_ino,after.st_dev]:raise ValueError('file changed while copying')
  out['data']=base64.b64encode(data).decode('ascii');out['offset']=request['offset'];out['bytes']=len(data)
 else:
  out['compilation']=capture(root,request.get('compilation_plans',[]))
  for item in request['files']:
   try:
    p=safe(item['path'],item.get('storage_root','checkout'))
    if p.exists():out['files'].append({**item,'fingerprint':fp(p)})
   except Exception as e:out['errors'].append(item['path']+':'+str(e))
  for item in request['report_dirs']:
   try:
    directory=safe(item['path'])
    if not directory.exists():continue
    if not directory.is_dir():raise ValueError('report directory is not directory')
    for current,dirs,files in os.walk(directory,followlinks=False,onerror=walk_error):
     for name in dirs:safe(str((pathlib.Path(current)/name).relative_to(root)))
     for name in files:
      if not name.startswith(item.get('filename_prefix','TEST-')) or not name.endswith('.xml'):continue
      relative=str((pathlib.Path(current)/name).relative_to(root));p=safe(relative)
      out['files'].append({**item,'path':relative,'fingerprint':fp(p)})
   except Exception as e:out['errors'].append(item['path']+':'+str(e))
  for relative in request['config_paths']:
   try:
    p=safe(relative);item={'path':relative,'present':p.exists()}
    if item['present']:
     fingerprint=fp(p)
     if fingerprint['bytes']>1024*1024:raise ValueError('configuration exceeds observation transport limit')
     raw=p.read_bytes()
     if hashlib.sha256(raw).hexdigest()!=fingerprint['sha256']:raise ValueError('configuration changed while copying')
     item.update(fingerprint=fingerprint,data=base64.b64encode(raw).decode('ascii'))
    out['configs'].append(item)
   except Exception as e:out['errors'].append(relative+':'+str(e))
  selected=request.get('selected_pom')
  if selected is not None:
   proof={'path':selected,'commit':request['commit'],'probes':[],'errors':[]};out['selected_pom']=proof
   try:
    p=safe(selected);fingerprint=fp(p)
    if fingerprint['bytes']>1024*1024:raise ValueError('selected POM exceeds observation transport limit')
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=fingerprint['sha256']:raise ValueError('selected POM changed while copying')
    proof.update(fingerprint=fingerprint,data=base64.b64encode(raw).decode('ascii'))
    commands=[['git','-C',str(root),'ls-files','--error-unmatch','-z','--',selected],['git','-C',str(root),'show',request['commit']+':'+selected]]
    for argv in commands:
     completed=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=10,check=False)
     proof['probes'].append({'argv':argv,'exit_code':completed.returncode,'stdout':base64.b64encode(completed.stdout).decode('ascii'),'stderr':base64.b64encode(completed.stderr).decode('ascii')})
   except Exception as e:proof['errors'].append(str(e))
except Exception as e:out['errors'].append(str(e))
print(json.dumps(out,sort_keys=True))
"""


def _relative(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("A frozen relative path is required")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Declared path escapes checkout")
    return str(path)


def _selectors(args):
    found = []
    for index, arg in enumerate(args):
        if arg in {"-f", "--file"}:
            found.append(args[index + 1] if index + 1 < len(args) else None)
        elif arg.startswith(("--file=", "-f=")):
            found.append(arg.partition("=")[2])
        elif arg.startswith("-f") and arg not in {"-fae", "-fn"}:
            # Maven accepts attached short-option values, but these have not
            # been reviewed as part of this narrow frozen selector support.
            found.append(None)
    return found


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


class SAGRequirementObserver:
    def __init__(self, session_dir, run_id, project_root, task_definition, requirements, execute):
        self.base = Path(session_dir).resolve()
        if self.base.is_relative_to(Path(project_root).resolve()):
            raise ValueError("Observer evidence must remain outside the tested checkout")
        if not callable(execute) or not isinstance(run_id, str) or not run_id:
            raise ValueError("Observer requires a clean executor and run identity")
        self.root, self.run_id, self.execute = project_root, run_id, execute
        self.task = deepcopy(task_definition)
        self.spec = deepcopy(validate_requirements(requirements, self.task))
        self.identity = evaluation_identity(self.spec)
        self.directory = self.base / "requirement-observer"
        self._pending, self._exports = {}, {}
        self._lock = threading.RLock()

    def _ref(self, path, **extra):
        return {
            "path": str(path.relative_to(self.base)),
            "sha256": file_digest(path),
            "bytes": path.stat().st_size,
            **extra,
        }

    def _execute(self, request):
        request = {"root": self.root, "commit": self.task["sha"],
                   "maven_observer": self.spec.get('maven_observer'), **request}
        answer = self.execute(
            shlex.join(["python3", "-c", compilation_evidence.capture_program() + "\n" + _PROGRAM, json.dumps(request)]),
            truncate_output=False,
            # Preserve the existing transport allowance outside the observed
            # subprocess deadline, so its timeout record can reach the host.
            timeout=120 + (request["timeout"] if request.get("operation") == "probe" else 0),
        )
        if (
            not isinstance(answer, dict)
            or answer.get("success") is False
            or type(answer.get("exit_code")) is not int
            or answer["exit_code"] != 0
            or answer.get("dispatch_status")
        ):
            raise ValueError("Clean evidence transport unavailable")
        payload = json.loads(answer.get("output", ""))
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("errors"), list)
            or not isinstance(payload.get("files"), list)
            or not isinstance(payload.get("configs"), list)
            or "head" not in payload
            or (not payload["errors"] and payload["head"] != self.task["sha"])
        ):
            raise ValueError("Incomplete evidence transport framing")
        return payload

    def _step(self, contract):
        if contract.get("run_id") != self.run_id or contract.get("target_sha") != self.task["sha"]:
            return None
        argv = shlex.split(contract.get("expected_argv") or "")
        for step in self.task["steps"]:
            if argv != step["argv"][1:] or contract.get("expected_cwd") != posixpath.normpath(
                posixpath.join(self.root, step.get("cwd", "."))
            ):
                continue
            runner = step["runner"]
            if runner in {"maven", "gradle"} and runner == contract.get("effective_tool"):
                return step
            if (
                runner == "native"
                and contract.get("effective_tool") == "bash"
                and (contract.get("requested_call") or {}).get("params", {}).get("command")
                == shlex.join(step["argv"])
            ):
                return step
        return None

    def _scope(self, step, local_repository=None):
        files, directories, errors = {}, {}, []
        for row in self.spec["requirements"]:
            if row["step_id"] != step["id"]:
                continue
            try:
                if row["kind"] == "test" and row.get("validation", {}).get("rule") in {"junit", "maven_invoker"}:
                    module = _relative(row.get("scope", {}).get("module_path"))
                    subtype = row.get("subtype") or "unit"
                    defaults = (
                        [
                            posixpath.join(
                                module,
                                "target",
                                (
                                    "failsafe-reports"
                                    if subtype == "integration"
                                    else "surefire-reports"
                                ),
                            )
                        ]
                        if step["runner"] == "maven"
                        else [posixpath.join(module, "build/test-results")]
                    )
                    for location in row.get("validation", {}).get("report_directories", defaults):
                        location = _relative(location)
                        item = {
                            "path": location,
                            "kind": "report",
                            "module": row.get("module"),
                            "test_kind": subtype,
                            "filename_prefix": "BUILD-" if row["validation"]["rule"] == "maven_invoker" else "TEST-",
                        }
                        directories[canonical_digest(item)] = item
                for artifact in row.get("expectations", {}).get("artifacts", []):
                    installed = row.get("validation", {}).get("rule") == "install"
                    if installed:
                        location = repository_artifact_path(artifact)
                        if not local_repository or not local_repository.get("path"):
                            raise ValueError("Effective installation repository is unavailable")
                    else:
                        location = _relative(artifact.get("path"))
                    item = {
                        "path": location,
                        "kind": "artifact",
                        "storage_root": "repository" if installed else "checkout",
                        **{
                            k: artifact[k]
                            for k in (
                                "role",
                                "module",
                                "coordinates",
                                "format",
                                "extension",
                                "classifier",
                                "repository_relative_path",
                            )
                            if k in artifact
                        },
                    }
                    files[canonical_digest(item)] = item
                    if installed and artifact.get("install_source_path"):
                        from .installed_evidence import validate_source
                        validate_source(artifact)
                        source = {k: v for k, v in item.items() if k != "repository_relative_path"}
                        source.update(path=artifact["install_source_path"], storage_root="checkout",
                                      installation_source_for=location)
                        files[canonical_digest(source)] = source
            except (KeyError, ValueError, TypeError) as exc:
                errors.append({"requirement_id": row["id"], "kind": row["kind"], "error": str(exc)})
        if step["runner"] == "native":
            location = _relative(posixpath.join(step.get("cwd", "."), step["argv"][0]))
            item = {"path": location, "kind": "native_input"}
            files[canonical_digest(item)] = item
        configs = []
        directory = _relative(step.get("cwd", "."))
        while True:
            configs.append(posixpath.join(directory, ".mvn/maven.config"))
            configs.append(posixpath.join(directory, ".mvn/jvm.config"))
            if directory == ".":
                break
            directory = posixpath.dirname(directory) or "."
        return {
            "operation": "snapshot",
            "redact_runtime_inputs": step["runner"] == "maven" and step["argv"][0] != "mvn",
            "files": list(files.values()),
            "report_dirs": list(directories.values()),
            "config_paths": configs,
            "compilation_plans": compilation_evidence.plans_for(self.spec, step["id"]),
            "local_repository": (local_repository or {}).get("path"),
            "selected_pom": (
                _relative(posixpath.join(step.get("cwd", "."), "pom.xml"))
                if step["runner"] == "maven"
                and _selectors(step["argv"][1:]) in (["pom.xml"], ["./pom.xml"])
                else None
            ),
        }, errors

    def _snapshot(self, scope):
        try:
            return self._execute({**scope, **self._environment()})
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return {"head": None, "files": [], "configs": [], "errors": [str(exc)]}

    def _environment(self):
        owner = getattr(self.execute, "__self__", None)
        resolve = getattr(owner, "_default_exec_environment", None)
        if callable(resolve):
            try:
                environment = resolve()
                if isinstance(environment, dict) and all(
                    isinstance(k, str) and isinstance(v, str) for k, v in environment.items()
                ):
                    return {"runtime_environment": environment, "environment_observed": True}
            except Exception:
                pass
        return {"environment_observed": False}

    def _wrapper_inputs(self, step, out, identity, boundary, evidence=None):
        unknown = {
            "inputs_complete": False,
            "serial": False,
            "wrapper_reviewed": False,
            "wrapper_status": "unreviewed",
            "argv": step["argv"],
            "errors": [],
        }
        evidence = deepcopy(evidence or {})
        try:
            review = review_for(self.spec, step)
            environment = self._environment()
            request = capture_request(
                self.task,
                step,
                review,
                self.root,
                self.run_id,
                identity,
                boundary,
                environment.get("runtime_environment", {}),
                environment.get("environment_observed") is True,
                environment_review=jvm_inputs.environment_review_for(self.spec, step),
            )
            request['maven_observer'] = self.spec.get('maven_observer')
            answer = self.execute(
                shlex.join(["python3", "-c", capture_program(), json.dumps(request)]),
                truncate_output=False,
                timeout=120,
            )
            if (
                not isinstance(answer, dict)
                or answer.get("success") is False
                or type(answer.get("exit_code")) is not int
                or answer["exit_code"] != 0
                or answer.get("dispatch_status")
            ):
                raise ValueError("Wrapper evidence transport unavailable")
            snapshot = json.loads(answer.get("output", ""))
            destination = out / ("wrapper-" + boundary + ".json")
            _write(destination, snapshot)
            evidence["before" if boundary == "acceptance_before" else "after"] = self._ref(
                destination
            )
            snapshot_inputs(review, self.task, step, snapshot, self.run_id, identity, boundary,
                            environment_review=jvm_inputs.environment_review_for(self.spec, step))
            if boundary == "acceptance_before":
                evidence["launcher_probe"] = self._version_probe(
                    ["./mvnw", "--version"], step, out, "wrapper-launcher"
                )
                before, after = snapshot, None
            else:
                from .requirements import bound_file, load_json

                before = load_json(bound_file(self.base, evidence["before"]))
                after = snapshot
            from .requirements import bound_file

            probe = evidence["launcher_probe"]
            inputs = certify_launcher(
                review,
                self.task,
                step,
                before,
                probe,
                bound_file(self.base, probe).read_bytes(),
                self.run_id,
                identity,
                after,
                environment_review=jvm_inputs.environment_review_for(self.spec, step),
            )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            inputs = {**unknown, "errors": [str(exc)]}
        return {**inputs, "wrapper_evidence": evidence}

    def _inputs(self, step, snapshot):
        if step["runner"] == "maven" and step["argv"][0] != "mvn":
            return {
                "inputs_complete": False,
                "serial": False,
                "wrapper_reviewed": False,
                "wrapper_status": "unreviewed",
                "argv": step["argv"],
                "errors": [],
            }
        config_args, refs, errors = [], [], []
        jvm_raw = None
        present = []
        for config in snapshot.get("configs", []):
            if not config.get("present"):
                continue
            raw = base64.b64decode(config["data"], validate=True)
            if hashlib.sha256(raw).hexdigest() != config["fingerprint"]["sha256"]:
                raise ValueError("Configuration byte hash differs")
            args = shlex.split(raw.decode(), comments=True)
            if config["path"].endswith("jvm.config"):
                jvm_raw = raw
                if any(arg.startswith(("-D", "@")) for arg in args) and not jvm_inputs.matches(self.spec, self.task, step, raw):
                    errors.append("JVM configuration may alter Maven execution")
            else:
                present.append(config["path"])
                config_args.extend(args)
        if jvm_inputs.review_for(self.spec, step) is not None and not jvm_inputs.matches(self.spec, self.task, step, jvm_raw):
            errors.append("JVM configuration differs from its frozen source review")
        known_env = snapshot.get("environment_observed") is True
        env_args = []
        if known_env:
            try:
                # This is the host-authorized environment used by this runner;
                # never read the clean control subprocess environment as runtime.
                environment = snapshot["runtime_inputs"]
                env_args = shlex.split(environment.get("MAVEN_ARGS", ""))
                errors.extend(jvm_inputs.environment_errors(self.spec, self.task, step, environment))
                if environment.get("MAVEN_CONFIG"):
                    errors.append("MAVEN_CONFIG may supply additional execution inputs")
                if (
                    environment.get("MAVEN_PROJECTBASEDIR")
                    and environment["MAVEN_PROJECTBASEDIR"] != self.root
                ):
                    errors.append("Maven project base was overridden")
                if (
                    environment.get("MAVEN_SKIP_RC") != "true"
                    and snapshot.get("startup_rc_present") is not False
                    and not jvm_inputs.observer_rc_known(self.spec, snapshot.get('startup_rc_hashes'))
                ):
                    errors.append("Maven startup rc is not reviewed")
            except Exception:
                known_env = False
        else:
            errors.append("Actual project environment unavailable")
        direct = step["runner"] == "maven" and step["argv"][0] == "mvn"
        complete = (
            known_env and direct and not snapshot.get("errors") and len(present) <= 1 and not errors
        )
        args = step["argv"] + config_args + env_args
        if any(arg.startswith("@") for arg in args):
            complete = False
            errors.append("Alternate Maven argfile configuration is not reviewed")
        selected, selection_error = self._selected_pom(step, snapshot, config_args + env_args)
        if selection_error:
            complete = False
            errors.append(selection_error)
        serial = complete and not any(a.startswith("-T") or a.startswith("--threads") for a in args)
        return {
            "inputs_complete": complete,
            "serial": serial,
            "wrapper_reviewed": direct,
            "argv": step["argv"],
            "maven_config": config_args,
            "maven_args": env_args,
            "maven_args_source": (
                "host_authorized_dispatch_environment" if known_env else "unavailable"
            ),
            "wrapper_status": "direct_maven" if direct else "unreviewed",
            "configuration_evidence": refs,
            "selected_pom": selected,
            "errors": errors,
        }

    def _selected_pom(self, step, snapshot, extra_args):
        if _selectors(extra_args):
            return None, "Runtime configuration overrides the frozen Maven project selector"
        requested = _selectors(step["argv"][1:])
        if not requested:
            return None, None
        if requested not in (["pom.xml"], ["./pom.xml"]):
            return None, "Alternate Maven project configuration is not reviewed"
        relative = _relative(posixpath.join(step.get("cwd", "."), "pom.xml"))
        try:
            proof = snapshot["selected_pom"]
            if (
                proof.get("errors")
                or proof.get("path") != relative
                or proof.get("commit") != self.task["sha"]
            ):
                raise ValueError("Frozen selected POM observation is unavailable")
            raw = base64.b64decode(proof["data"], validate=True)
            if (
                hashlib.sha256(raw).hexdigest() != proof["fingerprint"]["sha256"]
                or len(raw) != proof["fingerprint"]["bytes"]
            ):
                raise ValueError("Selected POM byte hash differs")
            expected = [
                ["git", "-C", self.root, "ls-files", "--error-unmatch", "-z", "--", relative],
                ["git", "-C", self.root, "show", self.task["sha"] + ":" + relative],
            ]
            probes = proof["probes"]
            if len(probes) != 2 or any(
                p.get("argv") != argv or type(p.get("exit_code")) is not int or p["exit_code"] != 0
                for p, argv in zip(probes, expected)
            ):
                raise ValueError("Selected POM tracked revision probes failed")
            observed = [base64.b64decode(p["stdout"], validate=True) for p in probes]
            if observed != [(relative + "\0").encode(), raw]:
                raise ValueError(
                    "Frozen selected POM is untracked or differs from its pinned bytes"
                )
            return {
                "producer_relative_path": relative,
                "sha256": proof["fingerprint"]["sha256"],
                "commit": self.task["sha"],
                "source": "frozen_cwd_pom_git_bytes",
            }, None
        except (KeyError, TypeError, ValueError) as exc:
            return None, str(exc)

    def before_contract(self, contract):
        with self._lock:
            started = time.monotonic()
            step = self._step(contract)
            identity = contract.get("contract_id")
            if step is None or re.fullmatch(r"ic-[0-9a-f]{12}", str(identity)) is None:
                return
            if identity in self._pending:
                return
            scope, errors = self._scope(step)
            before = self._snapshot(scope)
            before.update(
                run_id=self.run_id,
                contract_id=identity,
                boundary="acceptance_before",
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
            out = self.directory / identity
            _write(out / "before.json", before)
            try:
                inputs = self._inputs(step, before)
            except (KeyError, ValueError, TypeError, OSError) as exc:
                inputs = {
                    "inputs_complete": False,
                    "serial": False,
                    "wrapper_reviewed": False,
                    "errors": [str(exc)],
                }
            wrapper = step["runner"] == "maven" and step["argv"][0] != "mvn"
            if wrapper:
                inputs = self._wrapper_inputs(step, out, identity, "acceptance_before")
            # Match the public tool call's budget (600 seconds when omitted).
            # Metadata preparation remains visible in total unattended wall time.
            budget = contract.get("requested_call", {}).get("params", {}).get("timeout")
            if budget is None:
                budget = 600
            if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0:
                raise ValueError("A finite positive metadata preparation budget is required")
            local_repository = self._local_repository(step, inputs, out, timeout=budget,
                                                      launcher=contract.get("observed_launcher"))
            if local_repository and local_repository.get("status") == "observed":
                scope, errors = self._scope(step, local_repository)
                before = self._snapshot(scope)
                before.update(
                    run_id=self.run_id,
                    contract_id=identity,
                    boundary="acceptance_before",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )
                _write(out / "before.json", before)
                final_inputs = inputs if wrapper else self._inputs(step, before)
                if final_inputs != inputs:
                    final_inputs["inputs_complete"] = False
                    final_inputs["serial"] = False
                    final_inputs.setdefault("errors", []).append(
                        "Inputs changed during repository preparation"
                    )
                inputs = final_inputs
            inputs["configuration_evidence"] = [self._ref(out / "before.json")]
            pending = {
                "contract": deepcopy(contract),
                "step": step,
                "scope": scope,
                "errors": errors,
                "before": before,
                "effective_execution": inputs,
                "local_repository": local_repository,
                "preparation_seconds": time.monotonic() - started,
            }
            self._pending[identity] = pending
            _write(out / "pending.json", pending)

    def _local_repository(self, step, inputs, out, *, timeout, launcher=None):
        if step["runner"] != "maven" or not any(
            r["step_id"] == step["id"] and r.get("validation", {}).get("rule") == "install"
            for r in self.spec["requirements"]
        ):
            return None
        unknown = {
            "path": None,
            "status": "unavailable",
            "source": "maven_settings_probe",
            "exit_code": None,
        }
        if inputs.get("inputs_complete") is not True or inputs.get("wrapper_reviewed") is not True:
            return {**unknown, "reason": "Effective Maven inputs are not fully observed"}
        values = {
            "-f",
            "--file",
            "-pl",
            "--projects",
            "-P",
            "--activate-profiles",
            "-s",
            "--settings",
            "-gs",
            "--global-settings",
            "-t",
            "--toolchains",
            "-gt",
            "--global-toolchains",
            "-T",
            "--threads",
            "-rf",
            "--resume-from",
            "-D",
            "--define",
            "-l",
            "--log-file",
        }
        for source in (inputs["maven_config"], inputs["maven_args"]):
            value = False
            for arg in source:
                if value:
                    value = False
                elif arg in values:
                    value = True
                elif not arg.startswith("-"):
                    return {
                        **unknown,
                        "reason": "Configuration can append lifecycle goals to metadata probe",
                    }
            if value:
                return {**unknown, "reason": "Incomplete Maven configuration option"}
        options, value = [], False
        for arg in step["argv"][1:]:
            if value:
                options.append(arg)
                value = False
            elif arg.startswith("-"):
                options.append(arg)
                value = arg in values
        if value:
            return {**unknown, "reason": "Incomplete frozen Maven option"}
        argv = [
            launcher or step["argv"][0],
            *options,
            "help:evaluate",
            "-Dexpression=settings.localRepository",
            "-q",
            "-DforceStdout",
        ]
        try:
            payload = self._execute(
                {
                    "operation": "probe",
                    "redact_runtime_inputs": step["runner"] == "maven" and step["argv"][0] != "mvn",
                    "argv": argv,
                    "cwd": step.get("cwd", "."),
                    "timeout": timeout,
                    **self._environment(),
                }
            )
            _write(out / "local-repository.json", payload)
            stdout = base64.b64decode(payload["probe_stdout"], validate=True)
            stderr = base64.b64decode(payload["probe_stderr"], validate=True)
            (out / "local-repository.stdout").write_bytes(stdout)
            (out / "local-repository.stderr").write_bytes(stderr)
            combined = out / "local-repository.log"
            combined.write_bytes(stdout + b"\n" + stderr)
            code = payload.get("probe_exit_code")
            from .native_evidence import parse_local_repository

            location = parse_local_repository(combined.read_text(errors="replace"))
            result = {
                **unknown,
                "probe": self._ref(combined),
                "stdout": self._ref(out / "local-repository.stdout"),
                "stderr": self._ref(out / "local-repository.stderr"),
                "observation": self._ref(out / "local-repository.json"),
                "argv": argv,
                "exit_code": code,
                "executable": payload.get("probe_executable"),
                "seconds": payload.get("probe_seconds"),
                "timeout_seconds": timeout,
                "timeout_basis": "requested_tool_timeout_or_public_default",
            }
            if not payload.get("errors") and type(code) is int and code == 0 and location:
                result.update(path=location, status="observed")
            return result
        except (KeyError, ValueError, TypeError, OSError) as exc:
            return {**unknown, "reason": str(exc)}

    def _version_probe(self, argv, step, out, label):
        payload = self._execute(
            {
                "operation": "probe",
                "redact_runtime_inputs": step["runner"] == "maven" and step["argv"][0] != "mvn",
                "argv": argv,
                "cwd": step.get("cwd", "."),
                "timeout": 30,
                **self._environment(),
            }
        )
        _write(out / (label + ".json"), payload)
        if payload.get("errors"):
            raise ValueError("Version probe transport or checkout binding unavailable")
        stdout = base64.b64decode(payload["probe_stdout"], validate=True)
        stderr = base64.b64decode(payload["probe_stderr"], validate=True)
        (out / (label + ".stdout")).write_bytes(stdout)
        (out / (label + ".stderr")).write_bytes(stderr)
        combined = out / (label + ".log")
        combined.write_bytes(stdout + b"\n" + stderr)
        return self._ref(
            combined,
            argv=argv,
            exit_code=payload.get("probe_exit_code"),
            executable=payload.get("probe_executable"),
            status=payload.get("probe_status", "completed"),
            stdout=self._ref(out / (label + ".stdout")),
            stderr=self._ref(out / (label + ".stderr")),
            observation=self._ref(out / (label + ".json")),
        )

    def _native_image_probe(self, receipt, step, out):
        observed = {
            "source": "launcher_jdk_capability",
            "run_id": self.run_id,
            "invocation_id": receipt["contract_id"],
            "launcher_java_home": None,
        }
        try:
            jdk = receipt.get("effective_jdk") or {}
            dispatch = (jdk.get("provenance") or {}).get("dispatch_runtime") or {}
            executable = dispatch.get("executable")
            if (
                jdk.get("runtime_authority") != "dispatch_probe"
                or not isinstance(executable, str)
                or not posixpath.isabs(executable)
            ):
                raise ValueError("Actual dispatch JVM executable unavailable")
            probe = self._version_probe(
                [executable, "-XshowSettings:properties", "-version"], step, out, "java-home-probe"
            )
            observed["java_home_probe"] = probe
            if probe.get("exit_code") != 0:
                return observed
            home = launcher_java_home((self.base / probe["path"]).read_text(errors="replace"))
            observed["launcher_java_home"] = home
            if home is not None:
                observed["probe"] = self._version_probe(
                    [posixpath.join(home, "bin/native-image"), "--version"],
                    step,
                    out,
                    "native-image-probe",
                )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            observed["error"] = str(exc)
        return observed

    def _copy(self, item, destination):
        fingerprint = item["fingerprint"]
        temporary = destination.with_suffix(".part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        try:
            with temporary.open("wb") as stream:
                for offset in range(0, fingerprint["bytes"], 1024 * 1024):
                    length = min(1024 * 1024, fingerprint["bytes"] - offset)
                    result = self._execute(
                        {
                            "operation": "chunk",
                            "path": item["path"],
                            "storage_root": item.get("storage_root", "checkout"),
                            "local_repository": item.get("local_repository"),
                            "fingerprint": fingerprint,
                            "offset": offset,
                            "length": length,
                        }
                    )
                    if (
                        result.get("errors")
                        or result.get("offset") != offset
                        or result.get("bytes") != length
                    ):
                        raise ValueError("Evidence changed or truncated during copy")
                    data = base64.b64decode(result["data"], validate=True)
                    if len(data) != length:
                        raise ValueError("Evidence chunk length differs")
                    digest.update(data)
                    stream.write(data)
            if digest.hexdigest() != fingerprint["sha256"]:
                raise ValueError("Retained artifact hash differs")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def after_receipt(self, receipt):
        from sag.agent.receipt_structure import dispatch_terminated

        with self._lock:
            started = time.monotonic()
            identity = receipt.get("contract_id")
            pending = self._pending.get(identity)
            if pending is None or identity in self._exports:
                return
            contract = pending["contract"]
            if (
                receipt.get("run_id") != self.run_id
                or receipt.get("contract_hash") != contract.get("contract_hash")
                or receipt.get("target_sha") != self.task["sha"]
                or receipt.get("actual_cwd") != contract["expected_cwd"]
            ):
                return
            out = self.directory / identity
            after = self._snapshot(pending["scope"])
            after.update(
                run_id=self.run_id,
                contract_id=identity,
                receipt_id=receipt.get("receipt_id"),
                boundary="receipt_publication",
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
            _write(out / "after.json", after)
            snapshot_errors = pending["before"].get("errors", []) + after.get("errors", [])
            errors = list(snapshot_errors) + list(pending["errors"])
            terminal = dispatch_terminated(receipt)
            if not terminal:
                errors.append("Invocation has no normal terminal observation")
            before = {
                (f["kind"], f["path"]): f["fingerprint"] for f in pending["before"].get("files", [])
            }
            reports, artifacts, native = [], [], None
            copy_errors = {"artifact": [], "report": [], "native_input": []}
            valid_before = pending["before"].get("head") == self.task["sha"] and not pending[
                "before"
            ].get("errors")
            valid_after = after.get("head") == self.task["sha"] and not after.get("errors")
            inputs = deepcopy(pending["effective_execution"])
            wrapper = pending["step"]["runner"] == "maven" and pending["step"]["argv"][0] != "mvn"
            if wrapper:
                inputs = self._wrapper_inputs(
                    pending["step"],
                    out,
                    identity,
                    "acceptance_after",
                    inputs.get("wrapper_evidence"),
                )
            else:
                try:
                    final_inputs = self._inputs(pending["step"], after)
                    comparable = lambda value: {
                        k: v for k, v in value.items() if k != "configuration_evidence"
                    }
                    if comparable(inputs) != comparable(final_inputs):
                        inputs["inputs_complete"] = False
                        inputs["serial"] = False
                        inputs.setdefault("errors", []).append(
                            "Effective execution inputs changed during invocation"
                        )
                except (KeyError, ValueError, TypeError, OSError) as exc:
                    inputs["inputs_complete"] = False
                    inputs["serial"] = False
                    inputs.setdefault("errors", []).append(str(exc))
            inputs["configuration_evidence"] = [
                self._ref(out / "before.json"),
                self._ref(out / "after.json"),
            ]
            for index, item in enumerate(after.get("files", [])):
                try:
                    if item.get("storage_root") == "repository":
                        item = {
                            **item,
                            "local_repository": (pending.get("local_repository") or {}).get("path"),
                        }
                    if item["kind"] == "native_input":
                        previous = before.get((item["kind"], item["path"]))
                        observed = {
                            c.get("observation")
                            for c in receipt.get("capability_observations", [])
                            if c.get("feature") == "native_executable_sha256"
                            and c.get("probe_exit_code") == "0"
                        }
                        if (
                            valid_before
                            and valid_after
                            and terminal
                            and previous == item["fingerprint"]
                            and item["fingerprint"]["sha256"] in observed
                        ):
                            for producer, record in reversed(list(self._exports.items())):
                                matching = next(
                                    (
                                        a
                                        for a in record["artifacts"]
                                        if a.get("producer_relative_path") == item["path"]
                                        and a.get("sha256") == item["fingerprint"]["sha256"]
                                        and a.get("fresh") is True
                                    ),
                                    None,
                                )
                                if matching:
                                    native = {**matching, "producer_invocation_id": producer}
                                    break
                        continue
                    destination = out / "collected" / (str(index) + ".bin")
                    self._copy(item, destination)
                    meta = {
                        k: item[k]
                        for k in (
                            "module",
                            "test_kind",
                            "role",
                            "coordinates",
                            "format",
                            "extension",
                            "classifier",
                            "repository_relative_path",
                            "installation_source_for",
                        )
                        if k in item
                    }
                    previous = before.get((item["kind"], item["path"]))
                    fresh = (
                        valid_before
                        and valid_after
                        and previous != item["fingerprint"]
                        and terminal
                    )
                    ref = self._ref(
                        destination,
                        producer_relative_path=item["path"],
                        fresh=fresh,
                        freshness={"before": previous, "after": item["fingerprint"]},
                        **meta,
                    )
                    (reports if item["kind"] == "report" else artifacts).append(ref)
                except (KeyError, ValueError, TypeError, OSError) as exc:
                    errors.append(item.get("path", "unknown") + ":" + str(exc))
                    copy_errors.setdefault(item.get("kind", "artifact"), []).append(str(exc))
            artifact_observations = []
            for item in pending["scope"]["files"]:
                if item["kind"] != "artifact":
                    continue
                match = next(
                    (
                        f
                        for f in after.get("files", [])
                        if f["kind"] == "artifact" and f["path"] == item["path"]
                    ),
                    None,
                )
                artifact_observations.append(
                    {
                        "producer_relative_path": item["path"],
                        **{k: item[k] for k in ("role", "module", "coordinates") if k in item},
                        "state": (
                            "unavailable" if not valid_after else "present" if match else "missing"
                        ),
                        "fingerprint": match.get("fingerprint") if match else None,
                    }
                )
            artifact_errors = [e for e in pending["errors"] if e["kind"] != "test"]
            report_errors = [e for e in pending["errors"] if e["kind"] == "test"]
            native_capability = (
                self._native_image_probe(receipt, pending["step"], out)
                if requires_native_image(self.spec, pending["step"]["id"])
                else None
            )
            record = {
                "run_id": self.run_id,
                "project_root": self.root,
                "contract_id": identity,
                "receipt_id": receipt.get("receipt_id"),
                "evaluation_identity": self.identity,
                "effective_execution": inputs,
                "reports": reports,
                "reports_collection_complete": not snapshot_errors
                and not report_errors
                and not copy_errors["report"]
                and valid_before
                and valid_after
                and terminal,
                "artifacts": artifacts,
                "artifacts_collection_complete": not snapshot_errors
                and not artifact_errors
                and not copy_errors["artifact"]
                and valid_before
                and valid_after
                and terminal,
                "artifact_observations": artifact_observations,
                "local_repository": pending.get("local_repository"),
                "native_executable": native,
                "native_image_probe": native_capability,
                "errors": errors,
                "evidence_refs": [self._ref(out / "before.json"), self._ref(out / "after.json")],
                "preparation_seconds": pending["preparation_seconds"],
                "observation_seconds": time.monotonic() - started,
            }
            if pending["scope"].get("compilation_plans"):
                record["compilation_evidence"] = {}
                for boundary, snapshot in (("before", pending["before"]), ("after", after)):
                    path = out / ("compilation-" + boundary + ".json")
                    _write(path, {"run_id": self.run_id, "invocation_id": identity, "boundary": boundary,
                                  "observations": snapshot.get("compilation", {})})
                    record["compilation_evidence"][boundary] = self._ref(path)
            self._exports[identity] = record
            _write(out / "observation.json", record)

    def export_invocation(self, contract_id, base=None):
        if contract_id not in self._exports:
            return None
        base = self.base if base is None else Path(base).resolve()
        if not self.base.is_relative_to(base):
            raise ValueError("Export base must contain the host session evidence")
        record = deepcopy(self._exports[contract_id])
        prefix = self.base.relative_to(base)

        def rebase(value):
            if isinstance(value, dict):
                if "path" in value and "sha256" in value:
                    value["path"] = str(prefix / value["path"])
                for child in value.values():
                    rebase(child)
            elif isinstance(value, list):
                for child in value:
                    rebase(child)

        rebase(record)
        return record

    def completed_test_scope(self, contract, receipt, output):
        """Prove a frozen test scope independently of later build goals.

        This uses the same native-goal and fresh-report rules as the final
        evaluator. It does not infer a test scope from a green XML subtotal.
        Multiple test-bearing commands still need a task-level selection;
        one receipt cannot discharge those other commands.
        """
        from sag.agent.evidence_assessments import contract_receipt_binding_problem
        from sag.agent.invocation_receipts import output_content_hash
        from sag.agent.receipt_structure import dispatch_terminated
        from .evaluator import native_requirement, runtime_result
        from .native_evidence import maven_events
        from .requirements import bound_file, report_scope_errors
        from .wrapper_review import certified_invocation_inputs

        with self._lock:
            step = self._step(contract)
            tests = [r for r in self.spec["requirements"]
                     if r["kind"] == "test" and r.get("required", True)]
            if (
                step is None or not tests
                or self.spec.get("annotation_completeness", {}).get("status") != "complete"
                or any(r["step_id"] != step["id"] for r in tests)
                or contract_receipt_binding_problem(contract, receipt)
                or receipt.get("compliance") != "exact"
                or not dispatch_terminated(receipt)
                or type(receipt.get("exit_code")) is not int
                or not isinstance(output, str)
                or output_content_hash(output) != receipt.get("output_content_hash")
            ):
                return None
            observed = self.export_invocation(receipt["contract_id"])
            if (
                observed is None
                or observed.get("run_id") != self.run_id
                or observed.get("receipt_id") != receipt.get("receipt_id")
                or observed.get("evaluation_identity") != self.identity
                or observed.get("reports_collection_complete") is not True
            ):
                return None
            for ref in observed["evidence_refs"]:
                bound_file(self.base, ref)
            out = self.directory / receipt["contract_id"] / "test-completion"
            out.mkdir(parents=True, exist_ok=True)
            _write(out / "receipt.json", receipt)
            (out / "output.log").write_text(output)
            invocation = {
                **observed, "commit": self.task["sha"], "step_id": step["id"],
                "invocation_id": receipt["contract_id"], "runner": step["runner"],
                "argv": step["argv"], "observed_argv": shlex.split(receipt["argv"]),
                "cwd": step["cwd"], "status": "completed", "exit_code": receipt["exit_code"],
                "log": self._ref(out / "output.log"), "log_complete": True,
                "source_receipt": self._ref(out / "receipt.json"),
                "runtime": {"source": "sag_host_authorized_receipt",
                            "sag_dispatch_receipt": self._ref(out / "receipt.json")},
            }
            runtime = runtime_result(self.base, invocation, step,
                                     requires_native_image(self.spec, step["id"]))
            if runtime["status"] != "passed":
                return None
            invocation["effective_execution"] = certified_invocation_inputs(
                self.spec, self.task, step, invocation, self.base)
            events = (maven_events(output, terminal=True,
                                  serial=invocation["effective_execution"].get("serial") is True)
                      if step["runner"] == "maven" else [])
            issues = report_scope_errors(self.spec["requirements"],
                                         {s["id"]: s["runner"] for s in self.task["steps"]})
            outcomes = [native_requirement(r, invocation, self.base, output, events,
                                          issues.get(r["id"])) for r in tests]
            if any(r["status"] != "passed" for r in outcomes):
                return None
            proof = {"evaluation_identity": self.identity, "run_id": self.run_id,
                     "receipt_id": receipt["receipt_id"], "contract_id": receipt["contract_id"],
                     "scope": "all_required_test_pools_in_one_frozen_command",
                     "runtime": runtime, "requirements": outcomes,
                     "invocation": invocation,
                     "whole_command_success": receipt["exit_code"] == 0}
            _write(out / "proof.json", proof)
            return self._ref(out / "proof.json")
