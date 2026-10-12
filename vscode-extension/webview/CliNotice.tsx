/** Explain connection failures without claiming a timed-out CLI needs installation. */
import type { CliStatus } from "../src/core/types";
import { Notice } from "./components";
import { post } from "./vscodeApi";

export function CliNotice({ cli, compact = false }: { cli: CliStatus | undefined; compact?: boolean }) {
  if (!cli || cli.ok) return null;
  if (cli.health === "checking") return <Notice tone="info">Checking LeanFlow…</Notice>;
  const setupRequired = cli.health === "unavailable" || !cli.health;
  return <Notice tone={setupRequired ? "error" : "warn"}>
    <strong>{setupRequired ? "LeanFlow CLI setup is required" : "LeanFlow connection interrupted"}</strong>
    <div style={{ marginTop: 4 }}>{cli.error}</div>
    {!setupRequired && <div style={{ marginTop: 4 }}>Retrying automatically. Existing jobs and saved logs remain available.</div>}
    <div className="row tight" style={{ marginTop: 8 }}>
      {setupRequired && <>
        <button className="btn" onClick={() => post({ type: "openExternalDoc", topic: "install" })}>{compact ? "Install guide" : "Open install guide"}</button>
        <button className="btn ghost" onClick={() => post({ type: "openCliSettings" })}>{compact ? "CLI path" : "Configure CLI path"}</button>
      </>}
      <button className="btn ghost" onClick={() => post({ type: "refresh" })}>Check again</button>
    </div>
  </Notice>;
}
