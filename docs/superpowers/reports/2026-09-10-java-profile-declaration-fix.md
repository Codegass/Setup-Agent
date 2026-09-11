# Java profile declarations: Commons CSV regression

The original 23-project ablation remains frozen at its three v8 source commits.
This change is a separate implementation candidate; it does not amend those
attempts or their scores.

The completed legacy-interface Commons CSV attempt built and tested successfully
at `bb0f0fbb0f84de5cf5a0c73a9fa7051fb5135650`: 985 records, 974 passed,
11 skipped, no failed/error records. Its final local verdict remained partial
because the Java survey reported `profile_activation:benchmark` as unresolved.
The profile declares a Maven compiler plugin that changes testIncludes, without
an explicit Java version or toolchain setting. The previous parser marked any
profile containing the compiler plugin as an unresolved Java requirement.

An offline input ablation reproduced both sides of that imprecision: retaining
only the compiler artifactId still produced uncertainty, while removing the
plugin and declaring a profile-only `maven.compiler.target=21` property did not.
These are parser experiments, not Maven executions or reassessments of the run.

The parser now checks conditional Java declarations: compiler source/target/
release and test variants, compiler execution/toolchain options, Enforcer Java
rules, and corresponding Maven compiler properties. A plugin entry that only
changes test selection does not invent a Java constraint. Conditional version
properties remain unknown even without a plugin element. Explicit profile
deactivation still uses the existing receipt-bound resolution. Plugin execution
and dependency compatibility remain the native runner's responsibility.

The implementation does not add a Maven model interpreter or new execution
gate. Actual Java mismatches, unknown conditional Java requirements, missing
authority, changed source/configuration, and interrupted receipts retain their
existing checks. The focused parser, preflight, physical judge, fixed-command,
CI comparison, and finalization suite passed 258 tests. Formatting preserved
both changed Python ASTs; `git diff --check` passed. A new live regression is
still required after the fixed cohort finishes; the original partial result is
preserved.

Experiment evidence is under `logs/verification-23-20260910/` in the root
checkout: `cohort23/report/csv-diagnosis.json`,
`validation/csv-java-profile-ablation.json`, and
`validation/csv-java-profile-fixed-probe.json`.

The separate mandatory-plan CSV attempt did not reach Build: a plan with the
CI command under build_steps omitted test_steps, was refused, and the model
eventually closed Analyze as unknown. Its partial label has a different cause.
The legacy Agent also supplied incorrect report counts of 568/568 with no skips;
the existing report tool correctly retained the sealed 985/974/11 values. This
does not justify another execution gate or overriding measured counts with
model prose.
