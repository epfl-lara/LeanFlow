/** Read a controller-authorized artifact through one descriptor and verify its frozen bytes. */
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { FormalizationArtifact } from "./formalization";

const MAX_ARTIFACT_BYTES = 64 * 1024 * 1024;

/** Return frozen artifact bytes or a visible stale/missing error, never an unchecked file. */
export async function readFormalizationArtifact(artifact: FormalizationArtifact): Promise<Buffer> {
  if (!path.isAbsolute(artifact.path)) throw new Error("The controller did not resolve this artifact on the owning host.");
  const handle = await fs.promises.open(artifact.path, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  try {
    const stat = await handle.stat();
    if (!stat.isFile()) throw new Error("The source artifact is not a regular file.");
    if (stat.size > MAX_ARTIFACT_BYTES) throw new Error("This artifact exceeds the editor's 64 MiB preview limit. Inspect it on the owning host.");
    const bytes = await handle.readFile();
    if (bytes.length > MAX_ARTIFACT_BYTES) throw new Error("The artifact changed while it was opened.");
    if (crypto.createHash("sha256").update(bytes).digest("hex") !== artifact.sha256.replace(/^sha256:/, "")) {
      throw new Error("This source artifact is stale: its bytes no longer match the selected revision. Refresh the inventory.");
    }
    return bytes;
  } finally {
    await handle.close();
  }
}
