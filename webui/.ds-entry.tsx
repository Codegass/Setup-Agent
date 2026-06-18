// design-sync barrel entry — re-exports the scoped SAG design-system components.
// Bundled into window.SagUi.* by package-build.mjs. Hand-authored (not the synth
// entry) so main.tsx's render() side-effect never ships into the preview bundle,
// and so the `common/` design layer is exposed rather than the raw `ui/` shadcn
// primitives it wraps.

// Primitives (common/)
export { Badge, StatusBadge, LabeledStatus } from "@/components/common/Badge"
export { Button } from "@/components/common/Button"
export { Card, CardHead } from "@/components/common/Card"
export { Tabs } from "@/components/common/Tabs"
export { TestBar } from "@/components/common/TestBar"

// Session facets + cards
export { BuildCard } from "@/components/session/BuildCard"
export { TestCard } from "@/components/session/TestCard"
export { BuildFacet } from "@/components/session/BuildFacet"
export { TestFacet, TestConclusionCard } from "@/components/session/TestFacet"
export { FailingCard } from "@/components/session/FailingCard"
export { ModuleTable } from "@/components/session/ModuleTable"
export { BuildDetailPage } from "@/components/session/BuildDetailPage"
export { TestDetailPage } from "@/components/session/TestDetailPage"
export { ModuleBreakdownDialog } from "@/components/session/ModuleBreakdownDialog"
export { ContextTrace } from "@/components/session/ContextTrace"
export { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
export { FilesDigest } from "@/components/session/FilesDigest"
export { ReportDoc } from "@/components/session/ReportDoc"
export { LogsView } from "@/components/session/LogsView"

// Detail pane (master-detail)
export { SummaryBand } from "@/pages/detail/SummaryBand"
export { FacetTabs } from "@/pages/detail/FacetTabs"
export { DetailHeader } from "@/pages/detail/DetailHeader"
export { DetailPane } from "@/pages/detail/DetailPane"

// Workspace rail + shell
export { WorkspaceRail } from "@/pages/WorkspaceRail"
export { RailSkeleton } from "@/pages/RailSkeleton"
export { LaunchSetupsDialog } from "@/components/launch/LaunchSetupsDialog"
export { DeleteWorkspaceDialog } from "@/components/workspace/DeleteWorkspaceDialog"
export { NewTaskModal } from "@/components/workspace/NewTaskModal"
export { WorkspacePanel } from "@/components/workspace/WorkspacePanels"
export { WorkspaceSettings } from "@/components/workspace/WorkspaceSettings"
export { TerminalPanel } from "@/components/terminal/TerminalPanel"
