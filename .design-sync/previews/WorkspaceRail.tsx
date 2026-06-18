import { WorkspaceRail } from "sag-workbench"
import { workspaces } from "./_fixtures"

const data = {
  docker: { status: "running", version: "27.1.1" },
  workspaces,
}

export const Populated = () => (
  <div style={{ width: 320, height: 720 }}>
    <WorkspaceRail
      data={data}
      lastUpdatedAt={Date.now() - 120000}
      onLaunchSetups={() => {}}
      onSelect={() => {}}
      selectedId="sag-acme"
    />
  </div>
)

export const Empty = () => (
  <div style={{ width: 320, height: 720 }}>
    <WorkspaceRail
      data={{ docker: { status: "running", version: "27.1.1" }, workspaces: [] }}
      lastUpdatedAt={Date.now() - 5000}
      onLaunchSetups={() => {}}
      onSelect={() => {}}
      selectedId={null}
    />
  </div>
)
