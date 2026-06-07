import type * as React from "react"

import { Card as ShadCard } from "@/components/ui/card"
import { cn } from "@/lib/utils"

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {}

export function Card({ className, ...props }: CardProps) {
  return (
    <ShadCard
      className={cn("rounded-lg border-slate-200 bg-white shadow-none", className)}
      {...props}
    />
  )
}

export interface CardHeadProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  title: React.ReactNode
  sub?: React.ReactNode
  right?: React.ReactNode
  icon?: React.ReactNode
}

export function CardHead({
  title,
  sub,
  right,
  icon,
  className,
  ...props
}: CardHeadProps) {
  return (
    <div
      className={cn(
        "flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-3",
        className,
      )}
      {...props}
    >
      <div className="flex min-w-0 items-center gap-2">
        {icon}
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold leading-tight text-slate-800">
            {title}
          </div>
          {sub ? <div className="mt-0.5 truncate text-[11px] text-slate-500">{sub}</div> : null}
        </div>
      </div>
      {right ? <div className="shrink-0">{right}</div> : null}
    </div>
  )
}
