"""Disabled Maven profiles are resolved from current execution evidence only."""

import pytest

from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import add_published_mutable_json
from test_ci_comparison import ROOT, SHA, setup_run

from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.java_versions import maven_java_requirements

POM = """<project><properties><maven.compiler.release>8</maven.compiler.release></properties>
<build><plugins><plugin><artifactId>maven-enforcer-plugin</artifactId><configuration>
<rules><requireJavaVersion><version>[17,)</version></requireJavaVersion></rules>
</configuration></plugin></plugins></build><profiles><profile><id>toolchain</id>
<activation><property><name>!manual.compiler</name></property></activation>
<build><plugins><plugin><artifactId>maven-toolchains-plugin</artifactId></plugin>
</plugins></build></profile></profiles></project>"""


def test_excluding_profile_recomputes_requirements_instead_of_erasing_a_warning():
    pom = POM.replace(
        "<artifactId>maven-toolchains-plugin</artifactId>",
        "<artifactId>maven-toolchains-plugin</artifactId><configuration>"
        "<requireJavaVersion><version>[21]</version></requireJavaVersion>"
        "</configuration>",
    )
    before = maven_java_requirements([(pom, "pom.xml")])
    after = maven_java_requirements([(pom, "pom.xml")], disabled_profiles=frozenset({"toolchain"}))
    assert before["compiler_toolchain"] is True
    assert before["unresolved"]
    assert [r["constraint"] for r in before["runtime"]] == ["[17,)", "[21]"]
    assert after["compiler_toolchain"] is False
    assert after["unresolved"] == []
    assert [r["constraint"] for r in after["runtime"]] == ["[17,)"]
    assert after["compiler_release"] == "8"


def fixture_run(monkeypatch, *, args="-P-toolchain", termination_reason=None):
    run = setup_run(
        plan_test_args=args, execute_test_args=args, termination_reason=termination_reason
    )
    run.fs.files[ROOT + "/pom.xml"] = POM
    requirements = maven_java_requirements([(POM, ROOT + "/pom.xml")])
    manifest = complete_build_requirements_v1(
        project_root=ROOT,
        target_sha=SHA,
        config_fingerprint="cfg-7",
        java_requirements=requirements,
        java_version_source="maven-enforcer",
        java_version_enforced=True,
    )
    add_published_mutable_json(
        run.fs,
        run.fs,
        path=REQUIREMENTS_PATH,
        record_kind="build_requirements",
        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        payload=manifest,
    )
    monkeypatch.setattr("sag.agent.physical_survey.config_fingerprint", lambda *a: "cfg-7")
    monkeypatch.setattr(
        "sag.tools.internal.build_preflight.active_java_runtime",
        lambda *a: {"major": "17", "version": "17.0.20"},
    )
    return run, manifest


@pytest.mark.parametrize(
    "args",
    [
        "-P-toolchain",
        "-P=-toolchain",
        "-P !toolchain",
        "--activate-profiles=-toolchain",
        "-P-toolchain,nodoclint",
    ],
)
def test_live_receipts_resolve_explicit_profile_deactivation(monkeypatch, args):
    run, manifest = fixture_run(monkeypatch, args=args)
    result = run.validator._java_requirements_for_invocation(manifest)
    assert result["unresolved"] == [], [
        {
            k: row.get(k)
            for k in ("receipt_id", "argv", "config_fingerprint", "target_sha", "outcome")
        }
        for row in run.validator._current_scoped_receipts(ROOT) or ()
    ]
    assert result["compiler_toolchain"] is False
    from sag.tools.internal.build_preflight import read_live_build_requirements

    observed = read_live_build_requirements(run.fs)
    assert observed.complete and observed.conflict is None, observed
    assert not run.validator._java_requirements_for_invocation(observed.payload)[
        "unresolved"
    ], observed.payload
    assert run.validator._collect_env_conflicts() == []
    # Derived, command-specific facts do not rewrite the surveyed requirements.
    assert manifest["java_requirements"]["unresolved"]


@pytest.mark.parametrize(
    "change",
    [
        "no_flag",
        "other_profile",
        "contradictory_flags",
        "missing_receipts",
        "tampered_receipt",
        "stale_config",
        "changed_pom",
        "extra_parent_constraint",
        "timeout",
        "missing_pin",
    ],
)
def test_removing_or_conflicting_profile_evidence_never_clears_unknown(monkeypatch, change):
    args = {
        "no_flag": None,
        "other_profile": "-P-other",
        "contradictory_flags": "-Ptoolchain,-toolchain",
    }.get(change, "-P-toolchain")
    run, manifest = fixture_run(
        monkeypatch, args=args, termination_reason="timeout" if change == "timeout" else None
    )
    if change in {"missing_receipts", "tampered_receipt"}:
        for path in list(run.fs.files):
            if "/invocation_receipts/" in path and path.endswith(".json"):
                if change == "missing_receipts":
                    del run.fs.files[path]
                else:
                    run.fs.files[path] += " "
    elif change == "stale_config":
        monkeypatch.setattr("sag.agent.physical_survey.config_fingerprint", lambda *a: "changed")
    elif change == "changed_pom":
        run.fs.files[ROOT + "/pom.xml"] = POM.replace("[17,)", "[21,)")
    elif change == "extra_parent_constraint":
        manifest["java_requirements"]["runtime"].append(
            {"constraint": "[21,)", "source": "parent/pom.xml"}
        )
    elif change == "missing_pin":
        manifest["survey"]["config_fingerprint"] = None
    assert (
        run.validator._java_requirements_for_invocation(manifest) == manifest["java_requirements"]
    )


def test_unconditional_java_mismatch_remains_after_disabled_profile(monkeypatch):
    run, _ = fixture_run(monkeypatch)
    monkeypatch.setattr(
        "sag.tools.internal.build_preflight.active_java_runtime",
        lambda *a: {"major": "11", "version": "11.0.20"},
    )
    assert run.validator._collect_env_conflicts() == ["jdk_mismatch"]
