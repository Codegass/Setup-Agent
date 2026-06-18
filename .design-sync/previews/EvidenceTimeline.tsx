import { Card } from "sag-workbench"
import { EvidenceTimeline } from "sag-workbench"
import { evidence } from "./_fixtures"

export const Expanded = () => (
  <Card style={{ overflow: "hidden", width: 720 }}>
    <EvidenceTimeline groups={evidence} />
  </Card>
)

export const PreviewVariant = () => (
  <Card style={{ overflow: "hidden", width: 720 }}>
    <EvidenceTimeline groups={evidence} preview />
  </Card>
)
