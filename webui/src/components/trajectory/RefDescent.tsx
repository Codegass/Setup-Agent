import { useMemo, useState } from "react"

import { cn } from "@/lib/utils"

export type BytesStatus = "absent" | "loading" | "ready" | "error"

type MetadataValue = string | number | boolean | null

type DecodedOutput =
  | {
      kind: "text"
      text: string
    }
  | {
      kind: "json"
      formatted: string
      raw: string
    }
  | {
      kind: "record"
      text: string
      textKey: string
      metadata: Array<[string, MetadataValue]>
      otherFields: Record<string, unknown> | null
      raw: string
    }

const PRIMARY_TEXT_KEYS = ["content", "output", "message", "text"] as const

function isMetadataValue(value: unknown): value is MetadataValue {
  return value === null || ["string", "number", "boolean"].includes(typeof value)
}

function decodeOutput(bytes: string): DecodedOutput {
  let parsed: unknown
  try {
    parsed = JSON.parse(bytes) as unknown
  } catch {
    return { kind: "text", text: bytes }
  }

  if (typeof parsed === "string") {
    return {
      kind: "record",
      text: parsed,
      textKey: "content",
      metadata: [],
      otherFields: null,
      raw: bytes,
    }
  }

  if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
    const record = parsed as Record<string, unknown>
    const textKey = PRIMARY_TEXT_KEYS.find((key) => typeof record[key] === "string")
    if (textKey) {
      const metadata: Array<[string, MetadataValue]> = []
      const otherFields: Record<string, unknown> = {}

      for (const [key, value] of Object.entries(record)) {
        if (key === textKey) {
          continue
        }
        if (isMetadataValue(value)) {
          metadata.push([key, value])
        } else {
          otherFields[key] = value
        }
      }

      return {
        kind: "record",
        text: record[textKey] as string,
        textKey,
        metadata,
        otherFields: Object.keys(otherFields).length ? otherFields : null,
        raw: bytes,
      }
    }
  }

  return {
    kind: "json",
    formatted: JSON.stringify(parsed, null, 2),
    raw: bytes,
  }
}

function displayText(decoded: DecodedOutput): string {
  switch (decoded.kind) {
    case "text":
    case "record":
      return decoded.text
    case "json":
      return decoded.formatted
  }
}

function sizeSummary(text: string): string {
  const lines = text.length ? text.split(/\r\n|\r|\n/).length : 0
  return `${text.length.toLocaleString("en-US")} chars · ${lines.toLocaleString("en-US")} ${
    lines === 1 ? "line" : "lines"
  }`
}

function metadataText(value: MetadataValue): string {
  if (value === null) {
    return "null"
  }
  return String(value)
}

function metadataLabel(key: string): string {
  return key.replace(/_/g, " ")
}

function missingMessage(status: BytesStatus, refName: string): string {
  switch (status) {
    case "loading":
      return "Resolving bytes…"
    case "error":
      return "The bytes could not be fetched. Retry from the header."
    case "absent":
      return "Expand fetches the bytes at detail=full."
    case "ready":
      if (refName.startsWith("job:")) {
        return "This is a background-job handle, not stored output bytes."
      }
      // The full tier answered and did not carry this ref: a handle the output
      // store was never built to resolve (a detached job's books, a ref written
      // by a store this session no longer has). Said, not blanked.
      return "No store in this session answers this ref."
  }
}

function OutputBody({ decoded, refName }: { decoded: DecodedOutput; refName: string }) {
  const [showRaw, setShowRaw] = useState(false)
  const canShowRaw = decoded.kind !== "text"
  const text = showRaw && canShowRaw ? decoded.raw : displayText(decoded)
  const title = showRaw
    ? "Raw JSON"
    : decoded.kind === "json"
      ? "Formatted JSON"
      : decoded.kind === "record"
        ? `Formatted ${decoded.textKey}`
        : "Plain text"

  return (
    <div className="mt-1.5 overflow-hidden rounded-md border border-border bg-muted">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-2.5 py-1.5">
        <span className="text-[10.5px] font-medium text-muted-foreground">{title}</span>
        {canShowRaw ? (
          <button
            aria-label={`${showRaw ? "Show formatted output" : "Show raw JSON"} for ${refName}`}
            className="rounded px-1.5 py-0.5 text-[10.5px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            onClick={() => setShowRaw((value) => !value)}
            type="button"
          >
            {showRaw ? "Show formatted" : "Show raw JSON"}
          </button>
        ) : null}
      </div>

      {!showRaw && decoded.kind === "record" && decoded.metadata.length ? (
        <dl className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border px-2.5 py-1.5">
          {decoded.metadata.map(([key, value]) => (
            <div className="flex min-w-0 items-baseline gap-1.5" key={key}>
              <dt className="text-[10px] font-semibold uppercase tracking-[0.05em] text-muted-foreground">
                {metadataLabel(key)}
              </dt>
              <dd className="break-all font-mono text-[10.5px] text-foreground">
                {metadataText(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      <pre
        className="max-h-72 overflow-auto whitespace-pre-wrap break-words px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-foreground"
        data-testid={`ref-output-${refName}`}
      >
        {text}
      </pre>

      {!showRaw && decoded.kind === "record" && decoded.otherFields ? (
        <details className="border-t border-border px-2.5 py-1.5 text-[10.5px] text-muted-foreground">
          <summary className="cursor-pointer select-none font-medium hover:text-foreground">
            Other JSON fields
          </summary>
          <pre className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[10.5px] leading-relaxed text-foreground">
            {JSON.stringify(decoded.otherFields, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  )
}

/**
 * One ref, descendable to the bytes it names.
 *
 * The handle is always shown. Parsed JSON gets a readable view by default,
 * while the exact stored bytes remain available from the raw toggle.
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
  const decoded = useMemo(() => (bytes == null ? null : decodeOutput(bytes)), [bytes])

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
        {decoded ? (
          <span className="font-mono text-[10.5px] text-muted-foreground">
            {sizeSummary(displayText(decoded))}
          </span>
        ) : null}
      </div>
      {open ? (
        decoded ? (
          <OutputBody decoded={decoded} refName={refName} />
        ) : (
          <p className="mt-1.5 rounded-md border border-dashed border-border px-2.5 py-2 text-[11.5px] text-muted-foreground">
            {missingMessage(status, refName)}
          </p>
        )
      ) : null}
    </div>
  )
}
