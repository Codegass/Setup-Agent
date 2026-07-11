import type * as React from "react"

import { cn } from "@/lib/utils"

export type TabItem =
  | string
  | {
      id: string
      label: React.ReactNode
      count?: number
      disabled?: boolean
    }

export interface TabsProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "onChange"> {
  tabs: TabItem[]
  value: string
  onChange: (value: string) => void
}

function tabId(tab: TabItem): string {
  return typeof tab === "string" ? tab : tab.id
}

function tabLabel(tab: TabItem): React.ReactNode {
  return typeof tab === "string" ? tab : tab.label
}

export function Tabs({ tabs, value, onChange, className, ...props }: TabsProps) {
  return (
    <div
      className={cn("flex flex-wrap items-center gap-1 border-b border-border", className)}
      {...props}
    >
      {tabs.map((tab) => {
        const id = tabId(tab)
        const active = id === value
        const count = typeof tab === "string" ? undefined : tab.count
        const disabled = typeof tab === "string" ? false : tab.disabled

        return (
          <button
            key={id}
            className={cn(
              "relative -mb-px flex items-center gap-1.5 px-3 py-2 text-[13px] font-medium transition-colors",
              active ? "text-primary" : "text-muted-foreground hover:text-foreground",
              disabled && "cursor-not-allowed opacity-50 hover:text-muted-foreground",
            )}
            disabled={disabled}
            type="button"
            onClick={() => {
              if (!disabled) {
                onChange(id)
              }
            }}
          >
            {tabLabel(tab)}
            {count != null ? (
              <span
                className={cn(
                  "rounded px-1.5 py-0.5 font-mono text-[10px]",
                  active ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
                )}
              >
                {count}
              </span>
            ) : null}
            {active ? (
              <span className="absolute inset-x-0 -bottom-px h-0.5 rounded-full bg-primary" />
            ) : null}
          </button>
        )
      })}
    </div>
  )
}
