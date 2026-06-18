import { NewTaskModal } from "sag-workbench"
import { workspaces } from "./_fixtures"

export const Blank = () => (
  <NewTaskModal
    workspace={workspaces[0]}
    onClose={() => {}}
    onSubmit={async () => {}}
  />
)

export const FromSession = () => (
  <NewTaskModal
    workspace={workspaces[0]}
    sourceSession="SETUP-acme-20260618-143409"
    onClose={() => {}}
    onSubmit={async () => {}}
  />
)
