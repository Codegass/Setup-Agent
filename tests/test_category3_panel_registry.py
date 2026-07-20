"""The Category-3 probe registry + panel lock: pinned SHAs, arms, floors.

These are DATA assertions over the committed panel definition and its pinned
lock file — the registry is the panel spec's probe table made machine-checkable.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PANEL = ROOT / "scripts" / "category3_panel.json"
LOCK = ROOT / "logs" / "panel-category3" / "panel-lock.json"

# The exact release SHAs the panel pins (fixed recent releases). The spec's
# probe set is EXACTLY these four (analyzer-diet.md:442-447); cloudstack/dubbo
# were an invented "baseline-only" detour and are removed — the real panel
# precondition is the suite baseline-reds gate (analyzer-diet.md:536-555).
PINNED_SHAS = {
    "bigtop": "e32423c444a9311b802946d5b695767a9b921e1e",
    "tvm": "3a5b4d4e64707a1528146e28c0fb75f45da99dd7",
    "pyyaml": "49790e73684bebad1df05ef8d828fa12f685bffb",
    "httpcomponents-client": "4f86ca6a5eb528613edb892a4f7161e23dce15d7",
}

PANEL_PROBES = ("bigtop", "tvm", "pyyaml", "httpcomponents-client")


@pytest.fixture(scope="module")
def panel():
    return json.loads(PANEL.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lock():
    return json.loads(LOCK.read_text(encoding="utf-8"))


def test_panel_defines_every_probe_with_its_pinned_sha(panel):
    probes = panel["probes"]
    assert set(probes) == set(PINNED_SHAS)
    for name, sha in PINNED_SHAS.items():
        assert probes[name]["ref"] == sha, name


def test_new_python_probes_carry_their_upstream_urls(panel):
    probes = panel["probes"]
    assert probes["pyyaml"]["url"] == "https://github.com/yaml/pyyaml.git"
    assert (
        probes["httpcomponents-client"]["url"]
        == "https://github.com/apache/httpcomponents-client.git"
    )


def test_panel_has_exactly_the_four_spec_probes(panel):
    # No invented baseline-only probes (round-review P1-3).
    assert set(panel["probes"]) == set(PANEL_PROBES)


def test_panel_probes_all_run_both_arms(panel):
    probes = panel["probes"]
    for name in PANEL_PROBES:
        assert probes[name]["arms"] == ["P", "F"], name


def test_panel_registers_the_six_baseline_reds_as_the_precondition(panel):
    accepted = panel["baseline_reds"]["accepted"]
    by_keyword = {a["keyword"]: a["max_count"] for a in accepted}
    # SIX suite reds across four keywords (analyzer-diet.md:544-552).
    assert by_keyword == {
        "test_evidence_ingestion": 1,
        "test_stage1_review_fixes": 2,
        "test_lineage_idempotence_followup": 2,
        "test_packaging_smoke": 1,
    }
    assert sum(by_keyword.values()) == 6


def test_lock_registers_the_same_baseline_reds(lock):
    accepted = lock["baseline_reds"]["accepted"]
    assert sum(a["max_count"] for a in accepted) == 6


def test_lock_pins_the_same_shas_and_records_them_as_locked(lock):
    probes = lock["probes"]
    assert set(probes) == set(PINNED_SHAS)
    for name, sha in PINNED_SHAS.items():
        assert probes[name]["locked_sha"] == sha, name
        assert probes[name]["ref"] == sha, name


def test_lock_records_sag_sha_image_digest_source_and_model_note(lock):
    assert lock["baseline_sag_sha"]
    assert len(lock["baseline_sag_sha"]) == 40
    assert "run-pin.json" in lock["image_digest_source"]
    assert "container_image_digest" in lock["image_digest_source"]
    assert "run-pin.json" in lock["model_config_note"]


def test_lock_pins_the_evidence_source_mode(lock):
    assert lock["evidence_source_mode"] == "tool-emitted"


def test_bigtop_floors_are_the_spec_pre_registered_values(lock):
    floors = lock["pre_registered_floors"]["bigtop"]
    assert floors["compiled_classes_min"] == 96
    assert floors["unique_executed_min"] == 50
    assert floors["unique_failed_max"] == 0
    assert floors["verdict_not_unknown"] is True
    assert "phantom_green_guard" in floors
    assert "data_generators_build_success" in floors


def test_httpcomponents_floors_are_the_spec_pre_registered_values(lock):
    floors = lock["pre_registered_floors"]["httpcomponents-client"]
    assert floors["unique_executed_min"] == 1500
    assert floors["verdict"] == "success"
    assert floors["build_evidence_source"] == "physical"
    assert floors["test_phase_workdir_is_project_root"] is True


def test_tvm_floors_are_the_spec_pre_registered_values(lock):
    floors = lock["pre_registered_floors"]["tvm"]
    assert floors["collected_after_deselection_max"] == 50
    assert floors["verdict_not_unknown"] is True
    assert "build_judgment_failed_physical_or_strictly_better" in floors
    assert "pytest_node_or_k_filter_while_unbuilt" in floors


def test_pyyaml_floor_is_calibrated_not_pre_registered(lock):
    floors = lock["pre_registered_floors"]["pyyaml"]
    # The executed floor is DELIBERATELY null in the lock — calibration writes it
    # to the ledger before the panel, never here (a starved single run would have
    # registered floor 0 and made the count anchor vacuous).
    assert floors["unique_executed_min"] is None
    assert floors["unique_failed_max"] == 0
    assert floors["verdict"] == "success"
    assert floors["manifest_python_packages"] == ["yaml"]
    assert floors["stamped_manifest_exists_despite_skipped_analyze"] is True
