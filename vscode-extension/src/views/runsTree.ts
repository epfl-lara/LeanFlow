/** Tree view listing tracked runs and previously recorded ones. */
import * as vscode from "vscode";

import { redactSensitiveText } from "../core/runPrivacy";
import type { LeanFlowServices } from "../core/services";
import type { RunSummary, TrackedRun, TrackedRunStatus } from "../core/types";

const STATUS_ICON: Record<TrackedRunStatus, { id: string; color?: string }> = {
  starting: { id: "loading~spin" },
  running: { id: "loading~spin", color: "charts.blue" },
  finished: { id: "pass-filled", color: "charts.green" },
  failed: { id: "error", color: "charts.red" },
  stopped: { id: "circle-slash", color: "charts.yellow" },
};

type Node = TrackedNode | HistoryNode | SectionNode;

interface SectionNode {
  kind: "section";
  label: string;
  children: Node[];
}

interface TrackedNode {
  kind: "tracked";
  run: TrackedRun;
}

interface HistoryNode {
  kind: "history";
  summary: RunSummary;
}

function relativeTime(iso: string): string {
  if (!iso) {
    return "";
  }
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return iso;
  }
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m ago`;
  }
  if (seconds < 86400) {
    return `${Math.round(seconds / 3600)}h ago`;
  }
  return `${Math.round(seconds / 86400)}d ago`;
}

export class RunsTreeProvider implements vscode.TreeDataProvider<Node> {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;

  constructor(private readonly services: LeanFlowServices) {
    services.onDidChange(() => this.changed.fire(undefined));
  }

  refresh(): void {
    this.changed.fire(undefined);
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.kind === "section") {
      const item = new vscode.TreeItem(
        node.label,
        vscode.TreeItemCollapsibleState.Expanded,
      );
      item.contextValue = "leanflow.section";
      return item;
    }
    if (node.kind === "tracked") {
      const { run } = node;
      const safeLabel = redactSensitiveText(run.label);
      const safeCommand = redactSensitiveText(run.command);
      const item = new vscode.TreeItem(safeLabel, vscode.TreeItemCollapsibleState.None);
      const icon = STATUS_ICON[run.status];
      item.iconPath = new vscode.ThemeIcon(
        icon.id,
        icon.color ? new vscode.ThemeColor(icon.color) : undefined,
      );
      const started = relativeTime(run.startedAt);
      item.description = [run.status, started].filter(Boolean).join(" · ");
      // Launch labels and commands contain workspace/user text. Keep the
      // tooltip plain so crafted prompts cannot create Markdown links or
      // remote image loads when somebody hovers the run.
      item.tooltip = [
        safeLabel,
        safeCommand,
        `Status: ${run.status}${run.exitCode !== null ? ` (exit ${run.exitCode})` : ""}`,
        run.runId ? `Run id: ${run.runId}` : "",
        run.error === null ? "" : redactSensitiveText(run.error),
      ]
        .filter(Boolean)
        .join("\n");
      item.contextValue =
        run.status === "running" || run.status === "starting"
          ? "leanflow.run.active"
          : "leanflow.run.finished";
      item.command = {
        command: "leanflow.selectRun",
        title: "Select run",
        arguments: [run.id],
      };
      return item;
    }
    const { summary } = node;
    const safeCommand = redactSensitiveText(
      summary.workflow_command || summary.label,
    );
    const item = new vscode.TreeItem(
      safeCommand,
      vscode.TreeItemCollapsibleState.None,
    );
    item.iconPath = new vscode.ThemeIcon("history");
    item.description = [`${summary.event_count} events`, relativeTime(summary.updated_at)]
      .filter(Boolean)
      .join(" · ");
    item.tooltip = [
      safeCommand || summary.run_id,
      `Kind: ${summary.workflow_kind || "unknown"}`,
      `Skill: ${summary.active_skill || "—"}`,
      `Started: ${summary.started_at || "—"}`,
      `Last event: ${summary.last_event_type || "—"}`,
    ].join("\n");
    item.contextValue = "leanflow.run.recorded";
    item.command = {
      command: "leanflow.openRunLog",
      title: "Open run log",
      arguments: [node],
    };
    return item;
  }

  getChildren(node?: Node): Node[] {
    if (node) {
      return node.kind === "section" ? node.children : [];
    }
    const state = this.services.state();
    if (!state.project.found) {
      return [];
    }
    const sections: Node[] = [];
    if (state.runs.length > 0) {
      sections.push({
        kind: "section",
        label: "This session",
        children: state.runs.map((run) => ({ kind: "tracked", run })),
      });
    }
    // Streams already claimed by a tracked run would otherwise appear twice.
    const claimed = new Set(state.runs.map((run) => run.runId).filter(Boolean));
    const recorded = state.history.filter((summary) => !claimed.has(summary.run_id));
    if (recorded.length > 0) {
      sections.push({
        kind: "section",
        label: "Recorded",
        children: recorded
          .slice(0, 40)
          .map((summary) => ({ kind: "history", summary }) as HistoryNode),
      });
    }
    return sections;
  }
}
