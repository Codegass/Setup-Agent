import { StatusBadge } from "sag-workbench"

export const Statuses = () => (
  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
    <StatusBadge status="running" />
    <StatusBadge status="success" />
    <StatusBadge status="failed" />
    <StatusBadge status="blocked" />
    <StatusBadge status="idle" />
  </div>
)

export const WithoutDot = () => (
  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
    <StatusBadge status="success" dot={false} />
    <StatusBadge status="failed" dot={false} />
  </div>
)
