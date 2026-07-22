#!/usr/bin/env python3
"""Category-3 Stage-2 ablation runner (httpcomponents-client keep-set search).

The reviewer-specified Stage-2 for httpcomponents-client. Stage-1/confirm sealed
http as P-pass ∧ F-fail (confirm2: P 3/3 all-anchors, F-r2 real regression), so
per the three-outcome rule the probe enters Stage 2 to find the LOCALLY MINIMAL
keep-set of prescription dimensions.

Protocol (pre-registered in logs/panel-stage2-http/panel-lock.json BEFORE
launch — pre-registration discipline):

  * Fixed-point GREEDY BACKWARD ELIMINATION over the five dims a..e (order a→e):
      a plan_pipeline / b recommendation_fields / c project_brief /
      d objectives_wording / e python_prehoc_guidance
    encoded as five 0/1 chars (1=retained, 0=removed) in PRESCRIPTION_FLAG_NAMES
    order. The FULL keep-set {a,b,c,d,e} = mask 11111 is the BASELINE context
    (confirm2's arm-P; NOT re-run — referenced from the lock).
  * A candidate REMOVES one currently-retained dim → candidate mask. That
    candidate is run ×3 reps at stage S2-<bits> (the collector derives the
    canonical mask from the S2- name; the runner ALSO passes the matching
    <bits> to --prescriptions so the collector's bit-by-bit agreement check
    fires). A dim is DELETED iff 3/3 reps pass ALL pre-registered http anchors
    (panel_category3_evaluator.evaluate_probe('httpcomponents-client', ...)).
  * After EVERY successful deletion, RESCAN the remaining retained dims from a.
    Stop when a full pass over the retained dims deletes NOTHING → the retained
    set is the (locally minimal) keep-set.

Worst-case run budget: 45 candidate runs (5 dims removed one-by-one across at
most 5 rescan passes; 5+4+3+2+1 = 15 candidates × 3 reps).

Idempotent / resumable: every completed run is a "run" ledger row; a candidate
whose three run rows already exist is re-evaluated from the archived artifacts
(never re-run), and its decision row short-circuits the greedy step.

The EXECUTION worktree is pinned at 428fcb1 (the confirm2 code-under-test SHA)
EXPLICITLY — not repo HEAD, not any campaign pin. New runner commits on the
branch do NOT move it. Live side effects reuse run_category3_panel.execute_run
and its helpers VERBATIM (archival + sha256 checksums per run identical to the
panel runner); only the greedy decision logic and the S2 stage naming are new.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

# Direct-execution bootstrap: the in-file 'from scripts.*' / 'from sag.*'
# imports need the REPO ROOT on sys.path (pytest adds it; direct 'python
# scripts/run_...' does not).
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = str(_Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

# Reuse the panel runner's live side effects and ledger helpers VERBATIM — the
# archival + per-run sha256 checksums must be byte-identical to the panel
# runner's execute_run (the raw --record evidence Chenhao hand-verifies).
from scripts.run_category3_panel import (  # noqa: E402
    LedgerEntry,
    append_ledger,
    classify_suite_failures,
    committed_head_sha,
    create_clean_worktree,
    execute_run,
    load_ledger,
    ledger_has,
    run_key,
    run_suite_and_collect_failures,
)

PROBE = "httpcomponents-client"
# The five ablation dimensions a..e, in the collector/prescriptions mask order
# (PRESCRIPTION_FLAG_NAMES). Elimination scans this order a→e.
DIM_ORDER = ("a", "b", "c", "d", "e")
DIM_TO_INDEX = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4}
FULL_KEEP_MASK = "11111"  # {a,b,c,d,e} — confirm2 arm-P baseline context
REPEATS = (1, 2, 3)


class Stage2Error(RuntimeError):
    pass


# --------------------------------------------------------------------------
# mask helpers (pure)
# --------------------------------------------------------------------------
def mask_str(bits: Sequence[int]) -> str:
    if len(bits) != 5 or any(b not in (0, 1) for b in bits):
        raise Stage2Error(f"invalid mask bits: {bits!r}")
    return "".join(str(b) for b in bits)


def retained_dims(mask: str) -> list[str]:
    """The dims currently RETAINED (bit==1) in a mask, in a..e order."""
    return [dim for dim in DIM_ORDER if mask[DIM_TO_INDEX[dim]] == "1"]


def remove_dim(mask: str, dim: str) -> str:
    """Candidate mask that removes one retained dim (sets its bit to 0)."""
    index = DIM_TO_INDEX[dim]
    if mask[index] != "1":
        raise Stage2Error(f"dim {dim!r} is not retained in mask {mask!r}")
    bits = list(mask)
    bits[index] = "0"
    return "".join(bits)


def stage_name(mask: str) -> str:
    """The canonical S2 stage name whose suffix IS the mask (collector derives
    the treatment mask from this; the runner also passes the matching bits to
    --prescriptions so the collector's agreement check fires)."""
    return f"S2-{mask}"


# --------------------------------------------------------------------------
# ledger rows (Stage-2 decisions; run rows come from execute_run)
# --------------------------------------------------------------------------
@dataclass
class RepResult:
    repeat: int
    run_key: str
    verdict: str
    unique_executed: int
    all_anchors_pass: bool
    failing_anchors: list[str] = field(default_factory=list)
    # The rep's position in the Stage-2 total order — taken fresh for a live
    # rep, or recovered from the archived RUN row for a resumed one. Carried on
    # the decision row so the total order stays readable there too.
    run_order_index: int | None = None


@dataclass
class DecisionRow:
    kind: str  # "s2-decision"
    parent_mask: str
    tested_dim: str
    candidate_mask: str
    reps: list[dict[str, Any]]
    decision: str  # "delete" | "keep"

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _append_decision(ledger_path: Path, row: DecisionRow) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row.to_json(), sort_keys=True) + "\n")


def find_decision(rows: Sequence[dict[str, Any]], candidate_mask: str) -> dict[str, Any] | None:
    """A recorded Stage-2 decision for this candidate mask (resume)."""
    for row in rows:
        if row.get("kind") == "s2-decision" and row.get("candidate_mask") == candidate_mask:
            return row
    return None


def recover_run_order_index(rows: Sequence[dict[str, Any]], key: str) -> int | None:
    """The run_order_index a completed RUN row already recorded for this key
    (resume: a re-read rep keeps the index it originally consumed instead of
    taking a fresh one)."""
    for row in rows:
        if row.get("kind") == "run" and row.get("run_key") == key:
            idx = row.get("run_order_index")
            return idx if isinstance(idx, int) else None
    return None


# --------------------------------------------------------------------------
# anchor evaluation of one archived / just-run session
# --------------------------------------------------------------------------
def _evaluate_artifacts(artifacts) -> tuple[bool, list[str]]:
    from scripts.panel_category3_evaluator import all_anchors_pass, evaluate_probe

    results = evaluate_probe(PROBE, artifacts)
    passed = all_anchors_pass(results)
    failing = [r.name for r in results if not r.passed]
    return passed, failing


def _load_and_evaluate(campaign: Path, key: str) -> tuple[str, int, bool, list[str]]:
    """Re-distill an ARCHIVED run and re-evaluate the http anchors (resume: a
    ledgered run is never re-executed — the sealed artifacts are re-read exactly
    as the evaluator reads a live session)."""
    from scripts.panel_category3_evaluator import EvaluationError, load_run_artifacts

    try:
        artifacts = load_run_artifacts(campaign / key)
    except EvaluationError as exc:
        raise Stage2Error(f"cannot re-evaluate archived run {key}: {exc}") from exc
    passed, failing = _evaluate_artifacts(artifacts)
    return artifacts.verdict, artifacts.unique_executed, passed, failing


# --------------------------------------------------------------------------
# run one candidate (3 reps) and decide
# --------------------------------------------------------------------------
class RunOrderCounter:
    """A monotonic 0..N-1 index across every Stage-2 live run (its own total
    order). The collector verifies its presence on new live runs and refuses to
    archive a dropped index.

    Resume correctness (reviewer): the next free index must be recovered from
    ALL archived RUN pins' run_order_index (max+1) — NOT only from s2-decision
    rows. A candidate interrupted after its r1/r2 archived but before its
    decision row was written leaves run rows whose indexes are already spent;
    reading only decision rows re-hands those same indexes to the resumed reps
    (the real 2026-07-19 campaign reused 18,19 and skipped 20). Every run row
    now persists run_order_index, so the scan sees every consumed index.
    Uniqueness is validated on resume: a duplicate index in the archived runs
    is a corrupt ledger and MUST abort rather than silently continue."""

    def __init__(self, ledger_path: Path) -> None:
        seen: dict[int, str] = {}
        for row in load_ledger(ledger_path):
            if row.get("kind") != "run":
                continue
            idx = row.get("run_order_index")
            if not isinstance(idx, int):
                # A pre-fix run row carried no index (or an interrupted rep
                # never wrote one). We cannot prove global uniqueness against a
                # missing index; refuse to resume rather than risk a collision.
                key = row.get("run_key") or "<unknown>"
                raise Stage2Error(
                    f"cannot resume: archived run {key!r} has no run_order_index — "
                    "the total order is unrecoverable (re-run under the fixed runner)"
                )
            prior = seen.get(idx)
            if prior is not None:
                raise Stage2Error(
                    f"cannot resume: run_order_index {idx} is claimed by both "
                    f"{prior!r} and {row.get('run_key')!r} — the ledger's total order "
                    "is corrupt"
                )
            seen[idx] = str(row.get("run_key") or "<unknown>")
        # Continue past the highest index any archived RUN consumed.
        self._next = (max(seen) + 1) if seen else 0
        # Keep the claimed set so freshly-taken indexes are also validated
        # unique against the archived runs (a total order has no repeats).
        self._claimed = set(seen)

    def take(self) -> int:
        value = self._next
        if value in self._claimed:  # defensive: the scan already guarantees this
            raise Stage2Error(f"run_order_index {value} would collide with an archived run")
        self._claimed.add(value)
        self._next += 1
        return value


def run_candidate(
    *,
    parent_mask: str,
    tested_dim: str,
    candidate_mask: str,
    worktree: Path,
    campaign: Path,
    seed: int,
    dependency_cache: str,
    env_file: str | None,
    ledger_path: Path,
    order: RunOrderCounter,
) -> DecisionRow:
    """Run candidate_mask ×3 reps at stage S2-<mask>; DELETE iff 3/3 pass ALL
    http anchors. Reuses execute_run (archival + checksums identical to the
    panel runner). Idempotent: a rep already in the ledger is re-evaluated from
    its archived artifacts, never re-run."""
    stage = stage_name(candidate_mask)
    reps: list[RepResult] = []
    for repeat in REPEATS:
        key = run_key(PROBE, stage, repeat)
        if ledger_has(load_ledger(ledger_path), key):
            verdict, executed, passed, failing = _load_and_evaluate(campaign, key)
            # A re-read rep keeps the index its archived RUN row recorded; it
            # never takes a fresh one (that is exactly the collision the resume
            # fix closes).
            run_order_index = recover_run_order_index(load_ledger(ledger_path), key)
            print(f"resume {key}: verdict={verdict} exec={executed} "
                  f"{'ALL-PASS' if passed else 'FAIL(' + ','.join(failing) + ')'}",
                  file=sys.stderr)
        else:
            run_order_index = order.take()
            outcome = execute_run(
                worktree=worktree,
                campaign=campaign,
                probe=PROBE,
                stage=stage,
                repeat=repeat,
                # Pass the MATCHING mask bits explicitly so the collector's
                # bit-by-bit agreement check against the S2-<bits> derivation
                # fires (a disagreement raises CollectionError before archiving).
                prescriptions=candidate_mask,
                seed=seed,
                dependency_cache=dependency_cache,
                env_file=env_file,
                ledger_path=ledger_path,
                run_order_index=run_order_index,
                ledger_kind="run",
            )
            passed, failing = _evaluate_artifacts(outcome["artifacts"])
            verdict = outcome["verdict"]
            executed = outcome["unique_executed"]
            print(f"done {key}: verdict={verdict} exec={executed} "
                  f"{'ALL-PASS' if passed else 'FAIL(' + ','.join(failing) + ')'}",
                  file=sys.stderr)
        reps.append(
            RepResult(
                repeat=repeat,
                run_key=key,
                verdict=verdict,
                unique_executed=executed,
                all_anchors_pass=passed,
                failing_anchors=failing,
                run_order_index=run_order_index,
            )
        )

    delete = all(r.all_anchors_pass for r in reps)
    row = DecisionRow(
        kind="s2-decision",
        parent_mask=parent_mask,
        tested_dim=tested_dim,
        candidate_mask=candidate_mask,
        reps=[asdict(r) for r in reps],
        decision="delete" if delete else "keep",
    )
    return row


# --------------------------------------------------------------------------
# greedy backward elimination to a fixed point
# --------------------------------------------------------------------------
def eliminate(
    *,
    worktree: Path,
    campaign: Path,
    seed: int,
    dependency_cache: str,
    env_file: str | None,
    ledger_path: Path,
    start_mask: str = FULL_KEEP_MASK,
) -> str:
    """Fixed-point greedy backward elimination. Returns the final keep-set mask.

    Rescans the retained dims from a after every successful deletion; stops when
    a full pass deletes nothing."""
    order = RunOrderCounter(ledger_path)
    mask = start_mask
    while True:
        deleted_this_pass = False
        for dim in retained_dims(mask):
            candidate = remove_dim(mask, dim)
            recorded = find_decision(load_ledger(ledger_path), candidate)
            if recorded is not None:
                decision = recorded["decision"]
                print(f"resume decision {candidate} (remove {dim} from {mask}): {decision}",
                      file=sys.stderr)
            else:
                # run_candidate stamps each rep's run_order_index directly (a
                # fresh rep from order.take(), a resumed rep recovered from its
                # archived RUN row), so the decision row inherits the total
                # order with no post-hoc bookkeeping.
                row = run_candidate(
                    parent_mask=mask,
                    tested_dim=dim,
                    candidate_mask=candidate,
                    worktree=worktree,
                    campaign=campaign,
                    seed=seed,
                    dependency_cache=dependency_cache,
                    env_file=env_file,
                    ledger_path=ledger_path,
                    order=order,
                )
                _append_decision(ledger_path, row)
                decision = row.decision
                print(f"decision {candidate} (remove {dim} from {mask}): {decision}",
                      file=sys.stderr)
            if decision == "delete":
                mask = candidate
                deleted_this_pass = True
                break  # RESCAN the remaining retained dims from a
        if not deleted_this_pass:
            break
    return mask


# --------------------------------------------------------------------------
# suite baseline precondition (6 accepted reds; block on any NEW failure)
# --------------------------------------------------------------------------
def register_stage2_baseline(*, worktree: Path, ledger_path: Path) -> None:
    """Full-suite gate: the six accepted baseline reds only; any NEW suite
    failure BLOCKS the launch (analyzer-diet.md:536-555). Idempotent."""
    rows = load_ledger(ledger_path)
    if any(row.get("kind") == "baseline-red" for row in rows):
        print("suite baseline already registered", file=sys.stderr)
        return
    failed = run_suite_and_collect_failures(worktree)
    registered, new = classify_suite_failures(failed)
    if new:
        raise Stage2Error(
            "Stage-2 BLOCKED: NEW suite failure(s) beyond the six accepted baseline reds: "
            + ", ".join(sorted(new))
        )
    append_ledger(
        ledger_path,
        LedgerEntry(
            kind="baseline-red",
            note=json.dumps(
                {"observed_reds": sorted(registered), "total": len(failed)},
                sort_keys=True,
            ),
        ),
    )
    print(f"suite baseline registered ({len(failed)} pre-existing reds)", file=sys.stderr)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="the committed SAG repo (worktree source)")
    parser.add_argument(
        "--worktree",
        required=True,
        help="Stage-2 execution worktree (created at the pinned SHA)",
    )
    parser.add_argument(
        "--pinned-sha",
        default="428fcb1",
        help="EXECUTION worktree SHA — pinned at 428fcb1 (confirm2 code-under-test) "
        "EXPLICITLY; NOT repo HEAD, NOT any campaign pin. Runner commits do not move it.",
    )
    parser.add_argument(
        "--campaign",
        default="logs/panel-stage2-http",
        help="Stage-2 campaign dir (must already contain panel-lock.json)",
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--dependency-cache", choices=("cold", "warm"), required=True)
    parser.add_argument(
        "--env-file",
        help="dotenv passed to each probe (OPENAI_API_KEY etc.); a fresh worktree "
        "carries no gitignored .env, so pass the main worktree's absolute .env path",
    )
    parser.add_argument(
        "--skip-suite-baseline",
        action="store_true",
        help="skip the suite baseline-reds precondition (already registered)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    worktree = Path(args.worktree).resolve()
    campaign = Path(args.campaign).resolve()
    ledger_path = campaign / "campaign-ledger.jsonl"

    lock = campaign / "panel-lock.json"
    if not lock.is_file():
        raise Stage2Error(
            f"Stage-2 panel lock is missing: {lock} "
            "(pre-register + commit the lock BEFORE launch)"
        )

    # Resolve the pinned SHA to a full commit id and materialize the EXECUTION
    # worktree at it EXPLICITLY (not HEAD, not a campaign pin).
    from scripts.run_category3_panel import _run as _panel_run

    resolved = _panel_run(["git", "-C", str(repo), "rev-parse", args.pinned_sha])
    if resolved.returncode != 0:
        raise Stage2Error(f"cannot resolve pinned SHA {args.pinned_sha!r}: {resolved.stderr.strip()}")
    pinned_full = resolved.stdout.strip()
    create_clean_worktree(repo, worktree, pinned_full)
    print(f"execution worktree at {pinned_full[:12]} ({args.pinned_sha})", file=sys.stderr)

    if not args.skip_suite_baseline:
        register_stage2_baseline(worktree=worktree, ledger_path=ledger_path)

    keep_set = eliminate(
        worktree=worktree,
        campaign=campaign,
        seed=args.seed,
        dependency_cache=args.dependency_cache,
        env_file=args.env_file,
        ledger_path=ledger_path,
    )
    kept = retained_dims(keep_set)
    print(
        f"Stage-2 keep-set (locally minimal): mask={keep_set} dims={kept}",
        file=sys.stderr,
    )
    append_ledger(
        ledger_path,
        LedgerEntry(
            kind="note",
            note=json.dumps(
                {"stage2_keep_set_mask": keep_set, "retained_dims": kept},
                sort_keys=True,
            ),
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
