import { Badge } from "sag-workbench"

export const Tones = () => (
  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
    <Badge tone="neutral">idle</Badge>
    <Badge tone="blue">running</Badge>
    <Badge tone="green">passed</Badge>
    <Badge tone="red">failed</Badge>
    <Badge tone="amber">attention</Badge>
  </div>
)

export const Mono = () => (
  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
    <Badge tone="green" mono>
      97.5% pass
    </Badge>
    <Badge tone="blue" mono>
      maven
    </Badge>
    <Badge tone="neutral" mono>
      24 modules
    </Badge>
  </div>
)
