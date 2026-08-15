import { useState } from "react"

import { cn } from "@/lib/utils"

export type BytesStatus = "absent" | "loading" | "ready" | "error"

function missingMessage(status: BytesStatus): string {
  switch (status) {
    case "loading":
      return "Resolving bytes…"
    case "error":
      return "The bytes could not be fetched. Retry from the header."
    case "absent":
      return "Expand fetches the bytes at detail=full."
    case "ready":
      // The full tier answered and did not carry this ref: a handle the output
      // store was never built to resolve (a detached job's books, a ref written
      // by a store this session no longer has). Said, not blanked.
      return "No store in this session answers this ref."
  }
}

/**
 * One ref, descendable to the bytes it names.
 *
 * The handle is always shown — a reader can descend to the authoritative record
 * with it whether or not the full tier resolved it — and a ref the store cannot
 * answer says so instead of rendering as an empty box.
 */
export function RefDescent({
  refName,
  outputs,
  status,
  label,
  className,
}: {
  refName: string
  outputs: Record<string, string> | null
  status: BytesStatus
  label?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const bytes = outputs?.[refName]

  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex flex-wrap items-center gap-2">
        {label ? (
          <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            {label}
          </span>
        ) : null}
        <button
          aria-expanded={open}
          className={cn(
            "max-w-full truncate rounded-md border border-border bg-muted px-2 py-0.5 text-left font-mono text-[11px] text-foreground transition-colors hover:bg-accent",
            open && "border-primary/40",
          )}
          onClick={() => setOpen((value) => !value)}
          type="button"
        >
          {refName}
        </button>
      </div>
      {open ? (
        bytes != null ? (
          <pre className="mt-1.5 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-foreground">
            {bytes}
          </pre>
        ) : (
          <p className="mt-1.5 rounded-md border border-dashed border-border px-2.5 py-2 text-[11.5px] text-muted-foreground">
            {missingMessage(status)}
          </p>
        )
      ) : null}
    </div>
  )
}
