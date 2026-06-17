# Build/Test Facet Summary + Per-Module Detail Modal Design

## Purpose

Make the Build and Test **facet sections** match the prototype (`docs/Setup Agent Web UI/workbench/sections.jsx` `BuildBody`/`TestBody`) — a clean, conclusion-first summary — while **keeping** the per-submodule Features A/B detail (module tables, coverage, build/test breakdown) reachable as a "detail page" in a modal. Today the facets render the full per-module `BuildDetailPage`/`TestDetailPage` inline, which is busier than the prototype (Image #6: Test = a big pass/total conclusion + a FAILING list, nothing else inline).

## Target shape

**Test facet** (`workbench/sections.jsx TestBody`, Image #6):
- Conclusion card: `pass / total`, `runner executions passed`, a `NN% pass` badge, a full-width pass/fail bar, and `passed · failed · skipped · <note>`.
- **FAILING · N** card: the project's failing test names (`detail.test.failingNames`), each with a red ✗, when any exist.
- For multi-module: a **"View per-module breakdown (N modules) →"** button opening the detail modal.

**Build facet** (`BuildBody`):
- The two-card summary already shipped (conclusion KV + Outputs) — kept.
- For multi-module: the same **"View per-module breakdown (N modules) →"** button opening the detail modal.

**Detail modal (kept "detail page")** — opened from either facet's button:
- Build: module stats (Modules/Built/Failed/Skipped/Success rate) + the per-module `ModuleTable` (variant `build`).
- Test: the tiles (Unique methods / Modules w/ fails / Coverage) + the per-module `ModuleTable` (variant `test`, with the coverage column + failing-method expand).
- A modal/overlay (same primitive as the Terminal/Settings panels), closeable back to the facet.

Single-module projects have no per-module breakdown, so no button/modal — the facet summary (conclusion + FAILING for test; conclusion + outputs for build) is complete.

## Architecture

- The conclusion/outputs cards (build) and conclusion card (test) move into **facet summary** components; `BuildDetailPage`/`TestDetailPage` are slimmed to the **per-module content only** (stats + `ModuleTable`) and become the modal body — so the conclusion is shown once (in the facet, behind the modal).
- New `FacetBody` wiring: `build` → `BuildFacet`, `test` → `TestFacet` (each a small stateful component owning the modal open state). The other facets are unchanged.
- No data-contract change: `detail.test.failingNames` is already populated by the read model; `BuildSummary`/`ModuleRollup`/`ModuleSummary` consumed as-is.

## File structure

- New `webui/src/components/session/BuildFacet.tsx` — summary (ConclusionCard + OutputsCard) + breakdown button + modal(BuildDetailPage).
- New `webui/src/components/session/TestFacet.tsx` — summary (ConclusionCard + FailingCard) + breakdown button + modal(TestDetailPage).
- New `webui/src/components/session/FailingCard.tsx` — the FAILING · N list.
- New `webui/src/components/session/ModuleBreakdownDialog.tsx` — the modal shell (reuses the `Dialog` primitive).
- Slim `BuildDetailPage.tsx` / `TestDetailPage.tsx` to the per-module content (drop the conclusion/outputs — now in the facet); keep them exported (they are the modal body = the kept detail page). Shared conclusion/outputs card components extracted to avoid duplication.
- `webui/src/pages/detail/facets.tsx` — `FacetBody` build/test cases render `BuildFacet`/`TestFacet`.
- Tests updated: facet summary + FAILING + breakdown button + modal-open; detail-page (modal body) tests assert per-module content; no fake zeroes; Features A/B preserved.

## Constraints

- Presentation-layer only; Phase 1 `--status-*` tokens; reuse `Card`/`Badge`/`StatusBadge`/`TestBar`/`ModuleTable`/`Dialog`.
- Features A/B invariants preserved (per-module tables, coverage column, failing-method expand, no fake zeroes, coverage tile branch-only/>100% guards).
- `static/` uncommitted; docs force-added; no `Co-Authored-By`; tests green + `tsc` clean.

## Testing

- RTL: Test facet renders conclusion + FAILING (from failingNames) + (multi-module) the breakdown button that opens the modal; Build facet renders two-card + breakdown button; the modal shows the per-module table + coverage; single-module shows no button. `tsc` clean.
- Live: Chrome — Test facet matches Image #6 (conclusion + FAILING); clicking "View per-module breakdown" opens the modal with the module table + coverage; Build likewise.
