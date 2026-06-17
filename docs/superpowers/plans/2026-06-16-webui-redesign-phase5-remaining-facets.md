# Phase 5 — Remaining Facets Restyle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Restyle the Flow / Evidence / Files / Report / Logs facet renderers to the prototype's calm look by aligning their meaning-bearing colors to the Phase 1 `--status-*` tokens (one status vocabulary across the app), and apply the spec's Report prose-width cap. These renderers already render the right data and structure (built earlier, wired as facets in Phase 3); this phase is the token/polish pass, not a rebuild.

**Architecture:** Presentation-layer only. Mechanical color-token alignment + small polish. No data-contract or structural changes; all behavior and tests stay intact.

**Tech Stack:** React + TypeScript (Vite), Tailwind v4 (Phase 1 tokens), Vitest + Testing Library.

**Branch:** `feature/webui-workbench-redesign` (Phases 1–4 + fixes merged to main; branch even with main). Continue on it.

**Project policy:** no `npm run build`; never stage `src/sag/web/static/`; force-add docs; exact paths only; no `Co-Authored-By` trailer.

**Per-task verification:** `cd webui && npm test -- <path>`; type check `cd webui && npx tsc -p tsconfig.app.json --noEmit`.

---

## Token Mapping (apply consistently)

Replace meaning-bearing literals with the Phase 1 tokens. Keep `slate-*` (structure) and the dark `#0d1117`/`slate-900` log/terminal surfaces as-is.

| Literal family | Text | Solid bg / dot | Soft bg | Border |
|---|---|---|---|---|
| `emerald-*` (success/pass) | `text-status-success` | `bg-status-success` | `bg-status-success-soft` | `border-status-success-border` |
| `red-*` (failure/fail/conflict) | `text-status-failed` | `bg-status-failed` | `bg-status-failed-soft` | `border-status-failed-border` |
| `amber-*` (attention/partial/warn) | `text-status-attention` | `bg-status-attention` | `bg-status-attention-soft` | `border-status-attention-border` |
| `blue-*` (running/active/trunk accent) | `text-status-running` | `bg-status-running` | `bg-status-running-soft` | `border-status-running-border` |

Notes:
- ContextTrace's trunk/brand blue (Target icon, progress bar, "active branch" chip) maps to the **running** token family — one blue vocabulary with the rest of the app.
- Numeric shade → variant: `*-50` → `-soft`; `*-100`/`*-200` → `-border`; `*-500`/`*-600`/`*-700` → base text/solid. Pick the closest-meaning variant (e.g. an alert bg `bg-amber-50` → `bg-status-attention-soft`; its border `border-amber-200` → `border-status-attention-border`; its text `text-amber-700` → `text-status-attention`).
- `bg-X-500` used as a status **dot** → `bg-status-<role>` (solid). The dot-color pattern `c.replace("text-", "bg-")` (EvidenceTimeline) keeps working since both `text-status-*` and `bg-status-*` utilities exist.

---

## File Structure

**Modify:**
- `webui/src/components/session/ContextTrace.tsx` (Flow) — ~16 literals → tokens (incl. trunk blue → running).
- `webui/src/components/session/EvidenceTimeline.tsx` — status dots (emerald/red/amber/blue) → tokens.
- `webui/src/components/session/FilesDigest.tsx` — change-tone badges (added/modified/deleted/renamed) → tokens.
- `webui/src/components/session/ReportDoc.tsx` — status block (emerald/red) → tokens; cap prose width to ~65–75ch.
- `webui/src/components/session/ContextTrace.test.tsx` — only if it asserts a literal class (Step checks).

**Unchanged:** `LogsView.tsx` — already token-free (dark terminal block); leave as-is.

---

## Task 1: ContextTrace (Flow facet)

**Files:** Modify `webui/src/components/session/ContextTrace.tsx`; check `ContextTrace.test.tsx`.

- [ ] **Step 1: Check the test for literal-class assertions**

Run: `cd webui && grep -nE "emerald|red-[0-9]|amber|blue-[0-9]|toContain.*-[0-9]{2,3}" src/components/session/ContextTrace.test.tsx`
If any assert a literal color class, note them for Step 4. (The suite mostly asserts text/behavior, so expect none.)

- [ ] **Step 2: Apply the token mapping**

In `webui/src/components/session/ContextTrace.tsx`, replace every meaning-bearing literal per the mapping table:
- Status text/dots: `emerald-*`→success, `red-*`→failed, `amber-*`→attention.
- Trunk/active **blue**: `text-blue-600`/`text-blue-700`/`text-blue-800` → `text-status-running`; `bg-blue-500` (progress fill) → `bg-status-running`; `bg-blue-50`/`bg-blue-100` → `bg-status-running-soft`; `border-blue-*` → `border-status-running-border`.
- Alert/callout blocks (e.g. an amber/red bordered box): bg `*-50` → `-soft`, border `*-200` → `-border`, text `*-700` → base.

Leave all `slate-*` and the dark debug/pre surfaces untouched. Do not change any text content, conditionals, or component structure — colors only.

- [ ] **Step 3: Verify no meaning-bearing literals remain**

Run: `cd webui && grep -nE "emerald-|red-[0-9]|amber-|blue-[0-9]|green-[0-9]" src/components/session/ContextTrace.tsx`
Expected: no matches (only `slate-*` and `#0d1117`/`slate-900`-style surfaces remain).

- [ ] **Step 4: Run tests + tsc**

Run: `cd webui && npm test -- src/components/session/ContextTrace.test.tsx && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS / clean. If Step 1 found a literal assertion, update it to the token class.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/ContextTrace.tsx webui/src/components/session/ContextTrace.test.tsx
git commit -m "style(webui): token-align ContextTrace (Flow facet) colors"
```

---

## Task 2: EvidenceTimeline + FilesDigest

Both are small; no dedicated tests, so they're covered by the suite + tsc + the live check.

**Files:** Modify `webui/src/components/session/EvidenceTimeline.tsx`, `webui/src/components/session/FilesDigest.tsx`.

- [ ] **Step 1: Token-align EvidenceTimeline**

Replace the status-dot colors (`bg-emerald-500`/`bg-red-500`/`bg-amber-500`/`bg-blue-500` and any `text-*-500/600` status text) per the mapping. Keep the `c.replace("text-", "bg-")` dot pattern (works with `text-status-*`/`bg-status-*`). Leave `slate-*` and structure.

- [ ] **Step 2: Token-align FilesDigest**

The change-tone map (added→`green`, modified→`amber`, deleted→`red`, renamed→`blue`) drives badge tones. If it passes a `Tone` to `Badge` (`green/red/amber/blue`), that already routes through tokens — leave it. Replace any direct `emerald-*/red-*/amber-*/blue-*` literals (e.g. inline dot/text colors) per the mapping.

- [ ] **Step 3: Verify no meaning-bearing literals remain**

Run: `cd webui && grep -nE "emerald-|red-[0-9]|amber-|blue-[0-9]|green-[0-9]" src/components/session/EvidenceTimeline.tsx src/components/session/FilesDigest.tsx`
Expected: no matches (Tone-keyword strings like `"green"` passed to `Badge` are fine and won't match this pattern).

- [ ] **Step 4: Run suite + tsc**

Run: `cd webui && npm test && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/EvidenceTimeline.tsx webui/src/components/session/FilesDigest.tsx
git commit -m "style(webui): token-align Evidence + Files facet colors"
```

---

## Task 3: ReportDoc (token-align + prose width)

**Files:** Modify `webui/src/components/session/ReportDoc.tsx`.

- [ ] **Step 1: Token-align the status block**

In the `type === "status"` branch, replace the ok/not-ok classes:
- ok: `border-emerald-200 bg-emerald-50 text-emerald-700` → `border-status-success-border bg-status-success-soft text-status-success`
- not-ok: `border-red-200 bg-red-50 text-red-700` → `border-status-failed-border bg-status-failed-soft text-status-failed`

- [ ] **Step 2: Cap prose width (spec: 65–75ch)**

The doc body currently wraps blocks in `mx-auto max-w-[640px]`. Change it to `mx-auto max-w-[68ch]` so prose lines cap at a comfortable measure (spec's 65–75ch). Keep the table hairlines (`divide-y divide-slate-100`, `border-slate-200`) as-is.

- [ ] **Step 3: Verify no meaning-bearing literals remain**

Run: `cd webui && grep -nE "emerald-|red-[0-9]|amber-|blue-[0-9]|green-[0-9]" src/components/session/ReportDoc.tsx`
Expected: no matches.

- [ ] **Step 4: Run suite + tsc**

Run: `cd webui && npm test && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/ReportDoc.tsx
git commit -m "style(webui): token-align Report facet, cap prose to 68ch"
```

---

## Task 4: Full verification + live visual check

- [ ] **Step 1: Full suite** — `cd webui && npm test` — all green (132).
- [ ] **Step 2: Type-check** — `cd webui && npx tsc -p tsconfig.app.json --noEmit` — clean.
- [ ] **Step 3: No meaning-bearing literals across the five** — `cd webui && grep -rnE "emerald-|red-[0-9]|amber-|blue-[0-9]|green-[0-9]" src/components/session/ContextTrace.tsx src/components/session/EvidenceTimeline.tsx src/components/session/FilesDigest.tsx src/components/session/ReportDoc.tsx || echo "clean"` → `clean`.
- [ ] **Step 4: No `static/` staged** — `git -C .. diff --cached --name-only -- src/sag/web/static` — empty.
- [ ] **Step 5: Live visual** — drive the detail pane (dev server proxied to demo, Chrome): scroll Flow (trunk goal + phase ladder, running accent in the blue/running token), Evidence (grouped records with token status dots), Files (change-tone badges), Report (capped prose + hairline tables, token status blocks), Logs (dark terminal block unchanged). Confirm one consistent status vocabulary and no jarring raw colors.

---

## Self-Review

**Spec coverage (Phase 5 = Flow/Evidence/Files/Report/Logs restyle):**
- Flow command-center colors → tokens (Task 1).
- Evidence grouped refs + status dots → tokens (Task 2).
- Files digest change tones → tokens (Task 2).
- Report capped prose (65–75ch) + table hairlines + status blocks → tokens (Task 3).
- Logs mono block — already matches the prototype (dark terminal); no change.

**Consistency:** one status vocabulary (`--status-*`) now spans Badge, dashboard, rail, summary band, Build/Test facets (Phase 4), and these five facets.

**Risk notes:**
- Token swap is color-only; behavior/tests unchanged. The only test among the five is `ContextTrace.test.tsx` (asserts text/behavior, not colors) — Step 1.1 confirms.
- The `c.replace("text-", "bg-")` dot pattern in EvidenceTimeline relies on both `text-status-*` and `bg-status-*` utilities existing (they do, from Phase 1).
- This is a visual pass; the grep gates (no literals remain) + the live check are the real acceptance.
