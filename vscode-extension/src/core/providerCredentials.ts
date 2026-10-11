/** Keep native environment overrides in a secret vault, outside launch records. */
export const BACKEND_IDS = ["codex", "claude", "lemex"] as const;
export type BackendId = typeof BACKEND_IDS[number];
export const BACKEND_LABELS: Record<BackendId, string> = { codex: "Codex", claude: "Claude Code", lemex: "Lemex" };

export function isBackend(value: unknown): value is BackendId {
  return typeof value === "string" && (BACKEND_IDS as readonly string[]).includes(value);
}
export type ProviderAction = "apiKey" | "removeKey" | "endpoint" | "login" | "config";
export const NATIVE_KEY_NAMES: Record<BackendId, string> = {
  codex: "OPENAI_API_KEY", claude: "ANTHROPIC_API_KEY", lemex: "LEMEX_API_KEY",
};

export interface SecretVault {
  get(key: string): Thenable<string | undefined>;
  store(key: string, value: string): Thenable<void>;
  delete(key: string): Thenable<void>;
}

export interface BackendSetup {
  id: BackendId;
  installed: boolean;
  executable: string;
  version: string;
  auth: "environment" | "native_login" | "login_needed" | "not_checked" | "unknown";
  key_env: string;
  config_path: string;
  endpoint: string;
  checked: boolean;
  savedKey?: boolean;
  savedEndpoint?: boolean;
}

export interface ProviderSetupState {
  selected: string;
  backends: BackendSetup[];
  checking: boolean;
  error: string;
  host: string;
}

/** Validate key shape without interpreting or displaying the credential. */
export function validateApiKey(value: string): string | undefined {
  const trimmed = value.trim();
  return !trimmed || /[\s\0]/.test(trimmed) || trimmed.length > 16384
    ? "Enter a nonempty API key without whitespace (maximum 16384 characters)." : undefined;
}

/** Reject embedded credentials so an endpoint can be safely shown in the UI. */
export function validateEndpoint(value: string): string | undefined {
  if (!value.trim()) return undefined;
  try {
    const url = new URL(value.trim());
    if (!["https:", "http:"].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) {
      return "Use an HTTP(S) URL without a username, password, query, or fragment.";
    }
    if (/\s/.test(value.trim())) return "The endpoint cannot contain spaces.";
  } catch { return "Enter a valid HTTP(S) endpoint, such as https://inference.rcp.epfl.ch/v1."; }
  return undefined;
}

export class ProviderCredentials {
  constructor(private readonly vault: SecretVault, private readonly host: string) {}

  private key(name: string): string { return `native-provider:${this.host}:${name}`; }

  /** Store only an explicitly entered native key; cancellation is handled by the UI. */
  async saveKey(backend: BackendId, value: string): Promise<void> {
    const trimmed = value.trim();
    const error = validateApiKey(value);
    if (error) throw new Error(error);
    await this.vault.store(this.key(NATIVE_KEY_NAMES[backend]), trimmed);
  }

  async removeKey(backend: BackendId): Promise<void> { await this.vault.delete(this.key(NATIVE_KEY_NAMES[backend])); }

  /** Empty restores Lemex's inherited environment and native configuration. */
  async saveEndpoint(value: string): Promise<void> {
    const error = validateEndpoint(value);
    if (error) throw new Error(error);
    if (value.trim()) await this.vault.store(this.key("LEMEX_BASE_URL"), value.trim());
    else await this.vault.delete(this.key("LEMEX_BASE_URL"));
  }

  /** Fetch fresh values only at launch/check time; never attach these to a run. */
  async environment(backend?: BackendId): Promise<Record<string, string>> {
    const names = backend ? [NATIVE_KEY_NAMES[backend]] : Object.values(NATIVE_KEY_NAMES);
    if (!backend || backend === "lemex") names.push("LEMEX_BASE_URL");
    const result: Record<string, string> = {};
    try {
      for (const name of names) {
        const value = await this.vault.get(this.key(name));
        if (value) result[name] = value;
      }
      return result;
    } catch {
      throw new Error("Could not read native credentials from VS Code secret storage.");
    }
  }

  /** Return presence flags only, suitable for rendering in an unprivileged webview. */
  async presence(backend: BackendId): Promise<{ savedKey: boolean; savedEndpoint: boolean }> {
    const env = await this.environment(backend);
    return { savedKey: Boolean(env[NATIVE_KEY_NAMES[backend]]), savedEndpoint: Boolean(env.LEMEX_BASE_URL) };
  }
}
