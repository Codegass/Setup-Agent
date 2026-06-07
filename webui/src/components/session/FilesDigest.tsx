import { FileText, Folder, Search } from "lucide-react"
import { useState } from "react"

import type { FileChangeDigest, Tone } from "@/api/types"
import { Badge } from "@/components/common/Badge"

const changeTone: Record<string, Tone> = {
  added: "green",
  modified: "blue",
  deleted: "red",
  renamed: "amber",
}

export function FilesDigest({
  digest,
  preview = false,
}: {
  digest?: FileChangeDigest | null
  preview?: boolean
}) {
  const [openPath, setOpenPath] = useState<string | null>(null)

  if (!digest) {
    return (
      <div className="px-4 py-6 text-center text-[13px] text-slate-400">
        No file snapshot was captured for this execution.
      </div>
    )
  }

  const items = preview ? digest.items.slice(0, 4) : digest.items

  return (
    <div>
      {!preview ? (
        <div className="flex flex-wrap items-center gap-3 border-b border-slate-100 px-4 py-2.5">
          <CountDot color="bg-emerald-500" label={`${digest.counts.added} added`} />
          <CountDot color="bg-blue-500" label={`${digest.counts.modified} modified`} />
          <CountDot color="bg-red-500" label={`${digest.counts.deleted} deleted`} />
          <CountDot color="bg-amber-500" label={`${digest.counts.renamed} renamed`} />
          <div className="ml-auto font-mono text-[10.5px] text-slate-400">
            snapshot {digest.snapshot.base} - {digest.snapshot.head} / {digest.snapshot.mode}
          </div>
        </div>
      ) : null}
      <div className="divide-y divide-slate-100">
        {items.length ? (
          items.map((file) => {
            const open = openPath === file.path

            return (
              <div key={file.path}>
                <button
                  className={`flex w-full items-center gap-2.5 px-4 py-2.5 text-left ${
                    preview ? "" : "hover:bg-slate-50/70"
                  }`}
                  onClick={() => {
                    if (!preview) {
                      setOpenPath(open ? null : file.path)
                    }
                  }}
                  type="button"
                >
                  <Badge
                    tone={changeTone[file.change.trim().toLowerCase()] ?? "neutral"}
                    className="w-[72px] justify-center capitalize"
                  >
                    {file.change}
                  </Badge>
                  {file.type === "dir" ? (
                    <Folder size={14} className="shrink-0 text-slate-400" />
                  ) : (
                    <FileText size={14} className="shrink-0 text-slate-400" />
                  )}
                  <span className="truncate font-mono text-[12px] text-slate-600">{file.path}</span>
                  <span className="ml-auto shrink-0 font-mono text-[11px] text-slate-400">
                    {file.size}
                  </span>
                  <span className="hidden shrink-0 font-mono text-[10px] text-slate-300 sm:block">
                    {file.mtime}
                  </span>
                </button>
                {open && !preview ? (
                  <div className="border-t border-slate-100 bg-slate-50/50 px-4 py-3 sm:pl-[88px]">
                    <div className="text-[12px] text-slate-500">{file.note}</div>
                    <div className="mt-2 inline-flex items-center gap-1.5 font-mono text-[11px] text-slate-400">
                      <Search size={12} />
                      Content diff is intentionally on demand.
                    </div>
                  </div>
                ) : null}
              </div>
            )
          })
        ) : (
          <div className="px-4 py-6 text-center text-[13px] text-slate-400">
            No file changes captured in this digest.
          </div>
        )}
      </div>
    </div>
  )
}

function CountDot({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5 text-[12px]">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      <span className="text-slate-500">{label}</span>
    </div>
  )
}
