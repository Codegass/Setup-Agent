# Workbench Result-Detail Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-architect the Workbench detail pane into a tabbed view (Overview default) with one synthesized verdict, a flow-first agent trace, and an action output/observation modal — matching the downloaded Claude Design template.

**Architecture:** Backend gains a server-composed `verdict` plus `model`/`steps`/`stepBudget` on `ExecutionSessionDetail` (all nullable). Frontend replaces the scroll-spy shell with a real tab switcher: `VerdictBand` (replaces `SummaryBand`), restyled `DetailHeader`, new `OverviewTab` + `FlowTab` + `ActionDetailModal`, with existing facet components reused as panels.

**Tech Stack:** Python 3 + Pydantic (`src/sag/web`), React 18 + TypeScript + Tailwind v4 + Vitest (`webui`), pytest.

## Global Constraints

- **NEVER run `npm run build`; NEVER stage/modify/commit `src/sag/web/static/`.** Ships source only.
- Stage exact paths in commits (no `git add -A`/`.`). No `Co-Authored-By` trailer.
- `docs/` is gitignored — `git add -f` design/plan/spec files.
- Pydantic web models: camelCase via `serialization_alias`, `model_dump(mode="json", by_alias=True)`; nullable fields default `None`.
- Frontend tokens: status family `text-status-{idle,running,success,failed,attention}`, `bg-status-*-soft`, `border-status-*-border`; primary `bg-primary`/`text-primary`. Reuse `cn()` from `@/lib/utils`.
- Visual reference (exact markup/styling per component): `docs/Setup-Agent UI/templates/workbench-detail/WorkbenchDetail.dc.html` (cited by line ranges per task) and `docs/Setup-Agent UI/_review/*.png`.
- All work on branch `feat/webui-detail-redesign`. Tests must pass before each commit. Run vitest from `webui/` (`npx vitest run`), pytest from repo root (`uv run pytest`).

## File structure

**Backend (`src/sag/`):**
- `agent/react_engine.py` (MODIFY) — `get_execution_summary()` adds `model` + `max_iterations`.
- `web/models.py` (MODIFY) — `VerdictSummary` model; `ExecutionSessionDetail` + `verdict`/`model`/`steps`/`stepBudget`.
- `web/verdict.py` (CREATE) — `compose_verdict(build, test, module_summary, outcome, blocker) -> dict | None`.
- `web/session_registry.py` (MODIFY) — populate new fields in `_session_detail`.
- `web/demo_data.py` (MODIFY) — demo values for the new fields.

**Frontend (`webui/src/`):**
- `api/types.ts` (MODIFY) — `VerdictSummary` + new `ExecutionSessionDetail` fields.
- `pages/detail/VerdictBand.tsx` (CREATE) — one-sentence toned band.
- `pages/detail/DetailHeader.tsx` (MODIFY) — single-row + metadata mono-line + ⋯ menu.
- `pages/detail/facets.tsx` (MODIFY) — tab model (Overview + Flow + facet tabs, gating, counts).
- `pages/detail/OverviewTab.tsx` (CREATE) — goal button + KPI tiles + ModuleTable(overview) + NeedsAttention.
- `pages/detail/FlowTab.tsx` (CREATE) — goal trunk + phase timeline + actions → modal.
- `pages/detail/DetailPane.tsx` (MODIFY) — tab-switcher shell.
- `components/session/ModuleTable.tsx` (MODIFY) — `overview` variant.
- `components/session/NeedsAttention.tsx` (CREATE).
- `components/session/ActionDetailModal.tsx` (CREATE).
- `pages/detail/SummaryBand.tsx` (DELETE after VerdictBand wired) + its test.

---

## Task 1: Backend — surface model + max_iterations in execution summary

**Files:**
- Modify: `src/sag/agent/react_engine.py` (`get_execution_summary`, ~line 1721)
- Test: `tests/agent/test_execution_summary.py` (create)

**Interfaces:**
- Produces: `get_execution_summary()` dict additionally has `"model": str` and `"max_iterations": int`.

- [ ] **Step 1: Write the failing test**
```python
# tests/agent/test_execution_summary.py
from unittest.mock import MagicMock

def test_execution_summary_includes_model_and_budget(make_react_engine):
    engine = make_react_engine()  # fixture builds a ReActEngine with a stub Config
    engine.config.get_litellm_model_name = MagicMock(return_value="claude-sonnet-4.5")
    engine.max_iterations = 40
    summary = engine.get_execution_summary()
    assert summary["model"] == "claude-sonnet-4.5"
    assert summary["max_iterations"] == 40
```
If a `make_react_engine` fixture is impractical, instead assert on a thin helper extracted from `get_execution_summary` (`_summary_runtime(self) -> dict`) unit-tested directly. Prefer the extracted helper.

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/agent/test_execution_summary.py -v` → FAIL (KeyError `model`).

- [ ] **Step 3: Implement** — in `get_execution_summary()` return dict, add:
```python
"model": self.config.get_litellm_model_name("action"),
"max_iterations": getattr(self, "max_iterations", None) or getattr(self.config, "max_iterations", None),
```

- [ ] **Step 4: Run test** → PASS.

- [ ] **Step 5: Commit** — `git add src/sag/agent/react_engine.py tests/agent/test_execution_summary.py && git commit -m "feat(agent): record model + max_iterations in execution summary"`

> Note: these reach the read model via report metrics (report_tool already snapshots the execution summary). Older sessions lack them → backend ships null (graceful).

---

## Task 2: Backend — verdict composition helper

**Files:**
- Create: `src/sag/web/verdict.py`
- Test: `tests/web/test_verdict.py` (create)

**Interfaces:**
- Produces: `compose_verdict(*, build: dict | None, test: dict | None, module_summary: dict | None, outcome: str, blocker: dict | None) -> dict | None` returning `{"tone": "success"|"attention"|"failed", "headline": str, "detail": str | None}` (or `None` when there's nothing to say).

- [ ] **Step 1: Write the failing tests**
```python
# tests/web/test_verdict.py
from sag.web.verdict import compose_verdict

MS = {"modulesTotal": 4, "modulesBuilt": 3, "modulesFailed": 1, "singleModule": False}
TEST = {"state": "partial", "pass": 1186, "fail": 7, "total": 1205}

def test_partial_verdict():
    v = compose_verdict(build={"state": "success"}, test=TEST, module_summary=MS,
                        outcome="⚠️ PARTIAL", blocker=None)
    assert v["tone"] == "attention"
    assert "3 of 4 modules" in v["headline"]
    assert "7 of 1,205 tests failing" in v["headline"]
    assert "review before promoting" in v["headline"]

def test_success_verdict():
    v = compose_verdict(build={"state": "success"},
                        test={"state": "success", "pass": 1205, "fail": 0, "total": 1205},
                        module_summary={"modulesTotal": 4, "modulesBuilt": 4, "singleModule": False},
                        outcome="✅ SUCCESS", blocker=None)
    assert v["tone"] == "success"
    assert "all 4 modules" in v["headline"]

def test_failed_verdict_with_blocker_hint():
    v = compose_verdict(build={"state": "failed"}, test={"state": "none", "pass": 0, "fail": 0, "total": 0},
                        module_summary={"modulesTotal": 4, "modulesBuilt": 0, "modulesFailed": 1, "singleModule": False},
                        outcome="❌ FAILED",
                        blocker={"hint": "fix the missing dependency in acme-cli"})
    assert v["tone"] == "failed"
    assert v["detail"] == "fix the missing dependency in acme-cli"

def test_single_module_phrasing():
    v = compose_verdict(build={"state": "success"},
                        test={"state": "success", "pass": 320, "fail": 0, "total": 320},
                        module_summary={"singleModule": True}, outcome="✅ SUCCESS", blocker=None)
    assert "module" not in v["headline"].lower() or "modules" not in v["headline"]
    assert "320 tests passing" in v["headline"]

def test_returns_none_when_empty():
    assert compose_verdict(build=None, test=None, module_summary=None, outcome="", blocker=None) is None
```

- [ ] **Step 2: Run** — `uv run pytest tests/web/test_verdict.py -v` → FAIL (module not found).

- [ ] **Step 3: Implement** `src/sag/web/verdict.py`:
```python
"""Server-composed one-sentence verdict for the Workbench detail band."""
from __future__ import annotations


def _tone(outcome: str, build: dict | None, test: dict | None) -> str:
    o = (outcome or "").lower()
    if "fail" in o or (build and str(build.get("state", "")).lower() in {"failed", "failure"}):
        return "failed"
    if "partial" in o or (test and int(test.get("fail", 0) or 0) > 0):
        return "attention"
    return "success"


def _build_clause(build: dict | None, ms: dict | None) -> str | None:
    if not build:
        return None
    state = str(build.get("state", "")).lower()
    if ms and not ms.get("singleModule", False) and ms.get("modulesTotal"):
        total, built = int(ms["modulesTotal"]), int(ms.get("modulesBuilt", 0) or 0)
        if built >= total:
            return f"Build passed on all {total} modules"
        return f"Build passed on {built} of {total} modules" if built else f"Build failed — 0 of {total} modules compiled"
    if state in {"failed", "failure"}:
        return "Build failed"
    return "Build passed" if state in {"success", "ok"} else None


def _test_clause(test: dict | None) -> str | None:
    if not test:
        return None
    total = int(test.get("total", 0) or 0)
    fail = int(test.get("fail", 0) or 0)
    if total <= 0:
        return None
    if fail == 0:
        return f"{total:,} tests passing"
    return f"{fail:,} of {total:,} tests failing"


def compose_verdict(*, build, test, module_summary, outcome, blocker) -> dict | None:
    clauses = [c for c in (_build_clause(build, module_summary), _test_clause(test)) if c]
    if not clauses:
        return None
    tone = _tone(outcome, build, test)
    headline = ". ".join(clauses)
    if tone != "success":
        headline += " — review before promoting"
    detail = (blocker or {}).get("hint") if blocker else None
    return {"tone": tone, "headline": headline, "detail": detail}
```

- [ ] **Step 4: Run** → PASS. Adjust phrasing assertions/impl together if the comma-format (`1,205`) differs; keep `{n:,}` formatting.

- [ ] **Step 5: Commit** — `git add src/sag/web/verdict.py tests/web/test_verdict.py && git commit -m "feat(web): server-composed verdict helper"`

---

## Task 3: Backend — read-model fields + wiring + demo

**Files:**
- Modify: `src/sag/web/models.py` (`ExecutionSessionDetail`, ~line 514; add `VerdictSummary`)
- Modify: `src/sag/web/session_registry.py` (`_session_detail`, ~line 365–546)
- Modify: `src/sag/web/demo_data.py`
- Test: `tests/web/test_session_detail_fields.py` (create)

**Interfaces:**
- Consumes: `compose_verdict` (Task 2); `metrics` dict from `_read_report_metrics` (has `total_iterations`, and after Task 1 `model`/`max_iterations`).
- Produces: `ExecutionSessionDetail.verdict: VerdictSummary | None`, `.model: str | None`, `.steps: int | None`, `.step_budget: int | None` (alias `stepBudget`).

- [ ] **Step 1: Write the failing test**
```python
# tests/web/test_session_detail_fields.py
from sag.web.models import ExecutionSessionDetail, VerdictSummary

def test_detail_serializes_new_fields_camelcase():
    d = ExecutionSessionDetail.model_validate({
        "id": "S1", "workspace": "w", "title": "t", "status": "partial", "entry": "e",
        "start": "now", "duration": "1s", "outcome": "⚠️ PARTIAL", "report": "ready",
        "build": {"state": "success", "tool": "maven", "time": "2m", "note": ""},
        "test": {"state": "partial", "pass": 1, "fail": 1, "skip": 0, "total": 2},
        "evidence": [], "logs": [],
        "verdict": {"tone": "attention", "headline": "x", "detail": None},
        "model": "claude-sonnet-4.5", "steps": 6, "stepBudget": 40,
    })
    out = d.model_dump(mode="json", by_alias=True)
    assert out["verdict"]["tone"] == "attention"
    assert out["model"] == "claude-sonnet-4.5"
    assert out["stepBudget"] == 40

def test_new_fields_default_none():
    d = ExecutionSessionDetail.model_validate({
        "id": "S1", "workspace": "w", "title": "t", "status": "ok", "entry": "e",
        "start": "now", "duration": "1s", "outcome": "", "report": "none",
        "build": {"state": "success", "tool": "maven", "time": "", "note": ""},
        "test": {"state": "none", "pass": 0, "fail": 0, "skip": 0, "total": 0},
        "evidence": [], "logs": [],
    })
    out = d.model_dump(mode="json", by_alias=True)
    assert out["verdict"] is None and out["model"] is None and out["stepBudget"] is None
```

- [ ] **Step 2: Run** — `uv run pytest tests/web/test_session_detail_fields.py -v` → FAIL.

- [ ] **Step 3: Implement** — in `models.py` add:
```python
class VerdictSummary(WebModel):
    tone: str  # "success" | "attention" | "failed"
    headline: str
    detail: str | None = None
```
and on `ExecutionSessionDetail`:
```python
verdict: VerdictSummary | None = None
model: str | None = None
steps: int | None = None
step_budget: int | None = Field(default=None, serialization_alias="stepBudget", validation_alias=AliasChoices("step_budget", "stepBudget"))
```
In `session_registry._session_detail`, after `metrics`/`build_payload`/`module_summary` are computed, build the payload entries:
```python
from .verdict import compose_verdict
...
"verdict": compose_verdict(
    build=build_payload, test=test, module_summary=payload.get("module_summary"),
    outcome=payload.get("outcome", ""), blocker=payload.get("blocker"),
),
"model": _text(metrics.get("model")) or None,
"steps": metrics.get("total_iterations"),
"step_budget": metrics.get("max_iterations"),
```
(Place these in the same dict literal that constructs the detail payload near line 533–546; match the existing key names for `build`/`test`/`module_summary`/`outcome`/`blocker`.)

- [ ] **Step 4: Demo** — in `demo_data.py` `get_demo_session`, set `verdict={"tone":"attention","headline":"Build passed on 3 of 4 modules. 7 of 1,205 tests failing across acme-cli and acme-web — review before promoting","detail":None}`, `model="claude-sonnet-4.5"`, `steps=6`, `step_budget=40` (or via the model field names).

- [ ] **Step 5: Run** — `uv run pytest tests/web/ -v` → PASS.

- [ ] **Step 6: Commit** — `git add src/sag/web/models.py src/sag/web/session_registry.py src/sag/web/demo_data.py tests/web/test_session_detail_fields.py && git commit -m "feat(web): verdict/model/steps/stepBudget on session detail"`

---

## Task 4: Frontend — API types

**Files:**
- Modify: `webui/src/api/types.ts`
- Test: none (type-only; covered by component tests).

**Interfaces:**
- Produces: `VerdictSummary` type; `ExecutionSessionDetail` gains `verdict?`, `model?`, `steps?`, `stepBudget?`.

- [ ] **Step 1: Implement** — add:
```ts
export interface VerdictSummary {
  tone: "success" | "attention" | "failed"
  headline: string
  detail?: string | null
}
```
and on `ExecutionSessionDetail`:
```ts
  verdict?: VerdictSummary | null
  model?: string | null
  steps?: number | null
  stepBudget?: number | null
```

- [ ] **Step 2: Type-check** — `cd webui && npx tsc -p tsconfig.app.json --noEmit` → clean.

- [ ] **Step 3: Commit** — `git add webui/src/api/types.ts && git commit -m "feat(webui): verdict/model/steps types"`

---

## Task 5: Frontend — VerdictBand

**Files:**
- Create: `webui/src/pages/detail/VerdictBand.tsx`
- Test: `webui/src/pages/detail/VerdictBand.test.tsx`

**Interfaces:**
- Produces: `export function VerdictBand({ detail }: { detail: ExecutionSessionDetail })`.

- [ ] **Step 1: Write the failing test**
```tsx
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { VerdictBand } from "./VerdictBand"
afterEach(() => cleanup())

describe("VerdictBand", () => {
  it("renders the verdict headline with attention tone", () => {
    render(<VerdictBand detail={{ verdict: { tone: "attention", headline: "Build passed on 3 of 4 modules. 7 failing — review before promoting" }, outcome: "⚠️ PARTIAL" } as any} />)
    expect(screen.getByText(/7 failing — review before promoting/)).toBeInTheDocument()
  })
  it("falls back to outcome when verdict is null", () => {
    render(<VerdictBand detail={{ verdict: null, outcome: "⚠️ PARTIAL" } as any} />)
    expect(screen.getByText(/PARTIAL/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run** — `cd webui && npx vitest run src/pages/detail/VerdictBand.test.tsx` → FAIL.

- [ ] **Step 3: Implement** — render a toned band; tone→classes via the status family (`attention`→`bg-status-attention-soft border-status-attention-border text-status-attention`, `success`→`...success...`, `failed`→`...failed...`). Headline 13px; if `detail` present render it on a second muted line ("Why"). Fallback: when `verdict` is null, render `detail.outcome` with neutral/attention tone. Styling reference: `WorkbenchDetail.dc.html` verdict band (the amber `PARTIAL` row near the top of the AFTER block).

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit** — `git add webui/src/pages/detail/VerdictBand.tsx webui/src/pages/detail/VerdictBand.test.tsx && git commit -m "feat(webui): VerdictBand"`

---

## Task 6: Frontend — DetailHeader restyle

**Files:**
- Modify: `webui/src/pages/detail/DetailHeader.tsx`
- Test: `webui/src/pages/detail/DetailHeader.test.tsx` (update)

**Interfaces:**
- Consumes: existing props (`workspace`, `detail?`, `sessionId`, `onSession`, `onNewTask`, `onTerminal`, `onSettings`, `onDelete`).
- Produces: same component name; new metadata mono-line + ⋯ menu.

- [ ] **Step 1: Update the test** — assert the metadata line shows model + steps when present:
```tsx
it("shows model and steps in the metadata line", () => {
  render(<DetailHeader workspace={{ id: "sag-acme", project: "acme-platform", stack: "maven", commit: "9f8e7d6" } as any}
                       detail={{ model: "claude-sonnet-4.5", steps: 6, stepBudget: 40, duration: "8m 01s" } as any}
                       sessionId="S1" {...noopHandlers} />)
  expect(screen.getByText(/claude-sonnet-4\.5/)).toBeInTheDocument()
  expect(screen.getByText(/6\s*\/\s*40 steps/)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — row 1: bold project + `setup`/entry tag; right-aligned actions New task (primary) / Terminal / Settings / ⋯ (menu: Delete + session switcher when `workspace.sessions?.length > 1`). Row 2: mono line joining present pieces with ` · `: `workspace.container`, `workspace.stack`, `commit`, `detail.model`, `${detail.steps}/${detail.stepBudget} steps` (or `${steps} steps` if no budget), `detail.duration`, `finished ${workspace.updated}`. Omit null/empty. Reference: `WorkbenchDetail.dc.html` lines 27–47 (header block).

- [ ] **Step 4: Run** → PASS (use `getAllByLabelText`/`getAllByText` where the dual desktop/mobile render duplicates nodes).

- [ ] **Step 5: Commit** — `git add webui/src/pages/detail/DetailHeader.tsx webui/src/pages/detail/DetailHeader.test.tsx && git commit -m "feat(webui): single-row DetailHeader with run metadata"`

---

## Task 7: Frontend — tab model in facets.tsx

**Files:**
- Modify: `webui/src/pages/detail/facets.tsx`
- Test: `webui/src/pages/detail/facets.test.tsx` (update)

**Interfaces:**
- Produces: `buildDetailTabs(detail): TabMeta[]` where `TabMeta = { id: string; label: string; count?: number; tone?: "red"|"neutral" }`. IDs: `overview`, `flow`, `tests`, `build`, `files`, `evidence`, `logs`, `report`. Plus `TabBody({ tabId, detail, ... })` switch rendering the panel for a tab.

- [ ] **Step 1: Update tests** — assert `buildDetailTabs` returns `overview` first and `flow`, includes `tests` with `count` = `detail.test.fail` and `tone:"red"` when failing, and OMITS tabs with no data (e.g. no `files` tab when `detail.files` is null).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — `buildDetailTabs` always includes `overview`; includes `flow` when `detail.context` present; `tests`/`build` always (core); `files`/`evidence`/`logs`/`report` when their data present (mirror current `buildDetailFacets` gating). `TabBody` switch: `overview`→`<OverviewTab detail={detail}/>`, `flow`→`<FlowTab detail={detail}/>`, `tests`→`<TestFacet detail={detail}/>`, `build`→`<BuildFacet detail={detail}/>`, `files`→`<FilesDigest digest={detail.files}/>`, `evidence`→`<EvidenceTimeline groups={detail.evidence}/>`, `logs`→`<LogsView logs={detail.logs}/>`, `report`→`<ReportDoc doc={detail.reportDoc}/>`. (Keep the existing `buildDetailFacets`/`FacetBody` until DetailPane switches over in Task 12; add the new exports alongside.)

- [ ] **Step 4: Run** → PASS. `cd webui && npx tsc -p tsconfig.app.json --noEmit` clean.

- [ ] **Step 5: Commit** — `git add webui/src/pages/detail/facets.tsx webui/src/pages/detail/facets.test.tsx && git commit -m "feat(webui): tab model for detail pane"`

---

## Task 8: Frontend — ModuleTable overview variant

**Files:**
- Modify: `webui/src/components/session/ModuleTable.tsx`
- Test: `webui/src/components/session/ModuleTable.test.tsx` (update)

**Interfaces:**
- Consumes/Produces: `variant: "build" | "test" | "overview"`. The `overview` variant renders columns: Module · Build (status dot+label) · Tests (`P/T` + bar + `N failing`) · Line cov (bar) · Branch cov (bar).

- [ ] **Step 1: Update test** — render `<ModuleTable modules={fixtureModules} variant="overview" />`; assert it shows a module name, a "Built"/"Failed" build cell, a `540 / 542` tests cell, and a `86.4%` line-cov cell.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — add the `overview` branch using the existing cell helpers (status colors, `CoverageBar`, `passRate`/`num`). Column layout per `WorkbenchDetail.dc.html` lines 148–178 (the Overview per-module table grid). Reuse existing build-status + coverage cell renderers; do not duplicate them.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit** — `git add webui/src/components/session/ModuleTable.tsx webui/src/components/session/ModuleTable.test.tsx && git commit -m "feat(webui): ModuleTable overview variant"`

---

## Task 9: Frontend — NeedsAttention + OverviewTab

**Files:**
- Create: `webui/src/components/session/NeedsAttention.tsx`
- Create: `webui/src/pages/detail/OverviewTab.tsx`
- Test: `webui/src/components/session/NeedsAttention.test.tsx`, `webui/src/pages/detail/OverviewTab.test.tsx`

**Interfaces:**
- Produces: `NeedsAttention({ modules, warnings })`; `OverviewTab({ detail, onOpenFlow })`.

- [ ] **Step 1: NeedsAttention test** — given modules with `failingNames`, assert it groups by module (`acme-cli · 2 failing` + the two names) and renders warnings; renders nothing when there are no failures and no warnings.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement NeedsAttention** — iterate `modules.filter(m => (m.failingNames?.length))`, header `name · ${count} failing`, list names with `+N more` past 5; then a warnings row per `warnings`. Bordered amber card. Reference: `WorkbenchDetail.dc.html` lines 182–200.

- [ ] **Step 4: OverviewTab test** — assert: goal button calls `onOpenFlow` on click; KPI tiles show pass rate / failing / modules built; coverage tiles ABSENT when `moduleSummary.lineRate == null`; renders `ModuleTable variant="overview"` and `NeedsAttention`.

- [ ] **Step 5: Run** → FAIL.

- [ ] **Step 6: Implement OverviewTab** — goal button (`detail.context?.trunk.goal` + progress + "View flow →" → `onOpenFlow`); KPI tiles derived from `detail.test` (passRate, fail), `detail.moduleSummary` (modulesBuilt/Total, lineRate, branchRate), `detail.build.time`/`note`; coverage tiles conditional on rates present; then `<ModuleTable modules={detail.modules ?? []} variant="overview"/>` (guarded for single-module) and `<NeedsAttention modules={detail.modules ?? []} warnings={detail.build.warnings ?? []}/>`. KPI-tile markup per `WorkbenchDetail.dc.html` lines 108–140.

- [ ] **Step 7: Run** both tests → PASS; `npx tsc -p tsconfig.app.json --noEmit` clean.

- [ ] **Step 8: Commit** — `git add webui/src/components/session/NeedsAttention.tsx webui/src/components/session/NeedsAttention.test.tsx webui/src/pages/detail/OverviewTab.tsx webui/src/pages/detail/OverviewTab.test.tsx && git commit -m "feat(webui): OverviewTab + NeedsAttention"`

---

## Task 10: Frontend — ActionDetailModal

**Files:**
- Create: `webui/src/components/session/ActionDetailModal.tsx`
- Test: `webui/src/components/session/ActionDetailModal.test.tsx`

**Interfaces:**
- Consumes: a context-trace action (`{ toolName, success, output, observation, refs, dispatchStatus }`) — type `ExecutionSessionDetail["context"]` phase task iteration action shape.
- Produces: `ActionDetailModal({ action, onClose })` (reuses `Dialog` from `@/components/ui/dialog`, like `ModuleBreakdownDialog`).

- [ ] **Step 1: Write the failing test**
```tsx
it("shows tool output and observation separately", () => {
  render(<ActionDetailModal onClose={() => {}} action={{ toolName: "build", success: true,
    output: "$ mvn verify\nBUILD SUCCESS", observation: "All 4 modules compiled.", refs: [], dispatchStatus: null } as any} />)
  expect(screen.getByText(/raw tool result/i)).toBeInTheDocument()
  expect(screen.getByText(/BUILD SUCCESS/)).toBeInTheDocument()
  expect(screen.getByText(/agent's interpretation/i)).toBeInTheDocument()
  expect(screen.getByText(/All 4 modules compiled/)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — modal header: tool badge (dark mono) + honest status badge (`success`→ok / `dispatchStatus==="pending"`→running / else failed). Body: "TOOL OUTPUT · raw tool result" mono block showing `action.output`; if a ref carries fuller content (`action.refs.find(r => typeof r !== "string" && r.content)`), a "open full output" toggle reveals `ref.content`. Then "OBSERVATION · agent's interpretation" showing `action.observation`. Reference: `_review/ref.png` + `WorkbenchDetail.dc.html` modal block (after line 254).

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit** — `git add webui/src/components/session/ActionDetailModal.tsx webui/src/components/session/ActionDetailModal.test.tsx && git commit -m "feat(webui): ActionDetailModal (output + observation)"`

---

## Task 11: Frontend — FlowTab

**Files:**
- Create: `webui/src/pages/detail/FlowTab.tsx`
- Test: `webui/src/pages/detail/FlowTab.test.tsx`

**Interfaces:**
- Consumes: `detail.context` (ContextTrace), `ActionDetailModal` (Task 10).
- Produces: `FlowTab({ detail })`.

- [ ] **Step 1: Write the failing test** — given a `detail.context` with one phase/task/iteration with a `think` thought and one action, assert: the goal trunk shows `context.trunk.goal`; the phase title + `completed`/`failed` badge render; clicking the action row opens the modal (`getByText(/raw tool result/i)` appears).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — goal trunk card (goal/summary/progress bar from `context.trunk`); phase timeline (dots + line; per phase header with `metaText` from iteration/action counts); tasks→iterations→think rows (italic) + action rows (clickable, `useState` selected action → `<ActionDetailModal>`). Reuse `ContextTrace` internals where they fit (import its row renderers if exported; otherwise render inline per the template). Markup reference: `WorkbenchDetail.dc.html` lines 204–254 (FLOW block).

- [ ] **Step 4: Run** → PASS; `npx tsc -p tsconfig.app.json --noEmit` clean.

- [ ] **Step 5: Commit** — `git add webui/src/pages/detail/FlowTab.tsx webui/src/pages/detail/FlowTab.test.tsx && git commit -m "feat(webui): FlowTab timeline + action modal"`

---

## Task 12: Frontend — DetailPane tab-switcher shell

**Files:**
- Modify: `webui/src/pages/detail/DetailPane.tsx`
- Delete: `webui/src/pages/detail/SummaryBand.tsx` + `SummaryBand.test.tsx`
- Test: `webui/src/pages/detail/DetailPane.test.tsx` (update)

**Interfaces:**
- Consumes: `buildDetailTabs`/`TabBody` (Task 7), `VerdictBand`, restyled `DetailHeader`, `OverviewTab`, `FlowTab`.

- [ ] **Step 1: Update the test** — assert: header + `VerdictBand` render; tab bar shows Overview (active by default) and Flow; clicking the **Build** tab renders the build facet and HIDES the overview content (real switch, not scroll); `initialFacet="flow"` opens the Flow tab. Use `getAllByRole`/`fireEvent.click`.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — replace scroll-spy: `const tabs = buildDetailTabs(detail)`; `const [active, setActive] = useState(initialFacet && tabs.some(t=>t.id===initialFacet) ? initialFacet : "overview")`; reset on `sessionId` change (`useEffect`/`key`). Layout: `flex flex-col h-full`; fixed `DetailHeader`, fixed `VerdictBand`, fixed tab bar (`FacetTabs`/tab buttons calling `setActive`), then `<main class="flex-1 overflow-auto">` rendering `<TabBody tabId={active} detail={detail} onOpenFlow={() => setActive("flow")} ...handlers/>`. Remove `useScrollSpy` + `SummaryBand` imports/usage. Delete `SummaryBand.tsx` + its test. Keep dialogs (NewTask etc.) wiring intact.

- [ ] **Step 4: Run** — `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit` → all PASS, clean. Confirm no remaining import of `SummaryBand` (`grep -rn SummaryBand webui/src` → empty).

- [ ] **Step 5: Commit** — `git add webui/src/pages/detail/DetailPane.tsx webui/src/pages/detail/DetailPane.test.tsx && git rm webui/src/pages/detail/SummaryBand.tsx webui/src/pages/detail/SummaryBand.test.tsx && git commit -m "feat(webui): tabbed DetailPane shell (replaces scroll-spy + SummaryBand)"`

---

## Task 13: Live verification vs the design

**Files:** none (verification only).

- [ ] **Step 1:** Start the demo backend: `uv run sag ui --demo --port 8200 --host 127.0.0.1` (background).
- [ ] **Step 2:** Temp `/api` proxy in `webui/vite.config.ts` → `http://127.0.0.1:8200`; `cd webui && npm run dev -- --port 5188` (background). (NOT `npm run build`.)
- [ ] **Step 3:** Chrome (puppeteer-core, existing `/Applications/Google Chrome.app`) screenshots of the detail view: Overview, Flow (+ open an action modal), Tests, Build tabs.
- [ ] **Step 4:** Compare against `docs/Setup-Agent UI/_review/` (03-flow4, ref, 01-detail_mid for before/after) and the `WorkbenchDetail.dc.html` AFTER block. Fix visual gaps in the relevant component, re-run its vitest, re-screenshot.
- [ ] **Step 5: Cleanup** — kill dev + backend, `git checkout -- webui/vite.config.ts`, remove any temp logs. Do NOT commit `vite.config.ts` or `static/`.
- [ ] **Step 6: Final** — `cd webui && npx vitest run` (all pass), `uv run pytest tests/web tests/agent` (all pass). Report the diff; open the PR (`feat/webui-detail-redesign` → `main`) shipping source only.

---

## Self-review

**Spec coverage:** §1 IA → Tasks 7,12. §2 header/verdict/backend → Tasks 1,2,3,5,6. §3 Overview → Tasks 8,9. §4 Flow/modal/honest-badge → Tasks 10,11. §5 components/files/testing → all tasks + Task 13. Non-goals (no build/static, honest badge, no BEFORE/AFTER) honored in Global Constraints + Task 10/13. ✓ no gaps.

**Placeholder scan:** verdict helper, types, and test code are concrete; component *styling* defers to the cited design-template line ranges (the authoritative visual spec) rather than reproducing 680 lines of inline-styled HTML — markup-per-component is a reference, the React structure/data/tests are spelled out. No "TBD"/"handle edge cases" steps.

**Type consistency:** `compose_verdict` signature + `{tone,headline,detail}` shape matches `VerdictSummary` (Py + TS) across Tasks 2/3/4/5. `buildDetailTabs`/`TabBody`/`TabMeta` consistent Tasks 7/12. `ActionDetailModal({action,onClose})` consistent Tasks 10/11. `variant:"overview"` consistent Tasks 8/9. `stepBudget` alias consistent Tasks 3/4/6. ✓
