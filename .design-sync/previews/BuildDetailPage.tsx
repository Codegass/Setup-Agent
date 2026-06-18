import { BuildDetailPage } from "sag-workbench"
import { detail } from "./_fixtures"

export const PerModule = () => (
  <div style={{ width: 820 }}>
    <BuildDetailPage detail={detail} />
  </div>
)
