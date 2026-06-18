import { FailingCard } from "sag-workbench"
import { failingNames } from "./_fixtures"

export const Failing = () => (
  <div style={{ width: 420 }}>
    <FailingCard names={failingNames} />
  </div>
)
