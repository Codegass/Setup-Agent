---
name: SAG Workbench
description: Local mission-control for SAG's LLM setup agents
colors:
  background: "oklch(0.985 0.003 247)"
  foreground: "oklch(0.208 0.04 265)"
  card: "oklch(1 0 0)"
  primary: "oklch(0.55 0.2 255)"
  primary-foreground: "oklch(0.985 0.003 247)"
  secondary: "oklch(0.955 0.008 247)"
  muted: "oklch(0.952 0.006 247)"
  muted-foreground: "oklch(0.47 0.035 263)"
  accent: "oklch(0.94 0.018 247)"
  destructive: "oklch(0.59 0.22 29)"
  border: "oklch(0.89 0.012 247)"
  ring: "oklch(0.55 0.2 255)"
  status-running: "oklch(0.488 0.243 264)"
  status-success: "oklch(0.596 0.145 163)"
  status-failed: "oklch(0.577 0.245 27)"
  status-attention: "oklch(0.555 0.163 49)"
  status-idle: "oklch(0.446 0.043 257)"
  dark-background: "oklch(0.18 0.032 265)"
  dark-foreground: "oklch(0.965 0.006 247)"
  dark-card: "oklch(0.22 0.036 265)"
  dark-primary: "oklch(0.72 0.16 252)"
typography:
  page-title:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  metric:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "26px"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "normal"
  card-title:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "normal"
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  meta:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
  label:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "10px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "0.12em"
  data:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
  xl: "12px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 12px"
  button-primary-hover:
    backgroundColor: "oklch(0.55 0.2 255 / 90%)"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.md}"
  button-outline:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 12px"
  badge-status:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.muted-foreground}"
    rounded: "{rounded.full}"
    padding: "2px 8px"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
    padding: "16px"
  input:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    padding: "6px 8px"
---

# Design System: SAG Workbench

## 1. Overview

**Creative North Star: "The Instrument Panel"**

SAG Workbench is read the way a pilot reads a console: a dense field of
instruments where nothing draws the eye until something is wrong. The surface
is a near-white slate, the chrome is hairline, and the only saturated color on
screen belongs to live state, running, queued, failed, needs-attention. A
healthy fleet of workspaces should look almost monochrome; a failed setup
should be the one lit gauge you cannot miss. Type does the structural work that
ornament does elsewhere: scale and weight build the hierarchy, monospace
carries the engineering payload (container ids, refs, commit hashes, log
paths), and color is held in reserve as signal.

This system explicitly rejects the generic SaaS analytics dashboard: no hero
metric template, no identical card grids, no gradient accents, no decorative
illustration. Engineering detail is not noise to be hidden behind gloss; ids,
refs, exit codes, and paths are first-class content shown plainly. The mood is
observant, precise, and unhurried, an operations room, not a marketing page.

**Key Characteristics:**
- Status earns color; everything else is slate and ink.
- Monospace is a first-class voice for machine identifiers, not an accent.
- Flat by default; depth appears only on overlays and on hover.
- Dense but legible: many rows are welcome, hierarchy keeps them scannable.
- Light is the shipping theme; a full dark "night console" palette is wired in
  tokens and supported, awaiting a theme switch.

## 2. Colors

A restrained slate-and-ink base with a single blue accent, plus a five-hue
status vocabulary that is the only place saturation is allowed to live.

### Primary
- **Console Blue** (`oklch(0.55 0.2 255)`): The one accent. Primary buttons,
  the focus ring, current selection, active-session markers, and the "running"
  status hue. Reserved for action and live state, never decoration.

### Neutral
- **Slate Field** (`oklch(0.985 0.003 247)`): The body background, a near-white
  with the faintest cool tint; the canvas the instruments sit on.
- **Console Ink** (`oklch(0.208 0.04 265)`): Primary text and headings.
- **Surface White** (`oklch(1 0 0)`): Cards, panels, dialogs, inputs; the lit
  faces of the instruments, lifted off the field by a hairline border.
- **Muted Ink** (`oklch(0.47 0.035 263)`): Secondary text and meta lines. This
  is the floor for readable body text; do not go lighter for content.
- **Hairline** (`oklch(0.89 0.012 247)`): Borders, dividers, input strokes. The
  primary structural element of the whole UI.

### Status (the only saturated colors on screen)
- **Running Blue** (`oklch(0.488 0.243 264)`, Tailwind `blue-700` on `blue-50`):
  In-progress work, running containers, launching setups, active sessions.
- **Success Green** (`oklch(0.596 0.145 163)`, `emerald-600` on `emerald-50`):
  Completed setups, passing builds, ready reports.
- **Failed Red** (`oklch(0.577 0.245 27)`, `red-600` on `red-50`): Failed
  builds and tests, exited containers, failed launches; also the destructive
  action color (`oklch(0.59 0.22 29)`).
- **Attention Amber** (`oklch(0.555 0.163 49)`, `amber-700` on `amber-50`):
  Partial results, stopped containers, soft warnings.
- **Idle Slate** (`oklch(0.446 0.043 257)`, `slate-600` on `slate-100`): Queued,
  pending, created, none; states that are waiting, not working.

### Dark theme (supported, dormant)
A complete night-console palette is defined under `.dark` and tracked as
first-class (`dark-background oklch(0.18 0.032 265)`, `dark-card
oklch(0.22 0.036 265)`, `dark-primary oklch(0.72 0.16 252)`, borders as low-alpha
white). It is not yet wired to a toggle; treat it as a supported second theme to
keep on-brand, not a separate identity.

### Named Rules
**The Signal Rule.** Saturated color belongs to state and to the single primary
action only. If a hue on screen is not reporting status or marking the primary
action, it is wrong. A dashboard of healthy workspaces should read as nearly
grayscale.

**The Hairline Rule.** Structure is carried by 1px borders in Hairline, not by
shadow and not by fills. Colored side-stripe borders are forbidden.

## 3. Typography

**Body / UI Font:** Inter (with `ui-sans-serif, system-ui, sans-serif` fallback)
**Data / Mono Font:** `ui-mono` stack (`ui-monospace, SFMono-Regular, Menlo`)

**Character:** One humanist sans does all structural and prose work; a
monospace runs in parallel as the dedicated voice for machine identifiers.
Inter must be loaded as a web font; declared in the body stack, it currently
falls through to the system sans until loaded.

### Hierarchy
- **Page Title** (Inter 600, 22px, line-height 1.2): The one heading per view
  ("Workspaces"), with a `text-balance` treatment.
- **Metric** (Inter 600, 26px, tabular-nums): Large counts in summary stats.
- **Card Title** (Inter 600, 13px): Panel and card headers.
- **Body** (Inter 400, 13px, line-height 1.5): Task descriptions, prose,
  primary content. Cap prose at 65-75ch; data rows may run denser.
- **Meta** (Inter 400, 11px): Secondary descriptions, sub-labels, counts.
- **Label** (Inter 400, 10px, letter-spacing 0.12em, uppercase): Column headers
  and field eyebrows. Used heavily today; reserve for true table headers and
  badges, not as the default voice for every label.
- **Data** (mono 400, 11px): Container ids, refs, commit hashes, log paths,
  PIDs, durations; anything a machine emits.

### Named Rules
**The Mono Payload Rule.** Every machine-generated identifier (id, ref, hash,
path, PID, exit code) is set in the mono face. Prose and human labels are
Inter. The two voices never cross.

## 4. Elevation

The system is flat by default. Cards and panels are defined entirely by a
Surface White fill on the Slate Field, separated by a 1px Hairline border, with
no resting shadow (the shared `Card` wrapper forces `shadow-none`). Depth is a
response to state, not a resting property: rows tint on hover
(`bg-slate-50/70`), and the only genuinely elevated surfaces are overlays.

### Shadow Vocabulary
- **Overlay** (`box-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1)` / Tailwind
  `shadow-lg`-`shadow-xl`): Dialogs and popovers only. These float above the
  console; everything else is in-plane.

### Named Rules
**The Flat-By-Default Rule.** Surfaces are flat at rest. Borders do the
separating; shadow is reserved for elements that genuinely float (dialogs,
popovers). If a card has a resting drop shadow, it is wrong.

## 5. Components

### Buttons
- **Shape:** Gently rounded (6px, `rounded-md`), 32px tall at the default size,
  flat (no resting shadow).
- **Primary:** Console Blue fill, white text (`button-primary`); the single
  high-emphasis action per view (Launch setups, Submit). Hover darkens to 90%
  opacity of the accent.
- **Outline:** Surface White fill, Hairline border, ink text (`button-outline`);
  the default for secondary actions (Refresh, Cancel). Hover fills with Accent.
- **Ghost / Subtle:** Borderless, tints on hover; for low-emphasis inline
  actions and icon buttons.
- **Focus:** 1px Console Blue ring (`focus-visible:ring-ring`). Always visible,
  never removed.

### Badges
- **Style:** Pill (`rounded-full`), soft-tinted background with same-hue text,
  matching 200-weight border; 11px, medium weight.
- **StatusBadge:** Maps any status string through a single `statusMeta`
  vocabulary to one of five tones (running/success/failed/attention/idle). A
  leading 1.5px dot carries the tone; running and active states add an
  `animate-ping` pulse on the dot.

### Cards / Panels
- **Corner Style:** 8px (`rounded-lg`).
- **Background:** Surface White on the Slate Field.
- **Shadow Strategy:** None at rest (see Elevation).
- **Border:** 1px Hairline; this, not shadow, is what lifts the card.
- **Header:** Optional `CardHead` strip with a bottom Hairline, a 13px title,
  optional 11px sub, and a right-aligned action slot.
- **Internal Padding:** 16px (`p-4`) typical; 12-14px on dense rows. Nested
  cards are forbidden.

### Inputs / Fields
- **Style:** Surface White fill, 1px Hairline stroke, 6px radius; mono text for
  fields that hold machine values (repo URL, ref, name).
- **Focus:** Border shifts to Console Blue with a soft 2px blue ring
  (`focus:ring-blue-500/20`).
- **Error:** Red border with the message set below the field/row in Failed Red.
- **Disabled:** Reduced opacity, no pointer events (during submit).

### Navigation
- **Style:** A single sticky top bar (Surface White at 85% with backdrop blur,
  bottom Hairline). Left: wordmark plus a breadcrumb trail
  (dashboard / workspace / session) in 12.5px, active segment in ink, ancestors
  in Muted Ink. Right: a live Docker status pill. No side nav.

### Signature: Workspace Table / Card
The dashboard's core component. A CSS-grid table on desktop
(`grid-cols-[2fr_1fr_1.3fr_...]`) that collapses to stacked cards on mobile.
Each row is one workspace: project + meta, container id (mono) with a status
dot, current task, build state, a `TestBar` (a 20px split pass/fail meter plus
mono counts), report readiness, changed-file count, and hover-revealed actions.
The same cells render in both the desktop row and the mobile card so the
vocabulary never diverges.

## 6. Do's and Don'ts

### Do:
- **Do** keep saturated color for status and the single primary action only;
  a healthy dashboard should read nearly grayscale (The Signal Rule).
- **Do** set every machine identifier (id, ref, hash, path, PID) in the mono
  face, and all prose and human labels in Inter (The Mono Payload Rule).
- **Do** separate surfaces with 1px Hairline borders; reserve shadow for
  dialogs and popovers (The Flat-By-Default Rule).
- **Do** route every status string through `statusMeta` so a state maps to the
  same tone everywhere it appears.
- **Do** keep Muted Ink (`oklch(0.47 0.035 263)`) as the lightest text used for
  content; lighter slates are for icons and separators only.
- **Do** show engineering detail plainly; exit codes, paths, and refs are
  content, not noise.

### Don't:
- **Don't** build the generic SaaS analytics dashboard: no hero-metric
  template, no identical card grids, no gradient accents, no decorative
  illustration. (PRODUCT.md anti-reference.)
- **Don't** hide engineering detail behind gloss. (PRODUCT.md anti-reference.)
- **Don't** use `background-clip: text` gradients or any gradient as accent.
- **Don't** put a resting drop shadow on a card; flat-by-default is the rule.
- **Don't** use colored side-stripe borders (`border-left`/`border-right` > 1px)
  on cards, rows, or alerts.
- **Don't** nest cards inside cards.
- **Don't** use the 10px uppercase tracked label as the voice for every piece
  of text; reserve it for true table headers and badges.
- **Don't** let saturated color appear on inactive or healthy states; quiet is
  the default, color is the exception.
