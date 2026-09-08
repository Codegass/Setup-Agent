"""The external target record: what upstream CI proved, cell by cell.

One :class:`CellTarget` is one CI cell (a matrix job on one JDK and one OS).
One :class:`TargetRecord` is every cell harvested for a single repository
revision, plus the cell a SAG run was matched against.

The record is a claim about identities, not about rates: a cell names the tests
it executed and the tests that stayed red, so a later comparison is a set
operation rather than a percentage.  Counts are integers; there are no floats.
Every identity set the record cannot hold -- too many identities, or one too
long -- falls back to its count and never to nothing, so a harvest that drops
36,259 executions or one 600-character flaky name still states how many there
were.

A laundered cell is one whose workflow swallowed a build or test failure
(``continue-on-error``), so its conclusion states what the workflow file
allows, not what the code did.  The record keeps such a cell as disclosure but
gives its conclusion no standing: a laundered conclusion-grade cell reports no
build outcome and can never be the matched cell.  Counts are untouched by a
swallowed exit status, so a laundered cell that carries them stays a target.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sag.metrics.module_keys import module_key

TARGET_RECORD_SCHEMA_VERSION: Literal[2] = 2
ModulesBasis = Literal["log", "declared", "test_bearing"]

BuildOutcome = Literal["ok", "failed", "unknown"]
# Grade A: the cell's test identities come from parsed JUnit XML.
# Grade B: only the cell's aggregate conclusion is known.
CellGrade = Literal["A", "B"]

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
# Shape only. Whether the instant exists is the harvester's problem, not the
# record's; a record must not reject evidence over a leap second.
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?$")


def _canonical_ids(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} contains an empty identity")
        item = value.strip()
        if len(item) > 512:
            raise ValueError(f"{label} identity exceeds the character bound")
        if item in normalized:
            raise ValueError(f"{label} contains a duplicate identity")
        normalized.append(item)
    return tuple(sorted(normalized))


class CellTarget(BaseModel):
    """One CI cell's proven test universe on one revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cell_id: str = Field(min_length=1, max_length=128)
    build: BuildOutcome
    executed_count: int = Field(ge=0)
    executed_ids: tuple[str, ...] = ()
    red_count: int = Field(ge=0)
    red_ids: tuple[str, ...] = ()
    flaky_count: int = Field(default=0, ge=0)
    flaky_ids: tuple[str, ...] = ()
    skipped: int = Field(default=0, ge=0)
    modules: tuple[str, ...] = ()
    modules_basis: ModulesBasis | None = None
    command: str | None = Field(default=None, max_length=2_000)
    grade: CellGrade
    laundered_conclusion: bool = False
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_identity_sets(self) -> "CellTarget":
        cell_id = self.cell_id.strip()
        if not cell_id:
            raise ValueError("a cell id cannot be blank")

        executed = _canonical_ids(self.executed_ids, label="executed ids")
        red = _canonical_ids(self.red_ids, label="red ids")
        flaky = _canonical_ids(self.flaky_ids, label="flaky ids")
        modules = _canonical_ids(self.modules, label="modules")
        for name in modules:
            if module_key(name) != name:
                raise ValueError(f"module identity {name!r} is not canonical")
        if modules and self.modules_basis is None:
            raise ValueError("modules need a basis stating where they were read from")
        if self.modules_basis is not None and not modules:
            raise ValueError("a modules basis needs modules")
        command = self.command.strip() if self.command is not None else None
        if command is not None and not command:
            raise ValueError("command cannot be blank")
        refs = _canonical_ids(self.evidence_refs, label="cell evidence refs")

        if executed and self.executed_count != len(executed):
            raise ValueError("executed count does not match the executed identities")
        if red and self.red_count != len(red):
            raise ValueError("red count does not match the red identities")
        # A cell that lists its flaky identities need not restate their number,
        # but it may never state a number that contradicts them: a flaky test
        # too long to store is still a flaky test, and the count is where it
        # survives.
        flaky_count = self.flaky_count
        if flaky and flaky_count not in (0, len(flaky)):
            raise ValueError("flaky count does not match the flaky identities")
        if flaky:
            flaky_count = len(flaky)
        if set(red) & set(flaky):
            raise ValueError("a test cannot be both finally red and flaky")
        if executed:
            universe = set(executed)
            if not set(red).issubset(universe):
                raise ValueError("red ids must be a subset of executed ids")
            if not set(flaky).issubset(universe):
                raise ValueError("flaky ids must be a subset of executed ids")

        # A conclusion-grade cell knows only its conclusion, so laundering
        # leaves it nothing to report: it may be recorded, never as an outcome.
        if self.laundered_conclusion and self.grade == "B" and self.build != "unknown":
            raise ValueError("a laundered conclusion-grade cell cannot state a build outcome")

        object.__setattr__(self, "cell_id", cell_id)
        object.__setattr__(self, "executed_ids", executed)
        object.__setattr__(self, "red_ids", red)
        object.__setattr__(self, "flaky_count", flaky_count)
        object.__setattr__(self, "flaky_ids", flaky)
        object.__setattr__(self, "modules", modules)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "evidence_refs", refs)
        return self


class TargetRecord(BaseModel):
    """Every harvested cell for one repository revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = TARGET_RECORD_SCHEMA_VERSION
    repo: str = Field(min_length=3, max_length=256)
    sha: str = Field(min_length=7, max_length=64)
    harvested_at: str = Field(min_length=1, max_length=64)
    cells: tuple[CellTarget, ...] = Field(min_length=1)
    matched_cell: str | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_record(self) -> "TargetRecord":
        repo = self.repo.strip()
        if not _REPO_RE.match(repo):
            raise ValueError("repo must be owner/name")
        sha = self.sha.strip()
        if not _SHA_RE.match(sha):
            raise ValueError("sha must be 7 to 64 lowercase hex characters")
        harvested_at = self.harvested_at.strip()
        if not _ISO_RE.match(harvested_at):
            raise ValueError("harvested_at must be an ISO-8601 timestamp")

        cell_ids = [cell.cell_id for cell in self.cells]
        if len(set(cell_ids)) != len(cell_ids):
            raise ValueError("cells contain a duplicate cell id")

        matched = self.matched_cell.strip() if self.matched_cell is not None else None
        if matched is not None:
            if not matched:
                raise ValueError("matched cell cannot be blank")
            if matched not in set(cell_ids):
                raise ValueError("matched cell does not name a harvested cell")
            goalpost = next(cell for cell in self.cells if cell.cell_id == matched)
            # The goalpost is what a run must match or beat; a laundered
            # conclusion proves nothing, so a run cannot be measured against it.
            if goalpost.laundered_conclusion and goalpost.grade == "B":
                raise ValueError("a laundered conclusion-grade cell cannot be the matched cell")

        notes: list[str] = []
        for note in self.notes:
            if not isinstance(note, str) or not note.strip():
                raise ValueError("notes contain an empty note")
            item = note.strip()
            if len(item) > 2_000:
                raise ValueError("a note exceeds the character bound")
            notes.append(item)

        object.__setattr__(self, "repo", repo)
        object.__setattr__(self, "sha", sha)
        object.__setattr__(self, "harvested_at", harvested_at)
        object.__setattr__(self, "matched_cell", matched)
        # Note order is authored order: notes are prose, not an identity set.
        object.__setattr__(self, "notes", tuple(notes))
        return self


def target_record_sha256(record: TargetRecord) -> str:
    """Return the canonical identity digest of one target record."""

    return hashlib.sha256(
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
