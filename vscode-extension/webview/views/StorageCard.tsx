/** Explain the global cache policy and expose safe host-side collection. */
import { Card, Notice } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";

const gib = (bytes: number): string => `${(bytes / 1024 ** 3).toFixed(1)} GiB`;

export function StorageCard() {
  const storage = useStore().app?.storage;
  return <Card title="Shared dependencies and storage" subtitle="Compatible projects reuse sealed dependencies through private writable filesystem copies.">
    {storage ? <>
      <p>Managed cache and runtimes: <strong>{gib(storage.managedBytes)}</strong> · Protected by active work: {gib(storage.pinnedBytes)} · Agent dependency views: {gib(storage.workspaceBytes)}</p>
      <p>Free disk: <strong>{gib(storage.freeBytes)}</strong> · Cache target: {gib(storage.cacheTargetBytes)} · Free-space reserve: {gib(storage.minFreeBytes)}</p>
      <p className="muted">Automatic cleanup is {storage.automaticCleanup ? "on" : "off"}. Active agents, proofs, results, checkpoints and logs are retained. The target can be exceeded while storage is in use. Tree-size estimates may count shared blocks more than once. Change defaults with <code>leanflow config set storage.cache_target_gib 20</code> and <code>leanflow config set storage.min_free_gib 10</code>.</p>
      {storage.error && <Notice tone="error">{storage.error}</Notice>}
    </> : <p className="muted">Refresh to inspect managed storage. Requires the updated LeanFlow CLI.</p>}
    <div className="row">
      <button className="btn ghost" disabled={storage?.checking} onClick={() => post({ type: "refreshStorage" })}>Refresh storage</button>
      <button className="btn secondary" disabled={!storage || storage.checking} onClick={() => post({ type: "cleanStorage" })}>Clean unused storage</button>
    </div>
  </Card>;
}
