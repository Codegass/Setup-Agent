import type * as React from "react"

import type { Tone } from "@/api/types"
import { cn } from "@/lib/utils"

import { statusMeta } from "./status"

const toneClasses: Record<Tone, string> = {
  neutral: "border-slate-200 bg-slate-100 text-slate-600",
  blue: "border-blue-200 bg-blue-50 text-blue-700",
  green: "border-emerald-200 bg-emerald-50 text-emerald-700",
  red: "border-red-200 bg-red-50 text-red-700",
  amber: "border-amber-200 bg-amber-50 text-amber-700",
}

const dotClasses: Record<Tone, string> = {
  neutral: "bg-slate-400",
  blue: "bg-blue-500",
  green: "bg-emerald-500",
  red: "bg-red-500",
  amber: "bg-amber-500",
}

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone
  mono?: boolean
}

export function Badge({
  tone = "neutral",
  mono = false,
  className,
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-none",
        mono && "font-mono",
        toneClasses[tone],
        className,
      )}
      {...props}
    />
  )
}

export interface StatusBadgeProps
  extends Omit<BadgeProps, "children" | "tone"> {
  status: string
  dot?: boolean
  label?: React.ReactNode
  pulse?: boolean
}

export function StatusBadge({
  status,
  dot = true,
  label,
  pulse,
  className,
  ...props
}: StatusBadgeProps) {
  const meta = statusMeta(status)
  const normalized = status.trim().toLowerCase()
  const shouldPulse = pulse ?? (normalized === "running" || normalized === "active")

  return (
    <Badge tone={meta.tone} className={className} {...props}>
      {dot ? (
        <span
          aria-hidden="true"
          className={cn("relative inline-flex h-1.5 w-1.5 rounded-full", dotClasses[meta.tone])}
        >
          {shouldPulse ? (
            <span
              className={cn(
                "absolute inline-flex h-full w-full animate-ping rounded-full opacity-75",
                dotClasses[meta.tone],
              )}
            />
          ) : null}
        </span>
      ) : null}
      {label ?? meta.label}
    </Badge>
  )
}
