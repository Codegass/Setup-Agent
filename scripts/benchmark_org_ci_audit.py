"""Audit exact provisional expansion CI pins without promoting task admission.

Only explicitly requested official artifact indexes/reports use network. Source
candidate order and task selection are supplied by the independent collector.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from zipfile import BadZipFile

from scripts.benchmark_ci_recovery import Collector, artifact_indexes, inspect_artifact, log_links, ref, write
from scripts.benchmark_test_evidence_audit import case_counts, digest, identity_inventory, write_case_archive, zip_xml


ROOT = Path(__file__).resolve().parents[1]
PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SUMMARY = re.compile(r"^(?:\[(?:INFO|WARNING|ERROR)\]\s+)?Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$")
PLUGIN = re.compile(r"---\s+(?P<plugin>[^: ]+):[^: ]+:(?P<goal>[^ ]+)\s+\([^)]*\)\s+@\s+(?P<module>[^ ]+)\s+---")


def checked_source(value):
    path = Path(value["path"])
    if not path.is_absolute(): path = ROOT/path
    raw = path.read_bytes()
    if digest(raw) != value["sha256"] or type(value.get("bytes")) is not int or len(raw) != value["bytes"]:
        raise ValueError("Source hash/byte mismatch: " + str(path))
    return raw, ref(path, ROOT)


def bind_selected_log(task):
    """Verify attempt-scoped official metadata and the selected job log receipt."""
    issues, selected, sources = [], [], []
    for value in task.get("jobs_refs", []):
        raw, source = checked_source(value); sources.append(source)
        receipt_raw, receipt_ref = checked_source(value["receipt"]);sources.append(receipt_ref)
        receipt = json.loads(receipt_raw)
        expected = f"https://api.github.com/repos/{task['repo']}/actions/runs/{task['run_id']}/attempts/{task['run_attempt']}/jobs?"
        if (not receipt.get("url", "").startswith(expected) or value.get("url") != receipt.get("url")
                or receipt.get("http_status") != 200 or receipt.get("exit_code") != 0
                or receipt.get("sha256") != source["sha256"] or receipt.get("bytes") != source["bytes"]):
            issues.append("Selected jobs official response binding differs")
        data = json.loads(raw)
        selected.extend(job for job in data["jobs"] if job["id"] in task["selected_job_ids"])
    if len(selected) != len(task["selected_job_ids"]): issues.append("Selected job metadata missing or duplicated")
    for job in selected:
        if any(job.get(key) != task[key] for key in ("run_id", "run_attempt")) or job.get("head_sha") != task["sha"]:
            issues.append("Job run/attempt/commit differs from candidate pin")
    value = task.get("job_log_ref")
    if not value:
        return {"status":"unavailable", "issues":issues+[task.get("job_log_error") or "Selected log unavailable"], "sources":sources}, None
    raw, source = checked_source(value)
    receipt_raw, receipt_ref = checked_source(value["receipt"])
    receipt = json.loads(receipt_raw)
    expected = f"https://api.github.com/repos/{task['repo']}/actions/jobs/{task['job_id']}/logs"
    if (receipt.get("url") != expected or value.get("url") != expected
            or receipt.get("http_status") != 200 or receipt.get("exit_code") != 0
            or receipt.get("sha256") != source["sha256"] or receipt.get("bytes") != source["bytes"]):
        issues.append("Selected log official response binding differs")
    if task["selected_job_ids"] != [task["job_id"]]:
        issues.append("This adapter requires separate logs for each selected job")
    return {"status":"confirmed" if not issues else "unavailable", "issues":issues,
            "sources":sources+[source,receipt_ref]}, {"job_id":task["job_id"],"source":source,"text":raw.decode(errors="replace")}


def native_observations(text):
    """Keep native per-execution summaries; never sum retries or quiet repeats."""
    groups, current, plugin = [], None, None
    for number, original in enumerate(text.splitlines(),1):
        line = ANSI.sub("",PREFIX.sub("",original))
        if "##[group]" in line:
            if current: current["line_end"] = number-1
            command = line.split("##[group]",1)[1]
            current={"line_start":number,"line_end":None,"group":command,
                     "contains_build_tool":bool(re.search(r"(?:^|[ /])(?:mvnw?|gradlew?)(?:\s|$)",command)),
                     "native_test_summaries":[],"build_markers":[]}
            groups.append(current); plugin=None
        match = PLUGIN.search(line)
        if match: plugin=match.groupdict()
        summary=SUMMARY.fullmatch(line)
        if summary and current:
            reported,failed,errors,skipped=map(int,summary.groups())
            valid=reported>=failed+errors+skipped
            current["native_test_summaries"].append({"line":number,"producer":plugin,
                "reported_count":reported,"failed_count":failed,"error_count":errors,
                "skipped_count":skipped,"assessed_count":reported-skipped,
                "passed_count":reported-failed-errors-skipped,"conservation_valid":valid})
        if current and re.search(r"\bBUILD (?:SUCCESS|FAILURE)\b",line):
            current["build_markers"].append({"line":number,"value":line.strip()})
    if current:current["line_end"]=len(text.splitlines())
    return [g for g in groups if g["contains_build_tool"] or g["native_test_summaries"]]


def audit_task(task, collector, *, collect_reports=False):
    binding, log = bind_selected_log(task)
    row={"repo":task["repo"],"repository_id":task["repository_id"],"sha":task["sha"],
        "ci_identity":{key:task[key] for key in ("run_id","run_attempt","job_id","selected_job_ids")},
        "official_ci_url":task["official_ci_url"],"conclusion":task.get("conclusion"),
        "selection_status":"provisional_scope_and_search_extent_review_pending",
        "admitted":False,"selected_log_binding":binding["status"],"source_binding":binding,
        "native_execution_observations":native_observations(log["text"]) if log else [],
        "whole_task_test_counts":{"status":"unavailable","reason":"Complete ordered task/scope and repeated quiet executions are not certified by isolated summary lines"},
        "complete_case_records_available":False,"cross_run_identity_equivalence":"not_evaluated"}
    row.update(log_links([log]) if log else {"scan_links":[],"artifact_uploads":[],"no_files_upload_messages":[]})
    index, artifacts = artifact_indexes(row,collector,{})
    row["artifact_index"]=index
    row["artifacts"]=[inspect_artifact(row,item,collector.max_report_bytes) for item in artifacts]
    for artifact in row["artifacts"]:
        if artifact["disposition"] != "report_download_candidate":continue
        response=collector.get(artifact["url"]);artifact["download"]=response
        if response["status"] != "available":continue
        try:
            records, members, errors=zip_xml(collector.out/response["body"]["path"])
            key=digest(f"{task['repo']}:{task['run_id']}:{task['run_attempt']}:{artifact['artifact_id']}".encode())[:20]
            artifact["parsed_report"]={"case_records":write_case_archive(collector.out,{"task_id":key},records),
                "xml_members":members,"errors":errors,"observed_counts":case_counts(records),
                "identity_inventory":identity_inventory(records),"complete_producer_scope":False}
        except (ValueError, OSError, BadZipFile) as exc:
            artifact["parse_error"]=str(exc)
        # Bare artifact identity never closes the producer scope by count equality.
    if binding["status"] != "confirmed":row["case_evidence_status"]="selected_log_unavailable_or_unbound"
    elif index["status"] != "confirmed":row["case_evidence_status"]="artifact_index_unavailable"
    elif any(a["report_hint"] and a["job_attempt_commit_bound"] for a in row["artifacts"]):
        row["case_evidence_status"]="selected_report_requires_scope_review"
    else:row["case_evidence_status"]="no_selected_testcase_report_upload_observed"
    return row


def main():
    p=argparse.ArgumentParser(description=__doc__)
    base=ROOT/"output/java-benchmark-org-expansion-20260922"
    p.add_argument("--selected",type=Path,default=base/"fasterxml/selected-ci.json")
    p.add_argument("--candidates",type=Path,default=base/"fasterxml/candidates.json")
    p.add_argument("--output",type=Path,default=base/"ci")
    p.add_argument("--collect",action="store_true");p.add_argument("--collect-reports",action="store_true")
    a=p.parse_args(); selected=json.loads(a.selected.read_text()); candidates=json.loads(a.candidates.read_text())
    snapshots=[]
    for original in (a.selected,a.candidates):
        raw=original.read_bytes();path=a.output/"source-snapshots"/(digest(raw)+".json")
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        snapshots.append(ref(path.resolve(),ROOT))
    collector=Collector(a.output.resolve(),a.collect or a.collect_reports)
    tasks=[audit_task(t,collector,collect_reports=a.collect_reports) for t in selected["tasks"]]
    represented={t["repository_id"] for t in tasks}
    pending=[{"repo":c["repo"],"repository_id":c["repository_id"],"status":"no_selected_ci_task_yet"}
             for c in candidates["candidates"] if c["repository_id"] not in represented]
    result={"schema":"org-ci-evidence-audit-v1","organization":selected["organization"],
        "generation_script":ref(Path(__file__).resolve(),ROOT),
        "source_script":ref(ROOT/"scripts/benchmark_ci_recovery.py",ROOT),
        "selected_candidates_source":snapshots[0],"population_source":snapshots[1],
        "source_selection_complete":selected.get("complete"),"task_selection_provisional":True,
        "summary":{"metadata_candidates":len(candidates["candidates"]),"ci_candidate_pins":len(tasks),
                   "pending_ci_selection":len(pending),"case_evidence_status":dict(Counter(t["case_evidence_status"] for t in tasks)),
                   "complete_case_records":0,"requirements_v2_ready":0},"tasks":tasks,"pending":pending}
    write(a.output/"fasterxml-evidence-audit.json",result)
    lines=["# FasterXML CI test-evidence audit", "", "This is a provisional exact-job evidence inventory, not task admission or a complete CI denominator. Candidate selection and searched extent remain under independent review. No workflow or build was started.", "",
           f"Metadata candidates: {len(candidates['candidates'])}; CI candidate pins: {len(tasks)}; no selected CI candidate yet: {len(pending)}.","",
           "| Repository | Selected evidence disposition | Native reported/skipped observations |", "|---|---|---|"]
    for row in tasks:
        counts=[f"{s['reported_count']}/{s['skipped_count']}" for g in row['native_execution_observations'] for s in g['native_test_summaries']]
        lines.append(f"| {row['repo']} | {row['case_evidence_status']} | {', '.join(counts) or 'unavailable'} |")
    lines.extend(["", "Numbers are separate native Surefire/Failsafe execution observations, not added across repeated commands, modules or cells. Assessed = reported − skipped only within each observed execution. The JSON preserves command groups, line numbers, producer scope, failed/error counts and raw hash references.", "",
        "Uploaded JaCoCo CSV is coverage data, not testcase identity evidence. No selected testcase report was observed in the readable job logs and complete run artifact indexes. Missing/expired logs do not prove tests were absent. Search scope is these exact candidate pins only; this is not proof that the repositories never publish test reports.","",
        "Complete case records, within-run collision-free labels, cross-run equivalence and requirements-v2 readiness are separate: none is newly certified by this inventory. Native observations can still guide later task definition and reference selection without suppressing quiet repeated tests, docs or quality gates.",""])
    (a.output/"FASTERXML_REPORT.md").write_text('\n'.join(lines))
    print(result["summary"])


if __name__=="__main__":main()
