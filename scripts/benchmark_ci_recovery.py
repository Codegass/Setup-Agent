"""Recover official test-evidence indexes for the fixed 92 archived tasks.

No latest-run substitution, project execution, model requests or archive mutation.
Network collection is explicit (--collect); existing responses are reused by hash.
Download byte limits are operational deferrals, never dataset admission thresholds.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

from scripts.benchmark_test_evidence_audit import (
    case_counts, comparable_counts, digest, identity_inventory, jenkins_rows,
    normalize_counts, read, review_row, source, write_case_archive, zip_xml,
)
from sag.benchmark.ci_count_semantics import _parse_report


REPORT_TREE = "totalCount,passCount,failCount,skipCount,suites[name,enclosingBlockNames,nodeId,cases[className,name,status,skipped]],childReports[child[url],result[passCount,failCount,skipCount,suites[name,enclosingBlockNames,nodeId,cases[className,name,status,skipped]]]]"
BUILD_TREE = "number,url,result,building,actions[_class,lastBuiltRevision[SHA1],revision],artifacts[fileName,relativePath],runs[number,url,result]"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def ref(path, base):
    data = path.read_bytes()
    return {"path": str(path.relative_to(base)), "sha256": digest(data), "bytes": len(data)}


def safe_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class Collector:
    """Persist raw responses and minimal receipts without logging credentials."""
    def __init__(self, out, collect=False, max_report_bytes=64 * 1024 * 1024):
        self.out = out
        self.collect = collect
        self.max_report_bytes = max_report_bytes
        self.token = None
        if collect:
            self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if not self.token:
                result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
                if result.returncode == 0:
                    self.token = result.stdout.strip()

    def get(self, url, *, limit=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Only public HTTPS source endpoints are supported")
        key = digest(url.encode())
        folder = self.out / "responses" / key
        receipt_path = folder / "receipt.json"
        if receipt_path.is_file():
            receipt = read(receipt_path)
            if receipt.get("url") != url or receipt.get("method") != "GET":
                raise ValueError("Cached response request URL or method mismatch")
            body = (self.out / receipt["body"]["path"]).resolve()
            if body != (folder/"body.bin").resolve():
                raise ValueError("Cached response body path differs from request directory")
            raw = body.read_bytes()
            if (digest(raw) != receipt["body"]["sha256"]
                    or type(receipt["body"].get("bytes")) is not int or len(raw) != receipt["body"]["bytes"]):
                raise ValueError("Collected response hash or bytes changed: " + str(body))
            return receipt | {"receipt": ref(receipt_path, self.out), "cached": True}
        if not self.collect:
            return {"url": url, "status": "not_requested", "http_status": None}
        folder.mkdir(parents=True, exist_ok=True)
        body = folder / "body.bin"
        headers = folder / "headers.tmp"
        byte_limit = limit or self.max_report_bytes
        command = ["curl", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https",
                   "--max-time", "45", "--max-filesize", str(byte_limit), "--dump-header", str(headers),
                   "--output", str(body), "--write-out", "%{http_code}\n%{url_effective}",
                   "--header", "User-Agent: SAG-benchmark-evidence-acquisition", "--config", "-", url]
        configuration = ""
        if parsed.hostname == "api.github.com":
            configuration += 'header = "Accept: application/vnd.github+json"\n'
            configuration += 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
            if self.token:
                configuration += 'header = "Authorization: Bearer ' + self.token.replace('"', '') + '"\n'
        started = datetime.now(timezone.utc).isoformat()
        run = subprocess.run(command, input=configuration, capture_output=True, text=True, check=False)
        if not body.exists(): body.write_bytes(b"")
        pieces = run.stdout.strip().splitlines()
        status = int(pieces[0]) if pieces and pieces[0].isdigit() else None
        end_url = pieces[1] if len(pieces) > 1 else url
        selected_headers = {}
        if headers.exists():
            # Exclude redirect URLs: signed artifact URL query parameters are credentials.
            for line in headers.read_text(errors="replace").splitlines():
                if line.startswith("HTTP/"): selected_headers = {}
                if ":" in line:
                    name, value = line.split(":", 1)
                    if name.lower() in {"date", "content-type", "content-length", "etag", "link", "x-ratelimit-remaining", "x-ratelimit-reset"}:
                        selected_headers[name.lower()] = value.strip()
            headers.unlink()
        receipt = {"url": url, "method": "GET", "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(), "http_status": status,
            "curl_exit_code": run.returncode, "complete_transfer": run.returncode == 0,
            "status": "available" if status == 200 and run.returncode == 0 else "permission_denied" if status in {401,403} else "not_found" if status == 404 else "expired" if status == 410 else "transfer_or_http_error",
            "effective_url_without_query": safe_url(end_url), "response_headers": selected_headers,
            "operational_max_bytes": byte_limit, "body": ref(body, self.out)}
        write(receipt_path, receipt)
        return receipt | {"receipt": ref(receipt_path, self.out), "cached": False}

    def json(self, receipt):
        if receipt.get("status") != "available": return None
        try: return read(self.out / receipt["body"]["path"])
        except (ValueError, OSError): return None


def walk_local_refs(value, base):
    """Inventory local report references declared by the existing review."""
    found = {}
    def is_candidate(path):
        if not isinstance(path, str) or not path.endswith(".json") or "\n" in path or len(path)>4096:
            return False
        if not re.search(r"test|report|case|scan",path,re.I): return False
        try:return (base/path).is_file()
        except OSError:return False
    def visit(item):
        if isinstance(item, dict):
            path = item.get("file", item.get("archive", item.get("path")))
            if is_candidate(path):
                found[path] = source(base, item)
            for child in item.values(): visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
        elif is_candidate(item):
            found.setdefault(item, source(base, item))
    visit(value)
    return list(found.values())


def log_inventory(base, task, prior):
    """Return only archived logs already bound to the selected exact job/attempt."""
    binding = prior.get("source_binding", {})
    ci = task["ci_identity"]
    logs = []
    if binding.get("status") == "confirmed":
        zref = binding["logs"]
        if source(base, zref)["sha256"] != zref["sha256"]:
            raise ValueError("Archived log hash mismatch")
        with ZipFile(base/zref["path"]) as z:
            for job_id, member in binding["selected_log_members"].items():
                raw = z.read(member["member"])
                if digest(raw) != member["sha256"]: raise ValueError("Archived job log member hash mismatch")
                logs.append({"job_id":int(job_id), "source":zref, "member":member["member"],
                             "member_sha256":digest(raw), "text":raw.decode(errors="replace")})
    elif ci.get("log", {}).get("metadata") and ci["log"].get("run_metadata"):
        # Curator retained attempt1 directly, not under the generic capture path.
        log = ci["log"]
        run_ref = source(base, log["run_metadata"]); jobs_ref = source(base, log["metadata"])
        run = read(base/run_ref["path"]); jobs = read(base/jobs_ref["path"])
        jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
        selected = [j for j in jobs if j.get("id") == ci["job_id"]]
        if (run.get("id") == ci["run_id"] and run.get("run_attempt") == ci["run_attempt"]
                and run.get("head_sha") == task["sha"] and len(selected) == 1
                and selected[0].get("run_attempt") == ci["run_attempt"] and selected[0].get("head_sha") == task["sha"]):
            log_ref = source(base, log)
            logs.append({"job_id":ci["job_id"], "source":log_ref,"run_metadata":run_ref,"jobs_metadata":jobs_ref,
                         "text":(base/log_ref["path"]).read_text(errors="replace")})
    return logs


def log_links(logs):
    scans, uploads, no_upload = [], [], []
    for log in logs:
        upload_parameters = None
        in_upload_group = False
        for line_no, line in enumerate(log["text"].splitlines(), 1):
            if "##[group]" in line:
                in_upload_group = "Run actions/upload-artifact@" in line
                upload_parameters = {"group_line_start":line_no,"parameters":[]} if in_upload_group else None
            if in_upload_group and re.search(r"\s(?:name|path|retention-days):",line):
                upload_parameters["parameters"].append({"line":line_no,"text":re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s", "",line).strip()})
            if in_upload_group and "##[endgroup]" in line:
                upload_parameters["group_line_end"] = line_no
                in_upload_group = False
            for match in re.finditer(r"https://[^\s<>\"'\x1b]+/s/[A-Za-z0-9]+", line):
                scans.append({"url":match.group(), "job_id":log["job_id"], "line":line_no,
                              "log_source":{k:v for k,v in log.items() if k != "text"}})
            for match in re.finditer(r"(?:Artifact ID\s*(?::|=|is)\s*|artifacts/)(\d+)\b", line):
                uploads.append({"artifact_id":int(match.group(1)),"job_id":log["job_id"],"line":line_no,
                                "upload_action":upload_parameters})
            if "No files were found with the provided path" in line:
                no_upload.append({"job_id":log["job_id"],"line":line_no,"message":line.strip()})
    return {"scan_links":scans, "artifact_uploads":uploads, "no_files_upload_messages":no_upload}


def is_report_artifact(name):
    return bool(re.search(r"(?:surefire|failsafe|junit|test[-_ ]?(?:results?|reports?)|(?:test|junit)[-_ ]?xml|^unit[-_])", name, re.I))


def initial_inventory(base, audit, dataset):
    prior = {t["task_id"]: t for t in audit["tasks"]}
    tasks = []
    for task in dataset["reference_tasks"]:
        if prior[task["task_id"]].get("identity_audit",{}).get("complete_case_records_available"): continue
        review = review_row(base, task)
        row={"task_id":task["task_id"],"repo":task["repo"],"sha":task["sha"],
             "canonical":prior[task["task_id"]]["canonical"],"ci_identity":task["ci_identity"],
             "official_ci_url":task["official_ci_url"],"frozen_test_vectors":task["reported_test_results_by_invocation"],
             "review_source":source(base,task["review_source"]["file"]),
             "archived_artifacts":prior[task["task_id"]].get("artifact_recovery", []),
             "archived_report_candidates":[],"collection_status":"planned","case_recovery":[]}
        candidates=walk_local_refs(review,base)
        row["archived_report_candidates"]=[r for r in candidates if re.search(r"test|report|case|scan",r["path"],re.I) and r["path"].endswith(".json")]
        if "github.com/" in task["official_ci_url"]:
            logs=log_inventory(base,task,prior[task["task_id"]])
            row.update(provider="github_actions", selected_archived_job_logs=[{k:v for k,v in log.items() if k != "text"} for log in logs], **log_links(logs))
            row["selected_log_binding"]="confirmed" if logs else "unavailable"
        else: row["provider"]="apache_jenkins"
        tasks.append(row)
    return tasks


def index_urls(rows):
    urls=set()
    for row in rows:
        if row["provider"] == "github_actions":
            ci=row["ci_identity"]
            urls.add(f"https://api.github.com/repos/{row['repo']}/actions/runs/{ci['run_id']}/artifacts?per_page=100&page=1")
        else:
            urls.add(row["official_ci_url"].rstrip("/")+"/api/json?"+urlencode({"tree":BUILD_TREE}))
            urls.add(row["official_ci_url"].rstrip("/")+"/testReport/api/json?"+urlencode({"tree":REPORT_TREE}))
    return sorted(urls)


def artifact_indexes(row, collector, receipts):
    ci=row["ci_identity"]
    url=f"https://api.github.com/repos/{row['repo']}/actions/runs/{ci['run_id']}/artifacts?per_page=100&page=1"
    pages=[];artifacts=[];seen=set();total=None;issues=[]
    while url:
        if url in seen: issues.append("Pagination repeated URL");break
        seen.add(url)
        receipt=receipts.get(url) or collector.get(url)
        receipts[url]=receipt;pages.append(receipt)
        data=collector.json(receipt)
        if not isinstance(data,dict) or not isinstance(data.get("artifacts"),list):
            issues.append("Artifact index response unavailable or malformed");break
        if type(data.get("total_count")) is not int or data["total_count"]<0:
            issues.append("Artifact index total_count unavailable");break
        if total is not None and total!=data["total_count"]:issues.append("Pagination total_count changed")
        total=data["total_count"];artifacts.extend(data["artifacts"])
        link=receipt.get("response_headers",{}).get("link","")
        match=re.search(r'<([^>]+)>;\s*rel="next"',link)
        next_url=match.group(1) if match else None
        expected_prefix=f"https://api.github.com/repos/{row['repo']}/actions/runs/{ci['run_id']}/artifacts?"
        if next_url and not next_url.startswith(expected_prefix):issues.append("Unexpected pagination URL");break
        url=next_url
    if total is not None and total!=len(artifacts):issues.append("Listed artifacts do not reconcile to total_count")
    if len({a.get("id") for a in artifacts})!=len(artifacts):issues.append("Duplicate artifact IDs across pages")
    return {"status":"confirmed" if not issues else "unavailable", "pages":pages,
            "total_count":total,"listed_count":len(artifacts),"pagination_complete":not issues,"issues":issues},artifacts


def inspect_artifact(row, artifact, limit):
    uploads=[u for u in row.get("artifact_uploads",[]) if u["artifact_id"]==artifact["id"]]
    job_ids={u["job_id"] for u in uploads};workflow=artifact.get("workflow_run",{})
    bound=(row.get("selected_log_binding")=="confirmed" and len(job_ids)==1
           and workflow.get("id")==row["ci_identity"]["run_id"] and workflow.get("head_sha")==row["sha"])
    parameters="\n".join(p["text"] for u in uploads for p in (u.get("upload_action") or {}).get("parameters",[]))
    reports=is_report_artifact(artifact.get("name","")) or bool(re.search(r"surefire|failsafe|test-results|\.xml",parameters,re.I))
    existing=next((a for a in row.get("archived_artifacts",[]) if a["artifact_id"]==artifact["id"]),None)
    size=artifact.get("size_in_bytes")
    if not bound: disposition="not_bound_to_selected_job_attempt"
    elif not reports: disposition="non_report_artifact_not_downloaded"
    elif existing: disposition="report_already_archived"
    elif artifact.get("expired") is True: disposition="expired_report"
    elif type(size) is not int or size<0: disposition="report_size_unknown"
    elif size>limit: disposition="report_download_deferred_by_resource_limit"
    else: disposition="report_download_candidate"
    return {"artifact_id":artifact["id"],"name":artifact.get("name"),"size_in_bytes":size,
        "expired":artifact.get("expired"),"selected_job_ids":sorted(job_ids),"job_attempt_commit_bound":bound,
        "report_hint":reports,"upload_evidence":uploads,"workflow_run":workflow,
        "disposition":disposition,"archived_source":existing.get("source") if existing else None,
        "url":f"https://api.github.com/repos/{row['repo']}/actions/artifacts/{artifact['id']}/zip"}


def assess_case_pool(row, records, *, kind, report_ref, binding, native_counts=None, gaps=None):
    observed=case_counts(records);vectors=[normalize_counts(v["counts"]) for v in row["frozen_test_vectors"]]
    vector_match=len(vectors)==1 and comparable_counts(observed)==comparable_counts(vectors[0])
    issues=list(gaps or [])
    if not binding:issues.append("Exact task source binding is not verified")
    if not vector_match:issues.append("Case pool does not reconcile to one complete frozen invocation vector")
    # A native hierarchical report certifies its report pool. Bare ZIP parity still needs producer scope review.
    scope_closed=native_counts is not None and comparable_counts(native_counts)==comparable_counts(observed)
    if not scope_closed:issues.append("Complete native producer/report scope remains to be reviewed; equal totals alone are insufficient")
    return {"discovery":kind,"source":report_ref,"status":"confirmed" if not issues else "partial",
        "complete_case_records_available":not issues,"counts":observed,"frozen_vector_match":vector_match,
        "native_report_pool_closed":scope_closed,"identity_inventory":identity_inventory(records),
        "cross_run_identity_equivalence":"not_evaluated","gaps":issues}


def recover_archived_jenkins(row, base, out):
    recovered=[];ci=row["ci_identity"]
    if row["provider"]!="apache_jenkins":return recovered
    for candidate in row["archived_report_candidates"]:
        data=read(base/candidate["path"])
        if not isinstance(data,dict) or not ("childReports" in data or "suites" in data):continue
        try:
            native=_parse_report(data);records=jenkins_rows(data)
            metadata_ref=source(base,ci["metadata"]) if ci.get("metadata") else None
            metadata=read(base/metadata_ref["path"]) if metadata_ref else {}
            commits={a.get("lastBuiltRevision",{}).get("SHA1") for a in metadata.get("actions",[])}
            expected=source(base,ci["test_report"]) if ci.get("test_report") else None
            bound=bool(metadata.get("url")==row["official_ci_url"] and row["sha"] in commits
                       and expected and expected["sha256"]==candidate["sha256"])
            result=assess_case_pool(row,records,kind="existing_archive_reinterpretation",
                report_ref=candidate|{"reference_base":"original_dataset"},binding=bound,native_counts=native)
            result["metadata_source"]=metadata_ref
            result["case_records"]=write_case_archive(out,row,records)
            recovered.append(result)
        except (ValueError,KeyError,TypeError) as exc:
            recovered.append({"source":candidate,"status":"unavailable","gaps":[str(exc)]})
    return recovered


def complete_recovery(inventory, collector, base, receipts, *, collect_reports=False):
    rows=inventory["tasks"];plan=[]
    for row in rows:
        row["case_recovery"]=recover_archived_jenkins(row,base,collector.out)
        if row["provider"]=="github_actions":
            index,artifacts=artifact_indexes(row,collector,receipts)
            row["artifact_index"]=index
            row["artifacts"]=[inspect_artifact(row,a,collector.max_report_bytes) for a in artifacts]
            for a in row["artifacts"]:
                if a["disposition"]=="report_download_candidate":plan.append({"kind":"small_selected_test_output","task_id":row["task_id"],"artifact_id":a["artifact_id"],"url":a["url"],"bytes":a["size_in_bytes"]})
            scans={s["url"]:s for s in row.get("scan_links",[])}
            row["scan_indexes"]=[]
            for url,evidence in scans.items():
                if urlsplit(url).hostname not in {"develocity.apache.org","ge.spring.io"}:
                    row["scan_indexes"].append({"url":url,"status":"host_requires_source_review"});continue
                plan.append({"kind":"selected_job_public_scan_index","task_id":row["task_id"],"url":url,"bytes":None})
        else:
            row["jenkins_indexes"]={"build":receipts[row["official_ci_url"].rstrip("/")+"/api/json?"+urlencode({"tree":BUILD_TREE})],
                                     "test_report":receipts[row["official_ci_url"].rstrip("/")+"/testReport/api/json?"+urlencode({"tree":REPORT_TREE})]}
    write(collector.out/"DOWNLOAD_PLAN.json",{"scope":"Selected-task test outputs and public scan indexes only; no binary/cache artifacts", "items":plan})
    report_receipts={}
    for item in plan:
        # Offline mode reads already collected responses; it does not issue requests.
        receipt=collector.get(item["url"]) if collect_reports or not collector.collect else {"url":item["url"],"status":"not_requested"}
        report_receipts[item["url"]]=receipt
    for row in rows:
        for a in row.get("artifacts",[]):
            if a["disposition"] not in {"report_already_archived","report_download_candidate"}:continue
            if a["archived_source"]:
                path=base/a["archived_source"]["path"];report_source=a["archived_source"]|{"reference_base":"original_dataset"};kind="existing_archive_reanalysis"
            else:
                receipt=report_receipts[a["url"]];a["download"]=receipt
                if receipt["status"]!="available":
                    a["disposition"]="report_download_"+receipt["status"];continue
                path=collector.out/receipt["body"]["path"];report_source=receipt["body"]|{"reference_base":"acquisition_directory","url":a["url"]};kind="new_download"
            try:
                records,reports,errors=zip_xml(str(path))
                if not a["archived_source"]:
                    with ZipFile(path) as archive:
                        a["archive_members"]=[{"member":m.filename,"bytes":m.file_size,
                            "sha256":digest(archive.read(m))} for m in archive.infolist() if not m.is_dir()]
            except (ValueError,OSError,BadZipFile) as exc:
                a["disposition"]="report_parse_unavailable";a["parse_error"]=str(exc);continue
            a["xml_report_count"]=len(reports)
            if not reports:
                a["disposition"]="test_output_without_junit_xml";continue
            recovery=assess_case_pool(row,records,kind=kind,report_ref=report_source,binding=a["job_attempt_commit_bound"],gaps=errors)
            recovery["artifact_id"]=a["artifact_id"];recovery["xml_reports"]=reports
            recovery["case_records"]=write_case_archive(collector.out,{"task_id":row["task_id"]+"-"+str(a["artifact_id"])},records)
            row["case_recovery"].append(recovery);a["disposition"]="report_parsed_complete" if recovery["complete_case_records_available"] else "report_parsed_partial"
        for url in dict.fromkeys(s["url"] for s in row.get("scan_links",[])):
            receipt=report_receipts.get(url)
            if not receipt:continue
            row["scan_indexes"].append({"url":url,"response":receipt,"status":"public_app_shell_available_not_test_evidence" if receipt["status"]=="available" else receipt["status"],
                "scan_identity_api_checked":False,"testcase_endpoint_checked":False,
                "scope":"Linked from exact selected job; HTTP 200 app shell does not establish retained scan existence or tests; invocation-to-scan assignment remains to be reviewed"})
        if any(r.get("complete_case_records_available") for r in row["case_recovery"]):row["collection_status"]="complete_records_recovered_identity_review_separate"
        elif row["case_recovery"]:row["collection_status"]="partial_report_records_available"
        elif row.get("scan_links"):row["collection_status"]="scan_index_deeper_review_required"
        elif row["provider"]=="apache_jenkins":row["collection_status"]="original_test_report_endpoint_"+row["jenkins_indexes"]["test_report"]["status"]
        elif row["artifact_index"]["status"]!="confirmed":row["collection_status"]="artifact_index_unavailable"
        elif any(a["disposition"]=="expired_report" for a in row.get("artifacts",[])):row["collection_status"]="selected_test_report_expired"
        elif any(a["disposition"].startswith("report_download_") for a in row.get("artifacts",[])):row["collection_status"]="report_download_pending_or_unavailable"
        elif any(a["disposition"]=="test_output_without_junit_xml" for a in row.get("artifacts",[])):row["collection_status"]="selected_test_outputs_without_case_xml"
        elif row.get("selected_log_binding")!="confirmed":row["collection_status"]="selected_log_binding_unavailable"
        else:row["collection_status"]="no_selected_report_upload_observed"
    inventory["requests"]=list(receipts.values())+list(report_receipts.values())
    summary={"fixed_tasks":len(rows),"status_counts":dict(Counter(r["collection_status"] for r in rows)),
        "complete_records_recovered_tasks":sum(any(c.get("complete_case_records_available") for c in r["case_recovery"]) for r in rows),
        "complete_records_new_download_tasks":sum(any(c.get("complete_case_records_available") and c["discovery"]=="new_download" for c in r["case_recovery"]) for r in rows),
        "network_responses_available":sum(r.get("status")=="available" for r in inventory["requests"]),
        "scan_tasks_requiring_deeper_endpoint_scope_review":sum(bool(r.get("scan_links")) for r in rows)}
    inventory["summary"]=summary
    write(collector.out/"recovery.json",{"schema":"sag-ci-case-recovery-v1","summary":summary,
        "generation_script":inventory["generation_script"],"generation_dependencies":inventory["generation_dependencies"],
        "source_reference_base":inventory["source_reference_base"],"acquisition_reference_base":"this_directory",
        "prior_audit":inventory["prior_audit"],"source_dataset":inventory["source_dataset"],"tasks":rows})
    lines=["# 固定 CI 任务证据补采", "", "保留原有全部92个任务的commit、run、attempt、job/cell；没有选择最新运行替代原任务。", "", "```json",json.dumps(summary,ensure_ascii=False,indent=2),"```", "",
        "完整案例记录与稳定identity分开。旧归档恢复属于重新解释已有证据；新下载获得的记录单列。404仅表示当前原URL不可得，不自动等同于过期；expired只采用官方artifact索引的明确标记。没有观察到选定任务上传报告不等于证明历史上从未产生报告。",
        "", "| Task | 仓库 | 处置 | 完整案例记录恢复 |", "|---|---|---|---|"]
    lines.extend(f"| {r['task_id']} | {r['repo']} | {r['collection_status']} | {any(c.get('complete_case_records_available') for c in r['case_recovery'])} |" for r in rows)
    lines += ["", "TomEE 的原归档可以恢复完整7006条记录：13 skipped、6993 assessed，保留910个碰撞标签和1562条重复excess。属于已有证据的重新解释，不是新下载获得，也没有把它升级为稳定跨运行identity。",
              "", "Ratis 的四份选定unit附件共324466 bytes，均保留原ZIP和逐成员hash；每份仅含output.log、failures、summary.txt与jacoco-combined.exec，没有JUnit XML。不能将覆盖率执行数据或空summary当作完整案例列表。",
              "", "本批共96个不同端点响应：86个索引/原testReport请求与10个小附件/scan页面请求，均保留首次真实采集时间；纯离线重算会标记cached，不能据此再声称新增96次网络请求。原来已下载的ActiveMQ/Fory ZIP单独引用，未重下。",
              "", "每项原生reported/skipped/assessed、来源字节hash、重复标签多重集和缺口见recovery.json；响应URL/HTTP状态/采集时间/bytes/SHA-256见responses及inventory.json。所有下载上限均为运行安全限制，触发后待资源审阅，不作为项目淘汰条件。",
              "", "后续仍需对scan的tests API/schema和每次命令的归属进行审阅。六个URL当前只返回Develocity应用页面；HTTP 200不证明scan数据仍存在或案例可得。任何ZIP总数相等都不能代替报告生产者范围闭合。本批不改变旧collection/rescreen，也不宣称requirements完整、跨agent identity一致或campaign就绪。"]
    (collector.out/"REPORT.md").write_text("\n".join(lines)+"\n")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir",type=Path,default=Path("output/java-benchmark-20260916"))
    parser.add_argument("--prior-audit",type=Path,default=Path("output/java-benchmark-rescreen-20260922/test-evidence-audit/audit.json"))
    parser.add_argument("--output",type=Path,default=Path("output/java-benchmark-acquisition-20260922/ci"))
    parser.add_argument("--collect",action="store_true")
    parser.add_argument("--recover",action="store_true",help="Classify indexes and parse available reports without new network by default")
    parser.add_argument("--collect-reports",action="store_true",help="Fetch only the prepared small selected test outputs and public scan indexes")
    parser.add_argument("--max-report-bytes",type=int,default=64*1024*1024)
    args=parser.parse_args();base=args.dataset_dir.resolve();out=args.output.resolve()
    if out.is_relative_to(base) or out.is_relative_to(args.prior_audit.resolve().parent):
        raise ValueError("Acquisition output must not mutate old archive or audit")
    dataset=read(base/"dataset.json");audit=read(args.prior_audit)
    rows=initial_inventory(base,audit,dataset);urls=index_urls(rows)
    inventory={"schema":"sag-ci-evidence-acquisition-v1", "source_dataset":source(base,"dataset.json"),
        "generation_script":{"path":"scripts/benchmark_ci_recovery.py","sha256":digest(Path(__file__).read_bytes()),"bytes":Path(__file__).stat().st_size},
        "generation_dependencies":[ref(Path(__file__).resolve().parents[1]/path,Path(__file__).resolve().parents[1]) for path in
            ("scripts/benchmark_test_evidence_audit.py","src/sag/benchmark/ci_count_semantics.py")],
        "prior_audit":{"path":str(args.prior_audit),"sha256":digest(args.prior_audit.read_bytes())},
        "source_reference_base":str(args.dataset_dir),"scope":"All fixed tasks lacking complete case records; exact original task identities only",
        "operational_policy":{"max_response_or_report_bytes":args.max_report_bytes,"concurrency":4,
            "oversize_policy":"defer resource review, never exclude repository","no_build_model_or_workflow_execution":True},
        "tasks":rows,"requested_index_urls":urls,"requests":[]}
    write(out/"inventory.json",inventory)
    if args.collect_reports and not all((out/"responses"/digest(url.encode())/"receipt.json").is_file() for url in urls):
        raise ValueError("Collect and review the complete fixed index inventory before downloading reports")
    collector=Collector(out,args.collect or args.collect_reports,args.max_report_bytes)
    receipts={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(collector.get,url):url for url in urls}
        for future in as_completed(futures):
            url=futures[future]
            try:receipts[url]=future.result()
            except (ValueError,OSError) as exc:receipts[url]={"url":url,"status":"collection_error","error":str(exc)}
            inventory["requests"]=list(receipts.values());write(out/"inventory.json",inventory)
            if len(receipts)%10==0 or len(receipts)==len(urls):
                print(json.dumps({"completed_indexes":len(receipts),"planned_indexes":len(urls),"status":receipts[url]["status"]}),flush=True)
    inventory["requests"]=list(receipts.values())
    if args.recover or args.collect_reports:
        complete_recovery(inventory,collector,base,receipts,collect_reports=args.collect_reports)
    write(out/"inventory.json",inventory)
    print(json.dumps({"tasks":len(rows),"unique_index_requests":len(urls),"response_statuses":dict(Counter(r['status'] for r in receipts.values()))}))


if __name__=="__main__":main()
