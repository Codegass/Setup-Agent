import { LogsView } from "sag-workbench"
import { logs } from "./_fixtures"

export const Default = () => (
  <div style={{ width: 760 }}>
    <LogsView logs={logs} />
  </div>
)

export const Empty = () => (
  <div style={{ width: 760 }}>
    <LogsView logs={[]} />
  </div>
)
