import { WorkspacePanel } from "sag-workbench"
import { detail, workspaces } from "./_fixtures"

export const TerminalKind = () => (
  <WorkspacePanel
    kind="terminal"
    workspace={workspaces[0]}
    latest={detail}
    onClose={() => {}}
  />
)

export const SettingsKind = () => (
  <WorkspacePanel
    kind="settings"
    workspace={workspaces[0]}
    latest={detail}
    onClose={() => {}}
  />
)
