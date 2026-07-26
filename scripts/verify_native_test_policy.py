#!/usr/bin/env python
"""Machine-asserted acceptance verifier (Plan 4 Task 6, audit recommendation 6).

Walks a recorded session directory and asserts the native-test policy and
fact-projection contracts hold — no human reading of logs, no trusting the
run's own summaries. Exit 0 = every assertion passed; exit 1 = failures
listed on stdout.

Usage:
    python scripts/verify_native_test_policy.py SESSION_DIR [--profile tvm|bigtop|cli]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

SMOKE_PATH = "tests/python/all-platform-minimal-test"


def _events(session: str):
    path = os.path.join(session, ".setup_agent", "control_events.jsonl")
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _tool_results(session: str):
    for event in _events(session):
        if event.get("kind") == "tool_result":
            yield event["payload"]


def _pytest_attempts(session: str):
    """Tool results carrying collection metadata — the honest pytest signature."""
    for payload in _tool_results(session):
        meta = ((payload.get("result") or {}).get("metadata") or {})
        if "collection_scope" in meta:
            yield payload, meta


class Verifier:
    def __init__(self, session: str) -> None:
        self.session = session
        self.failures: list[str] = []
        self.passes: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passes.append(name)
        else:
            self.failures.append(f"{name}: {detail}" if detail else name)

    # -- shared assertions -------------------------------------------------

    def assert_pairing_and_hashes(self) -> None:
        from sag.agent.control_events import action_envelope_sha256

        envelopes: dict[str, dict] = {}
        results: list[str] = []
        hash_bad = 0
        for event in _events(self.session):
            kind, payload = event.get("kind"), event.get("payload", {})
            if kind == "action_envelope":
                envelopes[payload["envelope_id"]] = payload
                recomputed = action_envelope_sha256(
                    plan_index=payload.get("plan_index"),
                    tool_call_id=payload.get("tool_call_id"),
                    tool=payload["tool"],
                    exact_params=payload["exact_params"],
                )
                if recomputed != payload["envelope_sha256"]:
                    hash_bad += 1
            elif kind == "tool_result":
                results.append(payload.get("envelope_id"))
        unanswered = set(envelopes) - set(results)
        orphans = [r for r in results if r not in envelopes]
        double = len(results) != len(set(results))
        self.check("pairing.exact", not unanswered and not orphans and not double,
                   f"unanswered={sorted(unanswered)} orphans={orphans} double={double}")
        self.check("envelope.hashes", hash_bad == 0, f"{hash_bad} bad")
        self.check("no.scheduler.events",
                   all(e.get("kind") not in ("scheduler_decision", "planner_response")
                       for e in _events(self.session)))

    def _verdict(self) -> dict:
        with open(os.path.join(self.session, ".setup_agent", "verdict.json")) as handle:
            return json.load(handle)

    def _report_text(self) -> str:
        reports = sorted(glob.glob(os.path.join(self.session, "setup-report-*.md")))
        if not reports:
            return ""
        with open(reports[-1]) as handle:
            return handle.read()

    # -- tvm profile -------------------------------------------------------

    def assert_tvm(self) -> None:
        attempts = list(_pytest_attempts(self.session))
        self.check("tvm.pytest.attempted", bool(attempts), "no pytest attempt found")

        receipt_minted = False
        for index, (_payload, meta) in enumerate(attempts, 1):
            scope = meta.get("collection_scope")
            command = str(meta.get("command") or meta.get("collection_command") or "")
            if not receipt_minted:
                self.check(
                    f"tvm.attempt{index}.scope.filtered", scope == "filtered",
                    f"scope={scope!r} command={command[:120]!r} (full collect without receipt)",
                )
            if scope == "filtered":
                self.check(
                    f"tvm.attempt{index}.command.smoke_path", SMOKE_PATH in command,
                    f"command={command[:160]!r}",
                )
                selected = meta.get("collected_after_deselection")
                if selected is None:
                    selected = meta.get("selected")
                self.check(
                    f"tvm.attempt{index}.selected.bounded",
                    isinstance(selected, int) and 1 <= selected <= 50,
                    f"selected={selected!r}",
                )
            collection_errors = meta.get("collection_errors")
            executed = meta.get("executed")
            if isinstance(collection_errors, int) and collection_errors > 0:
                self.check(
                    f"tvm.attempt{index}.executed.zero_on_collection_failure",
                    executed in (0, None),
                    f"executed={executed!r} with collection_errors={collection_errors}",
                )
            if meta.get("smoke_receipt_written"):
                receipt_minted = True

        verdict = self._verdict()
        stats = verdict.get("test_stats") or {}
        summary = stats.get("collection_error_summary")
        if isinstance(stats.get("collection_errors"), int) and stats["collection_errors"] > 0:
            self.check("tvm.sealed.summary_present", bool(summary))
            report = self._report_text()
            first_line = str(summary or "").splitlines()[0][:80]
            self.check(
                "tvm.report.root_cause_matches_sealed",
                bool(first_line) and first_line in report,
                f"summary head {first_line!r} absent from report",
            )

    # -- regression profiles ----------------------------------------------

    def assert_bigtop(self) -> None:
        verdict = self._verdict()
        unique = (verdict.get("test_stats") or {}).get("unique") or {}
        passed = unique.get("passed", (verdict.get("test_stats") or {}).get("passed", 0))
        self.check("bigtop.primary.anchor", int(passed or 0) >= 50, f"passed={passed}")

    def assert_cli(self) -> None:
        verdict = self._verdict()
        stats = verdict.get("test_stats") or {}
        unique = stats.get("unique") or stats
        ok = (
            int(unique.get("passed", 0)) == 921
            and int(unique.get("failed", 0)) == 0
            and int(unique.get("errors", 0)) == 0
        )
        self.check("cli.canary.921_0_0", ok, f"unique={unique}")
        self.check("cli.verdict.success", verdict.get("verdict") == "success",
                   f"verdict={verdict.get('verdict')!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session")
    parser.add_argument("--profile", choices=["tvm", "bigtop", "cli"], required=True)
    options = parser.parse_args()

    verifier = Verifier(options.session)
    verifier.assert_pairing_and_hashes()
    getattr(verifier, f"assert_{options.profile}")()

    print(f"== {options.profile} :: {options.session}")
    for name in verifier.passes:
        print(f"  PASS {name}")
    for failure in verifier.failures:
        print(f"  FAIL {failure}")
    print(f"  => {len(verifier.passes)} passed, {len(verifier.failures)} failed")
    return 1 if verifier.failures else 0


if __name__ == "__main__":
    sys.exit(main())
