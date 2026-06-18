# SAG Workbench — how to build with these components

These are the real components from the **Setup-Agent (SAG) Workbench** — the web UI that
shows the result of an automated project-setup agent (build status, test results, per-module
coverage, evidence, logs, and the agent's execution trace). Compose them for dashboards,
detail panes, and status/report surfaces.

## Setup — no provider needed

Components are **prop-driven plain React** — there is **no theme/context provider to wrap**.
Import a component and render it with props; it is styled as soon as the bundle's `styles.css`
is loaded (it always is here). The brand font is **Inter Variable** (shipped in `fonts/`).
Pass data as props — these components never fetch; the host app supplies the data objects
(see each component's `<Name>.d.ts`). Callbacks (`onSelect`, `onOpenDetail`, …) are plain
functions. Dialog/modal components (`LaunchSetupsDialog`, `NewTaskModal`,
`DeleteWorkspaceDialog`, `ModuleBreakdownDialog`) render their open state and portal to
`document.body`.

## Styling idiom — Tailwind v4 + a semantic status family

Style with **Tailwind utility classes**. On top of standard Tailwind (the `slate` palette for
neutrals, `font-mono` for codey/metric text), this DS defines ONE semantic **status** vocabulary
— always prefer it over hand-picked colors so state reads consistently:

| Status   | text                  | soft background          | border                      | meaning            |
|----------|-----------------------|--------------------------|-----------------------------|--------------------|
| idle     | `text-status-idle`    | `bg-status-idle-soft`    | `border-status-idle-border` | neutral / pending  |
| running  | `text-status-running` | `bg-status-running-soft` | `border-status-running-border` | in progress (blue) |
| success  | `text-status-success` | `bg-status-success-soft` | `border-status-success-border` | passed (green)     |
| failed   | `text-status-failed`  | `bg-status-failed-soft`  | `border-status-failed-border`  | failed (red)       |
| attention| `text-status-attention`| `bg-status-attention-soft`| `border-status-attention-border` | needs review (amber) |

The primary action color is `--primary` (blue `#2563eb`): `bg-primary`, `text-primary`.
Don't hand-color state — use the components' own props instead:
`<Badge tone="green|red|blue|amber|neutral">`, `<StatusBadge status="running|success|failed|…">`,
`<TestBar pass fail total>`. The tokens behind all of this are CSS custom properties
(`--status-success`, `--primary`, `--radius`, …) defined in `styles.css`.

## Where the truth lives

- **`styles.css`** (bound copy: `_ds/<folder>/styles.css`) — the token + utility source; read it
  before introducing any new color or spacing.
- **`<Name>.d.ts`** — the prop contract. The primitives (`Badge`, `Button`, `Card`, `Tabs`,
  `TestBar`) carry full prop types; the data components (`BuildFacet`, `TestFacet`, `DetailPane`,
  `WorkspaceRail`, …) take a session/workspace data object — read `<Name>.prompt.md` for the shape.

## Idiomatic example

```tsx
import { Card, CardHead, StatusBadge, TestBar, Button } from "<bundle>"

function ModuleRow() {
  return (
    <Card className="p-4">
      <CardHead
        title="acme-core"
        sub="maven · 2m 41s"
        right={<StatusBadge status="success" />}
      />
      <div className="mt-3 flex items-center justify-between">
        <TestBar pass={540} fail={2} total={542} />
        <span className="font-mono text-[11px] text-status-success">86% line cov</span>
        <Button size="sm" variant="subtle">View report</Button>
      </div>
    </Card>
  )
}
```

Layout glue is yours (flex/grid + slate utilities); the library components carry the controls
and the status semantics.
