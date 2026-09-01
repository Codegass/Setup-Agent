# tests/test_cached_report_claims.py
"""Plan 7 — a build-cache hit is evidence, and it was unclaimable.

kafka's campaign run (`logs/session_20260727_054716_94267`) dispatched one
`gradlew --build-cache test`. The receipt claimed 50 reports across 10 modules
— 546 tests — and 4,686 further passing tests sat in auxiliary. Nothing had
gone wrong: Gradle served most test tasks FROM-CACHE, so their report files
were never rewritten, the content hashes did not move, and `report_delta`
could claim none of them. The run observed 5,232 tests, 78% more than the old
benchmark's 2,937, and reported 546.

A cache hit is not a weaker fact than a write. Gradle states that the report
on disk IS this build's result for that task, which is a stronger guarantee
than a file merely existing next to a build. So those reports are claimable —
in their own bucket, so a reader can always separate what ran from what was
vouched for.
"""

from sag.agent.invocation_receipts import report_delta
from sag.tools.internal.gradle_tool import _gradle_cached_report_dirs

KAFKA_TASKS = """> Task :api:test FROM-CACHE
> Task :json:test UP-TO-DATE
> Task :core:test
> Task :core:compileJava FROM-CACHE
> Task :streams:test FAILED
"""
# The names no allowlist had (r2-T4). geode's `distributedTest` is measured —
# it is why the summary unit is the (project, task dir) pair — and a build
# whose suite runs under `smokeTest` or `verify-integration` is the ordinary
# case the old three-name list read as "not a test task at all".
CUSTOM_TASKS = """> Task :geode-core:distributedTest FROM-CACHE
> Task :payments:smokeTest UP-TO-DATE
> Task :payments:verify-integration FROM-CACHE
"""


def test_every_cached_task_vouches_for_its_own_task_dir_by_name():
    """No allowlist: the task dir is the task's name, taken verbatim.

    `("test", "integrationTest", "check")` was the whole list, so geode's
    cached `distributedTest` vouched for nothing and its reports could be
    claimed by no receipt. A hyphenated name did not even survive the task
    regex.
    """
    dirs = _gradle_cached_report_dirs(CUSTOM_TASKS, "/workspace/proj")

    assert dirs == [
        "/workspace/proj/geode-core/build/test-results/distributedTest",
        "/workspace/proj/payments/build/test-results/smokeTest",
        "/workspace/proj/payments/build/test-results/verify-integration",
    ]


def test_a_cached_non_test_task_vouches_for_a_directory_that_holds_nothing():
    """A cached `compileJava` still says nothing about any test report.

    It names `build/test-results/compileJava`, which no build has ever written
    into, so the root claims nothing — the DISK decides what a vouched
    directory is worth, not a list of names this engine keeps.
    """
    dirs = _gradle_cached_report_dirs(KAFKA_TASKS, "/workspace/kafka")

    assert dirs == [
        "/workspace/kafka/api/build/test-results/test",
        "/workspace/kafka/json/build/test-results/test",
        "/workspace/kafka/core/build/test-results/compileJava",
    ]
    assert report_delta(
        {"/workspace/kafka/core/build/test-results/test/TEST-a.xml": "aa"},
        {"/workspace/kafka/core/build/test-results/test/TEST-a.xml": "aa"},
        dirs,
    ) == {"new": [], "changed": []}


def test_a_task_that_actually_ran_is_not_a_cache_hit():
    """`:core:test` rewrote its own reports; it belongs in new/changed."""
    dirs = _gradle_cached_report_dirs(KAFKA_TASKS, "/workspace/kafka")

    assert "/workspace/kafka/core/build/test-results/test" not in dirs


def test_an_unchanged_report_under_a_vouched_directory_is_claimed():
    before = {"/w/api/build/test-results/test/TEST-a.xml": "aa"}
    after = {"/w/api/build/test-results/test/TEST-a.xml": "aa"}

    delta = report_delta(before, after, ["/w/api/build/test-results/test"])

    assert delta["new"] == []
    assert delta["changed"] == []
    assert delta["cached"] == [
        {"path": "/w/api/build/test-results/test/TEST-a.xml", "sha256": "aa"}
    ]


def test_an_unchanged_report_nobody_vouched_for_stays_unclaimed():
    """The Bigtop rule stands: an untouched file is not this run's evidence."""
    before = {"/w/other/build/test-results/test/TEST-b.xml": "bb"}
    after = {"/w/other/build/test-results/test/TEST-b.xml": "bb"}

    delta = report_delta(before, after, ["/w/api/build/test-results/test"])

    assert delta == {"new": [], "changed": []}
    assert "cached" not in delta


def test_written_reports_are_never_relabelled_as_cached():
    """What the dispatch physically wrote keeps its own bucket."""
    before = {}
    after = {"/w/api/build/test-results/test/TEST-a.xml": "aa"}

    delta = report_delta(before, after, ["/w/api/build/test-results/test"])

    assert delta["new"] == [
        {"path": "/w/api/build/test-results/test/TEST-a.xml", "sha256": "aa"}
    ]
    assert "cached" not in delta


def test_no_cache_hits_leaves_the_delta_shape_untouched():
    """Byte-compat: a run with no cached tasks writes exactly what it did."""
    delta = report_delta({}, {"/w/x.xml": "aa"}, None)

    assert delta == {"new": [{"path": "/w/x.xml", "sha256": "aa"}], "changed": []}
