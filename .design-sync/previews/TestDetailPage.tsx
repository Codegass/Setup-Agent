import { TestDetailPage } from "sag-workbench"
import { detail } from "./_fixtures"

export const PerModule = () => (
  <div style={{ width: 820 }}>
    <TestDetailPage detail={detail} />
  </div>
)
