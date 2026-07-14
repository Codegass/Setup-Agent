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
      <div className="px-4 py-6 text-center text-[13px] text-muted-foreground">
        No file snapshot was captured for this execution.
      </div>
    )
  }

  const items = preview ? digest.items.slice(0, 4) : digest.items

  return (
    <div>
      {!preview ? (
        <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-2.5">
          <CountDot color="bg-status-success" label={`${digest.counts.added} added`} />
          <CountDot color="bg-status-running" label={`${digest.counts.modified} modified`} />
          <CountDot color="bg-status-failed" label={`${digest.counts.deleted} deleted`} />
          <CountDot color="bg-status-attention" label={`${digest.counts.renamed} renamed`} />
          <div className="ml-auto font-mono text-[10.5px] text-muted-foreground">
            snapshot {digest.snapshot.base} - {digest.snapshot.head} / {digest.snapshot.mode}
          </div>
        </div>
      ) : null}
      <div className="divide-y divide-border">
        {items.length ? (
          items.map((file) => {
            const open = openPath === file.path

            return (
              <div key={file.path}>
                <button
                  className={`flex w-full items-center gap-2.5 px-4 py-2.5 text-left ${
                    preview ? "" : "hover:bg-accent"
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
                    <Folder size={14} className="shrink-0 text-muted-foreground" />
                  ) : (
                    <FileText size={14} className="shrink-0 text-muted-foreground" />
                  )}
                  <span className="truncate font-mono text-[12px] text-muted-foreground">{file.path}</span>
                  <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground">
                    {file.size}
                  </span>
                  <span className="hidden shrink-0 font-mono text-[10px] text-muted-foreground sm:block">
                    {file.mtime}
                  </span>
                </button>
                {open && !preview ? (
                  <div className="border-t border-border bg-muted px-4 py-3 sm:pl-[88px]">
                    <div className="text-[12px] text-muted-foreground">{file.note}</div>
                    <div className="mt-2 inline-flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
                      <Search size={12} />
                      Content diff is intentionally on demand.
                    </div>
                  </div>
                ) : null}
              </div>
            )
          })
        ) : (
          <div className="px-4 py-6 text-center text-[13px] text-muted-foreground">
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
      <span className="text-muted-foreground">{label}</span>
    </div>
  )
}
