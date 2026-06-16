# Webui Redesign — Phase 1 (Foundations) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the small remaining design-foundation work the rest of the SAG Workbench redesign builds on — semantic state tokens, a semantic z-index scale, the final contrast cleanup, the fixed brand accent, and the label type-floor.

**Architecture:** Presentation-layer only. Define OKLCH design tokens in `webui/src/styles.css`, refactor the shared `Badge` primitive to consume them, replace scattered `z-` literals with a semantic scale, and sweep the remaining meaning-bearing `text-slate-400`. Reuse existing primitives; no new components, no data changes.

**Tech Stack:** React + TypeScript + Tailwind v4 (CSS-first `@theme`), vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-06-16-webui-workbench-redesign-design.md` (Foundations section).

**Branch:** `feature/webui-workbench-redesign` (exists, spec committed).

**Already done (do NOT redo — verified in the current tree):** Inter is loaded (`@fontsource-variable/inter` in `package.json` + imported in `src/main.tsx`); a `prefers-reduced-motion` block exists in `styles.css`; OKLCH theme tokens exist; the bulk of the contrast pass is done (`text-slate-400` is already down to ~24 sites).

**Conventions:**
- All work under `webui/`. Tests: `cd webui && npx vitest run`. Typecheck: `cd webui && npx tsc -p tsconfig.app.json --noEmit`.
- Stage only each task's files by exact path. NEVER `git add -A`. **Do NOT commit `src/sag/web/static/`** (rebuilt bundle, served from the `ui` branch) — leave it unstaged.
- No `Co-Authored-By` trailer. One commit per task with the message shown.
- `webui/src/pages/Dashboard.tsx` protection is lifted for the redesign (not touched in this phase anyway).

---

## File Structure

- Modify `webui/src/styles.css` — add semantic **state tokens** (5 tone triplets) and a **z-index scale** to `:root`, map them in `@theme inline` so Tailwind generates utilities, and set the fixed **accent**.
- Modify `webui/src/components/common/Badge.tsx` — `toneClasses`/`dotClasses` consume the token-backed utilities.
- Modify `webui/src/components/common/components.test.tsx` — update any assertions that pin the old literal classes.
- Modify the z-index literal sites: `webui/src/App.tsx`, `webui/src/components/ui/dialog.tsx`, `webui/src/components/launch/LaunchSetupsDialog.tsx` (the ContextTrace `z-10` dots are local stacking within a `relative` container — leave them).
- Modify the meaning-bearing `text-slate-400` sites (~24) across `webui/src/**` — Task 3 lists the rule and the sweep.
- Modify `webui/src/components/common/Badge.tsx` (`MetaLabel`/`LabeledStatus` label) + other shared label sites for the type-floor (Task 4).

---

## Task 1: Semantic state tokens + Badge consumes them

**Files:**
- Modify: `webui/src/styles.css`
- Modify: `webui/src/components/common/Badge.tsx`
- Test/Modify: `webui/src/components/common/components.test.tsx`

The 5 visual tones (`neutral/blue/green/red/amber`) become the single semantic vocabulary. Define a text / soft-bg / border triplet per tone as OKLCH and expose them as Tailwind utilities, so `Badge`, the dashboard, the section-nav, and callouts all draw from one source.

- [ ] **Step 1: Add the tokens to `styles.css`.** In `:root` (after `--radius`), add:

```css
  /* Semantic state palette (one vocabulary for badges, nav, callouts).
     status.ts maps statuses -> these tones. */
  --status-idle: oklch(0.45 0.03 264);        /* neutral text */
  --status-idle-soft: oklch(0.968 0.005 247); /* neutral bg */
  --status-idle-border: oklch(0.9 0.012 247);
  --status-running: oklch(0.5 0.18 258);
  --status-running-soft: oklch(0.97 0.03 255);
  --status-running-border: oklch(0.9 0.06 255);
  --status-success: oklch(0.52 0.13 158);
  --status-success-soft: oklch(0.97 0.04 158);
  --status-success-border: oklch(0.9 0.07 158);
  --status-failed: oklch(0.55 0.2 27);
  --status-failed-soft: oklch(0.97 0.03 27);
  --status-failed-border: oklch(0.9 0.06 27);
  --status-attention: oklch(0.62 0.13 75);
  --status-attention-soft: oklch(0.97 0.05 85);
  --status-attention-border: oklch(0.9 0.08 85);
```

In the `@theme inline` block (after the existing `--color-*` mappings), expose them so Tailwind emits `text-status-*`, `bg-status-*-soft`, `border-status-*-border` utilities:

```css
  --color-status-idle: var(--status-idle);
  --color-status-idle-soft: var(--status-idle-soft);
  --color-status-idle-border: var(--status-idle-border);
  --color-status-running: var(--status-running);
  --color-status-running-soft: var(--status-running-soft);
  --color-status-running-border: var(--status-running-border);
  --color-status-success: var(--status-success);
  --color-status-success-soft: var(--status-success-soft);
  --color-status-success-border: var(--status-success-border);
  --color-status-failed: var(--status-failed);
  --color-status-failed-soft: var(--status-failed-soft);
  --color-status-failed-border: var(--status-failed-border);
  --color-status-attention: var(--status-attention);
  --color-status-attention-soft: var(--status-attention-soft);
  --color-status-attention-border: var(--status-attention-border);
```

- [ ] **Step 2: Refactor `Badge.tsx` to consume the tokens.** Replace `toneClasses` and `dotClasses`:

```tsx
const toneClasses: Record<Tone, string> = {
  neutral: "border-status-idle-border bg-status-idle-soft text-status-idle",
  blue: "border-status-running-border bg-status-running-soft text-status-running",
  green: "border-status-success-border bg-status-success-soft text-status-success",
  red: "border-status-failed-border bg-status-failed-soft text-status-failed",
  amber: "border-status-attention-border bg-status-attention-soft text-status-attention",
}

const dotClasses: Record<Tone, string> = {
  neutral: "bg-status-idle",
  blue: "bg-status-running",
  green: "bg-status-success",
  red: "bg-status-failed",
  amber: "bg-status-attention",
}
```

- [ ] **Step 3: Update the Badge test.** In `components.test.tsx`, find any assertion pinning the old literals (e.g. `toHaveClass("text-emerald-700")` or `bg-blue-50`) and update to the token utilities (`text-status-success`, `bg-status-running-soft`, etc.). If the tests only assert label text / `tone` behavior (not raw classes), no change is needed — run them to see.

Run: `cd webui && npx vitest run src/components/common/components.test.tsx`
Expected: PASS (after updating any literal-class assertions).

- [ ] **Step 4: Add a token assertion.** Add a test that a `green`-tone badge carries the success token class:

```tsx
it("renders status tones from the semantic token utilities", () => {
  render(<Badge tone="green">ok</Badge>)
  expect(screen.getByText("ok")).toHaveClass("text-status-success")
})
```

(Place it alongside the existing Badge tests; import `Badge` as the file already does.)

- [ ] **Step 5: Verify.** `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit` — all pass, clean.

- [ ] **Step 6: Commit**

```bash
git add webui/src/styles.css webui/src/components/common/Badge.tsx webui/src/components/common/components.test.tsx
git commit -m "Add semantic state tokens; Badge consumes them"
```

---

## Task 2: Semantic z-index scale

**Files:**
- Modify: `webui/src/styles.css`
- Modify: `webui/src/App.tsx` (line ~212), `webui/src/components/ui/dialog.tsx` (lines ~24, ~41), `webui/src/components/launch/LaunchSetupsDialog.tsx` (line ~242)

Formalize the layering (the new sticky section-nav + overlays in later phases will reuse it). The `ContextTrace.tsx` `z-10` dots are local stacking inside a `relative` container — leave them.

- [ ] **Step 1: Add the scale to `styles.css`** `:root`:

```css
  /* Semantic z-index scale (low -> high). */
  --z-sticky: 30;     /* app bar, sticky section-nav */
  --z-dropdown: 40;
  --z-overlay: 50;    /* modal backdrop */
  --z-modal: 60;
  --z-tooltip: 70;
```

- [ ] **Step 2: Replace the literals with arbitrary z utilities backed by the vars.**
  - `App.tsx:212` header: `z-30` → `z-[var(--z-sticky)]`.
  - `dialog.tsx:24` overlay: `z-50` → `z-[var(--z-overlay)]`; `dialog.tsx:41` content wrapper: `z-50` → `z-[var(--z-modal)]`.
  - `LaunchSetupsDialog.tsx:242` tooltip: `z-50` → `z-[var(--z-tooltip)]`.

- [ ] **Step 3: Verify.** `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit` — all pass, clean (no test asserts these z literals; if one does, update it).

- [ ] **Step 4: Commit**

```bash
git add webui/src/styles.css webui/src/App.tsx webui/src/components/ui/dialog.tsx webui/src/components/launch/LaunchSetupsDialog.tsx
git commit -m "Add semantic z-index scale; replace z- literals"
```

---

## Task 3: Final contrast cleanup (meaning-bearing text-slate-400)

**Files:**
- Modify: the ~24 `text-slate-400` sites under `webui/src/**` (enumerate with the command below)

Rule: `text-slate-400` (~3:1 on white) fails AA for meaning-bearing small text. **Meaning-bearing text → `text-slate-500`** (and `text-slate-600` for ≤11px primary labels). **Keep `text-slate-400` only for decoration** — icons paired with a readable label, separators/dividers, and the dim "—"/empty placeholders.

- [ ] **Step 1: Enumerate the sites.**

Run: `cd webui && grep -rn "text-slate-400" src --include="*.tsx"`
Expected: ~24 lines.

- [ ] **Step 2: Classify and fix.** For each hit, decide:
  - **Meaning-bearing** (mono uppercase labels like `MetaLabel`/`LabeledStatus`, table-header text, provenance/meta lines, "updated …", counts the reader must read) → change to `text-slate-500`.
  - **Decoration** (an `<Icon … className="text-slate-400">` sitting next to a readable text label; a `·`/`/` separator span; a faint `—` placeholder where adjacent context conveys meaning) → leave as `text-slate-400`.

Apply the edits. Example: in `Badge.tsx` `MetaLabel`/`LabeledStatus`, the `text-slate-400` on the uppercase label is meaning-bearing → `text-slate-500`. In `TestBar` (common), the `·` separator span stays `text-slate-400`.

- [ ] **Step 3: Confirm no meaning-bearing slate-400 remains.** Re-run the grep; for every remaining `text-slate-400`, confirm it is decoration (icon/separator/placeholder). Record the kept ones in the commit body.

- [ ] **Step 4: Verify.** `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit` — all pass, clean. (Contrast is a CSS sweep; correctness is the classification rule + tsc/tests staying green. A later audit phase re-checks with axe.)

- [ ] **Step 5: Commit**

```bash
git add -p webui/src   # stage only the touched .tsx files (review each hunk)
git commit -m "Contrast: meaning-bearing text-slate-400 -> slate-500 (AA)"
```

(If `git add -p` is impractical, stage the specific files the grep reported by exact path. Do not stage `src/sag/web/static`.)

---

## Task 4: Fixed accent + label type-floor

**Files:**
- Modify: `webui/src/styles.css`
- Modify: `webui/src/components/common/Badge.tsx` (`MetaLabel`) + other shared 10px label sites

- [ ] **Step 1: Pin the accent to #2563eb.** In `styles.css` `:root`, set `--primary` and `--ring` to the OKLCH of `#2563eb` (blue-600):

```css
  --primary: oklch(0.546 0.215 263);
  --ring: oklch(0.546 0.215 263);
```

(Replaces the existing `--primary`/`--ring` values; everything already references these vars.)

- [ ] **Step 2: Raise the label type-floor (10px → 11px) for the shared meta label.** In `Badge.tsx` `MetaLabel`, change `text-[10px]` to `text-[11px]` (keep the uppercase+tracking — this is a true label). Do the same for `LabeledStatus`'s inner label span.

```tsx
// MetaLabel
<div className="font-mono text-[11px] uppercase tracking-[0.12em] text-slate-500 ...">
// LabeledStatus inner label
<span className="font-mono text-[11px] uppercase tracking-[0.12em] text-slate-500">
```

- [ ] **Step 3: Reserve uppercase+tracking for labels/badges only — spot fix.** Run `cd webui && grep -rn "uppercase tracking-" src --include="*.tsx" | wc -l` to size it. Where `uppercase tracking-[…]` is applied to non-label running text (not a kicker/table-header/badge), demote to sentence case. (Most uses are legitimate labels; only fix obvious misuse. Record what changed.)

- [ ] **Step 4: Verify.** `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit` — all pass, clean.

- [ ] **Step 5: Commit**

```bash
git add webui/src/styles.css webui/src/components/common/Badge.tsx
git commit -m "Pin #2563eb accent; raise label type-floor to 11px"
```

---

## Task 5: Build + visual verification (operator)

**Files:** none (verification only).

- [ ] **Step 1:** `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit` — full suite green, clean.
- [ ] **Step 2:** `cd webui && npm run build` (emits to `src/sag/web/static/`; leave uncommitted).
- [ ] **Step 3:** `sag ui --demo`; visually confirm badges/status across surfaces still read correctly (tones unchanged), the accent is the intended blue, labels are crisp at 11px, and reduced-motion still behaves. Screenshot the dashboard + a detail view for the before/after record.
- [ ] **Step 4:** Confirm `git status` shows only `src/sag/web/static/` (and pre-existing untracked files) uncommitted; nothing else stray.

---

## Self-review notes

- Spec coverage: semantic state tokens (T1), z-index scale (T2), contrast (T3), accent + type-floor (T4); Inter, reduced-motion, OKLCH already present (noted, skipped). Motion-safety already exists; no task needed.
- Tasks 3 and 4 Step 3 are className sweeps with classification rules rather than pure TDD — that is appropriate for CSS-only changes; correctness is the rule + tsc/tests staying green + the visual pass in Task 5. Tokenization (T1) and the z-scale (T2) are unit/typecheck-verifiable.
- Type consistency: the token names in `styles.css` (`--color-status-*`) match the Tailwind utilities used in `Badge.tsx` (`text-status-success`, `bg-status-running-soft`, `border-status-failed-border`, …).
