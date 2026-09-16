# Result Card Implementation Plan (phase 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one shared presentation model of a finished run and render it identically in the CLI end-of-run block, the Workbench session detail, and the Markdown setup report.

**Architecture:** A new `sag.result_card` package reads the sealed `verdict.json` snapshot plus optional sibling artifacts and produces a `RunResultCard`: exactly seven rows (setup, task, build, tests, coverage, ci, report), plus stats, attention items and notes. It copies statuses and numbers — it never recomputes a judgment. Two renderers consume it: a plain-text block for the terminal (`sag.console.result_block`) and a Markdown table for the report (`sag.result_card.markdown`). The Workbench serves the model itself as JSON.

**Tech Stack:** Python 3.10+, pydantic v2 (`BaseModel`, `ConfigDict(frozen=True, extra="forbid")`), pytest, Rich (console markup only, in the CLI renderer).

**Spec:** `docs/superpowers/specs/2026-09-15-result-card-and-trajectory-surfaces-design.md` — read §2, §4.1, §6 before starting. The plan argues from that spec; where they disagree, the spec wins and the plan is wrong.

## Global Constraints

Every task's requirements implicitly include all of these. They are the spec's §2.2 rules, copied verbatim where exact.

- **No coercion.** A `None` count is never rendered as `0`. An `unavailable` row always has a non-empty `reason`.
- **Every fraction names its denominator** in `detail`, never in the headline alone.
- **Bounds and guards survive.** `≥N` for counts whose metrics-v2 availability is `partial` with `bound: "lower"`. A rate that would round to `100%` while any failed or errored result exists renders `<100%`.
- **The card copies, it does not judge.** `verdict`, every row `status`, and every number come from the seal or a sealed sibling artifact. Conflicts become `notes`, never a different word.
- **Row order and count are fixed.** Seven rows, always, in `RowKey` order: `setup, task, build, tests, coverage, ci, report`.
- **Tone of the whole card is the setup row's tone.** Row tones are independent of each other.
- **Forbidden in any user-visible string** produced by this package: `sealed`, `canonical`, `claimed`, `quarantined`, `subject`, `snapshot`, `metrics-v2`, `verdict-bearing`, `promoting`. Use instead: "recorded", "bound to a receipt", "set aside", "test class", "result".
- **No judgment module changes.** `verdict.py`, `verdict_rates.py`, `verdict_finalizer.py`, `attainment.py`, `acceptance_task.py`'s and `ci_comparison.py`'s decision logic are read-only here. Task 1 extracts existing string literals into constants in two of them; the strings themselves must be byte-identical.
- **Commit messages carry no `Co-Authored-By` trailer.**
- **Test command:** `PYTHONPATH=. uv run pytest <path> -v`.

---

## File Structure

The spec names "a new module `src/sag/result_card.py`". Seven row builders, a gloss table and two renderers in one file would run past 900 lines, so it ships as a package with one responsibility per file. Everything else follows the spec's names exactly.

| File | Responsibility |
|---|---|
| `src/sag/result_card/__init__.py` | Public surface: `RunResultCard`, `ResultRow`, `ResultStats`, `AttentionItem`, `build_result_card`, `render_result_card_markdown`, `REASON_GLOSS`, `gloss` |
| `src/sag/result_card/glosses.py` | `REASON_GLOSS` table and `gloss(code)`; no imports from the rest of the package |
| `src/sag/result_card/models.py` | The pydantic models only; no derivation |
| `src/sag/result_card/rows.py` | One builder per row: `setup_row`, `task_row`, `build_row`, `tests_row`, `coverage_row`, `ci_row`, `report_row` |
| `src/sag/result_card/build.py` | `build_result_card`, `_stats`, `_attention`, `_notes` — assembly only |
| `src/sag/result_card/markdown.py` | `render_result_card_markdown` |
| `src/sag/console/__init__.py` | New package marker (empty) |
| `src/sag/console/result_block.py` | `render_result_block(card, width=78)` — the terminal block |
| `tests/result_card_fakes.py` | Shared snapshot/sibling-artifact fixture builders |

Modified: `src/sag/main.py` (Task 9), `src/sag/agent/agent.py` (Task 9), `src/sag/tools/report_tool.py` (Task 10), `src/sag/web/session_registry.py` + `src/sag/web/models.py` (Task 11), `src/sag/agent/ci_comparison.py` + `src/sag/agent/acceptance_task.py` (Task 1, constant extraction only).

Deleted: `src/sag/web/verdict.py` and `tests/test_web_verdict.py` (Task 11).

---

### Task 1: Reason-code inventories and the gloss table

Every code a row can cite must have one plain-English sentence, and a new code must not be able to ship without one. The CI and acceptance-task codes are inline string literals today, so they are first extracted into module constants — same strings, new names — which lets the coverage test enumerate them instead of grepping source.

**Files:**
- Create: `src/sag/result_card/__init__.py`, `src/sag/result_card/glosses.py`
- Modify: `src/sag/agent/ci_comparison.py` (lines 211, 216, 233, 243, 246, 268, 276, 577, 693, 699, 721, 727, 736, 747, 757, 784, 806, 810, 838, 859), `src/sag/agent/acceptance_task.py` (lines 140, 146, 148, 268, 280, 282, 284, 287, 293, 296)
- Test: `tests/test_result_card_glosses.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `sag.result_card.glosses.REASON_GLOSS: dict[str, str]`, `sag.result_card.glosses.gloss(code: str) -> str`, `sag.result_card.glosses.KNOWN_REASON_CODES: frozenset[str]`; `sag.agent.ci_comparison.CI_REASON_CODES: frozenset[str]`; `sag.agent.acceptance_task.TASK_REASON_CODES: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_result_card_glosses.py`:

```python
"""Every reason code a result row can cite has one plain-English sentence."""

from sag.agent.acceptance_task import TASK_REASON_CODES
from sag.agent.ci_comparison import CI_REASON_CODES
from sag.agent.java_success_certificates import BLOCKED_AXIS_REASON_CODES
from sag.metrics import attainment
from sag.result_card.glosses import KNOWN_REASON_CODES, REASON_GLOSS, gloss
from sag.verdict import ADJUDICATED_CONFLICTS, BUILD_SCOPE_CONFLICTS
from sag.verdict_rates import (
    HEAVY_RED_CONFLICT,
    UNBOUNDED_CONFLICT,
    UNCOUNTED_REPORT_CONFLICTS,
)

FORBIDDEN = (
    "sealed",
    "canonical",
    "claimed",
    "quarantined",
    "subject",
    "snapshot",
    "metrics-v2",
    "verdict-bearing",
    "promoting",
)


def _attainment_codes() -> set[str]:
    return {
        attainment.CERTIFICATE_AUTHORITY_UNAVAILABLE,
        attainment.COUNTS_NOT_RECEIPT_BOUND,
        attainment.TARGET_WITHOUT_COUNTS,
        attainment.TARGET_TEST_UNIVERSE_EMPTY,
        attainment.TARGET_BUILD_UNIVERSE_UNAVAILABLE,
        attainment.COMPARISON_SUBJECT_UNAVAILABLE,
        attainment.COMPARISON_SUBJECT_MISMATCH,
        attainment.NEW_RED_BEYOND_TARGET,
        attainment.CLEAN_BY_COUNTS_ONLY,
        attainment.MODULES_BELOW_TARGET,
        attainment.BUILD_AXIS_NOT_SUCCESSFUL,
        attainment.EXECUTION_BELOW_TARGET,
    }


def _certificate_codes() -> set[str]:
    return {
        "AUTHORITATIVE_BUILD_FAILURE",
        "TEST_EXECUTION_FAILURE",
        "TEST_OUTCOME_RED",
        "MISSING_REQUIRED_IDENTITY",
        "EMPTY_VERDICT_BEARING_RESULT",
        "IDENTITY_CONFLICT",
        "DIAGNOSTIC_ONLY_OBSERVATION",
        "TEST_RESULTS_UNAVAILABLE",
        "LINEAGE_UNAVAILABLE",
        "UNSEALED_DENOMINATOR",
        "DOCUMENTED_NO_AUTOMATED_TESTS",
        *BLOCKED_AXIS_REASON_CODES.values(),
    }


def _conflict_codes() -> set[str]:
    return {
        *ADJUDICATED_CONFLICTS,
        *BUILD_SCOPE_CONFLICTS,
        *UNCOUNTED_REPORT_CONFLICTS,
        HEAVY_RED_CONFLICT,
        UNBOUNDED_CONFLICT,
        "validated_test_stats_invalid",
        "test_execution_interrupted",
        "test_stats_basis_incomparable",
        "build_oracle_divergence",
    }


def test_inventory_covers_every_producing_module():
    expected = (
        _attainment_codes()
        | _certificate_codes()
        | _conflict_codes()
        | set(CI_REASON_CODES)
        | set(TASK_REASON_CODES)
    )
    missing = expected - KNOWN_REASON_CODES
    assert not missing, f"codes produced but not inventoried: {sorted(missing)}"


def test_every_inventoried_code_has_a_gloss():
    missing = {code for code in KNOWN_REASON_CODES if not REASON_GLOSS.get(code, "").strip()}
    assert not missing, f"inventoried codes without a gloss: {sorted(missing)}"


def test_glosses_are_plain_sentences():
    for code, text in REASON_GLOSS.items():
        assert text == text.strip(), code
        assert not text.endswith("."), f"{code}: glosses are clause-shaped, no trailing period"
        assert len(text) <= 120, f"{code}: {len(text)} chars"
        lowered = text.lower()
        for word in FORBIDDEN:
            assert word not in lowered, f"{code} uses the forbidden word {word!r}"


def test_unknown_code_returns_itself():
    assert gloss("SOME_CODE_WE_HAVE_NEVER_SEEN") == "SOME_CODE_WE_HAVE_NEVER_SEEN"


def test_known_code_returns_its_sentence():
    assert gloss("official_ci_cell_not_matched") == (
        "no CI job on this commit matches the run's JDK and OS"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/test_result_card_glosses.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.result_card'` (and `ImportError` for `CI_REASON_CODES`).

- [ ] **Step 3: Extract the CI reason literals into constants**

In `src/sag/agent/ci_comparison.py`, add after the existing imports (before the first class):

```python
# The reason strings this module publishes, named so consumers can enumerate
# them. Extracted verbatim from their raise/return sites; the strings are
# unchanged and remain the wire values in CIComparisonSnapshot.reasons.
CURRENT_RUN_CHECKOUT_UNAVAILABLE = "current_run_checkout_unavailable"
CURRENT_CHECKOUT_DIFFERS_FROM_RUN_PIN = "current_checkout_differs_from_run_pin"
FIXED_TASK_COMPLETION_UNAVAILABLE = "fixed_task_completion_unavailable"
FIXED_TASK_REPOSITORY_OR_REVISION_MISMATCH = "fixed_task_repository_or_revision_mismatch"
FIXED_TASK_CERTIFICATE_REQUIRES_JVM_RUNNER = "fixed_task_certificate_requires_jvm_runner"
ACCEPTED_EXECUTION_PLAN_UNAVAILABLE = "accepted_execution_plan_unavailable"
CURRENT_RECEIPT_OR_ASSESSMENT_PUBLICATION_UNAVAILABLE = (
    "current_receipt_or_assessment_publication_unavailable"
)
CERTIFICATE_ADAPTER_UNAVAILABLE = "certificate_adapter_unavailable"
OFFICIAL_CI_TARGET_NOT_SUPPLIED = "official_ci_target_not_supplied"
OFFICIAL_CI_CELL_NOT_MATCHED = "official_ci_cell_not_matched"
CI_TEST_IDENTITIES_NOT_COMPARABLE = "CI_TEST_IDENTITIES_NOT_COMPARABLE"
CI_TEST_SCOPE_OVERLAP_UNRESOLVED = "CI_TEST_SCOPE_OVERLAP_UNRESOLVED"

CI_REASON_CODES: frozenset[str] = frozenset(
    {
        CURRENT_RUN_CHECKOUT_UNAVAILABLE,
        CURRENT_CHECKOUT_DIFFERS_FROM_RUN_PIN,
        FIXED_TASK_COMPLETION_UNAVAILABLE,
        FIXED_TASK_REPOSITORY_OR_REVISION_MISMATCH,
        FIXED_TASK_CERTIFICATE_REQUIRES_JVM_RUNNER,
        ACCEPTED_EXECUTION_PLAN_UNAVAILABLE,
        CURRENT_RECEIPT_OR_ASSESSMENT_PUBLICATION_UNAVAILABLE,
        CERTIFICATE_ADAPTER_UNAVAILABLE,
        OFFICIAL_CI_TARGET_NOT_SUPPLIED,
        OFFICIAL_CI_CELL_NOT_MATCHED,
        CI_TEST_IDENTITIES_NOT_COMPARABLE,
        CI_TEST_SCOPE_OVERLAP_UNRESOLVED,
    }
)
```

Then replace each literal at the sites listed in **Files** with its constant, e.g. line 211 `raise ValueError("current_run_checkout_unavailable")` becomes `raise ValueError(CURRENT_RUN_CHECKOUT_UNAVAILABLE)`; line 806 `reasons=("official_ci_target_not_supplied",)` becomes `reasons=(OFFICIAL_CI_TARGET_NOT_SUPPLIED,)`; the `item.code == "CI_TEST_SCOPE_OVERLAP_UNRESOLVED"` comparisons at 838 and 859 become `item.code == CI_TEST_SCOPE_OVERLAP_UNRESOLVED`. Change no string value.

- [ ] **Step 4: Extract the acceptance-task reason literals into constants**

In `src/sag/agent/acceptance_task.py`, add after the imports:

```python
# Snapshot-level reason strings, named for enumeration. Values are unchanged.
TASK_EXECUTION_SCOPE_UNAVAILABLE = "task_execution_scope_unavailable"
TASK_RUN_PIN_UNAVAILABLE = "task_run_pin_unavailable"
TASK_DEFINITION_MISSING_OR_CHANGED = "task_definition_missing_or_changed"
TASK_REPOSITORY_OR_REVISION_MISMATCH = "task_repository_or_revision_mismatch"
TASK_CURRENT_CHECKOUT_MISMATCH = "task_current_checkout_mismatch"
TASK_SOURCE_CHANGED_OR_UNVERIFIED = "task_source_changed_or_unverified"
TASK_RECEIPT_PUBLICATION_UNAVAILABLE = "task_receipt_publication_unavailable"

TASK_REASON_CODES: frozenset[str] = frozenset(
    {
        TASK_EXECUTION_SCOPE_UNAVAILABLE,
        TASK_RUN_PIN_UNAVAILABLE,
        TASK_DEFINITION_MISSING_OR_CHANGED,
        TASK_REPOSITORY_OR_REVISION_MISMATCH,
        TASK_CURRENT_CHECKOUT_MISMATCH,
        TASK_SOURCE_CHANGED_OR_UNVERIFIED,
        TASK_RECEIPT_PUBLICATION_UNAVAILABLE,
    }
)
```

Replace each literal at lines 140, 146, 148, 268, 280, 282, 284, 287, 293, 296 with its constant.

- [ ] **Step 5: Write the gloss table**

Create `src/sag/result_card/glosses.py`:

```python
"""One plain-English sentence per reason code a result row can cite.

A code without an entry renders as the bare code — never dropped, so a new
code is visible in the UI the day it ships and a test says it is unglossed.
Sentences are clause-shaped (no trailing period) because renderers place them
after a colon or inside parentheses.
"""

from __future__ import annotations

REASON_GLOSS: dict[str, str] = {
    # Official-CI comparison: gates
    "CERTIFICATE_AUTHORITY_UNAVAILABLE": "this run's own results were not bound to a receipt, so nothing could be compared",
    "COUNTS_NOT_RECEIPT_BOUND": "the test counts were not bound to a receipt, so they cannot be compared",
    "TARGET_WITHOUT_COUNTS": "the CI job recorded no test counts to compare against",
    "TARGET_TEST_UNIVERSE_EMPTY": "the CI job recorded no test identities to compare against",
    "TARGET_BUILD_UNIVERSE_UNAVAILABLE": "the CI job recorded no module list to compare against",
    "COMPARISON_SUBJECT_UNAVAILABLE": "the repository and commit under comparison could not be read",
    "COMPARISON_SUBJECT_MISMATCH": "the CI job ran on a different repository or commit",
    # Official-CI comparison: findings
    "NEW_RED_BEYOND_TARGET": "tests failed here that pass in CI",
    "CLEAN_BY_COUNTS_ONLY": "results matched by count, not by test name, which is a weaker match",
    "MODULES_BELOW_TARGET": "fewer modules were built than CI built",
    "BUILD_AXIS_NOT_SUCCESSFUL": "the build did not succeed, so CI attainment cannot be met",
    "EXECUTION_BELOW_TARGET": "fewer tests ran than CI ran",
    # Official-CI comparison: availability
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
    # Required task
    "task_execution_scope_unavailable": "the run recorded no command scope to check the task against",
    "task_run_pin_unavailable": "the record that fixes this run's inputs could not be read",
    "task_definition_missing_or_changed": "the task definition is missing or no longer matches",
    "task_repository_or_revision_mismatch": "the task names a different repository or commit",
    "task_current_checkout_mismatch": "the workspace is not at the task's frozen commit",
    "task_source_changed_or_unverified": "the working tree was modified, so the task cannot be certified",
    "task_receipt_publication_unavailable": "the task's receipts were not published to the host record",
    # Certificate defeaters
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
    # Conflicts recorded on the run
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

KNOWN_REASON_CODES: frozenset[str] = frozenset(REASON_GLOSS)


def gloss(code: str) -> str:
    """Return the sentence for ``code``, or the code itself when unglossed."""

    return REASON_GLOSS.get(code) or code


__all__ = ["REASON_GLOSS", "KNOWN_REASON_CODES", "gloss"]
```

Create `src/sag/result_card/__init__.py`:

```python
"""One presentation model of a finished run, rendered by three surfaces."""

from sag.result_card.glosses import REASON_GLOSS, gloss

__all__ = ["REASON_GLOSS", "gloss"]
```

- [ ] **Step 6: Run the gloss tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_result_card_glosses.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 7: Run the affected existing suites to prove the extraction changed nothing**

Run: `PYTHONPATH=. uv run pytest tests/test_ci_comparison.py tests/test_ci_comparison_surfaces.py tests/test_acceptance_task.py tests/test_acceptance_task_surfaces.py tests/test_task_recovery_context.py -q`
Expected: PASS, same counts as before the change.

- [ ] **Step 8: Commit**

```bash
git add src/sag/result_card tests/test_result_card_glosses.py src/sag/agent/ci_comparison.py src/sag/agent/acceptance_task.py
git commit -m "feat(result-card): name every reason code and gloss it in plain English"
```

---

### Task 2: Card models and test fixtures

**Files:**
- Create: `src/sag/result_card/models.py`, `tests/result_card_fakes.py`
- Modify: `src/sag/result_card/__init__.py`
- Test: `tests/test_result_card_models.py`

**Interfaces:**
- Consumes: nothing from Task 1 except the package existing.
- Produces: `RowKey`, `Tone`, `ResultRow`, `ResultStats`, `AttentionItem`, `RunResultCard`, `ROW_ORDER: tuple[RowKey, ...]`, `ROW_LABELS: dict[RowKey, str]`; and for tests `result_card_fakes.snapshot_dict(**overrides) -> dict`, `result_card_fakes.module_metrics(**overrides) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_result_card_models.py`:

```python
"""The card's shape: seven rows in one order, absence stated, nothing coerced."""

import pytest
from pydantic import ValidationError

from sag.result_card.models import (
    ROW_LABELS,
    ROW_ORDER,
    AttentionItem,
    ResultRow,
    ResultStats,
    RunResultCard,
)


def _row(key: str) -> ResultRow:
    return ResultRow(key=key, label=ROW_LABELS[key], status="unknown", tone="neutral", headline="—")


def _card(**overrides) -> RunResultCard:
    base = dict(
        run_id="run-1",
        verdict="success",
        verdict_source="snapshot",
        rows=tuple(_row(key) for key in ROW_ORDER),
        stats=ResultStats(),
    )
    base.update(overrides)
    return RunResultCard(**base)


def test_row_order_is_the_reading_order():
    assert ROW_ORDER == ("setup", "task", "build", "tests", "coverage", "ci", "report")


def test_every_row_key_has_a_label():
    assert set(ROW_LABELS) == set(ROW_ORDER)
    assert ROW_LABELS["task"] == "Required task"
    assert ROW_LABELS["ci"] == "Official CI"


def test_card_requires_exactly_the_seven_rows_in_order():
    card = _card()
    assert tuple(row.key for row in card.rows) == ROW_ORDER


def test_card_rejects_a_missing_row():
    rows = tuple(_row(key) for key in ROW_ORDER if key != "coverage")
    with pytest.raises(ValidationError, match="seven rows"):
        _card(rows=rows)


def test_card_rejects_rows_out_of_order():
    rows = tuple(_row(key) for key in reversed(ROW_ORDER))
    with pytest.raises(ValidationError, match="seven rows"):
        _card(rows=rows)


def test_row_finder_returns_the_named_row():
    card = _card()
    assert card.row("tests").key == "tests"


def test_card_tone_is_the_setup_rows_tone():
    rows = list(_row(key) for key in ROW_ORDER)
    rows[0] = ResultRow(
        key="setup", label="Setup", status="partial", tone="attention", headline="—"
    )
    assert _card(rows=tuple(rows)).tone == "attention"


def test_stats_default_to_stated_absence():
    stats = ResultStats()
    assert stats.turns is None
    assert stats.wall_clock_seconds is None


def test_models_are_frozen():
    row = _row("build")
    with pytest.raises(ValidationError):
        row.headline = "changed"


def test_attention_item_carries_its_refs():
    item = AttentionItem(kind="task_step", title="ci-step-1: failed", refs=("inv-maven-1-a-0001",))
    assert item.detail is None
    assert item.refs == ("inv-maven-1-a-0001",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/test_result_card_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.result_card.models'`.

- [ ] **Step 3: Write the models**

Create `src/sag/result_card/models.py`:

```python
"""The presentation model of a finished run. Shape only; no derivation here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

RowKey = Literal["setup", "task", "build", "tests", "coverage", "ci", "report"]
Tone = Literal["success", "attention", "failed", "neutral"]

# Reading order, fixed so three surfaces and successive runs line up.
ROW_ORDER: tuple[RowKey, ...] = (
    "setup",
    "task",
    "build",
    "tests",
    "coverage",
    "ci",
    "report",
)

ROW_LABELS: dict[RowKey, str] = {
    "setup": "Setup",
    "task": "Required task",
    "build": "Build",
    "tests": "Tests",
    "coverage": "Coverage",
    "ci": "Official CI",
    "report": "Report",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResultRow(_Frozen):
    """One measurement, stated once.

    ``status`` is the word the run recorded, never a synonym. ``reason`` is
    present exactly when the row has nothing to measure, so a reader never has
    to guess whether a blank means zero or means unknown.
    """

    key: RowKey
    label: str
    status: str
    tone: Tone
    headline: str
    detail: str | None = None
    reason: str | None = None
    items: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()


class ResultStats(_Frozen):
    """How the run went, as counts. Every field may be absent."""

    phases_completed: int | None = None
    phases_total: int | None = None
    turns: int | None = None
    tool_calls: int | None = None
    tool_failures: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    wall_clock_seconds: float | None = None
    model: str | None = None
    advisor_model: str | None = None


class AttentionItem(_Frozen):
    kind: Literal[
        "failing_tests",
        "task_step",
        "blocked_phase",
        "ci_finding",
        "build_warning",
        "report",
    ]
    title: str
    detail: str | None = None
    refs: tuple[str, ...] = ()


class RunResultCard(_Frozen):
    schema_version: Literal[1] = 1
    run_id: str
    project: str | None = None
    goal: str | None = None
    commit: str | None = None
    container: str | None = None
    session_dir: str | None = None
    verdict: Literal["success", "partial", "failed", "unknown"]
    verdict_source: Literal["snapshot", "legacy", "unavailable"]
    rows: tuple[ResultRow, ...]
    stats: ResultStats
    attention: tuple[AttentionItem, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _carries_every_row_once_in_order(self) -> "RunResultCard":
        keys = tuple(row.key for row in self.rows)
        if keys != ROW_ORDER:
            raise ValueError(
                f"a card states all seven rows in reading order; got {keys}"
            )
        return self

    @property
    def tone(self) -> Tone:
        """The whole card reads as its setup row: one word for the run."""

        return self.rows[0].tone

    def row(self, key: RowKey) -> ResultRow:
        for row in self.rows:
            if row.key == key:
                return row
        raise KeyError(key)


__all__ = [
    "ROW_LABELS",
    "ROW_ORDER",
    "AttentionItem",
    "ResultRow",
    "ResultStats",
    "RowKey",
    "RunResultCard",
    "Tone",
]
```

Extend `src/sag/result_card/__init__.py`:

```python
"""One presentation model of a finished run, rendered by three surfaces."""

from sag.result_card.glosses import REASON_GLOSS, gloss
from sag.result_card.models import (
    ROW_LABELS,
    ROW_ORDER,
    AttentionItem,
    ResultRow,
    ResultStats,
    RowKey,
    RunResultCard,
    Tone,
)

__all__ = [
    "REASON_GLOSS",
    "ROW_LABELS",
    "ROW_ORDER",
    "AttentionItem",
    "ResultRow",
    "ResultStats",
    "RowKey",
    "RunResultCard",
    "Tone",
    "gloss",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. uv run pytest tests/test_result_card_models.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Write the shared fixture builders**

Create `tests/result_card_fakes.py`:

```python
"""Snapshot and sibling-artifact fixtures for result-card tests.

Built as plain dicts so a test can state exactly the fields it is about and
leave the rest at a neutral default. ``snapshot_dict`` returns the v5 payload
``RunVerdictSnapshot.model_validate`` accepts.
"""

from __future__ import annotations

from typing import Any

from verdict_rate_fakes import complete_verdict_rates

CLEAN_TEST_COUNTS = {
    "executed": 994,
    "passed": 933,
    "failed": 0,
    "errors": 0,
    "skipped": 61,
}


def snapshot_dict(**overrides: Any) -> dict[str, Any]:
    """A complete v5 verdict payload; override any top-level key."""

    payload: dict[str, Any] = {
        "schema_version": 5,
        "run_id": "20260914_210609_965730_e39856b237f2_9183-7-953da846d195",
        "finalized_at": "2026-09-14T21:14:51Z",
        "input_refs": [],
        "verdict": "success",
        "build_evidence": {
            "observed": True,
            "green": True,
            "judgment": "success",
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "refs": [],
            "compiled_classes": 119,
            "source_files": 36,
            "reactor_modules_succeeded": 1,
            "reactor_modules_total": 1,
        },
        "test_stats": {
            "discovered": 472,
            "denominator_basis": "complete",
            "unique": dict(CLEAN_TEST_COUNTS),
            "raw": dict(CLEAN_TEST_COUNTS),
            "flaky_count": 0,
            "judgment": "success",
            "collection_errors": 0,
            "receipt_scoped": True,
        },
        "rates": complete_verdict_rates("success"),
        "conflicts": [],
        "phase_records": [],
        "task_completion": {
            "run_id": "20260914_210609_965730_e39856b237f2_9183-7-953da846d195",
            "task_sha256": "a" * 64,
            "status": "complete",
            "steps": [
                {
                    "id": "smoke-build-test",
                    "command": "mvn clean verify",
                    "status": "complete",
                    "receipt_id": "inv-maven-1-ee86ae186d94-0002",
                    "exit_code": 0,
                    "reason": None,
                }
            ],
            "reasons": [],
        },
        "ci_comparison": {
            "schema_version": 1,
            "status": "no_matched_cell",
            "run_id": "20260914_210609_965730_e39856b237f2_9183-7-953da846d195",
            "repo": "apache/commons-cli",
            "target_sha": "e17111798da51037659b3594d9c0b3b525040081",
            "target_record_sha256": None,
            "certificate_input_sha256": None,
            "certificate": None,
            "attainment": None,
            "receipt_ids": [],
            "commands": [],
            "acceptance_command": None,
            "test_identity_basis": None,
            "reasons": ["official_ci_cell_not_matched"],
        },
    }
    payload.update(overrides)
    return payload


def phase_record(phase: str, *, outcome: str = "success", **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "phase": phase,
        "attempt_id": f"{phase}-1",
        "termination": "completed",
        "outcome": outcome,
        "transition": "advance",
        "key_results": "",
        "reason": "",
        "evidence": [],
        "evidence_refs": [],
        "claim": None,
        "validated_outcome": outcome,
        "claim_disposition": "confirmed",
        "legacy_claim": False,
        "prerequisite_ref": "",
    }
    record.update(overrides)
    return record


def attainment(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "verdict": "met",
        "cell_id": "Apache Jenkins commons-dbutils Linux JDK 17 #455",
        "cell_grade": "A",
        "valid": True,
        "target_usable": True,
        "clean": True,
        "clean_form": "ids",
        "built": True,
        "alpha": {"numerator": 523, "denominator": 523},
        "alpha_test": {"numerator": 523, "denominator": 523},
        "alpha_build": {"numerator": 1, "denominator": 1},
        "executed_observed": 523,
        "executed_target": 523,
        "red_observed": 0,
        "red_target": 0,
        "modules_matched": 1,
        "modules_target": 1,
        "missing_module_ids": [],
        "build_form": "modules",
        "modules_basis": "log",
        "unmatched_observed_module_ids": [],
        "lifecycle_parity": {
            "status": "equivalent",
            "form": "maven_phases",
            "ci_command": "mvn -B -f pom.xml -V clean test --batch-mode",
            "sag_commands": ["/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"],
            "ci_reach": "test",
            "sag_reach": "test",
            "missing": [],
            "extra": [],
        },
        "unexpected_red_ids": [],
        "reason_codes": [],
    }
    payload.update(overrides)
    return payload


def module_metrics(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "generated_at": "2026-09-14T21:14:44Z",
        "module_summary": {
            "modules_total": 1,
            "modules_built": 1,
            "modules_failed": 0,
            "modules_skipped": 0,
            "modules_tested": 1,
            "modules_not_tested": 0,
            "modules_test_bearing": 1,
            "modules_with_test_failures": 0,
            "build_systems": ["maven"],
            "single_module": True,
        },
        "modules": [
            {
                "name": "commons-cli",
                "path": ".",
                "build_status": "success",
                "build_source": "reactor",
                "class_count": 119,
                "jar_count": 4,
                "build_warnings": 0,
                "build_error_samples": [],
                "tests_total": 994,
                "tests_passed": 933,
                "tests_failed": 0,
                "tests_errors": 0,
                "tests_skipped": 61,
                "test_source": "runner_xml",
                "has_test_sources": True,
                "test_bearing_evidence": ["source_tree", "runner_xml"],
                "failing_names": [],
                "failing_count": 0,
                "evidence_refs": [],
            }
        ],
    }
    payload.update(overrides)
    return payload


__all__ = [
    "CLEAN_TEST_COUNTS",
    "attainment",
    "module_metrics",
    "phase_record",
    "snapshot_dict",
]
```

- [ ] **Step 6: Verify the fixtures produce a valid snapshot**

Run: `PYTHONPATH=.:tests uv run python -c "from sag.agent.verdict_finalizer import RunVerdictSnapshot; import result_card_fakes as f; s = RunVerdictSnapshot.model_validate(f.snapshot_dict()); print(s.verdict, s.test_stats.unique.executed, s.task_completion.status)"`
Expected: `success 994 complete`

- [ ] **Step 7: Commit**

```bash
git add src/sag/result_card tests/test_result_card_models.py tests/result_card_fakes.py
git commit -m "feat(result-card): the card's shape, with seven rows in one reading order"
```

---

### Task 3: Setup and task rows

**Files:**
- Create: `src/sag/result_card/rows.py`
- Test: `tests/test_result_card_rows.py`

**Interfaces:**
- Consumes: `sag.result_card.models` (Task 2), `sag.result_card.glosses.gloss` (Task 1), `tests/result_card_fakes.py` (Task 2).
- Produces: `setup_row(snapshot, *, stats, termination) -> ResultRow`; `task_row(snapshot) -> ResultRow`; helpers `_duration_text(seconds: float | None) -> str | None`, `_join(*parts: str | None) -> str`.

`snapshot` is always the `RunVerdictSnapshot` model (not a dict) in `rows.py`; `build_result_card` in Task 6 does the dict-to-model coercion once.

- [ ] **Step 1: Write the failing test**

Create `tests/test_result_card_rows.py`:

```python
"""Each row states one measurement in the run's own words."""

from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
)
from sag.result_card.models import ResultStats
from sag.result_card.rows import setup_row, task_row

from result_card_fakes import phase_record, snapshot_dict


def _snapshot(**overrides) -> RunVerdictSnapshot:
    return RunVerdictSnapshot.model_validate(snapshot_dict(**overrides))


def _termination(
    status: RunTerminationStatus = RunTerminationStatus.COMPLETED,
    delivery: ReportDeliveryStatus = ReportDeliveryStatus.DELIVERED,
) -> RunTermination:
    return RunTermination(termination=status, report_delivery_status=delivery)


def test_setup_row_counts_the_run():
    stats = ResultStats(
        phases_completed=5, phases_total=5, turns=12, tool_calls=20, wall_clock_seconds=390.2
    )
    row = setup_row(_snapshot(), stats=stats, termination=_termination())
    assert row.status == "success"
    assert row.tone == "success"
    assert row.headline == "5/5 phases · 12 turns · 20 tool calls · 6m 30s"
    assert row.detail is None
    assert row.reason is None


def test_setup_row_drops_pieces_it_does_not_have():
    row = setup_row(_snapshot(), stats=ResultStats(turns=3), termination=_termination())
    assert row.headline == "3 turns"


def test_setup_row_says_so_when_it_counted_nothing():
    row = setup_row(_snapshot(), stats=ResultStats(), termination=_termination())
    assert row.headline == "no run counts were recorded"


def test_setup_row_names_an_abnormal_ending():
    row = setup_row(
        _snapshot(verdict="partial"),
        stats=ResultStats(turns=9),
        termination=_termination(RunTerminationStatus.ABORTED),
    )
    assert row.status == "partial"
    assert row.tone == "attention"
    assert row.detail == "the run was aborted"


def test_setup_row_names_a_blocked_phase():
    row = setup_row(
        _snapshot(
            verdict="partial",
            phase_records=[
                phase_record("provision"),
                phase_record(
                    "build",
                    outcome="failed",
                    termination="blocked",
                    reason="enforcer rejected Maven 3.8.7",
                ),
            ],
        ),
        stats=ResultStats(turns=9),
        termination=_termination(),
    )
    assert row.detail == "blocked at build: enforcer rejected Maven 3.8.7"


def test_failed_verdict_is_a_failed_tone():
    row = setup_row(_snapshot(verdict="failed"), stats=ResultStats(), termination=_termination())
    assert row.tone == "failed"


def test_unknown_verdict_asks_for_attention():
    row = setup_row(_snapshot(verdict="unknown"), stats=ResultStats(), termination=_termination())
    assert row.tone == "attention"


def test_task_row_reports_a_complete_task():
    row = task_row(_snapshot())
    assert row.status == "complete"
    assert row.tone == "success"
    assert row.headline == "complete 1/1 steps"
    assert row.detail == "mvn clean verify → exit 0"
    assert row.items == ("smoke-build-test: complete — mvn clean verify → exit 0",)
    assert row.refs == ("inv-maven-1-ee86ae186d94-0002",)


def test_task_row_reports_a_failed_step_with_its_reason():
    snapshot = _snapshot(
        verdict="partial",
        task_completion={
            "run_id": "r",
            "task_sha256": "b" * 64,
            "status": "incomplete",
            "steps": [
                {
                    "id": "ci-step-1",
                    "command": "mvn -B clean test",
                    "status": "failed",
                    "receipt_id": "inv-maven-1-abc-0001",
                    "exit_code": 1,
                    "reason": "Required command returned a nonzero exit code.",
                },
                {
                    "id": "ci-step-2",
                    "command": "mvn -B verify",
                    "status": "missing",
                    "receipt_id": None,
                    "exit_code": None,
                    "reason": None,
                },
            ],
            "reasons": [],
        },
    )
    row = task_row(snapshot)
    assert row.status == "incomplete"
    assert row.tone == "failed"
    assert row.headline == "incomplete 0/2 steps"
    assert row.items == (
        "ci-step-1: failed — mvn -B clean test → exit 1; "
        "Required command returned a nonzero exit code.",
        "ci-step-2: missing — mvn -B verify",
    )


def test_task_row_glosses_its_unavailable_reason():
    snapshot = _snapshot(
        verdict="partial",
        task_completion={
            "run_id": "r",
            "task_sha256": None,
            "status": "unavailable",
            "steps": [],
            "reasons": ["task_current_checkout_mismatch"],
        },
    )
    row = task_row(snapshot)
    assert row.status == "unavailable"
    assert row.tone == "attention"
    assert row.headline == "step definition unavailable"
    assert row.reason == (
        "the workspace is not at the task's frozen commit (task_current_checkout_mismatch)"
    )


def test_task_row_is_neutral_when_no_task_was_supplied():
    snapshot_payload = snapshot_dict()
    snapshot_payload.pop("task_completion")
    row = task_row(RunVerdictSnapshot.model_validate(snapshot_payload))
    assert row.status == "not supplied"
    assert row.tone == "neutral"
    assert row.headline == "no required task was supplied"
    assert row.reason == "run with --acceptance-task-file to require specific commands"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_rows.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.result_card.rows'`.

- [ ] **Step 3: Write the setup and task row builders**

Create `src/sag/result_card/rows.py`:

```python
"""One builder per row. Each copies what the run recorded and states absence.

No builder computes a judgment: a status word always comes from the seal, and
a count always comes from the seal or a sealed sibling artifact.
"""

from __future__ import annotations

from typing import Any

from sag.result_card.glosses import gloss
from sag.result_card.models import ROW_LABELS, ResultRow, ResultStats, Tone

_VERDICT_TONE: dict[str, Tone] = {
    "success": "success",
    "partial": "attention",
    "failed": "failed",
    "unknown": "attention",
}

_TASK_TONE: dict[str, Tone] = {
    "complete": "success",
    "incomplete": "failed",
    "unavailable": "attention",
    "not supplied": "neutral",
}

NOT_SUPPLIED = "not supplied"


def _join(*parts: str | None) -> str:
    return " · ".join(part for part in parts if part)


def _duration_text(seconds: float | None) -> str | None:
    """`6m 30s`, `41.0s`, `1h 04m`. Sub-minute keeps a decimal; hours drop seconds."""

    if seconds is None or seconds < 0:
        return None
    total = int(round(seconds))
    if total < 60:
        return f"{seconds:.1f}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _first_reason(reasons: Any) -> str | None:
    for reason in reasons or ():
        text = str(reason).strip()
        if text:
            return f"{gloss(text)} ({text})"
    return None


def setup_row(snapshot: Any, *, stats: ResultStats, termination: Any | None) -> ResultRow:
    """How the run went, as counts, plus anything unusual about how it ended."""

    verdict = str(snapshot.verdict)
    phases = None
    if stats.phases_total:
        phases = f"{stats.phases_completed or 0}/{stats.phases_total} phases"
    headline = _join(
        phases,
        f"{stats.turns:,} turns" if stats.turns is not None else None,
        f"{stats.tool_calls:,} tool calls" if stats.tool_calls is not None else None,
        _duration_text(stats.wall_clock_seconds),
    )
    detail = None
    ending = getattr(getattr(termination, "termination", None), "value", None)
    if ending and ending != "completed":
        detail = f"the run was {ending}"
    else:
        for record in getattr(snapshot, "phase_records", ()) or ():
            if str(getattr(record, "termination", "")) not in {"completed", ""}:
                reason = str(getattr(record, "reason", "") or "").strip()
                detail = f"blocked at {record.phase}"
                if reason:
                    detail = f"{detail}: {reason}"
                break
    return ResultRow(
        key="setup",
        label=ROW_LABELS["setup"],
        status=verdict,
        tone=_VERDICT_TONE.get(verdict, "attention"),
        headline=headline or "no run counts were recorded",
        detail=detail,
    )


def task_row(snapshot: Any) -> ResultRow:
    """Whether the commands the run was required to execute actually ran."""

    completion = getattr(snapshot, "task_completion", None)
    if completion is None:
        return ResultRow(
            key="task",
            label=ROW_LABELS["task"],
            status=NOT_SUPPLIED,
            tone="neutral",
            headline="no required task was supplied",
            reason="run with --acceptance-task-file to require specific commands",
        )

    status = str(completion.status)
    steps = tuple(completion.steps or ())
    complete = sum(1 for step in steps if step.status == "complete")
    items: list[str] = []
    refs: list[str] = []
    for step in steps:
        exit_text = f" → exit {step.exit_code}" if step.exit_code is not None else ""
        line = f"{step.id}: {step.status} — {step.command}{exit_text}"
        if step.reason:
            line = f"{line}; {step.reason}"
        items.append(line)
        if step.receipt_id:
            refs.append(step.receipt_id)

    if steps:
        headline = f"{status} {complete}/{len(steps)} steps"
        first = steps[0]
        exit_text = f" → exit {first.exit_code}" if first.exit_code is not None else ""
        detail = f"{first.command}{exit_text}" if first.receipt_id else None
    else:
        headline = "step definition unavailable"
        detail = None

    return ResultRow(
        key="task",
        label=ROW_LABELS["task"],
        status=status,
        tone=_TASK_TONE.get(status, "attention"),
        headline=headline,
        detail=detail,
        reason=_first_reason(getattr(completion, "reasons", ())),
        items=tuple(items),
        refs=tuple(refs),
    )


__all__ = ["NOT_SUPPLIED", "setup_row", "task_row"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_rows.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sag/result_card/rows.py tests/test_result_card_rows.py
git commit -m "feat(result-card): setup and required-task rows"
```

---

### Task 4: Build, tests and coverage rows

**Files:**
- Modify: `src/sag/result_card/rows.py`
- Test: `tests/test_result_card_rows.py` (append)

**Interfaces:**
- Consumes: Task 3's `rows.py` helpers.
- Produces: `build_row(snapshot, *, module_metrics=None) -> ResultRow`; `tests_row(snapshot, *, report_metrics=None) -> ResultRow`; `coverage_row(snapshot) -> ResultRow`.

`module_metrics` and `report_metrics` are the parsed JSON dicts (shapes in `tests/result_card_fakes.py`), or `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_result_card_rows.py`:

```python
from sag.result_card.rows import build_row, coverage_row, tests_row

from result_card_fakes import CLEAN_TEST_COUNTS, module_metrics


def test_build_row_counts_modules_and_names_its_diagnostics():
    row = build_row(_snapshot(), module_metrics=module_metrics())
    assert row.status == "success"
    assert row.tone == "success"
    assert row.headline == "1/1 modules built"
    assert row.detail == (
        "119 class files · 4 jars · counts are diagnostic, CI defines scope"
    )


def test_build_row_without_a_reactor_count_states_the_word():
    snapshot = _snapshot(
        build_evidence={
            "observed": True,
            "green": False,
            "judgment": "failed",
            "source": "physical",
            "outcome": "failed",
            "evidence_status": "verified",
            "refs": [],
            "compiled_classes": None,
        }
    )
    row = build_row(snapshot)
    assert row.status == "failed"
    assert row.tone == "failed"
    assert row.headline == "failed"
    assert row.detail is None


def test_build_row_unknown_judgment_states_a_reason():
    snapshot = _snapshot(
        verdict="unknown",
        build_evidence={
            "observed": False,
            "green": False,
            "judgment": "unknown",
            "source": "none",
            "outcome": "unknown",
            "evidence_status": "unknown",
            "refs": [],
        },
    )
    row = build_row(snapshot)
    assert row.status == "unknown"
    assert row.reason == "no build result was recorded for this run"


def test_tests_row_reports_counts_and_the_non_skipped_rate():
    row = tests_row(_snapshot())
    assert row.status == "executed"
    assert row.tone == "success"
    assert row.headline == "994 executed · 933 passed · 0 failed · 0 errors · 61 skipped"
    assert row.detail == "100% of non-skipped passed"


def test_tests_row_never_rounds_red_up_to_a_full_hundred():
    counts = {"executed": 10_000, "passed": 9_999, "failed": 0, "errors": 1, "skipped": 0}
    snapshot = _snapshot(
        verdict="partial",
        test_stats={
            "discovered": 10_000,
            "denominator_basis": "complete",
            "unique": counts,
            "raw": counts,
            "flaky_count": 0,
            "judgment": "success",
            "receipt_scoped": True,
        },
    )
    row = tests_row(snapshot)
    assert row.tone == "attention"
    assert row.detail == "<100% of non-skipped passed"


def test_tests_row_notes_raw_executions_when_they_differ():
    raw = {"executed": 1_200, "passed": 1_139, "failed": 0, "errors": 0, "skipped": 61}
    snapshot = _snapshot(
        test_stats={
            "discovered": 472,
            "denominator_basis": "complete",
            "unique": dict(CLEAN_TEST_COUNTS),
            "raw": raw,
            "flaky_count": 0,
            "judgment": "success",
            "receipt_scoped": True,
        }
    )
    row = tests_row(snapshot)
    assert row.detail == "100% of non-skipped passed · 1,200 raw executions"


def test_tests_row_states_a_lower_bound_from_the_evidence_accounting():
    report_metrics = {
        "schema_version": 2,
        "tests": {
            "claimed": {
                "receipt_executions": {
                    "executed": 2048,
                    "passed": 2000,
                    "failed": 48,
                    "errors": 0,
                    "skipped": 0,
                    "availability": "partial",
                    "bound": "lower",
                    "reason": "row disclosure truncated",
                }
            }
        },
    }
    row = tests_row(_snapshot(), report_metrics=report_metrics)
    assert row.headline.startswith("≥994 executed")


def test_tests_row_says_when_no_tests_ran():
    zero = {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    snapshot = _snapshot(
        verdict="partial",
        test_stats={
            "discovered": 472,
            "denominator_basis": "complete",
            "unique": zero,
            "raw": zero,
            "flaky_count": 0,
            "judgment": "unknown",
            "receipt_scoped": True,
        },
    )
    row = tests_row(snapshot)
    assert row.status == "unavailable"
    assert row.tone == "attention"
    assert row.headline == "no test results were recorded"
    assert row.reason == "the run recorded no test outcomes"


def test_interrupted_tests_keep_their_prefix_counts():
    counts = {"executed": 120, "passed": 118, "failed": 2, "errors": 0, "skipped": 0}
    snapshot = _snapshot(
        verdict="partial",
        test_stats={
            "discovered": 472,
            "denominator_basis": "partial",
            "unique": counts,
            "raw": counts,
            "flaky_count": 0,
            "judgment": "partial",
            "receipt_scoped": True,
        },
    )
    row = tests_row(snapshot)
    assert row.status == "interrupted"
    assert row.tone == "attention"
    assert row.headline == "120 executed · 118 passed · 2 failed · 0 errors · 0 skipped"


def test_coverage_row_reports_a_collected_rate():
    rates = snapshot_dict()["rates"]
    rates["coverage"] = {"line_rate": 54.2, "source": "jacoco", "status": "collected"}
    row = coverage_row(_snapshot(rates=rates))
    assert row.status == "collected"
    assert row.headline == "54.2% line coverage"
    assert row.detail == "source jacoco"
    assert row.tone == "neutral"


def test_coverage_row_names_why_nothing_was_collected():
    row = coverage_row(_snapshot())
    assert row.status == "not collected"
    assert row.headline == "not collected"
    assert row.reason == "fixture coverage not collected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_rows.py -v -k "build_row or tests_row or coverage_row"`
Expected: FAIL with `ImportError: cannot import name 'build_row'`.

- [ ] **Step 3: Write the three builders**

Append to `src/sag/result_card/rows.py` (and add `ResultRow` usages; no new imports needed beyond `Mapping`):

```python
_TEST_JUDGMENT_WORD = {
    "success": "executed",
    "partial": "interrupted",
    "failed": "failed to run",
    "unknown": "unavailable",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def build_row(snapshot: Any, *, module_metrics: Any = None) -> ResultRow:
    """What compiled, with the module count marked as the diagnostic it is."""

    evidence = snapshot.build_evidence
    judgment = str(evidence.judgment)
    succeeded = evidence.reactor_modules_succeeded
    total = evidence.reactor_modules_total
    if total:
        headline = f"{succeeded or 0}/{total} modules built"
    else:
        headline = judgment

    rollup = _mapping(_mapping(module_metrics).get("module_summary"))
    jars = None
    for module in _mapping(module_metrics).get("modules") or ():
        count = module.get("jar_count") if isinstance(module, dict) else None
        if isinstance(count, int):
            jars = (jars or 0) + count
    pieces = []
    if evidence.compiled_classes is not None:
        pieces.append(f"{evidence.compiled_classes:,} class files")
    if jars is not None:
        pieces.append(f"{jars:,} jars")
    if rollup.get("modules_failed"):
        pieces.append(f"{rollup['modules_failed']:,} modules failed")
    detail = _join(*pieces)
    if detail:
        detail = f"{detail} · counts are diagnostic, CI defines scope"

    reason = None
    if judgment == "unknown":
        reason = "no build result was recorded for this run"

    return ResultRow(
        key="build",
        label=ROW_LABELS["build"],
        status=judgment,
        tone=_VERDICT_TONE.get(judgment, "attention"),
        headline=headline,
        detail=detail or None,
        reason=reason,
        refs=tuple(evidence.refs or ())[:12],
    )


def _lower_bounded(report_metrics: Any) -> bool:
    """True when the run's own accounting says its execution total is a floor."""

    layers = _mapping(_mapping(report_metrics).get("tests"))
    executions = _mapping(_mapping(layers.get("claimed")).get("receipt_executions"))
    return executions.get("availability") == "partial" and executions.get("bound") == "lower"


def tests_row(snapshot: Any, *, report_metrics: Any = None) -> ResultRow:
    """How many tests ran and how they came out, with skips out of the rate."""

    stats = snapshot.test_stats
    unique = stats.unique
    raw = stats.raw
    judgment = str(stats.judgment)
    status = _TEST_JUDGMENT_WORD.get(judgment, "unavailable")

    if unique.executed <= 0:
        return ResultRow(
            key="tests",
            label=ROW_LABELS["tests"],
            status="unavailable",
            tone="attention",
            headline="no test results were recorded",
            reason="the run recorded no test outcomes",
        )

    bound = "≥" if _lower_bounded(report_metrics) else ""
    headline = (
        f"{bound}{unique.executed:,} executed · {unique.passed:,} passed · "
        f"{unique.failed:,} failed · {unique.errors:,} errors · {unique.skipped:,} skipped"
    )

    non_skipped = unique.passed + unique.failed + unique.errors
    red = unique.failed + unique.errors
    rate_text = None
    if non_skipped > 0:
        percent = unique.passed / non_skipped * 100.0
        if red and percent >= 99.95:
            rate_text = "<100% of non-skipped passed"
        elif percent >= 99.95:
            rate_text = "100% of non-skipped passed"
        else:
            rate_text = f"{percent:.1f}% of non-skipped passed"
    raw_text = None
    if raw.executed and raw.executed != unique.executed:
        raw_text = f"{raw.executed:,} raw executions"

    if status == "failed to run":
        tone: Tone = "failed"
    elif status == "interrupted" or red > 0 or status == "unavailable":
        tone = "attention"
    else:
        tone = "success"

    return ResultRow(
        key="tests",
        label=ROW_LABELS["tests"],
        status=status,
        tone=tone,
        headline=headline,
        detail=_join(rate_text, raw_text) or None,
    )


def coverage_row(snapshot: Any) -> ResultRow:
    """Line coverage when it was collected, and why not when it was not."""

    coverage = _mapping(_mapping(snapshot.rates).get("coverage"))
    if coverage.get("status") == "collected" and coverage.get("line_rate") is not None:
        source = str(coverage.get("source") or "").strip()
        return ResultRow(
            key="coverage",
            label=ROW_LABELS["coverage"],
            status="collected",
            tone="neutral",
            headline=f"{float(coverage['line_rate']):g}% line coverage",
            detail=f"source {source}" if source else None,
        )
    reason = str(coverage.get("reason") or "").strip() or "coverage was not collected"
    return ResultRow(
        key="coverage",
        label=ROW_LABELS["coverage"],
        status="not collected",
        tone="neutral",
        headline="not collected",
        reason=reason,
    )
```

Extend the module's `__all__` to `["NOT_SUPPLIED", "build_row", "coverage_row", "setup_row", "task_row", "tests_row"]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_rows.py -v`
Expected: PASS, 22 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sag/result_card/rows.py tests/test_result_card_rows.py
git commit -m "feat(result-card): build, tests and coverage rows"
```

---

### Task 5: Official CI and report rows

**Files:**
- Modify: `src/sag/result_card/rows.py`
- Test: `tests/test_result_card_rows.py` (append)

**Interfaces:**
- Consumes: Task 3 and 4 helpers.
- Produces: `ci_row(snapshot) -> ResultRow`; `report_row(termination, *, report_path=None) -> ResultRow`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_result_card_rows.py`:

```python
from sag.result_card.rows import ci_row, report_row

from result_card_fakes import attainment


def _evaluated(**overrides) -> dict:
    comparison = dict(snapshot_dict()["ci_comparison"])
    comparison.update(
        {
            "status": "evaluated",
            "attainment": attainment(**overrides),
            "acceptance_command": "mvn -B -f pom.xml -V clean test --batch-mode",
            "receipt_ids": ["inv-maven-1-17c8a2e62d8a-0001"],
            "reasons": [],
        }
    )
    return comparison


def test_ci_row_reports_a_met_comparison_with_its_denominator():
    row = ci_row(_snapshot(ci_comparison=_evaluated()))
    assert row.status == "met"
    assert row.tone == "success"
    assert row.headline == "met 523/523"
    assert row.detail == (
        'cell "Apache Jenkins commons-dbutils Linux JDK 17 #455" · lifecycle equivalent'
    )


def test_ci_row_names_missing_lifecycle_phases():
    comparison = _evaluated(
        lifecycle_parity={
            "status": "not_equivalent",
            "form": "maven_phases",
            "ci_command": "mvn -V verify",
            "sag_commands": ["mvn test"],
            "ci_reach": "verify",
            "sag_reach": "test",
            "missing": ["package", "verify"],
            "extra": [],
        }
    )
    row = ci_row(_snapshot(ci_comparison=comparison))
    assert row.detail.endswith("lifecycle not_equivalent · missing package, verify")


def test_ci_row_without_a_scope_score_says_so():
    comparison = _evaluated(verdict="partial", alpha=None, build_form="conclusion")
    row = ci_row(_snapshot(ci_comparison=comparison))
    assert row.headline == "partial · scope score unavailable"
    assert row.tone == "attention"


def test_ci_row_lists_findings_with_their_glosses():
    comparison = _evaluated(
        verdict="not_met",
        clean=False,
        red_observed=3,
        unexpected_red_ids=["a.B#c", "a.B#d", "a.B#e"],
        reason_codes=["NEW_RED_BEYOND_TARGET"],
    )
    row = ci_row(_snapshot(ci_comparison=comparison))
    assert row.tone == "failed"
    assert row.items[0] == "NEW_RED_BEYOND_TARGET: tests failed here that pass in CI"
    assert row.items[1] == "red beyond CI: 3 tests"
    assert "a.B#c" in row.items[2]


def test_ci_row_not_compared_explains_itself():
    row = ci_row(_snapshot())
    assert row.status == "not compared"
    assert row.tone == "neutral"
    assert row.headline == "not compared"
    assert row.reason == (
        "no CI job on this commit matches the run's JDK and OS (official_ci_cell_not_matched)"
    )


def test_ci_row_without_any_comparison_at_all():
    payload = snapshot_dict()
    payload.pop("ci_comparison")
    row = ci_row(RunVerdictSnapshot.model_validate(payload))
    assert row.status == "not compared"
    assert row.reason == "no CI job was supplied to compare against"


def test_report_row_points_at_the_delivered_file():
    row = report_row(_termination(), report_path="logs/session_x/setup-report-1.md")
    assert row.status == "delivered"
    assert row.tone == "neutral"
    assert row.headline == "logs/session_x/setup-report-1.md"
    assert row.refs == ("logs/session_x/setup-report-1.md",)


def test_report_row_flags_a_failed_delivery():
    row = report_row(_termination(delivery=ReportDeliveryStatus.FAILED))
    assert row.status == "failed"
    assert row.tone == "attention"
    assert row.headline == "the setup report was not written"
    assert row.reason == "the run result itself is unchanged"


def test_report_row_without_a_termination_is_unavailable():
    row = report_row(None)
    assert row.status == "unavailable"
    assert row.reason == "the run did not record whether a report was written"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_rows.py -v -k "ci_row or report_row"`
Expected: FAIL with `ImportError: cannot import name 'ci_row'`.

- [ ] **Step 3: Write the two builders**

Append to `src/sag/result_card/rows.py`:

```python
_CI_TONE: dict[str, Tone] = {
    "met": "success",
    "exceeded": "success",
    "partial": "attention",
    "not_met": "failed",
    "invalid": "failed",
}

_MAX_RED_IDS = 10

NOT_COMPARED = "not compared"

_REPORT_TONE: dict[str, Tone] = {
    "delivered": "neutral",
    "skipped": "neutral",
    "failed": "attention",
    "unavailable": "attention",
}


def ci_row(snapshot: Any) -> ResultRow:
    """How this run measures against the project's own CI on the same commit."""

    comparison = getattr(snapshot, "ci_comparison", None)
    if comparison is None:
        return ResultRow(
            key="ci",
            label=ROW_LABELS["ci"],
            status=NOT_COMPARED,
            tone="neutral",
            headline=NOT_COMPARED,
            reason=gloss("official_ci_target_not_supplied"),
        )

    result = getattr(comparison, "attainment", None)
    if str(comparison.status) != "evaluated" or result is None:
        return ResultRow(
            key="ci",
            label=ROW_LABELS["ci"],
            status=NOT_COMPARED,
            tone="neutral",
            headline=NOT_COMPARED,
            reason=_first_reason(getattr(comparison, "reasons", ()))
            or "the comparison produced no result",
        )

    verdict = str(result.verdict)
    alpha = getattr(result, "alpha", None)
    if alpha is not None:
        headline = f"{verdict} {alpha.numerator:,}/{alpha.denominator:,}"
    else:
        headline = f"{verdict} · scope score unavailable"

    parity = getattr(result, "lifecycle_parity", None)
    detail_parts = []
    if result.cell_id:
        detail_parts.append(f'cell "{result.cell_id}"')
    if parity is not None:
        parity_text = f"lifecycle {parity.status}"
        if parity.missing:
            parity_text = f"{parity_text} · missing {', '.join(parity.missing)}"
        if parity.extra:
            parity_text = f"{parity_text} · extra {', '.join(parity.extra)}"
        detail_parts.append(parity_text)

    items = [f"{code}: {gloss(code)}" for code in getattr(result, "reason_codes", ()) or ()]
    red_ids = tuple(getattr(result, "unexpected_red_ids", ()) or ())
    if red_ids:
        items.append(f"red beyond CI: {len(red_ids):,} tests")
        shown = ", ".join(red_ids[:_MAX_RED_IDS])
        if len(red_ids) > _MAX_RED_IDS:
            shown = f"{shown}, +{len(red_ids) - _MAX_RED_IDS} more"
        items.append(shown)

    return ResultRow(
        key="ci",
        label=ROW_LABELS["ci"],
        status=verdict,
        tone=_CI_TONE.get(verdict, "attention"),
        headline=headline,
        detail=_join(*detail_parts) or None,
        items=tuple(items),
        refs=tuple(getattr(comparison, "receipt_ids", ()) or ()),
    )


def report_row(termination: Any | None, *, report_path: str | None = None) -> ResultRow:
    """Whether the written setup report exists, and where."""

    status = getattr(getattr(termination, "report_delivery_status", None), "value", None)
    if status is None:
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status="unavailable",
            tone="attention",
            headline="no report was recorded",
            reason="the run did not record whether a report was written",
        )
    if status == "delivered":
        headline = report_path or "written inside the container"
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status=status,
            tone="neutral",
            headline=headline,
            refs=(report_path,) if report_path else (),
        )
    if status == "failed":
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status=status,
            tone="attention",
            headline="the setup report was not written",
            reason="the run result itself is unchanged",
        )
    return ResultRow(
        key="report",
        label=ROW_LABELS["report"],
        status=status,
        tone="neutral",
        headline="no report was requested",
    )
```

Extend `__all__` to include `"ci_row"`, `"report_row"`, `"NOT_COMPARED"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_rows.py -v`
Expected: PASS, 31 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sag/result_card/rows.py tests/test_result_card_rows.py
git commit -m "feat(result-card): official-CI and report rows"
```

---

### Task 6: Assemble the card

**Files:**
- Create: `src/sag/result_card/build.py`
- Modify: `src/sag/result_card/__init__.py`
- Test: `tests/test_result_card_build.py`

**Interfaces:**
- Consumes: Tasks 2-5.
- Produces: `build_result_card(snapshot, *, module_metrics=None, report_metrics=None, run_pin=None, token_usage=None, trajectory_session=None, turn_count=None, tool_calls=None, tool_failures=None, termination=None, project=None, container=None, session_dir=None, report_path=None, goal=None, commit=None) -> RunResultCard`.

`trajectory_session` is the trajectory document's `session` mapping (`{"wall_clock_seconds": float, ...}`) or `None`; the caller supplies `turn_count` / `tool_calls` / `tool_failures` because only it knows whether reading a ledger is affordable. This keeps `result_card` free of any dependency on `sag.trajectory`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_result_card_build.py`:

```python
"""Assembly: seven rows, what needs attention, and what the run noted."""

from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
)
from sag.result_card.build import build_result_card
from sag.result_card.models import ROW_ORDER

from result_card_fakes import module_metrics, phase_record, snapshot_dict


def _termination(delivery=ReportDeliveryStatus.DELIVERED) -> RunTermination:
    return RunTermination(
        termination=RunTerminationStatus.COMPLETED, report_delivery_status=delivery
    )


def test_card_from_a_clean_run():
    card = build_result_card(
        snapshot_dict(),
        module_metrics=module_metrics(),
        termination=_termination(),
        project="commons-cli",
        container="sag-commons-cli",
        turn_count=12,
        tool_calls=20,
        tool_failures=2,
        trajectory_session={"wall_clock_seconds": 390.2},
    )
    assert card.verdict == "success"
    assert card.verdict_source == "snapshot"
    assert tuple(row.key for row in card.rows) == ROW_ORDER
    assert card.tone == "success"
    assert card.stats.turns == 12
    assert card.stats.tool_failures == 2
    assert card.row("setup").headline == "12 turns · 20 tool calls · 6m 30s"
    assert card.attention == ()
    assert card.notes == ()


def test_card_accepts_a_model_as_well_as_a_dict():
    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict())
    assert build_result_card(snapshot).verdict == "success"


def test_phase_counts_come_from_the_phase_records():
    card = build_result_card(
        snapshot_dict(
            phase_records=[
                phase_record("provision"),
                phase_record("analyze"),
                phase_record("build", outcome="failed", termination="blocked", reason="no JDK"),
            ]
        )
    )
    assert card.stats.phases_total == 3
    assert card.stats.phases_completed == 2


def test_model_names_come_from_the_run_pin():
    card = build_result_card(
        snapshot_dict(),
        run_pin={"action_model": "gpt-5.4-mini", "advisor": {"model": "openai/gpt-5.6-terra"}},
    )
    assert card.stats.model == "gpt-5.4-mini"
    assert card.stats.advisor_model == "openai/gpt-5.6-terra"


def test_tokens_come_from_the_token_usage_rows():
    card = build_result_card(
        snapshot_dict(),
        token_usage=[{"prompt_tokens": 100, "output_tokens": 7}, {"prompt_tokens": 50}],
    )
    assert card.stats.tokens_in == 150
    assert card.stats.tokens_out == 7


def test_attention_leads_with_incomplete_task_steps():
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            task_completion={
                "run_id": "r",
                "task_sha256": "c" * 64,
                "status": "incomplete",
                "steps": [
                    {
                        "id": "ci-step-1",
                        "command": "mvn verify",
                        "status": "failed",
                        "receipt_id": "inv-1",
                        "exit_code": 1,
                        "reason": "Required command returned a nonzero exit code.",
                    }
                ],
                "reasons": [],
            },
        )
    )
    first = card.attention[0]
    assert first.kind == "task_step"
    assert first.title == "Required task step ci-step-1 failed"
    assert first.detail == "mvn verify → exit 1"
    assert first.refs == ("inv-1",)


def test_attention_names_a_blocked_phase_then_failing_modules():
    metrics = module_metrics()
    metrics["modules"][0].update(
        {
            "tests_failed": 2,
            "failing_count": 2,
            "failing_names": ["a.BTest#one", "a.BTest#two"],
        }
    )
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            phase_records=[
                phase_record("build", outcome="failed", termination="blocked", reason="no JDK")
            ],
        ),
        module_metrics=metrics,
    )
    kinds = [item.kind for item in card.attention]
    assert kinds[0] == "blocked_phase"
    assert "failing_tests" in kinds
    failing = next(item for item in card.attention if item.kind == "failing_tests")
    assert failing.title == "commons-cli · 2 failing"
    assert failing.detail == "a.BTest#one, a.BTest#two"


def test_failing_names_are_capped_with_a_remainder():
    metrics = module_metrics()
    names = [f"a.BTest#m{index}" for index in range(8)]
    metrics["modules"][0].update(
        {"tests_failed": 8, "failing_count": 8, "failing_names": names}
    )
    card = build_result_card(snapshot_dict(verdict="partial"), module_metrics=metrics)
    failing = next(item for item in card.attention if item.kind == "failing_tests")
    assert failing.detail.endswith("+3 more")


def test_a_failed_report_delivery_needs_attention():
    card = build_result_card(
        snapshot_dict(), termination=_termination(ReportDeliveryStatus.FAILED)
    )
    assert card.attention[-1].kind == "report"


def test_notes_are_glossed_conflicts_in_order_without_repeats():
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            conflicts=["test_reports_stale", "test_reports_stale", "test_execution_interrupted"],
        )
    )
    assert card.notes == (
        "some test reports were rewritten after being read and were set aside",
        "the test run stopped before it finished; the counts are what it reached",
    )


def test_an_unknown_verdict_source_is_stated_not_guessed():
    card = build_result_card(snapshot_dict(verdict="unknown", schema_version=5))
    assert card.verdict == "unknown"
    assert card.verdict_source == "snapshot"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_build.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.result_card.build'`.

- [ ] **Step 3: Write the assembler**

Create `src/sag/result_card/build.py`:

```python
"""Assemble one card from the run's record and whatever artifacts came with it.

Every input but the verdict payload is optional. A missing input degrades the
fields that depend on it to a stated absence; it never invents a value and
never changes a row's status word.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from sag.result_card.glosses import gloss
from sag.result_card.models import (
    AttentionItem,
    ResultStats,
    ResultRow,
    RunResultCard,
)
from sag.result_card.rows import (
    build_row,
    ci_row,
    coverage_row,
    report_row,
    setup_row,
    task_row,
    tests_row,
)

_MAX_FAILING_NAMES = 5
_LEGACY_SCHEMA_VERSION = 3


def _as_snapshot(snapshot: Any) -> Any:
    from sag.agent.verdict_finalizer import RunVerdictSnapshot

    if isinstance(snapshot, RunVerdictSnapshot):
        return snapshot
    return RunVerdictSnapshot.model_validate(snapshot)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _stats(
    snapshot: Any,
    *,
    run_pin: Any,
    token_usage: Iterable[Mapping[str, Any]] | None,
    trajectory_session: Any,
    turn_count: int | None,
    tool_calls: int | None,
    tool_failures: int | None,
) -> ResultStats:
    records = tuple(getattr(snapshot, "phase_records", ()) or ())
    phases_total = len(records) or None
    phases_completed = (
        sum(1 for record in records if str(getattr(record, "termination", "")) == "completed")
        if records
        else None
    )

    tokens_in = tokens_out = None
    for row in token_usage or ():
        prompt = row.get("prompt_tokens")
        output = row.get("output_tokens")
        if isinstance(prompt, int):
            tokens_in = (tokens_in or 0) + prompt
        if isinstance(output, int):
            tokens_out = (tokens_out or 0) + output

    pin = _mapping(run_pin)
    advisor = _mapping(pin.get("advisor"))
    session = _mapping(trajectory_session)
    wall_clock = session.get("wall_clock_seconds")

    return ResultStats(
        phases_completed=phases_completed,
        phases_total=phases_total,
        turns=turn_count,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        wall_clock_seconds=wall_clock if isinstance(wall_clock, (int, float)) else None,
        model=pin.get("action_model") or None,
        advisor_model=advisor.get("model") or None,
    )


def _attention(
    snapshot: Any, *, module_metrics: Any, rows: dict[str, ResultRow]
) -> tuple[AttentionItem, ...]:
    items: list[AttentionItem] = []

    completion = getattr(snapshot, "task_completion", None)
    for step in getattr(completion, "steps", ()) or ():
        if step.status == "complete":
            continue
        exit_text = f" → exit {step.exit_code}" if step.exit_code is not None else ""
        items.append(
            AttentionItem(
                kind="task_step",
                title=f"Required task step {step.id} {step.status}",
                detail=f"{step.command}{exit_text}",
                refs=(step.receipt_id,) if step.receipt_id else (),
            )
        )

    for record in getattr(snapshot, "phase_records", ()) or ():
        termination = str(getattr(record, "termination", ""))
        if termination in {"completed", ""}:
            continue
        reason = str(getattr(record, "reason", "") or "").strip()
        items.append(
            AttentionItem(
                kind="blocked_phase",
                title=f"The {record.phase} phase did not finish",
                detail=reason or None,
                refs=tuple(getattr(record, "evidence_refs", ()) or ())[:5],
            )
        )

    for module in _mapping(module_metrics).get("modules") or ():
        if not isinstance(module, Mapping):
            continue
        failing = module.get("failing_count") or 0
        if failing <= 0:
            continue
        names = [str(name) for name in module.get("failing_names") or ()]
        shown = ", ".join(names[:_MAX_FAILING_NAMES])
        if len(names) > _MAX_FAILING_NAMES:
            shown = f"{shown}, +{len(names) - _MAX_FAILING_NAMES} more"
        items.append(
            AttentionItem(
                kind="failing_tests",
                title=f"{module.get('name') or module.get('path') or 'module'} · {failing:,} failing",
                detail=shown or None,
                refs=tuple(str(ref) for ref in module.get("evidence_refs") or ())[:5],
            )
        )

    for line in rows["ci"].items:
        code = line.split(":", 1)[0]
        if code in {
            "NEW_RED_BEYOND_TARGET",
            "MODULES_BELOW_TARGET",
            "EXECUTION_BELOW_TARGET",
            "BUILD_AXIS_NOT_SUCCESSFUL",
        }:
            items.append(AttentionItem(kind="ci_finding", title=line))

    for module in _mapping(module_metrics).get("modules") or ():
        if not isinstance(module, Mapping):
            continue
        for sample in module.get("build_error_samples") or ():
            items.append(
                AttentionItem(
                    kind="build_warning",
                    title=f"{module.get('name') or 'module'}: {sample}",
                )
            )

    if rows["report"].status == "failed":
        items.append(
            AttentionItem(
                kind="report",
                title="The setup report was not written",
                detail=rows["report"].reason,
            )
        )
    return tuple(items)


def _notes(snapshot: Any) -> tuple[str, ...]:
    seen: list[str] = []
    for conflict in getattr(snapshot, "conflicts", ()) or ():
        text = gloss(str(conflict))
        if text not in seen:
            seen.append(text)
    return tuple(seen)


def build_result_card(
    snapshot: Any,
    *,
    module_metrics: Any = None,
    report_metrics: Any = None,
    run_pin: Any = None,
    token_usage: Iterable[Mapping[str, Any]] | None = None,
    trajectory_session: Any = None,
    turn_count: int | None = None,
    tool_calls: int | None = None,
    tool_failures: int | None = None,
    termination: Any = None,
    project: str | None = None,
    container: str | None = None,
    session_dir: str | None = None,
    report_path: str | None = None,
    goal: str | None = None,
    commit: str | None = None,
) -> RunResultCard:
    """Derive the card. Reads; never writes, never re-judges."""

    sealed = _as_snapshot(snapshot)
    stats = _stats(
        sealed,
        run_pin=run_pin,
        token_usage=token_usage,
        trajectory_session=trajectory_session,
        turn_count=turn_count,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
    )
    rows = {
        "setup": setup_row(sealed, stats=stats, termination=termination),
        "task": task_row(sealed),
        "build": build_row(sealed, module_metrics=module_metrics),
        "tests": tests_row(sealed, report_metrics=report_metrics),
        "coverage": coverage_row(sealed),
        "ci": ci_row(sealed),
        "report": report_row(termination, report_path=report_path),
    }

    source = "legacy" if sealed.schema_version <= _LEGACY_SCHEMA_VERSION else "snapshot"
    resolved_commit = commit
    if resolved_commit is None:
        comparison = getattr(sealed, "ci_comparison", None)
        resolved_commit = getattr(comparison, "target_sha", None)

    return RunResultCard(
        run_id=sealed.run_id,
        project=project,
        goal=goal,
        commit=resolved_commit,
        container=container,
        session_dir=session_dir,
        verdict=sealed.verdict,
        verdict_source=source,
        rows=tuple(rows[key] for key in ("setup", "task", "build", "tests", "coverage", "ci", "report")),
        stats=stats,
        attention=_attention(sealed, module_metrics=module_metrics, rows=rows),
        notes=_notes(sealed),
    )


__all__ = ["build_result_card"]
```

Add to `src/sag/result_card/__init__.py`: `from sag.result_card.build import build_result_card` and `"build_result_card"` in `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_build.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sag/result_card tests/test_result_card_build.py
git commit -m "feat(result-card): assemble rows, stats, attention and notes"
```

---

### Task 7: The terminal block

**Files:**
- Create: `src/sag/console/__init__.py`, `src/sag/console/result_block.py`
- Test: `tests/test_result_block.py`

**Interfaces:**
- Consumes: Task 6's `build_result_card`, Task 2's models.
- Produces: `render_result_block(card, *, width: int = 78) -> str` — plain text with Rich markup for status words only.

- [ ] **Step 1: Write the failing test**

Create `tests/test_result_block.py`:

```python
"""The end-of-run block: seven rows, aligned, with nothing said twice."""

from rich.console import Console

from sag.console.result_block import render_result_block
from sag.result_card.build import build_result_card

from result_card_fakes import module_metrics, snapshot_dict


def _plain(card, width: int = 78) -> str:
    """Render, then strip Rich markup the way a terminal would resolve it."""

    console = Console(width=width, force_terminal=False, no_color=True, soft_wrap=False)
    with console.capture() as capture:
        console.print(render_result_block(card, width=width), markup=True, highlight=False)
    return capture.get()


def _card(**kwargs):
    defaults = dict(
        module_metrics=module_metrics(),
        project="commons-cli",
        container="sag-commons-cli",
        commit="e17111798da51037659b3594d9c0b3b525040081",
        session_dir="logs/session_20260914_210609",
        turn_count=12,
        tool_calls=20,
        trajectory_session={"wall_clock_seconds": 390.2},
    )
    payload = kwargs.pop("snapshot", snapshot_dict())
    defaults.update(kwargs)
    return build_result_card(payload, **defaults)


def test_block_opens_with_the_run_identity():
    text = _plain(_card())
    first = text.splitlines()[0]
    assert first.startswith("── commons-cli · e171117 · sag-commons-cli ")
    assert len(first) == 78


def test_every_row_appears_once_in_reading_order():
    lines = [line for line in _plain(_card()).splitlines() if line.startswith(" ")]
    labels = [line[1:15].strip() for line in lines if line[1:15].strip()]
    assert labels[:7] == [
        "Setup",
        "Required task",
        "Build",
        "Tests",
        "Coverage",
        "Official CI",
        "Report",
    ]


def test_headline_and_detail_are_stacked_under_one_label():
    text = _plain(_card())
    assert " Tests         executed       994 executed · 933 passed · 0 failed" in text
    assert "                              100% of non-skipped passed" in text


def test_a_reason_carries_its_code_in_parentheses():
    text = _plain(_card())
    assert "no CI job on this commit matches the run's JDK and OS" in text
    assert "(official_ci_cell_not_matched)" in text


def test_task_steps_are_listed_under_their_row():
    text = _plain(_card())
    assert "   smoke-build-test: complete — mvn clean verify → exit 0" in text


def test_evidence_and_next_lines_close_the_block():
    text = _plain(_card())
    assert " Evidence      logs/session_20260914_210609" in text
    assert " Next          uv run sag ui" in text


def test_a_clean_run_says_nothing_after_the_block():
    text = _plain(_card())
    assert "Setup verdict" not in text


def test_a_non_success_run_states_its_verdict_and_exit_code_once():
    text = _plain(_card(snapshot=snapshot_dict(verdict="partial")))
    assert text.count("Setup verdict: partial · exit 1") == 1


def test_attention_block_appears_only_when_there_is_something_to_do():
    metrics = module_metrics()
    metrics["modules"][0].update(
        {"tests_failed": 2, "failing_count": 2, "failing_names": ["a.T#one", "a.T#two"]}
    )
    text = _plain(_card(snapshot=snapshot_dict(verdict="partial"), module_metrics=metrics))
    assert " Needs attention" in text
    assert "commons-cli · 2 failing" in text
    assert "a.T#one, a.T#two" in text


def test_notes_are_listed_when_the_run_recorded_conflicts():
    text = _plain(
        _card(snapshot=snapshot_dict(verdict="partial", conflicts=["test_reports_stale"]))
    )
    assert " Notes" in text
    assert "some test reports were rewritten after being read and were set aside" in text


def test_no_line_exceeds_the_requested_width():
    for width in (78, 100, 120):
        for line in _plain(_card(), width=width).splitlines():
            assert len(line) <= width, (width, line)


def test_the_block_never_speaks_the_forbidden_vocabulary():
    text = _plain(_card(snapshot=snapshot_dict(verdict="partial"))).lower()
    for word in ("sealed", "canonical", "claimed", "quarantined", "metrics-v2", "promoting"):
        assert word not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_block.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.console'`.

- [ ] **Step 3: Write the renderer**

Create `src/sag/console/__init__.py`:

```python
"""Terminal renderers. These format what other layers already decided."""
```

Create `src/sag/console/result_block.py`:

```python
"""The end-of-run block: one card, seven rows, one screen.

Rich markup is used for the status word only. Everything else is plain text so
the block reads the same in a pipe, a log file and a terminal.
"""

from __future__ import annotations

import textwrap

from sag.result_card.models import RunResultCard, Tone

LABEL_WIDTH = 14
STATUS_WIDTH = 15
GUTTER = 1
MIN_WIDTH = 60
_COMMIT_CHARS = 7
_MAX_ITEMS = 12

_TONE_STYLE: dict[Tone, str] = {
    "success": "green",
    "attention": "yellow",
    "failed": "red",
    "neutral": "dim",
}

_NEXT_STEPS = "uv run sag ui        uv run sag result {target}"


def _rule(width: int, title: str | None = None) -> str:
    if not title:
        return "─" * width
    head = f"── {title} "
    return head + "─" * max(0, width - len(head))


def _identity(card: RunResultCard) -> str:
    parts = [
        card.project,
        card.commit[:_COMMIT_CHARS] if card.commit else None,
        card.container,
    ]
    return " · ".join(part for part in parts if part) or card.run_id


def _wrap(text: str, width: int, indent: str) -> list[str]:
    if width <= 0:
        return [indent + text]
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [indent.rstrip()]


def render_result_block(card: RunResultCard, *, width: int = 78) -> str:
    """Render the card as the block a user reads when a run ends."""

    width = max(MIN_WIDTH, width)
    body_indent = " " * (GUTTER + LABEL_WIDTH + STATUS_WIDTH)
    body_width = width - len(body_indent)

    lines: list[str] = [_rule(width, _identity(card))]

    for row in card.rows:
        style = _TONE_STYLE[row.tone]
        status = row.status[: STATUS_WIDTH - 1]
        head = (
            f"{' ' * GUTTER}{row.label:<{LABEL_WIDTH}}"
            f"[{style}]{status}[/{style}]{' ' * (STATUS_WIDTH - len(status))}"
        )
        headline_lines = _wrap(row.headline, body_width, body_indent)
        lines.append(head + headline_lines[0][len(body_indent) :])
        lines.extend(headline_lines[1:])
        if row.detail:
            lines.extend(_wrap(row.detail, body_width, body_indent))
        if row.reason:
            lines.extend(_wrap(row.reason, body_width, body_indent))
        shown = row.items[:_MAX_ITEMS]
        for item in shown:
            lines.extend(_wrap(item, width - 3, "   "))
        if len(row.items) > _MAX_ITEMS:
            lines.append(f"   +{len(row.items) - _MAX_ITEMS} more")

    lines.append(_rule(width))

    if card.attention:
        lines.append(f"{' ' * GUTTER}Needs attention")
        for item in card.attention:
            text = item.title if not item.detail else f"{item.title} — {item.detail}"
            lines.extend(_wrap(text, width - 3, "   "))

    if card.session_dir:
        lines.append(f"{' ' * GUTTER}{'Evidence':<{LABEL_WIDTH}}{card.session_dir}")

    target = card.container or card.session_dir
    if target:
        lines.append(
            f"{' ' * GUTTER}{'Next':<{LABEL_WIDTH}}{_NEXT_STEPS.format(target=target)}"
        )

    if card.notes:
        lines.append(f"{' ' * GUTTER}Notes")
        for note in card.notes:
            lines.extend(_wrap(note, width - 3, "   "))

    if card.verdict != "success":
        style = _TONE_STYLE[card.tone]
        lines.append(f"[{style}]Setup verdict: {card.verdict} · exit 1[/{style}]")

    return "\n".join(lines)


__all__ = ["render_result_block"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_block.py -v`
Expected: PASS, 12 tests. If a width assertion fails, adjust `_wrap`'s width arithmetic — never the assertion.

- [ ] **Step 5: Commit**

```bash
git add src/sag/console tests/test_result_block.py
git commit -m "feat(result-card): the end-of-run terminal block"
```

---

### Task 8: The Markdown table

**Files:**
- Create: `src/sag/result_card/markdown.py`
- Modify: `src/sag/result_card/__init__.py`
- Test: `tests/test_result_card_markdown.py`

**Interfaces:**
- Consumes: Task 6.
- Produces: `render_result_card_markdown(card) -> list[str]` — the `## Result` section as Markdown lines, no trailing blank beyond one.

- [ ] **Step 1: Write the failing test**

Create `tests/test_result_card_markdown.py`:

```python
"""The report's Result section is the same seven rows as the terminal block."""

from sag.result_card.build import build_result_card
from sag.result_card.markdown import render_result_card_markdown

from result_card_fakes import module_metrics, snapshot_dict


def _lines(**kwargs) -> list[str]:
    payload = kwargs.pop("snapshot", snapshot_dict())
    card = build_result_card(payload, module_metrics=module_metrics(), **kwargs)
    return render_result_card_markdown(card)


def test_section_opens_with_a_result_heading_and_a_table_head():
    lines = _lines()
    assert lines[0] == "## Result"
    assert lines[2] == "| | Status | Detail |"
    assert lines[3] == "|---|---|---|"


def test_every_row_is_one_table_row_in_order():
    labels = [line.split("|")[1].strip() for line in _lines() if line.startswith("| **")]
    assert labels == [
        "**Setup**",
        "**Required task**",
        "**Build**",
        "**Tests**",
        "**Coverage**",
        "**Official CI**",
        "**Report**",
    ]


def test_detail_and_reason_are_joined_in_the_third_column():
    row = next(line for line in _lines() if "**Official CI**" in line)
    assert "not compared" in row
    assert "no CI job on this commit matches the run's JDK and OS" in row
    assert "official_ci_cell_not_matched" in row


def test_pipes_inside_a_command_do_not_break_the_table():
    snapshot = snapshot_dict(
        task_completion={
            "run_id": "r",
            "task_sha256": "d" * 64,
            "status": "complete",
            "steps": [
                {
                    "id": "s1",
                    "command": "mvn test | tee out.log",
                    "status": "complete",
                    "receipt_id": "inv-1",
                    "exit_code": 0,
                    "reason": None,
                }
            ],
            "reasons": [],
        }
    )
    row = next(line for line in _lines(snapshot=snapshot) if "**Required task**" in line)
    assert row.count("|") == 4
    assert r"\|" in row


def test_attention_is_listed_under_its_own_heading():
    metrics = module_metrics()
    metrics["modules"][0].update(
        {"tests_failed": 1, "failing_count": 1, "failing_names": ["a.T#one"]}
    )
    lines = render_result_card_markdown(
        build_result_card(snapshot_dict(verdict="partial"), module_metrics=metrics)
    )
    assert "### Needs attention" in lines
    assert any(line.startswith("- commons-cli · 1 failing") for line in lines)


def test_no_attention_heading_when_nothing_needs_attention():
    assert "### Needs attention" not in _lines()


def test_notes_follow_the_table_when_present():
    lines = _lines(snapshot=snapshot_dict(verdict="partial", conflicts=["test_reports_stale"]))
    assert "### Data notes" in lines
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_markdown.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.result_card.markdown'`.

- [ ] **Step 3: Write the renderer**

Create `src/sag/result_card/markdown.py`:

```python
"""The card as the report's `## Result` section.

Same rows, same words as the terminal block; only the frame differs. Cell text
is escaped so a command containing a pipe cannot split a column.
"""

from __future__ import annotations

from sag.result_card.models import RunResultCard


def _cell(text: str | None) -> str:
    if not text:
        return "—"
    return text.replace("|", r"\|").replace("\n", " ")


def render_result_card_markdown(card: RunResultCard) -> list[str]:
    """Return the section's lines, ending with exactly one blank line."""

    lines = ["## Result", "", "| | Status | Detail |", "|---|---|---|"]
    for row in card.rows:
        detail = " · ".join(part for part in (row.headline, row.detail) if part)
        if row.reason:
            detail = f"{detail} ({row.reason})" if detail else row.reason
        lines.append(f"| **{row.label}** | {_cell(row.status)} | {_cell(detail)} |")
    lines.append("")

    for row in card.rows:
        for item in row.items:
            lines.append(f"- {_cell(item)}")
    if lines[-1] != "":
        lines.append("")

    if card.attention:
        lines.extend(["### Needs attention", ""])
        for item in card.attention:
            text = item.title if not item.detail else f"{item.title} — {item.detail}"
            lines.append(f"- {_cell(text)}")
        lines.append("")

    if card.notes:
        lines.extend(["### Data notes", ""])
        for note in card.notes:
            lines.append(f"- {_cell(note)}")
        lines.append("")

    return lines


__all__ = ["render_result_card_markdown"]
```

Add to `src/sag/result_card/__init__.py`: `from sag.result_card.markdown import render_result_card_markdown` and the name in `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_card_markdown.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sag/result_card tests/test_result_card_markdown.py
git commit -m "feat(result-card): the report's Result table"
```

---

### Task 9: Wire the CLI and remove the duplicate panel

**Files:**
- Modify: `src/sag/main.py:56-91` (`_render_setup_cli_result`), `src/sag/main.py:700-718` (post-run messages), `src/sag/agent/agent.py:1891-1968` (`_provide_setup_summary`) and its call site at `agent.py:1050`
- Test: `tests/test_cli_project_exit_codes.py`, `tests/test_cli_report_verdict_mirror.py`, `tests/test_agent_final_status.py`

**Interfaces:**
- Consumes: Tasks 6 and 7.
- Produces: `_render_setup_cli_result(snapshot, termination, project_name, *, module_metrics=None, report_metrics=None, run_pin=None, container=None, session_dir=None, report_path=None) -> tuple[str, int]` — the signature keeps its first three positional parameters and its return shape; `metrics_v2` is renamed `report_metrics`.

- [ ] **Step 1: Write the failing test**

Replace the assertion bodies in `tests/test_cli_project_exit_codes.py` that pin the old lines. Add to that file:

```python
def test_block_states_the_verdict_once_and_exits_one(monkeypatch, tmp_path):
    """A partial run says its verdict in the block's closing line, nowhere else."""

    from sag.agent.verdict_finalizer import (
        ReportDeliveryStatus,
        RunTermination,
        RunTerminationStatus,
        RunVerdictSnapshot,
    )
    from sag.main import _render_setup_cli_result

    from result_card_fakes import snapshot_dict

    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict(verdict="partial"))
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )
    text, code = _render_setup_cli_result(snapshot, termination, "commons-cli")
    assert code == 1
    assert text.count("Setup verdict: partial") == 1
    assert "Claimed latest subjects" not in text
    assert "Verdict (derived)" not in text
    assert "Required task" in text


def test_success_exits_zero_and_adds_no_trailer():
    from sag.agent.verdict_finalizer import (
        ReportDeliveryStatus,
        RunTermination,
        RunTerminationStatus,
        RunVerdictSnapshot,
    )
    from sag.main import _render_setup_cli_result

    from result_card_fakes import snapshot_dict

    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict())
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )
    text, code = _render_setup_cli_result(snapshot, termination, "commons-cli")
    assert code == 0
    assert "Setup verdict" not in text
    assert "setup completed" not in text.lower()


def test_failed_report_delivery_is_an_attention_line_not_a_warning_banner():
    from sag.agent.verdict_finalizer import (
        ReportDeliveryStatus,
        RunTermination,
        RunTerminationStatus,
        RunVerdictSnapshot,
    )
    from sag.main import _render_setup_cli_result

    from result_card_fakes import snapshot_dict

    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict())
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.FAILED,
    )
    text, _ = _render_setup_cli_result(snapshot, termination, "commons-cli")
    assert "the setup report was not written" in text
    assert "WARNING" not in text
```

In `tests/test_agent_final_status.py`, replace the `_provide_setup_summary` panel test (around line 861) with:

```python
def test_the_agent_no_longer_prints_its_own_setup_summary():
    """The CLI owns the end of a run; two panels said the same thing twice."""

    from sag.agent.agent import SetupAgent

    assert not hasattr(SetupAgent, "_provide_setup_summary")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_cli_project_exit_codes.py tests/test_agent_final_status.py -v`
Expected: FAIL — the new CLI tests fail on `Claimed latest subjects` still being present; the agent test fails because the method exists.

- [ ] **Step 3: Rewrite the CLI renderer**

Replace `_render_setup_cli_result` in `src/sag/main.py` (lines 56-91) with:

```python
def _render_setup_cli_result(
    snapshot: RunVerdictSnapshot,
    termination: RunTermination,
    project_name: str,
    *,
    module_metrics: Mapping[str, Any] | None = None,
    report_metrics: Mapping[str, Any] | None = None,
    run_pin: Mapping[str, Any] | None = None,
    container: str | None = None,
    session_dir: str | None = None,
    report_path: str | None = None,
) -> tuple[str, int]:
    """Render the run's result block and translate only its verdict to an exit code."""

    card = build_result_card(
        snapshot,
        module_metrics=module_metrics,
        report_metrics=report_metrics,
        run_pin=run_pin,
        termination=termination,
        project=project_name,
        container=container,
        session_dir=session_dir,
        report_path=report_path,
    )
    return render_result_block(card, width=console.width), (
        0 if snapshot.verdict == "success" else 1
    )
```

Add near the other imports in `main.py`:

```python
from sag.console.result_block import render_result_block
from sag.result_card import build_result_card
```

Remove now-unused imports if they have no other use in the file: `render_snapshot_metric_lines` stays only if `sag inspect`/`sag trajectory` still use it — check with `grep -n "render_snapshot_metric_lines\|render_rate_lines" src/sag/main.py` and delete the import only when the grep shows no remaining use.

- [ ] **Step 4: Pass the sibling artifacts at the call site**

In `src/sag/main.py` around line 688, replace the `metrics_v2=_read_metrics_v2_for_cli(orchestrator)` argument and the trailing messages:

```python
        snapshot = read_live_verdict_snapshot(orchestrator)
        session_logger = get_session_logger()
        session_dir = str(session_logger.session_log_dir) if session_logger else None
        cli_result, exit_code = _render_setup_cli_result(
            snapshot,
            termination,
            project_name,
            module_metrics=_read_module_metrics_for_cli(orchestrator),
            report_metrics=_read_metrics_v2_for_cli(orchestrator),
            container=docker_name,
            session_dir=session_dir,
        )

        if record:
            _save_setup_artifacts(orchestrator, project_name)

        console.print(cli_result)

        if exit_code:
            sys.exit(exit_code)
```

The `if not config.ui_mode:` guard, the `✅ Project '...' setup completed!` block, the `Next steps` lines and the `⚠️ Project setup needs attention.` block are deleted — the block carries all of it.

Add `_read_module_metrics_for_cli` beside `_read_metrics_v2_for_cli`:

```python
def _read_module_metrics_for_cli(orchestrator: DockerOrchestrator) -> Mapping[str, Any] | None:
    """Read the per-module diagnostic file, or nothing when it is absent."""

    try:
        text = read_container_text(orchestrator, MODULE_METRICS_PATH)
    except Exception:
        return None
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
```

- [ ] **Step 5: Delete the agent's duplicate panel**

In `src/sag/agent/agent.py`, delete the whole `_provide_setup_summary` method (lines 1891-1968) and its call at line 1050. Leave `_provide_legacy_setup_summary` and `_provide_task_summary` alone — they belong to `sag run`, which phase 2 rewrites.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_cli_project_exit_codes.py tests/test_cli_report_verdict_mirror.py tests/test_agent_final_status.py -v`
Expected: PASS. `test_cli_report_verdict_mirror.py` will need its three `"Verdict (derived): X"` assertions changed to `"Setup verdict: X"` for partial/failed and to a `"Setup verdict" not in text` assertion for success; the exit-code assertions are unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/sag/main.py src/sag/agent/agent.py tests/test_cli_project_exit_codes.py tests/test_cli_report_verdict_mirror.py tests/test_agent_final_status.py
git commit -m "feat(cli): one result block replaces two overlapping end-of-run summaries"
```

---

### Task 10: Wire the Markdown report

**Files:**
- Modify: `src/sag/tools/report_tool.py:4978-5072` (`_render_enhanced_header`), `:5074-5180` (`_render_summary_dashboard`), `:5182-5200` (the Metrics-v2 section inside `_render_detailed_test_analysis`)
- Test: `tests/test_report_honesty.py`

**Interfaces:**
- Consumes: Tasks 6 and 8.
- Produces: `ReportTool._result_card(snapshot: dict) -> RunResultCard | None` — built from the canonical snapshot dict the report already holds; `None` when the dict is not a v3+ verdict payload.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_report_honesty.py`:

```python
def test_report_leads_with_the_result_table_not_bold_metric_lines():
    """The report says what the CLI says, in the same words."""

    from sag.result_card.build import build_result_card
    from sag.result_card.markdown import render_result_card_markdown

    from result_card_fakes import snapshot_dict

    lines = render_result_card_markdown(build_result_card(snapshot_dict()))
    assert lines[0] == "## Result"
    assert not any(line.startswith("**Build:") for line in lines)
    assert not any("Metrics-v2" in line for line in lines)


def test_evidence_accounting_heading_replaces_the_metrics_v2_heading():
    import inspect

    from sag.tools import report_tool

    source = inspect.getsource(report_tool)
    assert "Metrics-v2 Evidence Layers" not in source
    assert "## Evidence accounting" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_report_honesty.py -v -k "result_table or evidence_accounting"`
Expected: FAIL on the `Metrics-v2 Evidence Layers` string still being in the source.

- [ ] **Step 3: Build the card inside the report tool**

Add to `ReportTool` in `src/sag/tools/report_tool.py`, next to `_snapshot_rate_lines`:

```python
    def _result_card(self, snapshot: dict | None):
        """The shared result card, or None when this report has no verdict payload."""

        from sag.result_card.build import build_result_card

        canonical = (snapshot or {}).get("canonical_verdict_payload")
        if not isinstance(canonical, dict):
            return None
        try:
            return build_result_card(
                canonical,
                module_metrics=self._read_module_metrics_payload(),
                report_metrics=(snapshot or {}).get("metrics_v2"),
                project=((snapshot or {}).get("project_info") or {}).get("name"),
                report_path=(snapshot or {}).get("report_filename"),
            )
        except (TypeError, ValueError):
            return None
```

If `_build_report_snapshot` does not already place the verdict payload under `canonical_verdict_payload`, add it there: locate the line in `_build_report_snapshot` (around `report_tool.py:1657`) that stores the snapshot's fields and add `payload["canonical_verdict_payload"] = snapshot.model_dump(mode="json")`. Add `_read_module_metrics_payload` returning the parsed `module_metrics.json` the tool already writes (`_persist_module_metrics` at `:4815` names the path).

- [ ] **Step 4: Replace the header and dashboard sections**

In `_render_enhanced_header`, replace the `rate_lines is not None` branch (the `lines.extend(f"**{line}**" ...)` block through its `return lines`) with:

```python
        card = self._result_card(snapshot)
        if card is not None:
            lines.extend(render_result_card_markdown(card))
            refs = evidence_result.get("evidence_refs") or []
            if refs:
                lines.append(f"**Evidence refs:** {'; '.join(refs)}")
                lines.append("")
            return lines
```

Import at the top of the module: `from sag.result_card.markdown import render_result_card_markdown`.

In `_render_summary_dashboard`, replace the whole `if snapshot.get("mode") == "setup":` branch with `return []` — the Result table above has already said it. Leave the non-setup branch untouched.

In `_render_detailed_test_analysis`, change the section heading from `"## 🧾 Metrics-v2 Evidence Layers"` to `"## Evidence accounting"` and relabel its lines using the §5.5 map:

```python
_ACCOUNTING_LABELS = {
    "Receipt executions": "Results bound to this run's receipts",
    "Claimed latest cases": "Tests identified by module and name",
    "Claimed latest subjects": "Test classes identified by module and name",
    "Quarantined observations (not verdict-bearing)": "Set aside: not from this run's receipts",
    "Unattributed observations (not verdict-bearing)": "Set aside: no module or test name recorded",
    "Stale observations (not verdict-bearing)": "Set aside: from an earlier run",
    "Evidence transport": "Evidence records",
}
```

Apply it by splitting each line from `format_evidence_layer_lines` on its first `": "` and substituting the label when the map has it. Move the resulting section so it is appended at the end of the markdown report rather than in the middle: in `_generate_markdown_report`, move the call that emits this section to just before the closing signature block.

- [ ] **Step 5: Run the report tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_report_honesty.py tests/test_report_tool_metrics_artifact.py tests/test_snapshot_surface_agreement.py -v`
Expected: PASS. `test_report_honesty.py`'s existing four-line block assertion (around line 221) must be rewritten to assert the new table's rows; keep it asserting the same facts, not the same formatting.

- [ ] **Step 6: Commit**

```bash
git add src/sag/tools/report_tool.py tests/test_report_honesty.py
git commit -m "feat(report): lead with the shared result table and move evidence accounting to the end"
```

---

### Task 11: Serve the card from the Workbench API

**Files:**
- Modify: `src/sag/web/models.py` (`ExecutionSessionDetail` around `:793-858`), `src/sag/web/session_registry.py:590-650` and `:930-1015`
- Delete: `src/sag/web/verdict.py`, `tests/test_web_verdict.py`
- Test: `tests/test_web_session_detail_fields.py`

**Interfaces:**
- Consumes: Task 6.
- Produces: `ExecutionSessionDetail.result_card: RunResultCard | None` serialized as `resultCard`; the fields `verdict`, `ci_comparison_lines`, `task_completion_lines` and `files` are removed from the model.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_session_detail_fields.py`:

```python
def test_detail_serves_the_result_card_not_a_composed_sentence():
    from sag.web.models import ExecutionSessionDetail

    fields = ExecutionSessionDetail.model_fields
    assert "result_card" in fields
    for gone in ("verdict", "ci_comparison_lines", "task_completion_lines", "files"):
        assert gone not in fields, f"{gone} should have been removed with the composed verdict"


def test_result_card_serializes_under_its_camel_case_alias():
    from sag.result_card.build import build_result_card
    from sag.web.models import ExecutionSessionDetail

    from result_card_fakes import snapshot_dict

    card = build_result_card(snapshot_dict())
    detail = ExecutionSessionDetail.model_construct(result_card=card)
    dumped = detail.model_dump(mode="json", by_alias=True, include={"result_card"})
    assert "resultCard" in dumped
    assert dumped["resultCard"]["rows"][0]["key"] == "setup"


def test_the_composed_verdict_module_is_gone():
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("sag.web.verdict")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_web_session_detail_fields.py -v -k "result_card or composed"`
Expected: FAIL — `result_card` is not a field and `sag.web.verdict` still imports.

- [ ] **Step 3: Change the model**

In `src/sag/web/models.py`: delete the `VerdictSummary` class and the `verdict`, `ci_comparison_lines`, `task_completion_lines` and `files` fields from `ExecutionSessionDetail`; delete `FileChangeDigest` and any field that referenced it. Add:

```python
from sag.result_card.models import RunResultCard
```

and, inside `ExecutionSessionDetail`:

```python
    result_card: RunResultCard | None = Field(default=None, alias="resultCard")
```

Keep `ci_comparison`, `task_completion`, `rates`, `build`, `test`, `modules`, `module_summary`, `evidence`, `context`, `logs`, `report`, `report_doc`, `snapshot_status`, `legacy`, `report_delivery_status`, `model`, `steps`, `step_budget`, `canonical_verdict` as they are.

- [ ] **Step 4: Build the card in the registry**

In `src/sag/web/session_registry.py` around line 976, replace the `render_ci_comparison_lines` / `render_task_completion_lines` block with a card build, and drop those two imports:

```python
    from sag.result_card.build import build_result_card

    comparison = snapshot.ci_comparison if snapshot is not None else None
    task_completion = snapshot.task_completion if snapshot is not None else None
    result_card = None
    if snapshot is not None:
        try:
            result_card = build_result_card(
                snapshot,
                module_metrics=module_metrics,
                report_metrics=metrics if isinstance(metrics, dict) else None,
                project=_text(trunk_data.get("project_name")) or None,
                goal=_text(trunk_data.get("goal")) or None,
                container=workspace_id,
                session_dir=str(session_dir) if session_dir else None,
                report_path=report_path or None,
            ).model_dump(mode="json")
        except (TypeError, ValueError):
            result_card = None
```

Put `"result_card": result_card,` into the returned dict, and delete the `"ci_comparison_lines"`, `"task_completion_lines"` and `"files"` keys.

In the `ExecutionSessionDetail(...)` construction around line 602, delete the `verdict=`, `files=`, `ci_comparison_lines=` and `task_completion_lines=` arguments, delete the `compose_verdict` call and its `VerdictSummary` import above it, and add:

```python
        result_card=(
            RunResultCard.model_validate(item["result_card"])
            if isinstance(item.get("result_card"), dict)
            else None
        ),
```

- [ ] **Step 5: Delete the composed-verdict module**

```bash
git rm src/sag/web/verdict.py tests/test_web_verdict.py
```

Then `grep -rn "web.verdict\|compose_verdict\|VerdictSummary\|FileChangeDigest" src tests` and remove every remaining reference (expect hits in `demo_data.py` and `test_web_models.py`).

- [ ] **Step 6: Give demo data a card**

In `src/sag/web/demo_data.py`, replace each demo session's `verdict=VerdictSummary(...)` with `result_card=build_result_card(<a demo snapshot dict>, ...)`. Reuse the shapes in `tests/result_card_fakes.py` by copying the dict inline — demo data must not import from `tests/`.

- [ ] **Step 7: Run the web tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_web_session_detail_fields.py tests/test_web_api.py tests/test_web_models.py tests/test_web_demo_data.py tests/test_web_read_model.py tests/test_web_session_registry.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A src/sag/web tests/test_web_session_detail_fields.py tests/test_web_models.py tests/test_web_demo_data.py
git commit -m "feat(web): serve the shared result card and retire the composed verdict sentence"
```

---

### Task 12: Prove the three surfaces agree

**Files:**
- Modify: `tests/test_snapshot_surface_agreement.py`

**Interfaces:**
- Consumes: Tasks 7, 8, 11.
- Produces: nothing; this is the fence that keeps them from drifting.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_snapshot_surface_agreement.py`:

```python
import re

from rich.console import Console

from sag.console.result_block import render_result_block
from sag.result_card.build import build_result_card
from sag.result_card.markdown import render_result_card_markdown
from sag.result_card.models import ROW_LABELS, ROW_ORDER

from result_card_fakes import module_metrics, snapshot_dict

_FIXTURES = {
    "clean": snapshot_dict(),
    "partial": snapshot_dict(verdict="partial", conflicts=["test_execution_interrupted"]),
    "failed": snapshot_dict(
        verdict="failed",
        build_evidence={
            "observed": True,
            "green": False,
            "judgment": "failed",
            "source": "physical",
            "outcome": "failed",
            "evidence_status": "verified",
            "refs": [],
        },
    ),
}


def _terminal_text(card) -> str:
    console = Console(width=120, force_terminal=False, no_color=True, soft_wrap=False)
    with console.capture() as capture:
        console.print(render_result_block(card, width=120), markup=True, highlight=False)
    return capture.get()


def _markdown_cells(card) -> dict[str, tuple[str, str]]:
    cells: dict[str, tuple[str, str]] = {}
    for line in render_result_card_markdown(card):
        match = re.match(r"^\| \*\*(?P<label>[^*]+)\*\* \| (?P<status>[^|]+) \| (?P<detail>.*) \|$", line)
        if match:
            cells[match.group("label").strip()] = (
                match.group("status").strip(),
                match.group("detail").strip(),
            )
    return cells


def test_terminal_and_report_state_the_same_status_for_every_row():
    for name, payload in _FIXTURES.items():
        card = build_result_card(payload, module_metrics=module_metrics())
        terminal = _terminal_text(card)
        cells = _markdown_cells(card)
        for key in ROW_ORDER:
            row = card.row(key)
            label = ROW_LABELS[key]
            assert cells[label][0] == row.status, (name, key)
            assert row.status in terminal, (name, key)
            assert row.headline.split(" · ")[0] in terminal, (name, key)
            assert row.headline.split(" · ")[0] in cells[label][1], (name, key)


def test_the_web_payload_carries_the_same_rows_as_the_terminal():
    for name, payload in _FIXTURES.items():
        card = build_result_card(payload, module_metrics=module_metrics())
        dumped = card.model_dump(mode="json")
        assert [row["key"] for row in dumped["rows"]] == list(ROW_ORDER), name
        for row, served in zip(card.rows, dumped["rows"]):
            assert served["status"] == row.status
            assert served["headline"] == row.headline
            assert served["reason"] == row.reason


def test_no_surface_invents_a_verdict_the_run_did_not_record():
    for payload in _FIXTURES.values():
        card = build_result_card(payload)
        assert card.verdict == payload["verdict"]
        assert card.row("setup").status == payload["verdict"]
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_snapshot_surface_agreement.py -v -k "same_status or same_rows or invents"`
Expected: PASS if Tasks 7, 8 and 11 are correct. A failure here means one renderer is paraphrasing; fix the renderer, never the assertion.

- [ ] **Step 3: Run the full suite**

Run: `PYTHONPATH=.:tests uv run pytest -q -x --ignore=tests/test_packaging_smoke.py`
Expected: PASS. Fix any test that pinned a removed CLI or report string by asserting the new wording for the same fact.

- [ ] **Step 4: Commit**

```bash
git add tests/test_snapshot_surface_agreement.py
git commit -m "test: the terminal, the report and the API state one result the same way"
```

---

## Self-Review

**Spec coverage.** §2.1 models → Task 2. §2.2 row semantics → Tasks 3-5. §2.3 glosses → Task 1. §2.4 attention and notes → Task 6. §2.5 sourcing → Task 6's keyword inputs plus Task 9's and 11's call sites; the trajectory-derived stats (`turns`, `tool_calls`, `tool_failures`, `wall_clock_seconds`) are passed in by the caller, and phase 2 of the delivery order is what starts passing them from a real ledger — until then they are `None` and the setup row degrades as §2.2 specifies. §4.1 block → Task 7. §6 report → Tasks 8 and 10. §5.1's `resultCard` field → Task 11. §7's surface-agreement test → Task 12.

Not in this plan, by the spec's own delivery order: the turn stream, the logger defaults, `--ui` removal, `sag result`, `sag list`, `sag run`'s exit code, `sag trajectory --format`, `inspect --turn` (all phase 2); every frontend change and the §5.5 label rendering in the Workbench UI (phase 3). Task 10 does apply the §5.5 labels to the report's own accounting section, because that section is in this phase's scope.

**Placeholder scan.** No "TBD", no "add error handling", no "similar to Task N". Every code step carries the code. Task 10 Step 3 says "if `_build_report_snapshot` does not already place the verdict payload under `canonical_verdict_payload`, add it there" — that is a conditional the implementer resolves by reading one function, with the exact key name and line given, not a deferred decision.

**Type consistency.** `build_result_card` keyword names match between Task 6's definition and its call sites in Tasks 9, 10 and 11. `render_result_block(card, *, width)` matches Task 7's definition and Task 9's and 12's calls. `render_result_card_markdown(card) -> list[str]` matches Tasks 8, 10 and 12. `ROW_ORDER`, `ROW_LABELS`, `ResultRow.key/status/tone/headline/detail/reason/items/refs` are used with the same names throughout. `gloss(code) -> str` is used in Tasks 3, 5 and 6 as defined in Task 1. Tests import fixtures as `from result_card_fakes import ...`, which is why every test command sets `PYTHONPATH=.:tests`.
