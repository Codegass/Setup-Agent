#!/usr/bin/env python3
"""Category-3 A/B panel runner (analyzer diet).

Sequence (the pinned protocol):

  a. Create a CLEAN worktree from the committed HEAD (never the dirty tree) and
     run everything from it.
  a2. PANEL PRECONDITION (analyzer-diet.md:536-555): run the SAG suite, register
     the six accepted baseline reds in the campaign ledger, and BLOCK on any
     NEW suite failure — the panel never launches over an unnoticed regression.
  b. pyyaml CALIBRATION: three arm-P runs EXCLUDED from the panel; each valid
     only if every non-count anchor passes AND unique.executed>0; abort on any
     invalid run (the gate is re-applied on resume, never deferred by the
     ledger); floor = max(1, floor(0.8 * min(executed))). Append the floor to
     the campaign ledger BEFORE any panel run.
  c. The 24-run panel: probes {bigtop,tvm,pyyaml,httpcomponents-client} x
     stages {P,F} x repeats 1..3, INTERLEAVED P,F,P,F,P,F per probe, via the
     collector's run-probe subcommand with --stage P|F (canonical mask binding)
     and the pinned seed / cache mode.
  d. After EVERY run: archive the FULL --record artifact set + probe logs into
     logs/panel-category3/<probe>-<stage>-r<rep>/ with sha256 checksums, append
     a ledger row, THEN clean the container.

Idempotent / resumable: any run already recorded in the ledger is skipped.

Only PURE decision logic is unit-tested here; the live side effects (worktree,
docker, sag runs) are driven by main() and guarded by the ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# Direct-execution bootstrap: the in-file 'from scripts.*' imports need the
# REPO ROOT on sys.path (pytest adds it; 'python scripts/run_...' does not).
import sys as _sys
from pathlib import Path as _Path
_REPO_ROOT = str(_Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

PANEL_PROBES = ("bigtop", "tvm", "pyyaml", "httpcomponents-client")
REPEATS = (1, 2, 3)
CALIBRATION_RUNS = 3

# The FULL --record artifact set archived per run (the raw evidence behind
# every anchor), plus the probe logs (round-review P1-4: the evaluator anchors
# read verdict.json + control_events + the stamped manifest, but the RAW JUnit
# reports, pytest collection, contexts and project artifacts are the evidence
# Chenhao hand-verifies — they must survive worktree removal, checksummed into
# repo logs/). Entries may be files OR directories; directories are archived
# recursively with a per-file checksum. Rendered summary markdown is excluded.
ARCHIVED_ARTIFACTS = (
    # sealed structured artifacts the anchors read
    ".setup_agent/verdict.json",
    ".setup_agent/control_events.jsonl",
    ".setup_agent/run-pin.json",
    ".setup_agent/build_requirements.json",
    ".setup_agent/report_metrics.json",
    # raw --record evidence behind the executed/failed/collected anchors
    ".setup_agent/pytest-reports",       # raw JUnit XML
    ".setup_agent/pytest_collected.json",
    ".setup_agent/project_brief.json",
    ".setup_agent/project_meta.json",
    ".setup_agent/contexts",             # trunk/branch contexts
    # probe logs (session root) — the honest execution trace
    "agent_execution.log",
    "token_usage.csv",
    "main.log",
    "errors.log",
    "run-pin.json",                      # host-side run-pin mirror
    "control_events.jsonl",              # session-root mirror (fallback)
)

# Session-root probe logs whose name is dynamic (one per probed project).
ARCHIVED_ARTIFACT_GLOBS = ("command_*.log",)

# The six ACCEPTED suite baseline reds (analyzer-diet.md:536-555). Any suite
# failure whose node id matches one of these keywords is pre-registered; any
# OTHER failure is a NEW regression that BLOCKS the panel. Keyword + max-count
# so an accepted red that spreads to new call sites is still caught.
BASELINE_REDS = (
    {"keyword": "test_evidence_ingestion", "max_count": 1},
    {"keyword": "test_stage1_review_fixes", "max_count": 2},
    {"keyword": "test_lineage_idempotence_followup", "max_count": 2},
    {"keyword": "test_packaging_smoke", "max_count": 1},
)


class RunnerError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# run plan
# --------------------------------------------------------------------------
def run_key(probe: str, stage: str, repeat: int) -> str:
    return f"{probe}-{stage}-r{repeat}"


def panel_run_plan() -> list[tuple[str, str, int]]:
    """The 24-run panel, grouped by probe and INTERLEAVED P,F per repeat."""
    plan: list[tuple[str, str, int]] = []
    for probe in PANEL_PROBES:
        for repeat in REPEATS:
            plan.append((probe, "P", repeat))
            plan.append((probe, "F", repeat))
    return plan


def calibration_run_plan() -> list[tuple[str, str, int]]:
    """Three arm-P pyyaml runs, EXCLUDED from the panel (own stage name)."""
    return [("pyyaml", "cal", index) for index in range(1, CALIBRATION_RUNS + 1)]


def filtered_run_order(plan: Sequence[tuple[str, str, int]]) -> dict[str, int]:
    """Assign the spec-required run-order index 0..N-1 over the EXACT plan being
    executed (reviewer P2). Under `--only-probes` the effective plan is a subset
    of the canonical 27-slot campaign, so the indices must run 0..N-1 over that
    subset — not carry the sparse full-plan slot numbers, which would make the
    stamped index unattributable to the actual total order.

    The index is assigned by run_key so it is STABLE and resumable: a resumed
    campaign re-derives the same index for each run regardless of which runs the
    ledger already recorded. The agent stamps it into the run pin
    (SAG_RUN_ORDER_INDEX) so the P/F interleave and any drift are attributable to
    a total order (analyzer-diet.md:453-455)."""
    order: dict[str, int] = {}
    for index, (probe, stage, repeat) in enumerate(plan):
        order[run_key(probe, stage, repeat)] = index
    return order


def campaign_run_order() -> dict[str, int]:
    """The canonical full-campaign run order, sequential 0..N-1 over the WHOLE
    plan (calibration first, then the interleaved panel). This is the reference
    total order for a full, unfiltered campaign; a `--only-probes` run instead
    numbers 0..N-1 over its FILTERED plan via `filtered_run_order` (reviewer
    P2)."""
    return filtered_run_order([*calibration_run_plan(), *panel_run_plan()])


# --------------------------------------------------------------------------
# calibration floor
# --------------------------------------------------------------------------
def calibration_floor(executed_values: Sequence[int]) -> int:
    """floor = max(1, floor(0.8 * min(executed))) over three VALID runs."""
    values = list(executed_values)
    if len(values) != CALIBRATION_RUNS:
        raise ValueError(
            f"calibration floor needs exactly {CALIBRATION_RUNS} valid runs, got {len(values)}"
        )
    if any(v <= 0 for v in values):
        raise ValueError("calibration executed counts must all be > 0 (a starved run is invalid)")
    return max(1, math.floor(0.8 * min(values)))


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------
@dataclass
class LedgerEntry:
    kind: str  # "run" | "floor" | "baseline-red" | "note"
    run_key: str | None = None
    probe: str | None = None
    stage: str | None = None
    repeat: int | None = None
    run_id: str | None = None
    artifact_dir: str | None = None
    checksums: dict[str, str] = field(default_factory=dict)
    # The run's 0..N-1 position in its plan's total order. Persisted on EVERY
    # run row so a resumed campaign recovers the next free index from the runs
    # themselves — not only from decision rows, which an interrupted candidate
    # (archived reps, no decision yet) never wrote. None on non-run rows.
    run_order_index: int | None = None
    floor: int | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != {}}


def append_ledger(ledger_path: Path, entry: LedgerEntry) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.to_json(), sort_keys=True) + "\n")


def load_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def ledger_has(rows: Iterable[dict[str, Any]], key: str) -> bool:
    """True iff a COMPLETED run with this key is recorded (floor/note rows never
    mark a run done)."""
    return any(row.get("kind") == "run" and row.get("run_key") == key for row in rows)


# --------------------------------------------------------------------------
# suite baseline reds (panel precondition, analyzer-diet.md:536-555)
# --------------------------------------------------------------------------
def classify_suite_failures(
    failed_node_ids: Sequence[str],
    accepted: Sequence[dict[str, Any]] = BASELINE_REDS,
) -> tuple[list[str], list[str]]:
    """Split the suite's failing node ids into (registered_reds, new_regressions).

    A failure is a REGISTERED red iff its node id contains an accepted keyword
    AND that keyword's failures do not exceed its pre-registered max_count. Any
    failure matching no keyword — or overflowing an accepted keyword's count —
    is a NEW regression that BLOCKS the panel (round-review P1-3)."""
    registered: list[str] = []
    new: list[str] = []
    seen: dict[str, int] = {}
    by_keyword = {a["keyword"]: a for a in accepted}
    for node_id in failed_node_ids:
        match = next((kw for kw in by_keyword if kw in node_id), None)
        if match is None:
            new.append(node_id)
            continue
        seen[match] = seen.get(match, 0) + 1
        if seen[match] <= by_keyword[match]["max_count"]:
            registered.append(node_id)
        else:
            new.append(node_id)  # an accepted red that spread to new call sites
    return registered, new


def register_suite_baseline(
    *,
    worktree: Path,
    ledger_path: Path,
    failed_node_ids: Sequence[str],
) -> None:
    """Record the six accepted reds in the campaign ledger at panel start and
    BLOCK on any new suite failure (analyzer-diet.md:554-555). Idempotent: a
    prior baseline row short-circuits."""
    rows = load_ledger(ledger_path)
    if any(row.get("kind") == "baseline-red" for row in rows):
        return
    registered, new = classify_suite_failures(failed_node_ids)
    if new:
        raise RunnerError(
            "panel BLOCKED: NEW suite failure(s) beyond the registered baseline reds: "
            + ", ".join(sorted(new))
        )
    append_ledger(
        ledger_path,
        LedgerEntry(
            kind="baseline-red",
            note=json.dumps(
                {
                    "accepted": [a["keyword"] for a in BASELINE_REDS],
                    "observed_reds": sorted(registered),
                },
                sort_keys=True,
            ),
        ),
    )


def run_suite_and_collect_failures(worktree: Path) -> list[str]:
    """Run the SAG test suite from the clean worktree, returning failing node
    ids (pytest's ``FAILED path::test`` lines). The suite gate is a precondition,
    not a probe, so it runs on the host worktree (no container)."""
    result = _run(
        [
            "uv",
            "--directory",
            str(worktree),
            "run",
            "python",
            "-m",
            "pytest",
            "-q",
            "--no-header",
        ]
    )
    failed: list[str] = []
    for line in (result.stdout + result.stderr).splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            node = stripped.split(None, 1)[1].split(" ", 1)[0]
            failed.append(node)
    return failed


# --------------------------------------------------------------------------
# checksums / archival
# --------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_one(source: Path, dest_root: Path, rel_key: str, checksums: dict[str, str]) -> None:
    """Copy a file OR a directory (recursively) under dest_root, checksumming
    every archived file. Directory members are keyed by their relative path so
    the ledger records a checksum for each (round-review P1-4)."""
    if source.is_file():
        target = dest_root / rel_key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        checksums[rel_key] = sha256_file(target)
    elif source.is_dir():
        for child in sorted(source.rglob("*")):
            if child.is_file():
                member_key = f"{rel_key}/{child.relative_to(source).as_posix()}"
                _archive_one(child, dest_root, member_key, checksums)


def archive_session(session: Path, destination: Path) -> dict[str, str]:
    """Archive the FULL --record artifact set + probe logs into destination,
    returning a per-file sha256 map. Files and directories both handled; the
    directory tree is preserved under destination so the raw JUnit reports and
    contexts survive worktree removal (round-review P1-4)."""
    destination.mkdir(parents=True, exist_ok=True)
    checksums: dict[str, str] = {}
    for relative in ARCHIVED_ARTIFACTS:
        source = session / relative
        if not source.exists():
            continue
        # Preserve the .setup_agent/ vs session-root layout in the archive.
        _archive_one(source, destination, relative, checksums)
    for pattern in ARCHIVED_ARTIFACT_GLOBS:
        for source in sorted(session.glob(pattern)):
            if source.is_file():
                _archive_one(source, destination, source.name, checksums)
    if not checksums:
        raise RunnerError(f"no sealed artifacts found to archive under {session}")
    return checksums


# --------------------------------------------------------------------------
# live side effects (worktree, collector, docker) — driven by main()
# --------------------------------------------------------------------------
def _run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=False, capture_output=True, text=True, **kwargs)


def committed_head_sha(repo: Path) -> str:
    result = _run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    if result.returncode != 0:
        raise RunnerError(f"cannot resolve committed HEAD: {result.stderr.strip()}")
    return result.stdout.strip()


def _campaign_pinned_sha(campaign: Path) -> str | None:
    """The campaign's authoritative SAG SHA: the sag_git_sha every archived
    run pin carries. A started campaign resumes at ITS pin — never at repo
    HEAD, which legitimately moves when harness-only fixes are committed
    mid-campaign (pin discipline: the system under test is what was pinned,
    not whatever the branch grew since)."""
    import json as _json

    shas = set()
    for pin_path in sorted(campaign.glob("*/run-pin.json")):
        try:
            shas.add(_json.loads(pin_path.read_text())["sag_git_sha"])
        except Exception:
            continue
    if not shas:
        return None
    if len(shas) > 1:
        raise RunnerError(f"campaign archives carry mixed sag_git_sha pins: {sorted(shas)}")
    return next(iter(shas))


def create_clean_worktree(repo: Path, worktree: Path, sha: str) -> None:
    """Materialize a CLEAN worktree at the committed SHA (never the dirty tree)."""
    if worktree.exists():
        existing = _run(["git", "-C", str(worktree), "rev-parse", "HEAD"])
        if existing.returncode == 0 and existing.stdout.strip().startswith(sha[:12]):
            return  # resumable: the worktree already sits at the pinned SHA
        raise RunnerError(
            f"worktree {worktree} exists at a different SHA; refusing to reuse "
            f"(worktree={existing.stdout.strip()[:12]!r} rc={existing.returncode} "
            f"expected={sha[:12]!r})"
        )
    result = _run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), sha])
    if result.returncode != 0:
        raise RunnerError(f"git worktree add failed: {result.stderr.strip()}")


def _find_created_session(worktree: Path, before: set[Path]) -> Path:
    after = {p.resolve() for p in (worktree / "logs").glob("session_*") if p.is_dir()}
    created = sorted(after - before)
    if len(created) != 1:
        raise RunnerError(f"expected exactly one new session dir, found {len(created)}")
    return created[0]


def clean_container(run_name: str) -> None:
    container = f"sag-{run_name}"
    _run(["docker", "rm", "-f", container])


def _collector_run_probe(
    *,
    worktree: Path,
    campaign: Path,
    probe: str,
    stage: str,
    repeat: int,
    seed: int,
    dependency_cache: str,
    env_file: str | None,
    prescriptions: str,
    run_order_index: int,
) -> subprocess.CompletedProcess:
    cmd = [
        "uv",
        "--directory",
        str(worktree),
        "run",
        "python",
        "-m",
        "scripts.collect_control_layer_ab",
        "run-probe",
        "--campaign",
        str(campaign),
        "--probe",
        probe,
        "--stage",
        stage,
        "--repeat",
        str(repeat),
        "--sag-root",
        str(worktree),
        "--seed",
        str(seed),
        "--dependency-cache",
        dependency_cache,
        "--prescriptions",
        prescriptions,
        "--run-order-index",
        str(run_order_index),
    ]
    if env_file:
        cmd += ["--env-file", env_file]
    return _run(cmd)


def execute_run(
    *,
    worktree: Path,
    campaign: Path,
    probe: str,
    stage: str,
    repeat: int,
    prescriptions: str,
    seed: int,
    dependency_cache: str,
    env_file: str | None,
    ledger_path: Path,
    run_order_index: int,
    ledger_kind: str = "run",
) -> dict[str, Any]:
    """Run one probe, archive its sealed artifacts, append the ledger, clean the
    container. Returns the distilled RunArtifacts-as-dict for callers that need
    the anchor evidence (calibration).

    `run_order_index` is the run's 0..N-1 position in the EXACT plan being
    executed (the caller derives it via filtered_run_order so `--only-probes`
    runs are numbered over their filtered plan, not the sparse full-plan slots —
    reviewer P2). The collector injects it as SAG_RUN_ORDER_INDEX so the agent
    stamps it into the run pin (analyzer-diet.md:453-455)."""
    from scripts.panel_category3_evaluator import load_run_artifacts

    key = run_key(probe, stage, repeat)
    name = f"ab-{probe}-{stage}-r{repeat}"
    logs = worktree / "logs"
    logs.mkdir(exist_ok=True)
    before = {p.resolve() for p in logs.glob("session_*") if p.is_dir()}

    result = _collector_run_probe(
        worktree=worktree,
        campaign=campaign,
        probe=probe,
        stage=stage,
        repeat=repeat,
        seed=seed,
        dependency_cache=dependency_cache,
        env_file=env_file,
        prescriptions=prescriptions,
        run_order_index=run_order_index,
    )
    (campaign / f"{name}-runner.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RunnerError(f"run-probe {key} failed (rc={result.returncode}); see {name}-runner.log")

    session = _find_created_session(worktree, before)
    artifact_dir = campaign / key
    checksums = archive_session(session, artifact_dir)
    # The runner/cli logs written to the campaign dir also carry evidence weight;
    # fold them into the archive with checksums so no probe log is unhashed
    # (round-review P1-4).
    runner_log = campaign / f"{name}-runner.log"
    if runner_log.is_file():
        target = artifact_dir / runner_log.name
        shutil.copy2(runner_log, target)
        checksums[runner_log.name] = sha256_file(target)
    cli_log = campaign / f"{name}-cli.log"
    if cli_log.is_file():
        target = artifact_dir / cli_log.name
        shutil.copy2(cli_log, target)
        checksums[cli_log.name] = sha256_file(target)
    artifacts = load_run_artifacts(session)
    run_id = _read_run_id(session)

    append_ledger(
        ledger_path,
        LedgerEntry(
            kind=ledger_kind,
            run_key=key,
            probe=probe,
            stage=stage,
            repeat=repeat,
            run_id=run_id,
            artifact_dir=key,
            checksums=checksums,
            run_order_index=run_order_index,
        ),
    )
    clean_container(name)
    return {
        "verdict": artifacts.verdict,
        "unique_executed": artifacts.unique_executed,
        "artifacts": artifacts,
    }


def _read_run_id(session: Path) -> str | None:
    for candidate in (
        session / ".setup_agent" / "verdict.json",
        session / "verdict.json",
    ):
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            run_id = data.get("run_id")
            if run_id:
                return str(run_id)
    return session.name


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------
def run_calibration(
    *,
    worktree: Path,
    campaign: Path,
    seed: int,
    dependency_cache: str,
    env_file: str | None,
    ledger_path: Path,
    run_order: dict[str, int],
) -> int:
    """Three arm-P pyyaml runs (excluded from the panel). Abort on any invalid
    run; register the floor to the ledger before returning.

    `run_order` maps run_key -> 0..N-1 index over the effective plan (reviewer
    P2); calibration keys occupy its first slots."""
    from scripts.panel_category3_evaluator import evaluate_pyyaml

    rows = load_ledger(ledger_path)
    # Resume: if the floor is already recorded, reuse it.
    for row in rows:
        if row.get("kind") == "floor" and row.get("probe") == "pyyaml":
            return int(row["floor"])

    executed_values: list[int] = []
    for probe, stage, repeat in calibration_run_plan():
        key = run_key(probe, stage, repeat)
        if ledger_has(load_ledger(ledger_path), key):
            # Already ran. Resumability must NOT defer the gate: re-load the FULL
            # archived artifacts and re-evaluate the SAME validity check (every
            # non-count anchor + executed>0). A ledgered-but-invalid run must
            # still abort — never register a floor from it (round-review P1-2).
            artifacts = _load_archived_artifacts(campaign / key)
            _assert_calibration_valid(key, artifacts)
            executed_values.append(artifacts.unique_executed)
            continue
        outcome = execute_run(
            worktree=worktree,
            campaign=campaign,
            probe=probe,
            stage=stage,
            repeat=repeat,
            prescriptions="on",
            seed=seed,
            dependency_cache=dependency_cache,
            env_file=env_file,
            ledger_path=ledger_path,
            run_order_index=run_order[key],
            ledger_kind="run",
        )
        artifacts = outcome["artifacts"]
        _assert_calibration_valid(key, artifacts)
        executed_values.append(artifacts.unique_executed)

    floor = calibration_floor(executed_values)
    append_ledger(
        ledger_path,
        LedgerEntry(
            kind="floor",
            run_key=None,
            probe="pyyaml",
            floor=floor,
            note=f"executed_floor=max(1, floor(0.8*min({executed_values})))={floor}",
        ),
    )
    return floor


def _load_archived_artifacts(artifact_dir: Path):
    """Re-distill RunArtifacts from an archived run. The archive preserves the
    .setup_agent/ layout (round-review P1-4), so the evaluator's loader reads it
    exactly as it reads a live session."""
    from scripts.panel_category3_evaluator import EvaluationError, load_run_artifacts

    try:
        return load_run_artifacts(artifact_dir)
    except EvaluationError as exc:
        raise RunnerError(f"cannot re-evaluate archived run {artifact_dir}: {exc}") from exc


def _assert_calibration_valid(key: str, artifacts) -> None:
    """The calibration validity gate (analyzer-diet.md:432-438), applied
    identically to a fresh run and to a ledgered-but-not-yet-gated resume: VALID
    only if every NON-COUNT anchor passes AND unique.executed>0; otherwise the
    run is invalid and calibration ABORTS (fix the probe, restart) — no floor is
    ever registered from an invalid run (round-review P1-2)."""
    from scripts.panel_category3_evaluator import evaluate_pyyaml

    # Sentinel floor of 1 so the count anchor never vetoes calibration itself.
    results = evaluate_pyyaml(artifacts, executed_floor=1)
    non_count = [r for r in results if r.name != "unique_executed_floor"]
    if not all(r.passed for r in non_count):
        failing = ", ".join(f"{r.name}: {r.reason}" for r in non_count if not r.passed)
        raise RunnerError(f"pyyaml calibration {key} INVALID (fix the probe, restart): {failing}")
    if artifacts.unique_executed <= 0:
        raise RunnerError(f"pyyaml calibration {key} INVALID: unique.executed == 0")


# --------------------------------------------------------------------------
# panel + baseline
# --------------------------------------------------------------------------
def run_sequence(
    plan: Sequence[tuple[str, str, int]],
    *,
    worktree: Path,
    campaign: Path,
    seed: int,
    dependency_cache: str,
    env_file: str | None,
    ledger_path: Path,
    run_order: dict[str, int],
) -> None:
    """Execute the panel `plan`. `run_order` maps run_key -> 0..N-1 index over
    the effective plan (reviewer P2), so `--only-probes` runs are numbered over
    their filtered plan rather than the sparse full-plan slots."""
    for probe, stage, repeat in plan:
        key = run_key(probe, stage, repeat)
        if ledger_has(load_ledger(ledger_path), key):
            print(f"skip {key} (already in ledger)", file=sys.stderr)
            continue
        prescriptions = "on" if stage == "P" else "off"
        execute_run(
            worktree=worktree,
            campaign=campaign,
            probe=probe,
            stage=stage,
            repeat=repeat,
            prescriptions=prescriptions,
            seed=seed,
            dependency_cache=dependency_cache,
            env_file=env_file,
            ledger_path=ledger_path,
            run_order_index=run_order[key],
        )
        print(f"done {key}", file=sys.stderr)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="the committed SAG repo (worktree source)")
    parser.add_argument("--worktree", required=True, help="clean worktree path to create/run from")
    parser.add_argument(
        "--campaign",
        default="logs/panel-category3",
        help="campaign dir (must already contain panel-lock.json)",
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--dependency-cache", choices=("cold", "warm"), required=True)
    parser.add_argument("--env-file")
    parser.add_argument(
        "--stop-after",
        choices=("calibration", "panel"),
        default="panel",
        help="stop the sequence early (for staged runs)",
    )
    parser.add_argument(
        "--skip-suite-baseline",
        action="store_true",
        help="skip the suite baseline-reds precondition (already registered this campaign)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.add_argument(
        "--only-probes",
        default=None,
        help="comma list restricting panel probes; pyyaml calibration is "
        "skipped when pyyaml is excluded (rerun campaigns)",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    worktree = Path(args.worktree).resolve()
    campaign = Path(args.campaign).resolve()
    ledger_path = campaign / "campaign-ledger.jsonl"

    lock = campaign / "panel-lock.json"
    if not lock.is_file():
        raise RunnerError(f"panel lock is missing: {lock} (run pin-panel / commit the lock first)")

    sha = _campaign_pinned_sha(campaign) or committed_head_sha(repo)
    create_clean_worktree(repo, worktree, sha)

    # (a) PANEL PRECONDITION: register the six accepted suite baseline reds and
    # BLOCK on any new suite failure (analyzer-diet.md:536-555). Runs before any
    # probe so the panel never launches over an unnoticed regression.
    if not args.skip_suite_baseline:
        failed = run_suite_and_collect_failures(worktree)
        register_suite_baseline(
            worktree=worktree, ledger_path=ledger_path, failed_node_ids=failed
        )
        print(f"suite baseline registered ({len(failed)} pre-existing reds)", file=sys.stderr)

    # Effective plan for THIS invocation: pyyaml calibration (only when pyyaml is
    # in scope) followed by the filtered panel plan. The run-order index is
    # assigned 0..N-1 over exactly this plan (reviewer P2), so `--only-probes`
    # runs never carry the sparse full-plan slot numbers. Computed once and
    # threaded to both calibration and the panel so the numbering is a single
    # total order.
    only = {x.strip() for x in (args.only_probes or "").split(",") if x.strip()}
    run_calibration_needed = not (only and "pyyaml" not in only)
    panel_plan = panel_run_plan()
    if only:
        panel_plan = [item for item in panel_plan if item[0] in only]
    effective_plan = [
        *(calibration_run_plan() if run_calibration_needed else []),
        *panel_plan,
    ]
    run_order = filtered_run_order(effective_plan)

    # (b) calibration BEFORE any panel run
    if not run_calibration_needed:
        floor = None  # calibration is pyyaml-specific; excluded from this rerun
    else:
        floor = run_calibration(
            worktree=worktree,
            campaign=campaign,
            seed=args.seed,
            dependency_cache=args.dependency_cache,
            env_file=args.env_file,
            ledger_path=ledger_path,
            run_order=run_order,
        )
        print(f"pyyaml calibrated executed_floor={floor}", file=sys.stderr)
    if args.stop_after == "calibration":
        return 0

    # (c) the 24-run panel
    run_sequence(
        panel_plan,
        worktree=worktree,
        campaign=campaign,
        seed=args.seed,
        dependency_cache=args.dependency_cache,
        env_file=args.env_file,
        ledger_path=ledger_path,
        run_order=run_order,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
