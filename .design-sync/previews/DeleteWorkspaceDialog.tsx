import { DeleteWorkspaceDialog } from "sag-workbench"
import { workspaces } from "./_fixtures"

export const Single = () => (
  <DeleteWorkspaceDialog
    target={{ workspaceId: workspaces[0].id, label: workspaces[0].project, kind: "workspace" }}
    onCancel={() => {}}
    onConfirm={async () => {}}
  />
)

export const Batch = () => (
  <DeleteWorkspaceDialog
    target={{ workspaceId: workspaces[0].id, label: workspaces[0].project, kind: "workspace" }}
    count={3}
    onCancel={() => {}}
    onConfirm={async () => {}}
  />
)
