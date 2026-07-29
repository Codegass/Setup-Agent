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


def test_only_cached_test_tasks_vouch_for_a_report_directory():
    """A cached `compileJava` says nothing about any test report."""
    dirs = _gradle_cached_report_dirs(KAFKA_TASKS, "/workspace/kafka")

    assert dirs == [
        "/workspace/kafka/api/build/test-results/test",
        "/workspace/kafka/json/build/test-results/test",
    ]


def test_a_task_that_actually_ran_is_not_a_cache_hit():
    """`:core:test` rewrote its own reports; it belongs in new/changed."""
    dirs = _gradle_cached_report_dirs(KAFKA_TASKS, "/workspace/kafka")

    assert not any("/core/" in directory for directory in dirs)


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
