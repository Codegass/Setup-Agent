# Plan 1 — Delete Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline) or subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make workspace deletion non-freezing (the row shows an inline "deleting…" state while the `DELETE` runs in the background) and add batch delete to the rail (a select mode with per-row checkboxes + a "Delete N selected" action bar).

**Architecture:** Presentation-layer only. `App.tsx` owns a `deletingIds` set and a fire-and-forget `startDelete(id)`; the confirm dialog closes immediately on confirm; `WorkspaceRail` renders the deleting state and owns the select-mode UI, calling back to delete one or many. The existing single `DELETE` endpoint is reused (looped client-side for batch).

**Tech Stack:** React + TypeScript (Vite), Tailwind v4 (Phase 1 tokens), Vitest + Testing Library, lucide-react.

**Branch:** `feature/webui-workbench-redesign` (even with main). Continue on it.

**Project policy:** no `npm run build`; never stage `src/sag/web/static/`; force-add docs; exact paths; no `Co-Authored-By` trailer.

**Per-task verification:** `cd webui && npm test -- <path>`; `cd webui && npx tsc -p tsconfig.app.json --noEmit`.

---

## File Structure

**Modify:**
- `webui/src/App.tsx` — `deletingIds` state; `startDelete(id)` (optimistic background, error → toast); detail-delete closes selection immediately; pass `deletingIds` + `onDeleteMany` to the rail; keep `deleteWorkspace` (awaited) only for the failed-launch removal path (small/fast).
- `webui/src/pages/WorkspaceRail.tsx` — accept `deletingIds`, `onDeleteMany`; render the inline "deleting…" row state; add select-mode (checkboxes + action bar + batch confirm).
- `webui/src/pages/WorkspaceRail.test.tsx` — deleting-state + select-mode + batch-delete tests.
- `webui/src/components/workspace/DeleteWorkspaceDialog.tsx` — optional `count` prop for batch copy.
- `webui/src/components/workspace/DeleteWorkspaceDialog.test.tsx` — batch-copy test (if the file exists; else add a minimal one).
- `webui/src/pages/detail/DetailPane.tsx` — its delete dialog `onConfirm` no longer awaits a slow call (App's handler returns immediately); no structural change needed beyond passing the faster handler.

**Reuse:** `DeleteWorkspaceDialog`, `Card`, `Button`, `Badge`, Phase 1 tokens.

---

## Task 1: Non-freezing delete in App (`deletingIds` + `startDelete`)

**Files:** `webui/src/App.tsx`, `webui/src/test/App.test.tsx`

- [ ] **Step 1: Add the failing App test**

Append to `webui/src/test/App.test.tsx` a test that delete is non-blocking (the workspace shows a deleting state and the request fires). Since the detail-header delete is the existing path, drive it: open a workspace, click the header Delete, confirm, and assert the `DELETE` fetch was called and the dialog closed without awaiting a slow promise. (Reuse the file's fetch-spy scaffolding; mock `DELETE` with a never-resolving promise to prove the UI doesn't block.)

```ts
  it("does not block the UI while a delete is in flight", async () => {
    let deleteCalls = 0
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input)
      if (url === "/api/project-launches") return Promise.resolve(jsonResponse(emptyLaunchQueue))
      if (url.startsWith("/api/sessions/")) return Promise.resolve(jsonResponse(sessionDetail))
      if (init?.method === "DELETE") {
        deleteCalls++
        return new Promise(() => {}) // never resolves — proves the UI doesn't await it
      }
      return Promise.resolve(jsonResponse(dashboard))
    })

    render(<App />)
    // open the detail header delete
    fireEvent.click(await screen.findByRole("button", { name: /^Delete$/i }))
    fireEvent.click(await screen.findByRole("button", { name: /delete workspace/i }))
    // dialog closed (no lingering dialog) and the request fired, despite never resolving
    await waitFor(() => expect(deleteCalls).toBe(1))
    expect(screen.queryByRole("dialog", { name: /delete workspace/i })).not.toBeInTheDocument()
  })
```

> Adapt selectors to the DetailHeader's delete control (`aria-label="Delete"`) and the dialog's confirm button (`"Delete workspace"`). If the header delete isn't reachable in jsdom because no workspace is selected, rely on auto-select (the dashboard fixture has one workspace).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/test/App.test.tsx` — FAIL (today the dialog awaits the DELETE, so it never closes when the request never resolves).

- [ ] **Step 3: Implement non-freezing delete**

In `webui/src/App.tsx`:

3a. Add state:

```ts
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set())
```

3b. Add `startDelete` (fire-and-forget) and keep `deleteWorkspace` (awaited) for the fast failed-launch path:

```ts
  const startDelete = (workspaceId: string) => {
    setDeletingIds((prev) => new Set(prev).add(workspaceId))
    void deleteWorkspaceRequest(workspaceId)
      .then(() => {
        void loadDashboard({ silent: true })
        void loadLaunchQueue()
      })
      .catch((err) => {
        setLaunchNotice(
          `Couldn't delete ${workspaceId}: ${err instanceof Error ? err.message : String(err)}`,
        )
      })
      .finally(() => {
        setDeletingIds((prev) => {
          const next = new Set(prev)
          next.delete(workspaceId)
          return next
        })
      })
  }
```

3c. Replace `deleteWorkspaceFromDetail` so it starts the background delete and clears the selection immediately (no await):

```ts
  const deleteWorkspaceFromDetail = (workspaceId: string): Promise<void> => {
    startDelete(workspaceId)
    setSelectedWorkspaceId(null)
    setSelectedSessionIdState(undefined)
    return Promise.resolve()
  }
```

3d. Add a batch handler:

```ts
  const deleteWorkspaces = (ids: string[]): Promise<void> => {
    ids.forEach(startDelete)
    return Promise.resolve()
  }
```

3e. Pass the new props to `WorkspaceRail`: `deletingIds={deletingIds}` and `onDeleteMany={deleteWorkspaces}`. (The rail's existing `onRemoveLaunch={deleteWorkspace}` stays — failed-launch removal is fast and can keep awaiting.)

3f. `DetailPane`'s `onDelete` already points at `deleteWorkspaceFromDetail`; with 3c it returns immediately, so the dialog (which awaits `onConfirm` then closes) closes at once.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/test/App.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add webui/src/App.tsx webui/src/test/App.test.tsx
git commit -m "feat(webui): non-freezing delete (optimistic background) with error toast"
```

---

## Task 2: Inline "deleting…" row state in the rail

**Files:** `webui/src/pages/WorkspaceRail.tsx`, `webui/src/pages/WorkspaceRail.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `webui/src/pages/WorkspaceRail.test.tsx`:

```tsx
  it("renders a workspace in a non-interactive deleting state", () => {
    const onSelect = vi.fn()
    render(<WorkspaceRail {...props} deletingIds={new Set(["sag-broken"])} onSelect={onSelect} />)
    const row = screen.getByRole("button", { name: /owner\/broken/ })
    expect(row).toBeDisabled()
    expect(screen.getByText(/deleting/i)).toBeInTheDocument()
    fireEvent.click(row)
    expect(onSelect).not.toHaveBeenCalled()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx` — FAIL (`deletingIds` not a prop; no deleting state).

- [ ] **Step 3: Implement**

In `webui/src/pages/WorkspaceRail.tsx`:
- Add `deletingIds?: Set<string>` and `onDeleteMany?: (ids: string[]) => Promise<void>` to the props type; default `deletingIds = new Set()`.
- Pass `deleting={deletingIds.has(w.id)}` to each `RailRow`.
- In `RailRow`, add `deleting?: boolean`. When `deleting`:
  - The `<button>` gets `disabled` and `aria-disabled`, `cursor-default opacity-60`, and `onClick` is a no-op.
  - Replace the build/test trailing cluster with `<span className="font-mono text-[10px] text-slate-500">deleting…</span>` (or a small `Loader2` spinner + "deleting…").

```tsx
function RailRow({ workspace, selected, highlighted, deleting = false, onSelect }: {
  workspace: WorkspaceSummary; selected: boolean; highlighted: boolean; deleting?: boolean; onSelect: (id: string) => void
}) {
  // …existing derivations…
  return (
    <button
      aria-current={selected}
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "group flex w-full items-center gap-3 border-b border-slate-100 px-3.5 py-2.5 text-left transition-colors last:border-b-0",
        deleting ? "cursor-default opacity-60" :
          selected ? "bg-status-running-soft" : attention ? "bg-status-failed-soft/40 hover:bg-status-failed-soft/60" : "hover:bg-slate-50/80",
        highlighted && !selected && !deleting ? "bg-blue-50/60" : "",
      )}
      disabled={deleting}
      onClick={() => onSelect(workspace.id)}
      type="button"
    >
      {/* …dot + name/meta… */}
      <span className="flex shrink-0 items-center gap-2">
        {deleting ? (
          <span className="inline-flex items-center gap-1 font-mono text-[10px] text-slate-500">
            <Loader2 className="animate-spin" size={11} /> deleting…
          </span>
        ) : (
          {/* …existing build glyph + TestBar… */}
        )}
      </span>
    </button>
  )
}
```

(Import `Loader2` from `lucide-react`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/WorkspaceRail.tsx webui/src/pages/WorkspaceRail.test.tsx
git commit -m "feat(webui): inline deleting state on rail rows"
```

---

## Task 3: Batch delete (select mode + action bar) + dialog count

**Files:** `webui/src/pages/WorkspaceRail.tsx`, `webui/src/pages/WorkspaceRail.test.tsx`, `webui/src/components/workspace/DeleteWorkspaceDialog.tsx`

- [ ] **Step 1: Add the `count` prop to DeleteWorkspaceDialog**

In `webui/src/components/workspace/DeleteWorkspaceDialog.tsx`, add an optional `count?: number` (default 1). When `count > 1`:
- title/confirm label: `Delete ${count} workspaces`.
- body: `This removes ${count} workspaces and cannot be undone.`
- omit the single `DELETE /api/workspaces/{id}` description line.

Keep `onConfirm(workspaceId)` unchanged (the batch caller ignores the id).

- [ ] **Step 2: Write the failing rail batch test**

Append to `webui/src/pages/WorkspaceRail.test.tsx`:

```tsx
  it("batch-deletes selected workspaces via select mode", async () => {
    const onDeleteMany = vi.fn().mockResolvedValue(undefined)
    render(<WorkspaceRail {...props} onDeleteMany={onDeleteMany} />)
    // enter select mode
    fireEvent.click(screen.getByRole("button", { name: /^select$/i }))
    // check both workspaces
    fireEvent.click(screen.getByRole("checkbox", { name: /owner\/healthy/i }))
    fireEvent.click(screen.getByRole("checkbox", { name: /owner\/broken/i }))
    // action bar → confirm
    fireEvent.click(screen.getByRole("button", { name: /delete 2 selected/i }))
    fireEvent.click(screen.getByRole("button", { name: /delete 2 workspaces/i }))
    await waitFor(() => expect(onDeleteMany).toHaveBeenCalled())
    expect(onDeleteMany.mock.calls[0][0]).toEqual(expect.arrayContaining(["sag-healthy", "sag-broken"]))
  })
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx` — FAIL (no select mode).

- [ ] **Step 4: Implement select mode**

In `webui/src/pages/WorkspaceRail.tsx`:
- State: `const [selectMode, setSelectMode] = useState(false)`, `const [picked, setPicked] = useState<Set<string>>(new Set())`, `const [batchConfirm, setBatchConfirm] = useState(false)`.
- Rail header: a **Select** toggle button (only when `onDeleteMany` provided and there are workspaces). Toggling off clears `picked`.
- `RailRow` gains `selectMode?: boolean`, `checked?: boolean`, `onToggleCheck?: (id) => void`. In select mode the row renders a leading `<input type="checkbox" aria-label={`Select ${workspace.project}`} checked onChange>` and clicking the row toggles the checkbox instead of selecting-to-open (or keep the row button but route its onClick to toggle). Simplest: in select mode, render the row as a label wrapping the checkbox; the checkbox's accessible name is the project (`aria-label="Select owner/x"` — RTL `getByRole("checkbox", { name: /owner\/x/ })`). Skip pending rows.
- Action bar (sticky, above the footer) shown when `selectMode`: `Delete {picked.size} selected` (disabled when 0) + `Cancel`. "Delete N selected" → `setBatchConfirm(true)`.
- Batch confirm: render `<DeleteWorkspaceDialog count={picked.size} target={{ workspaceId: "", label: `${picked.size} workspaces`, kind: "workspace" }} onCancel={() => setBatchConfirm(false)} onConfirm={async () => { await onDeleteMany?.([...picked]); setBatchConfirm(false); setSelectMode(false); setPicked(new Set()) }} />`.

- [ ] **Step 5: Run tests + tsc**

Run: `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add webui/src/pages/WorkspaceRail.tsx webui/src/pages/WorkspaceRail.test.tsx webui/src/components/workspace/DeleteWorkspaceDialog.tsx
git commit -m "feat(webui): batch delete in the rail (select mode + action bar)"
```

---

## Task 4: Full verification + live check

- [ ] **Step 1: Full suite** — `cd webui && npm test` — all green.
- [ ] **Step 2: Type-check** — `cd webui && npx tsc -p tsconfig.app.json --noEmit` — clean.
- [ ] **Step 3: No `static/` staged** — `git -C .. diff --cached --name-only -- src/sag/web/static` — empty.
- [ ] **Step 4: Live** — drive Chrome (dev server proxied to demo): delete a workspace from the detail header → the dialog closes instantly and its rail row shows "deleting…" (UI stays interactive); enter rail Select mode, check two, "Delete 2 selected" → confirm → both show "deleting…" and select mode exits. (Against the demo backend the DELETE may 404/no-op; the point is the non-blocking UX. On a real backend the rows disappear when the reload lands.)

---

## Self-Review

**Spec coverage (Subsystem A):** A1 non-freezing inline-deleting → Tasks 1–2; A2 batch select + action bar → Task 3.

**Risk notes:**
- Detail-header delete returns immediately + clears selection; auto-select then picks the next workspace while the deleted one shows "deleting…" in the rail until the silent reload drops it.
- Error path routes to the toast (`launchNotice`), since the dialog has already closed.
- Batch loops the single `DELETE`; each id enters `deletingIds` independently.
- jsdom can't test the DELETE actually removing the row (that needs a backend reload) — the App test asserts non-blocking via a never-resolving DELETE; the rail tests assert the deleting state + batch callback.
