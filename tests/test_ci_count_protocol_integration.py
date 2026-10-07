"""Formal runs keep native denominator provenance when relocating inputs."""
from copy import deepcopy
import json

import pytest

from sag.benchmark.ci_count_semantics import build_jenkins_count_semantics
from sag.benchmark.requirements import (
    copy_ci_count_sources, requirements_evidence_root, validate_ci_count_metadata,
)
from test_ci_count_semantics import archive
from test_d3r2_campaign import _formal_project
from scripts import d3r2_campaign as campaign


def definition(archive):
    index_ref, kwargs, index = archive(legacy=False)
    semantics = build_jenkins_count_semantics(index_ref, **kwargs)
    alignment = {"selected_url": index["selected_url"], "selected_cell": index["selected_cell"],
                 "archived_ci_index": index_ref, "test_count_semantics": semantics}
    return kwargs["base"], {"ci_alignment": alignment}


def test_sources_survive_bundle_to_campaign_to_attempt_relocation(archive):
    base, spec = definition(archive)
    source = base / "requirements" / "net.json"
    source.parent.mkdir()
    source.write_text(json.dumps(spec))
    destination = base / "campaign"
    copy_ci_count_sources(source, destination)
    relocated = destination / "requirements" / "net.json"
    relocated.parent.mkdir()
    relocated.write_bytes(source.read_bytes())
    validate_ci_count_metadata(spec, base=requirements_evidence_root(relocated), required=True)
    attempt = destination / "attempt"
    copy_ci_count_sources(relocated, attempt)
    final = attempt / "requirements.json"
    final.write_bytes(source.read_bytes())
    result = validate_ci_count_metadata(spec, base=requirements_evidence_root(final), required=True)
    assert (result["reported_count"], result["assessed_count"]) == (557, 555)


@pytest.mark.parametrize("mutation", ["cell", "url", "index"])
def test_valid_count_record_cannot_replace_the_selected_reference(archive, mutation):
    base, spec = definition(archive)
    alignment = spec["ci_alignment"]
    if mutation == "index":
        alignment["archived_ci_index"] = {**alignment["archived_ci_index"], "sha256": "a" * 64}
    else:
        alignment["selected_" + mutation] = "another"
    with pytest.raises(ValueError):
        validate_ci_count_metadata(spec, base=base, required=True)


def test_relocated_reference_bytes_are_reverified(archive):
    base, spec = definition(archive)
    report = base / spec["ci_alignment"]["test_count_semantics"]["sources"]["test_report"]["path"]
    report.write_text("{}")
    with pytest.raises(ValueError, match="bound native source"):
        validate_ci_count_metadata(spec, base=base, required=True)


def test_formal_launch_needs_explicit_semantics_but_unknown_does_not_reject_smoke(tmp_path):
    manifest, project, _ = _formal_project(tmp_path)
    assert campaign.requirements_preflight(manifest, project)
    path = tmp_path / "requirements.json"
    spec = json.loads(path.read_text())
    spec.pop("ci_alignment")
    path.write_text(json.dumps(spec))
    project["requirements_file_sha256"] = campaign.digest(path)
    with pytest.raises(ValueError, match="count semantics"):
        campaign.requirements_preflight(manifest, project)


def test_preregistered_count_comparable_campaign_rejects_unknown_reference(tmp_path):
    manifest, project, _ = _formal_project(tmp_path)
    manifest["require_ci_count_comparability"] = True
    with pytest.raises(ValueError, match="count comparability"):
        campaign.requirements_preflight(manifest, project)
