"""Non-JUnit test obligations. Their counts never enter JUnit/CI case totals."""
from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET

from .requirements import bound_file, report_directories


def validate(row):
    rule = row.get("validation", {}).get("rule")
    if rule not in {"maven_invoker", "antunit"}:
        return
    if row.get("kind") != "test":
        raise ValueError("Native test evidence requires a test obligation")
    required_goal = "invoker:run" if rule == "maven_invoker" else "antrun:run"
    validation = row["validation"]
    if validation.get("goals") != [required_goal] or (
        "native_bindings" in validation and len(validation["native_bindings"]) != 1
    ):
        raise ValueError("Native test evidence requires one matching producer occurrence")
    expected = row.get("expectations", {})
    if rule == "maven_invoker":
        names = expected.get("integration_projects")
        if not report_directories(row):
            raise ValueError("Invoker report directories must be explicit")
        if (not isinstance(names, list) or not names or len(set(names)) != len(names)
                or any(not isinstance(n, str) or posixpath.normpath(n) != n
                       or n.startswith(("/", "../")) or not n.endswith("pom.xml") for n in names)):
            raise ValueError("Invoker requires distinct source-declared project POM paths")
    else:
        suites = expected.get("antunit_suites")
        if not isinstance(suites, list) or not suites:
            raise ValueError("AntUnit requires source-declared suites and targets")
        paths = []
        for suite in suites:
            path, names = suite.get("path"), suite.get("targets")
            if (not isinstance(path, str) or path.startswith(("/", "../"))
                    or posixpath.normpath(path) != path or not path.endswith(".xml")
                    or not isinstance(names, list) or not names or len(set(names)) != len(names)
                    or any(not isinstance(n, str) or not re.fullmatch(r"test[\w.-]+", n) for n in names)):
                raise ValueError("Invalid source-declared AntUnit suite")
            paths.append(path)
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate AntUnit suite")


def invoker(base, reports, expected):
    """Maven Invoker 3.x BuildJob XML, including post-build-hook result."""
    jobs, evidence = {}, []
    allowed = {"success", "skipped", "error", "failure-build", "failure-pre-hook", "failure-post-hook"}
    for ref in reports:
        if ref.get("fresh") is not True:
            raise ValueError("Invoker report freshness unavailable")
        path = bound_file(base, ref)
        root = ET.parse(path).getroot()
        project, outcome = root.get("project"), root.get("result")
        if (root.tag != "build-job" or project not in expected or project in jobs
                or outcome not in allowed or root.get("type", "normal") != "normal"):
            raise ValueError("Invoker project/result identity conflict")
        if outcome == "success" and int(root.get("executionCount", "0")) < 1:
            raise ValueError("Invoker successful job has no executed invocation")
        jobs[project] = outcome
        evidence.append(ref)
    if set(jobs) != set(expected):
        raise ValueError("Invoker declared project reports incomplete")
    counts = {"reported": len(jobs), "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for outcome in jobs.values():
        counts["passed" if outcome == "success" else "skipped" if outcome == "skipped"
               else "errors" if outcome == "error" else "failed"] += 1
    return counts, {"unit": "integration_project", "outcomes": jobs, "evidence_refs": evidence}


def antunit(segment, suites):
    """AntUnit plainlistener records, scoped to one completed AntRun occurrence."""
    headers = list(re.finditer(r"^\[INFO\]\s+\[au:antunit\] Build File: (.+?)\s*$", segment, re.M))
    expected = {suite["path"]: set(suite["targets"]) for suite in suites}
    observed = {}
    counts = {"reported": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for index, header in enumerate(headers):
        matches = [p for p in expected if header[1].endswith("/" + p)]
        if len(matches) != 1 or matches[0] in observed:
            raise ValueError("AntUnit suite identity conflict")
        name = matches[0]
        block = segment[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(segment)]
        summaries = re.findall(r"^\[INFO\]\s+\[au:antunit\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Time elapsed:", block, re.M)
        targets = re.findall(r"^\[INFO\]\s+\[au:antunit\] Target: (\S+) took ", block, re.M)
        if len(summaries) != 1:
            raise ValueError("AntUnit suite summary unavailable")
        total, failed, errors = map(int, summaries[0])
        if (total != len(targets) or len(set(targets)) != len(targets)
                or set(targets) != expected[name] or failed + errors > total):
            raise ValueError("AntUnit target identity/count conflict")
        observed[name] = sorted(targets)
        for key, value in (("reported", total), ("passed", total - failed - errors),
                           ("failed", failed), ("errors", errors)):
            counts[key] += value
    if set(observed) != set(expected):
        raise ValueError("AntUnit declared suites incomplete")
    return counts, {"unit": "antunit_target", "suites": observed}
