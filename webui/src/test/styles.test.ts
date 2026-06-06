import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { describe, expect, it } from "vitest"

const stylesPath = resolve(__dirname, "../styles.css")

describe("global shadcn styles", () => {
  it("registers Tailwind v4 semantic tokens used by generated components", () => {
    const css = readFileSync(stylesPath, "utf-8")

    expect(css).toContain('@import "tw-animate-css";')
    expect(css).toContain("@theme inline")

    for (const token of [
      "--color-background: var(--background);",
      "--color-foreground: var(--foreground);",
      "--color-card: var(--card);",
      "--color-card-foreground: var(--card-foreground);",
      "--color-primary: var(--primary);",
      "--color-primary-foreground: var(--primary-foreground);",
      "--color-muted: var(--muted);",
      "--color-muted-foreground: var(--muted-foreground);",
      "--color-accent: var(--accent);",
      "--color-accent-foreground: var(--accent-foreground);",
      "--color-border: var(--border);",
      "--color-input: var(--input);",
      "--color-ring: var(--ring);",
    ]) {
      expect(css).toContain(token)
    }
  })
})
