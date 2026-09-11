# Maven feedback follow-up

The frozen v6 experiment completed Whisker with 155 passing test records and
JCS with 443. The Agent's Build observations reported 16 and 2 respectively:
the console parser retained the last module's summary. Both Agents repeated
that number in their Build claims. The sealed reports already used the correct
whole-run XML counts, so this is a feedback defect rather than a reason to
rewrite the historical scores.

The existing invocation producer now returns its validated XML execution
totals alongside a successfully persisted Maven receipt. Maven feedback uses
these counts and labels their source; console counts remain diagnostic
metadata. Missing or incomplete XML retains explicitly labelled console
diagnostics. This adds no report parser or evidence schema, changes no command,
and leaves failed, timed-out and conflicting outcomes intact. Repeated plugin
executions do not accumulate scores.

The HttpClient expansion also exposed a Java error label on a failed test run.
The error formatter previously treated any occurrence of RequireJavaVersion as
a failure, including a passed Enforcer rule. It also treated a caller-supplied
Maven requirement as a failed runtime observation. The formatter now requires
an actual failed rule or build-error requirement before emitting those version
diagnoses. Existing real Java and Maven mismatch tests remain enforced.

Validation: the full suite passed 7,529 tests with 32 skipped and one isolated
packaging download failure. That packaging test passed with network access.
Four additional passed-Enforcer/test-failure cases and the relevant regression
set passed together: 280 tests. The combined distinct coverage is 7,534 passed
and 32 skipped; this was not one all-green full-suite invocation. The tests
cover actual XML production, normal and detached feedback, malformed reports,
timeouts, nonzero exit codes, red XML behind green console summaries and
repeated plugin summaries. Producer and consumer paths are exercised together
through test fixtures; a fresh live Whisker run is still needed to demonstrate
the Agent-visible aggregate with this candidate.

The ten-project v6 campaign and both ablation arms remain frozen. This candidate
does not implement the missing multi-module Jenkins identity mapping, relax
CI comparability, repair target-project tests, or replace any earlier result.
