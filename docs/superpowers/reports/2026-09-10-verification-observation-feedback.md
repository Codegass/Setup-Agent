# Verification feedback after Tomcat and Commons CSV

These changes belong to a separate candidate. All three formal v8 arms remain
frozen, and their original outcomes and costs are preserved.

## Keep an executable request independent of its phase label

The legacy Tomcat attempt submitted `action=test` during Build and was refused
by `PHASE_ACTION_MISMATCH`. Changing action to install contradicted its source
command. The Agent eventually changed the actual command from test to install,
then produced 229 passing tests. The complete-command arm ran the original test
command directly. Both were locally successful, so success alone hides the
forced command change.

The candidate removes that phase-label veto, including for compatibility calls
that still use action/args. It preserves the submitted action and source command
when minting the exact envelope. Missing tool-call identity, inconsistent
intent, contract/receipt authorization, and completion validation still apply.
The regression checks both an identified test request and an unidentified one;
allowing the former does not seal evidence or advance the phase.

## Show the existing JVM test observation to the Advisor

The complete-interface CSV attempt's first, original CI invocation reported 985
records, 974 passed and 11 skipped from invocation-bound XML. At Test entry the
Advisor said to treat the existing Build receipt as a hint rather than test
evidence, and recommended another test execution. The Agent followed it through
Bash; the second invocation again produced 985 records. Both receipts remain
separate. The phase objective already allowed reuse, but the dedicated test
digest only represented pytest collection metadata.

The existing test-attempt digest now includes the latest Maven/Gradle runner
observation, with command, cwd, receipt/output references, tool outcome, and XML
counts when the tool explicitly marks `invocation_report_xml`. Without that
basis the counts remain unknown; in particular this is not proof that every
Gradle path currently publishes such counts. Newer unmeasured or failed attempts
do not fall back to older green counts. Pytest keeps its collection vocabulary
and chronology. The projection does not mutate evidence or adjudicate success.

This removes an information gap; it does not prove the Advisor will always
avoid unnecessary reruns. Its effect still needs a new live regression after
the fixed cohort. The focused control, fixed-command, Advisor and native-state
suite passed 198 tests. The earlier Java profile change passed its separate
258-test suite; some files overlap, so these are not added as distinct tests.

The full candidate test suite, including the Java profile fix, passed **7603 tests**,
with **32 skipped** and 44 warnings in 132.41 seconds. This is one complete
pytest run; it does not replace the pending live regression.
