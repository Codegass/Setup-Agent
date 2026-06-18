import { WorkspaceSettings } from "sag-workbench"
import { detail, workspaces } from "./_fixtures"

export const Loaded = () => (
  <div style={{ width: 760 }}>
    <WorkspaceSettings latest={detail} workspace={workspaces[0]} />
  </div>
)
