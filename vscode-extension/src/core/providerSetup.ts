/** Coordinate native setup commands and secure prompts on the extension host. */
import * as vscode from "vscode";
import { runCli } from "./cli";
import { BACKEND_IDS, ProviderCredentials, validateApiKey, validateEndpoint, type BackendId, type BackendSetup, type ProviderAction, type ProviderSetupState } from "./providerCredentials";
import { redactSensitiveText } from "./runPrivacy";
import { normalizeModelCatalog } from "./providerModelMetadata";

export class ProviderSetup implements vscode.Disposable {
  readonly credentials: ProviderCredentials;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;
  private pending: Promise<void> | null = null;
  private acting = false;
  private readonly secretsChanged: vscode.Disposable;
  private snapshot: ProviderSetupState;

  constructor(
    context: vscode.ExtensionContext,
    private readonly projectRoot: () => string | undefined = () => vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
  ) {
    const host = vscode.workspace.workspaceFolders?.[0]?.uri.authority || "local";
    this.credentials = new ProviderCredentials(context.secrets, host);
    this.snapshot = { selected: "", backends: [], checking: false, error: "", host };
    this.secretsChanged = context.secrets.onDidChange(() => { void this.refresh(); });
  }

  dispose(): void { this.secretsChanged.dispose(); this.changed.dispose(); }
  state(): ProviderSetupState { return this.snapshot; }

  /** Check CLI presence and native login only; this never spends model tokens. */
  refresh(): Promise<void> {
    if (this.pending) return this.pending;
    this.pending = this.check().finally(() => { this.pending = null; });
    return this.pending;
  }

  private async check(): Promise<void> {
    this.snapshot = { ...this.snapshot, checking: true, error: "" };
    this.changed.fire();
    try {
      const env = await this.credentials.environment();
      const cwd = this.projectRoot();
      const raw = JSON.parse(await runCli(["provider", "--json", "--check"], { env, cwd, timeoutMs: 65_000 })) as { selected: string; backends: BackendSetup[]; model_override?: string; reasoning_effort_override?: string };
      if (!Array.isArray(raw.backends) || raw.backends.length !== 3 || !BACKEND_IDS.every(id => raw.backends.some(row => row.id === id))) throw new Error("The installed CLI returned an unsupported provider setup response.");
      const text = (value: unknown): string => {
        let safe = typeof value === "string" ? value : "";
        for (const [name, secret] of Object.entries(env)) {
          if (name.endsWith("_API_KEY")) safe = safe.split(secret).join("[REDACTED]");
        }
        return redactSensitiveText(safe);
      };
      const backends = await Promise.all(raw.backends.map(async row => ({
        id: row.id, installed: row.installed === true, executable: text(row.executable),
        version: text(row.version), auth: row.auth, key_env: text(row.key_env),
        config_path: text(row.config_path), endpoint: text(row.endpoint), checked: row.checked === true,
        model_catalog: normalizeModelCatalog(row.model_catalog, text),
        ...await this.credentials.presence(row.id),
      })));
      this.snapshot = { ...this.snapshot, selected: raw.selected, backends, model_override: text(raw.model_override), reasoning_effort_override: text(raw.reasoning_effort_override) };
    } catch {
      this.snapshot = { ...this.snapshot, backends: [], model_override: "", reasoning_effort_override: "", error: "Provider setup is unavailable. Update LeanFlow or check the CLI path in Settings, then refresh." };
    } finally {
      this.snapshot = { ...this.snapshot, checking: false };
      this.changed.fire();
    }
  }

  /** Keep secret entry out of webview messages, settings, command lines and history. */
  async act(backend: BackendId, action: ProviderAction): Promise<void> {
    if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before configuring native agents.");
    if (this.acting) return;
    this.acting = true;
    try {
      if (action === "apiKey") {
        const value = await vscode.window.showInputBox({ title: `${backend}: API key`, prompt: "Stored in VS Code secret storage for this host. Used for new LeanFlow runs; native login files remain unchanged.", password: true, ignoreFocusOut: true, validateInput: validateApiKey });
        if (value !== undefined) await this.credentials.saveKey(backend, value);
      } else if (action === "removeKey") {
        await this.credentials.removeKey(backend);
        void vscode.window.showInformationMessage(`${backend}: saved key removed. New runs use the inherited environment or native login.`);
      } else if (action === "endpoint" && backend === "lemex") {
        const env = await this.credentials.environment("lemex");
        const value = await vscode.window.showInputBox({ title: "Lemex endpoint", prompt: "Leave empty to use Lemex's own endpoint settings. Takes effect for new runs.", value: env.LEMEX_BASE_URL || "", placeHolder: "https://inference.rcp.epfl.ch/v1", ignoreFocusOut: true, validateInput: validateEndpoint });
        if (value !== undefined) await this.credentials.saveEndpoint(value);
      } else {
        // Resolve again, since native setup may have changed since the last refresh.
        await this.refresh();
        const row = this.snapshot.backends.find(row => row.id === backend);
        if (!row) throw new Error("Refresh provider setup after updating the LeanFlow CLI.");
        if (action === "login" && backend !== "lemex") {
          if (!row.installed) throw new Error(`Install ${backend} on this host, then refresh provider setup.`);
          const terminal = vscode.window.createTerminal({ name: `LeanFlow: ${backend} login`, shellPath: row.executable, shellArgs: backend === "claude" ? ["auth", "login"] : ["login"], env: { OPENAI_API_KEY: null, CODEX_API_KEY: null, ANTHROPIC_API_KEY: null }, message: "Complete the native login, then refresh LeanFlow's Providers page. Saved API keys take precedence until removed." });
          terminal.show();
        } else if (action === "config") {
          const uri = vscode.Uri.file(row.config_path);
          try { await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri)); }
          catch { throw new Error("No native config file exists yet. Run the native CLI setup first; login and saved keys do not require editing this file."); }
        }
      }
      if (this.pending) await this.pending;
      await this.refresh();
    } catch (error) {
      // Storage failures must never echo a value supplied to the vault.
      if (["apiKey", "removeKey", "endpoint"].includes(action)) throw new Error("Could not update provider settings in VS Code secret storage. Try again.");
      throw new Error(redactSensitiveText(error instanceof Error ? error.message : String(error)));
    } finally { this.acting = false; }
  }
}
