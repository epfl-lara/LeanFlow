/** Extension entry point: wire services, surfaces, and commands. */
import * as vscode from "vscode";

import { CliError, runCli } from "./core/cli";
import { relativeTarget } from "./core/project";
import { redactSensitiveText } from "./core/runPrivacy";
import { LeanFlowServices } from "./core/services";
import { emptyLaunchRequest } from "./core/types";
import { ControlViewProvider } from "./panels/controlView";
import { DashboardPanel } from "./panels/dashboardPanel";
import { openBenchmarkPanel } from "./panels/benchmarkPanel";
import { RunsTreeProvider } from "./views/runsTree";

/** Show a command failure with direct routes to installation and configuration. */
async function showCommandError(error: unknown): Promise<void> {
  const message = redactSensitiveText(
    error instanceof Error ? error.message : String(error),
  );
  const actions =
    error instanceof CliError
      ? (["Open install guide", "Configure CLI path"] as const)
      : (["Open LeanFlow dashboard"] as const);
  const selected = await vscode.window.showErrorMessage(`LeanFlow: ${message}`, ...actions);
  if (selected === "Open install guide") {
    await vscode.commands.executeCommand(
      "vscode.open",
      vscode.Uri.parse("https://github.com/epfl-lara/LeanFlow#install"),
    );
  } else if (selected === "Configure CLI path") {
    await vscode.commands.executeCommand(
      "workbench.action.openSettings",
      "@ext:epfl-lara.leanflow-vscode leanflow.cliPath",
    );
  } else if (selected === "Open LeanFlow dashboard") {
    await vscode.commands.executeCommand("leanflow.openDashboard");
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const services = new LeanFlowServices(context);
  const runsTree = new RunsTreeProvider(services);

  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = "leanflow.openDashboard";
  const refreshStatusBar = () => {
    const state = services.state();
    if (!state.project.found) {
      statusBar.hide();
      return;
    }
    if (!state.cli.ok) {
      statusBar.text = "$(warning) LeanFlow: CLI setup needed";
      statusBar.tooltip =
        `${redactSensitiveText(state.cli.error)}\nOpen the dashboard for setup guidance.`;
      statusBar.show();
      return;
    }
    const active = state.runs.filter(
      (run) => run.status === "running" || run.status === "starting",
    );
    if (active.length > 0) {
      const phase = state.liveStatus?.phase ?? "running";
      statusBar.text = `$(loading~spin) LeanFlow: ${phase}`;
      statusBar.tooltip = active.map((run) => run.label).join("\n");
    } else {
      statusBar.text = `$(beaker) LeanFlow: ${state.project.label}`;
      statusBar.tooltip = "Open the LeanFlow dashboard";
    }
    statusBar.show();
  };

  context.subscriptions.push(
    services,
    statusBar,
    services.onDidChange(refreshStatusBar),
    vscode.window.registerWebviewViewProvider(
      ControlViewProvider.viewType,
      new ControlViewProvider(context.extensionUri, services),
      { webviewOptions: { retainContextWhenHidden: true } },
    ),
    vscode.window.createTreeView("leanflow.runs", {
      treeDataProvider: runsTree,
      showCollapseAll: true,
    }),

    vscode.commands.registerCommand("leanflow.openDashboard", () => {
      DashboardPanel.show(context.extensionUri, services);
    }),
    vscode.commands.registerCommand("leanflow.openBenchmark", (uri?: vscode.Uri) =>
      openBenchmarkPanel(context.extensionUri, services, uri),
    ),

    vscode.commands.registerCommand("leanflow.refresh", async () => {
      await services.reload();
      runsTree.refresh();
    }),

    vscode.commands.registerCommand("leanflow.selectRun", (id: string) => {
      services.selectRun(id);
    }),

    vscode.commands.registerCommand("leanflow.launch", () => {
      DashboardPanel.show(context.extensionUri, services);
    }),

    vscode.commands.registerCommand("leanflow.proveActiveFile", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor || !editor.document.fileName.endsWith(".lean")) {
        void vscode.window.showWarningMessage("Open a .lean file first.");
        return;
      }
      const state = services.state();
      if (!state.cli.ok) {
        await showCommandError(new CliError(state.cli.error));
        return;
      }
      if (!state.project.found) {
        void vscode.window.showWarningMessage("No LeanFlow project found for this file.");
        return;
      }
      const target = relativeTarget(state.project, editor.document.fileName);
      if (!target) {
        void vscode.window.showWarningMessage(
          "That file is outside the active LeanFlow project.",
        );
        return;
      }
      try {
        const run = await services.runs.launch(
          { ...emptyLaunchRequest(), kind: "prove", target },
          services.profiles,
          services.catalog,
        );
        services.selectRun(run.id);
        DashboardPanel.show(context.extensionUri, services);
      } catch (error) {
        await showCommandError(error);
      }
    }),

    vscode.commands.registerCommand("leanflow.stopRun", async (node?: { run?: { id: string } }) => {
      const id = node?.run?.id ?? services.state().selectedRunId;
      if (!id) {
        void vscode.window.showWarningMessage("No run selected.");
        return;
      }
      const outcome = await services.runs.stop(id);
      if (!outcome.stopped) {
        void vscode.window.showWarningMessage(redactSensitiveText(outcome.reason));
      } else if (outcome.reason) {
        void vscode.window.showInformationMessage(redactSensitiveText(outcome.reason));
      }
    }),

    vscode.commands.registerCommand(
      "leanflow.openRunLog",
      async (node?: {
        run?: { id: string };
        summary?: { run_id: string; project_root?: string };
      }) => {
        try {
          const trackedId = node?.run?.id ?? services.state().selectedRunId;
          const tracked = trackedId ? services.runs.getRun(trackedId) : undefined;
          const runId =
            tracked?.runId ?? node?.summary?.run_id ?? services.state().history[0]?.run_id ?? "";
          const text = await services.runs.runLog(
            runId,
            2000,
            tracked?.projectRoot || node?.summary?.project_root || undefined,
          );
          const document = await vscode.workspace.openTextDocument({
            content: text || "[no workflow run log recorded yet]",
            language: "log",
          });
          await vscode.window.showTextDocument(document, { preview: true });
        } catch (error) {
          await showCommandError(error);
        }
      },
    ),

    vscode.commands.registerCommand("leanflow.initProject", async () => {
      const folder = vscode.workspace.workspaceFolders?.[0];
      if (!folder) {
        void vscode.window.showWarningMessage("Open a folder containing a Lean project first.");
        return;
      }
      try {
        await vscode.window.withProgress(
          { location: vscode.ProgressLocation.Notification, title: "Initializing LeanFlow project" },
          async () => {
            await runCli(["project", "init", folder.uri.fsPath], {
              cwd: folder.uri.fsPath,
              timeoutMs: 600_000,
            });
          },
        );
        await services.reload();
      } catch (error) {
        await showCommandError(error);
      }
    }),

    vscode.commands.registerCommand("leanflow.doctor", async () => {
      try {
        const state = services.state();
        const output = await runCli(["doctor"], {
          cwd: state.project.found ? state.project.root : undefined,
          timeoutMs: 180_000,
        });
        const document = await vscode.workspace.openTextDocument({
          content: output,
          language: "log",
        });
        await vscode.window.showTextDocument(document, { preview: true });
      } catch (error) {
        await showCommandError(error);
      }
    }),
  );

  refreshStatusBar();
}

export function deactivate(): void {
  // Runs are detached on purpose and keep going after the window closes; the
  // state they record is what a later session reattaches to.
}
