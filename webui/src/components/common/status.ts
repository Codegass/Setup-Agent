import type { Tone } from "@/api/types"

export interface StatusMeta {
  label: string
  tone: Tone
}

const statusByName: Record<string, StatusMeta> = {
  success: { label: "Success", tone: "green" },
  pass: { label: "Passed", tone: "green" },
  passed: { label: "Passed", tone: "green" },
  completed: { label: "Completed", tone: "green" },
  ready: { label: "Ready", tone: "green" },
  available: { label: "Available", tone: "green" },
  running: { label: "Running", tone: "blue" },
  launching: { label: "Launching", tone: "blue" },
  connected: { label: "Connected", tone: "blue" },
  active: { label: "Active", tone: "blue" },
  partial: { label: "Partial", tone: "amber" },
  stopped: { label: "Stopped", tone: "amber" },
  pending: { label: "Pending", tone: "neutral" },
  created: { label: "Created", tone: "neutral" },
  queued: { label: "Queued", tone: "neutral" },
  info: { label: "Info", tone: "neutral" },
  none: { label: "—", tone: "neutral" },
  skipped: { label: "Skipped", tone: "neutral" },
  exited: { label: "Exited", tone: "red" },
  failure: { label: "Failure", tone: "red" },
  failed: { label: "Failed", tone: "red" },
  fail: { label: "Failed", tone: "red" },
  blocked: { label: "Blocked", tone: "red" },
}

function fallbackLabel(status: string): string {
  if (!status) {
    return "Unknown"
  }

  return status.charAt(0).toUpperCase() + status.slice(1)
}

export function statusMeta(status: string): StatusMeta {
  const normalized = status.trim().toLowerCase()

  return statusByName[normalized] ?? {
    label: fallbackLabel(normalized),
    tone: "neutral",
  }
}
