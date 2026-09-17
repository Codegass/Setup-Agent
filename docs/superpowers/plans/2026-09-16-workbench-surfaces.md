# Workbench Surfaces Implementation Plan (phase 3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Workbench around the shared result card and one trajectory, so the browser, the terminal and the report say the same thing, and every measurement the run records is reachable in two clicks.

**Architecture:** The detail pane's band renders the seven-row result card the API now serves, replacing the sentence the server used to compose. The Flow and Timeline tabs collapse into one Trajectory tab driven only by trajectory-v1, using the call and result summaries phase 2 added. A new Official CI tab renders the structured comparison the API has always shipped and the UI never read. The Tests, Build and Evidence tabs become receipt-centred, which needs one new backend payload. Five dead components and two parallel run models are deleted.

**Tech Stack:** React 18, TypeScript 5.7, Vite 6, Tailwind v4, Radix primitives, vitest + @testing-library/react. Backend: FastAPI + pydantic v2.

**Spec:** `docs/superpowers/specs/2026-09-15-result-card-and-trajectory-surfaces-design.md` — read §5 before starting.

**Depends on:** phase 1 (`docs/superpowers/plans/2026-09-16-result-card.md`) for `resultCard` on the session detail; phase 2 (`docs/superpowers/plans/2026-09-16-turn-stream-cli.md`) for `call.summary`, `observation.outcome`, `observation.summary` and the phase fields on trajectory-v1.

## Global Constraints

- **Copy is sentence case.** Uppercase tracked text only in table headers and status chips. No text below 11px.
- **Forbidden in any user-visible string** (JSX text, labels, aria-labels, titles): `sealed`, `canonical`, `claimed`, `quarantined`, `subject`, `snapshot`, `metrics-v2`, `verdict-bearing`, `promoting`. Replacements: "recorded", "bound to a receipt", "set aside", "test class", "result". Task 11 adds the lint that enforces this.
- **Status words are the run's own words**, lowercased, never re-synonymed: `success`, `partial`, `failed`, `complete`, `incomplete`, `met`, `not met` (rendered from `not_met`).
- **Status earns color.** Hue belongs to state. No gradient accents, no hero-metric card grids, no colored side stripes (`docs/DESIGN.md`'s Hairline Rule).
- **The frontend renders; it does not derive.** Every number and status comes from `resultCard`, `ciComparison`, `taskCompletion` or the trajectory document. No client-side recomputation of a rate that the card already states.
- **Every fetch has a designed failure rendering and every list a designed empty rendering.**
- **All frontend tests stay green** (`npm test --prefix webui`; the baseline is 352 tests in 46 files) and `(cd webui && npx tsc -b)` is clean.
  Type-check from inside `webui`: `npx --prefix webui tsc -b` resolves the binary there but reads `tsconfig.json` from the *current* directory, so from the repo root it exits 1 with `TS5083` whatever the code says, and bare `npx tsc -b` at the root downloads an unrelated registry package. `uv run python scripts/ship_gate.py` runs every leg and is the gate of record.
- **The Python suite carries pre-existing failures that are nobody's in this plan.** They are listed in `scripts/ship_gate.py`'s `PRE_EXISTING_FAILURES`, verified at the pre-implementation commit `7e3b0ede`. Never chain the pytest leg with `&&`: it exits non-zero by design. What must hold is that the FAILED set equals that list and gains nothing. Do not "fix" them.
- **Commit messages carry no `Co-Authored-By` trailer.**
- **Test commands:** `npm test --prefix webui` and `PYTHONPATH=.:tests uv run pytest <path> -v`.

---

## File Structure

| File | Responsibility |
|---|---|
| `webui/src/api/types.ts` | `ResultCard` and friends; full `CIComparison`, `TaskCompletion`, `RunRates`; trajectory summary fields; `receipts` |
| `webui/src/pages/detail/ResultBand.tsx` | **New.** The seven-row band; replaces `VerdictBand` |
| `webui/src/pages/detail/TrajectoryTab.tsx` | **Renamed** from `TimelineTab`; adds phase bands with gate context |
| `webui/src/pages/detail/OfficialCITab.tsx` | **New.** The comparison, its denominators and its findings |
| `webui/src/pages/detail/OverviewTab.tsx` | Attention first, then card-driven tiles and the module table |
| `webui/src/pages/detail/facets.tsx` | The new tab set and its gating |
| `webui/src/components/session/ReceiptTable.tsx` | **New.** One invocation per row, shared by Tests, Build and Evidence |
| `webui/src/components/session/EvidenceAccounting.tsx` | **New.** The collapsed accounting block with plain labels |
| `webui/src/components/trajectory/TurnRow.tsx` | Renders `call.summary` / `observation.summary` / outcome chip |
| `webui/src/components/trajectory/TrajectoryTimeline.tsx` | Phase bands show gate word, validator state, reason, key results |
| `webui/src/pages/WorkspaceRail.tsx` | Status strip and per-row result strip |
| `src/sag/web/models.py`, `src/sag/web/session_registry.py` | `receipts` payload; `WorkspaceSummary.result` |

Deleted: `webui/src/pages/detail/VerdictBand.tsx`, `FlowTab.tsx`, `FacetTabs.tsx`, `scrollSpy.ts`, `webui/src/components/session/ContextTrace.tsx`, `ActionDetailModal.tsx`, `BuildCard.tsx`, `TestCard.tsx`, `FilesDigest.tsx`, `webui/src/components/SummaryStrip.tsx`, and each one's test file.

---

### Task 1: Types for what the API already ships

**Files:**
- Modify: `webui/src/api/types.ts`
- Test: `webui/src/api/types.test.ts`

**Interfaces:**
- Consumes: phase 1's `resultCard`, phase 2's trajectory fields.
- Produces: `ResultRow`, `ResultStats`, `AttentionItem`, `ResultCard`, `Pair`, `LifecycleParity`, `Attainment`, `CIComparison`, `TaskStep`, `TaskCompletion`, `GrainRate`, `RunRates`, `WorkspaceResult`; `TrajectoryCall.summary`, `TrajectoryObservation.outcome`/`summary`, `TrajectoryPhase.validatorState`-equivalent snake_case fields.

- [ ] **Step 1: Write the failing test**

Add to `webui/src/api/types.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import type {
  Attainment,
  CIComparison,
  ResultCard,
  RunRates,
  TaskCompletion,
  TrajectoryTurn,
} from "./types"

describe("result card types", () => {
  it("names all seven rows in reading order", () => {
    const card: ResultCard = {
      schemaVersion: 1,
      runId: "r",
      verdict: "success",
      verdictSource: "snapshot",
      rows: [
        { key: "setup", label: "Setup", status: "success", tone: "success", headline: "5/5 phases" },
        { key: "task", label: "Required task", status: "complete", tone: "success", headline: "complete 1/1 steps" },
        { key: "build", label: "Build", status: "success", tone: "success", headline: "1/1 modules built" },
        { key: "tests", label: "Tests", status: "executed", tone: "success", headline: "994 executed" },
        { key: "coverage", label: "Coverage", status: "not collected", tone: "neutral", headline: "not collected" },
        { key: "ci", label: "Official CI", status: "not compared", tone: "neutral", headline: "not compared" },
        { key: "report", label: "Report", status: "delivered", tone: "neutral", headline: "report.md" },
      ],
      stats: {},
      attention: [],
      notes: [],
    }
    expect(card.rows.map((row) => row.key)).toEqual([
      "setup",
      "task",
      "build",
      "tests",
      "coverage",
      "ci",
      "report",
    ])
  })

  it("carries the whole attainment payload, not just a verdict word", () => {
    const attainment: Attainment = {
      verdict: "met",
      cell_id: "jenkins-17",
      cell_grade: "A",
      valid: true,
      target_usable: true,
      clean: true,
      clean_form: "ids",
      built: true,
      alpha: { numerator: 523, denominator: 523 },
      alpha_test: { numerator: 523, denominator: 523 },
      alpha_build: { numerator: 1, denominator: 1 },
      executed_observed: 523,
      executed_target: 523,
      red_observed: 0,
      red_target: 0,
      modules_matched: 1,
      modules_target: 1,
      missing_module_ids: [],
      build_form: "modules",
      modules_basis: "log",
      unmatched_observed_module_ids: [],
      lifecycle_parity: {
        status: "equivalent",
        form: "maven_phases",
        ci_command: "mvn -V test",
        sag_commands: ["mvn test"],
        ci_reach: "test",
        sag_reach: "test",
        missing: [],
        extra: [],
      },
      unexpected_red_ids: [],
      reason_codes: [],
    }
    const comparison: CIComparison = {
      schema_version: 1,
      status: "evaluated",
      run_id: "r",
      repo: "apache/commons-cli",
      target_sha: "e171117",
      attainment,
      reasons: [],
      receipt_ids: [],
      commands: [],
    }
    expect(comparison.attainment?.alpha?.denominator).toBe(523)
    expect(comparison.attainment?.lifecycle_parity?.missing).toEqual([])
  })

  it("carries each required-task step, not just the rollup status", () => {
    const completion: TaskCompletion = {
      run_id: "r",
      status: "incomplete",
      steps: [
        {
          id: "ci-step-1",
          command: "mvn verify",
          status: "failed",
          receipt_id: "inv-1",
          exit_code: 1,
          reason: "Required command returned a nonzero exit code.",
        },
      ],
      reasons: [],
    }
    expect(completion.steps[0].exit_code).toBe(1)
  })

  it("models a rate grain's three shapes", () => {
    const rates: RunRates = {
      build: {
        modules: { rate: 100, band: "fully", numerator: 1, denominator: 1 },
        classes: { band: "unavailable", reason: "counts are diagnostic" },
      },
      test: {
        cases: { band: "unbounded", reason: "cannot bound", numerator: 994, denominator: 472 },
        modules: { band: "unavailable", reason: "receipts identify domains" },
      },
      coverage: { status: "unavailable", reason: "coverage pass not run" },
    }
    expect(rates.build.modules.band).toBe("fully")
    expect(rates.test.cases.numerator).toBe(994)
  })

  it("lets a turn state how its call came out", () => {
    const turn: TrajectoryTurn = {
      turn_id: 8,
      phase: "build",
      actor: "model",
      call: { tool: "build", summary: "verify mvn clean verify" },
      observation: { outcome: "failed", summary: "exit 1 · enforcer" },
      control_seq: [65, 66],
    }
    expect(turn.observation?.outcome).toBe("failed")
    expect(turn.call?.summary).toContain("mvn clean verify")
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/api/types.test.ts`
Expected: FAIL — `ResultCard`, `Attainment`, `TaskCompletion`, `RunRates` are not exported; `call.summary` is not a property.

- [ ] **Step 3: Write the types**

In `webui/src/api/types.ts`, add:

```ts
export type RowKey = "setup" | "task" | "build" | "tests" | "coverage" | "ci" | "report"
export type Tone = "success" | "attention" | "failed" | "neutral"

/** One measurement, stated once. `reason` is present exactly when the row has
 *  nothing to measure, so a blank never has to be guessed at. */
export interface ResultRow {
  key: RowKey
  label: string
  status: string
  tone: Tone
  headline: string
  detail?: string | null
  reason?: string | null
  items?: string[]
  refs?: string[]
}

export interface ResultStats {
  phasesCompleted?: number | null
  phasesTotal?: number | null
  turns?: number | null
  toolCalls?: number | null
  toolFailures?: number | null
  tokensIn?: number | null
  tokensOut?: number | null
  wallClockSeconds?: number | null
  model?: string | null
  advisorModel?: string | null
}

export interface AttentionItem {
  kind: "failing_tests" | "task_step" | "blocked_phase" | "ci_finding" | "build_warning" | "report"
  title: string
  detail?: string | null
  refs?: string[]
}

export interface ResultCard {
  schemaVersion: 1
  runId: string
  project?: string | null
  goal?: string | null
  commit?: string | null
  container?: string | null
  sessionDir?: string | null
  verdict: CanonicalVerdict
  verdictSource: "snapshot" | "legacy" | "unavailable"
  rows: ResultRow[]
  stats: ResultStats
  attention: AttentionItem[]
  notes: string[]
}

/** An exact fraction as two integers. A percentage is presentation only. */
export interface Pair {
  numerator: number
  denominator: number
}

export interface LifecycleParity {
  status: "equivalent" | "not_equivalent" | "unknown"
  form: "maven_phases" | "gradle_tasks" | "none"
  ci_command: string
  sag_commands: string[]
  ci_reach?: string | null
  sag_reach?: string | null
  missing: string[]
  extra: string[]
}

export interface Attainment {
  verdict: "invalid" | "not_met" | "partial" | "met" | "exceeded"
  cell_id: string
  cell_grade: "A" | "B"
  valid: boolean
  target_usable: boolean
  clean: boolean
  clean_form: "ids" | "counts"
  built: boolean
  alpha?: Pair | null
  alpha_test?: Pair | null
  alpha_build?: Pair | null
  executed_observed: number
  executed_target: number
  red_observed: number
  red_target: number
  modules_matched: number
  modules_target: number
  missing_module_ids: string[]
  build_form: "modules" | "conclusion"
  modules_basis?: "log" | "declared" | "test_bearing" | null
  unmatched_observed_module_ids: string[]
  lifecycle_parity?: LifecycleParity | null
  unexpected_red_ids: string[]
  reason_codes: string[]
}

export interface CIComparison {
  schema_version: number
  status: "evaluated" | "no_target" | "no_matched_cell" | "unavailable"
  run_id: string
  repo?: string | null
  target_sha?: string | null
  target_record_sha256?: string | null
  attainment?: Attainment | null
  receipt_ids: string[]
  commands: string[]
  acceptance_command?: string | null
  test_identity_basis?: string | null
  reasons: string[]
}

export interface TaskStep {
  id: string
  command: string
  status: "complete" | "failed" | "missing" | "out_of_order" | "unavailable"
  receipt_id?: string | null
  exit_code?: number | null
  reason?: string | null
}

export interface TaskCompletion {
  run_id: string
  task_sha256?: string | null
  status: "complete" | "incomplete" | "unavailable"
  steps: TaskStep[]
  reasons: string[]
}

/** A grain is one of three shapes: measured, unbounded, or unavailable. */
export interface GrainRate {
  band: "fully" | "most" | "half" | "few" | "none" | "unavailable" | "unbounded"
  rate?: number | null
  numerator?: number
  denominator?: number
  reason?: string
}

export interface RunRates {
  build: { modules: GrainRate; classes: GrainRate }
  test: { cases: GrainRate; modules: GrainRate }
  coverage:
    | { status: "collected"; line_rate: number; source: string }
    | { status: "unavailable"; reason: string }
}

/** One invocation, as its receipt recorded it. */
export interface ReceiptSummary {
  receiptId: string
  tool: string
  argv: string
  workingDirectory?: string | null
  actualCwd?: string | null
  exitCode?: number | null
  outcome: string
  lifecycleState?: string | null
  toolchain?: { executable?: string | null; version?: string | null } | null
  jdkMajor?: string | null
  jdkVersion?: string | null
  reportsNew: number
  reportsChanged: number
  testsReported?: number | null
  roles: string[]
}

export interface WorkspaceResult {
  verdict: CanonicalVerdict
  task?: { status: string; completed: number; required: number } | null
  tests?: {
    executed: number
    passed: number
    failed: number
    errors: number
    skipped: number
  } | null
  ci?: { status: string; verdict?: string | null } | null
}
```

Replace the `ciComparison` and `taskCompletion` stubs and the `rates` field on `ExecutionSessionDetail`:

```ts
  resultCard?: ResultCard | null
  rates?: RunRates | null
  ciComparison?: CIComparison | null
  taskCompletion?: TaskCompletion | null
  receipts?: ReceiptSummary[]
```

and delete `verdict`, `ciComparisonLines`, `taskCompletionLines`, `files`, `VerdictSummary` and `FileChangeDigest`.

Add to `WorkspaceSummary`: `result?: WorkspaceResult | null`.

Extend the trajectory types:

```ts
export type ObservationOutcome = "ok" | "failed" | "refused" | "pending" | "cancelled"

export interface TrajectoryCall {
  tool: string
  params_ref?: string | null
  /** What this call asked for, in one line. */
  summary?: string | null
}

export interface TrajectoryObservation {
  ref?: string | null
  evidence_ref?: string | null
  error_code?: string | null
  failure_signature?: string | null
  outcome?: ObservationOutcome | null
  summary?: string | null
}

export interface TrajectoryPhase {
  name: string
  termination?: string | null
  gates: TrajectoryGate[]
  validator_state?: "green" | "partial" | "red" | "unavailable" | null
  reason?: string | null
  key_results?: string | null
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test --prefix webui -- src/api/types.test.ts`, then separately `(cd webui && npx tsc -b)`
Expected: the types test PASSES; `tsc` reports errors in every file that read a removed field. Those are Tasks 2-10's work; note the list and move on.

- [ ] **Step 5: Commit**

```bash
git add webui/src/api/types.ts webui/src/api/types.test.ts
git commit -m "feat(webui): type the result card and the CI, task and rate payloads the API already ships"
```

---

### Task 2: Serve receipts and the rail's result strip

**Files:**
- Modify: `src/sag/web/models.py`, `src/sag/web/session_registry.py`, `src/sag/web/demo_data.py`
- Test: `tests/test_web_receipts.py`

**Interfaces:**
- Consumes: phase 1's card; `execute_named_json_record_stream` / `decode_named_json_record_stream` from `sag.agent.evidence_records`; `RECEIPT_DIR` from `sag.agent.invocation_receipts`.
- Produces: `ExecutionSessionDetail.receipts: list[ReceiptSummary]`; `WorkspaceSummary.result: WorkspaceResult | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_web_receipts.py`:

```python
"""The Workbench can show what each command actually did."""

from sag.web.models import ReceiptSummary, WorkspaceResult
from sag.web.session_registry import _receipt_summary


def _receipt(**overrides) -> dict:
    payload = {
        "schema_version": 3,
        "receipt_id": "inv-maven-1-ee86ae186d94-0002",
        "run_id": "r",
        "tool": "maven",
        "requested_action": "verify",
        "effective_action": "verify",
        "argv": "/opt/apache-maven-3.9.9/bin/mvn clean verify",
        "working_directory": "/workspace/commons-cli",
        "actual_cwd": "/workspace/commons-cli",
        "outcome": "completed",
        "exit_code": 0,
        "lifecycle_state": "finished",
        "toolchain_fingerprint": {
            "executable": "/opt/apache-maven-3.9.9/bin/mvn",
            "version": "Apache Maven 3.9.9",
        },
        "effective_jdk": {
            "major": "17",
            "runtime_authority": "dispatch_probe",
            "provenance": {"dispatch_runtime": {"version": "17.0.20"}},
        },
        "report_delta": {"new": [{"path": "a.xml", "sha256": "x"}], "changed": []},
        "testcase_execution_totals": {"reported": 994},
    }
    payload.update(overrides)
    return payload


def test_summary_copies_the_command_and_how_it_ended():
    summary = _receipt_summary(_receipt())
    assert summary.receipt_id == "inv-maven-1-ee86ae186d94-0002"
    assert summary.argv == "/opt/apache-maven-3.9.9/bin/mvn clean verify"
    assert summary.exit_code == 0
    assert summary.outcome == "completed"


def test_summary_carries_the_observed_toolchain():
    summary = _receipt_summary(_receipt())
    assert summary.toolchain["version"] == "Apache Maven 3.9.9"
    assert summary.jdk_major == "17"
    assert summary.jdk_version == "17.0.20"


def test_summary_counts_the_reports_the_run_wrote():
    summary = _receipt_summary(_receipt())
    assert summary.reports_new == 1
    assert summary.reports_changed == 0
    assert summary.tests_reported == 994


def test_a_receipt_without_optional_evidence_states_absence():
    summary = _receipt_summary(
        _receipt(
            toolchain_fingerprint=None,
            effective_jdk=None,
            testcase_execution_totals=None,
            exit_code=None,
        )
    )
    assert summary.toolchain is None
    assert summary.jdk_major is None
    assert summary.tests_reported is None
    assert summary.exit_code is None


def test_a_malformed_receipt_is_skipped_not_guessed_at():
    assert _receipt_summary({"receipt_id": "x"}) is None
    assert _receipt_summary(None) is None


def test_workspace_result_serializes_under_camel_case():
    result = WorkspaceResult(verdict="partial")
    assert "verdict" in result.model_dump(mode="json", by_alias=True)


def test_receipt_summary_serializes_under_camel_case():
    summary = _receipt_summary(_receipt())
    dumped = summary.model_dump(mode="json", by_alias=True)
    assert dumped["receiptId"] == summary.receipt_id
    assert dumped["reportsNew"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_web_receipts.py -v`
Expected: FAIL with `ImportError: cannot import name 'ReceiptSummary'`.

- [ ] **Step 3: Add the models**

In `src/sag/web/models.py`:

```python
class ReceiptSummary(_CamelModel):
    """One invocation as its receipt recorded it. Copied, never recomputed."""

    receipt_id: str
    tool: str
    argv: str
    working_directory: str | None = None
    actual_cwd: str | None = None
    exit_code: int | None = None
    outcome: str
    lifecycle_state: str | None = None
    toolchain: dict[str, str | None] | None = None
    jdk_major: str | None = None
    jdk_version: str | None = None
    reports_new: int = 0
    reports_changed: int = 0
    tests_reported: int | None = None
    roles: list[str] = Field(default_factory=list)


class WorkspaceTaskResult(_CamelModel):
    status: str
    completed: int
    required: int


class WorkspaceTestResult(_CamelModel):
    executed: int
    passed: int
    failed: int
    errors: int
    skipped: int


class WorkspaceCIResult(_CamelModel):
    status: str
    verdict: str | None = None


class WorkspaceResult(_CamelModel):
    """The four numbers the rail shows for one workspace."""

    verdict: str
    task: WorkspaceTaskResult | None = None
    tests: WorkspaceTestResult | None = None
    ci: WorkspaceCIResult | None = None
```

`_CamelModel` is whatever base class in that file already produces camelCase aliases; reuse it rather than adding aliases by hand. Add `receipts: list[ReceiptSummary] = Field(default_factory=list)` to `ExecutionSessionDetail` and `result: WorkspaceResult | None = None` to `WorkspaceSummary`.

- [ ] **Step 4: Read the receipts**

In `src/sag/web/session_registry.py`:

```python
def _receipt_summary(payload: Any) -> ReceiptSummary | None:
    """One receipt's presentable surface, or None when it is not a receipt."""

    if not isinstance(payload, dict):
        return None
    required = ("receipt_id", "tool", "argv", "outcome")
    if any(not payload.get(field) for field in required):
        return None
    toolchain = payload.get("toolchain_fingerprint")
    jdk = payload.get("effective_jdk") if isinstance(payload.get("effective_jdk"), dict) else {}
    runtime = ((jdk.get("provenance") or {}).get("dispatch_runtime") or {}) if jdk else {}
    delta = payload.get("report_delta") if isinstance(payload.get("report_delta"), dict) else {}
    totals = payload.get("testcase_execution_totals")
    return ReceiptSummary(
        receipt_id=str(payload["receipt_id"]),
        tool=str(payload["tool"]),
        argv=str(payload["argv"]),
        working_directory=_optional_text(payload.get("working_directory")),
        actual_cwd=_optional_text(payload.get("actual_cwd")),
        exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
        outcome=str(payload["outcome"]),
        lifecycle_state=_optional_text(payload.get("lifecycle_state")),
        toolchain=(
            {
                "executable": _optional_text(toolchain.get("executable")),
                "version": _optional_text(toolchain.get("version")),
            }
            if isinstance(toolchain, dict)
            else None
        ),
        jdk_major=_optional_text(jdk.get("major")) if jdk else None,
        jdk_version=_optional_text(runtime.get("version")) if runtime else None,
        reports_new=len(delta.get("new") or ()),
        reports_changed=len(delta.get("changed") or ()),
        tests_reported=(
            totals.get("reported") if isinstance(totals, dict) and isinstance(totals.get("reported"), int) else None
        ),
        roles=[],
    )


def _read_receipts(orchestrator: Any) -> list[ReceiptSummary]:
    """Read the run's receipts from the container, newest last. Best effort."""

    from sag.agent.evidence_records import (
        decode_named_json_record_stream,
        execute_named_json_record_stream,
    )
    from sag.agent.invocation_receipts import RECEIPT_DIR

    try:
        decoded = decode_named_json_record_stream(
            execute_named_json_record_stream(orchestrator, RECEIPT_DIR)
        )
    except Exception:
        return []
    summaries = [_receipt_summary(record.payload) for record in decoded.records]
    return [summary for summary in summaries if summary is not None]
```

Call `_read_receipts(orchestrator)` where the detail item is assembled and put the result under `"receipts"`; pass it into `ExecutionSessionDetail(receipts=...)`.

- [ ] **Step 5: Build the rail result**

Add beside the card build from phase 1 Task 11:

```python
def _workspace_result(card: dict | None) -> dict | None:
    """The rail's four cells, read off the card so the rail cannot disagree."""

    if not isinstance(card, dict):
        return None
    rows = {row.get("key"): row for row in card.get("rows") or () if isinstance(row, dict)}
    task_row = rows.get("task") or {}
    tests_row = rows.get("tests") or {}
    ci_row = rows.get("ci") or {}
    return {
        "verdict": card.get("verdict", "unknown"),
        "task": _task_cell(task_row),
        "tests": _tests_cell(tests_row),
        "ci": {"status": ci_row.get("status", "not compared"), "verdict": ci_row.get("status")},
    }
```

`_task_cell` parses `"complete 1/1 steps"` — do not parse prose. Instead take the numbers from the snapshot directly:

```python
def _task_cell(snapshot: Any) -> dict | None:
    completion = getattr(snapshot, "task_completion", None)
    if completion is None:
        return None
    steps = tuple(completion.steps or ())
    return {
        "status": completion.status,
        "completed": sum(1 for step in steps if step.status == "complete"),
        "required": len(steps),
    }


def _tests_cell(snapshot: Any) -> dict | None:
    stats = getattr(snapshot, "test_stats", None)
    if stats is None or stats.unique.executed <= 0:
        return None
    unique = stats.unique
    return {
        "executed": unique.executed,
        "passed": unique.passed,
        "failed": unique.failed,
        "errors": unique.errors,
        "skipped": unique.skipped,
    }
```

and call `_workspace_result(card=..., snapshot=snapshot)` with both, taking the verdict and the CI status from the card and the counts from the snapshot. Wire the result into `ReadModelBuilder._with_session_state`'s `model_copy(update={...})` so the rail row carries it.

- [ ] **Step 6: Give demo data receipts and results**

In `src/sag/web/demo_data.py`, add two `ReceiptSummary` rows and a `WorkspaceResult` per demo workspace, consistent with the demo card from phase 1.

- [ ] **Step 7: Run the tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_web_receipts.py tests/test_web_session_detail_fields.py tests/test_web_read_model.py tests/test_web_demo_data.py tests/test_web_api.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/sag/web tests/test_web_receipts.py
git commit -m "feat(web): serve invocation receipts and the rail's per-workspace result"
```

---

### Task 3: The result band

**Files:**
- Create: `webui/src/pages/detail/ResultBand.tsx`, `webui/src/pages/detail/ResultBand.test.tsx`
- Delete: `webui/src/pages/detail/VerdictBand.tsx`, `VerdictBand.test.tsx`
- Modify: `webui/src/pages/detail/DetailPane.tsx:121-123`, `webui/src/pages/detail/facets.tsx` (the `TabId` union only)

**Interfaces:**
- Consumes: Task 1's `ResultCard`, `ResultRow`.
- Produces: `<ResultBand card={card} onOpenTab={(id: TabId) => void} />`; the final `TabId` union.

The band links each row to the tab that answers it, so it needs the final tab vocabulary. Widen `TabId` here rather than in Task 6, which then changes only `buildDetailTabs` and `TabBody`.

- [ ] **Step 1: Write the failing test**

Create `webui/src/pages/detail/ResultBand.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import type { ResultCard, ResultRow } from "@/api/types"

import { ResultBand } from "./ResultBand"

function row(overrides: Partial<ResultRow> & Pick<ResultRow, "key" | "label">): ResultRow {
  return { status: "unknown", tone: "neutral", headline: "—", ...overrides }
}

function card(overrides: Partial<ResultCard> = {}): ResultCard {
  return {
    schemaVersion: 1,
    runId: "r",
    verdict: "success",
    verdictSource: "snapshot",
    rows: [
      row({ key: "setup", label: "Setup", status: "success", tone: "success", headline: "5/5 phases · 12 turns" }),
      row({
        key: "task",
        label: "Required task",
        status: "complete",
        tone: "success",
        headline: "complete 1/1 steps",
        detail: "mvn clean verify → exit 0",
        items: ["smoke: complete — mvn clean verify → exit 0"],
      }),
      row({ key: "build", label: "Build", status: "success", tone: "success", headline: "1/1 modules built" }),
      row({ key: "tests", label: "Tests", status: "executed", tone: "success", headline: "994 executed" }),
      row({ key: "coverage", label: "Coverage", status: "not collected", headline: "not collected", reason: "run with --coverage" }),
      row({
        key: "ci",
        label: "Official CI",
        status: "not compared",
        headline: "not compared",
        reason: "no CI job on this commit matches the run's JDK and OS (official_ci_cell_not_matched)",
      }),
      row({ key: "report", label: "Report", status: "delivered", headline: "setup-report.md" }),
    ],
    stats: {},
    attention: [],
    notes: [],
    ...overrides,
  }
}

describe("ResultBand", () => {
  it("shows every row's label, status and headline", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    for (const label of ["Setup", "Required task", "Build", "Tests", "Coverage", "Official CI", "Report"]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByText("994 executed")).toBeInTheDocument()
    expect(screen.getByText("complete 1/1 steps")).toBeInTheDocument()
  })

  it("shows a row's reason when it has nothing to measure", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    expect(
      screen.getByText(/no CI job on this commit matches the run's JDK and OS/),
    ).toBeInTheDocument()
  })

  it("hides a row's items behind a disclosure", async () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    expect(screen.queryByText(/smoke: complete/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: /show 1 step/i }))
    expect(screen.getByText(/smoke: complete/)).toBeInTheDocument()
  })

  it("opens the tab a row belongs to", async () => {
    const onOpenTab = vi.fn()
    render(<ResultBand card={card()} onOpenTab={onOpenTab} />)
    await userEvent.click(screen.getByRole("button", { name: /Tests/ }))
    expect(onOpenTab).toHaveBeenCalledWith("tests")
  })

  it("takes the band's tone from the setup row", () => {
    const failed = card({
      verdict: "failed",
      rows: card().rows.map((r) =>
        r.key === "setup" ? { ...r, status: "failed", tone: "failed" as const } : r,
      ),
    })
    const { container } = render(<ResultBand card={failed} onOpenTab={() => {}} />)
    expect(container.firstChild).toHaveAttribute("data-tone", "failed")
  })

  it("says so when no result was recorded at all", () => {
    render(<ResultBand card={null} onOpenTab={() => {}} />)
    expect(screen.getByText(/No result was recorded for this run/)).toBeInTheDocument()
  })

  it("never uses the retired vocabulary", () => {
    const { container } = render(<ResultBand card={card()} onOpenTab={() => {}} />)
    const text = (container.textContent ?? "").toLowerCase()
    for (const word of ["sealed", "canonical", "claimed", "quarantined", "promoting"]) {
      expect(text).not.toContain(word)
    }
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/pages/detail/ResultBand.test.tsx`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Write the band**

Create `webui/src/pages/detail/ResultBand.tsx`:

```tsx
import { useState } from "react"

import type { ResultCard, ResultRow, RowKey, Tone } from "@/api/types"
import { cn } from "@/lib/utils"

import type { TabId } from "./facets"

/** Which tab answers each row. Clicking a row is the two-click path from a
 *  number to the evidence behind it. */
const ROW_TAB: Record<RowKey, TabId> = {
  setup: "trajectory",
  task: "evidence",
  build: "build",
  tests: "tests",
  coverage: "tests",
  ci: "ci",
  report: "report",
}

const ITEM_NOUN: Partial<Record<RowKey, string>> = {
  task: "step",
  ci: "finding",
}

const CHIP: Record<Tone, string> = {
  success: "bg-status-success-soft text-status-success",
  attention: "bg-status-attention-soft text-status-attention",
  failed: "bg-status-failed-soft text-status-failed",
  neutral: "bg-accent text-muted-foreground",
}

const BAND: Record<Tone, string> = {
  success: "border-status-success-border bg-status-success-soft/30",
  attention: "border-status-attention-border bg-status-attention-soft/30",
  failed: "border-status-failed-border bg-status-failed-soft/30",
  neutral: "border-border bg-card",
}

function Items({ row }: { row: ResultRow }) {
  const [open, setOpen] = useState(false)
  const items = row.items ?? []
  if (items.length === 0) return null
  const noun = ITEM_NOUN[row.key] ?? "detail"
  return (
    <div className="mt-1">
      <button
        className="text-[11px] text-muted-foreground underline-offset-2 hover:underline"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        {open ? "Hide" : `Show ${items.length} ${noun}${items.length === 1 ? "" : "s"}`}
      </button>
      {open ? (
        <ul className="mt-1 space-y-0.5">
          {items.map((item) => (
            <li className="font-mono text-[11px] text-muted-foreground" key={item}>
              {item}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function Row({ row, onOpenTab }: { row: ResultRow; onOpenTab: (id: TabId) => void }) {
  return (
    <div className="flex items-start gap-3 py-1.5">
      <button
        className="w-28 shrink-0 text-left text-[13px] font-semibold text-foreground underline-offset-2 hover:underline"
        onClick={() => onOpenTab(ROW_TAB[row.key])}
        type="button"
      >
        {row.label}
      </button>
      <span
        className={cn(
          "mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold",
          CHIP[row.tone],
        )}
      >
        {row.status.replace(/_/g, " ")}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] text-foreground">{row.headline}</p>
        {row.detail ? <p className="text-[11px] text-muted-foreground">{row.detail}</p> : null}
        {row.reason ? (
          <p className="text-[11px] italic text-muted-foreground">{row.reason}</p>
        ) : null}
        <Items row={row} />
      </div>
    </div>
  )
}

/** The run's whole result, seven rows, in the order the terminal prints them. */
export function ResultBand({
  card,
  onOpenTab,
}: {
  card: ResultCard | null | undefined
  onOpenTab: (id: TabId) => void
}) {
  if (!card) {
    return (
      <div className="rounded-lg border border-border bg-card p-4" data-tone="neutral">
        <p className="text-[13px] text-muted-foreground">
          No result was recorded for this run yet.
        </p>
      </div>
    )
  }
  const tone = card.rows[0]?.tone ?? "neutral"
  return (
    <div
      className={cn("rounded-lg border px-4 py-2", BAND[tone])}
      data-tone={tone}
    >
      {card.rows.map((row) => (
        <Row key={row.key} onOpenTab={onOpenTab} row={row} />
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Widen `TabId` and swap the band into the detail pane**

In `webui/src/pages/detail/facets.tsx`, replace the `TabId` union with its final vocabulary (Task 6 changes `buildDetailTabs` and `TabBody` to match):

```tsx
export type TabId =
  | "overview"
  | "trajectory"
  | "tests"
  | "build"
  | "ci"
  | "evidence"
  | "logs"
  | "report"
```

`tsc` will now flag the `timeline`, `flow` and `files` cases in `buildDetailTabs` and `TabBody`. Leave them flagged; Task 6 removes them. If the repo's dev loop refuses to run with type errors, do Task 6 Step 3 now and Task 6's tests after.

In `webui/src/pages/detail/DetailPane.tsx`, replace the `VerdictBand` import and its render with `<ResultBand card={detail.resultCard} onOpenTab={setActiveTab} />`, wired to whatever state setter the pane already uses for the active tab.

```bash
git rm webui/src/pages/detail/VerdictBand.tsx webui/src/pages/detail/VerdictBand.test.tsx
```

- [ ] **Step 5: Run the tests**

Run: `npm test --prefix webui -- src/pages/detail/ResultBand.test.tsx`
Expected: PASS, 7 tests.

- [ ] **Step 6: Commit**

```bash
git add -A webui/src/pages/detail
git commit -m "feat(webui): the result band states all seven rows instead of one sentence"
```

---

### Task 4: One Trajectory tab

**Files:**
- Rename: `webui/src/pages/detail/TimelineTab.tsx` → `TrajectoryTab.tsx` (and its test)
- Modify: `webui/src/components/trajectory/TurnRow.tsx`, `TrajectoryTimeline.tsx`, `webui/src/pages/detail/facets.tsx`
- Delete: `webui/src/pages/detail/FlowTab.tsx`, `FlowTab.test.tsx`, `webui/src/components/session/ContextTrace.tsx`, `ContextTrace.test.tsx`, `ActionDetailModal.tsx`, `ActionDetailModal.test.tsx`

**Interfaces:**
- Consumes: Task 1's trajectory types.
- Produces: `TrajectoryTab` at tab id `trajectory`; `TurnRow` renders `call.summary`, an outcome chip and `observation.summary`; `TrajectoryTimeline` phase headers render `validator_state`, `reason` and a `key_results` disclosure.

- [ ] **Step 1: Write the failing test**

Add to `webui/src/components/trajectory/TurnRow.test.tsx` (create the file if it does not exist):

```tsx
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import type { TrajectoryTurn } from "@/api/types"

import { TurnRow } from "./TurnRow"

function turn(overrides: Partial<TrajectoryTurn> = {}): TrajectoryTurn {
  return {
    turn_id: 8,
    phase: "build",
    iteration: 5,
    actor: "model",
    call: { tool: "build", summary: "verify mvn clean verify", params_ref: "envelope-000065" },
    observation: { outcome: "failed", summary: "exit 1 · enforcer", error_code: "MAVEN_LOW" },
    control_seq: [65, 66, 68],
    ...overrides,
  }
}

describe("TurnRow", () => {
  it("reads as one line: number, tool, what it asked, how it came out", () => {
    render(<TurnRow sessionId="s" turn={turn()} />)
    expect(screen.getByText("Turn 8")).toBeInTheDocument()
    expect(screen.getByText("build")).toBeInTheDocument()
    expect(screen.getByText("verify mvn clean verify")).toBeInTheDocument()
    expect(screen.getByText("failed")).toBeInTheDocument()
    expect(screen.getByText("exit 1 · enforcer")).toBeInTheDocument()
  })

  it("marks a controller turn as the engine", () => {
    render(<TurnRow sessionId="s" turn={turn({ actor: "controller" })} />)
    expect(screen.getByText(/engine/i)).toBeInTheDocument()
  })

  it("says a pending call is still running", () => {
    render(
      <TurnRow
        sessionId="s"
        turn={turn({ observation: { outcome: "pending", summary: "running · job c523e6" } })}
      />,
    )
    expect(screen.getByText("pending")).toBeInTheDocument()
    expect(screen.getByText("running · job c523e6")).toBeInTheDocument()
  })

  it("says when a turn called nothing", () => {
    render(<TurnRow sessionId="s" turn={turn({ call: null, observation: null })} />)
    expect(screen.getByText(/no call/i)).toBeInTheDocument()
  })

  it("expands into the model context, the call and the result", async () => {
    render(<TurnRow sessionId="s" turn={turn()} />)
    await userEvent.click(screen.getByRole("button", { name: /Turn 8/ }))
    expect(screen.getByText(/Model context/i)).toBeInTheDocument()
    expect(screen.getByText(/Tool call/i)).toBeInTheDocument()
    expect(screen.getByText(/Tool result/i)).toBeInTheDocument()
  })
})
```

Add to `webui/src/components/trajectory/TrajectoryTimeline.test.tsx`:

```tsx
it("shows what each phase's gate decided", () => {
  render(
    <TrajectoryTimeline
      document={{
        schema_version: 1,
        session: { run_id: "r" },
        phases: [
          {
            name: "build",
            termination: "advance",
            gates: [{ word: "success" }],
            validator_state: "green",
            reason: "JVM build execution validated",
            key_results: "Executed mvn clean verify in /workspace/commons-cli",
          },
        ],
        turns: [],
        annotations: [],
        warnings: [],
      }}
      sessionId="s"
    />,
  )
  expect(screen.getByText("build")).toBeInTheDocument()
  expect(screen.getByText(/green/)).toBeInTheDocument()
  expect(screen.getByText(/JVM build execution validated/)).toBeInTheDocument()
})
```

Match the existing props of `TrajectoryTimeline` in that file rather than the shape above if they differ; the assertion is what matters.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/components/trajectory`
Expected: FAIL — no summary or outcome is rendered.

- [ ] **Step 3: Render the summaries in TurnRow**

In `webui/src/components/trajectory/TurnRow.tsx`, in the collapsed row, after the tool name, render:

```tsx
        <span className="min-w-0 flex-1 truncate text-[12px] text-foreground">
          {turn.call?.summary ?? (turn.call ? turn.call.tool : "no call")}
        </span>
        {turn.observation?.outcome ? (
          <span
            className={cn(
              "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold",
              OUTCOME_CHIP[turn.observation.outcome],
            )}
          >
            {turn.observation.outcome}
          </span>
        ) : null}
        {turn.observation?.summary ? (
          <span className="min-w-0 truncate text-[12px] text-muted-foreground">
            {turn.observation.summary}
          </span>
        ) : null}
```

with

```tsx
const OUTCOME_CHIP: Record<NonNullable<TrajectoryObservation["outcome"]>, string> = {
  ok: "bg-status-success-soft text-status-success",
  failed: "bg-status-failed-soft text-status-failed",
  refused: "bg-status-attention-soft text-status-attention",
  cancelled: "bg-status-attention-soft text-status-attention",
  pending: "bg-status-running-soft text-status-running",
}
```

Keep the existing badges (`error_code`, recurrence, gate, tokens, duration, seq) and the `TurnQuad` expansion untouched.

- [ ] **Step 4: Render the gate context on phase bands**

In `TrajectoryTimeline.tsx`, in the band header, after the termination label add:

```tsx
        {band.validatorState ? (
          <span className="text-[11px] text-muted-foreground">
            validator {band.validatorState}
          </span>
        ) : null}
        {band.reason ? (
          <span className="truncate text-[11px] text-muted-foreground">{band.reason}</span>
        ) : null}
```

and below the header, when `band.keyResults` is present, a `<details>` whose summary is `What this phase reported` and whose body is the text.

`bandTurns` in `webui/src/lib/trajectory.ts` must carry the three new fields through onto `PhaseBand`; add them where it copies `termination` and `gates`.

- [ ] **Step 5: Rename the tab and delete the second run model**

```bash
git mv webui/src/pages/detail/TimelineTab.tsx webui/src/pages/detail/TrajectoryTab.tsx
git mv webui/src/pages/detail/TimelineTab.test.tsx webui/src/pages/detail/TrajectoryTab.test.tsx
git rm webui/src/pages/detail/FlowTab.tsx webui/src/pages/detail/FlowTab.test.tsx
git rm webui/src/components/session/ContextTrace.tsx webui/src/components/session/ContextTrace.test.tsx
git rm webui/src/components/session/ActionDetailModal.tsx webui/src/components/session/ActionDetailModal.test.tsx
```

Rename the exported component to `TrajectoryTab` and update its own test's imports and titles.

- [ ] **Step 6: Run the tests**

Run: `npm test --prefix webui -- src/components/trajectory src/pages/detail/TrajectoryTab.test.tsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A webui/src
git commit -m "feat(webui): one trajectory, with each turn's call and result in words"
```

---

### Task 5: The Official CI tab

**Files:**
- Create: `webui/src/pages/detail/OfficialCITab.tsx`, `OfficialCITab.test.tsx`
- Create: `webui/src/lib/ciGlosses.ts`

**Interfaces:**
- Consumes: Task 1's `CIComparison`, `Attainment`.
- Produces: `<OfficialCITab comparison={comparison} />`; `gloss(code: string): string` mirroring the backend table.

The gloss table is duplicated in TypeScript rather than served, because it is presentation text for codes the frontend already receives; Task 11 adds a test that the two tables hold the same keys.

- [ ] **Step 1: Write the failing test**

Create `webui/src/pages/detail/OfficialCITab.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { Attainment, CIComparison } from "@/api/types"

import { OfficialCITab } from "./OfficialCITab"

function attainment(overrides: Partial<Attainment> = {}): Attainment {
  return {
    verdict: "met",
    cell_id: "Apache Jenkins commons-dbutils Linux JDK 17 #455",
    cell_grade: "A",
    valid: true,
    target_usable: true,
    clean: true,
    clean_form: "ids",
    built: true,
    alpha: { numerator: 523, denominator: 523 },
    alpha_test: { numerator: 523, denominator: 523 },
    alpha_build: { numerator: 1, denominator: 1 },
    executed_observed: 523,
    executed_target: 523,
    red_observed: 0,
    red_target: 0,
    modules_matched: 1,
    modules_target: 1,
    missing_module_ids: [],
    build_form: "modules",
    modules_basis: "log",
    unmatched_observed_module_ids: [],
    lifecycle_parity: {
      status: "equivalent",
      form: "maven_phases",
      ci_command: "mvn -B -f pom.xml -V clean test",
      sag_commands: ["/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"],
      ci_reach: "test",
      sag_reach: "test",
      missing: [],
      extra: [],
    },
    unexpected_red_ids: [],
    reason_codes: [],
    ...overrides,
  }
}

function comparison(overrides: Partial<CIComparison> = {}): CIComparison {
  return {
    schema_version: 1,
    status: "evaluated",
    run_id: "r",
    repo: "apache/commons-dbutils",
    target_sha: "e1e2d9edfab8f237aab9e733c6d61e23b35d2079",
    attainment: attainment(),
    receipt_ids: ["inv-maven-1-17c8a2e62d8a-0001"],
    commands: ["mvn -B -f pom.xml -V clean test"],
    acceptance_command: "mvn -B -f pom.xml -V clean test",
    test_identity_basis: "jenkins_single_module:commons-dbutils",
    reasons: [],
    ...overrides,
  }
}

describe("OfficialCITab", () => {
  it("names the job it compared against", () => {
    render(<OfficialCITab comparison={comparison()} />)
    expect(screen.getByText(/Apache Jenkins commons-dbutils Linux JDK 17 #455/)).toBeInTheDocument()
    expect(screen.getByText(/apache\/commons-dbutils/)).toBeInTheDocument()
  })

  it("says what each fraction is measured against", () => {
    render(<OfficialCITab comparison={comparison()} />)
    expect(screen.getByText(/523 of CI's 523 tests/)).toBeInTheDocument()
    expect(screen.getByText(/1 of CI's 1 modules/)).toBeInTheDocument()
    expect(screen.getByText(/job log/)).toBeInTheDocument()
  })

  it("shows the two commands side by side", () => {
    render(<OfficialCITab comparison={comparison()} />)
    expect(screen.getByText("mvn -B -f pom.xml -V clean test")).toBeInTheDocument()
    expect(
      screen.getByText("/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"),
    ).toBeInTheDocument()
  })

  it("names the phases a narrower run skipped", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            verdict: "partial",
            lifecycle_parity: {
              status: "not_equivalent",
              form: "maven_phases",
              ci_command: "mvn -V verify",
              sag_commands: ["mvn test"],
              ci_reach: "verify",
              sag_reach: "test",
              missing: ["package", "verify"],
              extra: [],
            },
          }),
        })}
      />,
    )
    expect(screen.getByText(/package, verify/)).toBeInTheDocument()
  })

  it("lists tests that fail here and pass in CI", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            verdict: "not_met",
            clean: false,
            red_observed: 2,
            unexpected_red_ids: ["a.BTest#one", "a.BTest#two"],
            reason_codes: ["NEW_RED_BEYOND_TARGET"],
          }),
        })}
      />,
    )
    expect(screen.getByText("a.BTest#one")).toBeInTheDocument()
    expect(screen.getByText(/tests failed here that pass in CI/)).toBeInTheDocument()
  })

  it("says there is no scope score when the CI job carries no module list", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({ verdict: "partial", alpha: null, build_form: "conclusion" }),
        })}
      />,
    )
    expect(screen.getByText(/No overall score/)).toBeInTheDocument()
  })

  it("explains itself when nothing was compared", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          status: "no_matched_cell",
          attainment: null,
          reasons: ["official_ci_cell_not_matched"],
        })}
      />,
    )
    expect(screen.getByText(/was not compared/)).toBeInTheDocument()
    expect(
      screen.getByText(/no CI job on this commit matches the run's JDK and OS/),
    ).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/pages/detail/OfficialCITab.test.tsx`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Write the gloss table**

Create `webui/src/lib/ciGlosses.ts` holding the same `code → sentence` pairs as `src/sag/result_card/glosses.py`, exported as `REASON_GLOSS: Record<string, string>` with `export function gloss(code: string): string { return REASON_GLOSS[code] ?? code }`. Copy the sentences verbatim from the Python file — Task 11 asserts the key sets match.

- [ ] **Step 4: Write the tab**

Create `webui/src/pages/detail/OfficialCITab.tsx`. Structure, top to bottom:

1. **Target** — a definition list: repository, commit (first 7 chars, mono, full value in `title`), CI job (`cell_id` + grade), CI command.
2. **Attainment** — the verdict as a chip; then one line per fraction, each naming its denominator:
   - `alpha_test`: `{numerator} of CI's {denominator} tests ran here`
   - `alpha_build`: `{numerator} of CI's {denominator} modules matched · basis: {modules_basis mapped to "job log" | "declared reactor" | "modules with tests"}`
   - `alpha`: `Overall {numerator}/{denominator}`, or `No overall score: the CI job recorded no module list` when `alpha` is null.
3. **Lifecycle** — `ci_command` and each of `sag_commands` in mono, the parity status, and `Missing: {missing.join(", ")}` / `Extra: …` when non-empty.
4. **Tests failing here that pass in CI** — `unexpected_red_ids` as a list, capped at 25 with `+N more`; omitted when empty.
5. **Findings** — one row per `reason_codes` entry: the code in mono, the gloss beside it.
6. When `status !== "evaluated"`: replace 2-5 with one sentence, `Official CI was not compared: {gloss(reasons[0])}`, then the full reasons list.

Every section renders an explicit empty state rather than disappearing silently, except sections 4 and 5, which are legitimately absent when there is nothing to report.

- [ ] **Step 5: Run the tests**

Run: `npm test --prefix webui -- src/pages/detail/OfficialCITab.test.tsx`
Expected: PASS, 7 tests.

- [ ] **Step 6: Commit**

```bash
git add webui/src/pages/detail/OfficialCITab.tsx webui/src/pages/detail/OfficialCITab.test.tsx webui/src/lib/ciGlosses.ts
git commit -m "feat(webui): show the official-CI comparison the API has always shipped"
```

---

### Task 6: The new tab set

**Files:**
- Modify: `webui/src/pages/detail/facets.tsx`
- Delete: `webui/src/pages/detail/FacetTabs.tsx`, `FacetTabs.test.tsx`, `scrollSpy.ts`, `scrollSpy.test.ts`, `webui/src/components/session/FilesDigest.tsx`
- Test: `webui/src/pages/detail/FacetTabs.test.tsx` is replaced by assertions in `DetailPane.test.tsx`

**Interfaces:**
- Consumes: Tasks 3-5; the `TabId` union Task 3 widened.
- Produces: `buildDetailTabs(detail)` and `TabBody` over the new tab set.

- [ ] **Step 1: Write the failing test**

Add to `webui/src/pages/detail/DetailPane.test.tsx`:

```tsx
it("offers the tabs in reading order", () => {
  const tabs = buildDetailTabs(detailWithEverything())
  expect(tabs.map((tab) => tab.id)).toEqual([
    "overview",
    "trajectory",
    "tests",
    "build",
    "ci",
    "evidence",
    "logs",
    "report",
  ])
})

it("hides the official CI tab when nothing was compared at all", () => {
  const tabs = buildDetailTabs({ ...detailWithEverything(), ciComparison: null })
  expect(tabs.map((tab) => tab.id)).not.toContain("ci")
})

it("keeps the trajectory tab for a real session even before its first turn", () => {
  const tabs = buildDetailTabs({ ...detailWithEverything(), demo: false })
  expect(tabs.map((tab) => tab.id)).toContain("trajectory")
})

it("has no files tab", () => {
  expect(buildDetailTabs(detailWithEverything()).map((tab) => tab.id)).not.toContain("files")
})
```

`detailWithEverything()` is a local helper in that test file returning an `ExecutionSessionDetail` with a `resultCard`, a `ciComparison`, evidence, logs and a `reportDoc`.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/pages/detail/DetailPane.test.tsx`
Expected: FAIL — `timeline` and `flow` still appear, `ci` does not.

- [ ] **Step 3: Rewrite the tab builder**

In `webui/src/pages/detail/facets.tsx`, replace `buildDetailTabs` (Task 3 already widened `TabId`):

```tsx
/**
 * One reading order: what happened, how it happened, then each measurement's
 * evidence. Trajectory is never gated on data for a real session — it derives
 * from the control ledger every run writes, and a run with no ledger yet says
 * so in its own words. A demo session stands for no run, so it is not offered.
 */
export function buildDetailTabs(d: ExecutionSessionDetail): TabMeta[] {
  const tabs: TabMeta[] = [{ id: "overview", label: "Overview" }]
  if (!d.demo) tabs.push({ id: "trajectory", label: "Trajectory" })

  const failing = testIssues(d)
  tabs.push({
    id: "tests",
    label: "Tests",
    ...(failing != null ? { count: failing, tone: "red" as const } : {}),
  })
  tabs.push({ id: "build", label: "Build" })
  if (d.ciComparison) tabs.push({ id: "ci", label: "Official CI" })

  const evidenceCount = nonZero(d.evidence.length) ?? nonZero(d.receipts?.length)
  if (evidenceCount) {
    tabs.push({ id: "evidence", label: "Evidence", count: evidenceCount, tone: "neutral" })
  }
  if (nonZero(d.logs.length)) tabs.push({ id: "logs", label: "Logs" })
  if (d.reportDoc) tabs.push({ id: "report", label: "Report" })
  return tabs
}
```

Update `TabBody` to dispatch `trajectory → TrajectoryTab`, `ci → OfficialCITab`, and delete the `timeline`, `flow` and `files` cases. Delete `buildDetailFacets`, `FacetBody`, `FacetId` and `Empty` from the file — they have had no caller since the redesign.

```bash
git rm webui/src/pages/detail/FacetTabs.tsx webui/src/pages/detail/FacetTabs.test.tsx
git rm webui/src/pages/detail/scrollSpy.ts webui/src/pages/detail/scrollSpy.test.ts
git rm webui/src/components/session/FilesDigest.tsx
```

- [ ] **Step 4: Run the tests**

Run: `npm test --prefix webui -- src/pages/detail`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A webui/src/pages/detail webui/src/components/session
git commit -m "feat(webui): one tab set, with official CI in it and the dead nav gone"
```

---

### Task 7: Evidence accounting and the Tests tab

**Files:**
- Create: `webui/src/components/session/EvidenceAccounting.tsx`, `EvidenceAccounting.test.tsx`
- Modify: `webui/src/components/session/TestFacet.tsx`, `TestFacet.test.tsx`

**Interfaces:**
- Consumes: Task 1's types; the existing `MetricsV2Summary` on `test.evidenceLayers`.
- Produces: `<EvidenceAccounting layers={layers} />` — a collapsed `<details>` block.

- [ ] **Step 1: Write the failing test**

Create `webui/src/components/session/EvidenceAccounting.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import { EvidenceAccounting } from "./EvidenceAccounting"

const layers = {
  schemaVersion: 2 as const,
  identityVersion: "module-qualified-v1" as const,
  claimed: {
    latestSubjects: { executed: 47, passed: 47, failed: 0, errors: 0, skipped: 0 },
    latestCases: { executed: 994, passed: 933, failed: 0, errors: 0, skipped: 61 },
    receiptExecutions: {
      executed: 2048,
      passed: 2000,
      failed: 48,
      errors: 0,
      skipped: 0,
      availability: "partial" as const,
      bound: "lower" as const,
      reason: "row disclosure truncated",
    },
  },
  quarantinedObservations: { executed: null, passed: null, failed: null, errors: null, skipped: null, availability: "unavailable" as const, reason: "no module-qualified identity" },
  unattributedObservations: { executed: 12, passed: 10, failed: 2, errors: 0, skipped: 0 },
  staleObservations: { executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0 },
}

describe("EvidenceAccounting", () => {
  it("is collapsed until asked for", () => {
    render(<EvidenceAccounting layers={layers} />)
    expect(screen.getByText("Evidence accounting")).toBeInTheDocument()
    expect(screen.queryByText(/Results bound to this run's receipts/)).not.toBeVisible()
  })

  it("labels every layer in plain English", async () => {
    render(<EvidenceAccounting layers={layers} />)
    await userEvent.click(screen.getByText("Evidence accounting"))
    for (const label of [
      "Results bound to this run's receipts",
      "Tests identified by module and name",
      "Test classes identified by module and name",
      "Set aside: not from this run's receipts",
      "Set aside: no module or test name recorded",
      "Set aside: from an earlier run",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })

  it("marks a lower bound as a floor, not a total", async () => {
    render(<EvidenceAccounting layers={layers} />)
    await userEvent.click(screen.getByText("Evidence accounting"))
    expect(screen.getByText(/≥2,048 kept/)).toBeInTheDocument()
  })

  it("says why a layer is unavailable instead of showing zero", async () => {
    render(<EvidenceAccounting layers={layers} />)
    await userEvent.click(screen.getByText("Evidence accounting"))
    expect(screen.getByText(/unavailable: no module-qualified identity/)).toBeInTheDocument()
  })

  it("never uses the retired vocabulary", () => {
    const { container } = render(<EvidenceAccounting layers={layers} />)
    const text = (container.textContent ?? "").toLowerCase()
    for (const word of ["quarantined", "claimed", "subject", "verdict-bearing"]) {
      expect(text).not.toContain(word)
    }
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/components/session/EvidenceAccounting.test.tsx`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Write the block**

Create `webui/src/components/session/EvidenceAccounting.tsx`. It renders a `<details>` whose summary is `Evidence accounting` and whose body is one row per layer, in this order and with these labels:

| source field | label |
|---|---|
| `claimed.receiptExecutions` | Results bound to this run's receipts |
| `claimed.latestCases` | Tests identified by module and name |
| `claimed.latestSubjects` | Test classes identified by module and name |
| `quarantinedObservations` | Set aside: not from this run's receipts |
| `unattributedObservations` | Set aside: no module or test name recorded |
| `staleObservations` | Set aside: from an earlier run |

Each row's value is, in order of preference: `unavailable: {reason}` when `availability === "unavailable"`; `≥{executed} kept` when `availability === "partial" && bound === "lower"`; otherwise `{passed} / {executed} passed · {failed} failed · {errors} errors · {skipped} skipped`. Numbers use `toLocaleString()`. A row whose source is absent renders `not recorded`.

- [ ] **Step 4: Restructure the Tests tab**

In `TestFacet.tsx`, order the panel:

1. The headline counts, taken from `detail.resultCard`'s `tests` row (`headline` and `detail`) rather than recomputed.
2. Failing tests grouped by module, from `detail.modules`, with a Copy-all button per group (keep the existing `FailingCard` where it fits).
3. The per-module table (`ModuleTable variant="test"`, unchanged).
4. `<ReceiptTable receipts={testReceipts} />` where `testReceipts` filters `detail.receipts` to those with `testsReported != null`. `ReceiptTable` is Task 8's component; until it lands, render nothing for this section.
5. `<EvidenceAccounting layers={detail.test.evidenceLayers} />`.

Delete the `LayerRow` block and every string it used (`Verified test cases`, `Recorded test executions`, `Excluded observations`, `Observations without a test name`, `Earlier-run observations`).

- [ ] **Step 5: Run the tests**

Run: `npm test --prefix webui -- src/components/session`
Expected: PASS. `TestFacet.test.tsx` needs its `LayerRow` assertions replaced with the new labels.

- [ ] **Step 6: Commit**

```bash
git add webui/src/components/session
git commit -m "feat(webui): evidence accounting in plain English, collapsed by default"
```

---

### Task 8: Receipts in Build and Evidence

**Files:**
- Create: `webui/src/components/session/ReceiptTable.tsx`, `ReceiptTable.test.tsx`
- Modify: `webui/src/components/session/BuildFacet.tsx`, `webui/src/components/session/EvidenceTimeline.tsx` (or the Evidence tab body in `facets.tsx`), `TestFacet.tsx` (section 4 from Task 7)

**Interfaces:**
- Consumes: Task 2's `ReceiptSummary`.
- Produces: `<ReceiptTable receipts={receipts} caption={...} />`.

- [ ] **Step 1: Write the failing test**

Create `webui/src/components/session/ReceiptTable.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { ReceiptSummary } from "@/api/types"

import { ReceiptTable } from "./ReceiptTable"

const receipt: ReceiptSummary = {
  receiptId: "inv-maven-1-ee86ae186d94-0002",
  tool: "maven",
  argv: "/opt/apache-maven-3.9.9/bin/mvn clean verify",
  workingDirectory: "/workspace/commons-cli",
  actualCwd: "/workspace/commons-cli",
  exitCode: 0,
  outcome: "completed",
  lifecycleState: "finished",
  toolchain: { executable: "/opt/apache-maven-3.9.9/bin/mvn", version: "Apache Maven 3.9.9" },
  jdkMajor: "17",
  jdkVersion: "17.0.20",
  reportsNew: 47,
  reportsChanged: 0,
  testsReported: 994,
  roles: ["build", "test"],
}

describe("ReceiptTable", () => {
  it("shows the command that actually ran, with its exit code", () => {
    render(<ReceiptTable receipts={[receipt]} />)
    expect(screen.getByText("/opt/apache-maven-3.9.9/bin/mvn clean verify")).toBeInTheDocument()
    expect(screen.getByText("0")).toBeInTheDocument()
  })

  it("shows the toolchain the run observed, not the one it asked for", () => {
    render(<ReceiptTable receipts={[receipt]} />)
    expect(screen.getByText(/Apache Maven 3.9.9/)).toBeInTheDocument()
    expect(screen.getByText(/17.0.20/)).toBeInTheDocument()
  })

  it("counts the report files the command wrote", () => {
    render(<ReceiptTable receipts={[receipt]} />)
    expect(screen.getByText(/47 new/)).toBeInTheDocument()
  })

  it("states absence rather than zero for an unknown exit code", () => {
    render(<ReceiptTable receipts={[{ ...receipt, exitCode: null, testsReported: null }]} />)
    expect(screen.getAllByText("—").length).toBeGreaterThan(0)
  })

  it("teaches when there is nothing to show", () => {
    render(<ReceiptTable receipts={[]} />)
    expect(screen.getByText(/No commands were recorded/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/components/session/ReceiptTable.test.tsx`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Write the table**

Create `webui/src/components/session/ReceiptTable.tsx`. Columns: Command (mono, `argv`, with `actualCwd` beneath in muted 11px), Tool (`tool` + `toolchain.version`), Runtime (`JDK {jdkMajor} · {jdkVersion}`, or `—`), Exit (`exitCode` or `—`, red when non-zero), Reports (`{reportsNew} new` + `· {reportsChanged} changed` when non-zero, or `—`), Tests (`testsReported.toLocaleString()` or `—`). Header cells may use the uppercase tracked style; nothing else may. Empty state: `No commands were recorded for this run.`

- [ ] **Step 4: Use it in three places**

- `BuildFacet.tsx`: render `<ReceiptTable receipts={buildReceipts} />` above the module diagnostic table, where `buildReceipts` filters `detail.receipts` to `tool` in `{maven, gradle, bash, python}`. Keep the caption `Module counts are diagnostic. The project's CI defines the build scope.` on the module table.
- Evidence tab: render `<ReceiptTable receipts={detail.receipts ?? []} />` first, then the existing `EvidenceTimeline`.
- `TestFacet.tsx`: fill in Task 7's section 4.

- [ ] **Step 5: Run the tests**

Run: `npm test --prefix webui -- src/components/session`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add webui/src/components/session webui/src/pages/detail/facets.tsx
git commit -m "feat(webui): show what each command ran, with its toolchain and reports"
```

---

### Task 9: Overview leads with what needs attention

**Files:**
- Modify: `webui/src/pages/detail/OverviewTab.tsx`, `OverviewTab.test.tsx`
- Delete: `webui/src/components/session/NeedsAttention.tsx` if the card's `attention` fully replaces it (check its call sites first)

**Interfaces:**
- Consumes: Task 1's `ResultCard`.
- Produces: `<OverviewTab detail={detail} onOpenTab={...} />`.

- [ ] **Step 1: Write the failing test**

Replace the body of `webui/src/pages/detail/OverviewTab.test.tsx` with tests for:

```tsx
it("leads with what needs attention", () => {
  render(<OverviewTab detail={detailWithAttention()} onOpenTab={() => {}} />)
  const headings = screen.getAllByRole("heading").map((node) => node.textContent)
  expect(headings[0]).toMatch(/Needs attention/)
  expect(screen.getByText("commons-cli · 2 failing")).toBeInTheDocument()
})

it("says so plainly when nothing needs attention", () => {
  render(<OverviewTab detail={cleanDetail()} onOpenTab={() => {}} />)
  expect(screen.getByText("Nothing needs attention.")).toBeInTheDocument()
})

it("takes its build and test tiles from the card, word for word", () => {
  const detail = cleanDetail()
  render(<OverviewTab detail={detail} onOpenTab={() => {}} />)
  const buildRow = detail.resultCard!.rows.find((row) => row.key === "build")!
  expect(screen.getByText(buildRow.headline)).toBeInTheDocument()
  expect(screen.getByText(buildRow.detail!)).toBeInTheDocument()
})

it("shows the run's notes in a disclosure", async () => {
  render(<OverviewTab detail={detailWithNotes()} onOpenTab={() => {}} />)
  await userEvent.click(screen.getByText("Data notes"))
  expect(screen.getByText(/were set aside/)).toBeInTheDocument()
})

it("renders without a card at all", () => {
  render(<OverviewTab detail={{ ...cleanDetail(), resultCard: null }} onOpenTab={() => {}} />)
  expect(screen.getByText(/No result was recorded/)).toBeInTheDocument()
})
```

with local helpers building details whose `resultCard.attention` and `resultCard.notes` are populated.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/pages/detail/OverviewTab.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Rewrite the tab**

Order the panel:

1. **Needs attention** — `card.attention` as a list, one row per item: title bold, detail muted, `refs` as mono chips. Empty state: `Nothing needs attention.`
2. **Build** and **Tests** tiles — two tiles reading `card.row("build")` and `card.row("tests")`: `headline` as the tile value, `detail` beneath. No client-side recomputation.
3. **Per-module breakdown** — `ModuleTable variant="overview"`, unchanged; `Module details are not available for this run.` when absent.
4. **Data notes** — a `<details>` over `card.notes`, omitted when empty.

Delete the `Required task completion`, `Official CI comparison` and `Diagnostic observations` sections: the band states the first two and the Official CI tab holds the third. Delete the goal button — the trunk goal moves to the header's metadata line, and `card.goal` carries it.

- [ ] **Step 4: Run the tests**

Run: `npm test --prefix webui -- src/pages/detail`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail
git commit -m "feat(webui): the overview answers what needs me first"
```

---

### Task 10: The rail

**Files:**
- Modify: `webui/src/pages/WorkspaceRail.tsx`, `WorkspaceRail.test.tsx`, `webui/src/pages/dashboardAttention.ts`
- Delete: `webui/src/components/SummaryStrip.tsx`, `SummaryStrip.test.tsx`

**Interfaces:**
- Consumes: Task 2's `WorkspaceResult`.
- Produces: a one-line status strip and a four-cell result strip per row.

- [ ] **Step 1: Write the failing test**

Add to `webui/src/pages/WorkspaceRail.test.tsx`:

```tsx
it("summarises the fleet in one line, not three cards", () => {
  render(<WorkspaceRail {...props({ workspaces: [running(), failing(), healthy()] })} />)
  expect(screen.getByText(/3 workspaces · 1 running · 1 needs attention/)).toBeInTheDocument()
  expect(screen.queryByText("Build coverage")).not.toBeInTheDocument()
  expect(screen.queryByText("Run results")).not.toBeInTheDocument()
})

it("filters to what needs attention when asked", async () => {
  render(<WorkspaceRail {...props({ workspaces: [failing(), healthy()] })} />)
  await userEvent.click(screen.getByRole("button", { name: /needs attention/i }))
  expect(screen.queryByText(healthy().project!)).not.toBeInTheDocument()
  expect(screen.getByText(failing().project!)).toBeInTheDocument()
})

it("shows each row's four results", () => {
  render(<WorkspaceRail {...props({ workspaces: [healthy()] })} />)
  expect(screen.getByText("success")).toBeInTheDocument()
  expect(screen.getByText("1/1")).toBeInTheDocument()
  expect(screen.getByText("933/994")).toBeInTheDocument()
  expect(screen.getByText("met")).toBeInTheDocument()
})

it("marks red results on a row", () => {
  render(<WorkspaceRail {...props({ workspaces: [failing()] })} />)
  expect(screen.getByText("+2")).toBeInTheDocument()
})

it("puts an incomplete required task at the top", () => {
  const order = renderedOrder([healthy(), taskIncomplete()])
  expect(order[0]).toBe(taskIncomplete().id)
})
```

with local helpers `healthy()`, `failing()`, `running()`, `taskIncomplete()` returning `WorkspaceSummary` objects carrying a `result`.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --prefix webui -- src/pages/WorkspaceRail.test.tsx`
Expected: FAIL — the stat cards are still there.

- [ ] **Step 3: Replace the cards with a strip**

In `WorkspaceRail.tsx`, delete `RailSummary` and the three `Chip`s and render one line:

```tsx
<div className="flex items-center gap-2 px-3 py-2 text-[12px] text-muted-foreground">
  <span>{`${total} workspace${total === 1 ? "" : "s"}`}</span>
  {running > 0 ? <span>{`· ${running} running`}</span> : null}
  {attention > 0 ? (
    <button
      aria-pressed={attentionOnly}
      className={cn(
        "rounded-full px-2 py-0.5 text-[11px] font-semibold",
        attentionOnly
          ? "bg-status-attention text-background"
          : "bg-status-attention-soft text-status-attention",
      )}
      onClick={() => setAttentionOnly((value) => !value)}
      type="button"
    >
      {`${attention} needs attention`}
    </button>
  ) : null}
</div>
```

```bash
git rm webui/src/components/SummaryStrip.tsx webui/src/components/SummaryStrip.test.tsx
```

`rollup()` moves into `WorkspaceRail.tsx` only if it is still needed after the strip lands; it is not, so delete it with the file.

- [ ] **Step 4: Add the row result strip**

In `RailRow`, replace the build icon and `TestBar` with a right-aligned four-cell strip reading `workspace.result`:

| cell | content |
|---|---|
| Setup | `result.verdict`, toned |
| Task | `{completed}/{required}` or `—` |
| Tests | `{passed}/{executed}`, plus a red `+{failed+errors}` when non-zero, or `—` |
| CI | `result.ci?.verdict` with `_` replaced by a space, or `—` |

- [ ] **Step 5: Extend attention ordering**

In `dashboardAttention.ts`, add to the attention predicate: `result.verdict === "failed"`, `result.task?.status === "incomplete"`, and `result.ci?.verdict` in `{"not_met", "invalid"}`.

- [ ] **Step 6: Run the tests**

Run: `npm test --prefix webui -- src/pages`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A webui/src/pages webui/src/components
git commit -m "feat(webui): one status line and a four-cell result on every rail row"
```

---

### Task 11: Copy pass, lint, and ship the bundle

**Files:**
- Create: `webui/src/test/copy.test.ts`
- Create: `tests/test_gloss_parity.py`
- Modify: every file the lint flags; `docs/DESIGN.md`, `docs/Setup-Agent UI/README.md`
- Delete: `webui/src/components/session/BuildCard.tsx`, `BuildCard.test.tsx`, `TestCard.tsx`, `TestCard.test.tsx`

**Interfaces:**
- Consumes: Tasks 1-10.
- Produces: a lint that fails the build on retired vocabulary; the rebuilt bundle under `src/sag/web/static`.

- [ ] **Step 1: Write the failing test**

Create `webui/src/test/copy.test.ts`:

```ts
import { readFileSync } from "node:fs"
import { join } from "node:path"

import fg from "fast-glob"
import { describe, expect, it } from "vitest"

const FORBIDDEN = [
  "sealed",
  "canonical",
  "quarantined",
  "verdict-bearing",
  "metrics-v2",
  "promoting",
]

/** Text a person reads: JSX text nodes, and the string literals in the props
 *  that become text. Identifiers and field names are not copy. */
function visibleText(source: string): string[] {
  const found: string[] = []
  for (const match of source.matchAll(/>\s*([A-Za-z][^<>{}]{3,})\s*</g)) found.push(match[1])
  for (const match of source.matchAll(/(?:label|title|aria-label|placeholder)=["']([^"']+)["']/g))
    found.push(match[1])
  for (const match of source.matchAll(/["'`]([A-Z][a-z][^"'`]{6,})["'`]/g)) found.push(match[1])
  return found
}

describe("user-visible copy", () => {
  const files = fg.sync("src/**/*.{ts,tsx}", {
    cwd: join(__dirname, "..", ".."),
    absolute: true,
    ignore: ["**/*.test.ts", "**/*.test.tsx", "**/api/types.ts", "**/lib/ciGlosses.ts"],
  })

  it("never shows the retired vocabulary", () => {
    const offenders: string[] = []
    for (const file of files) {
      const source = readFileSync(file, "utf8")
      for (const text of visibleText(source)) {
        const lowered = text.toLowerCase()
        for (const word of FORBIDDEN) {
          if (lowered.includes(word)) offenders.push(`${file}: ${text.trim()}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it("has no dead components left in the tree", () => {
    for (const name of [
      "SummaryStrip",
      "BuildCard",
      "TestCard",
      "FacetTabs",
      "ContextTrace",
      "FlowTab",
      "ActionDetailModal",
      "FilesDigest",
      "VerdictBand",
      "scrollSpy",
    ]) {
      expect(fg.sync(`src/**/${name}.{ts,tsx}`, { cwd: join(__dirname, "..", "..") })).toEqual([])
    }
  })
})
```

Create `tests/test_gloss_parity.py`:

```python
"""The browser and the terminal explain a reason code the same way."""

import re
from pathlib import Path

from sag.result_card.glosses import REASON_GLOSS

_TS = Path("webui/src/lib/ciGlosses.ts")


def _typescript_glosses() -> dict[str, str]:
    source = _TS.read_text(encoding="utf-8")
    body = source[source.index("{") : source.rindex("}") + 1]
    return {
        match.group("code"): match.group("text")
        for match in re.finditer(
            r'"(?P<code>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"(?P<text>[^"]*)"', body
        )
    }


def test_both_tables_hold_the_same_codes():
    assert set(_typescript_glosses()) == set(REASON_GLOSS)


def test_both_tables_hold_the_same_sentences():
    assert _typescript_glosses() == REASON_GLOSS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npm test --prefix webui -- src/test/copy.test.ts` and `PYTHONPATH=.:tests uv run pytest tests/test_gloss_parity.py -v`
Expected: FAIL on leftover copy and on any gloss drift. Install `fast-glob` as a dev dependency if it is missing: `npm install --prefix webui --save-dev fast-glob`.

- [ ] **Step 3: Fix every offender**

Work the list the lint prints. Typical fixes: `"This generated report is preserved for context. When its numbers differ from the sealed Overview…"` → `"…differ from the recorded result…"`; `"Canonical verdict unknown"` → `"No result was recorded"`; `"Review before promoting"` → `"Review before relying on it"`. Delete the last dead components:

```bash
git rm webui/src/components/session/BuildCard.tsx webui/src/components/session/BuildCard.test.tsx
git rm webui/src/components/session/TestCard.tsx webui/src/components/session/TestCard.test.tsx
```

Raise any remaining sub-11px text (`WorkspaceRail.tsx`'s 8.5px labels) to 11px and change 10px uppercase tracked labels to sentence case outside table headers and chips.

- [ ] **Step 4: Update the design docs**

In `docs/DESIGN.md`, correct the front-matter note that dark theme is dormant (the toggle ships) and replace `SummaryBand` in the component inventory. In `docs/Setup-Agent UI/README.md`, remove `SummaryBand`, `BuildCard`, `TestCard` and `FacetTabs` from the inventory and add `ResultBand`, `ReceiptTable`, `EvidenceAccounting`, `OfficialCITab`.

- [ ] **Step 5: Run everything**

Run: `uv run python scripts/ship_gate.py`
Expected: `SHIP GATE PASSED`. It runs all four legs — `npm test`, `(cd webui && npx tsc -b)`, `npm run build`, and pytest — without `&&`, so every leg runs whatever the one before it did and each prints its own verdict. The frontend count will differ from the 352 baseline; what matters is zero failures. The pytest leg passes when the FAILED set equals the documented pre-existing list and gains nothing.

- [ ] **Step 6: Look at it**

```bash
uv run sag ui --demo --port 8765
```

Open `http://127.0.0.1:8765` and check, at 1180px and at 400px, in light and dark:
- the band's seven rows are legible and the whole run reads in under a second;
- the Trajectory tab shows phase bands and one row per turn with summaries;
- the Official CI tab renders both the evaluated and the not-compared shapes;
- no text is smaller than 11px and nothing is horizontally scrolled except a table inside its own scroller.

Then, against one real recorded run, confirm the Trajectory tab renders a real ledger:

```bash
uv run sag ui --port 8765
```

- [ ] **Step 7: Build and commit the bundle**

```bash
npm run build --prefix webui
git add -A webui src/sag/web/static docs/DESIGN.md "docs/Setup-Agent UI/README.md" tests/test_gloss_parity.py
git commit -m "feat(webui): plain-English copy throughout, dead components gone, bundle rebuilt"
```

---

## Self-Review

**Spec coverage.** §5.1 read-model changes → phase 1 Task 11 (`resultCard`, removals) and this plan's Task 2 (`receipts`, `WorkspaceResult`, demo data). §5.2 TypeScript types → Task 1. §5.3 rail → Task 10. §5.4: header keeps its anatomy (untouched); result band → Task 3; the tab table → Task 6, with Trajectory in Task 4, Official CI in Task 5, Tests in Tasks 7-8, Build in Task 8, Evidence in Task 8, Overview in Task 9, Logs and Report untouched; the removals list → Tasks 3, 4, 6, 10, 11. §5.5 accounting labels → Task 7. §5.6 copy and type rules → Task 11.

One spec item is deliberately deferred: §5.4 says `context` (ContextTrace) "stays in the API for now and is no longer read by the frontend". Task 4 deletes the frontend consumers; the backend field stays, as the spec says, and its removal is the follow-up the spec names.

One addition beyond the spec: `tests/test_gloss_parity.py`. The spec has the frontend render `card.rows` verbatim, which covers the result band, but the Official CI tab glosses `reason_codes` client-side, so a second table exists and needs a fence. The alternative — serving glosses from the API — is a larger change for the same guarantee.

**Placeholder scan.** No "TBD", no "add appropriate error handling". Task 5 Step 4 and Task 7 Step 3 describe their components as an ordered specification of sections with exact labels and exact fallback strings rather than a full code listing, because each is a straightforward layout over types already defined and the tests above them pin every string that matters. Task 9's helpers (`detailWithAttention`, `cleanDetail`, `detailWithNotes`) are named and their required contents stated. Task 10's `healthy()` / `failing()` / `running()` / `taskIncomplete()` likewise.

**Type consistency.** `ResultCard`, `ResultRow`, `AttentionItem`, `ReceiptSummary`, `CIComparison`, `Attainment`, `TaskCompletion`, `WorkspaceResult` are defined once in Task 1 and used under those names in Tasks 3, 5, 7, 8, 9 and 10. `TabId` gains `trajectory` and `ci` and loses `timeline`, `flow` and `files` in **Task 3**, because `ResultBand`'s `ROW_TAB` links a row to the tab that answers it and therefore needs the final vocabulary; Task 6 then changes only `buildDetailTabs` and `TabBody` to match. The first draft of this plan widened the union in Task 6 and had Task 3 reference names that did not exist yet. `gloss(code)` has the same name in `webui/src/lib/ciGlosses.ts` (Task 5) and `src/sag/result_card/glosses.py` (phase 1 Task 1), and Task 11 asserts the two tables are identical. The backend `ReceiptSummary` field names in Task 2 are snake_case with camelCase aliases; Task 1's TypeScript interface uses the camelCase alias names, which is what the API serves.
