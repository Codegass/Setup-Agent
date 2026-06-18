import { DetailPane } from "sag-workbench"
import { detail, workspaces } from "./_fixtures"

export const Partial = () => (
  <div style={{ width: 860, height: 720 }}>
    <DetailPane
      detail={detail}
      onDelete={async () => {}}
      onSession={() => {}}
      onSubmitTask={async () => ({
        workspace_id: "sag-acme",
        session_id: "SETUP-acme-20260618-150000",
        source_session: "SETUP-acme-20260618-143409",
        status: "accepted",
      })}
      sessionId="SETUP-acme-20260618-143409"
      workspace={workspaces[0]}
    />
  </div>
)
