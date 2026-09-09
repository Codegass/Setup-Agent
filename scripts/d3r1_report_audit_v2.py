"""Versioned, read-only R8 audit. Historical receipt metrics are never rewritten.

The archive replay raises the sample cap only in this offline subprocess so its
raw execution counts can be compared with the old physical-pool audit. It is not
an authorized receipt producer and does not create module or CI identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from sag.agent.receipt_suite_totals import receipt_suite_totals
from sag.agent.receipt_test_rows import _row_parser_program, aggregate_testcase_execution_rows

VERSION = "d3r1-report-audit-v2"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipt_counts(receipts):
    rows = [
        row
        for receipt in receipts
        for row in receipt.get("testcase_execution_rows", {}).get("rows", [])
    ]
    counts = aggregate_testcase_execution_rows(rows)["claimed"]
    executions = Counter()
    row_tier = []
    for receipt in receipts:
        totals = receipt_suite_totals(receipt)
        if totals is None:
            row_tier.extend(receipt.get("testcase_execution_rows", {}).get("rows", []))
        else:
            executions.update(
                executed=totals.tests,
                passed=totals.passed,
                failed=totals.failed,
                errors=totals.errors,
                skipped=totals.skipped,
            )
    executions.update(aggregate_testcase_execution_rows(row_tier)["claimed"]["receipt_executions"])
    counts["receipt_executions"] = dict(executions)
    return counts


def physical_replay(directory, receipts):
    latest = {}
    for receipt in receipts:
        for bucket in ("new", "changed"):
            for entry in receipt["report_delta"][bucket]:
                latest[entry["path"]] = entry["sha256"]
    errors, claims, formats = [], [], Counter()
    with tempfile.TemporaryDirectory(prefix="sag-r8-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        members = {}
        for archive in sorted(directory.glob("physical-test*.tar.gz")):
            tar = stack.enter_context(tarfile.open(archive))
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = "/workspace/" + member.name.removeprefix("./")
                if name in members:
                    errors.append({"path": name, "reason": "duplicate archived path"})
                members[name] = tar, member
        for name, expected in sorted(latest.items()):
            if name not in members:
                errors.append({"path": name, "reason": "claimed report absent"})
                continue
            tar, member = members[name]
            stream = tar.extractfile(member)
            assert stream is not None
            raw = stream.read()
            if hashlib.sha256(raw).hexdigest() != expected:
                errors.append({"path": name, "reason": "claimed digest mismatch"})
                continue
            relative = Path(name.removeprefix("/workspace/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe archive claim")
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            claims.append({"path": str(path), "sha256": expected})
            formats[
                (
                    "testng"
                    if path.name == "testng-results.xml"
                    else "failsafe-summary" if path.name == "failsafe-summary.xml" else "junit"
                )
            ] += 1
        manifest = root / "claims.json"
        manifest.write_text(json.dumps(claims))
        program = _row_parser_program().replace("TOTAL_ROW_CAP = 2048", "TOTAL_ROW_CAP = 1000000")
        result = subprocess.run(
            [sys.executable, "-c", program, str(manifest)],
            check=True,
            capture_output=True,
            text=True,
        )
        parsed = json.loads(result.stdout)
        rows = parsed.pop("rows")
        counts = Counter(row["outcome"] for row in rows)
        return {
            "basis": "offline raw execution rows after verified alternate-format reconciliation",
            "identity_authority": False,
            "format_counts": dict(formats),
            "complete": parsed["status"] == "complete"
            and not errors
            and not parsed["bounds"]["unparsed_reports"],
            "raw_execution_rows": len(rows),
            "outcomes": dict(counts),
            "parser": parsed,
            "archive_errors": errors,
        }


def audit(archive):
    baseline_path = archive / "evaluation.json"
    baseline = json.loads(baseline_path.read_bytes())
    projects, total_receipts = [], 0
    for old in baseline["projects"]:
        directory = archive / "runs" / old["seat"]
        receipts_path = directory / "audit/receipts.json"
        receipts = json.loads(receipts_path.read_bytes())
        total_receipts += len(receipts)
        counts = receipt_counts(receipts)
        mismatches = []
        for grain, values in counts.items():
            expected = old["metrics"]["tests"]["claimed"][grain]
            if expected["executed"] is None:
                continue
            for field, value in values.items():
                if value != expected[field]:
                    mismatches.append(
                        {"grain": grain, "field": field, "old": expected[field], "current": value}
                    )
        item = {
            "project": old["seat"],
            "receipts": len(receipts),
            "receipt_source_sha256": digest(receipts_path),
            "sealed_counts": counts,
            "published_count_mismatches": mismatches,
            "original_pool": {
                key: old["physical_pool"][key]
                for key in ("complete", "report_count", "raw_testcase_rows", "executed")
            },
            "original_declaration_conflicts": old["physical_pool"]["declared_count_mismatches"],
        }
        if old["seat"] in ("curator", "struts", "seatunnel"):
            item["replay"] = physical_replay(directory, receipts)
        projects.append(item)
    return {
        "version": VERSION,
        "archive": str(archive),
        "baseline_evaluation_sha256": digest(baseline_path),
        "script_sha256": digest(Path(__file__)),
        "production_row_parser_sha256": hashlib.sha256(_row_parser_program().encode()).hexdigest(),
        "offline_total_row_cap": 1000000,
        "receipts_examined": total_receipts,
        "historical_receipts_rewritten": False,
        "published_count_mismatches": sum(len(p["published_count_mismatches"]) for p in projects),
        "projects": projects,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("logs/d3r1-20260907"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.archive.resolve() in args.output.resolve().parents:
        parser.error("output must be new and outside the sealed archive")
    value = audit(args.archive)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value[key]
                for key in ("version", "receipts_examined", "published_count_mismatches")
            }
        )
    )


if __name__ == "__main__":
    main()
