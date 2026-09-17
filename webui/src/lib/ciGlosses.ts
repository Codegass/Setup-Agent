/**
 * One plain-English sentence per reason code a CI comparison can cite.
 *
 * This is a copy of `src/sag/result_card/glosses.py`, key for key and word for
 * word: the codes arrive with the payload, the sentences are presentation, and
 * a round trip to the server to fetch a static table would buy nothing. The
 * parity test in `tests/test_gloss_parity.py` is what keeps the two in step —
 * add a code there first, then here.
 *
 * A code with no entry renders as the bare code rather than being dropped, so a
 * code that ships before its sentence is visible the day it ships.
 */
export const REASON_GLOSS: Record<string, string> = {
  // Official-CI comparison: gates
  "CERTIFICATE_AUTHORITY_UNAVAILABLE": "this run's own results were not bound to a receipt, so nothing could be compared",
  "COUNTS_NOT_RECEIPT_BOUND": "the test counts were not bound to a receipt, so they cannot be compared",
  "TARGET_WITHOUT_COUNTS": "the CI job recorded no test counts to compare against",
  "TARGET_TEST_UNIVERSE_EMPTY": "the CI job recorded no test identities to compare against",
  "TARGET_BUILD_UNIVERSE_UNAVAILABLE": "the CI job recorded no module list to compare against",
  "COMPARISON_SUBJECT_UNAVAILABLE": "the repository and commit under comparison could not be read",
  "COMPARISON_SUBJECT_MISMATCH": "the CI job ran on a different repository or commit",
  // Official-CI comparison: findings
  "NEW_RED_BEYOND_TARGET": "tests failed here that pass in CI",
  "CLEAN_BY_COUNTS_ONLY": "results matched by count, not by test name, which is a weaker match",
  "MODULES_BELOW_TARGET": "fewer modules were built than CI built",
  "BUILD_AXIS_NOT_SUCCESSFUL": "the build did not succeed, so CI attainment cannot be met",
  "EXECUTION_BELOW_TARGET": "fewer tests ran than CI ran",
  // Official-CI comparison: availability
  "official_ci_target_not_supplied": "no CI job was supplied to compare against",
  "official_ci_cell_not_matched": "no CI job on this commit matches the run's JDK and OS",
  "certificate_adapter_unavailable": "this run's evidence could not be put in comparable form",
  "CI_TEST_IDENTITIES_NOT_COMPARABLE": "the two sides name their tests differently, so they cannot be matched",
  "CI_TEST_SCOPE_OVERLAP_UNRESOLVED": "it is unclear whether both sides ran the same set of tests",
  "current_run_checkout_unavailable": "the workspace checkout could not be read",
  "current_checkout_differs_from_run_pin": "the workspace moved off the commit the run started on",
  "fixed_task_completion_unavailable": "the required task result was not available to compare",
  "fixed_task_repository_or_revision_mismatch": "the required task names a different repository or commit",
  "fixed_task_certificate_requires_jvm_runner": "comparison needs a Maven or Gradle step, and the task has none",
  "accepted_execution_plan_unavailable": "the accepted build plan was not recorded",
  "current_receipt_or_assessment_publication_unavailable": "this run's receipts were not published to the host record",
  // Required task
  "task_execution_scope_unavailable": "the run recorded no command scope to check the task against",
  "task_run_pin_unavailable": "the record that fixes this run's inputs could not be read",
  "task_definition_missing_or_changed": "the task definition is missing or no longer matches",
  "task_repository_or_revision_mismatch": "the task names a different repository or commit",
  "task_current_checkout_mismatch": "the workspace is not at the task's frozen commit",
  "task_source_changed_or_unverified": "the working tree was modified, so the task cannot be certified",
  "task_receipt_publication_unavailable": "the task's receipts were not published to the host record",
  // Certificate defeaters
  "AUTHORITATIVE_BUILD_FAILURE": "a build command failed",
  "TEST_EXECUTION_FAILURE": "the test run itself failed to complete",
  "TEST_OUTCOME_RED": "tests failed or errored",
  "MISSING_REQUIRED_IDENTITY": "a required module or step produced no result",
  "EMPTY_VERDICT_BEARING_RESULT": "no test results were produced where results were required",
  "IDENTITY_CONFLICT": "two records disagree about the same module or test",
  "DIAGNOSTIC_ONLY_OBSERVATION": "the results are diagnostic only and do not grade the run",
  "TEST_RESULTS_UNAVAILABLE": "no test results were available",
  "LINEAGE_UNAVAILABLE": "the chain from command to result is incomplete",
  "UNSEALED_DENOMINATOR": "what the run was supposed to cover was never fixed, so no fraction is shown",
  "DOCUMENTED_NO_AUTOMATED_TESTS": "the project documents that it has no automated tests",
  "SCOPE_AUTHORITY_BLOCKED": "the run's scope could not be established",
  "BUILD_AUTHORITY_BLOCKED": "the build result could not be established",
  "TEST_EXECUTION_AUTHORITY_BLOCKED": "whether the tests ran could not be established",
  "TEST_OUTCOME_AUTHORITY_BLOCKED": "whether the tests passed could not be established",
  "INTEGRITY_AUTHORITY_BLOCKED": "the evidence records could not be verified",
  // Conflicts recorded on the run
  "test_failures_detected": "some tests failed",
  "test_errors_detected": "some tests errored",
  "test_failures_heavy": "more than half the results are red, which usually means the environment, not the project",
  "rate_denominator_not_a_bound": "the count these were measured against cannot bound them, so no percentage is shown",
  "test_executions_unattributed_to_receipts": "some test reports belong to no recorded command and were set aside",
  "test_reports_stale": "some test reports were rewritten after being read and were set aside",
  "test_report_parse_error": "some test reports could not be read",
  "test_census_sources_disagree": "two counts of the project's tests disagree; the per-module sum is used",
  "build_modules_incomplete": "the disk scan found modules the build run did not cover",
  "reactor_scope_narrowed": "the build ran over fewer modules than the project declares",
  "build_coverage_scope_unverified": "how much of the project the build covered could not be checked",
  "module_scan_contradicts_physical_build": "the disk scan and the build output disagree about which modules built",
  "validated_test_stats_invalid": "the recorded test totals do not add up, so no completion ratio is shown",
  "test_execution_interrupted": "the test run stopped before it finished; the counts are what it reached",
  "test_stats_basis_incomparable": "two test counts were measured differently and cannot be combined",
  "build_oracle_divergence": "two checks disagree about whether the build succeeded",
}

/** The sentence for `code`, or the code itself when it has none. */
export function gloss(code: string): string {
  return REASON_GLOSS[code] ?? code
}
