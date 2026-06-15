# Build and Test Metrics Display Design

## Purpose

The Status view should make build and test outcomes easy to trust at a glance. It must show the most important conclusion data first, then offer structured details that explain how each number was derived. The design should avoid the previous failure mode where report text, phase state, and physical evidence appear to contradict one another.

## Scope

This design covers the web UI surfaces that display build and test summaries for a SAG session:

- Session Status cards for Build and Test.
- Expanded Build and Test detail panels.
- Read-model fields required by the frontend.

This design does not change the phase state machine, the build/test execution tools, or the report generation algorithm beyond consuming the evidence fields they already produce.

## Design Direction

Use the approved refined B design: conclusion-first cards with expandable, formatted details.

Default cards emphasize what the user cares about immediately:

- Did the build produce real artifacts?
- Did the tests mostly pass?
- How many tests failed or were skipped?
- What physical evidence was used?

Details explain the calculation with tables, grouped lists, source labels, and evidence links. Details must not be plain raw text dumps.

## Build Card

The default Build card should show:

- Verdict: success, partial, failed, blocked, or unknown.
- Build system/tool when known, such as Maven or Gradle.
- Class file count.
- JAR count.
- Artifact evidence source, such as physical artifact scan.
- A compact note when module output coverage is known.

The primary conclusion line should be human-readable:

- `Artifacts verified`
- `No build artifacts found`
- `Build evidence unavailable`

The Build card should avoid overclaiming. A wrapper JAR, cache directory, or build file alone is not enough to show success.

## Build Details

The expanded Build detail panel should be structured into sections:

- **Artifact Summary**: class count, JAR count, output directories, module count with outputs.
- **Evidence Samples**: a short list of representative class/JAR paths.
- **Warnings**: missing outputs, stale evidence, unknown build system, or skipped artifact checks.

If details are unavailable, show a small unavailable state with the missing source, not an empty card.

## Test Card

The default Test card should show conclusion data in this order:

- Pass rate as the visual headline.
- Runner executions: raw JUnit XML testcase count.
- Passed, failed, skipped counts.
- XML report count.
- Unique normalized method count.
- Method execution percentage when declared/static test count is available.

For example:

- `99.8% passed`
- `18,805 / 18,839 runner executions passed`
- `5 failed · 29 skipped · 760 XML reports`
- `9,497 unique methods · 46.3% method coverage`

The card must label runner executions separately from unique normalized methods. Runner executions are the physical test-runner fact. Unique normalized methods are a derived method-level view that folds parameterized and dynamic executions.

## Test Details

The expanded Test detail panel should use formatted sections:

- **Calculation** table:
  - Runner executions.
  - Passed executions.
  - Failed executions.
  - Skipped executions.
  - Unique normalized methods.
  - Declared/static test methods.
  - Method execution percentage.
- **Attention** list:
  - Failing test methods, grouped and truncated with a clear path to full details.
  - Skipped count and source.
  - Conflicts such as parse errors or missing reports.
- **Evidence Sources**:
  - XML report count.
  - Evidence refs or representative report paths.
  - Link to phase/test context where available.

Details must use tables, compact rows, chips, and grouped failure entries. They must not render raw JSON or long unformatted text as the primary experience.

## Data Contract

The backend read model should expose enough fields for the UI to avoid parsing markdown tables when structured evidence is available.

Recommended Test fields:

- `tests.total`: runner XML execution count.
- `tests.passed`
- `tests.failed`
- `tests.errors`
- `tests.skipped`
- `tests.passRate`
- `tests.reportFileCount`
- `tests.uniqueTotal`
- `tests.uniquePassed`
- `tests.uniqueFailed`
- `tests.uniqueErrors`
- `tests.uniqueSkipped`
- `tests.declaredTotal`
- `tests.methodExecutionRate`
- `tests.failingNames`
- `tests.conflicts`
- `tests.evidenceRefs`

Recommended Build fields:

- `build.state`
- `build.system`
- `build.tool`
- `build.classCount`
- `build.jarCount`
- `build.moduleOutputCount`
- `build.artifactSamples`
- `build.warnings`
- `build.evidenceRefs`

If a field cannot be computed, omit it or return `null`. The frontend should render an unavailable state instead of manufacturing placeholder metrics.

## UI Behavior

Default cards should be concise and conclusion-first. Expanded detail controls should be available from the card header or footer.

The UI should use source labels such as:

- `Runner XML`
- `Physical artifact scan`
- `Static catalog`
- `Normalized runtime methods`

The pass-rate progress bar should represent runner execution pass rate, not method coverage. Method execution percentage is a separate row because it answers a different question.

## Error Handling

When physical evidence is partial or contradictory:

- Keep the main verdict visible.
- Show a compact conflict chip.
- Put the explanation in the expanded details.
- Link to evidence refs when possible.

If XML report parsing fails, show the parse conflict in the Test details and keep any successfully parsed metrics labeled as partial evidence.

## Testing

Backend tests should cover:

- Maven Surefire/Failsafe metrics flow into the read model.
- Gradle `build/test-results` metrics flow into the read model.
- Runner execution count and unique method count are both preserved.
- Markdown fallback still works when structured fields are missing.

Frontend or snapshot tests should cover:

- Status card renders conclusion metrics.
- Expanded Test details render a calculation table and failing list.
- Expanded Build details render artifact summary and samples.
- Unavailable metrics render without fake zeroes.

Manual verification should use at least one Maven project and one Gradle project. Kafka-style data should show runner executions and unique normalized methods as separate facts.
