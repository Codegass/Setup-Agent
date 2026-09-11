"""Multi-module Jenkins IDs retain module identity through live certificates."""

import shlex

import pytest

from test_ci_comparison import ROOT, XML, compare, setup_run, target_with


def fixture_run(monkeypatch):
    reports = {
        ROOT + "/" + module + "/target/surefire-reports/TEST-a.xml": XML for module in ("a", "b")
    }
    run = setup_run(
        report_files=reports,
        modules=[
            {"module": "Module A", "status": "success"},
            {"module": "Module B", "status": "success"},
        ],
    )
    run.fs.files[ROOT + "/a/pom.xml"] = (
        "<project><groupId>g</groupId><artifactId>a</artifactId></project>"
    )
    run.fs.files[ROOT + "/b/pom.xml"] = (
        "<project><parent><groupId>g</groupId></parent><artifactId>b</artifactId></project>"
    )
    execute = run.fs.execute_command

    def source_transport(command, **kwargs):
        if "ls-files --error-unmatch --" in command:
            relative = shlex.split(command)[-1]
            exists = ROOT + "/" + relative in run.fs.files
            return {
                "success": exists,
                "exit_code": 0 if exists else 1,
                "output": relative if exists else "",
            }
        return execute(command, **kwargs)

    monkeypatch.setattr(run.fs, "execute_command", source_transport)
    target = target_with(
        run,
        modules=["Module A", "Module B"],
        executed_count=2,
        executed_ids=["g$a::a.T#one", "g$b::a.T#one"],
    )
    return run, target


def test_identical_class_and_name_in_distinct_modules_stay_distinct(monkeypatch):
    run, target = fixture_run(monkeypatch)
    result = compare(run, target=target)
    assert result.certificate.test_counts.reported == 2
    assert result.attainment.verdict == "met", result
    assert result.test_identity_basis == "jenkins_module_pom_coordinates"


@pytest.mark.parametrize(
    "change",
    [
        "missing_pom",
        "changed_source",
        "property_group",
        "duplicate_coordinate",
        "wrong_namespace",
        "partial_ci_pool",
        "wrong_test_name",
        "deleted_report",
        "duplicate_artifact",
    ],
)
def test_missing_or_ambiguous_module_proof_cannot_fall_back_to_equal_counts(monkeypatch, change):
    run, target = fixture_run(monkeypatch)
    path = ROOT + "/b/pom.xml"
    if change == "missing_pom":
        del run.fs.files[path]
    elif change == "changed_source":
        run.fs.dirty = True
    elif change == "property_group":
        run.fs.files[path] = run.fs.files[path].replace(
            "<groupId>g</groupId>", "<groupId>${group}</groupId>"
        )
    elif change == "duplicate_coordinate":
        run.fs.files[path] = run.fs.files[path].replace(
            "<artifactId>b</artifactId>", "<artifactId>a</artifactId>"
        )
    elif change == "duplicate_artifact":
        run.fs.files[path] = run.fs.files[path].replace(
            "</project>", "<artifactId>other</artifactId></project>"
        )
    elif change == "deleted_report":
        del run.fs.files[ROOT + "/b/target/surefire-reports/TEST-a.xml"]
    else:
        identities = {
            "wrong_namespace": ["g$a::a.T#one", "other$b::a.T#one"],
            "partial_ci_pool": ["g$a::a.T#one"],
            "wrong_test_name": ["g$a::a.T#one", "g$b::a.T#other"],
        }[change]
        if change == "partial_ci_pool":
            with pytest.raises(ValueError, match="executed count does not match"):
                target_with(
                    run, modules=["Module A", "Module B"], executed_count=2, executed_ids=identities
                )
            return
        target = target_with(
            run, modules=["Module A", "Module B"], executed_count=2, executed_ids=identities
        )
    result = compare(run, target=target)
    assert result.attainment.alpha is None, result
    assert result.attainment.verdict != "met"
