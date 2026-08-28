"""The external target record: what upstream CI proved, cell by cell.

One :class:`CellTarget` is one CI cell (a matrix job on one JDK and one OS).
One :class:`TargetRecord` is every cell harvested for a single repository
revision, plus the cell a SAG run was matched against.

The record is a claim about identities, not about rates: a cell names the tests
it executed and the tests that stayed red, so a later comparison is a set
operation rather than a percentage.  Counts are integers; there are no floats.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TARGET_RECORD_SCHEMA_VERSION: Literal[1] = 1

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
    flaky_ids: tuple[str, ...] = ()
    skipped: int = Field(default=0, ge=0)
    modules: tuple[str, ...] = ()
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
        refs = _canonical_ids(self.evidence_refs, label="cell evidence refs")

        if executed and self.executed_count != len(executed):
            raise ValueError("executed count does not match the executed identities")
        if red and self.red_count != len(red):
            raise ValueError("red count does not match the red identities")
        if set(red) & set(flaky):
            raise ValueError("a test cannot be both finally red and flaky")
        if executed:
            universe = set(executed)
            if not set(red).issubset(universe):
                raise ValueError("red ids must be a subset of executed ids")
            if not set(flaky).issubset(universe):
                raise ValueError("flaky ids must be a subset of executed ids")

        object.__setattr__(self, "cell_id", cell_id)
        object.__setattr__(self, "executed_ids", executed)
        object.__setattr__(self, "red_ids", red)
        object.__setattr__(self, "flaky_ids", flaky)
        object.__setattr__(self, "modules", modules)
        object.__setattr__(self, "evidence_refs", refs)
        return self


class TargetRecord(BaseModel):
    """Every harvested cell for one repository revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = TARGET_RECORD_SCHEMA_VERSION
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
