import { useEffect, useRef, useState } from "react"
import { Ban, CheckCircle2, CircleDashed, Loader2 } from "lucide-react"

import {
  fetchPhaseHistory as defaultFetchPhaseHistory,
  fetchPhaseJournal as defaultFetchPhaseJournal,
  fetchPhases as defaultFetchPhases,
  type PhaseHistoryEntry,
  type PhaseHistoryResponse,
  type PhaseJournalRecord,
  type PhaseJournalResponse,
  type PhaseSummary,
  type PhaseWindowMeta,
} from "@/api/client"

interface PhaseTimelineProps {
  workspaceId: string
  // Fetchers are injected for testability; defaults hit the real API.
  fetchPhases?: (workspaceId: string) => Promise<PhaseSummary[]>
  fetchPhaseJournal?: (workspaceId: string, phase: string) => Promise<PhaseJournalResponse>
  fetchPhaseHistory?: (workspaceId: string, phase: string) => Promise<PhaseHistoryResponse>
}

const BLOCKED_STATUSES = new Set(["failed", "blocked"])

function StatusIcon({ status }: { status: string }) {
  if (status === "completed") {
    return <CheckCircle2 aria-label="done" size={14} className="shrink-0 text-emerald-600" />
  }
  if (BLOCKED_STATUSES.has(status)) {
    return <Ban aria-label="blocked" size={14} className="shrink-0 text-red-600" />
  }
  if (status === "in_progress") {
    return (
      <Loader2 aria-label="running" size={14} className="shrink-0 animate-spin text-sky-600" />
    )
  }
  return <CircleDashed aria-label="pending" size={14} className="shrink-0 text-slate-400" />
}

/** One journal record as a single text line: iter, window size, delta, markers. */
export function journalLine(record: PhaseJournalRecord): string {
  const added = record.delta?.added ?? 0
  const compacted = record.delta?.compacted ?? 0

  let line = `iter ${record.iteration} · ${record.total_chars} chars · +${added}`
  if (compacted > 0) {
    line += ` · compacted=${compacted}`
  }
  if (record.step_span != null) {
    line += ` · span=${record.step_span}`
  }
  if (record.intro_text) {
    line += " · INTRO"
  }
  if (record.ledger_text) {
    line += " · LEDGER"
  }
  return line
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function formatParameters(parameters: unknown): string {
  if (parameters == null) return ""
  try {
    return JSON.stringify(parameters, null, 2)
  } catch {
    return String(parameters)
  }
}

function historyTitle(entry: PhaseHistoryEntry, index: number): string {
  const type = entry.type ?? "entry"
  if (type === "action") {
    const status = entry.success ? "ok" : "failed"
    return `${index}. action · ${entry.tool_name ?? "tool"} · ${status}`
  }
  return `${index}. ${type}`
}

function windowMetaLabel(kind: string, count: number, meta: PhaseWindowMeta | null): string {
  if (!meta) return ""
  const label = meta.truncated ? "showing" : "loaded"
  return `${label} ${count} of ${meta.total} ${kind}`
}

export function PhaseTimeline({
  workspaceId,
  fetchPhases = defaultFetchPhases,
  fetchPhaseJournal = defaultFetchPhaseJournal,
  fetchPhaseHistory = defaultFetchPhaseHistory,
}: PhaseTimelineProps) {
  const [phases, setPhases] = useState<PhaseSummary[] | null>(null)
  const [phasesError, setPhasesError] = useState<string | null>(null)
  const [selectedPhase, setSelectedPhase] = useState<string | null>(null)
  const [records, setRecords] = useState<PhaseJournalRecord[] | null>(null)
  const [journalMeta, setJournalMeta] = useState<PhaseWindowMeta | null>(null)
  const [journalError, setJournalError] = useState<string | null>(null)
  const [journalLoading, setJournalLoading] = useState(false)
  const [historyEntries, setHistoryEntries] = useState<PhaseHistoryEntry[] | null>(null)
  const [historyMeta, setHistoryMeta] = useState<PhaseWindowMeta | null>(null)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  // Monotonic token so a slow journal response cannot clobber a newer click.
  const journalRequest = useRef(0)

  useEffect(() => {
    let cancelled = false
    setPhases(null)
    setPhasesError(null)

    fetchPhases(workspaceId)
      .then((items) => {
        if (!cancelled) setPhases(items)
      })
      .catch((err) => {
        if (!cancelled) setPhasesError(errorMessage(err))
      })

    return () => {
      cancelled = true
    }
  }, [workspaceId, fetchPhases])

  const selectPhase = (phase: string) => {
    const token = ++journalRequest.current
    setSelectedPhase(phase)
    setRecords(null)
    setJournalMeta(null)
    setHistoryEntries(null)
    setHistoryMeta(null)
    setJournalError(null)
    setHistoryError(null)
    setJournalLoading(true)
    setHistoryLoading(true)

    fetchPhaseJournal(workspaceId, phase)
      .then((payload) => {
        if (journalRequest.current !== token) return
        setRecords(payload.records)
        setJournalMeta(payload)
        setJournalLoading(false)
      })
      .catch((err) => {
        if (journalRequest.current !== token) return
        setJournalError(errorMessage(err))
        setJournalLoading(false)
      })

    fetchPhaseHistory(workspaceId, phase)
      .then((payload) => {
        if (journalRequest.current !== token) return
        setHistoryEntries(payload.entries)
        setHistoryMeta(payload)
        setHistoryLoading(false)
      })
      .catch((err) => {
        if (journalRequest.current !== token) return
        setHistoryError(errorMessage(err))
        setHistoryLoading(false)
      })
  }

  return (
    <div className="space-y-3">
      <h3 className="text-[13px] font-semibold text-slate-800">Phase timeline</h3>

      {phasesError ? (
        <div className="text-[12px] text-red-600">{phasesError}</div>
      ) : phases === null ? (
        <div className="text-[12px] text-slate-500">Loading phases…</div>
      ) : (
        <ul className="space-y-1">
          {phases.map((phase) => {
            const blocked = BLOCKED_STATUSES.has(phase.status)
            return (
              <li key={phase.name}>
                <button
                  type="button"
                  aria-pressed={selectedPhase === phase.name}
                  onClick={() => selectPhase(phase.name)}
                  className="flex w-full items-center gap-2 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-left text-[12.5px] hover:bg-slate-50 aria-pressed:border-slate-400"
                >
                  <StatusIcon status={phase.status} />
                  <span className="font-medium text-slate-800">{phase.name}</span>
                  {phase.key_results ? (
                    <span className="truncate text-[11.5px] text-slate-500">
                      {phase.key_results}
                    </span>
                  ) : null}
                </button>
                {blocked && phase.notes ? (
                  <div className="mt-0.5 pl-7 text-[11.5px] text-red-600">{phase.notes}</div>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}

      {selectedPhase ? (
        <div className="space-y-3 rounded-md border border-slate-200 bg-white p-3">
          <h4 className="font-mono text-[11px] uppercase tracking-wide text-slate-500">
            phase detail · {selectedPhase}
          </h4>

          <section className="space-y-1.5">
            <div className="flex items-baseline justify-between gap-3">
              <div className="text-[12px] font-medium text-slate-700">Context journal</div>
              {records !== null && journalMeta ? (
                <div className="font-mono text-[10.5px] uppercase tracking-wide text-slate-400">
                  {windowMetaLabel("records", records.length, journalMeta)}
                </div>
              ) : null}
            </div>
            {journalLoading ? (
              <div className="text-[12px] text-slate-500">Loading journal…</div>
            ) : null}
            {journalError ? <div className="text-[12px] text-red-600">{journalError}</div> : null}
            {records !== null && records.length === 0 ? (
              <div className="text-[12px] text-slate-500">No journal records.</div>
            ) : null}
            {records !== null && records.length > 0 ? (
              <ol className="space-y-1.5">
                {records.map((record) => (
                  <li
                    key={record.iteration}
                    className="rounded border border-slate-200 bg-slate-50 px-2 py-1.5"
                  >
                    <div className="font-mono text-[11px] leading-relaxed text-slate-600">
                      {journalLine(record)}
                    </div>
                    {record.intro_text ? (
                      <pre className="mt-1 max-h-44 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 font-mono text-[11px] leading-relaxed text-slate-700">
                        {record.intro_text}
                      </pre>
                    ) : null}
                    {record.ledger_text ? (
                      <pre className="mt-1 max-h-44 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 font-mono text-[11px] leading-relaxed text-slate-700">
                        {record.ledger_text}
                      </pre>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : null}
          </section>

          <section className="space-y-1.5">
            <div className="flex items-baseline justify-between gap-3">
              <div className="text-[12px] font-medium text-slate-700">Branch history</div>
              {historyEntries !== null && historyMeta ? (
                <div className="font-mono text-[10.5px] uppercase tracking-wide text-slate-400">
                  {windowMetaLabel("entries", historyEntries.length, historyMeta)}
                </div>
              ) : null}
            </div>
            {historyLoading ? (
              <div className="text-[12px] text-slate-500">Loading history…</div>
            ) : null}
            {historyError ? <div className="text-[12px] text-red-600">{historyError}</div> : null}
            {historyEntries !== null && historyEntries.length === 0 ? (
              <div className="text-[12px] text-slate-500">No branch history entries.</div>
            ) : null}
            {historyEntries !== null && historyEntries.length > 0 ? (
              <ol className="space-y-1.5">
                {historyEntries.map((entry, index) => {
                  const parameters = formatParameters(entry.parameters)
                  return (
                    <li
                      key={`${entry.type ?? "entry"}-${index}`}
                      className="rounded border border-slate-200 bg-slate-50 px-2 py-1.5"
                    >
                      <div className="font-mono text-[11px] uppercase tracking-wide text-slate-500">
                        {historyTitle(entry, index + 1)}
                      </div>
                      {entry.content ? (
                        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-slate-700">
                          {entry.content}
                        </pre>
                      ) : null}
                      {parameters ? (
                        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 font-mono text-[11px] leading-relaxed text-slate-700">
                          {parameters}
                        </pre>
                      ) : null}
                      {entry.output ? (
                        <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 font-mono text-[11px] leading-relaxed text-slate-700">
                          {entry.output}
                        </pre>
                      ) : null}
                    </li>
                  )
                })}
              </ol>
            ) : null}
          </section>
        </div>
      ) : null}
    </div>
  )
}
