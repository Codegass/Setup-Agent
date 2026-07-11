import type * as React from "react"

import { cn } from "@/lib/utils"

export interface TestBarProps extends React.HTMLAttributes<HTMLDivElement> {
  pass: number
  fail: number
  total: number
}

function percent(value: number, total: number): string {
  return `${Math.max(0, Math.min(100, (value / total) * 100))}%`
}

export function TestBar({ pass, fail, total, className, ...props }: TestBarProps) {
  if (total <= 0) {
    return <span className="text-muted-foreground">—</span>
  }

  return (
    <div className={cn("flex items-center gap-2", className)} {...props}>
      <div
        aria-label={`${pass} passed, ${fail} failed, ${total} total`}
        className="flex h-1.5 w-20 overflow-hidden rounded-full bg-muted"
        role="img"
      >
        <div className="h-full bg-status-success" style={{ width: percent(pass, total) }} />
        <div className="h-full bg-status-failed" style={{ width: percent(fail, total) }} />
      </div>
      <span className="font-mono text-[11px] text-muted-foreground">
        <span className="text-status-success">{pass}</span>
        {fail ? <span className="text-status-failed"> / {fail}</span> : null}{" "}
        <span className="text-muted-foreground">· {total}</span>
      </span>
    </div>
  )
}
