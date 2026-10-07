"""Dataset-level denominators and strata preserve grain and missingness."""
import pytest

from scripts.assemble_benchmark_rescreen import observed_kinds, size_strata, stratum, task_index


def source(value, status="java_content_verified"):
    return {"source_completeness": status, "measured_size": {"java_physical_lines": value}}


def test_quartiles_use_only_verified_sources_and_keep_unknown_separate():
    rows = [source(v) for v in [10, 20, 30, 40]] + [source(1000000, "conflict")]
    threshold = size_strata(rows)
    assert (threshold["q1"], threshold["q3"], threshold["verified_projects"]) == (17.5, 32.5, 4)
    assert [stratum(r, threshold) for r in rows] == ["small", "medium", "medium", "large", "size_unknown"]
    assert threshold["is_admission_threshold"] is False


def test_ties_are_not_split_to_force_group_quotas():
    rows = [source(100)] * 8
    threshold = size_strata(rows)
    assert {stratum(r, threshold) for r in rows} == {"small"}
    assert stratum(source(10), size_strata([source(10)])) == "size_unknown"


@pytest.mark.parametrize("rows", [
    [{"task_id": "a", "repo": "o/r", "sha": "bad"}],
    [{"task_id": "a", "repo": "o/r", "sha": "x"}] * 2,
    [{"task_id": "b", "repo": "o/r", "sha": "x"}],
])
def test_task_joins_reject_revision_drift_missing_or_duplicate_tasks(rows):
    with pytest.raises(ValueError):
        task_index(rows, {"a": {"task_id": "a", "repo": "o/r", "sha": "x"}})


def test_observed_labels_count_each_project_category_once_not_each_module():
    task = {"labels": [{"kind": "test", "status": "observed"}] * 20 +
                      [{"kind": "package", "status": "candidate"}, {"kind": "native_compile", "status": "unknown"}]}
    assert observed_kinds(task) == ["test"]
