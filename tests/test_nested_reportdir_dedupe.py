# tests/test_nested_reportdir_dedupe.py
"""Regression for Bug B (live bigtop run): nested Gradle module report dirs.

scan_modules lists report_subdirs = ["build/test-results/test",
"build/test-results"] and BOTH exist in a modern Gradle layout (the second is
the *parent* of the first). parse_module_test_reports then iterated over both
dirs and `find`-ed the SAME TEST-*.xml files twice, double-counting every
module: the live per-module breakdown showed 72/2/26 (= 2x36, 2x1, 2x13) while
the honest reactor header said 50 executed.

The fix dedupes at the XML-file level: file paths are resolved from ALL report
dirs and deduped by absolute path before parsing, so a nested (ancestor +
descendant) dir pair can never double-count. This must NOT regress the
non-nested Maven surefire+failsafe case (distinct files across two sibling
dirs must both count), and it must still count XMLs that live directly in
build/test-results with no test/ subdir (older Gradle layout).
"""

from sag.agent.physical_validator import PhysicalValidator


class DirTreeOrch:
    """Fake orchestrator backed by a {report_dir -> [absolute xml paths]} map.

    A `find <rd> ...` returns EVERY xml at or below <rd> -- i.e. a parent dir
    yields its own files PLUS all descendants' files, exactly as real `find`
    does. Each xml path maps to its own testsuite content, so double-parsing a
    file shows up as doubled counts.
    """

    def __init__(self, dir_files, file_content):
        # dir_files: {report_dir: [xml_abs_path, ...]} (files AT that dir only)
        # file_content: {xml_abs_path: xml_string}
        self.dir_files = dir_files
        self.file_content = file_content

    def _descendant_files(self, rd):
        # Every file whose owning dir is rd or nested under rd/.
        out = []
        for d, files in self.dir_files.items():
            if d == rd or d.startswith(rd.rstrip("/") + "/"):
                out.extend(files)
        return out

    def execute_command(self, command, **kwargs):
        if command.startswith("cat "):
            for path, content in self.file_content.items():
                if f"'{path}'" in command or path in command:
                    return {"success": True, "exit_code": 0, "output": content}
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("find "):
            # Which report dir is being listed? Longest matching key wins so a
            # nested dir isn't shadowed by its parent.
            match = None
            for rd in sorted(self.dir_files, key=len, reverse=True):
                if f"find {rd} " in command:
                    match = rd
                    break
            if match is not None:
                files = self._descendant_files(match)
                return {"success": True, "exit_code": 0, "output": "\n".join(files)}
            return {"success": True, "exit_code": 0, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def _suite(tests, failures=0, errors=0, skipped=0):
    return (
        f'<testsuite tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}"></testsuite>'
    )


def test_nested_gradle_report_dirs_do_not_double_count():
    """The live bigtop shape: build/test-results/test holds the XMLs and
    build/test-results (its parent) is ALSO listed. The 36 tests of a module
    must count once (36), not twice (72)."""
    nested = "/w/m/build/test-results/test"
    parent = "/w/m/build/test-results"
    xml = f"{nested}/TEST-com.x.FooTest.xml"
    orch = DirTreeOrch(
        dir_files={nested: [xml], parent: []},  # files physically live in test/
        file_content={xml: _suite(36)},
    )
    v = PhysicalValidator(docker_orchestrator=orch)
    # scan_modules would hand BOTH dirs (descendant first, then ancestor).
    res = v.parse_module_test_reports("/w/m", [nested, parent])
    assert res["tests_total"] == 36, res["tests_total"]


def test_nested_gradle_dirs_counts_36_1_13_not_72_2_26():
    """Exact live bigtop numbers per module: 36/1/13 once, never 72/2/26."""
    v = lambda: None  # noqa: E731 (placeholder, replaced below)

    def count(n):
        nested = "/w/m/build/test-results/test"
        parent = "/w/m/build/test-results"
        xml = f"{nested}/TEST-suite.xml"
        orch = DirTreeOrch(
            dir_files={nested: [xml], parent: []},
            file_content={xml: _suite(n)},
        )
        pv = PhysicalValidator(docker_orchestrator=orch)
        return pv.parse_module_test_reports("/w/m", [nested, parent])["tests_total"]

    assert count(36) == 36   # bigpetstore-data-generator (was 72)
    assert count(1) == 1     # bigtop-name-generator     (was 2)
    assert count(13) == 13   # bigtop-samplers           (was 26)


def test_maven_surefire_plus_failsafe_non_nested_still_sums_both():
    """Regression guard: two SIBLING (non-nested) dirs with DISTINCT files must
    both count. Dedupe is by absolute path, so distinct files are never merged.
    """
    surefire = "/w/m/target/surefire-reports"
    failsafe = "/w/m/target/failsafe-reports"
    ut = f"{surefire}/TEST-com.x.UnitTest.xml"
    it = f"{failsafe}/TEST-com.x.IT.xml"
    orch = DirTreeOrch(
        dir_files={surefire: [ut], failsafe: [it]},
        file_content={ut: _suite(10), it: _suite(4)},
    )
    v = PhysicalValidator(docker_orchestrator=orch)
    res = v.parse_module_test_reports("/w/m", [surefire, failsafe])
    assert res["tests_total"] == 14  # 10 + 4, both counted


def test_gradle_xmls_directly_in_build_test_results_no_test_subdir():
    """Older Gradle layout: XMLs live DIRECTLY in build/test-results with no
    test/ subdir. Only build/test-results exists as a report dir, and its files
    must still be counted (dedupe must not drop lone-parent files)."""
    parent = "/w/m/build/test-results"
    xml = f"{parent}/TEST-com.x.OldTest.xml"
    orch = DirTreeOrch(
        dir_files={parent: [xml]},
        file_content={xml: _suite(7)},
    )
    v = PhysicalValidator(docker_orchestrator=orch)
    # Only the parent dir is present in report_dirs (test/ didn't exist).
    res = v.parse_module_test_reports("/w/m", [parent])
    assert res["tests_total"] == 7
