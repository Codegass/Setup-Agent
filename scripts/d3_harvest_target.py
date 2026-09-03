#!/usr/bin/env python3
"""Assemble one CI run's evidence into a :mod:`sag.metrics` target record.

Two modes, one assembly.  ``--from-dir`` reads a snapshot directory that is
already on disk -- JUnit XML pool zips, the run's job list, the commit's
statuses, and any workflow config harvested beside them -- and is the whole of
the logic worth testing.  ``--repo/--sha`` is a thin courier in front of it: it
pulls those same files through ``gh api`` into an output directory and then
runs the identical offline assembly, so the network fetches evidence and never
judges it.

What a cell is here:

* **Grade A, one per JUnit pool.**  Every XML in the pool zip is parsed in
  sorted-name order and retry-deconvolved as one document sequence, so a test
  that failed and passed on retry counts once, as flaky, and not as red.  The
  cell is named for the pool artifact; when that name carries a toolchain the
  record layer cannot read (``junit-xml-17-noflaky-nonew``), the harvester's
  reading of it is appended in a form ``match_cell`` can see, and the record's
  notes disclose that the reading is the harvester's.
* **Grade B, one per uncovered check.**  A check that no pool measures is worth
  its conclusion and nothing more.  A check a pool already measures gets no
  second cell *unless it concluded failed*: a green conclusion is not extra
  evidence about work whose XML is already counted, but a red one is a defeater
  of those counts -- a suite that crashed after uploading partial green XML
  reports exactly that -- so it keeps its cell and the notes name the pool it
  contradicts.

Laundering is vetted over any workflow config in the snapshot.  A config that
swallows a build or test failure makes every conclusion in the run a statement
about the workflow file, so grade-B cells are marked ``laundered_conclusion``
and their build outcome is withheld as unknown -- grade-A cells keep their
counts, which ``continue-on-error`` cannot touch.

Identities are stored only when the record can hold them: kafka's main pool
carries 36,259 executions and identities past 1,100 characters, so that cell
reports counts and says so in the notes.  Executed, red and flaky identities are
each judged against those bounds on their own, and every set the record cannot
hold is counted in the cell and disclosed in the notes -- an unlistable flaky
test is still a flaky test the run had.

Examples::

    python scripts/d3_harvest_target.py --from-dir logs/ci-probe/kafka-run --jdk 17
    python scripts/d3_harvest_target.py --repo apache/kafka --sha 26b251a4 \
      --out-dir logs/d3-targets/kafka --jdk 17
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sag.metrics.ci_vetting import (  # noqa: E402
    LaunderingVet,
    extract_cell_jdk,
    extract_cell_os,
    match_cell,
    vet_workflow_config,
)
from sag.metrics.junit_deconvolution import (  # noqa: E402
    Deconvolved,
    TestEntry,
    deconvolve,
    parse_junit_entries,
)
from sag.metrics.target_record import (  # noqa: E402
    BuildOutcome,
    CellTarget,
    TargetRecord,
    target_record_sha256,
)

POOL_GLOB = "junit-xml-*.zip"
JOBS_FILE = "run-jobs.json"
STATUSES_FILE = "commit-statuses.json"
METADATA_FILE = "run-metadata.json"
ARTIFACTS_FILE = "run-artifacts.json"
WORKFLOWS_DIR = "workflows"
RECORD_FILE = "target_record.json"

# The record layer rejects an identity past 512 characters, and a pool the size
# of kafka's would bury the record under 36,259 of them.  Past either bound the
# cell reports counts and the record's notes say which bound it hit.
IDENTITY_CHARACTER_BOUND = 512
IDENTITY_COUNT_BOUND = 2_000

# A JDK major outside this range is not a toolchain, it is a shard index.
MIN_JDK_MAJOR = 6
MAX_JDK_MAJOR = 99

# Words that name the artifact kind rather than the cell, dropped before a pool
# name and a check name are compared.  "java" and "jdk" go too: the pool writes
# the major bare ("...-17-...") where the check spells it ("JUnit tests Java 17").
GENERIC_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "artifact",
        "artifacts",
        "build",
        "ci",
        "gradle",
        "java",
        "jdk",
        "junit",
        "maven",
        "mvn",
        "pool",
        "report",
        "reports",
        "result",
        "results",
        "suite",
        "suites",
        "surefire",
        "test",
        "tests",
        "xml",
    }
)

_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")

# A conclusion is a build claim only when it decided something.  Cancelled,
# skipped and neutral runs judged no code, so they leave the outcome unknown.
CHECK_BUILD_OUTCOME: dict[str, BuildOutcome] = {
    "success": "ok",
    "failure": "failed",
    "timed_out": "failed",
    "startup_failure": "failed",
    "action_required": "failed",
}

# The platform labels one cell may stand in for another under, once the
# toolchain already agrees.  This is the class ``match_cell`` itself admits
# from: linux is the platform SAG's container is, and a pool artifact name
# states no platform at all, so the two are one class; macOS and windows each
# stand only for themselves.
LINUX_COMPATIBLE_OS: frozenset[str] = frozenset({"linux", "unknown"})


class HarvestError(Exception):
    """A harvest that cannot produce a target record."""


class PoolReading(BaseModel):
    """One JUnit pool zip, parsed and deconvolved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_id: str
    artifact: str
    deconvolved: Deconvolved
    unreadable: tuple[str, ...] = ()


class HarvestedCell(BaseModel):
    """One assembled cell and whatever the record must disclose about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cell: CellTarget
    notes: tuple[str, ...] = ()


def canonical_json(payload: Any) -> str:
    """Render a payload the one way its digest is taken."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def name_signature(name: str) -> frozenset[str]:
    """Return a name's distinguishing tokens, artifact vocabulary removed."""

    tokens = (token for token in _TOKEN_SPLIT_RE.split((name or "").lower()) if token)
    return frozenset(token for token in tokens if token not in GENERIC_NAME_TOKENS)


def pool_covers_check(pool_id: str, check_name: str) -> bool:
    """Whether a pool already measures the work a check reports on.

    Containment, not equality: the pool ``junit-xml-17-flaky-nonew`` names the
    axes it was built from, and the check ``build / JUnit tests Java 17
    (flaky)`` names a subset of them.  A check with no distinguishing tokens is
    never treated as covered -- it would be covered by every pool.
    """

    check_tokens = name_signature(check_name)
    if not check_tokens:
        return False
    return check_tokens <= name_signature(pool_id)


def pool_jdk_annotation(pool_id: str) -> int | None:
    """Return the JDK major a pool name states but the record layer cannot read.

    ``None`` when the record layer already reads the name, and when the name
    carries no single plausible major -- an ambiguous name is left unannotated
    rather than guessed at.
    """

    if extract_cell_jdk(pool_id) is not None:
        return None
    majors = {
        int(token)
        for token in _TOKEN_SPLIT_RE.split((pool_id or "").lower())
        if token.isdigit() and len(token) <= 2 and MIN_JDK_MAJOR <= int(token) <= MAX_JDK_MAJOR
    }
    if len(majors) != 1:
        return None
    return majors.pop()


def pool_cell_id(pool_id: str) -> str:
    """Return the cell id for a pool: its artifact name, toolchain made legible."""

    major = pool_jdk_annotation(pool_id)
    return pool_id if major is None else f"{pool_id} (jdk {major})"


def read_pool(pool_path: Path) -> PoolReading:
    """Parse and deconvolve every JUnit XML in one pool zip.

    Sorted-name order is the document order deconvolution reads: a retry lives
    beside its first attempt in the same suite file, and sorting keeps every
    pool's assembly reproducible.
    """

    entries: list[TestEntry] = []
    unreadable: list[str] = []
    try:
        with zipfile.ZipFile(pool_path) as archive:
            names = sorted(name for name in archive.namelist() if name.endswith(".xml"))
            for name in names:
                try:
                    entries.extend(parse_junit_entries(archive.read(name)))
                except ValueError:
                    unreadable.append(name)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HarvestError(f"{pool_path.name} is not a readable pool archive: {exc}") from exc

    return PoolReading(
        pool_id=pool_path.stem,
        artifact=pool_path.name,
        deconvolved=deconvolve(tuple(entries)),
        unreadable=tuple(unreadable),
    )


def _storable(ids: tuple[str, ...]) -> bool:
    if not ids:
        return False
    if len(ids) > IDENTITY_COUNT_BOUND:
        return False
    return all(len(identity) <= IDENTITY_CHARACTER_BOUND for identity in ids)


def _storable_ids(
    ids: tuple[str, ...], *, cell_id: str, label: str
) -> tuple[tuple[str, ...], str | None]:
    """Return the identities the record can hold, and the note when it cannot.

    Every set the record refuses is still a set the run had, so the omission is
    disclosed rather than left to read as an empty set.
    """

    if not ids or _storable(ids):
        return ids, None
    reason = (
        f"the record holds at most {IDENTITY_COUNT_BOUND}"
        if len(ids) > IDENTITY_COUNT_BOUND
        else f"an identity exceeds the record's {IDENTITY_CHARACTER_BOUND}-character bound"
    )
    return (), (
        f"cell {cell_id}: {label} identities are counted, not listed "
        f"({len(ids)} of them) -- {reason}"
    )


def cell_from_pool(reading: PoolReading) -> HarvestedCell:
    """Build the grade-A cell one pool proves."""

    result = reading.deconvolved
    cell_id = pool_cell_id(reading.pool_id)
    executed_ids, executed_note = _storable_ids(
        result.executed_ids, cell_id=cell_id, label="executed"
    )
    red_ids, red_note = _storable_ids(result.final_red_ids, cell_id=cell_id, label="red")
    flaky_ids, flaky_note = _storable_ids(result.flaky_ids, cell_id=cell_id, label="flaky")
    notes: list[str] = [note for note in (executed_note, red_note, flaky_note) if note]
    if reading.unreadable:
        notes.append(
            f"cell {cell_id}: {len(reading.unreadable)} XML files in {reading.artifact} "
            "could not be parsed and are not counted"
        )
    if result.duplicate_name_ids:
        notes.append(
            f"cell {cell_id}: {len(result.duplicate_name_ids)} display names render more "
            "than once and are kept as separate executions"
        )

    cell = CellTarget(
        cell_id=cell_id,
        build="failed" if result.final_red_ids else "ok",
        executed_count=len(result.executed_ids),
        executed_ids=executed_ids,
        red_count=len(result.final_red_ids),
        red_ids=red_ids,
        flaky_count=len(result.flaky_ids),
        flaky_ids=flaky_ids,
        skipped=len(result.final_skipped_ids),
        grade="A",
        evidence_refs=(reading.artifact,),
    )
    return HarvestedCell(cell=cell, notes=tuple(notes))


def check_build_outcome(conclusion: str) -> BuildOutcome:
    """Return the build outcome one check conclusion decides, before laundering."""

    return CHECK_BUILD_OUTCOME.get((conclusion or "").strip().lower(), "unknown")


def cell_from_check(name: str, conclusion: str, *, laundered: bool) -> CellTarget:
    """Build the grade-B cell one check's conclusion proves."""

    outcome: BuildOutcome = check_build_outcome(conclusion)
    if laundered:
        # A laundered conclusion is a claim about the workflow file, so it may
        # not stand as this cell's build outcome.
        outcome = "unknown"
    return CellTarget(
        cell_id=name,
        build=outcome,
        executed_count=0,
        red_count=0,
        grade="B",
        laundered_conclusion=laundered,
        evidence_refs=(JOBS_FILE,),
    )


def read_jobs(snapshot_dir: Path) -> tuple[tuple[str, str], ...]:
    """Return ``(check name, conclusion)`` in the run's own job order."""

    path = snapshot_dir / JOBS_FILE
    if not path.exists():
        return ()
    body = _read_json(path)
    jobs = body.get("jobs") if isinstance(body, dict) else body
    if not isinstance(jobs, list):
        raise HarvestError(f"{JOBS_FILE} does not carry a job list")
    checks: list[tuple[str, str]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "").strip()
        if not name:
            continue
        checks.append((name, str(job.get("conclusion") or "").strip().lower()))
    return tuple(checks)


def vet_snapshot_workflows(snapshot_dir: Path) -> tuple[LaunderingVet, tuple[str, ...]]:
    """Vet every workflow config in the snapshot; report the files vetted."""

    workflows = sorted(
        path
        for path in (snapshot_dir / WORKFLOWS_DIR).glob("*")
        if path.suffix in {".yml", ".yaml"} and path.is_file()
    )
    locations: list[str] = []
    laundered = False
    for path in workflows:
        vet = vet_workflow_config(path.read_text(encoding="utf-8", errors="replace"))
        laundered = laundered or vet.laundered
        locations.extend(f"{path.name}:{location}" for location in vet.locations)
    return LaunderingVet(laundered=laundered, locations=tuple(locations)), tuple(
        path.name for path in workflows
    )


def interchangeable_os(cell_os: str) -> frozenset[str]:
    """Return the platform labels a cell on ``cell_os`` may be compared against."""

    return LINUX_COMPATIBLE_OS if cell_os in LINUX_COMPATIBLE_OS else frozenset({cell_os})


def select_matched_cell(
    cells: tuple[CellTarget, ...], jdk_major: int
) -> tuple[str | None, tuple[str, ...]]:
    """Choose the cell a run on ``jdk_major`` is measured against.

    :func:`match_cell` decides which toolchain is admissible and what that cost.
    Among the cells of that same toolchain on an interchangeable platform the
    widest proven universe wins: a 31-test flaky rerun pool is not the goalpost
    the main suite of the same JDK sets, and alphabetical order alone would have
    picked it.

    Interchangeable is the same class :func:`match_cell` admits from, not string
    equality of the platform label: a pool artifact name carries no OS, so a
    strict comparison would let an ``ubuntu``-labelled conclusion cell of the
    same JDK -- which :func:`match_cell` ranks first for being labelled at all --
    keep a match away from the pool that actually counted that toolchain's tests.

    A laundered conclusion-grade cell is not a candidate at all: its conclusion
    is a statement about the workflow file, so it proves nothing for a run to
    match, and its bare toolchain annotation would otherwise outrank the pool
    that actually counted tests.
    """

    notes: list[str] = []
    eligible = tuple(
        cell for cell in cells if not (cell.laundered_conclusion and cell.grade == "B")
    )
    excluded = len(cells) - len(eligible)
    if excluded:
        notes.append(
            "laundered conclusion-grade cells cannot be the target: "
            f"{excluded} of {len(cells)} excluded from matching"
        )

    match = match_cell(eligible, jdk_major)
    if match.caveat:
        notes.append(f"matched cell: {match.caveat}")
    if match.cell_id is None:
        return None, tuple(notes)

    chosen_jdk = extract_cell_jdk(match.cell_id)
    chosen_os = extract_cell_os(match.cell_id)
    peers = interchangeable_os(chosen_os)
    equivalent = [
        cell
        for cell in eligible
        if extract_cell_jdk(cell.cell_id) == chosen_jdk and extract_cell_os(cell.cell_id) in peers
    ]
    widest = sorted(
        equivalent,
        key=lambda cell: (-cell.executed_count, 0 if cell.grade == "A" else 1, cell.cell_id),
    )[0]
    if widest.cell_id != match.cell_id:
        widest_os = extract_cell_os(widest.cell_id)
        platform = (
            ""
            if widest_os == chosen_os
            else f", whose platform reads {widest_os} where {chosen_os} was first matched"
        )
        notes.append(
            f"matched cell {widest.cell_id} over {match.cell_id}: the widest proven "
            f"universe on the same toolchain ({widest.executed_count} executions){platform}"
        )
    return widest.cell_id, tuple(notes)


def assemble_target_record(
    snapshot_dir: Path,
    *,
    repo: str,
    sha: str,
    harvested_at: str,
    jdk_major: int | None = None,
) -> TargetRecord:
    """Assemble one target record from a snapshot directory, offline."""

    if not snapshot_dir.is_dir():
        raise HarvestError(f"{snapshot_dir} is not a directory")

    vet, workflow_files = vet_snapshot_workflows(snapshot_dir)
    notes: list[str] = []

    pools = sorted(snapshot_dir.glob(POOL_GLOB))
    cells: list[CellTarget] = []
    taken: set[str] = set()
    pool_ids: list[str] = []
    annotated = 0
    for pool_path in pools:
        harvested = cell_from_pool(read_pool(pool_path))
        if harvested.cell.cell_id in taken:
            raise HarvestError(f"two pools claim the cell id {harvested.cell.cell_id}")
        taken.add(harvested.cell.cell_id)
        pool_ids.append(pool_path.stem)
        if harvested.cell.cell_id != pool_path.stem:
            annotated += 1
        cells.append(harvested.cell)
        notes.extend(harvested.notes)
    if annotated:
        notes.append(
            f"{annotated} cell ids end in '(jdk N)': that toolchain is the harvester's "
            "reading of the pool artifact name, not a name upstream chose"
        )

    covered = 0
    duplicates = 0
    defeaters: list[str] = []
    for name, conclusion in read_jobs(snapshot_dir):
        covering = next((pool_id for pool_id in pool_ids if pool_covers_check(pool_id, name)), None)
        # A conclusion that decided nothing, or decided green, says nothing a
        # pool's own XML has not already said.  A failed one contradicts it, and
        # a defeater of counted work is evidence, so it keeps its cell.
        if covering is not None and check_build_outcome(conclusion) != "failed":
            covered += 1
            continue
        if name in taken:
            duplicates += 1
            continue
        taken.add(name)
        cells.append(cell_from_check(name, conclusion, laundered=vet.laundered))
        if covering is not None:
            defeaters.append(f"{name} ({conclusion}) over {pool_cell_id(covering)}")

    if not cells:
        raise HarvestError(f"{snapshot_dir} carries no JUnit pool and no check conclusion")

    if workflow_files:
        notes.append(f"laundering vet read {', '.join(workflow_files)}")
    else:
        notes.append("no workflow config was harvested, so no laundering vet was possible")
    if vet.laundered:
        notes.append(
            "the workflow config swallows a build or test failure at "
            f"{', '.join(vet.locations)}; conclusion-grade cells are marked laundered "
            "and their build outcome is withheld"
        )
    if covered:
        notes.append(
            f"{covered} checks are already measured by a JUnit pool and carry no "
            "separate conclusion-grade cell"
        )
    if defeaters:
        # Bounded because the record refuses a note past 2,000 characters and a
        # cell id may be 128 of them; the count is the claim, the names are aid.
        listed = "; ".join(defeaters[:5])
        remainder = f", and {len(defeaters) - 5} more" if len(defeaters) > 5 else ""
        notes.append(
            f"{len(defeaters)} checks concluded failed over work a JUnit pool already "
            f"measures and keep their own cell as a defeater of its counts: {listed}{remainder}"
        )
    if duplicates:
        notes.append(f"{duplicates} repeated check names were collapsed into their first cell")

    matched_cell: str | None = None
    if jdk_major is not None:
        matched_cell, match_notes = select_matched_cell(tuple(cells), jdk_major)
        notes.extend(match_notes)

    return TargetRecord(
        repo=repo,
        sha=sha,
        harvested_at=harvested_at,
        cells=tuple(cells),
        matched_cell=matched_cell,
        notes=tuple(notes),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarvestError(f"{path.name} is not readable JSON: {exc}") from exc


def identify_revision(snapshot_dir: Path) -> tuple[str | None, str | None]:
    """Read ``(repo, sha)`` out of whatever the snapshot recorded."""

    repo: str | None = None
    sha: str | None = None
    metadata = snapshot_dir / METADATA_FILE
    if metadata.exists():
        body = _read_json(metadata)
        if isinstance(body, dict):
            sha = sha or _clean(body.get("head_sha"))
            repo = repo or _repo_from_api_url(_clean(body.get("url")))
    statuses = snapshot_dir / STATUSES_FILE
    if statuses.exists():
        body = _read_json(statuses)
        if isinstance(body, dict):
            sha = sha or _clean(body.get("sha"))
            repository = body.get("repository")
            if isinstance(repository, dict):
                repo = repo or _clean(repository.get("full_name"))
    jobs = snapshot_dir / JOBS_FILE
    if jobs.exists():
        body = _read_json(jobs)
        first = (body.get("jobs") or [None])[0] if isinstance(body, dict) else None
        if isinstance(first, dict):
            sha = sha or _clean(first.get("head_sha"))
            repo = repo or _repo_from_api_url(_clean(first.get("run_url")))
    return repo, sha


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _repo_from_api_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/repos/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", url)
    return match.group(1) if match else None


def harvest_from_dir(
    snapshot_dir: Path,
    *,
    repo: str | None,
    sha: str | None,
    harvested_at: str,
    jdk_major: int | None,
    out_path: Path | None = None,
) -> tuple[TargetRecord, str, Path]:
    """Assemble, write and digest one target record from a snapshot directory."""

    found_repo, found_sha = identify_revision(snapshot_dir)
    repo = repo or found_repo
    sha = sha or found_sha
    if not repo:
        raise HarvestError("the snapshot does not name its repository; pass --repo")
    if not sha:
        raise HarvestError("the snapshot does not name its commit; pass --sha")

    record = assemble_target_record(
        snapshot_dir,
        repo=repo,
        sha=sha,
        harvested_at=harvested_at,
        jdk_major=jdk_major,
    )
    destination = out_path or (snapshot_dir / RECORD_FILE)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Written as the exact bytes the digest is taken over, so the file's own
    # sha256 and the record's identity digest are the same number.
    destination.write_text(canonical_json(record.model_dump(mode="json")), encoding="utf-8")
    return record, target_record_sha256(record), destination


def summarize(record: TargetRecord, digest: str, destination: Path) -> str:
    """Return the one-line summary the CLI prints."""

    grade_a = sum(1 for cell in record.cells if cell.grade == "A")
    matched = next(
        (cell for cell in record.cells if cell.cell_id == record.matched_cell),
        None,
    )
    laundered = any(cell.laundered_conclusion for cell in record.cells)
    return " ".join(
        [
            f"sha256={digest}",
            f"repo={record.repo}",
            f"sha={record.sha}",
            f"cells={len(record.cells)}",
            f"grade_a={grade_a}",
            f"grade_b={len(record.cells) - grade_a}",
            f"laundered={'true' if laundered else 'false'}",
            f"matched={matched.cell_id if matched else '-'}",
            f"executed={matched.executed_count if matched else '-'}",
            f"red={matched.red_count if matched else '-'}",
            f"flaky={matched.flaky_count if matched else '-'}",
            f"out={destination}",
        ]
    )


# ---------------------------------------------------------------------------
# The network courier: never exercised by tests, and never a judge.
# ---------------------------------------------------------------------------
def fetch(path: str) -> Any:
    """Return the parsed JSON body of one ``gh api`` GET."""

    completed = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise HarvestError(f"gh api {path} failed: {detail[-1] if detail else 'no detail'}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HarvestError(f"gh api {path} did not answer JSON: {exc}") from exc


def download(path: str, destination: Path) -> None:
    """Stream one ``gh api`` GET into a file."""

    with destination.open("wb") as handle:
        completed = subprocess.run(
            ["gh", "api", path], stdout=handle, stderr=subprocess.PIPE, check=False
        )
    if completed.returncode != 0:
        destination.unlink(missing_ok=True)
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise HarvestError(f"gh api {path} failed: {detail[-1] if detail else 'no detail'}")


# The same noise vocabulary the selector walks past: a workflow whose name or
# path matches proves nothing about build or test outcomes.  Dependabot runs
# additionally report a virtual path under dynamic/ that is not a fetchable
# workflow file.
NOISE_WORKFLOWS = (
    "codeql", "dependabot", "copilot", "label", "stale", "docs", "website",
    "site", "sonar", "triage", "comment", "notify", "lint-pr", "semantic",
    "dco", "analyze", "dependency review", "license", "scorecard",
)


def _is_noise_run(run: dict) -> bool:
    text = f"{run.get('name') or ''} {run.get('path') or ''}".lower()
    return str(run.get("path") or "").startswith("dynamic/") or any(
        token in text for token in NOISE_WORKFLOWS
    )


_RUN_ID_IN_URL = re.compile(r"/actions/runs/(\d+)")


def fetch_snapshot(repo: str, sha: str, out_dir: Path) -> Path:
    """Pull one revision's run evidence into ``out_dir`` and return it.

    The commit's own check-run surface is the authority on what CI concluded
    here — merge-queue and workflow_run-triggered workflows stamp checks on a
    commit whose ``actions/runs?head_sha`` listing is empty (kafka trunk), and
    dependabot noise dominates that listing elsewhere (curator).  So the
    courier reads check-runs first, converts them to the jobs shape the
    assembler already understands, and follows their details_url run ids only
    to collect JUnit pool artifacts and workflow configs.  Attempt 1 of the
    d3 freeze (2026-08-30) read one recency-picked run instead and harvested
    noise or nothing; this is the repair.
    """

    out_dir.mkdir(parents=True, exist_ok=True)

    checks: list[dict] = []
    for page in (1, 2):
        body = fetch(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}")
        page_runs = (body.get("check_runs") if isinstance(body, dict) else []) or []
        checks.extend(run for run in page_runs if isinstance(run, dict))
        if len(page_runs) < 100:
            break
    # The raw check surface is the one artifact a later re-assembly cannot
    # re-derive offline (it carries the check-to-run attribution a
    # per-workflow laundering vet will need), so it is kept verbatim.
    (out_dir / "check-runs.json").write_text(canonical_json(checks), encoding="utf-8")
    signal_checks = [
        check
        for check in checks
        if str(check.get("status") or "") == "completed"
        and not any(token in str(check.get("name") or "").lower() for token in NOISE_WORKFLOWS)
    ]

    run_ids: list[int] = []
    for check in signal_checks:
        match = _RUN_ID_IN_URL.search(str(check.get("details_url") or ""))
        if match and int(match.group(1)) not in run_ids:
            run_ids.append(int(match.group(1)))
    listed = fetch(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")
    for run in (listed.get("workflow_runs") if isinstance(listed, dict) else []) or []:
        if (
            isinstance(run, dict)
            and run.get("status") == "completed"
            and not _is_noise_run(run)
            and run["id"] not in run_ids
        ):
            run_ids.append(run["id"])

    merged_jobs: list[dict] = [
        {"name": str(check.get("name") or "").strip(), "conclusion": check.get("conclusion")}
        for check in signal_checks
        if str(check.get("name") or "").strip()
    ]
    seen_workflow_paths: set[str] = set()
    signal_runs: list[dict] = []
    for run_id in run_ids:
        try:
            run = fetch(f"repos/{repo}/actions/runs/{run_id}")
        except HarvestError:
            continue
        if not isinstance(run, dict) or _is_noise_run(run):
            continue
        signal_runs.append(run)
        artifacts = fetch(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
        (out_dir / f"run-{run_id}-artifacts.json").write_text(
            canonical_json(artifacts), encoding="utf-8"
        )
        for artifact in (artifacts.get("artifacts") if isinstance(artifacts, dict) else []) or []:
            name = str(artifact.get("name") or "").strip()
            destination = out_dir / f"{name}.zip"
            if not name.startswith("junit-xml") or destination.exists():
                continue
            download(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", destination)
        seen_workflow_paths.add(str(run.get("path") or "").strip())

    if not merged_jobs and not signal_runs:
        raise HarvestError(f"{repo}@{sha} has no completed non-noise check or workflow run")

    if signal_runs:
        signal_runs.sort(key=lambda item: (str(item.get("run_started_at") or ""), item["id"]))
        (out_dir / "runs-index.json").write_text(canonical_json(signal_runs), encoding="utf-8")
        (out_dir / METADATA_FILE).write_text(canonical_json(signal_runs[-1]), encoding="utf-8")

    (out_dir / JOBS_FILE).write_text(canonical_json({"jobs": merged_jobs}), encoding="utf-8")
    statuses = fetch(f"repos/{repo}/commits/{sha}/status")
    (out_dir / STATUSES_FILE).write_text(canonical_json(statuses), encoding="utf-8")

    for workflow_path in sorted(seen_workflow_paths):
        if not workflow_path or workflow_path.startswith("dynamic/"):
            continue
        try:
            body = fetch(f"repos/{repo}/contents/{workflow_path}?ref={sha}")
        except HarvestError:
            # A workflow file that moved between the run and the harvest is a
            # gap in the vet, not a reason to drop the whole snapshot; the
            # assembler discloses the un-vetted state on its own.
            continue
        content = body.get("content") if isinstance(body, dict) else None
        if isinstance(content, str):
            workflows = out_dir / WORKFLOWS_DIR
            workflows.mkdir(parents=True, exist_ok=True)
            (workflows / Path(workflow_path).name).write_bytes(base64.b64decode(content))
    return out_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="d3_harvest_target.py",
        description="Assemble a CI run's evidence into a sag.metrics target record.",
    )
    parser.add_argument("--from-dir", default=None, help="an already-harvested snapshot directory")
    parser.add_argument("--repo", default=None, help="owner/name, e.g. apache/kafka")
    parser.add_argument("--sha", default=None, help="the revision the target is about")
    parser.add_argument(
        "--jdk", type=int, default=None, help="the run's JDK major, to match a cell"
    )
    parser.add_argument("--out", default=None, help=f"where to write {RECORD_FILE}")
    parser.add_argument("--out-dir", default=None, help="where the network mode fetches evidence")
    parser.add_argument(
        "--harvested-at",
        default=None,
        help="the harvest timestamp to record (default: now, UTC)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    harvested_at = (args.harvested_at or "").strip() or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        if args.from_dir:
            snapshot_dir = Path(args.from_dir)
        elif args.repo and args.sha:
            if not args.out_dir:
                raise HarvestError("network mode needs --out-dir to fetch the evidence into")
            snapshot_dir = fetch_snapshot(args.repo, args.sha, Path(args.out_dir))
        else:
            parser.error("pass --from-dir, or --repo with --sha and --out-dir")
            return 2
        record, digest, destination = harvest_from_dir(
            snapshot_dir,
            repo=args.repo,
            sha=args.sha,
            harvested_at=harvested_at,
            jdk_major=args.jdk,
            out_path=Path(args.out) if args.out else None,
        )
    except (HarvestError, ValueError) as exc:
        print(f"D3 HARVEST: {exc}", file=sys.stderr)
        return 1
    print(summarize(record, digest, destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
