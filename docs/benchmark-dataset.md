# Java benchmark dataset

The dataset records reproducible official Java CI tasks for comparing setup
agents. A repository's presence in the registry does not by itself establish a
complete CI reference or permission to start a formal experiment.

## Current version and reproduction

The active cohort contains **98 reference projects and 113 tasks**, including
21 projects whose canonical task has a complete testcase archive. It retains the
original 96 historical references with their later audit states and adds two
independently reviewed references: JetBrains/Grammar-Kit and
micronaut-projects/micronaut-starter. This does not certify all 98 under the new
campaign protocol. Eleven projects have unambiguous labels within their recorded
run; cross-run identity is not yet validated. Formal campaign readiness remains
zero until the common requirements-v2 and reference-replay gates are satisfied.

Eclipse Foundation and FasterXML were withdrawn as active expansion sources
because their completed reviews added no complete testcase archive. Their prior
integration remains a sealed historical audit; this is not evidence that every
remaining project in those organizations is unusable.

- [Active selection](../output/java-benchmark-replacement-scout-20260922/active-selection.json)
- [Registry and evidence references](../output/java-benchmark-replacement-scout-20260922/registry.json)
- [Project inventory](../output/java-benchmark-replacement-scout-20260922/PROJECTS.csv)
- [Replacement results and limitations](../output/java-benchmark-replacement-scout-20260922/REPORT.md)
- [Replacement feasibility protocol and search history](../output/java-benchmark-replacement-scout-20260922/PROTOCOL.md)

The replacement phase used unchanged project and task criteria. Report this
evidence-driven enrichment as purposive selection, separate from the original
cohort; do not interpret it as a random Java sample. Other feasibility probes
remain audit records and unresolved candidates, not automatic admissions.

Grammar-Kit contributes 309 passing case records from a May 2025 TeamCity build;
its September 2026 repository activity independently meets the population rule.
The original rule does not impose a CI-age cutoff. Micronaut Starter contributes
11,040 execution records (10,876 passed and 164 skipped), retaining repeated
labels. Its declared local-publishing variant preserves packaging, docs and CLI
verification; source review is complete, execution replay is pending. Exact
commands, source pins, runtime observations and limitations are in the task
packets linked from the active registry.

The integrated September 22 registry below describes the **withdrawn expansion
snapshot**, including its one count-only ClassMate addition. Its 97-reference
count is not the active-cohort count. These local artifacts may be absent in a
fresh clone of this repository.

- [Historical withdrawn registry](../output/java-benchmark-integrated-20260922/registry.json)
- [Historical withdrawn dataset card](../output/java-benchmark-integrated-20260922/DATASET_CARD.md)

Rebuild the active view from retained inputs without collecting new data or
running projects:

```sh
UV_CACHE_DIR=/private/tmp/setup-agent-uv-cache uv run --offline --no-sync python output/java-benchmark-replacement-scout-20260922/assemble_active.py
```

Evidence remains in its original archive. Resolve a reference against its
declared evidence root and verify its hash; moving a task JSON does not move its
referenced files. The original snapshots and their hashes are preserved.

## One set of standards, separate decisions

| Decision | Required evidence | What it establishes |
|---|---|---|
| Population eligibility | Public, non-fork, non-archived repository; primary language Java; strictly more than 200 stars; default-branch commit activity within the declared calendar-year window | A repository is eligible for task review |
| Official CI reference admission | A reviewed Maven or Gradle task, immutable source revision, official CI identity, declared commands and environment, build scope, and complete native test-result accounting with archived provenance | A reference task can support the comparisons justified by its evidence |
| Formal campaign readiness | Reviewed requirements v2, source and runtime closure, reference replay, frozen task and CI target, and complete recording arrangements | The task may enter the formal agent experiment under the common protocol |

The task must satisfy the same Linux, non-Android and dependency rules as the
original collection. Required nested Docker or a second production compilation
toolchain makes that task unsuitable for this benchmark. A Dockerfile, a Kotlin
Gradle script, an Android example or native test fixtures alone does not establish
such a dependency; the selected task must be reviewed.

A complete count-level CI reference can be admitted without individual testcase
records. Case identity is a separate evidence capability, not a new admission
requirement imposed on the expansion. Counts cannot prove that two agents ran
the same cases. Complete case records, collision-free labels within one run, and
validated cross-run identity mappings must be reported separately.

Requirements v2 and reference replay are common campaign gates for both old and
new references. They do not retroactively change the original reference-admission
standard. Missing review or evidence stays explicit; an unknown requirement is
neither a pass nor an exclusion.

## Collection waves and denominators

The active registry contains the old 315-candidate ledger and 15 new
population-eligible candidates (12 JetBrains and three Micronaut).
**330 is a registry count, not a common-stage screening denominator.** The old
ledger includes candidates that failed the activity check; the new 15 have
already passed their wave's population checks. Namespace enumeration covered
864 JetBrains and 149 Micronaut repositories. Only one reference from each has
passed the full static task and complete-case review.
Attrition must use each wave's full discovery frame and the same decision stage.

The original observation is from September 16–17; the extension is from
September 22. Activity windows are relative to each declared observation date.
This is a multi-wave collection, not a same-day census. The original 96 reference
projects and 111 tasks describe that historical collection. The later recovery
counts of 19 projects with complete case records and 10 with collision-free
observed labels apply only to its original canonical task set. They are not
counts for the active expanded registry, and they do not establish cross-run
identity. Use the replacement report for current admissions and their reasons.

In the withdrawn historical expansion, Eclipse membership follows its frozen official project registry, including
declared dedicated GitHub organizations and explicitly named repositories in
shared organizations. It is not inferred from an organization-name prefix and
does not claim exhaustive coverage of the foundation. FasterXML uses its
enumerated namespace. Preserve the collection wave, maintainer, repository ID,
project family and task identity when aggregating; related Jackson repositories
are not independent ecosystem samples.

## Requirements and project size

Build requirements are overlapping categories: compilation, packaging,
installation, testing, documentation, quality checks and native compilation.
Observed goals in a CI job are distinguished from mandatory obligations in the
frozen task. Pending classification must not become an empty requirement list.

Size measures Java physical lines at the pinned source revision, including
tests, examples, comments and blank lines. This is neither semantic source lines
nor runtime cost. The integrated view applies the same frozen descriptive
thresholds from the original source audit to verified measurements in each wave:

- Small: at most 27,580.75 Java physical lines.
- Medium: more than 27,580.75 and at most 225,897.25 lines.
- Large: more than 225,897.25 lines.
- Unknown: no verified measurement for the relevant pinned revision.

These cut points are the inclusive first and third quartiles of the original
80 verified canonical source measurements. Keeping them fixed allows consistent
descriptive labels across waves. They are not admission limits, expected memory
budgets, or quartiles recomputed to force equal groups in the expanded pool.
Retain the raw size and measurement provenance alongside every label.

## Historical records and experiment protocol

- [Original collection protocol](../output/java-benchmark-20260916/PROTOCOL.md)
- [September 22 offline rescreen](../output/java-benchmark-rescreen-20260922/REPORT.md)
- [Additional source and CI recovery](../output/java-benchmark-acquisition-20260922/REPORT.md)
- [Two-maintainer expansion protocol](../output/java-benchmark-org-expansion-20260922/EXPANSION_PROTOCOL.md)
- [Requirements v2 recording and evaluation](benchmark-requirements-v2.md)

The September 17 insights dashboard and the dated rescreen audit remain
historical views with their original inputs and denominators. Adding candidates
to the current registry does not update those views or certify a SAG result.
