import type * as React from "react"

import type { Tone } from "@/api/types"
import { cn } from "@/lib/utils"

import { isUsefulEvidenceStatus, statusMeta } from "./status"

const toneClasses: Record<Tone, string> = {
  neutral: "border-status-idle-border bg-status-idle-soft text-status-idle",
  blue: "border-status-running-border bg-status-running-soft text-status-running",
  green: "border-status-success-border bg-status-success-soft text-status-success",
  red: "border-status-failed-border bg-status-failed-soft text-status-failed",
  amber: "border-status-attention-border bg-status-attention-soft text-status-attention",
}

const dotClasses: Record<Tone, string> = {
  neutral: "bg-status-idle",
  blue: "bg-status-running",
  green: "bg-status-success",
  red: "bg-status-failed",
  amber: "bg-status-attention",
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

export function LabeledStatus({
  label,
  status,
  hideUnknown = false,
}: {
  label: string
  status?: string | null
  hideUnknown?: boolean
}) {
  const normalized = status?.trim() || "unknown"
  if (hideUnknown && !isUsefulEvidenceStatus(normalized)) {
    return null
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
        {label}
      </span>
      <StatusBadge status={normalized} />
    </span>
  )
}
