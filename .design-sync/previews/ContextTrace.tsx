import { ContextTrace } from "sag-workbench"
import { context } from "./_fixtures"

export const Trace = () => (
  <div style={{ width: 760 }}>
    <ContextTrace ctx={context} />
  </div>
)

export const PreviewVariant = () => (
  <div style={{ width: 760 }}>
    <ContextTrace ctx={context} preview />
  </div>
)
