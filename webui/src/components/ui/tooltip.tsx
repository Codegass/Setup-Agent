import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

/**
 * CSS-only hover/focus tooltip: no deps, no JS state. Wraps its child in an
 * inline-flex group; the bubble fades in on hover or keyboard focus within.
 * ponytail: pure CSS means no collision handling — pass side="bottom" for
 * elements near the top edge; upgrade to a positioning lib only if labels
 * start clipping in real layouts.
 */
export function Tooltip({
  label,
  side = "top",
  children,
  className,
}: {
  label: string
  side?: "top" | "bottom"
  children: ReactNode
  className?: string
}) {
  return (
    <span className={cn("group/tt relative inline-flex", className)}>
      {children}
      <span
        role="tooltip"
        className={cn(
          "pointer-events-none absolute left-1/2 z-[var(--z-popover,40)] -translate-x-1/2",
          "whitespace-nowrap rounded-md bg-foreground px-2 py-1 text-[11px] font-medium text-background shadow-md",
          "opacity-0 transition-opacity delay-200 duration-100",
          "group-hover/tt:opacity-100 group-focus-within/tt:opacity-100",
          side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5",
        )}
      >
        {label}
      </span>
    </span>
  )
}
