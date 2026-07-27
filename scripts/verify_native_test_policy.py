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
import hashlib
import json
import os
import sys

SMOKE_PATH = "tests/python/all-platform-minimal-test"

# Invocation receipts (Plan 5 Stage B, schema v1/v2). Both versions are read:
# v2 only ADDS keys and keeps every v1 key byte-stable, so no assertion here
# may reject a receipt for carrying the newer version.
RECEIPT_DIRNAME = os.path.join(".setup_agent", "invocation_receipts")
RECEIPT_SCHEMA_VERSIONS = (1, 2)

# Where a session's own events may record a receipt's content hash. Plan 5
# sessions record none, so the immutability assertion falls back to integrity.
RECEIPT_HASH_KEYS = ("receipt_sha256", "receipt_content_hash", "receipt_hash")


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


def _junit_counts(meta: dict):
    """(tests, failed, errors, skipped) — None when the attempt reported none."""
    values = [meta.get(key) for key in ("tests", "failed_tests", "error_tests", "skipped_tests")]
    if not all(isinstance(value, int) for value in values):
        return None
    return tuple(values)


def _junit_passed(meta: dict):
    """Non-skipped passes the attempt actually proved — None when unknowable."""
    counts = _junit_counts(meta)
    if counts is None:
        return None
    tests, failed, errors, skipped = counts
    return tests - failed - errors - skipped


def _receipt_files(session: str):
    """Every archived invocation-receipt file, sorted. Empty when none exist."""
    return sorted(glob.glob(os.path.join(session, RECEIPT_DIRNAME, "*.json")))


def _recorded_receipt_hashes(session: str) -> dict:
    """receipt_id -> the LAST content hash the session's own events recorded.

    Plan 5 receipts carry no hash in their ToolResult metadata, so this is
    normally empty; when a later stage records one, the last value wins because
    that is the state the run itself last vouched for.
    """
    hashes: dict[str, str] = {}
    for payload in _tool_results(session):
        meta = (payload.get("result") or {}).get("metadata") or {}
        receipt_id = str(meta.get("receipt_id") or "").strip()
        if not receipt_id:
            continue
        for key in RECEIPT_HASH_KEYS:
            digest = str(meta.get(key) or "").strip().lower()
            if digest:
                hashes[receipt_id] = digest
                break
    return hashes


def _all_skipped(meta: dict) -> bool:
    """A clean run in which every selected test skipped — proves no capability."""
    counts = _junit_counts(meta)
    if counts is None:
        return False
    tests, failed, errors, skipped = counts
    return tests >= 1 and skipped == tests and failed == 0 and errors == 0


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
            if kind == "forced_action":
                # Harness-forced attempts pair forced_action <-> tool_result
                # (their provenance rides action_sha256, not an envelope).
                envelopes[payload.get("envelope_id")] = payload
            elif kind == "action_envelope":
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

    def assert_receipts_immutable(self) -> None:
        """A finalized receipt is never rewritten (Plan 6 Stage 0, spec §C4).

        Two branches, one assertion:

        * the session's events recorded a content hash for a receipt — the file
          on disk must still hash to it, so the ``mark_semantic_failure``-style
          rewrite this stage deletes grades as a FAILURE;
        * no hash was recorded (every Plan 5 session) — the file must still be
          non-empty, parseable JSON of a schema version we read, which is the
          integrity half of immutability: a truncated or half-written receipt is
          exactly the state an interrupted rewrite leaves behind.

        A session with no receipt files asserts nothing (absent facts stay
        absent), so receipt-free recorded sessions keep their assertion set.
        """
        files = _receipt_files(self.session)
        if not files:
            return
        recorded = _recorded_receipt_hashes(self.session)
        problems: list[str] = []
        for path in files:
            receipt_id = os.path.basename(path)[: -len(".json")]
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError as exc:
                problems.append(f"{path}: unreadable ({exc})")
                continue
            expected = recorded.get(receipt_id)
            if expected:
                digest = hashlib.sha256(body).hexdigest()
                if digest != expected:
                    problems.append(
                        f"{path}: content sha256 {digest} does not match the last hash "
                        f"the session recorded for it ({expected}) — the receipt was rewritten"
                    )
                continue
            if not body.strip():
                problems.append(f"{path}: receipt file is empty")
                continue
            try:
                payload = json.loads(body)
            except (TypeError, ValueError) as exc:
                problems.append(f"{path}: truncated or unparseable JSON ({exc})")
                continue
            if not isinstance(payload, dict):
                problems.append(f"{path}: receipt is not a JSON object")
            elif payload.get("schema_version", 1) not in RECEIPT_SCHEMA_VERSIONS:
                problems.append(
                    f"{path}: unsupported schema_version {payload.get('schema_version')!r}"
                )
        self.check("receipts.immutable", not problems, "; ".join(problems[:5]))

    def _verdict(self) -> dict:
        with open(os.path.join(self.session, ".setup_agent", "verdict.json")) as handle:
            return json.load(handle)

    def _receipt(self):
        path = os.path.join(self.session, ".setup_agent", "native_smoke_receipt.json")
        if not os.path.exists(path):
            return None
        with open(path) as handle:
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
            if _all_skipped(meta):
                self.check(
                    f"tvm.attempt{index}.no_receipt_on_all_skipped",
                    not meta.get("smoke_receipt_written"),
                    f"all {meta.get('tests')} selected tests skipped, "
                    "yet smoke_receipt_written is set (capability NOT proven)",
                )
                # Plan 5 Stage E anchor: the skip reasons are projected facts,
                # not silent labels — the model must see WHY nothing ran.
                reasons = meta.get("smoke_skip_reasons")
                self.check(
                    f"tvm.attempt{index}.skip_reasons_projected",
                    isinstance(reasons, list) and len(reasons) >= 1,
                    f"smoke_skip_reasons={reasons!r}",
                )
            # A mint only counts when this attempt's junit counts back it: an
            # all-skipped receipt proves nothing, so it cannot unlock a later
            # full collect.
            if meta.get("smoke_receipt_written"):
                passed = _junit_passed(meta)
                if isinstance(passed, int) and passed >= 1:
                    receipt_minted = True

        receipt = self._receipt()
        if receipt is not None:
            passed = (receipt.get("stats") or {}).get("passed")
            self.check(
                "tvm.receipt.positive_evidence",
                isinstance(passed, int) and passed >= 1,
                f"native_smoke_receipt.json stats.passed={passed!r} "
                f"(stats={receipt.get('stats')!r})",
            )

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
        stats = verdict.get("test_stats") or {}
        unique = stats.get("unique") or {}
        passed = unique.get("passed", stats.get("passed", 0))
        self.check("bigtop.primary.anchor", int(passed or 0) >= 50, f"passed={passed}")
        # Plan 5 anchors (ground-truth review): the primary count is
        # receipt-scoped and EXACTLY the data-generators 50 — auxiliary
        # test-framework passes are quarantined, never merged.
        self.check(
            "bigtop.primary.receipt_scoped",
            stats.get("receipt_scoped") is True,
            f"receipt_scoped={stats.get('receipt_scoped')!r}",
        )
        if stats.get("receipt_scoped"):
            self.check(
                "bigtop.primary.exactly_50",
                int(passed or 0) == 50,
                f"passed={passed} (auxiliary leaked into the primary count)",
            )
        # The stale consumers (spark 3.6, transaction 3.5) can never close
        # green on this checkout — blocked when a literal incompatibility was
        # derivable, otherwise honestly failed at attempt time. Producer
        # coordinates are Groovy-derived (nothing literal), so the graph seals
        # name-only "unverified" edges rather than inventing blockers.
        domain_states = (verdict.get("build_evidence") or {}).get("domain_states") or {}
        states = {r: (s or {}).get("state") for r, s in domain_states.items()}
        # untried is an honest terminal state here: with unverified edges
        # named pre-attempt, not attempting a doomed consumer is legitimate —
        # the truth table already forbids global success while it lasts.
        stale_consumers = [
            r
            for r in states
            if r.endswith("bigpetstore-spark") or r.endswith("bigpetstore-transaction-queue")
        ]
        self.check(
            "bigtop.domains.stale_consumers_not_green",
            len(stale_consumers) == 2
            and all(states[r] in ("failed", "blocked", "untried") for r in stale_consumers),
            f"domain_states={states}",
        )
        self.check(
            "bigtop.verdict.partial",
            verdict.get("verdict") == "partial",
            f"verdict={verdict.get('verdict')!r}",
        )
        requirements_path = os.path.join(self.session, ".setup_agent", "build_requirements.json")
        edges = []
        if os.path.exists(requirements_path):
            with open(requirements_path) as handle:
                edges = json.load(handle).get("domain_edges") or []
        named = [e for e in edges if "bigpetstore-data-generator" in str(e.get("detail") or "")]
        self.check(
            "bigtop.edges.data_generator_linked",
            len(named) >= 2,
            f"domain_edges={[(e.get('status'), (e.get('detail') or '')[:60]) for e in edges]}",
        )

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
    verifier.assert_receipts_immutable()
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
