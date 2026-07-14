import { FileText } from "lucide-react"

import type { ReportDocument } from "@/api/types"
import { Card, CardHead } from "@/components/common/Card"

export function ReportDoc({ doc }: { doc?: ReportDocument | null }) {
  if (!doc) {
    return (
      <div className="px-4 py-10 text-center text-[13px] text-muted-foreground">
        No report generated for this session.
      </div>
    )
  }

  return (
    <Card className="overflow-hidden">
      <CardHead
        icon={<FileText size={16} className="text-muted-foreground" />}
        right={
          doc.path ? (
            <span
              className="block max-w-[280px] truncate rounded-md border border-border bg-muted px-2 py-1 font-mono text-[10.5px] text-muted-foreground"
              title={doc.path}
            >
              {doc.path}
            </span>
          ) : null
        }
        sub={`Generated ${doc.generated}`}
        title={doc.title}
      />
      <div className="px-6 py-5">
        <div className="mx-auto max-w-[68ch] space-y-3">
          {doc.blocks.map((block, index) => (
            <ReportBlock block={block} key={index} />
          ))}
        </div>
      </div>
    </Card>
  )
}

function ReportBlock({ block }: { block: Record<string, unknown> }) {
  const type = typeof block.type === "string" ? block.type : "unknown"
  const text = typeof block.text === "string" ? block.text : ""
  const heading = typeof block.heading === "string" ? block.heading : ""
  const body = typeof block.body === "string" ? block.body : text

  if ((type === "summary" || type === "evidence") && body) {
    return (
      <section className="rounded-md border border-border bg-muted px-3 py-2.5">
        {heading ? (
          <div className="text-[13px] font-semibold text-foreground">{heading}</div>
        ) : null}
        <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">{body}</p>
      </section>
    )
  }

  if (type === "h1") {
    return <h1 className="text-[20px] font-semibold tracking-tight text-foreground">{text}</h1>
  }

  if (type === "h2") {
    return (
      <h2 className="!mt-6 border-b border-border pb-1.5 text-[15px] font-semibold text-foreground">
        {text}
      </h2>
    )
  }

  if (type === "meta") {
    return <div className="font-mono text-[11px] text-muted-foreground">{text}</div>
  }

  if (type === "p") {
    return <p className="text-[13.5px] leading-relaxed text-muted-foreground">{text}</p>
  }

  if (type === "status") {
    const ok = Boolean(block.ok)

    return (
      <div
        className={`flex items-center gap-2 rounded-md border px-3 py-2 text-[13px] ${
          ok ? "border-status-success-border bg-status-success-soft text-status-success" : "border-status-failed-border bg-status-failed-soft text-status-failed"
        }`}
      >
        {text}
      </div>
    )
  }

  if (type === "ul" && Array.isArray(block.items)) {
    return (
      <ul className="space-y-1.5">
        {block.items.map((item, index) => (
          <li key={index} className="flex gap-2 text-[13.5px] text-muted-foreground">
            <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-muted-foreground" />
            {String(item)}
          </li>
        ))}
      </ul>
    )
  }

  if (type === "table" && Array.isArray(block.rows)) {
    const rows = block.rows.filter((row): row is unknown[] => Array.isArray(row))

    return (
      <div className="overflow-hidden rounded-md border border-border">
        <table className="w-full border-collapse text-[13px]">
          <tbody className="divide-y divide-border">
            {rows.map((row, index) => (
              <tr key={index} className="bg-card">
                {row.map((cell, cellIndex) => (
                  <td
                    key={`${index}-${cellIndex}`}
                    className={
                      cellIndex === 0
                        ? "w-40 bg-muted px-3 py-2 font-medium text-muted-foreground"
                        : "px-3 py-2 text-muted-foreground"
                    }
                  >
                    {String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <pre className="overflow-auto rounded-md border border-border bg-muted p-3 font-mono text-[11px] leading-relaxed text-muted-foreground">
      {JSON.stringify(block, null, 2)}
    </pre>
  )
}
