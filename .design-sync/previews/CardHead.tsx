import { Card, CardHead } from "sag-workbench"

export const Default = () => (
  <Card style={{ width: 340 }}>
    <CardHead
      title="Build"
      sub="maven · 49.3s"
      right={
        <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, color: "#dc2626" }}>FAILED</span>
      }
    />
    <div style={{ padding: 16, fontFamily: "ui-monospace, monospace", fontSize: 12, color: "#64748b" }}>
      mvn compile
    </div>
  </Card>
)
