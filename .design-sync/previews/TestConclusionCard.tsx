import { TestConclusionCard } from "sag-workbench"
import { test } from "./_fixtures"

export const Partial = () => (
  <div style={{ width: 420 }}>
    <TestConclusionCard test={test} />
  </div>
)

export const AllPassing = () => (
  <div style={{ width: 420 }}>
    <TestConclusionCard
      test={{
        state: "success",
        pass: 320,
        fail: 0,
        skip: 0,
        total: 320,
        passRate: 100,
        uniqueTotal: 318,
        note: "surefire (commons-cli)",
      }}
    />
  </div>
)
