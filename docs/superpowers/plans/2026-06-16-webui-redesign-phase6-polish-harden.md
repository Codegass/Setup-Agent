# Phase 6 — Polish & Harden Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Finish the redesign: make the master-detail layout **responsive** (the fixed 320px rail collapses to a drawer below `lg`), do an **a11y/keyboard** pass (landmarks, Escape-to-close, focus, reduced-motion), confirm the **full state vocabulary** is consistent, and capture **before/after** screenshots. The bundle is rebuilt by the maintainer (policy: `static/` stays uncommitted).

**Architecture:** Presentation-layer only. The app shell (`App.tsx`) gains a `railOpen` drawer state: at `lg+` the rail is the persistent side column (today's behavior); below `lg` it's hidden behind a slide-in drawer toggled from a slim mobile top bar, with a backdrop and Escape-to-close. `WorkspaceRail` gains an optional `className` + `onAfterSelect` so the drawer can close on selection. Reduced-motion is already handled globally (Phase 1); the drawer transition must respect it.

**Tech Stack:** React + TypeScript (Vite), Tailwind v4 (Phase 1 tokens + z-scale), Vitest + Testing Library.

**Branch:** `feature/webui-workbench-redesign` (Phases 1–5 merged to main; branch even with main). Continue on it.

**Project policy:** no `npm run build`; never stage `src/sag/web/static/`; force-add docs; exact paths; no `Co-Authored-By` trailer.

**Per-task verification:** `cd webui && npm test -- <path>`; type check `cd webui && npx tsc -p tsconfig.app.json --noEmit`.

---

## File Structure

**Modify:**
- `webui/src/pages/WorkspaceRail.tsx` — accept optional `className` (merged onto the `<aside>`) and `onAfterSelect?` (called after `onSelect`, so the drawer closes on mobile). No visual change at `lg+`.
- `webui/src/pages/WorkspaceRail.test.tsx` — assert `onAfterSelect` fires on row click.
- `webui/src/App.tsx` — responsive shell: `railOpen` state, mobile top bar with a menu toggle, drawer positioning + backdrop, Escape-to-close, `aria` landmarks.
- `webui/src/test/App.test.tsx` — assert the mobile menu toggle exists and toggles `aria-expanded` (jsdom renders both layouts; assert the control + state, not media queries).

**Reference:** `docs/Setup Agent Web UI/workbench/app.jsx` (the `stacked`/`horizontal` rail is the prototype's narrow-width answer; a drawer is the equivalent, more standard for a tool).

---

## Task 1: WorkspaceRail accepts className + onAfterSelect

**Files:** Modify `webui/src/pages/WorkspaceRail.tsx`, `webui/src/pages/WorkspaceRail.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `webui/src/pages/WorkspaceRail.test.tsx` (reuse the file's `props`/`data` fixtures):

```tsx
  it("calls onAfterSelect after selecting a workspace (drawer close hook)", () => {
    const onSelect = vi.fn()
    const onAfterSelect = vi.fn()
    render(<WorkspaceRail {...props} onAfterSelect={onAfterSelect} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole("button", { name: /owner\/broken/ }))
    expect(onSelect).toHaveBeenCalledWith("sag-broken")
    expect(onAfterSelect).toHaveBeenCalled()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx` — FAIL (`onAfterSelect` not a prop / not called).

- [ ] **Step 3: Implement**

In `webui/src/pages/WorkspaceRail.tsx`:
- Add `className?: string` and `onAfterSelect?: () => void` to the props type.
- Import `cn` from `@/lib/utils` (if not already imported).
- Merge `className` onto the `<aside>`: `className={cn("flex h-full min-h-0 w-[320px] shrink-0 flex-col border-r border-slate-200 bg-white", className)}`.
- Wrap the row select so it also fires `onAfterSelect`: pass `onSelect={(id) => { onSelect(id); onAfterSelect?.() }}` down to the rows (define a local `handleSelect` and use it for both `RailRow` and any pending-launch row select). Do **not** call `onAfterSelect` for the launch/remove actions — only for workspace selection.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx` — PASS (all existing rail tests still green; `className`/`onAfterSelect` are optional).

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/WorkspaceRail.tsx webui/src/pages/WorkspaceRail.test.tsx
git commit -m "feat(webui): rail accepts className + onAfterSelect (for drawer)"
```

---

## Task 2: Responsive drawer + mobile top bar in App

**Files:** Modify `webui/src/App.tsx`, `webui/src/test/App.test.tsx`

- [ ] **Step 1: Update the App test (failing first)**

In `webui/src/test/App.test.tsx`, add a test (after the dashboard-loads test) asserting the mobile rail toggle:

```ts
  it("exposes a workspace-rail toggle that opens and closes the drawer", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      Promise.resolve(
        String(input) === "/api/project-launches" ? jsonResponse(emptyLaunchQueue) : jsonResponse(dashboard),
      ),
    )
    render(<App />)
    const toggle = await screen.findByRole("button", { name: /workspaces menu/i })
    expect(toggle).toHaveAttribute("aria-expanded", "false")
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute("aria-expanded", "true")
  })
```

> Adapt `dashboard`/`emptyLaunchQueue`/`jsonResponse` to the file's existing scaffolding.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/test/App.test.tsx` — FAIL (no such toggle).

- [ ] **Step 3: Implement the responsive shell**

In `webui/src/App.tsx`:

3a. Add drawer state + Escape handler near the other `useState`s:

```ts
  const [railOpen, setRailOpen] = useState(false)
```

```ts
  useEffect(() => {
    if (!railOpen) {
      return
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setRailOpen(false)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [railOpen])
```

3b. Replace the rail render block so the rail is a responsive column/drawer. Replace the `{loading && !dashboard ? (<RailSkeleton/>) : dashboard ? (<WorkspaceRail .../>) : null}` block with:

```tsx
      {/* Mobile backdrop when the drawer is open */}
      {railOpen ? (
        <button
          aria-label="Close workspaces menu"
          className="fixed inset-0 z-[var(--z-overlay)] bg-slate-900/30 lg:hidden"
          onClick={() => setRailOpen(false)}
          type="button"
        />
      ) : null}

      {loading && !dashboard ? (
        <RailSkeleton className="hidden lg:flex" />
      ) : dashboard ? (
        <WorkspaceRail
          className={cn(
            // Persistent column at lg+, slide-in drawer below lg.
            "fixed inset-y-0 left-0 z-[var(--z-modal)] transition-transform lg:static lg:z-auto lg:translate-x-0",
            railOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0",
          )}
          data={dashboard}
          highlightedWorkspaces={highlightedWorkspaces}
          lastUpdatedAt={lastUpdatedAt}
          launchQueue={launchQueue}
          onAfterSelect={() => setRailOpen(false)}
          onLaunchSetups={() => setLaunchDialogOpen(true)}
          onRemoveLaunch={deleteWorkspace}
          onSelect={selectWorkspace}
          pollFailed={Boolean(dashboardError)}
          selectedId={selectedWorkspaceId}
        />
      ) : null}
```

(Import `cn` from `@/lib/utils` at the top.)

3c. Add a slim mobile top bar inside `<main>`, before the conditional content, so there's a way to open the drawer below `lg`:

```tsx
      <main className="flex min-h-0 flex-1 flex-col overflow-hidden bg-white">
        <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-2 lg:hidden">
          <button
            aria-controls="workspace-rail"
            aria-expanded={railOpen}
            aria-label="Workspaces menu"
            className="rounded-md border border-slate-200 p-1.5 text-slate-600 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
            onClick={() => setRailOpen((v) => !v)}
            type="button"
          >
            <Menu size={16} />
          </button>
          <span className="truncate font-mono text-[12px] font-semibold text-slate-700">
            {selectedWorkspace ? selectedWorkspace.project : "SAG Workbench"}
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-hidden">
          {/* …existing conditional content (error / DetailPane / states) moves here, unchanged… */}
        </div>
      </main>
```

Wrap the existing `<main>` body (the error/DetailPane/loading/empty conditional) inside the new `<div className="min-h-0 flex-1 overflow-hidden">`. Change the `<main>` to `flex flex-col` as shown. Import `Menu` from `lucide-react`.

3d. Give the rail an `id="workspace-rail"` for the `aria-controls`. Add `id` to the `<aside>` in `WorkspaceRail.tsx` (a static `id="workspace-rail"` is fine — single instance) OR pass it through; simplest is a literal on the aside.

- [ ] **Step 4: Run tests + tsc**

Run: `cd webui && npm test -- src/test/App.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean. (jsdom has no media queries, so the rail + the mobile bar both render; the test asserts the toggle's `aria-expanded` flips.)

- [ ] **Step 5: Commit**

```bash
git add webui/src/App.tsx webui/src/test/App.test.tsx webui/src/pages/WorkspaceRail.tsx
git commit -m "feat(webui): responsive rail drawer + mobile top bar"
```

---

## Task 3: a11y landmarks + reduced-motion polish

**Files:** Modify `webui/src/App.tsx` (and `DetailPane.tsx` if needed)

- [ ] **Step 1: Landmark roles**

In `App.tsx`, the rail is the complementary/nav region and `<main>` is the main region. Ensure: the `<main>` element is the only `main` landmark (it is). The rail `<aside>` is a complementary landmark (it is, via `<aside>`). Add `aria-label="Workspaces"` to the `<aside>` in `WorkspaceRail.tsx` so screen readers name it. The detail facet nav already has `aria-label="Detail sections"`.

- [ ] **Step 2: Reduced-motion check**

Confirm the Phase 1 global `@media (prefers-reduced-motion: reduce)` block neutralizes the drawer's `transition-transform` (it should, as it zeroes transition/animation durations). If the rule is scoped (only `animate-*`), add `transition` to its reset so the drawer snaps instead of sliding under reduced motion. Check `webui/src/styles.css` for the reduced-motion block and widen it to cover `transition` if needed:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

(If the existing block already includes `transition-duration`, leave it.)

- [ ] **Step 3: Run suite + tsc**

Run: `cd webui && npm test && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean.

- [ ] **Step 4: Commit**

```bash
git add webui/src/pages/WorkspaceRail.tsx webui/src/styles.css
git commit -m "a11y(webui): label rail landmark; ensure reduced-motion covers transitions"
```

> If `styles.css` already covers transitions and the aside is already labelled, this task is a no-op — skip the commit and note it.

---

## Task 4: Final verification + before/after + bundle

- [ ] **Step 1: Full suite** — `cd webui && npm test` — all green.
- [ ] **Step 2: Type-check** — `cd webui && npx tsc -p tsconfig.app.json --noEmit` — clean.
- [ ] **Step 3: No `static/` staged** — `git -C .. diff --cached --name-only -- src/sag/web/static` — empty.
- [ ] **Step 4: Live visual at two widths** (dev server proxied to demo, Chrome):
  - **Desktop (1440px):** rail + detail side-by-side, no mobile bar.
  - **Narrow (430px):** rail hidden, mobile top bar with the menu button; clicking it slides the drawer in over a backdrop; selecting a workspace closes it; Escape closes it; detail pane is full-width and scrolls.
  Capture before/after screenshots of the dashboard→workbench transformation for the redesign record.
- [ ] **Step 5: Bundle (maintainer)** — `cd webui && npm run build` then `sag ui` to confirm the production bundle; `static/` stays uncommitted (served from the `ui` branch).

---

## Self-Review

**Spec coverage (Phase 6 = polish & harden):**
- Responsive narrow-width rail collapse → Tasks 1–2 (drawer + mobile bar).
- a11y/keyboard (landmarks, Escape-to-close, focus-visible on the toggle, aria-expanded/controls) → Tasks 2–3.
- Full state vocabulary — already consistent across phases (skeleton/empty/error/partial); no new work, confirmed in Step 4.
- Before/after screenshots + rebuilt bundle → Task 4.

**Risk notes:**
- jsdom can't test media queries, so Task 2's test asserts the toggle + `aria-expanded` state, not the visual collapse; the real responsive behavior is verified live (Task 4).
- The drawer uses `z-overlay` (backdrop) < `z-modal` (rail) so dialogs (`z-modal`/`z-tooltip`) still stack above; confirm the launch/new-task/delete dialogs still appear above the drawer.
- `onAfterSelect` fires only on workspace selection, not launch/remove — so opening the launch dialog from the rail on mobile doesn't immediately close the drawer underneath (the dialog is modal anyway).
