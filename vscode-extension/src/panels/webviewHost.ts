/**
 * Bridge one webview to the services layer.
 *
 * Both surfaces — the sidebar view and the dashboard panel — run the same
 * bundle and speak the same message protocol, so the handling lives once here
 * and each surface only supplies its own `vscode.Webview`.
 */
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import {
  CliError,
  fetchEventDetail,
  fetchProfileDiff,
  previewLaunch,
  sendProverMessage,
} from "../core/cli";
import { proverArtifactAllowed } from "../core/prover";
import {
  buildWorkflowArgs,
  rejectedProfileKnobNames,
  resolveLaunchEnv,
  validateLaunch,
} from "../core/launch";
import { validateLaunchProjectInputs } from "../core/launchPaths";
import {
  activityEventForDisplay,
  launchPlanForDisplay,
  profileDiffForDisplay,
  redactSensitiveText,
} from "../core/runPrivacy";
import type { LeanFlowServices } from "../core/services";
import {
  deleteProjectStorageFile,
  resolveExistingProjectPath,
  writeProjectStorageFile,
} from "../core/storageSecurity";
import { parseWebviewMessage } from "./messageSchema";
import type { FlagProfile, HostMessage, WebviewMessage } from "../core/types";

export type WebviewMode = "panel" | "sidebar";

/** Profile names that are safe to use as a filename, and nothing else. */
const SAFE_PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/**
 * Validate a profile name before it reaches the filesystem.
 *
 * The name arrives from the webview and is used to build a path, so `..` or a
 * separator would escape the profiles directory — for the delete path that
 * would mean removing an arbitrary file. Allowlist rather than escape, and
 * reject the dot names outright since they are valid under the pattern.
 */
function safeProfileName(raw: string): string | null {
  const name = String(raw ?? "").trim();
  if (!SAFE_PROFILE_NAME.test(name) || name === "." || name === "..") {
    return null;
  }
  return name;
}

export function buildWebviewHtml(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
  mode: WebviewMode,
): string {
  const nonce = crypto.randomBytes(16).toString("base64");
  const scriptUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, "dist", "webview.js"),
  );
  const styleUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, "dist", "webview.css"),
  );
  // Strict CSP: no remote loads, and only the nonce'd bundle may execute.
  const csp = [
    "default-src 'none'",
    `img-src ${webview.cspSource} data:`,
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `script-src 'nonce-${nonce}'`,
    `font-src ${webview.cspSource}`,
  ].join("; ");

  return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta http-equiv="Content-Security-Policy" content="${csp}" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link href="${styleUri}" rel="stylesheet" />
    <title>LeanFlow</title>
  </head>
  <body data-mode="${mode}">
    <div id="root"></div>
    <script nonce="${nonce}">window.__LEANFLOW_MODE__ = ${JSON.stringify(mode)};</script>
    <script nonce="${nonce}" src="${scriptUri}"></script>
  </body>
</html>`;
}

export class WebviewHost implements vscode.Disposable {
  private readonly disposables: vscode.Disposable[] = [];
  private ready = false;
  private readonly proverLoads = new Set<string>();

  constructor(
    private readonly webview: vscode.Webview,
    private readonly services: LeanFlowServices,
  ) {
    this.disposables.push(
      webview.onDidReceiveMessage((raw: unknown) => {
        // The webview is a separate document; nothing it posts is trusted until
        // it has been validated against the schema.
        const parsed = parseWebviewMessage(raw);
        if (parsed.ok === false) {
          // Surface the drop. A rejected request otherwise looks like a button
          // that did nothing — the field is over its length cap and the user
          // has no way to learn that. The reason names the message type and
          // the offending field only, never the value that failed, so echoing
          // it back cannot leak prompt or credential text.
          console.warn(`[leanflow] rejected webview message: ${parsed.reason}`);
          this.notify("error", `LeanFlow could not accept that request (${parsed.reason}).`);
          return;
        }
        void this.handle(parsed.message);
      }),
      services.onDidChange(() => this.pushState()),
      services.runs.onDidAppendEvents(({ runId, events, reset }) => {
        this.post({
          type: "events",
          runId,
          events: events.map(activityEventForDisplay),
          reset,
        });
      }),
    );
  }

  dispose(): void {
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
  }

  private post(message: HostMessage): void {
    void this.webview.postMessage(message);
  }

  private pushState(): void {
    if (!this.ready) {
      return;
    }
    this.post({ type: "state", state: this.services.state() });
  }

  private notify(level: "info" | "warn" | "error", message: string): void {
    this.post({ type: "notify", level, message: redactSensitiveText(message) });
  }

  private async handle(message: WebviewMessage): Promise<void> {
    try {
      await this.dispatch(message);
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error);
      this.notify("error", text);
    }
  }

  private async dispatch(message: WebviewMessage): Promise<void> {
    const { services } = this;
    switch (message.type) {
      case "ready": {
        this.ready = true;
        this.pushState();
        return;
      }
      case "refresh": {
        await services.reload();
        return;
      }
      case "launch": {
        const problems = validateLaunch(message.request);
        if (problems.length > 0) {
          this.notify("error", problems.join(" "));
          return;
        }
        // RunManager performs a fresh durable-owner probe immediately before
        // spawning. A cached status shown in the UI may be stale, so admission
        // must not be decided from that snapshot here.
        const run = await services.runs.launch(
          message.request,
          services.profiles,
          services.catalog,
        );
        services.selectRun(run.id);
        // The run id is minted at launch, so this is the first place it exists.
        this.notify("info", `Started ${run.label} · run id ${run.runId}`);
        return;
      }
      case "preview": {
        const { requestId, request } = message;
        if (!services.project.found) {
          this.post({
            type: "preview",
            requestId,
            plan: null,
            error: "No LeanFlow project is open.",
          });
          return;
        }
        try {
          await validateLaunchProjectInputs(
            services.project.root,
            request.target,
            request.additionalSkills,
          );
          const { env, rejected } = resolveLaunchEnv(
            request,
            services.profiles,
            services.catalog,
          );
          if (rejected.length > 0) {
            this.post({
              type: "preview",
              requestId,
              plan: null,
              error: `Unknown, invalid, non-editable, or terminal-only LeanFlow knobs: ${rejected.join(", ")}`,
            });
            return;
          }
          const plan = await previewLaunch(
            services.project.root,
            buildWorkflowArgs(request, env.set),
            env,
          );
          this.post({
            type: "preview",
            requestId,
            plan: launchPlanForDisplay(plan, request.prompt),
            error: "",
          });
        } catch (error) {
          this.post({
            type: "preview",
            requestId,
            plan: null,
            error: redactSensitiveText(
              error instanceof CliError ? error.message : String(error),
              [request.prompt],
            ),
          });
        }
        return;
      }
      case "stopRun": {
        const outcome = await services.runs.stop(message.id);
        if (!outcome.stopped) {
          this.notify("warn", outcome.reason);
        } else if (outcome.reason) {
          this.notify("info", outcome.reason);
        }
        return;
      }
      case "selectRun": {
        services.selectRun(message.id);
        return;
      }
      case "loadEvents": {
        const events = await services.runs.loadEvents(message.runId);
        this.post({
          type: "events",
          runId: message.runId,
          events: events.map(activityEventForDisplay),
          reset: true,
        });
        return;
      }
      case "loadEventDetail": {
        const { runId, eventId } = message;
        const root = services.runs.projectRootForRun(runId);
        if (!root) {
          this.post({
            type: "eventDetail",
            runId,
            eventId,
            detail: null,
            error: "This run is not in the project's recorded history.",
          });
          return;
        }
        try {
          const detail = await fetchEventDetail(root, runId, eventId);
          this.post({ type: "eventDetail", runId, eventId, detail, error: "" });
        } catch (error) {
          const raw = error instanceof Error ? error.message : String(error);
          // An older CLI has no `runs event`; argparse reports the unknown choice.
          const reason = /invalid choice/i.test(raw)
            ? "The installed LeanFlow CLI cannot expand events (no `runs event` command). Update LeanFlow to read model output here."
            : redactSensitiveText(raw);
          this.post({ type: "eventDetail", runId, eventId, detail: null, error: reason });
        }
        return;
      }
      case "loadRunLog": {
        try {
          const text = await services.runs.runLog(message.runId, 600);
          this.post({ type: "runLog", runId: message.runId, text });
        } catch (error) {
          // Clear the previous selection before surfacing the error; otherwise
          // the raw-log pane labels run B while still showing run A's text.
          this.post({ type: "runLog", runId: message.runId, text: "" });
          throw error;
        }
        return;
      }
      case "saveProfile": {
        await this.saveProfile(message.profile);
        return;
      }
      case "deleteProfile": {
        await this.deleteProfile(message.name);
        return;
      }
      case "diffProfiles": {
        try {
          const rows = await fetchProfileDiff(
            message.left,
            message.right,
            services.project.found ? services.project.root : undefined,
          );
          this.post({
            type: "diff",
            requestId: message.requestId,
            rows: profileDiffForDisplay(rows),
            error: "",
          });
        } catch (error) {
          this.post({
            type: "diff",
            requestId: message.requestId,
            rows: [],
            error: redactSensitiveText(
              error instanceof Error ? error.message : String(error),
            ),
          });
        }
        return;
      }
      case "createExperiment": {
        await services.experiments.create(message.matrix);
        this.notify("info", `Created sweep "${message.matrix.name}".`);
        return;
      }
      case "startExperiment": {
        const pending = services.experiments.pendingCellCount(message.matrixId);
        if (pending > 10) {
          const confirmed = await vscode.window.showWarningMessage(
            `Start a research sweep with ${pending} pending cells?`,
            {
              modal: true,
              detail:
                "Each cell starts a separate LeanFlow workflow and may incur charges from " +
                "the configured model provider. Results use isolated source clones and the " +
                "cell already in flight continues if this window is closed.",
            },
            "Start sweep",
          );
          if (confirmed !== "Start sweep") {
            this.notify("info", "Sweep start cancelled; no additional provider run was launched.");
            return;
          }
        }
        void services.experiments.start(message.matrixId).catch((error) => {
          this.notify("error", error instanceof Error ? error.message : String(error));
        });
        return;
      }
      case "stopExperiment": {
        services.experiments.stop(message.matrixId);
        return;
      }
      case "deleteExperiment": {
        await services.experiments.delete(message.matrixId);
        return;
      }
      case "exportExperiment": {
        const file = await services.experiments.export(
          message.matrixId,
          services.project.root,
        );
        this.notify("info", `Exported to ${file}`);
        void vscode.window.showTextDocument(vscode.Uri.file(file));
        return;
      }
      case "pickTarget": {
        await this.pickTarget(message.kind);
        return;
      }
      case "openDashboard": {
        await vscode.commands.executeCommand("leanflow.openDashboard");
        return;
      }
      case "openPath": {
        // Check the real target too: an in-project symlink may otherwise expose
        // a file elsewhere on the machine to the webview.
        const target = await resolveExistingProjectPath(
          services.project.root,
          message.path,
        );
        if (target === null) {
          this.notify(
            "error",
            "That path does not exist inside the active LeanFlow project.",
          );
          return;
        }
        await vscode.window.showTextDocument(vscode.Uri.file(target));
        return;
      }
      case "loadProver": {
        // One load per run at a time; the service serves an unchanged state
        // file from cache, so a fast webview refresh costs no CLI process.
        if (this.proverLoads.has(message.runId)) return;
        this.proverLoads.add(message.runId);
        try {
          const result = await services.prover.load(message.runId);
          this.post({ type: "proverState", runId: message.runId, snapshot: result.snapshot, error: result.error });
        } finally {
          this.proverLoads.delete(message.runId);
        }
        return;
      }
      case "proverMessage": {
        try {
          const root = services.runs.projectRootForRun(message.runId);
          if (!root) throw new Error("The selected prover run is no longer available.");
          await sendProverMessage(root, message.runId, message.agentId, message.message);
          this.post({ type: "proverMessageResult", runId: message.runId, requestId: message.requestId ?? "", success: true, error: "" });
          this.notify("info", "Guidance queued. The agent will receive it at its next decision boundary.");
        } catch (error) {
          const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
          this.post({ type: "proverMessageResult", runId: message.runId, requestId: message.requestId ?? "", success: false, error: reason });
          this.notify("error", reason);
        }
        return;
      }
      case "openProverFile": {
        const root = services.runs.projectRootForRun(message.runId);
        if (!root) throw new Error("The selected prover run is no longer available.");
        // The allowlist is the run's own advertised artifacts; the cached
        // snapshot is that same list, so opening a file needs no new CLI read.
        const snapshot = services.prover.cached(message.runId) ?? (await services.prover.load(message.runId)).snapshot;
        if (!snapshot || !proverArtifactAllowed(snapshot, message.path, message.baselinePath)) {
          throw new Error("This artifact is not part of the selected prover run.");
        }
        const target = await resolveExistingProjectPath(root, message.path);
        if (!target) throw new Error("The artifact does not exist inside this run's project.");
        if (message.baselinePath) {
          const baseline = await resolveExistingProjectPath(root, message.baselinePath);
          if (!baseline) throw new Error("The recorded baseline does not exist inside this run's project.");
          await vscode.commands.executeCommand("vscode.diff", vscode.Uri.file(baseline), vscode.Uri.file(target), `${path.basename(target)} · run changes`);
        } else {
          const line = Math.max(0, (message.line ?? 1) - 1);
          await vscode.window.showTextDocument(vscode.Uri.file(target), {
            selection: new vscode.Range(line, 0, line, 0), preview: true,
          });
        }
        return;
      }
      case "openExternalDoc": {
        const anchor = message.topic === "install" ? "#install" : "#quick-start";
        await vscode.commands.executeCommand(
          "vscode.open",
          vscode.Uri.parse(`https://github.com/epfl-lara/LeanFlow${anchor}`),
        );
        return;
      }
      case "openCliSettings": {
        await vscode.commands.executeCommand(
          "workbench.action.openSettings",
          "@ext:epfl-lara.leanflow-vscode leanflow.cliPath",
        );
        return;
      }
    }
  }

  private async pickTarget(kind: string): Promise<void> {
    const { services } = this;
    if (!services.project.found) {
      this.notify("error", "No LeanFlow project is open.");
      return;
    }
    const filters: Record<string, string[]> =
      kind === "formalize"
        ? { "Source documents": ["tex", "pdf"] }
        : { "Lean files": ["lean"] };
    const picked = await vscode.window.showOpenDialog({
      canSelectMany: false,
      defaultUri: vscode.Uri.file(services.project.root),
      filters,
      openLabel: "Use as target",
    });
    if (!picked?.[0]) {
      return;
    }
    const realTarget = await resolveExistingProjectPath(
      services.project.root,
      picked[0].fsPath,
    );
    if (realTarget === null) {
      this.notify("error", "The target must be inside the LeanFlow project.");
      return;
    }
    const realRoot = await fs.promises.realpath(services.project.root);
    this.post({ type: "targetPicked", path: path.relative(realRoot, realTarget) });
  }

  private async saveProfile(profile: FlagProfile): Promise<void> {
    const { services } = this;
    if (!services.project.found) {
      this.notify("error", "Open a LeanFlow project before saving a profile.");
      return;
    }
    const name = safeProfileName(profile.name);
    if (name === null) {
      this.notify(
        "error",
        "A profile name may use letters, digits, dot, dash, and underscore only.",
      );
      return;
    }
    const rejected = rejectedProfileKnobNames(profile, services.catalog);
    if (rejected.length > 0) {
      this.notify(
        "error",
        `Unknown, invalid, non-editable, or terminal-only knobs cannot be saved: ${rejected.join(", ")}`,
      );
      return;
    }
    await writeProjectStorageFile(
      services.project.root,
      [".leanflow", "flag-profiles"],
      `${name}.json`,
      JSON.stringify(
        { name, summary: profile.summary, overrides: profile.overrides },
        null,
        2,
      ) + "\n",
    );
    await services.reloadProfiles();
    this.notify("info", `Saved profile "${name}".`);
  }

  private async deleteProfile(rawName: string): Promise<void> {
    const { services } = this;
    if (!services.project.found) {
      return;
    }
    const name = safeProfileName(rawName);
    if (name === null) {
      this.notify("error", "That profile name is not valid.");
      return;
    }
    const deleted = await deleteProjectStorageFile(
      services.project.root,
      [".leanflow", "flag-profiles"],
      `${name}.json`,
    );
    if (!deleted) {
      this.notify("warn", `"${rawName}" is a built-in profile and cannot be deleted.`);
      return;
    }
    await services.reloadProfiles();
    this.notify("info", `Deleted profile "${name}".`);
  }
}
