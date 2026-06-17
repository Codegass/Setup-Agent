import type * as React from "react"

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"

export function ModuleBreakdownDialog({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: React.ReactNode
}) {
  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[900px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="border-b border-slate-100 px-4 py-3">
          <DialogTitle className="text-[13px] font-semibold text-slate-800">{title}</DialogTitle>
        </DialogHeader>
        <div className="max-h-[72vh] overflow-auto p-4">{children}</div>
      </DialogContent>
    </Dialog>
  )
}
