/** Native setup cards send actions only; API keys never enter the webview. */
import { changeNativeSelection } from "../../src/core/providerModels";
import { Card, Notice } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";
import { StorageCard } from "./StorageCard";
import { BACKEND_LABELS as NAMES, type BackendSetup, type ProviderAction } from "../../src/core/providerCredentials";


/** Explain presence without claiming an API credential has been validated. */
export function credentialLabel(row: BackendSetup): string {
  if (row.savedKey) return "Saved API key · used for new runs";
  if (row.auth === "environment") return "Native credentials available in the environment";
  if (row.auth === "native_login") return "Native login available";
  if (row.auth === "login_needed") return "Sign in or add an API key";
  return "Using native configuration · authentication not verified";
}

export function ProvidersView() {
  const { app, view, setView, setForm } = useStore();
  const setup = app?.providers;
  return <div className="providers-view">
    <Card title="Providers" subtitle="Configure once on the host that runs LeanFlow. Each native agent keeps its own model defaults, login, tools, and context management.">
      <div className="row"><span className="muted">Host: {setup?.host || "loading…"}</span><span className="spacer" /><button className="btn ghost" disabled={setup?.checking} onClick={() => post({ type: "refreshProviders" })}>{setup?.checking ? "Checking…" : "Refresh setup"}</button></div>
      <p>API keys are entered in a private VS Code prompt and stored in VS Code secret storage. Saved values apply to new runs launched by this extension. Native terminal sessions keep their own settings.</p>
      <p className="muted">Saved keys take precedence over inherited environment keys and native login. Removing a saved key restores the inherited setup. Setup checks do not send a model request or verify API access.</p>
      {setup?.error && <Notice tone="error">{setup.error} <button className="link" onClick={() => post({ type: "openCliSettings" })}>Configure CLI path</button></Notice>}
    </Card>
    {setup?.backends.map(row => {
      const action = (action: ProviderAction) => post({ type: "providerAction", backend: row.id, action });
      return <Card key={row.id} title={NAMES[row.id]} subtitle={row.id === "lemex" ? "External API models, including RCP, through Lemex’s native runtime." : "Use your native account login or a standard API key."}>
        <div className="row"><span className={`pill ${row.installed ? "ok" : "warn"}`}>{row.installed ? `Installed${row.version ? ` · ${row.version}` : ""}` : "Installation needed"}</span>{setup.selected === row.id && <span className="tag">Configured default</span>}</div>
        <p>{credentialLabel(row)}</p>
        <div className="row tight">
          {row.id !== "lemex" && <button className="btn" disabled={!row.installed} onClick={() => action("login")}>Sign in</button>}
          <button className="btn" onClick={() => action("apiKey")}>{row.savedKey ? "Replace API key" : "Add API key"}</button>
          {row.savedKey && <button className="btn ghost" onClick={() => action("removeKey")}>Remove saved key</button>}
          {row.id === "lemex" && <button className="btn" onClick={() => action("endpoint")}>Set endpoint</button>}
          <button className="btn ghost" onClick={() => action("config")}>Open native config</button>
          <button className="btn ghost" onClick={() => { setForm(changeNativeSelection(view.form, { provider: row.id }, setup, app?.profiles?.profiles.find(profile => profile.name === view.form.profile)?.overrides)); setView({ tab: "launch" }); }}>Use for next run</button>
        </div>
        {row.id === "lemex" && <p className="muted">Endpoint: <span className="mono">{row.endpoint || "Lemex native default"}</span> {row.savedEndpoint ? "· saved in VS Code" : "· inherited"}. Leave the endpoint blank to restore native settings.</p>}
        {!row.installed && <p className="muted">{row.id === "lemex" ? "Run the LeanFlow installer on this host to install its managed Lemex runtime." : `Install ${NAMES[row.id]} on this host and make its CLI available on PATH.`} <button className="link" onClick={() => post({ type: "openExternalDoc", topic: "install" })}>Installation guide</button></p>}
        <details className="provider-native-details"><summary>Native paths and environment</summary><p className="mono">{row.executable || "Executable not found"}</p><p className="mono">{row.config_path}</p><p className="mono">{row.key_env}{row.id === "lemex" ? " · LEMEX_BASE_URL · LEMEX_HOME" : row.id === "codex" ? " · CODEX_HOME" : " · CLAUDE_CONFIG_DIR"}</p></details>
      </Card>;
    })}
    <StorageCard />
  </div>;
}
