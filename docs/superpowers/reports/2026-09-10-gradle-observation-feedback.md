# Gradle XML observations at the tool boundary

The first full and legacy FreeMarker attempts each produced 1529 XML records
(1526 passed, 3 skipped, no red) in one Gradle invocation. The sealed report
retained those counts, but the Agent received no test_stats/report_test_counts
and a message that a test task ran without captured results. The recording
path already parses and hashes the complete current report delta; its returned
count metadata was explicitly limited to Maven.

The separate candidate returns those existing complete totals for Gradle too.
It changes no receipt schema or stored receipt fields and does not promote an
identity sample to a total. The Gradle tool uses the counts for its observation
and labels their invocation XML basis; console totals remain separate diagnostic
metadata. Failures, errors and skips are distinct. A failed or timed-out command
keeps its outcome when current reports exist. Unparseable or absent reports do
not become zero. Cached reports are described as claimed by the invocation,
not written or executed again.

The success summary presents the command outcome, observed task labels, actual
count basis and full output reference. Task names alone no longer claim
compilation or test success. The existing compile-NO-SOURCE and final evidence
checks remain. The Advisor digest added earlier can consume the same metadata
without another Gradle-specific path.

Nine new regression cases include the real XML parser/publication/Gradle tool
path, custom task names, failures versus errors, cached claims, missing or bad
XML, and completed-failure/detached/timeout consumers. The first harness run
tried to bind two container stores to one evidence authority and correctly
failed; the consumer tests now reuse the original store, without bypassing the
authority check.

Final complete validation: **7617 passed, 32 skipped, 44 warnings**, 120.93 seconds.
Earlier focused and full logs remain archived separately. No formal v8 arm was
edited and no project result was replaced. Real candidate project regressions
remain pending the frozen cohort.

This feedback repair does not solve FreeMarker's missing GraalVM nativeCompile
and binary execution. Both earlier successful JVM invocations covered only the
first of the fixed task's two steps. That task-completeness gap is recorded
separately and must not be hidden by better test feedback.
