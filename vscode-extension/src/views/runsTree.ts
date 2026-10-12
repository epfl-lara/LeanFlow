/** Show the same clickable live and historical jobs in VS Code's native Runs tree. */
import * as vscode from "vscode";

import { appRunChoices, type RunChoice } from "../core/runCatalog";
import { redactSensitiveText } from "../core/runPrivacy";
import type { LeanFlowServices } from "../core/services";
import type { TrackedRun } from "../core/types";

type Node = { kind: "section"; label: string; children: Node[] } | {
  kind: "job"; choice: RunChoice; run?: TrackedRun; summary: { run_id: string };
};

export class RunsTreeProvider implements vscode.TreeDataProvider<Node> {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;

  constructor(private readonly services: LeanFlowServices) {
    services.onDidChange(() => this.changed.fire(undefined));
  }

  refresh(): void { this.changed.fire(undefined); }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.kind === "section") return new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.Expanded);
    const { choice } = node;
    const item = new vscode.TreeItem(redactSensitiveText(choice.label), vscode.TreeItemCollapsibleState.None);
    item.iconPath = new vscode.ThemeIcon(choice.active ? "loading~spin" : choice.status === "failed" ? "error" : "history");
    item.description = choice.status;
    item.tooltip = [choice.label, `Status: ${choice.status}`, `Run id: ${choice.runId}`, choice.tracked?.launchNotice, choice.tracked?.error].filter(Boolean).join("\n");
    item.contextValue = choice.active ? "leanflow.run.active" : "leanflow.run.recorded";
    item.command = { command: "leanflow.selectRun", title: "Open job", arguments: [choice.id] };
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (node) return node.kind === "section" ? node.children : [];
    const state = this.services.state();
    if (!state.project.found) return [];
    const choices = appRunChoices(state);
    return [true, false].flatMap(active => {
      const rows = choices.filter(choice => choice.active === active);
      return rows.length ? [{ kind: "section" as const, label: `${active ? "Live jobs" : "Past jobs"} (${rows.length})`, children: rows.map(choice => ({
        kind: "job" as const, choice, run: choice.tracked, summary: { run_id: choice.runId },
      })) }] : [];
    });
  }
}
