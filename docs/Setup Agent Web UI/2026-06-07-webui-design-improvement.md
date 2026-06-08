# SAG Workbench Design Improvement Plan

Driven by the impeccable skill (product register). Strategic ground truth lives
in `docs/PRODUCT.md` (calm mission-control; status earns color; glanceability
first; evidence within two clicks; self-explanatory on first run; WCAG AA
basics). `docs/DESIGN.md` (Phase 0 output) sits beside it; both stay directly
in `docs/` because the impeccable loader only searches the project root,
`.agents/context/`, and `docs/` (flat). All other design artifacts (this plan,
critique snapshots, screenshots) live in `docs/Setup Agent Web UI/`. Every phase below is an impeccable command run against a named
surface, with evidence-based findings already verified in the code and
concrete acceptance criteria.

Standing guardrails for every phase:

- Register rules: restrained color (accent = primary actions, selection, and
  state only), full state vocabulary per interactive component (default,
  hover, focus, active, disabled, loading, error), skeletons over spinners,
  empty states that teach, 150-250ms motion that conveys state only.
- Anti-reference: no generic SaaS dashboard moves (hero metrics, identical
  card grids, gradient accents). The existing summary-card trio is on this
  list.
- All 64 frontend tests stay green; `npx tsc -b` clean; rebuild + commit
  `src/sag/web/static` after each phase that changes the UI.
- Backend payloads are not redesigned here; this is presentation-layer work.

## Verified baseline problems (evidence, not vibes)

| # | Finding | Evidence |
|---|---|---|
| B1 | No `prefers-reduced-motion` support anywhere | `grep prefers-reduced-motion webui/src` returns nothing; animate-ping status pulses, dialog pop, highlight fade, refresh spinner all unconditional |
| B2 | Intended fonts never ported from the prototype | The design prototype (`docs/Setup Agent Web UI/SAG Workbench.html`) loads Inter 400-700 + Space Mono 400/700 from Google Fonts; production declares `font-family: Inter` (`styles.css:89`) but loads nothing, so users silently get system sans and system mono |
| B3 | 87 instances of `text-slate-400` used for meaningful text | ~3.0:1 contrast on white; fails AA 4.5:1 at the 10-12px sizes it is used for (table headers, meta lines, provenance text) |
| B4 | No dashboard empty state | `Dashboard.tsx` has no `workspaces.length === 0` branch; first-run shows a bare table header |
| B5 | Summary-card trio is the hero-metric template | Three identical Cards (number + label + sub) at the top of the dashboard; the exact anti-reference pattern |
| B6 | Loading is text, not skeleton | "Loading workspaces..." inline card on first load; 5s polling has no stale/refreshing affordance beyond the spinner button |
| B7 | Footer copy is wrong | "GET /api/workspaces · manual refresh" while the app polls every 5s |
| B8 | Queue fetch failures are invisible | `loadLaunchQueue` swallows errors; a permanently failing queue endpoint shows stale data with no indicator |
| B9 | 10px uppercase tracked micro-labels are the default voice | Used for the page kicker, all table headers, all field labels, panel headers; below comfortable reading size and an AI-grammar tell when universal |
| B10 | No semantic state tokens | Status hues are scattered Tailwind literals (`border-blue-200 bg-blue-50 text-blue-700` repeated); no single vocabulary to keep state color consistent |

## Phase 0: Capture the visual system

**Command:** `/impeccable document`

The design prototype in `docs/Setup Agent Web UI/` (`SAG Workbench.html`,
`src/*.jsx` reference implementations, `screenshots/` of intended states) is
the design source of truth; production was ported from it incompletely.
DESIGN.md captures both: the tokens production actually uses and the
prototype intent it drifted from (fonts being the proven case).

Generate `docs/DESIGN.md` from the existing code: OKLCH tokens in `styles.css`,
component inventory (`Badge`/`StatusBadge`, `Button`, `Card`/`CardHead`,
`TestBar`, dialog primitives, launch components), spacing and type scale in
use. This is the before-photo every later phase diffs against.

Acceptance: docs/DESIGN.md exists and names the real tokens, not aspirational ones.

## Phase 1: Scored critique with screenshots

**Command:** `/impeccable critique` on the four surfaces: Dashboard,
Workspace, SessionDetail (report + context map + logs tabs), Launch dialog.

Run `sag ui` against real workspaces (sag-dubbo, sag-commons-vfs exist),
screenshot each surface at desktop and narrow widths, score against the
product-register heuristics, and produce the P0/P1/P2 backlog. The baseline
table above seeds it; the critique confirms, prioritizes, and catches what
code reading missed (real rendering, real data widths, real overflow).

Acceptance: a scored critique snapshot the later phases consume as a backlog.

## Phase 2: Foundations (tokens, type, contrast, motion safety)

**Commands:** `/impeccable typeset` + `/impeccable audit` fixes, app-wide.

The highest-leverage, lowest-risk fixes; everything later builds on them.

1. Restore the prototype's type voice (fixes B2): self-host Inter
   (`@fontsource-variable/inter`) and Space Mono (`@fontsource/space-mono`,
   400/700) so the mono voice that carries ids, refs, and labels matches the
   original design; `font-display: swap`; no CDN for a local tool. Tune the scale while
   at it: establish a fixed rem scale with 1.125-1.2 steps; raise the 10px
   floor to 11px; reserve uppercase+tracking for true table headers and
   badges, demote everywhere else to sentence-case labels (B9).
2. Contrast pass (B3): replace meaning-bearing `text-slate-400` with
   `text-slate-500`/`600` per context; keep slate-400 only for true
   decoration (icons next to labels, separators). Verify every replacement
   hits 4.5:1 (small text) or 3:1 (large/bold).
3. Semantic state tokens (B10): define `--status-running`, `--status-queued`,
   `--status-success`, `--status-failed`, `--status-attention` (+ matching
   soft backgrounds) in `styles.css` as OKLCH; refactor `Badge`
   tone classes and `status.ts` to consume them. One vocabulary, used by
   badges, queue panel, highlight, and notices.
4. Motion safety (B1): a global `@media (prefers-reduced-motion: reduce)`
   block: status pulses become static dots, dialog pop becomes instant fade,
   highlight becomes a non-animated tint, spinner keeps rotating only where
   it conveys in-progress state (allowed, but cap to opacity change).
5. Semantic z-index scale (dropdown, sticky header, overlay, modal, tooltip)
   replacing the scattered `z-30`/`z-50` literals.

Acceptance: axe/contrast checks pass on all four surfaces; reduced-motion
emulation shows no movement except progress indication; visual diff shows no
layout regressions.

## Phase 3: Dashboard reshape (the glanceability surface)

**Command:** `/impeccable shape` dashboard, then implement the confirmed brief.

The dashboard's job (PRODUCT.md): answer "is anything wrong?" first. Current
layout spends its best real estate on the metric trio (B5) and treats
attention rows identically to healthy ones.

Shape brief to explore, then commit to:

1. Replace the summary-card trio with a single status strip: one line that
   says "5 workspaces · 2 running · 1 needs attention", where "needs
   attention" is a filter affordance, not a statistic. Frees vertical space,
   kills the hero-metric template.
2. Attention-first ordering: rows needing attention (failed build/test,
   exited container, failed launch) sort to the top with a status-token tint;
   healthy rows stay quiet. Status earns color.
3. Integrate the launch queue panel into the same hierarchy: active batch
   progress inline near the top while running; collapse to a one-line summary
   when idle instead of a permanent panel (fixes the lingering-panel issue).
4. Teaching empty state (B4): zero workspaces renders an explanation of what
   a workspace is plus the primary "Launch setups" action and a hint that the
   dialog accepts pasted repo lists.
5. Skeleton rows on first load (B6); subtle "updated Ns ago" stamp replacing
   the wrong footer copy (B7); a quiet inline indicator when queue or
   dashboard polling is failing (B8).

Acceptance: with one failed and one running workspace, a screenshot answers
"what needs me?" in under a second; empty-state screenshot teaches the first
action; tests updated for reordering and empty state.

## Phase 4: Launch dialog craft pass

**Command:** `/impeccable polish` launch dialog (+ `clarify` for copy).

The dialog works but is utilitarian. Within the existing structure:

1. Full state vocabulary on the grid: per-cell error states (red border +
   message under the row, not only text below), disabled-while-submitting
   styling, focus ring consistency.
2. Row affordances: zebra or hairline separation for >3 rows; the Record
   checkbox gets a visible label slot at narrow widths; remove-row button
   only on hover/focus-within for quiet density.
3. Concurrency field shows the server's valid range (min 1, max from a new
   `max_concurrency` field already computable server-side, or omit max and
   keep server-side message surfacing, which now works).
4. Paste affordance discoverability: a one-line hint under the grid
   ("Paste multiple `repo url [version]` lines into any URL cell"), replacing
   knowledge buried in the dialog description.
5. Submit feedback: button shows progress state; on mixed results, the
   in-dialog summary appears before close so the user sees what happened in
   context (decide in-dialog vs dashboard notice during the pass; currently
   notice-only).

Acceptance: dialog passes the component state checklist (default, hover,
focus, disabled, error, loading per control); paste behavior discoverable
without docs; dialog tests extended.

## Phase 5: Session detail readability (evidence surfaces)

**Command:** `/impeccable layout` + `/impeccable typeset` on SessionDetail
(report, context map, evidence, logs tabs).

Evidence is the product here (principle 3). Focus:

1. Report prose capped at 65-75ch with a real reading hierarchy; tables in
   the report keep density but gain row hairlines.
2. Context map: task list scannability (status token + title line); refs and
   output handles rendered as mono chips with copy affordance; the
   trunk/branch structure visible at a glance.
3. Logs tab: mono block with line numbers, sticky horizontal scroll, and a
   "this log is shared with another setup" caveat removed now that session
   dirs are unique (verify copy).
4. Cross-tab consistency: same header anatomy (title, status, meta) across
   tabs; evidence counts in tab labels.

Constraint: the context-map data plumbing (`context_map.py`,
`session_registry.py`) is recently stabilized; this phase changes
presentation components only (`SessionDetail.tsx`, `ContextMap.tsx` rendering
layer), not data shape.

Acceptance: a failed setup's "why" is findable from the dashboard in two
clicks and readable without zooming; no data-layer changes.

## Phase 6: Motion with intent

**Command:** `/impeccable animate` app-wide (small scope by design).

Calm mission-control earns little motion; what exists must signal state:

1. Status transitions: a workspace row entering "needs attention" gets one
   ease-out tint transition (200ms), not a pulse.
2. Launch flow: queued-to-running in the queue panel animates the badge swap;
   the existing 8s highlight keeps its fade but adopts status tokens.
3. Dialog pop stays as-is (already fixed); list insertions in the launch grid
   get a 150ms fade-in.
4. Everything respects the Phase 2 reduced-motion block by construction.

Acceptance: no animation longer than 250ms; nothing moves without a state
change; reduced-motion shows crossfades only.

## Phase 7: Harden the edges

**Command:** `/impeccable harden` + `/impeccable onboard`.

1. Error states: dashboard-unavailable, queue-unavailable (B8), session
   detail fetch failure, terminal unavailable; consistent inline pattern with
   retry affordances (exists for dashboard; extend the vocabulary).
2. Empty states beyond the dashboard: workspace with no sessions, session
   with no evidence/report yet ("agent is still working" with live status),
   empty queue panel.
3. First-run onboarding moment: the Phase 3 empty state is the entry; verify
   the full first-launch journey (open UI, launch one repo, watch it run)
   has no dead ends.

Acceptance: every fetch in `client.ts` has a designed failure rendering;
every list has a designed empty rendering.

## Phase 8: Final audit and polish

**Commands:** `/impeccable audit` then `/impeccable polish`, all surfaces.

Full regression: contrast re-check, keyboard walk of every flow (launch,
inspect, task submit), responsive pass at 375/768/1180/full, performance
sanity (bundle size delta, render churn from 5s polling), copy pass against
the no-buzzword rules. Close out with screenshots before/after per surface
and the rebuilt committed bundle.

## Sequencing and effort

| Phase | Size | Depends on |
|---|---|---|
| 0 document | S | none |
| 1 critique | M | 0 |
| 2 foundations | M | 1 |
| 3 dashboard reshape | L | 2 |
| 4 launch dialog | M | 2 |
| 5 session detail | M | 2 |
| 6 motion | S | 2 (3-5 ideally done) |
| 7 harden/onboard | M | 3 |
| 8 audit/polish | M | all |

Phases 3, 4, 5 are independent of each other after Phase 2 lands. Each phase
ends with: tests green, tsc clean, bundle rebuilt and committed, screenshots
captured for the before/after record.
