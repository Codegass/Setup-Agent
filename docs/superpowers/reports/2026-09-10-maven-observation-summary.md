# Maven observation summary: facts before log-parser labels

The frozen full CSV and DBCP attempts returned invocation-bound XML counts, yet
also displayed `Phases executed: NONE DETECTED (possible parsing issue)`. The
parser only recognizes some Maven plugin-banner formats. Its plugin labels are
neither an exhaustive lifecycle record nor evidence that compilation or tests
completed. Both attempts later ran tests again; this timing does not prove that
the banner warning alone caused those reruns.

The separate candidate removes that phase-based narrative from the enhanced
success summary. It reports command completion, existing test totals (including
failures, errors, and skips), their XML or console basis, artifact observations
and the full-output reference. Missing counts stay unavailable, explicit zero
counts stay zero, and a plugin label alone no longer claims tests ran or
compilation succeeded. The parser metadata, receipt, invocation outcome, native
command, and final judgments are unchanged. No new banner parser or Maven model
interpreter is added.

Validation: 125 focused tests passed before the final wording adjustment. The
complete candidate suite after that adjustment passed **7608 tests**, with
**32 skipped** and 44 warnings in 122.76 seconds. Five new cases exercise XML
without recognized banners, absent counts with/without plugin labels, console
counts with red tests, and explicit zero counts. The tests also preserve the
input analysis and output reference.

Formal v8 attempts remain frozen. Real candidate regressions are still pending
the fixed cohort; these test results do not replace its observed failures or
prove the Agent will stop repeating Maven invocations.
