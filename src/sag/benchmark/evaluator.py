"""Offline evaluation of trusted, byte-bound recorder records (stdlib only).

The recorder boundary is external to the agent. Hashes detect damaged/mixed
records, not a malicious author able to rewrite the recorder and every hash.
"""

from __future__ import annotations

import re
import math
import posixpath
import xml.etree.ElementTree as ET

from .wrapper_review import certified_invocation_inputs

from .native_evidence import (
    artifact_proof,
    fail_fast_observable,
    goal_name,
    junit_counts,
    maven_events,
    launcher_java_home,
)
from .requirements import (
    POLICY_VERSION,
    bound_file,
    canonical_digest,
    evaluation_identity,
    load_json,
    summarize,
    task_status,
    validate_requirements,
    normalize_task,
    report_directories,
    report_scope_errors,
    requires_native_image,
)


def result(id, status="unavailable", reason="evidence_unavailable", **extra):
    return {"id": id, "status": status, "reason": reason, **extra}


def worktree_result(base, evidence, run_id, commit, invocations):
    states, boundaries, observed, roots = [], set(), [], set()
    try:
        for ref in evidence:
            snapshot = load_json(bound_file(base, ref))
            if snapshot.get("run_id") != run_id:
                raise ValueError("Worktree snapshot belongs to another run")
            boundary = snapshot.get("boundary")
            root = snapshot.get("project_root")
            if not isinstance(root, str) or not root:
                raise ValueError("Worktree root unavailable")
            roots.add(root)
            if len(roots) != 1:
                raise ValueError("Worktree snapshots describe different roots")
            key = (boundary, snapshot.get("invocation_id"))
            boundaries.add(key)
            raw = {}
            for name in ("head", "tracked", "untracked"):
                probe = snapshot["probes"][name]
                raw[name] = bound_file(base, probe["stdout"]).read_bytes()
                bound_file(base, probe["stderr"])
                if type(probe.get("exit_code")) is not int or probe["exit_code"] != 0:
                    raise ValueError(f"{name} probe failed")
                expected = {
                    "head": ["rev-parse", "HEAD"],
                    "tracked": ["diff", "--name-only", "-z", "HEAD", "--"],
                    "untracked": ["ls-files", "--others", "--exclude-standard", "-z"],
                }[name]
                if probe.get("argv") != ["git", "-C", root, *expected]:
                    raise ValueError("Unexpected worktree probe command")
                if name != "head" and raw[name] and not raw[name].endswith(b"\0"):
                    raise ValueError("Incomplete NUL-delimited worktree paths")
            bad = raw["head"].decode().strip() != commit or bool(raw["tracked"])
            states.append("failed" if bad else "passed")
            observed.append(
                {
                    "boundary": boundary,
                    "invocation_id": snapshot.get("invocation_id"),
                    "untracked_paths": (
                        raw["untracked"].decode(errors="surrogateescape").rstrip("\0").split("\0")
                        if raw["untracked"]
                        else []
                    ),
                    "source": ref,
                }
            )
        required = {("task_start", None), ("evidence_close", None)}
        for invocation in invocations:
            required.update(
                {
                    ("acceptance_before", invocation["invocation_id"]),
                    ("acceptance_after", invocation["invocation_id"]),
                }
            )
        if not required <= boundaries:
            states.append("unavailable")
    except (KeyError, ValueError, OSError, TypeError) as exc:
        states.append("unavailable")
        error = str(exc)
    else:
        error = None
    status = task_status(states)
    state = {"complete": "passed", "incomplete": "failed", "unavailable": "unavailable"}[status]
    return result(
        "worktree_integrity",
        state,
        (
            "protected_source_changed"
            if state == "failed"
            else (
                error or "boundary_snapshots"
                if state == "passed"
                else error or "boundary_evidence_missing"
            )
        ),
        snapshots=observed,
    )


def _launcher_runtime_result(base, invocation, step):
    expected_java, expected_maven = step.get("java_major"), step.get("maven_version")
    required = bool(expected_java or expected_maven)
    if not required:
        return result(step["id"], "passed", "no_launcher_constraint")
    try:
        runtime = invocation["runtime"]
        if invocation.get("recorder_version") == "native-tool-hooks-v1":
            # A native hook sees the parent shell, not necessarily the JVM that
            # ran the build. Read the actual, byte-bound Maven session instead.
            from .native_hooks import launcher_runtime
            observed = launcher_runtime(base, invocation)
            java, maven = observed["java_major"], observed["maven_version"]
            checks = ["unavailable" if actual is None else "passed" if actual == expected else "failed"
                      for actual, expected in ((java, expected_java), (maven, expected_maven)) if expected is not None]
            state = {"complete": "passed", "incomplete": "failed", "unavailable": "unavailable"}[task_status(checks)]
            return result(step["id"], state, "native_launcher_session", expected={"java_major": expected_java, "maven_version": expected_maven},
                          observed=observed, evidence_refs=[runtime["native_launch"]])
        if runtime.get("source") == "sag_host_authorized_receipt":
            probe = runtime["sag_dispatch_receipt"]
            receipt = load_json(bound_file(base, probe))
            if (
                receipt.get("run_id") != invocation.get("run_id")
                or receipt.get("contract_id") != invocation.get("invocation_id")
                or receipt.get("receipt_id") != invocation.get("receipt_id")
            ):
                raise ValueError("Dispatch observation belongs to another invocation")
            jdk = receipt.get("effective_jdk") or {}
            dispatch = (jdk.get("provenance") or {}).get("dispatch_runtime") or {}
            fingerprint = receipt.get("toolchain_fingerprint") or {}
            java = None
            if (
                jdk.get("runtime_authority") == "dispatch_probe"
                and dispatch.get("executable")
                and dispatch.get("major") == jdk.get("major")
                and str(dispatch.get("major", "")).isdigit()
            ):
                java = int(dispatch["major"])
            match = (
                re.search(
                    r"(?:Apache Maven\s+)?(\d+\.\d+\.\d+)", str(fingerprint.get("version", ""))
                )
                if fingerprint.get("executable")
                else None
            )
            maven = match[1] if match else None
            checks = [
                "unavailable" if actual is None else "passed" if actual == value else "failed"
                for actual, value in [(java, expected_java), (maven, expected_maven)]
                if value is not None
            ]
            state = {"complete": "passed", "incomplete": "failed", "unavailable": "unavailable"}[
                task_status(checks)
            ]
            return result(
                step["id"],
                state,
                "receipt_bound_dispatch_observations",
                expected={"java_major": expected_java, "maven_version": expected_maven},
                observed={"java_major": java, "maven_version": maven},
                evidence_refs=[probe],
                probe_format="structured_dispatch_observation",
            )
        probe = runtime["launcher_probe"]
        text = bound_file(base, probe).read_text(errors="replace")
        if (
            type(probe.get("exit_code")) is not int
            or probe["exit_code"] != 0
            or not probe.get("executable")
        ):
            raise ValueError("Actual launcher probe unavailable")
        java_match = re.search(
            r"(?:Java version:\s*|(?:Launcher )?JVM:\s*|(?:java|openjdk) version\s*\")([0-9]+)(?:\.([0-9]+))?",
            text,
            re.I,
        )
        java = (
            (int(java_match[2]) if java_match[1] == "1" and java_match[2] else int(java_match[1]))
            if java_match
            else None
        )
        maven_match = re.search(r"Apache Maven\s+(\d+\.\d+\.\d+)", text)
        maven = maven_match[1] if maven_match else None
        checks = [
            "unavailable" if actual is None else "passed" if actual == required_value else "failed"
            for actual, required_value in [(java, expected_java), (maven, expected_maven)]
            if required_value is not None
        ]
        state = {"complete": "passed", "incomplete": "failed", "unavailable": "unavailable"}[
            task_status(checks)
        ]
        return result(
            step["id"],
            state,
            "launcher_versions",
            expected={"java_major": expected_java, "maven_version": expected_maven},
            observed={"java_major": java, "maven_version": maven},
            evidence_refs=[probe],
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return result(step["id"], reason=str(exc))


def native_image_result(base, invocation, step):
    """Capability of this invocation's observed JVM installation, not any PATH tool."""
    try:
        observation = invocation["native_image_probe"]
        if (
            not isinstance(observation, dict)
            or observation.get("source") != "launcher_jdk_capability"
            or observation.get("run_id") != invocation.get("run_id")
            or observation.get("invocation_id") != invocation.get("invocation_id")
        ):
            raise ValueError("Native-image observation belongs to another invocation")
        home_probe = observation["java_home_probe"]
        home_text = bound_file(base, home_probe).read_text(errors="replace")
        if type(home_probe.get("exit_code")) is not int or home_probe["exit_code"] != 0:
            raise ValueError("Launcher JVM home probe unavailable")
        runtime = invocation["runtime"]
        if runtime.get("source") == "sag_host_authorized_receipt":
            receipt = load_json(bound_file(base, runtime["sag_dispatch_receipt"]))
            jdk = receipt.get("effective_jdk") or {}
            dispatch = (jdk.get("provenance") or {}).get("dispatch_runtime") or {}
            if (
                receipt.get("run_id") != invocation.get("run_id")
                or receipt.get("contract_id") != invocation.get("invocation_id")
                or receipt.get("receipt_id") != invocation.get("receipt_id")
                or jdk.get("runtime_authority") != "dispatch_probe"
                or not dispatch.get("executable")
                or dispatch.get("major") != jdk.get("major")
                or home_probe.get("argv")
                != [dispatch["executable"], "-XshowSettings:properties", "-version"]
            ):
                raise ValueError("JVM home probe is not bound to this dispatch launcher")
            major = re.search(r'(?:java|openjdk) version "(?:1\.)?(\d+)', home_text, re.I)
            if not major or major[1] != str(dispatch.get("major")):
                raise ValueError("JVM home probe version differs from dispatch")
        else:
            original = runtime["launcher_probe"]
            if (
                home_probe.get("path") != original.get("path")
                or home_probe.get("sha256") != original.get("sha256")
                or type(original.get("exit_code")) is not int
                or original["exit_code"] != 0
                or not original.get("executable")
                or home_probe.get("argv") != original.get("argv")
                or home_probe.get("argv")
                != [(invocation.get("effective_argv") or invocation["argv"])[0], "--version"]
            ):
                raise ValueError("JVM home is not from the actual launcher probe")
        home = launcher_java_home(home_text)
        if home is None or observation.get("launcher_java_home") != home:
            raise ValueError("A unique observed launcher JVM home is unavailable")
        if runtime.get("source") == "sag_host_authorized_receipt" and (
            not isinstance(home_probe.get("executable"), str)
            or posixpath.normpath(home_probe["executable"]) != home_probe["executable"]
            or not home_probe["executable"].startswith(home + "/")
        ):
            raise ValueError("Observed Java executable differs from its reported JVM home")
        probe = observation["probe"]
        text = bound_file(base, probe).read_text(errors="replace")
        if probe.get("argv") != [posixpath.join(home, "bin/native-image"), "--version"]:
            raise ValueError("Native-image probe did not use the observed JVM home")
        executable = probe.get("executable")
        if executable is not None and (
            not isinstance(executable, str)
            or posixpath.normpath(executable) != executable
            or not executable.startswith(home + "/")
        ):
            raise ValueError("Native-image executable belongs to another JVM installation")
        code = probe.get("exit_code")
        common = {"evidence_refs": [home_probe, probe], "java_home": home, "executable": executable}
        if probe.get("status") == "launch_failed" or type(code) is int and code != 0:
            return result(step["id"], "failed", "native_image_probe_failed", **common)
        if type(code) is not int or code != 0 or not executable:
            return result(step["id"], reason="native_image_probe_unavailable", **common)
        if not re.search(r"(?:^|\n)native-image\s+\d", text, re.I):
            return result(step["id"], reason="native_image_identity_unavailable", **common)
        return result(step["id"], "passed", "observed_native_image_capability", **common)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return result(step["id"], reason=str(exc))


def runtime_result(base, invocation, step, native_image_required=False):
    launcher = _launcher_runtime_result(base, invocation, step)
    if not native_image_required:
        return launcher
    capability = native_image_result(base, invocation, step)
    state = {"complete": "passed", "incomplete": "failed", "unavailable": "unavailable"}[
        task_status([launcher["status"], capability["status"]])
    ]
    return result(
        step["id"],
        state,
        "launcher_and_native_image_capability",
        launcher=launcher,
        native_image=capability,
    )


def matching_reports(row, invocation):
    directories = report_directories(row)
    return [
        ref
        for ref in invocation.get("reports", [])
        if ref.get("module") == row.get("module")
        and ref.get("test_kind", "unit") == row.get("subtype", "unit")
        and (
            directories is None
            or any(
                isinstance(ref.get("producer_relative_path"), str)
                and posixpath.normpath(ref["producer_relative_path"])
                == ref["producer_relative_path"]
                and ref["producer_relative_path"].startswith(directory + "/")
                for directory in directories
            )
        )
    ]


def _matching_native_events(row, events):
    """Match frozen native occurrences without requiring their completion."""
    validation = row.get("validation", {})
    events = [e for e in events if not e.get('plugin_identity_conflict') and ('module_path' not in e
              or e['module_path'] == row.get('scope', {}).get('module_path'))]
    goals = [
        goal_name(g.rsplit(":", 1)[0], g.rsplit(":", 1)[-1]) for g in validation.get("goals", [])
    ]
    bindings = validation.get("native_bindings")
    if bindings is not None:
        pools = [[e for e in events if e["module"] == row.get("module")
                  and all(e[k] == binding[k] for k in ("goal", "execution", "occurrence"))]
                 for binding in bindings]
        matched = [event for pool in pools for event in pool]
        positions = [event["position"] for event in matched]
        occurrences_complete = (all(len(pool) == 1 for pool in pools)
                                and positions == sorted(set(positions)))
    else:
        matched = [
            e for e in events
            if e["goal"] in goals and e["module"] == row.get("module")
            and (validation.get("execution") is None or e["execution"] == validation["execution"])
            and (validation.get("occurrence") is None or e["occurrence"] == validation["occurrence"])
        ]
        occurrences_complete = len(matched) == len(goals)
    return goals, matched, occurrences_complete


def requirement_started(row, invocation, text, events):
    """Observe entry independently of completion, fresh outputs or report parsing.

    A compound obligation starts when any non-skipped native member starts.
    Explicit skips establish false only when every required member is observed;
    an absent member is unknown, never presumed skipped.
    """
    if invocation.get("runner") == "native" and row.get("validation", {}).get("rule") == "native_exit":
        # evaluate() has already checked exact argv and invocation identity and
        # recomputed binary lineage. A planned record is not an observed launch.
        if invocation.get("status") == "launch_failed":
            return False
        if invocation.get("native_lineage_verified") is True and invocation.get("status") in {"completed", "timeout"}:
            argv = invocation.get("argv") or []
            invoked = posixpath.normpath(posixpath.join(invocation.get("cwd", "."), argv[0])) if argv else None
            expected = row.get("expectations", {}).get("executable")
            if invoked and (not expected or posixpath.normpath(expected) == invoked):
                return True
        return None
    if invocation.get("runner") == "maven":
        _, matched, complete = _matching_native_events(row, events)
        nested = row.get('validation', {}).get('nested_gradle_task')
        if nested:
            if not complete or len(matched) != 1:
                return None
            outcomes = _gradle_outcomes(matched[0]['segment'], returned=False, serial=False)
            outcome = outcomes.get(nested)
            return (False if outcome in {'SKIPPED', 'NO-SOURCE', 'UP-TO-DATE', 'FROM-CACHE'}
                    else True if outcome in {'FAILED', 'UNAVAILABLE'} else None)
        if any(e["status"] != "not_run" for e in matched):
            return True
        return False if matched and complete else None
    if invocation.get("runner") == "gradle":
        tasks = row.get("validation", {}).get("tasks", [])
        observed = [m for m in re.finditer(
            r"^> Task (:\S+?)(?:\s+(FAILED|SKIPPED|NO-SOURCE|UP-TO-DATE|FROM-CACHE))?\s*$",
            text, re.M,
        ) if m[1] in tasks]
        if any(m[2] in {None, "FAILED"} for m in observed):
            return True
        return False if tasks and {m[1] for m in observed} == set(tasks) else None
    return None


def _gradle_outcomes(text, *, returned, serial):
    headers = list(re.finditer(
        r'^> Task (:\S+?)(?:\s+(FAILED|SKIPPED|NO-SOURCE|UP-TO-DATE|FROM-CACHE))?\s*$',
        text, re.M))
    outcomes = {}
    for index, match in enumerate(headers):
        outcome = match[2] or ('EXECUTED' if returned or serial and index+1 < len(headers)
                               else 'UNAVAILABLE')
        # Duplicate task identities could come from separate nested builds.
        outcomes[match[1]] = 'UNAVAILABLE' if match[1] in outcomes else outcome
    return outcomes


def native_requirement(row, invocation, base, text, events, report_scope_error=None):
    rule = row.get("validation", {}).get("rule", "unclassified")
    validation = row.get("validation", {})
    scope = row.get("scope", {})
    bindings = validation.get("native_bindings")
    goals, matched, occurrences_complete = _matching_native_events(row, events)
    common = {"invocation_id": invocation["invocation_id"], "evidence_refs": [invocation["log"]],
              "started": requirement_started(row, invocation, text, events)}
    identities = [dict(plugin=e['plugin_identity'], banner=e['banner_plugin'],
                       source=e['plugin_identity_evidence']) for e in matched if e.get('plugin_identity')]
    if identities:
        common['plugin_identity_bindings'] = identities
    if matched and all('native_pair' in e for e in matched):
        common['evidence_refs'].append(matched[0]['evidence_ref'])
    for reused in validation.get("reused_native_bindings", []):
        reused_events = [e for e in events if e["module"] == row.get("module")
                         and all(e[k] == reused[k] for k in ("goal", "execution", "occurrence"))]
        if any(e["status"] == "failed" for e in reused_events):
            common["started"] = True
            return result(row["id"], "failed", "native_goal_failed", **common)
        observed_reuse = False
        if len(reused_events) == len(matched) == 1:
            from .maven_observation import test_reuse
            observed_reuse = test_reuse(matched[0], reused_events[0])
        if not observed_reuse and (validation.get('reuse_protocol') == 'surefire-3.5.3-context' or len(reused_events) != 1 or not re.search(
                r"^\[INFO\]\s+Skipping execution of surefire because it has already been run for this configuration\s*$",
                reused_events[0]["segment"], re.M)):
            occurrences_complete = False
    if rule == "junit" and bindings:
        expected = {(b["goal"], b["execution"], b["occurrence"])
                    for b in [*bindings, *validation.get("reused_native_bindings", [])]}
        producers = {(b["goal"], b["execution"]) for b in bindings}
        if any(e["module"] == row.get("module") and (e["goal"], e["execution"]) in producers
               and (e["goal"], e["execution"], e["occurrence"]) not in expected for e in events):
            occurrences_complete = False
    if any(e["status"] == "failed" for e in matched):
        if rule == 'junit' and len(matched) == 1 and 'native_pair' in matched[0]:
            try:
                from .maven_observation import reports as producer_reports
                common['test_counts'] = junit_counts(base, producer_reports(base, row, matched[0]),
                                                     native_segment=matched[0]['segment'])
                common['native_report_producer'] = {
                    'trace_sha256': matched[0]['native_trace']['trace_sha256'],
                    'mojo_id': matched[0]['native_pair']['before']['mojo_id']}
            except (ValueError, KeyError, OSError, TypeError, ET.ParseError):
                pass  # A failed native goal stays failed even with missing counts.
        return result(row["id"], "failed", "native_goal_failed", **common)
    if any(e["status"] == "not_run" for e in matched):
        return result(row["id"], "not_run", "explicit_native_skip", **common)
    if scope.get("status") not in {"declared", "resolved"} or row.get("kind") == "unclassified":
        return result(row["id"], reason="requirement_scope_unresolved", **common)
    terminal = (
        invocation.get("status") == "completed"
        and type(invocation.get("exit_code")) is int
        and invocation.get("log_complete") is True
    )
    if not terminal:
        return result(row["id"], reason="normal_terminal_unavailable", **common)
    # A step dedicated to one exact native executable can establish its smoke check.
    if rule == "native_exit":
        proof = invocation.get("native_executable")
        if proof is None or invocation.get("native_lineage_verified") is not True:
            return result(row["id"], reason="native_binary_lineage_unavailable", **common)
        argv = invocation.get("argv") or []
        invoked = (
            posixpath.normpath(posixpath.join(invocation.get("cwd", "."), argv[0]))
            if argv
            else None
        )
        expected_executable = row.get("expectations", {}).get("executable")
        if (
            proof.get("producer_relative_path") != invoked
            or expected_executable
            and posixpath.normpath(expected_executable) != invoked
        ):
            return result(row["id"], reason="native_binary_path_mismatch", **common)
        bound_file(base, proof)
        common["started"] = True
        return result(
            row["id"],
            "passed" if invocation["exit_code"] == 0 else "failed",
            "native_exit",
            **common,
        )
    # Maven success requires each specified occurrence, not just a global success line.
    native_pass = (
        bool(goals)
        and occurrences_complete
        and {e["goal"] for e in matched} == set(goals)
        and all(e["status"] == "passed" for e in matched)
    )
    nested_task = validation.get('nested_gradle_task')
    if nested_task:
        # A successful exec:exec does not prove every nested Gradle task ran.
        # Only the exact reviewed parent occurrence owns these task lines.
        if invocation.get('runner') != 'maven' or len(matched) != 1 or not occurrences_complete:
            return result(row['id'], reason='nested_gradle_parent_unavailable', **common)
        outcome = _gradle_outcomes(matched[0]['segment'], returned=native_pass,
                                   serial=False).get(nested_task)
        common['nested_gradle_task'] = {'task': nested_task, 'outcome': outcome}
        if outcome == 'FAILED':
            return result(row['id'], 'failed', 'nested_gradle_task_failed', **common)
        if outcome == 'SKIPPED':
            return result(row['id'], 'not_run', 'nested_gradle_task_skipped', **common)
        native_pass = native_pass and outcome == 'EXECUTED'
    # Gradle's explicit task outcomes are independent even under --continue.
    if invocation.get("runner") == "gradle":
        tasks = validation.get("tasks", [])
        serial = invocation.get("effective_execution", {}).get("serial") is True
        outcomes = _gradle_outcomes(text, returned=invocation['exit_code'] == 0, serial=serial)
        if any(outcomes.get(t) == "FAILED" for t in tasks):
            return result(row["id"], "failed", "gradle_task_failed", **common)
        native_pass = bool(tasks) and all(outcomes.get(t) == "EXECUTED" for t in tasks)
    if rule in {"maven_invoker", "antunit"}:
        from . import native_tests
        if report_scope_error:
            return result(row["id"], reason=report_scope_error, **common)
        if not native_pass:
            return result(row["id"], reason="native_test_completion_unavailable", **common)
        if rule == "maven_invoker":
            if invocation.get("reports_collection_complete") is not True:
                return result(row["id"], reason="invoker_report_collection_incomplete", **common)
            counts, proof = native_tests.invoker(base, matching_reports(row, invocation),
                                                  row["expectations"]["integration_projects"])
        else:
            if len(matched) != 1:
                return result(row["id"], reason="antunit_occurrence_ambiguous", **common)
            counts, proof = native_tests.antunit(matched[0]["segment"], row["expectations"]["antunit_suites"])
        # Deliberately not test_counts: these are projects/Ant targets, not JUnit cases.
        common["native_test_counts"] = {"unit": proof["unit"], **counts}
        common["native_test_evidence"] = proof
        state = "failed" if counts["failed"] + counts["errors"] else "not_run" if counts["skipped"] else "passed"
        return result(row["id"], state, "source_scoped_native_test_results", **common)
    if rule == "junit":
        directories = report_directories(row)
        if report_scope_error or (directories is None and invocation.get("runner") == "gradle"):
            return result(
                row["id"], reason=report_scope_error or "test_report_scope_unavailable", **common
            )
        reports = matching_reports(row, invocation)
        scoped_native = len(matched) == 1 and 'native_pair' in matched[0]
        if validation.get('producer_scoped_reports') and not scoped_native:
            return result(row['id'], reason='native_test_producer_unavailable', **common)
        if scoped_native:
            from .maven_observation import reports as producer_reports
            reports = producer_reports(base, row, matched[0])
            common['native_report_producer'] = {
                'trace_sha256': matched[0]['native_trace']['trace_sha256'],
                'mojo_id': matched[0]['native_pair']['before']['mojo_id']}
        reconciliations = []
        test_segment = matched[0]["segment"] if (len(matched) == 1 and
            matched[0]["goal"] in {"surefire:test", "failsafe:integration-test"}) else None
        counts = junit_counts(base, reports, native_segment=test_segment,
                              reconciliations=reconciliations)
        common["test_counts"] = counts
        if reconciliations:
            common["test_count_reconciliation"] = reconciliations
        if counts["failed"] + counts["errors"]:
            return result(row["id"], "failed", "fresh_red_tests", **common)
        if directories and any(
            junit_counts(
                base,
                [r for r in reports if r["producer_relative_path"].startswith(directory + "/")],
                native_segment=test_segment,
            )["assessed"]
            == 0
            for directory in directories
        ):
            return result(row["id"], reason="declared_test_report_directory_unavailable", **common)
        for event in matched:
            summaries = re.findall(
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)",
                event["segment"],
            )
            if summaries and tuple(map(int, summaries[-1])) != tuple(
                counts[k] for k in ("reported", "failed", "errors", "skipped")
            ):
                return result(row["id"], reason="native_and_junit_count_conflict", **common)
        # A retry can leave ERROR diagnostics even when the same native test
        # goal returned normally. Require XML/class/global counts to corroborate
        # that recovery; explicit goal failures and missing returns never pass.
        if not native_pass and reconciliations and occurrences_complete and len(matched) == 1:
            event = matched[0]
            totals = re.findall(
                r"^\[(?:INFO|WARNING)\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),"
                r"\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+),\s*Flakes:\s*(\d+)\s*$",
                event["segment"], re.M)
            expected = tuple(counts[k] for k in ("reported", "failed", "errors", "skipped"))
            native_pass = (event.get("returned") is True and event["status"] == "unavailable"
                and len(totals) == 1 and tuple(map(int, totals[0][:4])) == expected
                and int(totals[0][4]) == sum(p["flaky_cases"] for p in reconciliations))
        if (
            native_pass
            and counts["assessed"] > 0
            and invocation.get("reports_collection_complete") is True
        ):
            return result(row["id"], "passed", "fresh_test_reports_and_native_completion", **common)
        return result(row["id"], reason="test_execution_or_fresh_reports_unavailable", **common)
    if rule in {"artifact", "install"}:
        expected = row.get("expectations", {}).get("artifacts", [])
        installed_copies = {}
        if native_pass and rule == "install":
            from .installed_evidence import verified_copies
            installed_copies = verified_copies(base, expected, invocation, matched)
        if native_pass and artifact_proof(base, expected, invocation.get("artifacts", []),
                                          verified_installed_copies=installed_copies):
            if rule == "install":
                repository = invocation.get("local_repository") or {}
                if (
                    repository.get("source") != "maven_settings_probe"
                    or type(repository.get("exit_code")) is not int
                    or repository["exit_code"] != 0
                ):
                    return result(row["id"], reason="effective_local_repository_unknown", **common)
                from .native_evidence import parse_local_repository

                observed_path = parse_local_repository(
                    bound_file(base, repository["probe"]).read_text(errors="replace")
                )
                if observed_path is None or observed_path != repository.get("path"):
                    return result(row["id"], reason="effective_local_repository_conflict", **common)
                if any(not a.get("repository_relative_path") for a in expected):
                    return result(
                        row["id"], reason="installation_coordinate_path_unresolved", **common
                    )
                if installed_copies:
                    common["installation_continuity"] = installed_copies
                    common["evidence_refs"].extend(ref for copy in installed_copies.values() for ref in copy["evidence_refs"])
            return result(row["id"], "passed", "native_completion_and_declared_artifacts", **common)
        return result(row["id"], reason="artifact_or_native_completion_unavailable", **common)
    if rule == "native_goal" and native_pass:
        if row['kind'] == 'compile' and invocation.get('verified_maven_observation') and not nested_task:
            from .maven_observation import compiler_proof
            proof = compiler_proof(invocation['verified_maven_observation'], matched)
            if proof:
                common['evidence_refs'].extend(proof['evidence_refs'])
                return result(row['id'], 'passed', 'native_compiler_physical_outputs',
                              compilation_observation=proof, **common)
        if (
            row["kind"] == "compile"
            and invocation.get("runner") == "maven"
            and not nested_task
            and not any(re.search(r"Compiling\s+\d+\s+source", e["segment"], re.I) for e in matched)
        ):
            proof = invocation.get("verified_compilation_reuse", {}).get(row["id"])
            # A cache message alone proves nothing. The separately recomputed
            # four-boundary proof must bind these exact outputs to an earlier
            # successful compiler in this attempt, with unchanged inputs.
            reuse_message = r"^\[INFO\]\s+(?:Nothing to compile - all classes are up to date[.!]?|Loaded from the build cache, saving \d+(?:\.\d+)?s)\s*$"
            if proof and matched and all(re.search(reuse_message, e["segment"], re.M) for e in matched):
                common["evidence_refs"].extend(proof["evidence_refs"])
                return result(row["id"], "passed", "same_attempt_compiled_output_reused",
                              compilation_reuse=proof, **common)
            return result(row["id"], reason="compilation_or_reuse_provenance_unavailable", **common)
        expected = row.get("expectations", {}).get("artifacts", [])
        if expected and not artifact_proof(base, expected, invocation.get("artifacts", [])):
            return result(row["id"], reason="required_output_unavailable", **common)
        return result(row["id"], "passed", "native_goal_completed", **common)
    return result(row["id"], reason="native_completion_unavailable", **common)


def evaluate(task, spec, run, base, *, ci_source_base=None, required_execution_origin=None):
    """Evaluate one final attempt. Never splice best requirements across retries."""
    if required_execution_origin not in {None, "agent", "independent_replay"}:
        raise ValueError("Unknown required execution origin")
    task = normalize_task(task)
    validate_requirements(spec, task)
    identity = evaluation_identity(spec)
    valid = (
        run.get("evaluation_identity") == identity
        and run.get("commit") == task["sha"]
        and run.get("repo") == task["repo"]
        and isinstance(run.get("run_id"), str)
        and bool(run["run_id"])
    )
    invocations, invalid = [], []
    origin = run.get("execution_origin")
    if origin is not None and origin not in ("agent", "independent_replay"):
        invalid.append("Unknown run execution origin")
    expected_origin = required_execution_origin or origin
    if required_execution_origin is not None and origin != required_execution_origin:
        invalid.append("Run execution origin does not match the requested scoring boundary")
    for ref in run.get("invocations", []):
        try:
            record = load_json(bound_file(base, ref))
            if (
                record.get("run_id") != run.get("run_id")
                or record.get("evaluation_identity") != identity
                or record.get("commit") != task["sha"]
            ):
                raise ValueError("Invocation identity mismatch")
            if expected_origin is not None and record.get("execution_origin") != expected_origin:
                raise ValueError("Invocation execution origin does not match the requested scoring boundary")
            if record.get('recorder_version') == 'native-tool-hooks-v1':
                from .native_hooks import validate_native_process
                validate_native_process(base, record)
            invocations.append(record)
        except (KeyError, ValueError, OSError, TypeError) as exc:
            invalid.append(str(exc))
    # A mixed attempt invalidates the run. Missing files are never a failed command.
    invocation_ids = [r.get("invocation_id") for r in invocations]
    if not all(isinstance(value, str) and value for value in invocation_ids):
        invalid.append("Invalid invocation identity")
    elif len(set(invocation_ids)) != len(invocation_ids):
        invalid.append("Duplicate invocation identity")
    valid = valid and not invalid
    rows, commands, runtime, selected = [], [], [], []
    compile_producers = {}
    scope_errors = report_scope_errors(
        spec["requirements"], {step["id"]: step["runner"] for step in task["steps"]}
    )
    for step in task["steps"]:
        candidates = [r for r in invocations if r.get("step_id") == step["id"]]
        relevant = [r for r in spec["requirements"] if r["step_id"] == step["id"]]
        if not valid or not candidates:
            commands.append(result(step["id"], reason="invocation_identity_or_record_unavailable"))
            runtime.append(result(step["id"]))
            rows.extend(result(r["id"]) for r in relevant)
            continue
        # Exactly one invocation per step for the final acceptance attempt. Retry
        # selection belongs to the trusted recorder, with a new attempt directory.
        if len(candidates) != 1:
            commands.append(result(step["id"], reason="multiple_attempts_not_adjudicated"))
            runtime.append(result(step["id"]))
            rows.extend(
                result(r["id"], reason="multiple_attempts_not_adjudicated") for r in relevant
            )
            continue
        invocation = candidates[0]
        # A native smoke executable must be the binary preserved by an earlier
        # producer in this same final attempt, not merely a matching filename.
        invocation = dict(invocation)
        invocation["verified_compilation_reuse"] = {}
        proof = invocation.get("native_executable") or {}
        invocation["native_lineage_verified"] = False
        if step["runner"] == "native" and proof:
            invoked = posixpath.normpath(posixpath.join(step.get("cwd", "."), step["argv"][0]))
            for previous in selected:
                if (
                    previous.get("invocation_id") != proof.get("producer_invocation_id")
                    or previous.get("status") != "completed"
                    or previous.get("exit_code") != 0
                    or proof.get("producer_relative_path") != invoked
                ):
                    continue
                for artifact in previous.get("artifacts", []):
                    if (
                        artifact.get("sha256") == proof.get("sha256")
                        and artifact.get("producer_relative_path") == invoked
                        and artifact.get("fresh") is True
                    ):
                        try:
                            bound_file(base, artifact)
                            bound_file(base, proof)
                            invocation["native_lineage_verified"] = True
                        except (OSError, ValueError, TypeError):
                            pass
        selected.append(invocation)
        if (
            invocation.get("argv") != step["argv"]
            or invocation.get("cwd") != step.get("cwd", ".")
            or invocation.get("runner") != step["runner"]
            or not invocation.get("invocation_id")
        ):
            commands.append(result(step["id"], reason="literal_command_mismatch"))
            runtime.append(result(step["id"]))
            rows.extend(result(r["id"], reason="literal_command_mismatch") for r in relevant)
            continue
        runtime.append(
            runtime_result(base, invocation, step, requires_native_image(spec, step["id"]))
        )
        try:
            text = bound_file(base, invocation["log"]).read_text(errors="replace")
            terminal = (
                invocation.get("status") == "completed"
                and type(invocation.get("exit_code")) is int
                and invocation.get("log_complete") is True
            )
            command_state = (
                "passed"
                if terminal and invocation["exit_code"] == 0
                else (
                    "failed"
                    if terminal or invocation.get("status") == "timeout"
                    else "unavailable"
                )
            )
            commands.append(
                result(
                    step["id"],
                    command_state,
                    "execution_timeout" if invocation.get("status") == "timeout" else "native_terminal",
                    invocation_id=invocation["invocation_id"],
                    execution_status=invocation.get("status"),
                    started=True if terminal or invocation.get("status") == "timeout" else None,
                )
            )
            invocation = {**invocation, "effective_execution": certified_invocation_inputs(
                spec, task, step, invocation, base)}
            from . import jvm_inputs
            environment_review = jvm_inputs.environment_review_for(spec, step)
            if environment_review is not None:
                try:
                    from .ci_sources import archived_source_root
                    source_root = ci_source_base or archived_source_root(spec, run, base) or base
                    jvm_inputs.validate_environment_review(environment_review, task, step, base=source_root)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    inputs = invocation["effective_execution"]
                    inputs.update(inputs_complete=False, serial=False)
                    inputs["errors"] = [*inputs.get("errors", []), "JVM environment source unavailable: " + str(exc)]
            serial = invocation.get("effective_execution", {}).get("serial") is True
            plugin_bindings = []
            if step['runner'] == 'maven':
                try:
                    from .ci_sources import archived_source_root, maven_plugin_bindings
                    source_root = ci_source_base or archived_source_root(spec, run, base) or base
                    plugin_bindings = maven_plugin_bindings(spec, source_root, step['id'])
                except (ValueError, KeyError, TypeError, OSError, StopIteration):
                    # Missing identity evidence cannot broaden historical matches.
                    plugin_bindings = []
            events = (
                maven_events(text, terminal=terminal, serial=serial, plugin_bindings=plugin_bindings)
                if step["runner"] == "maven"
                else []
            )
            if step['runner'] == 'maven' and invocation.get('input_policy', {}).get('maven_witness'):
                from .maven_observation import load as load_maven_observation
                if not invocation.get('maven_observation'):
                    raise ValueError('Required Maven observation unavailable: ' + str(invocation.get('maven_observation_error')))
                observation = load_maven_observation(base, invocation, bound_file(base, invocation['log']).read_bytes(),
                                                    policy=spec.get('maven_observer', {}))
                invocation['verified_maven_observation'] = observation
                events = observation['events']
                from .native_evidence import bound_maven_goal
                for event in events:
                    native = event['native_pair']['before']
                    _, binding, conflict = bound_maven_goal(
                        native['plugin'], event['version'], native['goal'], event['execution'],
                        event['module'], plugin_bindings)
                    if conflict:
                        event['plugin_identity_conflict'] = True
                    elif binding:
                        event.update(plugin_identity=binding['coordinates'], banner_plugin=native['plugin'],
                                     plugin_identity_evidence=binding['source'])
            local = {}
            for row in relevant:
                try:
                    from . import compilation_evidence
                    plan = next((p for p in spec.get("compilation_reuse", []) if p["consumer_requirement"] == row["id"]), None)
                    earlier = compile_producers.get(plan["producer_requirement"]) if plan else None
                    if earlier and runtime[-1]["status"] == "passed":
                        previous, previous_runtime, previous_goals = earlier
                        try:
                            _, matched, _ = _matching_native_events(row, events)
                            if (previous_runtime["status"] == "passed" and previous["run_id"] == invocation["run_id"]
                                    and all(type(r.get("sequence")) is int for r in (previous, invocation))
                                    and previous["sequence"] < invocation["sequence"]
                                    and [(e["goal"], e["version"]) for e in matched] == previous_goals
                                    and compilation_evidence.runtime_identity(base, previous) == compilation_evidence.runtime_identity(base, invocation)):
                                invocation["verified_compilation_reuse"][row["id"]] = compilation_evidence.continuity(base, plan, previous, invocation)
                        except (ValueError, KeyError, TypeError, OSError):
                            pass  # A fresh compile may still succeed; no cache credit.
                    local[row["id"]] = native_requirement(
                        row, invocation, base, text, events, scope_errors.get(row["id"])
                    )
                    if (row["kind"] == "compile" and local[row["id"]]["status"] == "passed"
                            and local[row["id"]]["reason"] == "native_goal_completed"):
                        _, matched, _ = _matching_native_events(row, events)
                        if all(re.search(r"Compiling\s+[1-9]\d*\s+source", e["segment"], re.I) for e in matched):
                            compile_producers[row["id"]] = (invocation, runtime[-1], [(e["goal"], e["version"]) for e in matched])
                except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
                    local[row["id"]] = result(
                        row["id"], reason=str(exc),
                        started=requirement_started(row, invocation, text, events),
                        invocation_id=invocation["invocation_id"], evidence_refs=[invocation["log"]],
                    )
            # All four fail-fast conditions are explicit. The frozen occurrence
            # plan, not plugin names alone, supplies ordering and module scope.
            if step["runner"] == "maven" and fail_fast_observable(invocation, text):
                for row in sorted(
                    relevant,
                    key=lambda r: (
                        r.get("validation", {}).get("position")
                        if type(r.get("validation", {}).get("position")) is int
                        else -1
                    ),
                ):
                    item = local[row["id"]]
                    binding = row.get("validation", {})
                    if (
                        item["status"] != "unavailable"
                        or not row.get("dependencies_complete")
                        or binding.get("plan_resolved") is not True
                        or type(binding.get("position")) is not int
                    ):
                        continue
                    for parent_id in row.get("depends_on", []):
                        parent = next((r for r in relevant if r["id"] == parent_id), None)
                        if (
                            parent is None
                            or parent.get("module") is None
                            or parent.get("module") != row.get("module")
                            or not (
                                local[parent_id]["status"] == "failed"
                                or local[parent_id].get("inference_rule")
                                == "maven_same_module_fail_fast"
                            )
                        ):
                            continue
                        previous = parent.get("validation", {})
                        failure_positions = [previous.get("position")]
                        if previous.get("native_bindings") and local[parent_id]["status"] == "failed":
                            failure_positions = [b["position"] for b in previous["native_bindings"]
                                                 if any(e["module"] == parent["module"]
                                                        and e["status"] == "failed"
                                                        and all(e[k] == b[k] for k in ("goal", "execution", "occurrence"))
                                                        for e in events)]
                        goals = {
                            goal_name(g.rsplit(":", 1)[0], g.rsplit(":", 1)[-1])
                            for g in binding.get("goals", [])
                        }
                        counter = any(
                            e["module"] == row["module"] and e["goal"] in goals for e in events
                        )
                        if row["kind"] == "test":
                            counter = counter or bool(matching_reports(row, invocation))
                        expected_paths = {
                            a.get("path") for a in row.get("expectations", {}).get("artifacts", [])
                        }
                        counter = counter or any(
                            a.get("fresh") is True
                            and a.get("producer_relative_path") in expected_paths
                            for a in invocation.get("artifacts", [])
                        )
                        if (
                            previous.get("plan_resolved") is True
                            and any(type(position) is int and position < binding["position"]
                                    for position in failure_positions)
                            and goals
                            and not counter
                        ):
                            local[row["id"]] = result(
                                row["id"],
                                "not_run",
                                "upstream_failed",
                                inference_rule="maven_same_module_fail_fast",
                                blocker_id=parent_id,
                                binding=binding,
                                invocation_id=invocation["invocation_id"],
                                evidence_refs=[invocation["log"]],
                                started=False,
                            )
            rows.extend(local.values())
        except (ValueError, KeyError, TypeError, OSError) as exc:
            commands.append(result(step["id"], reason=str(exc)))
            rows.extend(result(r["id"], reason=str(exc)) for r in relevant)
    # The order of final invocations must be monotonic, even when step IDs differ.
    sequences = [r.get("sequence") for r in selected]
    if sequences and (
        any(type(x) is not int for x in sequences)
        or any(a >= b for a, b in zip(sequences, sequences[1:]))
    ):
        commands.append(result("ordered_steps", "unavailable", "ordered_lineage_unavailable"))
    worktree = (
        worktree_result(base, run.get("worktree", []), run.get("run_id"), task["sha"], selected)
        if valid
        else result("worktree_integrity")
    )
    preconditions = [
        worktree,
        result(
            "runtime_conformance",
            {"complete": "passed", "incomplete": "failed", "unavailable": "unavailable"}[
                task_status(r["status"] for r in runtime)
            ],
            "per_invocation_launcher",
            invocations=runtime,
        ),
    ]
    for row in rows:
        row.setdefault("started", None)
        definition = next(r for r in spec["requirements"] if r["id"] == row["id"])
        goals = definition.get("validation", {}).get("goals", [])
        if (
            row["status"] == "failed"
            and definition["kind"] == "quality_check"
            and any("rat" in g.rsplit(":", 1)[0] for g in goals)
        ):
            from .rat_attribution import attribute_rat_failure

            provenance = run.get("rat_provenance", {}).get(row["id"], {})
            row["attribution"] = attribute_rat_failure(
                base,
                run.get("run_id"),
                provenance.get("before"),
                provenance.get("after"),
                provenance.get("write_events"),
                provenance.get("rat_report"),
            )
    summary = summarize(spec, rows, preconditions, commands, identity_valid=valid)
    telemetry = run.get("telemetry")
    human = None
    costs = {
        "total_tokens": None,
        "known_tokens": None,
        "unattended_seconds": None,
        "cost_coverage": "unavailable",
    }
    if telemetry:
        try:
            t = load_json(bound_file(base, telemetry))
            if t.get("run_id") == run.get("run_id") and t.get("source") in {
                "trusted_noninteractive_runner",
                "trusted_runner",
            }:
                seconds = t.get("unattended_seconds")
                if type(seconds) in {int, float} and math.isfinite(seconds) and seconds >= 0:
                    costs["unattended_seconds"] = seconds
                calls = t.get("model_calls")
                if isinstance(calls, list) and all(
                    isinstance(c, dict)
                    and c.get("role") in {"actor", "advisor", "summary", "retry"}
                    and all(
                        type(c.get(k)) is int and c[k] >= 0
                        for k in ("input_tokens", "output_tokens")
                    )
                    for c in calls
                ):
                    costs["known_tokens"] = sum(
                        c["input_tokens"] + c["output_tokens"] for c in calls
                    )
                    if t.get("model_calls_complete") is True:
                        costs["total_tokens"] = costs["known_tokens"]
                        costs["cost_coverage"] = (
                            "complete" if costs["unattended_seconds"] is not None else "tokens_only"
                        )
            if t.get("intervention_protocol") is not None:
                from .intervention_protocol import verified_intervention_count

                human = verified_intervention_count(base, t, run)
            elif (
                t.get("run_id") == run.get("run_id")
                and t.get("source") == "trusted_noninteractive_runner"
                and t.get("coverage") == "complete"
                and t.get("runner_version")
                and t.get("started_at")
                and t.get("finished_at")
                and t.get("input_policy", {}).get("stdin") == "DEVNULL"
                and t.get("input_policy", {}).get("external_channels") == "enforced"
                and isinstance(t.get("events"), list)
                and type(t.get("human_interventions")) is int
                and t["human_interventions"] == len(t["events"])
                and t["human_interventions"] >= 0
            ):
                human = t["human_interventions"]
        except (ValueError, KeyError, TypeError, OSError):
            pass
    from .ci_sources import archived_source_root
    from .ci_verification import compare_verified, unavailable

    source_error = None
    if ci_source_base is None:
        try:
            ci_source_base = archived_source_root(spec, run, base)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            source_error = str(exc)
    comparison = compare_verified(task, spec, summary, source_base=ci_source_base)
    if source_error:
        comparison.update(reference={"status": "unavailable", "reason": source_error},
                          scope=unavailable(source_error), test_counts=unavailable(source_error))
    return {
        **({"execution_policy": "agent-execution-v1", "execution_origin": origin,
            "required_execution_origin": required_execution_origin}
           if origin is not None or required_execution_origin is not None else {}),
        "metric_version": POLICY_VERSION,
        "evaluation_identity": identity,
        "project_id": spec.get("project_id"),
        "run_id": run.get("run_id"),
        "agent": run.get("agent"),
        **summary,
        "human_interventions": human,
        **costs,
        "autonomous_success": (
            None if origin == "independent_replay" else
            False
            if summary["status"] == "incomplete" or human is not None and human > 0
            else True if summary["status"] == "complete" and human == 0 else None
        ),
        "ci_verification": comparison,
        "ci_scope_attainment": comparison["scope"] if comparison["scope"]["status"] in {"met", "not_met"} else None,
        "ci_test_count_attainment": comparison["test_counts"] if comparison["test_counts"]["status"] in {"equal_counts", "different_counts"} else None,
        "ci_case_identity_equivalence": None,
        "limitations": [
            "Requirements analysis does not upgrade missing CI comparisons.",
            "Boundary snapshots cannot rule out transient workspace edits.",
            "Unknown execution plans, cache reuse and unsupported runner outcomes stay unavailable.",
        ],
        "record_errors": invalid,
    }
