import { TestBar } from "sag-workbench"

export const States = () => (
  <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
    <TestBar pass={312} fail={8} total={320} />
    <TestBar pass={3841} fail={0} total={3841} />
    <TestBar pass={0} fail={0} total={0} />
  </div>
)
