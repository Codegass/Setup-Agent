"""Audit existing dataset test evidence offline; never promote equal counts to identity.

Only writes a new audit directory. No API calls, checkout, build or model calls.
Canonical selection and historical dataset files remain unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile

from sag.benchmark.ci_count_semantics import _parse_report


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read(path):
    return json.loads(Path(path).read_bytes())


def source(base, value):
    """Verify a declared reference, or label a newly inventoried hash as such."""
    value = {"file": value} if isinstance(value, str) else value
    relative = value.get("file", value.get("archive", value.get("path")))
    if not isinstance(relative, str):
        raise ValueError("No local evidence path")
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError("Missing or escaped evidence: " + relative)
    raw = path.read_bytes()
    sha = value.get("sha256", value.get("archive_sha256"))
    if sha is not None and sha != digest(raw):
        raise ValueError("Declared evidence hash differs: " + relative)
    if value.get("bytes") is not None and value["bytes"] != len(raw):
        raise ValueError("Declared evidence byte length differs: " + relative)
    return {"path": relative, "sha256": digest(raw), "bytes": len(raw),
            "hash_basis": "matches_archived_reference" if sha else "observed_during_offline_audit",
            **({"url": value["url"]} if value.get("url") else {})}


def normalize_counts(value):
    reported = value.get("reported_count", value.get("reported_total", value.get("reported")))
    skipped = value.get("skipped_count", value.get("skipped"))
    passed = value.get("passed_count", value.get("passed"))
    failed = value.get("failed_count", value.get("failures"))
    error = value.get("error_count", value.get("errors"))
    combined = value.get("failed_or_error_count", value.get("failed_or_error", value.get("failed_or_errored")))
    if any(type(x) is not int or x < 0 for x in (reported, skipped, passed)):
        raise ValueError("Missing/noninteger reported, skipped or passed")
    if any(x is not None and (type(x) is not int or x < 0) for x in (failed, error)):
        raise ValueError("Invalid separate failure/error count")
    if combined is None:
        if any(type(x) is not int or x < 0 for x in (failed, error)):
            raise ValueError("Missing failure/error semantics")
        combined = failed + error
    elif failed is not None and error is not None and combined != failed + error:
        raise ValueError("Combined and separate failure/error counts conflict")
    elif type(combined) is int and any(x is not None and x > combined for x in (failed, error)):
        raise ValueError("Separate failure/error count exceeds combined total")
    if type(combined) is not int or combined < 0 or reported != skipped + passed + combined:
        raise ValueError("Native count conservation conflict")
    return {"reported_count": reported, "skipped_count": skipped, "assessed_count": reported - skipped,
            "passed_count": passed, "failed_count": failed, "error_count": error,
            "failed_or_error_count": combined if failed is None or error is None else None}


def case_counts(rows):
    c = Counter(r["outcome"] for r in rows)
    if set(c) - {"passed", "skipped", "failed_or_error", "failed", "error"}:
        raise ValueError("Unknown native testcase outcome")
    return {"reported_count": len(rows), "skipped_count": c["skipped"],
            "assessed_count": len(rows) - c["skipped"], "passed_count": c["passed"],
            "failed_or_error_count": c["failed_or_error"] + c["failed"] + c["error"]}


def comparable_counts(value):
    return (value["reported_count"], value["skipped_count"], value["assessed_count"], value["passed_count"],
            value.get("failed_or_error_count") if value.get("failed_or_error_count") is not None
            else value.get("failed_count", 0) + value.get("error_count", 0))


def identity_inventory(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[canonical(row["identity"])].append(row)
    collisions = [{"identity": json.loads(key), "multiplicity": len(values),
                   "outcomes": dict(Counter(r["outcome"] for r in values)),
                   "source_occurrences": [r.get("source_pointer") for r in values]}
                  for key, values in groups.items() if len(values) > 1]
    multiset = sorted((key, tuple(sorted(Counter(r["outcome"] for r in values).items()))) for key, values in groups.items())
    native_ids = Counter(r["native_record_id"] for r in rows if r.get("native_record_id") is not None)
    return {"record_occurrences": len(rows), "distinct_observed_identity_keys": len(groups),
            "collision_key_count": len(collisions), "duplicate_excess": len(rows) - len(groups),
            "native_record_id_collisions": {key: n for key, n in native_ids.items() if n > 1},
            "collisions": collisions, "identity_outcome_multiset_sha256": digest(canonical(multiset).encode()),
            "ordinal_is_identity": False, "duplicates_removed": False}


def jenkins_rows(report, blocks=None, publisher=None):
    pools = [(c.get("child", {}).get("url"), c["result"]) for c in report["childReports"]] if "childReports" in report else [(None, report)]
    rows = []
    labels = {"PASSED": "passed", "FIXED": "passed", "SKIPPED": "skipped", "FAILED": "failed_or_error", "REGRESSION": "failed_or_error"}
    for pi, (module, pool) in enumerate(pools):
        for si, suite in enumerate(pool.get("suites", [])):
            if blocks is not None and suite.get("enclosingBlockNames") != blocks:
                continue
            if publisher is not None and str(suite.get("nodeId")) != str(publisher):
                continue
            for ci, case in enumerate(suite.get("cases", [])):
                state = labels.get(case.get("status"))
                if state is None or not case.get("className") or not case.get("name"):
                    raise ValueError("Missing case label or unsupported Jenkins outcome")
                if "skipped" in case and case["skipped"] is not (state == "skipped"):
                    raise ValueError("Jenkins skipped/status conflict")
                rows.append({"identity": {"module_child_url": module, "suite": suite.get("name"),
                    "class_name": case["className"], "name": case["name"],
                    "enclosing_blocks": suite.get("enclosingBlockNames"), "publisher_node": suite.get("nodeId")},
                    "outcome": state, "native_outcome": case["status"],
                    "source_pointer": f"pool/{pi}/suites/{si}/cases/{ci}"})
    return rows


def scan_rows(report):
    if report.get("status") != "COMPLETED":
        raise ValueError("Scan is not complete")
    data = report["data"]
    suites, units, tests = data["suites"], data["workUnits"], data["tests"]
    rows = []
    seen_tests, seen_suites = [], []
    for wi, unit in enumerate(units):
        for si in unit["suites"]:
            suite = suites[si]
            if suite["parentWorkUnit"] != wi or suite["workUnitName"] != unit["name"]:
                raise ValueError("Scan suite/work-unit join conflict")
            seen_suites.append(si)
            for ti in suite["tests"]:
                test = tests[ti]
                if test["parentSuite"] != si or test["suiteName"] != suite["name"] or test["workUnitName"] != unit["name"]:
                    raise ValueError("Scan testcase parent join conflict")
                outcome = {0: "passed", 1: "skipped", 2: "failed_or_error"}.get(test["outcome"]) if type(test["outcome"]) is int else None
                if outcome is None:
                    raise ValueError("Scan outcome needs a separate flaky/selection policy")
                rows.append({"identity": {"work_unit": unit["name"], "suite": suite["name"], "name": test["name"]},
                             "native_record_id": test["testId"], "display_name": test["displayName"],
                             "outcome": outcome, "source_pointer": f"data/tests/{ti}"})
                seen_tests.append(ti)
    if sorted(seen_tests) != list(range(len(tests))) or sorted(seen_suites) != list(range(len(suites))):
        raise ValueError("Missing/duplicate scan parent membership")
    return rows


@lru_cache(maxsize=16)
def zip_xml(path):
    """Read reports in place, preserving report paths and duplicate occurrences."""
    rows, reports, problems = [], [], []
    with ZipFile(path) as z:
        for member in z.infolist():
            if not member.filename.lower().endswith(".xml"):
                continue
            raw = z.read(member)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                problems.append(member.filename + ": DTD/entity declarations not evaluated")
                continue
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                if (Path(member.filename).name.startswith("TEST-")
                        or any(part in member.filename for part in ("surefire-reports/", "failsafe-reports/", "test-results/"))):
                    problems.append(member.filename + ": malformed potential test report")
                continue  # Unrelated source XML is not assumed to be a JUnit report.
            if root.tag.rsplit("}", 1)[-1] not in {"testsuite", "testsuites"}:
                continue
            local = []
            suites = [s for s in root.iter() if s.tag.rsplit("}", 1)[-1] == "testsuite"
                      and not any(c.tag.rsplit("}", 1)[-1] == "testsuite" for c in s)]
            for si, suite in enumerate(suites):
                cases = [c for c in suite if c.tag.rsplit("}", 1)[-1] == "testcase"]
                start = len(local)
                for ci, case in enumerate(cases):
                    states = {c.tag.rsplit("}", 1)[-1] for c in case} & {"failure", "error", "skipped"}
                    if len(states) > 1 or not case.get("name") or not case.get("classname"):
                        problems.append(member.filename + ": conflicting/missing case identity or outcome")
                    outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}.get(next(iter(states), None), "passed")
                    local.append({"identity": {"report": member.filename, "suite": suite.get("name"),
                        "class_name": case.get("classname"), "name": case.get("name")}, "outcome": outcome,
                        "source_pointer": f"{member.filename}#suite/{si}/case/{ci}"})
                try:
                    c = Counter(r["outcome"] for r in local[start:])
                    declared = [int(suite.get(k, "0")) for k in ("tests", "failures", "errors", "skipped")]
                    if declared != [len(cases), c["failed"], c["error"], c["skipped"]]:
                        problems.append(member.filename + ": suite/case count conflict")
                except ValueError:
                    problems.append(member.filename + ": invalid declared suite counts")
            rows.extend(local)
            reports.append({"member": member.filename, "sha256": digest(raw), "bytes": len(raw), "case_rows": len(local)})
    return rows, reports, sorted(set(problems))


def review_row(base, task):
    review = read(base / task["review_source"]["file"])
    row = review.get("rows", review.get("decisions"))[task["review_source"]["row_index"]]
    if row["repo"] != task["repo"]:
        raise ValueError("Review/task repository join mismatch")
    return row


def capture_request_ref(base, capture_path, url, local_ref):
    """Bind existing response bytes to one recorded successful endpoint request."""
    capture = source(base, capture_path)
    records = read(base / capture["path"])
    matches = [r for r in records if r.get("url") == url and r.get("status") == 200
               and r.get("final_url", url) == url]
    if len(matches) != 1:
        raise ValueError("Request URL not uniquely bound in archived capture")
    response = source(base, local_ref)
    request = matches[0]
    if request.get("sha256") != response["sha256"] or request.get("bytes") != response["bytes"]:
        raise ValueError("Capture request/response hash or byte count mismatch")
    return capture, response | {"url": url, "hash_basis": "matches_archived_capture_request"}


def github_context(base, task):
    ci = task["ci_identity"]
    run, attempt = ci.get("run_id"), ci.get("run_attempt")
    directory = base / "raw/actions" / task["repo"] / "runs" / str(run) / f"attempt-{attempt}"
    path = directory / "capture.json"
    if not path.is_file():
        return {"status": "unavailable", "gaps": ["No archived exact-attempt capture at canonical path"], "artifacts": []}
    capture = read(path)
    jobs = ci.get("selected_jobs") or [{"job_id": ci.get("job_id")}]
    selected = [j for j in capture.get("jobs", []) if j["id"] in {v["job_id"] for v in jobs}]
    bound = (capture.get("id") == run and capture.get("run_attempt") == attempt
             and capture.get("head_sha") == task["sha"] and len(selected) == len(jobs)
             and all(j.get("head_sha") == task["sha"] and j.get("run_attempt") == attempt for j in selected))
    gaps = [] if bound else ["Capture run/attempt/job/checkout SHA needs further binding review"]
    log_ref = source(base, capture["logs"]) if capture.get("logs") else None
    log_path = base / log_ref["path"] if log_ref else None
    selected_logs = {}
    if log_path and log_path.is_file():
        with ZipFile(log_path) as z:
            for j in selected:
                matches = []
                for name in z.namelist():
                    if "/" not in name and name.endswith(".txt"):
                        raw = z.read(name)
                        if ("Complete job name: " + j["name"]).encode() in raw:
                            matches.append((name, raw.decode(errors="replace"), digest(raw), len(raw)))
                if len(matches) == 1:
                    selected_logs[j["id"]] = matches[0]
                else:
                    gaps.append("Exact selected complete-job log member not uniquely identified: " + str(j["id"]))
    artifacts = []
    for artifact in capture.get("artifacts", []):
        download = artifact.get("download")
        if not isinstance(download, dict) or not download.get("file") or not (base / download["file"]).is_file():
            continue
        a = {"artifact_id": artifact["id"], "name": artifact.get("name"), "source": source(base, download)}
        workflow = artifact.get("workflow_run", {})
        uploaded = [jid for jid, (_, text, _, _) in selected_logs.items()
                    if re.search(r"(?:Artifact ID\s*(?::|=|is)\s*|artifacts/)" + str(artifact["id"]) + r"\b", text)]
        run_sha_bound = workflow.get("id") == run and workflow.get("head_sha") == task["sha"]
        a.update(selected_job_upload_ids=uploaded, run_sha_bound=run_sha_bound,
                 job_attempt_bound=bool(bound and run_sha_bound and len(uploaded) == 1), promotable=False)
        try:
            rows, reports, errors = zip_xml(str(base / a["source"]["path"]))
            a.update(xml_report_count=len(reports), counts=case_counts(rows) if reports else None, problems=errors,
                     count_basis="Parsed XML record occurrences only; may include redundant report formats or unselected task scopes",
                     parser_status="partial" if errors else "confirmed" if reports else "unavailable",
                     identity_inventory={k:v for k,v in identity_inventory(rows).items() if k != "collisions"})
        except (ValueError, OSError, BadZipFile) as exc:
            a["problems"] = [str(exc)]
        artifacts.append(a)
    return {"status": "confirmed" if bound and not gaps else "partial", "gaps": gaps,
            "capture": source(base, str(path.relative_to(base))), "logs": log_ref,
            "selected_log_members": {str(k): {"member": v[0], "sha256": v[2], "bytes": v[3]} for k,v in selected_logs.items()},
            "artifacts": artifacts, "_selected_logs": selected_logs}


def write_case_archive(out, task, rows):
    directory = out / "case-records"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (task["task_id"] + ".jsonl.gz")
    raw = "".join(canonical(r) + "\n" for r in rows).encode()
    path.write_bytes(gzip.compress(raw, mtime=0))
    return source(out, str(path.relative_to(out)))


def audit_task(base, out, task, canonical_task):
    review = review_row(base, task)
    refs = [source(base, task["review_source"]["file"])]
    vectors, gaps = [], []
    for vector in task["reported_test_results_by_invocation"]:
        try:
            vectors.append({k:v for k,v in vector.items() if k != "counts"} | {"counts": normalize_counts(vector["counts"])})
        except ValueError as exc:
            gaps.append(str(exc))
    result = {"task_id": task["task_id"], "repo": task["repo"], "sha": task["sha"],
        "canonical": canonical_task, "historical_evidence_level": task["evidence_level"],
        "ci_identity": task["ci_identity"], "official_ci_url": task["official_ci_url"], "scope": task["scope"],
        "count_audit": {"status": "partial" if vectors else "unavailable", "vectors": vectors,
                        "basis": "Frozen declared vectors reconciled arithmetically; native scope not yet re-parsed for this task", "gaps": gaps},
        "identity_audit": {"status": "unavailable", "complete_case_records_available": False,
                           "unambiguous_observed_identity_supported": False, "gaps": ["No complete raw case source revalidated for this task"]},
        "artifact_recovery": [], "sources": refs}
    context = None
    if "github.com/" in task["official_ci_url"]:
        try:
            context = github_context(base, task)
            result["artifact_recovery"] = context["artifacts"]
            result["source_binding"] = {k:v for k,v in context.items() if k not in {"artifacts", "_selected_logs"}}
        except (ValueError, OSError, KeyError, TypeError, BadZipFile) as exc:
            result["source_binding"] = {"status": "unavailable", "gaps": [str(exc)]}
    rows, source_bound, parser = None, False, None
    try:
        if task["evidence_level"] == "complete_case_results" and "tests" in review:
            tests = review["tests"]
            rawref = source(base, tests["case_evidence"]); refs.append(rawref)
            data = read(base / rawref["path"])
            native = _parse_report(data)  # Shared native count arithmetic; no synthetic index.
            rows = jenkins_rows(data, tests.get("selected_enclosing_blocks"))
            metaref = source(base, review["evidence"]["selected_metadata"]); refs.append(metaref)
            metadata = read(base / metaref["path"])
            commits = {a.get("lastBuiltRevision", {}).get("SHA1") for a in metadata.get("actions", [])}
            source_bound = metadata.get("url") == task["official_ci_url"] and task["sha"] in commits
            result["source_binding"] = {"status": "confirmed" if source_bound else "partial",
                "basis": "Frozen review binds report bytes to selected native Jenkins build metadata",
                "selected_blocks": tests.get("selected_enclosing_blocks"), "observed_build_url": metadata.get("url"),
                "observed_git_revisions": sorted(c for c in commits if c), "root_native_counts": native}
            parser = "Jenkins raw report + exact selected enclosing blocks + publisher node context"
        elif task["repo"] == "apache/cassandra-java-driver" and task["evidence_level"] == "complete_case_results":
            test = review["test_evidence"]
            rawref = source(base, test["source_report"]); refs.append(rawref)
            data = read(base / rawref["path"]); native = _parse_report(data)
            case_ref = source(base, test["case_report"]); refs.append(case_ref)
            selection = read(base / case_ref["path"])
            rows = jenkins_rows(data, publisher=test["publisher_node_id"])
            ci = task["ci_identity"]
            folder = Path(rawref["path"]).parent
            capture_ref, report_response = capture_request_ref(base, str(folder / "build-capture.json"),
                ci["run_url"] + "testReport/api/json", rawref)
            _, build_ref = capture_request_ref(base, str(folder / "build-capture.json"),
                ci["run_url"] + "api/json", str(folder / f"3x-{ci['number']}-build.json"))
            refs.extend([capture_ref, report_response, build_ref])
            build = read(base / build_ref["path"])
            revisions = {a.get("lastBuiltRevision", {}).get("SHA1") for a in build.get("actions", [])}
            graph_ref = source(base, review["raw_evidence"]["single_cell_graph"])
            checkout_ref = source(base, review["source_checkout"]["evidence"])
            refs.extend([graph_ref, checkout_ref])
            graph = read(base / graph_ref["path"]); checkout = read(base / checkout_ref["path"])
            nodes = {str(n["id"]): n for n in graph["stageFlowNodes"]}
            publisher = nodes.get(str(ci["report_publisher_node_id"]), {})
            source_bound = (selection["source_sha"] == task["sha"] and selection["stage_node"] == ci["stage_node_id"]
                and selection["publisher_node"] == ci["report_publisher_node_id"]
                and selection["run"] == ci["number"] and build.get("url") == ci["run_url"]
                and task["sha"] in revisions and graph["id"] == ci["stage_node_id"]
                and str(checkout["nodeId"]) in nodes and checkout.get("hasMore") is False
                and "Checking out Revision " + task["sha"] in checkout["text"]
                and ci["shell_node_id"] in publisher.get("parentNodes", [])
                and publisher.get("status") == "SUCCESS")
            # The derived record array is checked as a multiset, not used as IDs.
            original = Counter((r["className"],r["name"],r["status"]) for r in selection["records"])
            observed = Counter((r["identity"]["class_name"],r["identity"]["name"],r["native_outcome"]) for r in rows)
            if original != observed: raise ValueError("Selected Cassandra raw and derived occurrence multisets differ")
            result["source_binding"] = {"status": "confirmed" if source_bound else "partial",
                "basis": "Archived request/response hash, run BuildData, selected cell checkout, stage graph and publisher-to-shell parent plus raw case occurrence reconciliation",
                "root_native_counts": native}
            parser = "Jenkins selected publisher; TestNG repeated labels preserved"
        elif task["evidence_level"] == "complete_case_results":
            test = review.get("test_evidence", {})
            if task["repo"] in {"apache/dubbo", "apache/logging-flume"}:
                artifact = test.get("report_artifact", test.get("case_report", {}).get("artifact"))
                rawref = source(base, artifact); refs.append(rawref)
                rows, reports, errors = zip_xml(str(base / rawref["path"]))
                if errors: raise ValueError("; ".join(errors))
                chosen = [a for a in result["artifact_recovery"] if a["source"]["sha256"] == rawref["sha256"]]
                source_bound = len(chosen) == 1 and chosen[0]["job_attempt_bound"]
                result["xml_reports"] = reports
                parser = "Fresh official archived JUnit XML; selected artifact ID in exact job log"
            elif task["repo"] in {"spring-projects/spring-ws", "apache/geode"}:
                case = review.get("case_report") or {"file": test["case_file"], "sha256": test["case_sha256"]}
                rawref = source(base, case); refs.append(rawref)
                rows = scan_rows(read(base / rawref["path"]))
                scan = case.get("scan_id") or test.get("public_scan", "").rstrip("/").split("/")[-1]
                logs = context.get("_selected_logs", {}) if context else {}
                source_bound = bool(context and context["status"] == "confirmed" and any("/s/" + scan in item[1] for item in logs.values()))
                parser = "Develocity work-unit/suite/case parent closure, complete native name/display name"
            elif task["repo"] == "apache/iceberg":
                rawref = source(base, {"file": test["case_file"], "sha256": test["case_sha256"]}); refs.append(rawref)
                case_data = read(base / rawref["path"])
                logref = source(base, case_data["source_log"]); refs.append(logref)
                with ZipFile(base / logref["path"]) as z:
                    log = z.read(case_data["source_log"]["zip_member"]).decode()
                lines = log.splitlines(); rows = []
                declared_terminals = Counter()
                for c in case_data["cases"]:
                    line = lines[c["line"] - 1]
                    label = c["class_name"].rsplit(".",1)[-1] + " > " + c["display_name"]
                    if label + " " + c["outcome"] not in line:
                        raise ValueError("Derived Gradle event identity does not match exact native source line")
                    starts = sum(label + " STARTED" in x for x in lines)
                    ends = sum(label + " " + c["outcome"] in x for x in lines)
                    if starts != 1 or ends != 1: raise ValueError("Gradle event occurrence is not uniquely paired")
                    declared_terminals[(label, c["outcome"])] += 1
                    rows.append({"identity": {"task": c["task"], "class_name": c["class_name"], "display_path": c["display_name"]},
                                 "outcome": c["outcome"].lower(), "source_pointer": "log line " + str(c["line"])})
                observed_terminals = Counter()
                for line in lines:
                    match = re.search(r"\s(\S+ > .+) (PASSED|SKIPPED|FAILED)$", line)
                    if match: observed_terminals[match.groups()] += 1
                if declared_terminals != observed_terminals:
                    raise ValueError("Selected Gradle log contains missing/additional terminal events")
                member = case_data["source_log"]["zip_member"]
                source_bound = bool(context and context["status"] == "confirmed"
                    and context["logs"]["sha256"] == logref["sha256"]
                    and member in {v[0] for v in context.get("_selected_logs", {}).values()})
                parser = "Complete STARTED/terminal Gradle display paths; archived class expansion retained"
        if rows is not None:
            observed = case_counts(rows)
            matches = len(vectors) == 1 and comparable_counts(observed) == comparable_counts(vectors[0]["counts"])
            inv = identity_inventory(rows)
            complete = bool(source_bound and matches)
            result["identity_audit"] = {"status": "confirmed" if complete else "partial", **inv,
                "complete_case_records_available": complete,
                "unambiguous_observed_identity_supported": complete and inv["collision_key_count"] == 0 and not inv["native_record_id_collisions"],
                "unambiguous_identity_scope": "within_this_selected_CI_run_only; not cross-run stable identity",
                "identity_ambiguity": "duplicate_labels_in_same_preserved_execution_context" if inv["collision_key_count"] else None,
                "cross_agent_identity_equivalence": "not_evaluated; namespace mapping and per-case join required",
                "cross_run_identity_equivalence": {"status": "unavailable",
                    "reason": "No second-run case join or stable producer namespace mapping validated. Jenkins child URLs/publisher node IDs and artifact report paths locate observations; they are not portable cross-run identities."},
                "parser": parser, "case_records": write_case_archive(out, task, rows),
                "gaps": ([] if source_bound else ["Selected run/job/attempt/source binding not fully revalidated"])
                        + ([] if matches else ["Raw cases do not match the full selected invocation vector(s)"])}
            result["count_audit"].update(status="confirmed" if complete else "partial", observed_counts=observed,
                native_occurrences_equal_frozen_vector=matches, basis=parser)
    except (ValueError, OSError, KeyError, TypeError, BadZipFile, ET.ParseError) as exc:
        result["identity_audit"]["gaps"].append(str(exc))
        result["count_audit"]["gaps"].append(str(exc))
    # An unselected or incomplete artifact is useful evidence, never an automatic promotion.
    for artifact in result["artifact_recovery"]:
        if artifact.get("counts") is not None:
            artifact["counts_equal_selected_vector"] = len(vectors) == 1 and comparable_counts(artifact["counts"]) == comparable_counts(vectors[0]["counts"])
            artifact["gaps"] = ([] if artifact["job_attempt_bound"] else ["Artifact not uniquely bound to selected job/attempt"])
            if not artifact["counts_equal_selected_vector"]: artifact["gaps"].append("Artifact counts do not cover the selected full test vector")
            artifact["gaps"].append("Count equality alone cannot establish report/execution scope closure or identity equivalence")
        else:
            artifact["counts_equal_selected_vector"] = None
            artifact["gaps"] = ["No parseable JUnit XML reports in this downloaded artifact; no zero-test conclusion inferred"]
    result["status"] = "confirmed" if result["count_audit"]["status"] == "confirmed" else "partial" if vectors else "unavailable"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("output/java-benchmark-20260916"))
    parser.add_argument("--output", type=Path, default=Path("output/java-benchmark-rescreen-20260922/test-evidence-audit"))
    args = parser.parse_args(); base=args.dataset_dir.resolve(); out=args.output.resolve()
    if out.is_relative_to(base): raise ValueError("Audit output must not modify the original dataset directory")
    out.mkdir(parents=True, exist_ok=True)
    dataset = read(base / "dataset.json")
    canonical_ids = {p["canonical_task_id"] for p in dataset["projects"]}
    rows = []
    for task in dataset["reference_tasks"]:
        try:
            result = audit_task(base, out, task, task["task_id"] in canonical_ids)
        except (ValueError,OSError,KeyError,TypeError,BadZipFile) as exc:
            result = {"task_id":task["task_id"],"repo":task["repo"],"sha":task["sha"],"canonical":task["task_id"] in canonical_ids,
                      "historical_evidence_level":task["evidence_level"],"status":"unavailable","error":str(exc)}
        rows.append(result)
    summary = {"tasks":len(rows),"canonical_tasks":sum(r["canonical"] for r in rows),
        "status_counts":dict(Counter(r["status"] for r in rows)),
        "historical_case_claim_tasks":sum(r["historical_evidence_level"]=="complete_case_results" for r in rows),
        "complete_case_record_tasks":sum(r.get("identity_audit",{}).get("complete_case_records_available",False) for r in rows),
        "unambiguous_observed_identity_tasks":sum(r.get("identity_audit",{}).get("unambiguous_observed_identity_supported",False) for r in rows),
        "tasks_with_downloaded_artifacts":sum(bool(r.get("artifact_recovery")) for r in rows)}
    summary.update(
        canonical_complete_case_record_tasks=sum(r["canonical"] and r.get("identity_audit",{}).get("complete_case_records_available",False) for r in rows),
        canonical_within_run_collision_free_tasks=sum(r["canonical"] and r.get("identity_audit",{}).get("unambiguous_observed_identity_supported",False) for r in rows),
        cross_run_identity_comparison="not_evaluated",
        newly_promoted_count_only_tasks=0,
        frozen_invocation_vectors_inventoried=sum(len(r.get("count_audit",{}).get("vectors",[])) for r in rows))
    audit={"schema_version":1,"source_dataset":source(base,"dataset.json"),"script_sha256":digest(Path(__file__).read_bytes()),
        "source_reference_base":str(args.dataset_dir), "normalized_case_reference_base":"this_audit_directory",
        "scope":"Existing archives only; no source dataset mutations, network, builds, models, or agent score changes",
        "status_meaning":{"confirmed":"native case/count evidence and selected task binding revalidated, not agent success or one-to-one cross-run identity",
                          "partial":"complete inventory/arithmetic or raw cases available, but selected scope/source reread incomplete",
                          "unavailable":"input or test evidence cannot be interpreted safely"},
        "summary":summary,"tasks":rows}
    (out/"audit.json").write_text(json.dumps(audit,indent=2,ensure_ascii=False)+"\n")
    lines=["# 已有 CI 测试证据复核", "", "只审阅 2026-09-16 冻结档案；没有新网络采集、构建、模型调用或对旧数据的修改。", "", "```json", json.dumps(summary,indent=2),"```", "",
        "历史的 18/78 是 96 个 canonical 项目中“完整案例记录 / 完整计数”的证据层级。另一个 Commons Net 历史备选任务也有完整案例记录，所以本轮逐条重审的是 19 个 task。一个项目的不同 CI cell/attempt 不相加。",
        "", f"**完整记录、选定运行内无碰撞、跨运行可稳定匹配是三件不同的事。** 本轮确认完整案例记录并保留所有重复项及结果多重集；其中 {summary['unambiguous_observed_identity_tasks']} 个 task（{summary['canonical_within_run_collision_free_tasks']} 个 canonical）在保留的运行上下文内没有标签碰撞。这个数量不是“已能与其他 agent 逐条比较”的数量：Jenkins child URL、publisher node、报告路径含有运行定位信息，仍须映射 producer/module/step 命名空间并与另一运行做显式匹配。本轮没有计算 case 一致率。",
        "", "所有计数采用 `reported = passed + skipped + failed_or_error`、`assessed = reported - skipped`。只在原生来源明确拆分时保留 failure/error；Jenkins 合并红色计数不伪拆。案例重复不会被删除，也不会用数组序号制造身份。",
        "", "| Task | Reported | Skipped | Assessed | 不同上下文标签数 | 碰撞标签数 | 完整记录 |", "|---|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        if r["historical_evidence_level"] != "complete_case_results": continue
        a=r.get("identity_audit",{}); c=r.get("count_audit",{})
        counts=c.get("observed_counts",{})
        lines.append(f"| {r['repo']} ({r['task_id']}) | {counts.get('reported_count','unknown')} | {counts.get('skipped_count','unknown')} | {counts.get('assessed_count','unknown')} | {a.get('distinct_observed_identity_keys','unknown')} | {a.get('collision_key_count','unknown')} | {a.get('complete_case_records_available',False)} |")
    lines += ["", "碰撞例子（完整原始定位和结果多重集保存在 audit.json，完整规范化记录保存在 case-records）：", ""]
    for r in rows:
        inv=r.get("identity_audit",{})
        if not inv.get("collisions"): continue
        collision=inv["collisions"][0]
        label=collision["identity"].get("class_name",collision["identity"].get("suite",""))+"#"+collision["identity"].get("name", "")
        lines.append(f"- {r['repo']}: `{label}` 有 {collision['multiplicity']} 条记录；完整选定范围内共有 {inv['duplicate_excess']} 条超出不同标签数的记录。这些记录仍保留在 reported/assessed 中。")
    lines += ["", "下载附件复核：", "", "| Task | 下载附件数 | 选定 job 已绑定的 XML 记录 | 结论 |", "|---|---:|---|---|"]
    for r in rows:
        artifacts=r.get("artifact_recovery",[])
        if not artifacts: continue
        selected=[a for a in artifacts if a.get("job_attempt_bound") and a.get("counts")]
        values="; ".join(f"{a['artifact_id']}: {a['counts']['reported_count']} reported / {a['counts']['skipped_count']} skipped / {a['counts']['assessed_count']} assessed" for a in selected) or "unavailable"
        conclusion="已在原完整案例组中，不是新增项目" if r.get("identity_audit",{}).get("complete_case_records_available") else "不升级：缺范围、记录冲突或没有可解析报告"
        lines.append(f"| {r['repo']} | {len(artifacts)} | {values} | {conclusion} |")
    lines += ["", "ActiveMQ 的选定 artifact 有 10,836 条 XML 记录，但完整 CI invocation 是 11,401 条；二者 skipped 都是 340，assessed 分别是 10,496 与 11,061，相差 565 条。还有报告层级的计数冲突，因此不能升级为完整 identity。Fory 的选定 artifact 混有多个报告视图且 suite/case 计数不一致，解析出的 occurrence 总数只是诊断值，不是可信完整任务分母。Maven 和 Maven Wrapper 的现有下载件没有可解析的 JUnit XML，这不表示测试数为零。",
        "", "其余 92 个 task 的所有冻结 invocation vector 都已盘点并检查整数/计数守恒；已有 GitHub exact-attempt capture 和下载附件也已核对。它们的完整原生命令/测试 scope 没有在本轮逐个重建，故状态为 partial。这个状态表示本轮审计深度，不表示项目或 CI 执行失败，也不能推断它们永久无法取得案例记录。",
        "", "来源绑定包括原 dataset/review 文件和字节散列，Jenkins 原始 testReport 与选定 build/cell，上下文过滤，GitHub run/attempt/job/head SHA 与上传 artifact ID，或 exact-job 日志中的 Develocity scan 链接。复核沿用原审阅已声明的执行范围；不将本轮同计数校验包装成全新 command scope 证明。Iceberg 是完整 Gradle STARTED/terminal 展示路径记录，沿用原档案的源码类名展开，不声称它是 JUnit unique ID。",
        "", "`sources` 的相对路径基于 `output/java-benchmark-20260916`；`case_records` 基于本报告目录。原字节与派生记录都有 SHA-256。`confirmed` 只表示本项已有原始测试记录、计数和选定来源绑定通过，不表示 agent 成功、runtime 全面适配或跨运行 identity 已完成。",
        "", "复现：`UV_CACHE_DIR=/private/tmp/setup-agent-uv-cache PYTHONPATH=. uv run --offline --no-sync python scripts/benchmark_test_evidence_audit.py`。"]
    (out/"report.md").write_text("\n".join(lines)+"\n")
    print(json.dumps(summary))


if __name__ == "__main__": main()
