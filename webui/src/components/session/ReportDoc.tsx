import { ExternalLink, FileText } from "lucide-react"

import type { ReportDocument } from "@/api/types"
import { Button } from "@/components/common/Button"
import { Card, CardHead } from "@/components/common/Card"

export function ReportDoc({ doc }: { doc?: ReportDocument | null }) {
  if (!doc) {
    return (
      <div className="px-4 py-10 text-center text-[13px] text-slate-400">
        No report generated for this session.
      </div>
    )
  }

  return (
    <Card className="overflow-hidden">
      <CardHead
        icon={<FileText size={16} className="text-slate-400" />}
        right={
          doc.path ? (
            <Button asChild size="sm" variant="outline">
              <a href={doc.path}>
                <ExternalLink size={13} />
                Open raw
              </a>
            </Button>
          ) : null
        }
        sub={`Generated ${doc.generated}`}
        title={doc.title}
      />
      <div className="px-6 py-5">
        <div className="mx-auto max-w-[680px] space-y-3">
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

  if (type === "h1") {
    return <h1 className="text-[20px] font-semibold tracking-tight text-slate-900">{text}</h1>
  }

  if (type === "h2") {
    return (
      <h2 className="!mt-6 border-b border-slate-100 pb-1.5 text-[15px] font-semibold text-slate-800">
        {text}
      </h2>
    )
  }

  if (type === "meta") {
    return <div className="font-mono text-[11px] text-slate-400">{text}</div>
  }

  if (type === "p") {
    return <p className="text-[13.5px] leading-relaxed text-slate-600">{text}</p>
  }

  if (type === "status") {
    const ok = Boolean(block.ok)

    return (
      <div
        className={`flex items-center gap-2 rounded-md border px-3 py-2 text-[13px] ${
          ok ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-red-200 bg-red-50 text-red-700"
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
          <li key={index} className="flex gap-2 text-[13.5px] text-slate-600">
            <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-slate-400" />
            {String(item)}
          </li>
        ))}
      </ul>
    )
  }

  return (
    <pre className="overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 font-mono text-[11px] leading-relaxed text-slate-500">
      {JSON.stringify(block, null, 2)}
    </pre>
  )
}
