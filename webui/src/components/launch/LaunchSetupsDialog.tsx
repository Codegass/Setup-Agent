import { useState } from "react"
import type { ClipboardEvent } from "react"
import { Info, Plus, Rocket, X } from "lucide-react"

import type { LaunchBatchRequestBody, LaunchBatchResult } from "@/api/types"
import { Button } from "@/components/common/Button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

import { emptyLaunchRow, parsePastedRepoLines, type LaunchRowDraft } from "./launchRows"

interface LaunchSetupsDialogProps {
  defaultConcurrency: number
  onClose: () => void
  onSubmit: (payload: LaunchBatchRequestBody) => Promise<LaunchBatchResult>
  onSubmitted: (result: LaunchBatchResult) => void
}

const cellClass =
  "w-full rounded-md border border-slate-200 px-2 py-1.5 font-mono text-[12px] text-slate-700 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"

const VERSION_HELP =
  "Branch, release tag, or commit hash (short or full), e.g. rel/commons-cli-1.11.0 or 1a2b3c4. Leave empty for the default branch."

function isRowEmpty(row: LaunchRowDraft): boolean {
  return (
    !row.repoUrl.trim() &&
    !row.name.trim() &&
    !row.ref.trim() &&
    !row.goal.trim() &&
    !row.record
  )
}

export function LaunchSetupsDialog({
  defaultConcurrency,
  onClose,
  onSubmit,
  onSubmitted,
}: LaunchSetupsDialogProps) {
  const [rows, setRows] = useState<LaunchRowDraft[]>([emptyLaunchRow()])
  const [concurrency, setConcurrency] = useState(String(defaultConcurrency))
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const updateRow = (index: number, patch: Partial<LaunchRowDraft>) => {
    setRows((current) =>
      current.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)),
    )
  }

  const addRow = () => setRows((current) => [...current, emptyLaunchRow()])

  const removeRow = (index: number) => {
    setRows((current) => {
      const next = current.filter((_, rowIndex) => rowIndex !== index)
      return next.length ? next : [emptyLaunchRow()]
    })
    setRowErrors({})
  }

  const handleRepoPaste = (index: number, event: ClipboardEvent<HTMLInputElement>) => {
    const text = event.clipboardData.getData("text")
    if (!/\s/.test(text.trim())) {
      // A plain URL: let the native paste handle it.
      return
    }
    event.preventDefault()
    const parsed = parsePastedRepoLines(text)
    if (!parsed.length) {
      return
    }
    setRows((current) => {
      const next = [...current]
      const [first, ...rest] = parsed
      next[index] = { ...next[index], repoUrl: first.repoUrl, ref: first.ref || next[index].ref }
      const extra = rest.map((line) => ({
        ...emptyLaunchRow(),
        repoUrl: line.repoUrl,
        ref: line.ref,
      }))
      next.splice(index + 1, 0, ...extra)
      return next
    })
  }

  const handleSubmit = async () => {
    setFormError(null)
    setRowErrors({})

    const parsedConcurrency = Number(concurrency)
    if (!Number.isInteger(parsedConcurrency) || parsedConcurrency < 1) {
      setFormError("Concurrency must be a whole number of 1 or more.")
      return
    }

    const submittedIndexes: number[] = []
    const errors: Record<number, string> = {}
    rows.forEach((row, index) => {
      if (isRowEmpty(row)) {
        return
      }
      if (!row.repoUrl.trim()) {
        errors[index] = "Repository URL is required."
        return
      }
      submittedIndexes.push(index)
    })

    if (Object.keys(errors).length) {
      setRowErrors(errors)
      return
    }
    if (!submittedIndexes.length) {
      setFormError("Add at least one repository URL.")
      return
    }

    const payload: LaunchBatchRequestBody = {
      concurrency: parsedConcurrency,
      projects: submittedIndexes.map((index) => {
        const row = rows[index]
        return {
          repo_url: row.repoUrl.trim(),
          name: row.name.trim() || null,
          ref: row.ref.trim() || null,
          goal: row.goal.trim() || null,
          record: row.record,
        }
      }),
    }

    setSubmitting(true)
    try {
      const result = await onSubmit(payload)
      if (result.status === 409) {
        const conflictErrors: Record<number, string> = {}
        for (const rejection of result.rejected) {
          const rowIndex = submittedIndexes[rejection.row_index]
          if (rowIndex !== undefined) {
            conflictErrors[rowIndex] = rejection.message
          }
        }
        setRowErrors(conflictErrors)
        setFormError("No rows were launched.")
        return
      }
      onSubmitted(result)
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !submitting) {
          onClose()
        }
      }}
    >
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[920px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="border-b border-slate-100 px-4 py-3">
          <DialogTitle>Launch setups</DialogTitle>
          <DialogDescription>
            One row per repository. Each accepted row runs sag project in its own
            process. Paste multiple lines (repo URL, optionally followed by a
            version) into a repository cell to fill the grid.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={(event) => { event.preventDefault(); void handleSubmit() }}>
        <div className="max-h-[60vh] overflow-y-auto p-4">
          <div className="flex items-center gap-2">
            <label
              className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500"
              htmlFor="launch-concurrency"
            >
              Concurrency
            </label>
            <input
              aria-label="Concurrency"
              className={`${cellClass} w-20`}
              id="launch-concurrency"
              min={1}
              onChange={(event) => setConcurrency(event.target.value)}
              type="number"
              value={concurrency}
            />
            <span className="text-[11px] text-slate-500">
              parallel setups for this batch
            </span>
          </div>

          <div className="mt-3 grid grid-cols-[2.2fr_1fr_1.2fr_1.6fr_56px_36px] items-center gap-2">
            {["Repo URL", "Name", "Version", "Goal", "Record", ""].map((header) => (
              <div
                key={header || "actions"}
                className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500"
              >
                {header}
                {header === "Version" ? (
                  <span
                    aria-label="Version help"
                    className="group relative inline-flex cursor-help text-slate-300 hover:text-slate-500 focus-visible:text-slate-500 focus-visible:outline-none"
                    tabIndex={0}
                  >
                    <Info aria-hidden="true" size={12} />
                    <span
                      className="pointer-events-none absolute left-1/2 top-full z-50 mt-1.5 hidden w-64 -translate-x-1/2 whitespace-normal rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-left font-sans text-[11px] font-normal normal-case tracking-normal text-slate-600 shadow-md group-hover:block group-focus-visible:block"
                      role="tooltip"
                    >
                      {VERSION_HELP}
                    </span>
                  </span>
                ) : null}
              </div>
            ))}
            {rows.map((row, index) => (
              <RowCells
                key={index}
                error={rowErrors[index]}
                index={index}
                onChange={(patch) => updateRow(index, patch)}
                onRemove={() => removeRow(index)}
                onRepoPaste={(event) => handleRepoPaste(index, event)}
                row={row}
              />
            ))}
          </div>

          <Button
            className="mt-3"
            onClick={addRow}
            size="sm"
            type="button"
            variant="outline"
          >
            <Plus size={13} />
            Add row
          </Button>

          {formError ? (
            <div className="mt-3 text-[12px] text-red-600">{formError}</div>
          ) : null}
        </div>

        <DialogFooter className="gap-2 border-t border-slate-100 px-4 py-3 sm:space-x-0">
          <Button disabled={submitting} onClick={onClose} type="button" variant="outline">
            Cancel
          </Button>
          <Button disabled={submitting} type="submit">
            <Rocket size={13} />
            {submitting ? "Launching" : "Launch setups"}
          </Button>
        </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function RowCells({
  row,
  index,
  error,
  onChange,
  onRemove,
  onRepoPaste,
}: {
  row: LaunchRowDraft
  index: number
  error?: string
  onChange: (patch: Partial<LaunchRowDraft>) => void
  onRemove: () => void
  onRepoPaste: (event: ClipboardEvent<HTMLInputElement>) => void
}) {
  const rowLabel = index + 1

  return (
    <>
      <input
        aria-label={`Repository URL row ${rowLabel}`}
        autoFocus={index === 0}
        className={cellClass}
        onChange={(event) => onChange({ repoUrl: event.target.value })}
        onPaste={onRepoPaste}
        placeholder="https://github.com/owner/repo.git"
        value={row.repoUrl}
      />
      <input
        aria-label={`Name row ${rowLabel}`}
        className={cellClass}
        onChange={(event) => onChange({ name: event.target.value })}
        placeholder="optional"
        value={row.name}
      />
      <input
        aria-label={`Version row ${rowLabel}`}
        className={cellClass}
        onChange={(event) => onChange({ ref: event.target.value })}
        placeholder="branch, tag, or commit"
        title={VERSION_HELP}
        value={row.ref}
      />
      <input
        aria-label={`Goal row ${rowLabel}`}
        className={cellClass}
        onChange={(event) => onChange({ goal: event.target.value })}
        placeholder="optional"
        value={row.goal}
      />
      <div className="flex justify-center">
        <input
          aria-label={`Record row ${rowLabel}`}
          checked={row.record}
          className="h-4 w-4 accent-blue-600"
          onChange={(event) => onChange({ record: event.target.checked })}
          type="checkbox"
        />
      </div>
      <button
        aria-label={`Remove row ${rowLabel}`}
        className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
        onClick={onRemove}
        type="button"
      >
        <X size={14} />
      </button>
      {error ? (
        <div className="col-span-6 -mt-1 text-[12px] text-red-600">{error}</div>
      ) : null}
    </>
  )
}
