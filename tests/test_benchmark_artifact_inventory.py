"""Comparable observations cannot turn stale or malformed outputs into success."""
from copy import deepcopy
from pathlib import Path
import zipfile

import pytest

from sag.benchmark.artifact_inventory import capture, changed_outputs, inventory_plan


def spec():
    return {"steps": [{"step_id":"s", "modules":[{"id":"parent","path":"."},{"id":"child","path":"child"}]}],
            "requirements":[{"step_id":"s","expectations":{"artifacts":[
                {"path":"child/target/main.jar","role":"main"},
                {"path":"child/target/main-sources.jar","classifier":"sources"}]}}]}


def output(root, path, content=b'\xca\xfe\xba\xbe\x00\x00\x00\x3d'):
    target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
    return target


def snap(root, plan=None, boundary="before", run_id="run"):
    return capture(root, plan or inventory_plan(spec()), run_id=run_id, boundary=boundary, invocation_id="inv")


def test_parent_child_scope_and_test_main_outputs_do_not_double_count(tmp_path):
    output(tmp_path,"target/classes/Outer.class")
    output(tmp_path,"target/classes/Outer$Inner.class")
    output(tmp_path,"child/target/classes/Child.class")
    output(tmp_path,"child/target/test-classes/ChildTest.class")
    output(tmp_path,".m2/repository/Unrelated.class")
    inv=snap(tmp_path)
    assert inv["counts"] == {"class:main":3,"class:test":1}
    assert next(r for r in inv["files"] if r["path"].endswith("Child.class"))["module"] == "child"


def test_jar_classifiers_and_internal_classes_are_separate(tmp_path):
    for name in ["main.jar","main-sources.jar","dependency.jar"]:
        path=tmp_path/"child/target"/name;path.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(path,"w") as z:z.writestr("Inside.class",b"bytecode")
    inv=snap(tmp_path)
    assert inv["counts"] == {"jar:main":1,"jar:sources":1,"jar:unclassified":1}


def test_stale_files_are_not_counted_as_this_invocations_new_outputs(tmp_path):
    old=output(tmp_path,"target/classes/Old.class")
    before=snap(tmp_path)
    output(tmp_path,"target/test-classes/New.class")
    after=snap(tmp_path,boundary="after")
    assert [r["path"] for r in changed_outputs(before,after)] == ["target/test-classes/New.class"]
    old.unlink();output(tmp_path,"target/classes/Old.class")
    recreated=snap(tmp_path,boundary="after")
    assert "target/classes/Old.class" in [r["path"] for r in changed_outputs(after,recreated)]


def test_malformed_class_and_jar_do_not_enter_valid_counts(tmp_path):
    output(tmp_path,"target/classes/Fake.class",b"not a class")
    output(tmp_path,"target/broken.jar",b"not a zip")
    inv=snap(tmp_path)
    assert inv["counts"] == {"class:invalid":1,"jar:invalid":1}


def test_symlink_and_missing_checkout_are_not_observed_empty_success(tmp_path):
    root=tmp_path/"root";root.mkdir()
    (root/"target").symlink_to(tmp_path/"elsewhere")
    partial=snap(root)
    assert partial["status"] == "partial" and partial["errors"]
    missing=snap(tmp_path/"absent")
    assert missing["status"] == "unavailable" and missing["counts"] is None
    assert changed_outputs(partial,partial) is None


def test_different_producer_cannot_supply_a_freshness_baseline(tmp_path):
    before=snap(tmp_path)
    after=snap(tmp_path,run_id="other")
    with pytest.raises(ValueError,match="producer scopes"):
        changed_outputs(before,after)


def test_custom_gradle_source_set_is_disclosed_unclassified(tmp_path):
    output(tmp_path,"build/classes/java/main/App.class")
    output(tmp_path,"build/classes/java/integrationTest/Other.class")
    inv=snap(tmp_path)
    assert inv["counts"] == {"class:main":1,"class:unclassified":1}


def test_inventory_does_not_write_or_mutate_definitions(tmp_path):
    definition=spec();original=deepcopy(definition)
    paths=set(tmp_path.rglob("*"));snap(tmp_path,inventory_plan(definition))
    assert definition == original and set(tmp_path.rglob("*")) == paths


def test_reviewed_archive_role_vocabulary_is_preserved(tmp_path):
    definition=spec()
    artifacts=definition['requirements'][0]['expectations']['artifacts']
    artifacts.extend([{'path':'target/app.jar','role':'main_archive'},
                      {'path':'target/app-test-sources.jar','role':'test_source_archive'}])
    for name in ['app.jar','app-test-sources.jar']:
        path=tmp_path/'target'/name;path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path,'w') as z:z.writestr('entry','fixture')
    result=snap(tmp_path,inventory_plan(definition))
    assert result['counts'] == {'jar:main':1,'jar:test_sources':1}


def test_missing_declared_module_scope_is_unknown_not_zero(tmp_path):
    definition = spec()
    del definition['steps'][0]['modules']
    output(tmp_path, 'target/classes/Present.class')
    result = snap(tmp_path, inventory_plan(definition))
    assert result['status'] == 'unavailable'
    assert result['counts'] is None
    assert 'not declared for: s' in result['errors'][0]['reason']
