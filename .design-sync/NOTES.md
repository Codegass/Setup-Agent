# design-sync notes — sag-workbench (Setup-Agent UI)

The DS source is the **`webui/` Vite app** (`sag-workbench`), not a published library — there
is **no dist/, no package exports, no Storybook**. The sync runs the package shape in
**synth-entry mode via a hand-authored barrel**.

## Build prerequisites (run before `package-build.mjs`)

Two artifacts must be regenerated from `webui/` before every build (both gitignored):

1. **Compiled Tailwind CSS** → `webui/.ds-compiled.css` (cssEntry must live inside the package):
   ```sh
   cd webui && npx -y @tailwindcss/cli@4.3.0 -i src/styles.css -o .ds-compiled.css
   ```
   Tailwind v4 is CSS-first (`@import "tailwindcss"` + `@theme inline`); `styles.css` itself is
   NOT a compiled stylesheet. Match the installed `tailwindcss` version (currently 4.3.0).

2. **TypeScript declarations** → `webui/dist/types` (real prop contracts; `findTypesRoot` finds `dist/types`):
   ```sh
   cd webui && npx tsc -p tsconfig.app.json --noEmit false --declaration --emitDeclarationOnly --skipLibCheck --outDir dist/types
   ```
   `--noEmit false` is required (tsconfig.app.json sets `noEmit: true`, which otherwise suppresses emit).

## Build / validate (from repo root)

```sh
node .ds-sync/package-build.mjs --config .design-sync/config.json --node-modules webui/node_modules --entry ./webui/.ds-entry.tsx --out ./ds-bundle
node .ds-sync/package-validate.mjs ./ds-bundle
```

- `--entry ./webui/.ds-entry.tsx` (the barrel) makes `PKG_DIR` resolve to `webui/` (sag-workbench
  isn't in node_modules) AND keeps `main.tsx`'s `render()` side-effect out of the bundle.
- The barrel exposes the **`common/` design layer**, not the raw `ui/` shadcn primitives it wraps
  (they share names: Badge/Button/Card/Tabs). `exportedNames` finds no main-entry .d.ts, so the
  component list comes entirely from `cfg.componentSrcMap` — no ui/ collisions or bloat.
- `componentSrcMap` pins all 35 components; `App`/`Empty`/`FacetBody` are excluded (`null`).

## Props

- Components with a named `XProps` interface (the `common/` primitives) get real props.
- Components with **inline destructured props** (most session/detail/app-shell — `({ detail }: { detail: ExecutionSessionDetail })`)
  fall back to `[key: string]: unknown`. The authored preview + synthesized `.prompt.md` carry the
  real usage instead. Add `cfg.dtsPropsFor.<Name>` if a specific contract matters.

## Previews

- All app-shell components are **prop-driven** (no react-query/router/zustand in deps) — previews
  need realistic **fixtures**, NOT provider mocking. `TerminalPanel` is the exception (xterm +
  websocket; renders the terminal chrome only, no live connection).
- Fixture references: `webui/src/api/types.ts` (the type shapes), the component `*.test.tsx` files
  (existing fixtures), and `src/sag/web/demo_data.py` (realistic example data shapes).

## Known render warns / accepted

- `[FONT_MISSING] "Inter"` — accepted. The brand font **Inter Variable** IS shipped (via
  `cfg.extraFonts` → `@fontsource-variable/inter/index.css`, 7 @font-face) and is first in the
  `"Inter Variable", Inter, …` stack, so it renders. "Inter" is a redundant non-variable fallback
  that never needs to load; not worth shipping a second static family.

## Re-sync risks

- `webui/.ds-compiled.css` and `webui/dist/types` are gitignored build inputs — they MUST be
  regenerated (above) before each build or the bundle ships stale CSS / weak props.
- The barrel (`webui/.ds-entry.tsx`) is gitignored but is a real sync input — if it's lost, recreate
  it from `cfg.componentSrcMap` (one `export { … } from "@/…"` per component).
- Tailwind/tsc versions are pinned to what `webui/` currently installs — bumps may change output.
