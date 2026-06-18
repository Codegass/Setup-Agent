import { Card } from "sag-workbench"
import { FilesDigest } from "sag-workbench"
import { files } from "./_fixtures"

export const Full = () => (
  <Card style={{ overflow: "hidden", width: 720 }}>
    <FilesDigest digest={files} />
  </Card>
)

export const PreviewVariant = () => (
  <Card style={{ overflow: "hidden", width: 720 }}>
    <FilesDigest digest={files} preview />
  </Card>
)
