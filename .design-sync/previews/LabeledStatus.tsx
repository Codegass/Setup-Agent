import { LabeledStatus } from "sag-workbench"

export const Default = () => (
  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
    <LabeledStatus label="Build" status="success" />
    <LabeledStatus label="Tests" status="failed" />
    <LabeledStatus label="Report" status="running" />
  </div>
)
