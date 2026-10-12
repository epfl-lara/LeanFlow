/** Retry transient CLI checks without confusing them with a missing installation. */
import type { CliStatus } from "./types";

export class CliHealth {
  state: CliStatus = { ok: false, path: "leanflow", version: "", error: "", health: "checking" };
  private pending: { path: string; result: Promise<CliStatus> } | undefined;
  private retry: ReturnType<typeof setTimeout> | undefined;
  private generation = 0;
  private failures = 0;
  private disposed = false;

  constructor(
    private readonly probe: () => Promise<CliStatus>,
    private readonly changed: () => void,
    private readonly recovered: () => void,
  ) {}

  /** Coalesce checks and discard replies for an obsolete executable selection. */
  check(path: string): Promise<CliStatus> {
    if (this.disposed) return Promise.resolve(this.state);
    if (this.pending?.path === path) return this.pending.result;
    clearTimeout(this.retry);
    const generation = ++this.generation;
    const previous = this.state.path === path ? this.state : undefined;
    if (!previous) this.failures = 0;
    this.state = { ok: previous?.ok ?? false, path, version: previous?.version ?? "", error: "", health: "checking" };
    this.changed();
    const result = this.probe().catch(error => ({
      ok: false, path, version: "", error: error instanceof Error ? error.message : String(error), issue: "failed" as const,
    })).then(value => {
      if (this.disposed || generation !== this.generation) return this.state;
      const unavailable = value.issue === "missing" || value.issue === "permission";
      this.state = { ...value, path, version: value.version || previous?.version || "", health: value.ok ? "ready" : unavailable ? "unavailable" : "retrying" };
      this.pending = undefined;
      this.changed();
      if (value.ok) this.failures = 0;
      else if (!unavailable) {
        const delay = [5_000, 15_000, 30_000][Math.min(this.failures++, 2)];
        this.retry = setTimeout(() => {
          void this.check(path).then(state => {
            if (!this.disposed && state.ok && state.path === path) this.recovered();
          });
        }, delay);
      }
      return this.state;
    });
    this.pending = { path, result };
    return result;
  }

  /** Cancel retries and prevent late checks from updating disposed UI. */
  dispose(): void {
    this.disposed = true;
    this.generation++;
    clearTimeout(this.retry);
  }
}
