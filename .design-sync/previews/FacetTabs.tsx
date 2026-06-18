import { Activity, Box, FileText, Layers, Sparkles, Terminal } from "lucide-react"

import { FacetTabs } from "sag-workbench"

// Mirrors buildDetailFacets(detail) for the acme-platform PARTIAL story: 7 failing
// tests surface as a red count on the Test tab, evidence + files carry neutral counts.
const facets = [
  { id: "build", label: "Build", icon: Box, count: null, countTone: "neutral" },
  { id: "test", label: "Test", icon: Activity, count: 7, countTone: "red" },
  { id: "flow", label: "Flow", icon: Layers, count: null, countTone: "neutral" },
  { id: "evidence", label: "Evidence", icon: Sparkles, count: 2, countTone: "neutral" },
  { id: "files", label: "Files", icon: FileText, count: 5, countTone: "neutral" },
  { id: "report", label: "Report", icon: FileText, count: null, countTone: "neutral" },
  { id: "logs", label: "Logs", icon: Terminal, count: null, countTone: "neutral" },
] as const

export const TestActive = () => (
  <div style={{ width: 720 }}>
    <FacetTabs active="test" facets={[...facets]} onJump={() => {}} />
  </div>
)

export const BuildActive = () => (
  <div style={{ width: 720 }}>
    <FacetTabs active="build" facets={[...facets]} onJump={() => {}} />
  </div>
)
