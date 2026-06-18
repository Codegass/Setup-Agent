import { BuildCard } from "sag-workbench"
import { build } from "./_fixtures"

export const Verified = () => (
  <div style={{ width: 360 }}>
    <BuildCard build={build} onOpenDetail={() => {}} />
  </div>
)

export const NoArtifacts = () => (
  <div style={{ width: 360 }}>
    <BuildCard
      build={{ state: "failed", tool: "maven", system: "maven", time: "49.3s", note: "mvn compile", classCount: 0, jarCount: 0 }}
      onOpenDetail={() => {}}
    />
  </div>
)
