"""A spelling alias is earned by a frozen execution binding, not string stripping."""
import hashlib
import pytest

from sag.benchmark.evaluator import _matching_native_events
from sag.benchmark.native_evidence import maven_events

BINDING = dict(module="demo", execution="check", goal="check", prefix="spotbugs",
               artifact="spotbugs-maven-plugin", coordinates="com.github.spotbugs:spotbugs-maven-plugin",
               version="4.8.6.8", source={"path": "effective-pom.xml", "sha256": "a" * 64, "bytes": 1})
ROW = {"module": "demo", "validation": {"goals": ["spotbugs:check"],
       "native_bindings": [{"goal": "spotbugs:check", "execution": "check", "occurrence": 0}]}}


def parse(plugin="spotbugs-maven-plugin", version="4.8.6.8", module="demo", tail="[INFO] BUILD SUCCESS\n",
          bindings=None, **kwargs):
    return maven_events(f"[INFO] --- {plugin}:{version}:check (check) @ {module} ---\n" + tail,
                        terminal=kwargs.get("terminal", True), serial=kwargs.get("serial", True),
                        plugin_bindings=[BINDING] if bindings is None else bindings)


@pytest.mark.parametrize("spelling", ["spotbugs", "spotbugs-maven-plugin", "com.github.spotbugs:spotbugs-maven-plugin"])
def test_both_maven_banner_styles_bind_to_same_execution(spelling):
    events = parse(plugin=spelling)
    _, matched, complete = _matching_native_events(ROW, events)
    assert complete and matched[0]["status"] == "passed"
    assert matched[0]["plugin_identity"] == BINDING["coordinates"]


@pytest.mark.parametrize("change", [
    {"plugin": "wrong.group:spotbugs-maven-plugin"}, {"module": "another-module"},
    {"version": "4.0.0"}, {"plugin": "unrelated-maven-plugin"},
    {"bindings": [BINDING, {**BINDING, "coordinates": "ambiguous:spotbugs-maven-plugin"}]},
    {"bindings": []},
])
def test_wrong_or_ambiguous_identity_does_not_satisfy_requirement(change):
    _, matched, complete = _matching_native_events(ROW, parse(**change))
    assert not complete and not matched


@pytest.mark.parametrize("kwargs, state", [
    ({"tail": ""}, "unavailable"),
    ({"terminal": False}, "unavailable"),
    ({"serial": False}, "unavailable"),
    ({"tail": "[INFO] Skipping SpotBugs execution.\n[INFO] BUILD SUCCESS\n"}, "not_run"),
    ({"tail": "[ERROR] Failed to execute goal com.github.spotbugs:spotbugs-maven-plugin:4.8.6.8:check (check) on project demo: defects\n[INFO] BUILD FAILURE\n"}, "failed"),
])
def test_alias_never_manufactures_success_from_start_skip_or_failure(kwargs, state):
    assert parse(**kwargs)[0]["status"] == state


def test_wrong_group_failure_is_not_attributed_to_declared_plugin():
    event = parse(tail="[ERROR] Failed to execute goal other:spotbugs-maven-plugin:4.8.6.8:check (check) on project demo: error\n")[0]
    assert event["status"] == "unavailable"


def test_fail_fast_leaves_absent_module_absent():
    events = parse(tail="[ERROR] Failed to execute goal com.github.spotbugs:spotbugs-maven-plugin:4.8.6.8:check (check) on project demo: defects\n")
    assert events[0]["status"] == "failed"
    assert not _matching_native_events({**ROW, "module": "later"}, events)[1]


def test_alias_declaration_requires_unique_clean_byte_bound_model(tmp_path):
    from sag.benchmark.ci_sources import maven_plugin_bindings

    def save(name, text):
        p = tmp_path / name
        p.write_text(text)
        return dict(path=name, sha256=hashlib.sha256(p.read_bytes()).hexdigest(), bytes=p.stat().st_size)

    plugin = ('<plugin><groupId>com.github.spotbugs</groupId><artifactId>spotbugs-maven-plugin</artifactId>'
              '<version>4.8.6.8</version><executions><execution><id>check</id><goals><goal>check</goal>'
              '</goals></execution></executions></plugin>')
    def pom(plugins):
        return ('<project><groupId>demo</groupId><artifactId>demo</artifactId><version>1.0</version>'
                '<build><plugins>' + plugins + '</plugins></build></project>')
    sources = dict(head=save('HEAD', 'a' * 40), tracked_diff=save('diff', ''),
                   effective_pom=save('pom.xml', pom(plugin)))
    spec = dict(commit='a' * 40, requirements=[{**ROW, 'step_id': 'one'}],
                steps=[dict(step_id='one', modules=[dict(id='demo', path='.')])],
                reviewed_execution_plan=dict(sources=sources))
    bindings = maven_plugin_bindings(spec, tmp_path, 'one')
    assert len(bindings) == 1 and bindings[0]['coordinates'] == BINDING['coordinates']
    (tmp_path / 'pom.xml').write_text(pom(plugin.replace('com.github.spotbugs', 'wrong.group')))
    with pytest.raises(ValueError):
        maven_plugin_bindings(spec, tmp_path, 'one')
    sources['effective_pom'] = save('pom.xml', pom(plugin + plugin.replace('com.github.spotbugs', 'another.group')))
    assert maven_plugin_bindings(spec, tmp_path, 'one') == []
    sources['tracked_diff'] = save('diff', 'pom.xml\n')
    with pytest.raises(ValueError):
        maven_plugin_bindings(spec, tmp_path, 'one')
