# tests/test_mixed_build_module_metrics.py
"""Mixed-build-system module rows (live bigtop repro).

Live evidence: a Maven-rooted project whose tests live in a Gradle subtree
(bigtop-data-generators; the analyzer's test recommendation records
test_system='gradle' there). All 50 tests ran and passed, but the modules line
read "1 built / 2 detected · 0 tested / 2 not tested" because scan_modules ran
with the MAVEN globs only — the Gradle test cluster's build/test-results
modules were invisible.

The fix: when marker files for BOTH maven and gradle exist under the project,
run scan_modules for EACH system and merge the module lists (path-keyed union;
richer record wins). Single-system projects must stay byte-identical.
"""
import json

from sag.tools.module_metrics import assemble_module_metrics
from sag.tools.report_tool import ReportTool


GRADLE_SAMPLERS = "bigtop-data-generators/bigtop-datagenerators/datagen-samplers"
GRADLE_WEATHER = "bigtop-data-generators/bigtop-datagenerators/datagen-weatherman"


def _maven_scan():
    # What the live maven-glob scan saw: root built, one maven submodule inert.
    return [
        {"path": ".", "name": ".", "class_count": 120, "jar_count": 2,
         "report_dirs": [], "has_test_sources": False},
        {"path": "bigtop-tests", "name": "bigtop-tests", "class_count": 0,
         "jar_count": 0, "report_dirs": [], "has_test_sources": False},
    ]


def _gradle_scan():
    # The invisible Gradle test cluster: built classes + build/test-results.
    return [
        {"path": ".", "name": ".", "class_count": 0, "jar_count": 0,
         "report_dirs": [], "has_test_sources": False},
        {"path": GRADLE_SAMPLERS, "name": GRADLE_SAMPLERS.replace("/", ":"),
         "class_count": 40, "jar_count": 1,
         "report_dirs": [f"/workspace/bigtop/{GRADLE_SAMPLERS}/build/test-results/test"],
         "has_test_sources": True},
        {"path": GRADLE_WEATHER, "name": GRADLE_WEATHER.replace("/", ":"),
         "class_count": 25, "jar_count": 1,
         "report_dirs": [f"/workspace/bigtop/{GRADLE_WEATHER}/build/test-results/test"],
         "has_test_sources": True},
    ]


class BigtopValidator:
    """Bigtop-shaped fixture: maven root + gradle test subtree, both present."""

    def __init__(self):
        self.scan_calls = []

    def _detect_build_system(self, project_dir):
        return "maven"  # root pom.xml wins, exactly like live

    def detect_java_build_systems(self, project_dir):
        return ["maven", "gradle"]

    def scan_modules(self, project_dir, build_system):
        self.scan_calls.append(build_system)
        return _maven_scan() if build_system == "maven" else _gradle_scan()

    def parse_module_test_reports(self, module_dir, report_dirs):
        if not report_dirs:
            return {}
        total = 30 if "samplers" in report_dirs[0] else 20
        return {"tests_total": total, "tests_passed": total, "tests_failed": 0,
                "tests_errors": 0, "tests_skipped": 0,
                "failing_names": [], "failing_count": 0,
                "evidence_refs": report_dirs}


def test_bigtop_mixed_layout_counts_gradle_test_cluster(monkeypatch):
    """Repro of the live false line: with maven globs only the summary said
    '0 tested / 2 not tested' while all 50 gradle tests had passed. The merged
    scan must surface the gradle modules as tested."""
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/bigtop", "build_system": "Maven"})
    validator = BigtopValidator()
    tool.physical_validator = validator

    metrics = tool._build_module_metrics({}, generated_at="t")

    assert sorted(validator.scan_calls) == ["gradle", "maven"]  # both scans ran
    by_path = {m["path"]: m for m in metrics["modules"]}
    # gradle test cluster present, honest, and tested
    assert by_path[GRADLE_SAMPLERS]["tests_total"] == 30
    assert by_path[GRADLE_WEATHER]["tests_total"] == 20
    assert by_path[GRADLE_SAMPLERS]["build_status"] == "success"  # artifacts
    assert by_path[GRADLE_SAMPLERS]["has_test_sources"] is True
    s = metrics["module_summary"]
    assert s["modules_total"] == 4          # 2 maven-scanned + 2 gradle-only
    assert s["modules_tested"] == 2         # the false "0 tested" line is gone
    assert s["modules_not_tested"] == 2
    assert s["build_systems"] == ["maven", "gradle"]
    # root "." found by both scans keeps the richer (maven, 120-class) record
    assert by_path["."]["class_count"] == 120


def test_mixed_layout_gradle_rows_survive_maven_reactor(monkeypatch):
    """Reactor-authoritative path stays authoritative for MAVEN rows only: a
    live reactor summary must not drop the gradle cluster (its modules never
    appear in a maven reactor), nor mark it skipped off a maven failure."""
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/bigtop", "build_system": "Maven"})
    tool.physical_validator = BigtopValidator()

    test_history = {
        "reactor_records": [
            {"module": ".", "status": "success"},
            {"module": "bigtop-tests", "status": "failure"},
        ],
    }
    metrics = tool._build_module_metrics(test_history, generated_at="t")
    by_path = {m["path"]: m for m in metrics["modules"]}
    # maven rows: reactor-sourced, authoritative
    assert by_path["bigtop-tests"]["build_status"] == "failure"
    assert by_path["bigtop-tests"]["build_source"] == "reactor"
    # gradle rows survive the reactor filter with artifact-based status
    assert by_path[GRADLE_SAMPLERS]["build_status"] == "success"
    assert by_path[GRADLE_SAMPLERS]["build_source"] == "artifacts"
    assert metrics["module_summary"]["modules_tested"] == 2


def test_mixed_layout_gradle_rows_exempt_from_active_maven_narrowing(monkeypatch):
    """Without a reactor summary, maven rows are narrowed to the root pom's
    active <modules>. The gradle subtree is never declared there — it must be
    exempt from that narrowing, not silently dropped."""
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/bigtop", "build_system": "Maven"})

    class V(BigtopValidator):
        def _active_maven_module_dirs(self, project_dir):
            return [project_dir]  # root only: bigtop-tests is not active

    tool.physical_validator = V()
    metrics = tool._build_module_metrics({}, generated_at="t")
    paths = {m["path"] for m in metrics["modules"]}
    assert GRADLE_SAMPLERS in paths and GRADLE_WEATHER in paths
    assert "bigtop-tests" not in paths  # inactive maven module still narrowed
    assert metrics["module_summary"]["modules_tested"] == 2


def test_assemble_gradle_tagged_rows_bypass_reactor_drop_and_skip_inference():
    """assemble-level guarantee: rows tagged scan_build_system='gradle' are not
    dropped by the authoritative reactor filter, and a maven reactor failure
    must not infer 'skipped' for a gradle row (the reactor never saw it)."""
    metrics = assemble_module_metrics(
        modules=[
            {"path": ".", "name": ".", "class_count": 10, "jar_count": 1,
             "report_dirs": [], "scan_build_system": "maven"},
            {"path": "gsub", "name": "gsub", "class_count": 5, "jar_count": 0,
             "report_dirs": ["/w/gsub/build/test-results/test"],
             "has_test_sources": True, "scan_build_system": "gradle"},
            {"path": "gempty", "name": "gempty", "class_count": 0, "jar_count": 0,
             "report_dirs": [], "scan_build_system": "gradle"},
            # untagged stray dir not in the reactor: still dropped (regression)
            {"path": "stray", "name": "stray", "class_count": 0, "jar_count": 0,
             "report_dirs": []},
        ],
        reactor_status={".": "failure"},
        tests={"gsub": {"tests_total": 7, "tests_passed": 7, "tests_failed": 0,
                        "tests_errors": 0, "tests_skipped": 0,
                        "failing_names": [], "failing_count": 0,
                        "evidence_refs": ["/w/gsub/build/test-results/test"]}},
        build_systems=["maven", "gradle"],
        build_error_samples={},
        generated_at="t",
    )
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert "stray" not in by_path                      # reactor still authoritative
    assert by_path["gsub"]["build_status"] == "success"
    assert by_path["gsub"]["tests_total"] == 7
    # maven failure does not cascade "skipped" onto a gradle row
    assert by_path["gempty"]["build_status"] == "unknown"
    assert by_path["gempty"]["build_source"] == "none"
    assert metrics["module_summary"]["modules_tested"] == 1


DUAL = "dual-module"  # has pom.xml AND build.gradle; built by the maven reactor


class DualMarkerValidator:
    """A dir with BOTH markers: found by both scans, gradle record is richer
    (gradle ran the tests there -> report_dirs), and the maven reactor built it
    too. Root '.' is prepended to BOTH scans, exactly like scan_modules."""

    def _detect_build_system(self, project_dir):
        return "maven"

    def detect_java_build_systems(self, project_dir):
        return ["maven", "gradle"]

    def scan_modules(self, project_dir, build_system):
        if build_system == "maven":
            return [
                {"path": ".", "name": ".", "class_count": 120, "jar_count": 2,
                 "report_dirs": [], "has_test_sources": False},
                {"path": DUAL, "name": DUAL, "class_count": 10, "jar_count": 1,
                 "report_dirs": [], "has_test_sources": True},
            ]
        return [
            {"path": ".", "name": ".", "class_count": 0, "jar_count": 0,
             "report_dirs": [], "has_test_sources": False},
            {"path": DUAL, "name": DUAL, "class_count": 10, "jar_count": 1,
             "report_dirs": [f"/workspace/p/{DUAL}/build/test-results/test"],
             "has_test_sources": True},
        ]

    def parse_module_test_reports(self, module_dir, report_dirs):
        if not report_dirs:
            return {}
        return {"tests_total": 12, "tests_passed": 12, "tests_failed": 0,
                "tests_errors": 0, "tests_skipped": 0, "failing_names": [],
                "failing_count": 0, "evidence_refs": report_dirs}


def test_dual_marker_module_not_double_counted_and_reactor_stays_authoritative(
    monkeypatch,
):
    """PoC repro: a module found by BOTH scans where the gradle record wins
    richness was tagged 'gradle' -> reactor-exempt -> its maven reactor entry
    went unmatched -> the fallback appended a phantom second row, AND the
    surviving row reported artifact-inferred success while the reactor said
    FAILURE. One physical module must yield ONE row with the reactor verdict,
    keeping the gradle test evidence on that same row."""
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/p", "build_system": "Maven"})
    tool.physical_validator = DualMarkerValidator()

    metrics = tool._build_module_metrics({
        "reactor_records": [
            {"module": ".", "status": "success"},
            {"module": DUAL, "status": "failure"},  # maven says it FAILED
        ],
    }, generated_at="t")

    dual_rows = [m for m in metrics["modules"]
                 if m["name"] == DUAL or m["path"] == DUAL]
    assert len(dual_rows) == 1  # no phantom reactor fallback row
    row = dual_rows[0]
    assert row["build_status"] == "failure"   # reactor verdict wins
    assert row["build_source"] == "reactor"
    assert row["tests_total"] == 12           # gradle test evidence kept
    assert metrics["module_summary"]["modules_total"] == 2
    assert metrics["module_summary"]["modules_failed"] == 1
    # merge-internal tags never leak into the emitted artifact
    assert "scan_found_by_both" not in json.dumps(metrics)
    assert "scan_build_system" not in json.dumps(metrics)


def test_aggregator_root_collision_keeps_reactor_verdict(monkeypatch):
    """Root variant: '.' is in BOTH scans unconditionally. An aggregator-pom
    maven root (0 classes / 0 jars, richness (0,0,0,0)) loses to a gradle root
    with test-results -> root flipped to gradle-tagged, the reactor's '.' entry
    went unmatched (phantom row) and its 'success' verdict was ignored."""
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/p", "build_system": "Maven"})

    class RootV(DualMarkerValidator):
        def scan_modules(self, project_dir, build_system):
            if build_system == "maven":
                return [
                    {"path": ".", "name": ".", "class_count": 0, "jar_count": 0,
                     "report_dirs": [], "has_test_sources": False},  # aggregator
                    {"path": "core", "name": "core", "class_count": 50,
                     "jar_count": 1, "report_dirs": [], "has_test_sources": False},
                ]
            return [
                {"path": ".", "name": ".", "class_count": 0, "jar_count": 0,
                 "report_dirs": ["/workspace/p/build/test-results/test"],
                 "has_test_sources": True},  # gradle tests at root
            ]

    tool.physical_validator = RootV()
    metrics = tool._build_module_metrics({
        "reactor_records": [
            {"module": ".", "status": "success"},
            {"module": "core", "status": "success"},
        ],
    }, generated_at="t")

    root_rows = [m for m in metrics["modules"]
                 if m["name"] == "." or m["path"] == "."]
    assert len(root_rows) == 1
    assert root_rows[0]["build_status"] == "success"  # reactor verdict, not unknown
    assert root_rows[0]["build_source"] == "reactor"
    assert root_rows[0]["tests_total"] == 12          # gradle root tests kept
    assert metrics["module_summary"]["modules_total"] == 2


def test_assemble_both_found_gradle_row_outside_reactor_survives():
    """A collision row (found by both scans, gradle record won) whose module is
    NOT in the captured reactor must not be dropped by the authoritative-reactor
    filter: maven does not speak for it, but the gradle evidence does."""
    metrics = assemble_module_metrics(
        modules=[
            {"path": ".", "name": ".", "class_count": 10, "jar_count": 1,
             "report_dirs": [], "scan_build_system": "maven",
             "scan_found_by_both": True},
            {"path": "dual", "name": "dual", "class_count": 5, "jar_count": 1,
             "report_dirs": ["/w/dual/build/test-results/test"],
             "has_test_sources": True, "scan_build_system": "gradle",
             "scan_found_by_both": True},
        ],
        reactor_status={".": "failure"},  # partial reactor: 'dual' not in it
        tests={"dual": {"tests_total": 4, "tests_passed": 4, "tests_failed": 0,
                        "tests_errors": 0, "tests_skipped": 0,
                        "failing_names": [], "failing_count": 0,
                        "evidence_refs": ["/w/dual/build/test-results/test"]}},
        build_systems=["maven", "gradle"],
        build_error_samples={},
        generated_at="t",
    )
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert "dual" in by_path                    # not dropped, not phantom-duplicated
    assert by_path["dual"]["build_status"] == "success"   # artifacts, not "skipped"
    assert by_path["dual"]["build_source"] == "artifacts"
    assert by_path["dual"]["tests_total"] == 4
    assert metrics["module_summary"]["modules_total"] == 2


def _single_system_validator(system, scan, *, with_probe):
    class V:
        def __init__(self):
            self.scan_calls = []

        def _detect_build_system(self, project_dir):
            return system

        def scan_modules(self, project_dir, build_system):
            self.scan_calls.append(build_system)
            return [dict(m) for m in scan]

        def parse_module_test_reports(self, module_dir, report_dirs):
            if report_dirs:
                return {"tests_total": 9, "tests_passed": 9, "tests_failed": 0,
                        "tests_errors": 0, "tests_skipped": 0,
                        "failing_names": [], "failing_count": 0,
                        "evidence_refs": report_dirs}
            return {}

    if with_probe:
        V.detect_java_build_systems = lambda self, project_dir: [system]
    return V()


def _metrics_for(validator, monkeypatch_target):
    tool = ReportTool()
    tool._get_project_info = lambda: {
        "directory": "/workspace/p", "build_system": "Unknown"}
    tool.physical_validator = validator
    return tool._build_module_metrics({}, generated_at="t")


def test_pure_maven_project_metrics_byte_identical(monkeypatch):
    scan = [
        {"path": ".", "name": ".", "class_count": 33, "jar_count": 1,
         "report_dirs": ["/workspace/p/target/surefire-reports"],
         "has_test_sources": True},
        {"path": "core", "name": "core", "class_count": 12, "jar_count": 1,
         "report_dirs": [], "has_test_sources": False},
    ]
    probed = _single_system_validator("maven", scan, with_probe=True)
    legacy = _single_system_validator("maven", scan, with_probe=False)
    a = _metrics_for(probed, monkeypatch)
    b = _metrics_for(legacy, monkeypatch)
    assert probed.scan_calls == ["maven"]  # single scan, single system
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert "scan_build_system" not in json.dumps(a)  # no tag leaks


def test_pure_gradle_project_metrics_byte_identical(monkeypatch):
    scan = [
        {"path": "caffeine", "name": "caffeine", "class_count": 200, "jar_count": 1,
         "report_dirs": ["/workspace/p/caffeine/build/test-results/test"],
         "has_test_sources": True},
        {"path": "guava", "name": "guava", "class_count": 30, "jar_count": 1,
         "report_dirs": [], "has_test_sources": False},
    ]
    probed = _single_system_validator("gradle", scan, with_probe=True)
    legacy = _single_system_validator("gradle", scan, with_probe=False)
    a = _metrics_for(probed, monkeypatch)
    b = _metrics_for(legacy, monkeypatch)
    assert probed.scan_calls == ["gradle"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["module_summary"]["build_systems"] == ["gradle"]


def test_secondary_scan_failure_degrades_to_primary_rows(monkeypatch):
    """The mixed merge is additive: a failing secondary scan must never break
    the primary path (bounded degradation, no hard block)."""
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/bigtop", "build_system": "Maven"})

    class V(BigtopValidator):
        def scan_modules(self, project_dir, build_system):
            if build_system == "gradle":
                raise RuntimeError("gradle scan exploded")
            return super().scan_modules(project_dir, build_system)

    tool.physical_validator = V()
    metrics = tool._build_module_metrics({}, generated_at="t")
    assert metrics is not None
    assert {m["path"] for m in metrics["modules"]} == {".", "bigtop-tests"}


def test_detect_java_build_systems_probes_root_and_subtree():
    """Validator-level probe: root pom + gradle files only at depth -> both."""
    from sag.agent.physical_validator import PhysicalValidator

    class Orch:
        def execute_command(self, command, **kwargs):
            if "test -f /w/bigtop/pom.xml" in command:
                return {"success": True, "exit_code": 0, "output": "EXISTS"}
            if "build.gradle" in command:  # root test fails, find fallback hits
                return {"success": True, "exit_code": 0,
                        "output": "/w/bigtop/bigtop-data-generators/build.gradle"}
            return {"success": True, "exit_code": 0, "output": ""}

    v = PhysicalValidator(docker_orchestrator=Orch())
    assert v.detect_java_build_systems("/w/bigtop") == ["maven", "gradle"]


def test_detect_java_build_systems_single_system():
    from sag.agent.physical_validator import PhysicalValidator

    class Orch:
        def execute_command(self, command, **kwargs):
            if "test -f /w/solo/pom.xml" in command:
                return {"success": True, "exit_code": 0, "output": "EXISTS"}
            return {"success": True, "exit_code": 0, "output": ""}

    v = PhysicalValidator(docker_orchestrator=Orch())
    assert v.detect_java_build_systems("/w/solo") == ["maven"]
