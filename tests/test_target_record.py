"""The external target record's validators and its canonical digest."""

import pytest
from pydantic import ValidationError

from sag.metrics.target_record import (
    TARGET_RECORD_SCHEMA_VERSION,
    CellTarget,
    TargetRecord,
    target_record_sha256,
)


def _cell(**overrides):
    payload = {
        "cell_id": "JDK17 ubuntu-latest",
        "build": "ok",
        "executed_count": 2,
        "executed_ids": ("b#two", "a#one"),
        "red_count": 0,
        "grade": "A",
    }
    payload.update(overrides)
    return CellTarget(**payload)


def _record(**overrides):
    payload = {
        "repo": "apache/kafka",
        "sha": "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b",
        "harvested_at": "2026-08-27T12:04:00Z",
        "cells": (_cell(),),
    }
    payload.update(overrides)
    return TargetRecord(**payload)


class TestCellTargetIdentitySets:
    def test_id_lists_are_stripped_and_sorted(self):
        cell = _cell(executed_ids=("  b#two ", "a#one"))
        assert cell.executed_ids == ("a#one", "b#two")

    def test_a_blank_identity_is_rejected(self):
        with pytest.raises(ValidationError, match="empty identity"):
            _cell(executed_ids=("a#one", "   "), executed_count=2)

    def test_a_duplicate_identity_is_rejected(self):
        with pytest.raises(ValidationError, match="duplicate identity"):
            _cell(executed_ids=("a#one", "a#one"), executed_count=2)

    def test_an_over_long_identity_is_rejected(self):
        with pytest.raises(ValidationError, match="character bound"):
            _cell(executed_ids=("x" * 513,), executed_count=1)

    def test_an_identity_at_the_bound_is_accepted(self):
        cell = _cell(executed_ids=("x" * 512,), executed_count=1)
        assert cell.executed_ids == ("x" * 512,)

    def test_modules_and_evidence_refs_are_canonicalized_too(self):
        cell = _cell(modules=(" core ", "clients"), evidence_refs=(" ref:b", "ref:a"))
        assert cell.modules == ("clients", "core")
        assert cell.evidence_refs == ("ref:a", "ref:b")


class TestCellTargetCountAgreement:
    def test_executed_count_must_match_named_identities(self):
        with pytest.raises(ValidationError, match="executed count"):
            _cell(executed_count=3)

    def test_executed_count_stands_alone_when_no_identities_are_named(self):
        cell = _cell(executed_count=41, executed_ids=(), grade="B")
        assert cell.executed_count == 41
        assert cell.executed_ids == ()

    def test_red_count_must_match_named_red_identities(self):
        with pytest.raises(ValidationError, match="red count"):
            _cell(red_ids=("a#one",), red_count=2)

    def test_red_count_stands_alone_when_no_red_identities_are_named(self):
        cell = _cell(red_count=7, executed_ids=(), executed_count=0, grade="B")
        assert cell.red_count == 7

    def test_flaky_count_must_not_contradict_named_flaky_identities(self):
        with pytest.raises(ValidationError, match="flaky count"):
            _cell(flaky_ids=("a#one",), flaky_count=2)

    def test_named_flaky_identities_state_the_count_themselves(self):
        assert _cell(flaky_ids=("a#one",)).flaky_count == 1

    def test_flaky_count_stands_alone_when_no_flaky_identities_are_named(self):
        cell = _cell(flaky_count=3, flaky_ids=())
        assert cell.flaky_count == 3
        assert cell.flaky_ids == ()

    def test_negative_counts_are_rejected(self):
        with pytest.raises(ValidationError):
            _cell(executed_count=-1, executed_ids=())
        with pytest.raises(ValidationError):
            _cell(skipped=-1)


class TestCellTargetSubsetRules:
    def test_red_and_flaky_cannot_overlap(self):
        with pytest.raises(ValidationError, match="both finally red and flaky"):
            _cell(red_ids=("a#one",), red_count=1, flaky_ids=("a#one",))

    def test_red_ids_must_be_executed(self):
        with pytest.raises(ValidationError, match="red ids must be a subset"):
            _cell(red_ids=("c#three",), red_count=1)

    def test_flaky_ids_must_be_executed(self):
        with pytest.raises(ValidationError, match="flaky ids must be a subset"):
            _cell(flaky_ids=("c#three",))

    def test_subset_rules_are_waived_when_no_universe_is_named(self):
        cell = _cell(
            executed_ids=(),
            executed_count=9,
            red_ids=("c#three",),
            red_count=1,
            flaky_ids=("d#four",),
            grade="B",
        )
        assert cell.red_ids == ("c#three",)
        assert cell.flaky_ids == ("d#four",)

    def test_a_red_and_flaky_cell_that_obeys_every_rule_validates(self):
        cell = _cell(
            executed_count=3,
            executed_ids=("a#one", "b#two", "c#three"),
            red_ids=("c#three",),
            red_count=1,
            flaky_ids=("b#two",),
        )
        assert cell.red_ids == ("c#three",)
        assert cell.flaky_ids == ("b#two",)


class TestCellTargetShape:
    def test_the_cell_is_frozen_and_forbids_extra_fields(self):
        cell = _cell()
        with pytest.raises(ValidationError):
            cell.build = "failed"
        with pytest.raises(ValidationError):
            _cell(unexpected="x")

    def test_build_and_grade_are_closed_vocabularies(self):
        assert _cell(build="failed").build == "failed"
        assert _cell(build="unknown").build == "unknown"
        assert _cell(grade="B").grade == "B"
        with pytest.raises(ValidationError):
            _cell(build="green")
        with pytest.raises(ValidationError):
            _cell(grade="C")

    def test_a_blank_cell_id_is_rejected(self):
        with pytest.raises(ValidationError):
            _cell(cell_id="   ")
        with pytest.raises(ValidationError):
            _cell(cell_id="")

    def test_an_over_long_cell_id_is_rejected(self):
        with pytest.raises(ValidationError):
            _cell(cell_id="x" * 129)

    def test_laundered_conclusion_defaults_to_false(self):
        assert _cell().laundered_conclusion is False
        assert _cell(laundered_conclusion=True).laundered_conclusion is True


class TestLaunderedCells:
    """A swallowed exit status buys a cell no standing it did not earn."""

    def _conclusion_cell(self, **overrides):
        payload = {
            "grade": "B",
            "executed_count": 0,
            "executed_ids": (),
            "red_count": 0,
            "laundered_conclusion": True,
        }
        payload.update(overrides)
        return _cell(**payload)

    def test_a_laundered_conclusion_grade_cell_cannot_state_a_build_outcome(self):
        for outcome in ("ok", "failed"):
            with pytest.raises(ValidationError, match="cannot state a build outcome"):
                self._conclusion_cell(build=outcome)
        assert self._conclusion_cell(build="unknown").build == "unknown"

    def test_a_laundered_cell_that_counted_tests_keeps_its_outcome(self):
        # continue-on-error cannot touch the XML, so count-grade cells survive.
        cell = _cell(laundered_conclusion=True, grade="A", build="ok")
        assert cell.build == "ok"

    def test_a_laundered_conclusion_cannot_be_the_goalpost(self):
        cell = self._conclusion_cell(build="unknown")
        with pytest.raises(ValidationError, match="cannot be the matched cell"):
            _record(cells=(cell,), matched_cell=cell.cell_id)

    def test_a_laundered_conclusion_may_still_be_recorded_beside_a_real_goalpost(self):
        laundered = self._conclusion_cell(build="unknown", cell_id="JDK17 windows-latest")
        record = _record(cells=(_cell(), laundered), matched_cell="JDK17 ubuntu-latest")
        assert record.matched_cell == "JDK17 ubuntu-latest"
        assert record.cells[1].laundered_conclusion is True

    def test_a_laundered_count_grade_cell_is_still_a_usable_goalpost(self):
        cell = _cell(laundered_conclusion=True, grade="A")
        record = _record(cells=(cell,), matched_cell=cell.cell_id)
        assert record.matched_cell == cell.cell_id


class TestTargetRecordShape:
    def test_the_schema_version_is_pinned(self):
        assert _record().schema_version == TARGET_RECORD_SCHEMA_VERSION == 1
        with pytest.raises(ValidationError):
            _record(schema_version=2)

    def test_repo_must_be_owner_slash_name(self):
        assert _record(repo=" apache/kafka ").repo == "apache/kafka"
        for bad in ("kafka", "apache/kafka/core", "apache /kafka", ""):
            with pytest.raises(ValidationError):
                _record(repo=bad)

    def test_sha_must_be_lowercase_hex_within_bounds(self):
        assert _record(sha="0a1b2c3").sha == "0a1b2c3"
        for bad in ("0a1b2c", "0A1B2C3D", "zzzzzzz", "0a1b2c3d" * 9):
            with pytest.raises(ValidationError):
                _record(sha=bad)

    def test_harvested_at_is_validated_by_shape(self):
        for good in (
            "2026-08-27T12:04:00Z",
            "2026-08-27 12:04:00",
            "2026-08-27T12:04:00.123456+02:00",
        ):
            assert _record(harvested_at=good).harvested_at == good
        for bad in ("27-08-2026", "2026-08-27", "yesterday", "2026-08-27T12:04"):
            with pytest.raises(ValidationError):
                _record(harvested_at=bad)

    def test_at_least_one_cell_is_required(self):
        with pytest.raises(ValidationError):
            _record(cells=())

    def test_cell_ids_must_be_unique(self):
        with pytest.raises(ValidationError, match="duplicate cell id"):
            _record(cells=(_cell(), _cell()))

    def test_notes_keep_authored_order_and_reject_blanks(self):
        record = _record(notes=(" zebra ", "alpha"))
        assert record.notes == ("zebra", "alpha")
        with pytest.raises(ValidationError, match="empty note"):
            _record(notes=("  ",))
        with pytest.raises(ValidationError, match="character bound"):
            _record(notes=("x" * 2_001,))

    def test_the_record_is_frozen_and_forbids_extra_fields(self):
        record = _record()
        with pytest.raises(ValidationError):
            record.repo = "other/repo"
        with pytest.raises(ValidationError):
            _record(unexpected="x")


class TestMatchedCell:
    def test_matched_cell_defaults_to_none(self):
        assert _record().matched_cell is None

    def test_matched_cell_must_name_a_harvested_cell(self):
        with pytest.raises(ValidationError, match="does not name a harvested cell"):
            _record(matched_cell="JDK21 ubuntu-latest")

    def test_matched_cell_accepts_a_harvested_cell(self):
        record = _record(matched_cell=" JDK17 ubuntu-latest ")
        assert record.matched_cell == "JDK17 ubuntu-latest"

    def test_a_blank_matched_cell_is_rejected(self):
        with pytest.raises(ValidationError, match="cannot be blank"):
            _record(matched_cell="   ")


class TestTargetRecordDigest:
    def test_the_digest_is_a_sha256_hex_string(self):
        digest = target_record_sha256(_record())
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_the_digest_ignores_how_identities_were_ordered_on_input(self):
        left = _record(cells=(_cell(executed_ids=("a#one", "b#two")),))
        right = _record(cells=(_cell(executed_ids=("b#two", " a#one")),))
        assert target_record_sha256(left) == target_record_sha256(right)

    def test_the_digest_tracks_a_changed_verdict(self):
        baseline = _record()
        changed = _record(cells=(_cell(build="failed"),))
        assert target_record_sha256(baseline) != target_record_sha256(changed)

    def test_the_digest_tracks_the_matched_cell(self):
        assert target_record_sha256(_record()) != target_record_sha256(
            _record(matched_cell="JDK17 ubuntu-latest")
        )
