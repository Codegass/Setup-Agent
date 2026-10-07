"""Source-bound evidence for one reviewed Maven launcher template.

This is not a shell interpreter. Unsupported or changed launchers stay unknown;
collectors still execute the original task. No environment secrets are retained.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from pathlib import PurePosixPath
import re
import shlex

from . import jvm_inputs

POLICY = "apache-maven-only-script-exec-v1"
SCRIPT_SHA256 = "cae96cef89ebea3531221f4ae17c23cf8edf67d00eae8306d4186ae1bbed4d02"
PROPERTIES = ".mvn/wrapper/maven-wrapper.properties"
OPTIONAL_INPUTS = (".mvn/maven.config", ".mvn/jvm.config", ".mvn/extensions.xml")
REVIEW_BASIS = (
    "Apache Maven Wrapper only-script 3.3.4: reviewed script lines 108-146 read "
    "pinned distribution properties; lines 148-150 exec the selected Maven with "
    "unchanged $@; both cached (155) and downloaded (295) paths use that function. "
    "All checkout .mvn inputs must remain pinned; actual launcher version and "
    "startup/environment inputs are observed independently for every invocation."
)


DEVELOCITY_STATE = {
    "path": ".mvn/.develocity/develocity-workspace-id",
    "role": "workspace_identifier",
    "format": "base32_lower_unpadded_26",
    "producer": {
        "group_id": "com.gradle",
        "artifact_id": "develocity-maven-extension",
        "version": "2.5.0",
    },
    "reviewed_sources": {
        "launcher_state_producer_jar": {
            "sha256": "c7879095d411bb6dcbe79d4ca3d25a1e92a26e290d4087f19e1150e5255ce2f7",
            "bytes": 25637290,
        },
        "launcher_state_capture_class": {
            "sha256": "96c223ab8a41e09916d9e99275bcde0d1d9adf6046ebc63a994dc059822a1c55",
            "bytes": 6950,
        },
        "launcher_state_codec_class": {
            "sha256": "c3ef7d84203bcaf01e717d7d88f8da423481ed0e7a99d555ca2ed24f971b11c3",
            "bytes": 3163,
        },
    },
    "review_basis": "Develocity 2.5.0 workspace capture/p/b.c(File) constructs this exact path; b(File,Path,int) atomically writes its workspace identifier. common/g/a/c generates a random UUID and encodes it with BaseEncoding.base32().lowerCase().omitPadding(), validates [a-z2-7]{26}, and writes those 26 UTF-8 bytes. This identifies a Build Scan workspace, not Maven goals/options. Before/after state bytes remain evidence; unrelated or invalid files are never exempted.",
}


LEGACY_POLICY = "apache-maven-wrapper-jar-exec-v1"
LEGACY_SCRIPT_SHA256 = "94793c07cfa412621d5bca972fa04296cd46e32e0e53c7ebe7fc83bd6bbecaf7"
LEGACY_JAR_STATE = {
    "path": ".mvn/wrapper/maven-wrapper.jar",
    "role": "reviewed_launcher_binary",
    "format": "sha256_bytes",
    "bytes": 62547,
    "sha256": "e63a53cfb9c4d291ebe3c2b0edacb7622bbc480326beaa5a0456e412f52f066a",
    "producer": {"group_id": "org.apache.maven.wrapper", "artifact_id": "maven-wrapper", "version": "3.2.0"},
    "review_basis": "Pinned Maven Wrapper 3.2.0 jar from Maven Central. MavenWrapperMain.main forwards args to WrapperExecutor.execute and BootstrapMainStarter.start unchanged, then invokes ClassWorlds Launcher.main. Both project and user maven.properties must be absent; extra JVM and wrapper environment inputs remain constrained.",
}
LEGACY_REVIEW_BASIS = (
    "Apache Maven Wrapper 3.2.0 script: fixed jar URL/properties; exec Java with "
    "unchanged argv into the byte-pinned wrapper jar. Reviewed WrapperExecutor "
    "and BootstrapMainStarter preserve args. Project/user maven.properties are "
    "separate inputs and must be observed absent. All actual launcher source "
    "and binary bytes, runtime versions and environment are checked per invocation."
)
DEVELOCITY_1222_STATE = json.loads(json.dumps(DEVELOCITY_STATE))
DEVELOCITY_1222_STATE["producer"]["version"] = "1.22.2"
DEVELOCITY_1222_STATE["reviewed_sources"].update({
    "launcher_state_producer_jar": {"sha256": "e3be83e090b07defa7552d45d3124c117c9d9e17d55fd43323738bc56b993592", "bytes": 22916759},
    "launcher_state_capture_class": {"sha256": "51cf227b534d894801617a2361dd050789c884ba32c0c80d97a27c8d1c01e7bd", "bytes": 6945},
})
DEVELOCITY_1222_STATE["review_basis"] = "Develocity 1.22.2 capture/o/b.c(File) selects the exact .mvn/.develocity/develocity-workspace-id path; b(File,Path,int) atomically writes its identifier. The codec class is byte-identical to the reviewed 2.5.0 codec: UUID encoded as 26 lower-case unpadded base32 characters, not build options. All raw boundary bytes remain recorded."


def _legacy(files):
    return hashlib.sha256(files.get("mvnw", b"")).hexdigest() == LEGACY_SCRIPT_SHA256


def _extension_runtime_state(files):
    """Recognize only a source-reviewed producer declared in the pinned inputs."""
    import xml.etree.ElementTree as ET

    raw = files.get(".mvn/extensions.xml")
    # A deliberately tracked identifier remains immutable source input. The
    # runtime-state exception applies only to the producer's generated file.
    if raw is None or DEVELOCITY_STATE["path"] in files:
        return []
    root = ET.fromstring(raw)
    if root.tag.rsplit("}", 1)[-1] != "extensions":
        return []
    matching = []
    for item in root:
        if item.tag.rsplit("}", 1)[-1] != "extension":
            continue
        values = {
            name: [
                c.text.strip() if c.text else "" for c in item if c.tag.rsplit("}", 1)[-1] == name
            ]
            for name in ("groupId", "artifactId", "version")
        }
        if values["groupId"] == ["com.gradle"] and values["artifactId"] == [
            "develocity-maven-extension"
        ]:
            matching.append(values["version"])
    profile = {"2.5.0": DEVELOCITY_STATE, "1.22.2": DEVELOCITY_1222_STATE}
    return [json.loads(json.dumps(profile[matching[0][0]]))] if len(matching) == 1 and len(matching[0]) == 1 and matching[0][0] in profile else []


def runtime_state_for_files(files):
    states = _extension_runtime_state(files)
    if _legacy(files) and LEGACY_JAR_STATE["path"] not in files:
        states.insert(0, json.loads(json.dumps(LEGACY_JAR_STATE)))
    return states


def _path(path):
    if not isinstance(path, str) or "\0" in path:
        raise ValueError("Invalid wrapper input path")
    parsed = PurePosixPath(path)
    if str(parsed) != path or parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("Wrapper input path escapes its scope")
    if path != "mvnw" and not path.startswith(".mvn/"):
        raise ValueError("Wrapper review may only bind mvnw and .mvn inputs")
    return path


def _properties(raw, legacy=False):
    values = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key in values or key != key.strip() or value != value.strip():
            raise ValueError("Unsupported wrapper properties syntax")
        values[key] = value
    if legacy:
        if set(values) - {"distributionUrl", "wrapperUrl", "distributionSha256Sum", "wrapperSha256Sum"}:
            raise ValueError("Unreviewed legacy wrapper property")
        if values.get("wrapperUrl") != "https://repo.maven.apache.org/maven2/org/apache/maven/wrapper/maven-wrapper/3.2.0/maven-wrapper-3.2.0.jar":
            raise ValueError("Legacy wrapper binary URL is not reviewed")
        if "wrapperSha256Sum" in values and values["wrapperSha256Sum"].lower() != LEGACY_JAR_STATE["sha256"]:
            raise ValueError("Legacy wrapper binary checksum differs")
        values = {k: v for k, v in values.items() if k not in {"wrapperUrl", "wrapperSha256Sum"}}
        values.update(wrapperVersion="3.3.4", distributionType="only-script")
    if values.get("wrapperVersion") != "3.3.4" or values.get("distributionType") != "only-script":
        raise ValueError("Wrapper delegation is outside the reviewed only-script profile")
    if set(values) - {
        "wrapperVersion",
        "distributionType",
        "distributionUrl",
        "distributionSha256Sum",
    }:
        raise ValueError("Unreviewed wrapper property")
    match = re.fullmatch(
        r"https://repo\.maven\.apache\.org/maven2/org/apache/maven/apache-maven/"
        r"(\d+\.\d+\.\d+)/apache-maven-\1-bin\.zip",
        values.get("distributionUrl", ""),
    )
    if not match:
        raise ValueError("Only a pinned Apache Maven distribution is reviewed")
    if "distributionSha256Sum" in values and not re.fullmatch(
        "[a-fA-F0-9]{64}", values["distributionSha256Sum"]
    ):
        raise ValueError("Invalid distribution checksum")
    return match[1]


def make_launcher_review(commit, files):
    """Make an explicit review from already archived, pinned raw source bytes.

    Callers retain their byte references; this function never discovers or
    approves arbitrary wrappers by repository identity or version banner.
    """
    legacy = _legacy(files)
    if not legacy and hashlib.sha256(files.get("mvnw", b"")).hexdigest() != SCRIPT_SHA256:
        raise ValueError("Maven wrapper source template has not been reviewed")
    version = _properties(files[PROPERTIES], legacy)
    review = {
        "policy": LEGACY_POLICY if legacy else POLICY,
        "source_commit": commit,
        "launcher_path": "mvnw",
        "forwarding": "exec_maven_argv_unchanged",
        "maven_version": version,
        "review_basis": LEGACY_REVIEW_BASIS if legacy else REVIEW_BASIS,
        "files": [
            {"path": _path(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            for path, raw in sorted(files.items())
        ],
        "absent_paths": [path for path in OPTIONAL_INPUTS if path not in files],
    }
    states = runtime_state_for_files(files)
    if states:
        review["runtime_state"] = states
    validate_launcher_review(review, commit, {"runner": "maven", "argv": ["./mvnw"], "cwd": "."})
    return review


def validate_launcher_review(review, commit, step):
    if (
        not isinstance(review, dict)
        or review.get("policy") not in {POLICY, LEGACY_POLICY}
        or review.get("source_commit") != commit
        or re.fullmatch("[0-9a-f]{40}", str(commit)) is None
        or review.get("launcher_path") != "mvnw"
        or review.get("forwarding") != "exec_maven_argv_unchanged"
        or not isinstance(review.get("review_basis"), str)
        or not review["review_basis"].strip()
        or re.fullmatch(r"\d+\.\d+\.\d+", str(review.get("maven_version"))) is None
        or step.get("runner") != "maven"
        or step.get("argv", [None])[0] != "./mvnw"
        or step.get("cwd", ".") != "."
    ):
        raise ValueError("Unsupported source-bound launcher review")
    entries = review.get("files")
    if not isinstance(entries, list):
        raise ValueError("Wrapper input inventory is missing")
    files = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid wrapper source binding")
        path = _path(entry.get("path"))
        if (
            path in files
            or not re.fullmatch("[0-9a-f]{64}", str(entry.get("sha256")))
            or type(entry.get("bytes")) is not int
            or entry["bytes"] < 0
        ):
            raise ValueError("Invalid or duplicate wrapper source binding")
        files[path] = entry
    legacy = review["policy"] == LEGACY_POLICY
    if (
        files.get("mvnw", {}).get("sha256") != (LEGACY_SCRIPT_SHA256 if legacy else SCRIPT_SHA256)
        or files.get("mvnw", {}).get("bytes") != (11289 if legacy else 11790)
        or PROPERTIES not in files
        or ".mvn/wrapper/maven-wrapper.jar" in files
        or review.get("absent_paths") != [p for p in OPTIONAL_INPUTS if p not in files]
    ):
        raise ValueError("Wrapper template or input closure has not been reviewed")
    states = review.get("runtime_state", [])
    prefix = [LEGACY_JAR_STATE] if legacy else []
    if states not in (prefix, prefix + [DEVELOCITY_STATE], prefix + [DEVELOCITY_1222_STATE]):
        raise ValueError("Unreviewed runtime state cannot be excluded from launcher inputs")
    if len(states) > len(prefix) and ".mvn/extensions.xml" not in files:
        raise ValueError("Unreviewed runtime state producer declaration is absent")
    if any(state["path"] in files for state in states):
        raise ValueError("Tracked files remain launcher inputs, not runtime state")
    return review


def capture_wrapper_inputs(request):
    """Fixed stdlib program used locally and through SAG's clean executor.

    Configuration bytes, raw Git probes and safe environment observations are
    retained. Unsupported environment values are represented only by presence.
    """
    import base64
    import hashlib
    import json
    import os
    import pathlib
    import re
    import shlex
    import subprocess
    import pwd

    root = pathlib.Path(request["root"])
    out = {k: request[k] for k in ("run_id", "invocation_id", "boundary", "commit")}
    out.update(root=str(root), files=[], probes=[], errors=[], environment={}, runtime_state=[])
    encode = lambda raw: base64.b64encode(raw).decode("ascii")

    def safe(relative):
        path = root / relative
        for item in (path, *path.parents):
            if item.is_symlink():
                raise ValueError("Symlink launcher input")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Launcher path escapes checkout")
        return path

    def git(args):
        argv = ["git", "-C", str(root), *args]
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False
        )
        out["probes"].append(
            {
                "argv": argv,
                "exit_code": result.returncode,
                "stdout": encode(result.stdout),
                "stderr": encode(result.stderr),
            }
        )
        if result.returncode != 0:
            raise ValueError("Launcher revision probe failed")
        return result.stdout

    try:
        safe(".")
        env = {**os.environ, **request.get("runtime_environment", {})}
        denied = (
            "MAVEN_ARGS",
            "MAVEN_CONFIG",
            "MAVEN_PROJECTBASEDIR",
            "MAVEN_BASEDIR",
            "MVNW_REPOURL",
            "MAVEN_DEBUG_OPTS",
            "JAVACMD",
        )
        forbidden = [name for name in denied if env.get(name)]
        if env.get("MVNW_VERBOSE") not in (None, "", "true", "false"):
            forbidden.append("MVNW_VERBOSE")
        options = {}
        for name in ("MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS"):
            try:
                parsed = shlex.split(env.get(name, ""))
            except ValueError:
                parsed = ["unparsed"]
            approved = request.get("reviewed_environment", {}).get(name)
            if approved is not None:
                if env.get(name, "") == approved:
                    options[name] = {"reviewed_task_options": parsed}
                else:
                    forbidden.append(name)
                    options[name] = {"status": "frozen_task_value_mismatch"}
                continue
            if any(
                not re.fullmatch(
                    r"-(?:Xm[sx]\d+[kKmMgG]?|XX:(?:MaxMetaspaceSize|ReservedCodeCacheSize)=\d+[kKmMgG]?)",
                    v,
                )
                for v in parsed
            ):
                forbidden.append(name)
                options[name] = {"status": "unreviewed_value_present"}
            else:
                options[name] = {"safe_memory_options": parsed}
        rc = [
            pathlib.Path("/etc/mavenrc"),
            pathlib.Path("/usr/local/etc/mavenrc"),
            pathlib.Path(env.get("HOME", str(pathlib.Path.home()))) / ".mavenrc",
        ]
        rc_known = env.get("MAVEN_SKIP_RC") == "true" or not any(
            p.exists() or p.is_symlink() for p in rc
        )
        if not rc_known and request.get('maven_observer'):
            policy=request['maven_observer']
            expected={path:policy.get(key) for path,key in (
                ('/etc/mavenrc','activation_sha256'),('/opt/sag-maven-witness/launch.py','launcher_sha256'),
                ('/opt/sag-maven-witness/witness.jar','observer_sha256'))}
            paths=rc+[pathlib.Path('/opt/sag-maven-witness/launch.py'),pathlib.Path('/opt/sag-maven-witness/witness.jar')]
            observed={str(p):hashlib.sha256(p.read_bytes()).hexdigest() if not p.is_symlink() else 'symlink'
                      for p in paths if p.exists() or p.is_symlink()}
            out['observer_startup_hashes']=observed
            rc_known=(policy.get('policy')=='passive-maven-events-v1'
                      and all(isinstance(v,str) and len(v)==64 for v in expected.values()) and observed==expected)
        # Only hashes of path-selection inputs are needed for before/after parity.
        selection = {
            name: hashlib.sha256(env.get(name, "").encode()).hexdigest()
            for name in ("PATH", "HOME", "JAVA_HOME", "MAVEN_USER_HOME")
        }
        extra_properties = []
        if request.get("legacy_wrapper") is True:
            # JVM user.home defaults to the account directory; -Duser.home and
            # alternative JVM option channels are rejected above.
            user_dir = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
            user_maven = pathlib.Path(env.get("MAVEN_USER_HOME") or user_dir / ".m2")
            for label, path in (("project", root / "maven.properties"), ("user", user_maven / "maven.properties")):
                extra_properties.append({"scope": label, "path": str(path), "absent": not (path.exists() or path.is_symlink())})
        out["environment"] = {
            **({"legacy_system_properties": extra_properties} if request.get("legacy_wrapper") else {}),
            "observed": request.get("environment_observed") is True,
            "unreviewed_overrides": forbidden,
            "jvm_options": options,
            "startup_rc_known": rc_known,
            "selection_fingerprints": selection,
        }
        head = git(["rev-parse", "HEAD"]).decode("ascii").strip()
        if head != request["commit"]:
            raise ValueError("Launcher checkout revision differs")
        tracked = git(["ls-files", "-z", "--", "mvnw", ".mvn"])
        pinned = git(
            ["ls-tree", "-r", "--name-only", "-z", request["commit"], "--", "mvnw", ".mvn"]
        )
        current = []
        if safe("mvnw").exists():
            current.append("mvnw")
        directory = safe(".mvn")
        if directory.exists():
            if not directory.is_dir():
                raise ValueError(".mvn is not a directory")

            def walk_error(error):
                raise error

            for here, dirs, files in os.walk(directory, followlinks=False, onerror=walk_error):
                for name in dirs:
                    safe(str((pathlib.Path(here) / name).relative_to(root)))
                for name in files:
                    current.append(str((pathlib.Path(here) / name).relative_to(root)))
        expected = sorted(request["paths"])
        if (
            sorted(tracked.decode().rstrip("\0").split("\0")) != expected
            or sorted(pinned.decode().rstrip("\0").split("\0")) != expected
        ):
            raise ValueError("Tracked and pinned launcher input inventories differ")
        out["current_paths"] = sorted(current)
        state_paths = {item["path"] for item in request.get("runtime_state", [])}
        unexpected = sorted(set(current) - set(expected) - state_paths)
        missing = sorted(set(expected) - set(current))
        if unexpected or missing:
            out["errors"].append(
                "Current launcher input inventory differs: unexpected="
                + repr(unexpected)
                + "; missing="
                + repr(missing)
            )
        for item in request.get("runtime_state", []):
            path = safe(item["path"])
            state = {"path": item["path"], "present": path.exists()}
            out["runtime_state"].append(state)
            if state["present"]:
                expected_size = item["bytes"] if item.get("format") == "sha256_bytes" else 26
                if not path.is_file() or path.stat().st_size != expected_size:
                    raise ValueError("Reviewed workspace identifier or launcher binary is nonregular or has another size")
                before = path.stat()
                raw = path.read_bytes()
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ):
                    raise ValueError("Workspace identifier changed while copying")
                state.update(
                    data=encode(raw), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)
                )
                if item.get("format") == "sha256_bytes":
                    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                        raise ValueError("Reviewed launcher binary digest differs")
                elif re.fullmatch(rb"[a-z2-7]{26}", raw) is None:
                    raise ValueError("Reviewed workspace identifier has an invalid encoding")
        for relative in expected:
            path = safe(relative)
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError("Launcher input is nonregular or exceeds observation limit")
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                raise ValueError("Launcher input changed while copied")
            original = git(["show", request["commit"] + ":" + relative])
            out["files"].append(
                {"path": relative, "data": encode(raw), "pinned_data": encode(original)}
            )
    except Exception as exc:
        out["errors"].append(str(exc))
    return out


def capture_program():
    return (
        inspect.getsource(capture_wrapper_inputs)
        + "\nimport json,sys\nprint(json.dumps(capture_wrapper_inputs(json.loads(sys.argv[1])),sort_keys=True))\n"
    )


def review_for(spec, step):
    return next(
        (s.get("launcher_review") for s in spec.get("steps", []) if s.get("step_id") == step["id"]),
        None,
    )


def capture_request(
    task, step, review, root, run_id, invocation_id, boundary, environment, observed=True,
    *, environment_review=None,
):
    validate_launcher_review(review, task["sha"], step)
    if environment_review is not None:
        jvm_inputs.validate_environment_review(environment_review, task, step)
    return {
        "root": str(root),
        "commit": task["sha"],
        "run_id": run_id,
        "invocation_id": invocation_id,
        "boundary": boundary,
        "paths": [entry["path"] for entry in review["files"]],
        "legacy_wrapper": review["policy"] == LEGACY_POLICY,
        "runtime_state": review.get("runtime_state", []),
        "runtime_environment": environment,
        "environment_observed": observed,
        "reviewed_environment": {name: item["value"] for name, item in
                                 (environment_review or {}).get("values", {}).items()},
    }


def snapshot_inputs(review, task, step, snapshot, run_id, invocation_id, boundary, *, environment_review=None):
    validate_launcher_review(review, task["sha"], step)
    approved = {}
    if environment_review is not None:
        jvm_inputs.validate_environment_review(environment_review, task, step)
        approved = {name: shlex.split(item["value"]) for name, item in environment_review["values"].items()}
    if not isinstance(snapshot, dict):
        raise ValueError("Launcher snapshot is not an observation object")
    if isinstance(snapshot.get("errors"), list) and snapshot["errors"]:
        raise ValueError(
            "Launcher snapshot failed: " + "; ".join(str(e) for e in snapshot["errors"])
        )
    if (
        snapshot.get("errors") != []
        or snapshot.get("run_id") != run_id
        or snapshot.get("invocation_id") != invocation_id
        or snapshot.get("boundary") != boundary
        or snapshot.get("commit") != task["sha"]
    ):
        raise ValueError("Launcher snapshot is missing, failed or belongs to another invocation")
    env = snapshot.get("environment", {})
    if not isinstance(env, dict):
        raise ValueError("Launcher environment observation is malformed")
    if (
        env.get("observed") is not True
        or env.get("startup_rc_known") is not True
        or env.get("unreviewed_overrides") != []
    ):
        raise ValueError("Launcher environment or startup inputs are unreviewed")
    if review["policy"] == LEGACY_POLICY:
        extra = env.get("legacy_system_properties")
        if (not isinstance(extra, list) or len(extra) != 2 or any(not isinstance(item, dict) for item in extra)
                or [item.get("scope") for item in extra] != ["project", "user"]
                or any(item.get("absent") is not True or not PurePosixPath(item.get("path", "")).is_absolute() for item in extra)
                or extra[0]["path"] != str(PurePosixPath(snapshot.get("root", "")) / "maven.properties")
                or any(not item["path"].endswith("/maven.properties") for item in extra)):
            raise ValueError("Legacy wrapper system-property files were not observed absent")
    option_names = {"MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS"}
    option_proof = env.get("jvm_options", {})
    selections = env.get("selection_fingerprints", {})
    if (
        not isinstance(option_proof, dict)
        or not isinstance(selections, dict)
        or set(option_proof) != option_names
        or any(
            option_proof.get(name) != {"reviewed_task_options": tokens}
            for name, tokens in approved.items()
        )
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("safe_memory_options"), list)
            or any(
                not isinstance(value, str)
                or not re.fullmatch(
                    r"-(?:Xm[sx]\d+[kKmMgG]?|XX:(?:MaxMetaspaceSize|ReservedCodeCacheSize)=\d+[kKmMgG]?)",
                    value,
                )
                for value in item["safe_memory_options"]
            )
            for name, item in option_proof.items() if name not in approved
        )
        or set(selections) != {"PATH", "HOME", "JAVA_HOME", "MAVEN_USER_HOME"}
        or any(re.fullmatch("[0-9a-f]{64}", str(value)) is None for value in selections.values())
    ):
        raise ValueError("Launcher safe environment proof is incomplete")
    observed = snapshot.get("files", [])
    expected = {entry["path"]: entry for entry in review["files"]}
    if (
        not isinstance(observed, list)
        or any(not isinstance(entry, dict) for entry in observed)
        or len(observed) != len(expected)
        or {entry.get("path") for entry in observed} != set(expected)
    ):
        raise ValueError("Launcher source inventory is incomplete")
    raw_files = {}
    for entry in observed:
        raw = base64.b64decode(entry["data"], validate=True)
        pinned = base64.b64decode(entry["pinned_data"], validate=True)
        binding = expected[entry["path"]]
        if (
            raw != pinned
            or len(raw) != binding["bytes"]
            or hashlib.sha256(raw).hexdigest() != binding["sha256"]
        ):
            raise ValueError("Launcher inputs differ from reviewed pinned bytes")
        raw_files[entry["path"]] = raw
    declared_states = review.get("runtime_state", [])
    available_states = runtime_state_for_files(raw_files)
    if declared_states and declared_states != available_states:
        raise ValueError("Runtime state producer differs from the pinned extension declaration")
    states = snapshot.get("runtime_state", [])
    if (
        not isinstance(states, list)
        or len(states) != len(declared_states)
        or any(not isinstance(item, dict) for item in states)
        or [item.get("path") for item in states] != [item["path"] for item in declared_states]
    ):
        raise ValueError("Runtime state observations are incomplete")
    present = []
    for state, declaration in zip(states, declared_states):
        if type(state.get("present")) is not bool:
            raise ValueError("Runtime state presence was not observed")
        if state["present"]:
            raw = base64.b64decode(state["data"], validate=True)
            if (
                (len(raw) != declaration["bytes"] or hashlib.sha256(raw).hexdigest() != declaration["sha256"]
                 if declaration.get("format") == "sha256_bytes" else re.fullmatch(rb"[a-z2-7]{26}", raw) is None)
                or state.get("bytes") != len(raw)
                or state.get("sha256") != hashlib.sha256(raw).hexdigest()
            ):
                raise ValueError("Runtime state bytes do not match the reviewed identifier format")
            present.append(state["path"])
        elif any(key in state for key in ("data", "bytes", "sha256")):
            raise ValueError("Absent runtime state has conflicting bytes")
    if review["policy"] == LEGACY_POLICY and boundary == "acceptance_after" and LEGACY_JAR_STATE["path"] not in present:
        raise ValueError("Legacy wrapper execution lacks the reviewed launcher binary at close")
    current = snapshot.get("current_paths")
    if current != sorted([*expected, *present]):
        raise ValueError("Current launcher inventory includes missing or unreviewed inputs")
    probes = snapshot.get("probes", [])
    root = snapshot.get("root")
    paths = sorted(expected)
    commands = [
        ["rev-parse", "HEAD"],
        ["ls-files", "-z", "--", "mvnw", ".mvn"],
        ["ls-tree", "-r", "--name-only", "-z", task["sha"], "--", "mvnw", ".mvn"],
        *[["show", task["sha"] + ":" + path] for path in paths],
    ]
    if (
        not isinstance(root, str)
        or not PurePosixPath(root).is_absolute()
        or not isinstance(probes, list)
        or any(not isinstance(probe, dict) for probe in probes)
        or len(probes) != len(commands)
    ):
        raise ValueError("Launcher raw revision proof is incomplete")
    values = []
    for probe, command in zip(probes, commands):
        if (
            probe.get("argv") != ["git", "-C", root, *command]
            or type(probe.get("exit_code")) is not int
            or probe["exit_code"] != 0
        ):
            raise ValueError("Launcher raw revision probe failed")
        values.append(base64.b64decode(probe["stdout"], validate=True))
    if (
        values[0].decode().strip() != task["sha"]
        or any(not value.endswith(b"\0") for value in values[1:3])
        or any(sorted(v.decode().rstrip("\0").split("\0")) != paths for v in values[1:3])
        or values[3:] != [raw_files[p] for p in paths]
    ):
        raise ValueError("Launcher raw revision bytes disagree")
    if _properties(raw_files[PROPERTIES], review["policy"] == LEGACY_POLICY) != review["maven_version"]:
        raise ValueError("Launcher distribution differs from review")
    config = shlex.split(raw_files.get(".mvn/maven.config", b"").decode(), comments=True)
    jvm = shlex.split(raw_files.get(".mvn/jvm.config", b"").decode(), comments=True)
    args = step["argv"][1:] + config
    if any(a.startswith(("-D", "@")) for a in jvm) or any(a.startswith("@") for a in args):
        raise ValueError("Additional JVM/argfile inputs are not reviewed")
    if any(
        a == "--file"
        or a.startswith("--file=")
        or (a.startswith("-f") and a not in {"-fae", "-fn", "-ff"})
        for a in args
    ):
        raise ValueError("Wrapper project selector is not reviewed")
    return {
        "argv": step["argv"],
        "maven_config": config,
        "maven_args": [],
        "jvm_config": jvm,
        "serial": not any(a.startswith(("-T", "--threads")) for a in args),
        "environment": env,
        "root": root,
    }


def certify_launcher(
    review, task, step, before, probe, probe_bytes, run_id, invocation_id, after=None,
    *, environment_review=None,
):
    inputs = snapshot_inputs(review, task, step, before, run_id, invocation_id, "acceptance_before", environment_review=environment_review)
    if after is not None:
        final = snapshot_inputs(
            review, task, step, after, run_id, invocation_id, "acceptance_after", environment_review=environment_review
        )
        if inputs != final:
            raise ValueError("Launcher execution inputs changed during invocation")
    expected = str(PurePosixPath(inputs["root"]) / "mvnw")
    if (
        not isinstance(probe, dict)
        or type(probe.get("exit_code")) is not int
        or probe["exit_code"] != 0
        or probe.get("argv") != ["./mvnw", "--version"]
        or probe.get("executable") != expected
    ):
        raise ValueError("Actual reviewed wrapper launcher probe is unavailable")
    versions = set(
        re.findall(r"Apache Maven (\d+\.\d+\.\d+)\b", probe_bytes.decode(errors="replace"))
    )
    if versions != {review["maven_version"]}:
        raise ValueError("Actual launcher Maven version differs from reviewed distribution")
    return {k: v for k, v in inputs.items() if k not in {"environment", "root"}} | {
        "inputs_complete": True,
        "wrapper_reviewed": True,
        "wrapper_status": review["policy"],
        "maven_args_source": "reviewed_wrapper_environment",
        "errors": [],
    }


def certified_invocation_inputs(spec, task, step, invocation, base):
    """Revalidate archived proof before any native success/fail-fast inference."""
    from .requirements import bound_file, load_json

    previous = invocation.get("effective_execution", {})
    if step["runner"] != "maven" or step["argv"][0] == "mvn":
        return previous
    unknown = {**previous, "inputs_complete": False, "serial": False, "wrapper_reviewed": False}
    try:
        review = review_for(spec, step)
        environment_review = jvm_inputs.environment_review_for(spec, step)
        evidence = previous["wrapper_evidence"]
        before = load_json(bound_file(base, evidence["before"]))
        after = load_json(bound_file(base, evidence["after"]))
        # Preserve the primary source/configuration error even when it prevented
        # the optional launcher probe from being performed.
        snapshot_inputs(
            review,
            task,
            step,
            before,
            invocation["run_id"],
            invocation["invocation_id"],
            "acceptance_before",
            environment_review=environment_review,
        )
        snapshot_inputs(
            review,
            task,
            step,
            after,
            invocation["run_id"],
            invocation["invocation_id"],
            "acceptance_after",
            environment_review=environment_review,
        )
        probe = evidence["launcher_probe"]
        result = certify_launcher(
            review,
            task,
            step,
            before,
            probe,
            bound_file(base, probe).read_bytes(),
            invocation["run_id"],
            invocation["invocation_id"],
            after,
            environment_review=environment_review,
        )
        # Collection/input errors cannot be repaired by deleting them from a sidecar.
        result["inputs_complete"] = previous.get("inputs_complete") is True
        result["serial"] = (
            result["serial"] and result["inputs_complete"] and previous.get("serial") is True
        )
        result["errors"] = list(previous.get("errors", []))
        return {**previous, **result}
    except (KeyError, ValueError, TypeError, OSError) as exc:
        return {**unknown, "errors": [*previous.get("errors", []), str(exc)]}
