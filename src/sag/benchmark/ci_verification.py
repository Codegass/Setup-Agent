"""Recompute a selected official CI reference before comparing any scores.

The v1 transport supports complete, serial Maven commands in archived GitHub
Actions jobs. Unsupported transports remain explicit unavailable results.
Counts describe report occurrences, never unique or equivalent test identities.
"""

import io
import json
import re
import shlex
from pathlib import PurePosixPath
import xml.etree.ElementTree as ET
import zipfile

from .ci_sources import module_aliases, read_source, reviewed_steps
from .ci_native import ci_native_text as native_text
from .native_evidence import ANSI, FORK, FORK_LINE, maven_events
from .requirements import BUILD_KINDS, canonical_digest

VERSION = "selected-ci-verification-v2"
COUNT_KEYS = ("reported", "skipped", "assessed", "passed", "failed", "errors")


def unavailable(reason):
    return {
        "status": "unavailable",
        "reason": reason,
        "numerator": None,
        "denominator": None,
        "rate": None,
    }


def _selected_job(task, spec, base):
    alignment = spec["ci_alignment"]
    index_ref = alignment["archived_ci_index"]
    index = json.loads(read_source(base, index_ref))
    url = alignment["selected_url"]
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/actions/runs/(\d+)/job/(\d+)", url)
    if not match or match[1] != task["repo"]:
        raise ValueError("Selected CI transport or repository is unsupported")
    identity = index["ci_identity"]
    if (index.get("repo"), index.get("sha"), index.get("selected_url")) != (
        task["repo"],
        task["sha"],
        url,
    ):
        raise ValueError("CI index repository, commit or selected job mismatch")
    if (
        (identity.get("run_id"), identity.get("job_id"), identity.get("job_name"))
        != (int(match[2]), int(match[3]), alignment["selected_cell"])
        or type(identity.get("run_attempt")) is not int
        or identity["run_attempt"] < 1
    ):
        raise ValueError("CI index run, attempt or cell mismatch")
    sources = index["sources"]
    selection_ref = sources.get("selection") or sources.get("selected_cell_index")
    zip_ref = sources.get("raw_zip") or sources.get("run_log_zip")
    selection = json.loads(read_source(base, selection_ref))
    if selection.get("repo") != task["repo"]:
        raise ValueError("CI selection repository mismatch")
    runs = [
        r
        for r in selection["runs"]
        if (r.get("run_id"), r.get("run_attempt")) == (identity["run_id"], identity["run_attempt"])
    ]
    if (
        len(runs) != 1
        or runs[0].get("head_sha") != task["sha"]
        or runs[0].get("repo") != task["repo"]
    ):
        raise ValueError("Official CI run does not bind the pinned commit and attempt")
    run = runs[0]
    cells = [c for c in run["cells"] if c.get("job_id") == identity["job_id"]]
    if len(cells) != 1:
        raise ValueError("Official CI job selection is ambiguous")
    cell = cells[0]
    if (
        cell.get("job_name"),
        cell.get("job_url"),
        cell.get("job_status"),
        cell.get("job_conclusion"),
    ) != (alignment["selected_cell"], url, "completed", "success"):
        raise ValueError("Selected job is not a completed successful reference")
    revision = cell.get("source_revision") or {}
    if (
        revision.get("run_head_sha") != task["sha"]
        or revision.get("observed_target_repository_shas") != [task["sha"]]
        or revision.get("other_checkout_repositories") != []
    ):
        raise ValueError("Selected job checkout evidence is ambiguous")
    member = index["zip_member"]
    if (
        cell["log_selection"].get("members") != [member]
        or cell["log_selection"].get("job_binding") != "exact_job_name"
    ):
        raise ValueError("Log archive member is not uniquely bound to the selected job")
    if any(run["log_archive"].get(k) != zip_ref[k] for k in ("sha256", "bytes")):
        raise ValueError("CI run log archive differs from the independent selection")
    raw = read_source(base, sources["job_log"])
    with zipfile.ZipFile(io.BytesIO(read_source(base, zip_ref))) as archive:
        if archive.namelist().count(member) != 1 or archive.read(member) != raw:
            raise ValueError("Selected job bytes differ from the official run archive")
    return (
        raw,
        {
            "index": index_ref,
            "job_log": sources["job_log"],
            "log_archive": zip_ref,
            "selection": selection_ref,
        },
        identity,
    )


def _command_slices(task, raw):
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    normalized = [native_text(line) for line in lines]
    headings = [
        (i, shlex.split(line.strip()[len("##[group]Run ") :]))
        for i, line in enumerate(normalized)
        if line.startswith("##[group]Run ")
    ]
    slices, previous = {}, -1
    for step in task["steps"]:
        if step["runner"] != "maven" or step.get("cwd", ".") != ".":
            raise ValueError("CI verifier currently requires reviewed root Maven commands")
        if any(a == "-T" or a.startswith("-T") or a.startswith("--threads") for a in step["argv"]):
            raise ValueError("Parallel Maven output is not a comparable CI reference")
        matches = [i for i, argv in headings if argv == step["argv"]]
        if len(matches) != 1 or matches[0] <= previous:
            raise ValueError("CI command missing, repeated or in a different order")
        start = matches[0]
        end = next((i for i, _ in headings if i > start), len(lines))
        text = "".join(normalized[start:end])
        if re.findall(r"^\[INFO\]\s+BUILD (SUCCESS|FAILURE)\s*$", text, re.M) != ["SUCCESS"]:
            raise ValueError("CI command lacks one complete successful Maven terminal")
        if re.search(r"Using the MultiThreadedBuilder", text):
            raise ValueError("CI command used parallel Maven execution")
        # Malformed fork framing cannot quietly hide test pools.
        stack = []
        for marker in FORK_LINE.finditer(text):
            m = FORK.fullmatch(marker.group())
            if m is None or (m[1], m[6]) not in {(">>>", ">"), ("<<<", "<")}:
                raise ValueError("Malformed CI Maven fork framing")
            key = tuple(m[i] for i in (2, 3, 4, 5, 7, 8))
            if m[1] == ">>>":
                stack.append(key)
            elif stack and stack[-1] == key:
                stack.pop()
            else:
                raise ValueError("Unbalanced CI Maven fork framing")
        if stack:
            raise ValueError("Incomplete CI Maven fork framing")
        slices[step["id"]] = (text, start + 1, end + 1, "".join(lines[start:end]).encode())
        previous = start
    return slices


def _pool_counts(event):
    # Class summaries include a suffix such as '- in FooTest'. Only the
    # separate Results total is a pool denominator; never sum both levels.
    pattern = r"^\[(?:INFO|WARNING|ERROR)\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)\s*$"
    totals = re.findall(pattern, event["segment"], re.M)
    if (
        event["status"] != "passed"
        or len(totals) != 1
        or len(re.findall(r"^\[INFO\]\s+Results:\s*$", event["segment"], re.M)) != 1
    ):
        raise ValueError("CI test pool has missing, repeated or incomplete native Results totals")
    reported, failed, errors, skipped = map(int, totals[0])
    passed = reported - skipped - failed - errors
    if passed < 0 or reported - skipped <= 0:
        raise ValueError("CI test counts conflict or contain no assessed test bodies")
    return dict(
        reported=reported,
        skipped=skipped,
        assessed=reported - skipped,
        passed=passed,
        failed=failed,
        errors=errors,
    )


def _command_runtime(step, text, *, start, previous):
    java = re.findall(r"^(?:\[INFO\]\s+)?Java version:\s*([\d.]+)", text, re.M)
    maven = re.findall(r"^(?:\[INFO\]\s+)?Apache Maven\s+(\d+\.\d+\.\d+)", text, re.M)
    homes = set(re.findall(r"^\s*JAVA_HOME:\s*(\S+)\s*$", text, re.M))
    if len(set(java)) > 1 or len(set(maven)) > 1 or len(homes) != 1:
        raise ValueError("CI launcher runtime or environment is ambiguous")
    home = next(iter(homes))
    environment = re.findall(r"^[ \t]*env:[ \t]*\n((?:[ \t]+[^\n]*\n)*)", text, re.M)
    environment_digest = canonical_digest(environment[0]) if len(environment) == 1 else None
    observed_java = int(java[0].split(".")[1 if java[0].startswith("1.") else 0]) if java else None
    observed_maven = maven[0] if maven else None
    basis = "command_version_banner"
    if observed_java is None or observed_maven is None:
        if (
            not previous
            or previous["end_line_exclusive"] != start
            or previous["java_home"] != home
            or previous["executable"] != step["argv"][0]
            or environment_digest is None
            or previous["environment_digest"] != environment_digest
        ):
            raise ValueError(
                "CI command runtime lacks a banner or contiguous unchanged launcher evidence"
            )
        observed_java = observed_java if observed_java is not None else previous["java_major"]
        observed_maven = observed_maven or previous["maven_version"]
        basis = "contiguous_command_same_job_and_launcher_environment"
    if any(
        expected is not None and expected != actual
        for expected, actual in (
            (step.get("java_major"), observed_java),
            (step.get("maven_version"), observed_maven),
        )
    ):
        raise ValueError("CI observed launcher version differs from the frozen task")
    return {
        "java_major": observed_java,
        "maven_version": observed_maven,
        "java_home": home,
        "executable": step["argv"][0],
        "basis": basis,
        "environment_digest": environment_digest,
    }


def _empty_selection(binding, review, task, spec, base):
    from .empty_test_selection import reviewed_empty_surefire_selection

    witness = binding.get("disabled_witness") or {}
    if witness.get("type") != "source_empty_surefire_selection":
        return False
    sources = review["sources"]
    if witness.get("effective_pom_sha256") != sources["effective_pom"]["sha256"] or any(
        witness["official_ci"].get(k) != sources["official_ci"].get(k) for k in ("sha256", "bytes")
    ):
        raise ValueError("Empty test selection proof belongs to another model or command")
    root = ET.fromstring(read_source(base, sources["effective_pom"]))
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    models = [root] if root.tag == "project" else root.findall("project")
    models = [m for m in models if m.findtext("artifactId") == binding["module"]]
    if len(models) != 1:
        raise ValueError("Empty selection module has no unique effective POM")
    step = next(s for s in task["steps"] if s["id"] == review["step_id"])
    module = next(
        m
        for s in spec["steps"]
        if s["step_id"] == step["id"]
        for m in s["modules"]
        if m["id"] == binding["module"]
    )
    suffix = "/" + (module["path"] + "/" if module["path"] != "." else "") + "target"
    directory = models[0].findtext("build/directory") or ""
    if not directory.endswith(suffix):
        raise ValueError("Empty selection workspace is not bound to the module path")
    workspace = directory[: -len(suffix)]
    if not PurePosixPath(workspace).is_absolute():
        raise ValueError("Empty selection workspace unavailable")
    reviewed_empty_surefire_selection(
        binding, models[0], witness, base, {**task, "steps": [step]}, workspace
    )
    return True


def verify_reference(task, spec, base):
    """Re-read raw sources. Metadata totals and agent assertions are not input."""
    jenkins_counts = None
    selected_url = spec["ci_alignment"]["selected_url"]
    if not isinstance(selected_url, str) or not selected_url.strip():
        raise ValueError("Selected official CI job URL is unavailable")
    if selected_url.startswith("https://github.com/"):
        raw, sources, identity = _selected_job(task, spec, base)
        slices = _command_slices(task, raw)
        runtimes = {}
    else:
        from .ci_jenkins import selected_jenkins

        raw, sources, identity, jenkins_counts, runtimes = selected_jenkins(task, spec, base)
        slices = {
            task["steps"][0]["id"]: (
                native_text(raw.decode("utf-8")),
                1,
                len(raw.splitlines()) + 1,
                raw,
            )
        }
    aliases = module_aliases(spec, base)
    reviews = reviewed_steps(spec)
    if [r["step_id"] for r in reviews] != [s["id"] for s in task["steps"]]:
        raise ValueError("Reviewed CI steps differ from the ordered task")
    pools, covered, previous, excluded = [], set(), None, []
    keys = ("goal", "version", "module", "execution", "occurrence", "position")
    for review in reviews:
        if review.get("declared_variant"):
            raise ValueError("Declared diagnostic variants are not the original CI task")
        step_id = review["step_id"]
        text, start, end, command_bytes = slices[step_id]
        task_step = next(s for s in task["steps"] if s["id"] == step_id)
        runtime = runtimes.get(step_id) or _command_runtime(
            task_step, text, start=start, previous=previous
        )
        runtimes[step_id] = runtime
        previous = {**runtime, "end_line_exclusive": end}
        recorded = read_source(base, review["sources"]["official_ci"])
        if recorded not in (raw, command_bytes):
            raise ValueError("Reviewed command is not the entire selected CI command or job")
        lineage = review.get("ci_source_lineage")
        if lineage and (
            lineage.get("start_line"),
            lineage.get("end_line_exclusive"),
            lineage.get("raw_job_sha256"),
        ) != (start, end, sources["job_log"]["sha256"]):
            raise ValueError("Reviewed CI command lineage mismatch")
        events = maven_events(text, terminal=True, serial=True)
        bindings = review["ci_bindings"]
        if [tuple(e[k] for k in keys) for e in events] != [
            tuple(b[k] for k in keys) for b in bindings
        ]:
            raise ValueError("Native CI goal inventory differs from the reviewed plan")
        for event, binding in zip(events, bindings):
            req = binding.get("requirement_id")
            required = binding.get("requirement_ids", [req] if req else [])
            if (
                not isinstance(required, list)
                or len(required) != len(set(required))
                or req
                and required != [req]
            ):
                raise ValueError("CI requirement binding is duplicated or contradictory")
            for requirement_id in required:
                requirements = [
                    r
                    for r in spec["requirements"]
                    if r["id"] == requirement_id
                    and r["step_id"] == step_id
                    and r["module"] == event["module"]
                    and r["scope"].get("module_path") == aliases.get(event["module"])
                    and event["goal"] in r["validation"].get("goals", [])
                ]
                if len(requirements) != 1 or event["status"] != "passed":
                    raise ValueError(
                        "CI requirement binding has a conflicting goal, scope or outcome"
                    )
                covered.add(requirement_id)
            if event["goal"] not in {"surefire:test", "failsafe:integration-test"}:
                continue
            req = required[0] if len(required) == 1 else None
            if (
                binding.get("disposition") == "ci_disabled"
                and not required
                and event["status"] == "not_run"
            ):
                excluded.append(
                    {
                        "step_id": step_id,
                        "module": event["module"],
                        "goal": event["goal"],
                        "reason": "native_explicit_skip_or_no_tests",
                    }
                )
                continue
            if (
                binding.get("disposition") == "ci_disabled"
                and not required
                and event["status"] == "passed"
                and _empty_selection(binding, review, task, spec, base)
            ):
                if re.search(r"Tests run:|Results:", event["segment"]):
                    raise ValueError("Reviewed empty selection conflicts with observed tests")
                excluded.append(
                    {
                        "step_id": step_id,
                        "module": event["module"],
                        "goal": event["goal"],
                        "reason": "source_verified_empty_default_selection",
                        "proof": binding["disabled_witness"],
                    }
                )
                continue
            if not req or binding.get("disposition") != "requirement":
                raise ValueError(
                    "Native CI test pool is not bound to exactly one frozen requirement"
                )
            rows = [
                r
                for r in spec["requirements"]
                if r["id"] == req
                and r["step_id"] == step_id
                and r["kind"] == "test"
                and r["module"] == event["module"]
            ]
            if len(rows) != 1 or any(p["requirement_id"] == req for p in pools):
                raise ValueError("CI test pool scope is ambiguous or duplicated")
            pools.append(
                {
                    "requirement_id": req,
                    "step_id": step_id,
                    "module": event["module"],
                    "module_path": aliases[event["module"]],
                    "goal": event["goal"],
                    "execution": event["execution"],
                    "counts": _pool_counts(event),
                }
            )
    if covered != {r["id"] for r in spec["requirements"]}:
        raise ValueError("CI goals do not cover the complete frozen requirements denominator")
    if {p["requirement_id"] for p in pools} != {
        r["id"] for r in spec["requirements"] if r["kind"] == "test"
    }:
        raise ValueError("CI test pools differ from the complete frozen test scope")
    counts = {k: sum(p["counts"][k] for p in pools) for k in COUNT_KEYS}
    if jenkins_counts is not None and (
        any(
            counts[k] != jenkins_counts[k + "_count"]
            for k in ("reported", "assessed", "skipped", "passed")
        )
        or counts["failed"] + counts["errors"] != jenkins_counts["failed_or_error_count"]
    ):
        raise ValueError("Jenkins console pools and native testReport totals disagree")
    return {
        "status": "verified",
        "selected_url": spec["ci_alignment"]["selected_url"],
        "identity": identity,
        "sources": sources,
        "pools": pools,
        "excluded_test_invocations": excluded,
        "runtimes": runtimes,
        "module_aliases": aliases,
        "counts": counts,
        "requirement_ids": sorted(covered),
    }


def compare_verified(task, spec, summary, *, source_base):
    """Called only with the independently recomputed local evaluator result."""
    result = {
        "protocol": VERSION,
        "requirements_digest": canonical_digest(spec),
        "reference": {"status": "unavailable"},
        "scope": unavailable("CI source evidence unavailable"),
        "test_counts": unavailable("CI source evidence unavailable"),
        "case_identity": unavailable(
            "Complete official case identities are not verified by this transport"
        ),
    }
    try:
        if source_base is None:
            raise ValueError("Explicit CI source archive unavailable")
        reference = verify_reference(task, spec, source_base)
        result["reference"] = reference
        if spec["ci_alignment"].get("comparison_admitted") is not True:
            raise ValueError("Frozen task has not admitted an official CI comparison")
        if any(r["status"] != "passed" for r in summary["preconditions"]):
            raise ValueError("Local runtime or worktree conformance is not established")
        rows = {r["id"]: r for r in summary["requirements"]}

        def coverage(kinds):
            selected = [rows[r["id"]] for r in spec["requirements"] if r["kind"] in kinds]
            if not selected:
                return {**unavailable("No requirements in this group"), "status": "not_applicable"}
            if any(r["status"] == "unavailable" for r in selected):
                return unavailable("Local requirement evidence incomplete")
            passed = sum(r["status"] == "passed" for r in selected)
            return {
                "status": "met" if passed == len(selected) else "not_met",
                "numerator": passed,
                "denominator": len(selected),
                "rate": passed / len(selected),
            }

        scope = coverage({r["kind"] for r in spec["requirements"]})
        scope["groups"] = {
            "build": coverage(BUILD_KINDS),
            "test": coverage({"test"}),
            "documentation": coverage({"documentation"}),
            "quality_check": coverage({"quality_check"}),
        }
        # Successful requirements alone do not authorize a task-success verdict.
        scope["task_status"] = summary["status"]
        if scope["status"] == "met" and summary["status"] != "complete":
            scope = {
                **unavailable("Original ordered CI task did not complete"),
                "groups": scope["groups"],
                "task_status": summary["status"],
            }
        result["scope"] = scope
        compared = []
        for pool in reference["pools"]:
            local = rows[pool["requirement_id"]]
            counts = local.get("test_counts")
            if (
                local["status"] not in {"passed", "failed"}
                or not counts
                or any(type(counts.get(k)) is not int for k in COUNT_KEYS)
            ):
                raise ValueError("Local test pool evidence incomplete")
            compared.append(
                {"requirement_id": pool["requirement_id"], "ci": pool["counts"], "local": counts}
            )
        if not compared:
            raise ValueError("No comparable nonempty CI test pool")
        local_counts = {k: sum(p["local"][k] for p in compared) for k in COUNT_KEYS}
        exact = all(all(p["ci"][k] == p["local"][k] for k in COUNT_KEYS) for p in compared)
        exceeds = any(
            p["local"][k] > p["ci"][k] for p in compared for k in ("reported", "assessed")
        )
        ci_counts = reference["counts"]
        result["test_counts"] = {
            "status": (
                "equal_counts" if exact else "scope_conflict" if exceeds else "different_counts"
            ),
            "unit": "test_report_occurrence",
            "pools": compared,
            "ci": ci_counts,
            "local": local_counts,
            "assessed_ratio": local_counts["assessed"] / ci_counts["assessed"],
            "passed_ratio": (
                local_counts["passed"] / ci_counts["passed"] if ci_counts["passed"] else None
            ),
            "identity_equivalence": None,
        }
    except (
        KeyError,
        ValueError,
        TypeError,
        OSError,
        UnicodeError,
        zipfile.BadZipFile,
        ET.ParseError,
        StopIteration,
    ) as exc:
        reason = str(exc) or type(exc).__name__
        if result["reference"]["status"] == "unavailable":
            result["reference"]["reason"] = reason
        if result["scope"]["status"] == "unavailable":
            result["scope"] = unavailable(reason)
        result["test_counts"] = unavailable(reason)
    return result
