/** Show the durable Lean-IMO queue and open any cell in the native prover dashboard. */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { randomUUID } from "node:crypto";
import * as vscode from "vscode";

import { benchmarkCost, benchmarkEscape as esc, benchmarkProject, benchmarkSummary, type BenchmarkCampaign } from "../core/benchmarkCampaign";
import type { LeanFlowServices } from "../core/services";
import { DashboardPanel } from "./dashboardPanel";

/** Open a campaign manifest without starting or altering a paid proof attempt. */
export async function openBenchmarkPanel(extensionUri: vscode.Uri, services: LeanFlowServices, supplied?: vscode.Uri): Promise<void> {
  const selected = supplied ?? (await vscode.window.showOpenDialog({ canSelectMany: false, filters: { "LeanFlow campaign": ["json"] }, title: "Open the benchmark campaign.json" }))?.[0];
  if (!selected || selected.scheme !== "file") return;
  const directory = path.dirname(selected.fsPath);
  const manifest = path.join(directory, "campaign.json");
  const panel = vscode.window.createWebviewPanel("leanflow.benchmark", "LeanFlow · IMO benchmark", vscode.ViewColumn.One, { enableScripts: true, retainContextWhenHidden: true });
  const nonce = randomUUID();
  panel.webview.html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';"><style>
  body{font-family:var(--vscode-font-family);padding:24px;color:var(--vscode-foreground);background:var(--vscode-editor-background)}h1{font-size:24px}p{line-height:1.5}.metrics{font-size:18px;margin:22px 0}.muted{color:var(--vscode-descriptionForeground)}.active{border-left:3px solid var(--vscode-progressBar-background);padding:12px;margin:12px 0;background:var(--vscode-textCodeBlock-background)}table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:9px 6px;text-align:left;border-bottom:1px solid var(--vscode-panel-border)}button{background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:0;padding:6px 10px;cursor:pointer;margin:3px}.ok{color:var(--vscode-testing-iconPassed)}.error{color:var(--vscode-errorForeground)}
  </style></head><body><main id="content">Loading campaign…</main><script nonce="${nonce}">const api=acquireVsCodeApi();window.addEventListener('message',e=>{document.getElementById('content').innerHTML=e.data.html});document.addEventListener('click',e=>{const b=e.target.closest('button[data-action]');if(b)api.postMessage({action:b.dataset.action,id:b.dataset.id})});api.postMessage({action:'refresh'});</script></body></html>`;
  let disposed = false;
  let refreshing = false;
  async function read(): Promise<BenchmarkCampaign> {
    const data = JSON.parse(await fs.readFile(manifest, "utf8")) as BenchmarkCampaign;
    if (data.version !== 1 || !Array.isArray(data.cells)) throw new Error("Unsupported benchmark manifest.");
    return data;
  }
  async function refresh(): Promise<void> {
    if (disposed || refreshing) return;
    refreshing = true;
    try {
      const c = await read();
      const s = benchmarkSummary(c);
      const button = (action: string, id: string, label: string) => `<button data-action="${action}" data-id="${esc(id)}">${label}</button>`;
      const active = c.cells.filter((cell) => ["running", "preparing"].includes(cell.status)).map((cell) => `<div class="active"><b>Lane ${cell.lane}: ${esc(cell.problem.id)} · ${esc(cell.condition)}</b><p>${esc(cell.phase || cell.status)} · ${Number(cell.metrics.api_calls || 0)} calls</p>${cell.project ? button("dashboard", cell.id, "Open prover dashboard") + button("source", cell.id, "Source") + button("plan", cell.id, "Plan") + button("log", cell.id, "Process log") : "Preparing isolated project…"}</div>`).join("");
      const rows = c.cells.map((cell) => `<tr><td>${esc(cell.problem.id)}</td><td>${esc(cell.condition)}</td><td>${cell.lane ?? "—"}</td><td class="${cell.verified ? "ok" : ""}">${cell.verified ? "✓ verified" : esc(cell.status)}${cell.stop_reason?.message ? `<br><span class="muted">${esc(cell.stop_reason.message)}</span>` : ""}</td><td title="${esc(cell.runtime_sha256 || "Runtime not yet assigned")}">${esc(cell.runtime_sha256?.slice(0, 10) || "unassigned")}</td><td>${cell.metrics.api_calls ?? "—"}</td><td>${cell.metrics.input_tokens ?? "—"} / ${cell.metrics.output_tokens ?? "—"}</td><td>${cell.metrics.plan_refinements ?? "—"} / ${cell.metrics.decompositions ?? "—"}</td><td>${cell.metrics.elapsed_s == null ? "—" : (Number(cell.metrics.elapsed_s) / 60).toFixed(1)}</td><td>${esc(benchmarkCost(cell.metrics))}</td><td>${cell.project ? button("dashboard", cell.id, "Inspect") : ""}</td></tr>`).join("");
      const html = `<h1>${esc(c.name)}</h1><p class="muted">${esc(c.status)} · updated ${esc(c.updated_at)}<br>Runtime ${esc(c.provenance.commit.slice(0, 10))} + working changes · snapshot ${esc(c.provenance.runtime_sha256.slice(0, 12))} · ${esc(c.provenance.toolchain)}</p><p>Astra xhigh ↓ → Astra xhigh ↑ → Terra xhigh ↓ → Terra xhigh ↑, independently within each problem lane.<br>Per cell: 4 provers · 200 calls/pass · 50 calls/planning stage · 2,000 total calls · 8 hours. Internet research disabled; local source search and computation enabled.</p><div class="metrics">${s.verified} / ${c.cells.length} cells verified · ${s.active} active · ${s.calls.toLocaleString()} API calls · ${s.tokens.toLocaleString()} tokens</div>${button("pause", "", "Pause queue after active cells")}${button("csv", "", "Open metrics CSV")}${c.pause_reason ? `<p class="error">${esc(c.pause_reason)}</p>` : ""}${active}<table><thead><tr><th>Problem</th><th>Condition</th><th>Lane</th><th>Outcome</th><th>Runtime</th><th>Calls</th><th>Input / output tokens</th><th>Refinements / splits</th><th>Minutes</th><th>Cost*</th><th></th></tr></thead><tbody>${rows}</tbody></table><p class="muted">*Costs identify their source and partial coverage. Historical unlabelled estimates are unavailable; missing cost is never counted as zero. Only independent final Lean acceptance counts as verified. Each of the four conditions has 18 problems. LEAP used Lean/Mathlib 4.27.0; this fixture uses 4.33.1.</p>`;
      await panel.webview.postMessage({ html });
    } catch (error) {
      await panel.webview.postMessage({ html: `<p class="error">${esc(String(error))}</p>` });
    } finally { refreshing = false; }
  }
  const receive = panel.webview.onDidReceiveMessage(async (message: { action?: string; id?: string }) => {
    try {
      if (message.action === "refresh") { await refresh(); return; }
      if (message.action === "pause") { await fs.writeFile(path.join(directory, "PAUSE_AFTER_ACTIVE"), "Requested from VS Code\n"); await refresh(); return; }
      if (message.action === "csv") { await vscode.commands.executeCommand("vscode.open", vscode.Uri.file(path.join(directory, "metrics.csv"))); return; }
      const campaign = await read();
      const cell = campaign.cells.find((item) => item.id === message.id);
      if (!cell) throw new Error("Unknown benchmark cell.");
      const project = benchmarkProject(directory, cell);
      if (message.action === "log") {
        if (!/^[A-Za-z0-9_-]+$/.test(cell.run_id || "")) throw new Error("Invalid run identifier.");
        await vscode.commands.executeCommand("vscode.open", vscode.Uri.file(path.join(directory, "cell-logs", `${cell.run_id}.log`)));
        return;
      }
      if (message.action === "dashboard") {
        await vscode.workspace.getConfiguration("leanflow").update("projectRoot", project, vscode.ConfigurationTarget.Workspace);
        await services.reload();
        if (cell.run_id) services.selectRun(cell.run_id);
        DashboardPanel.show(extensionUri, services);
        return;
      }
      const relative = message.action === "source" ? cell.problem.file : message.action === "plan" && /^[A-Za-z0-9_-]+$/.test(cell.run_id || "") ? `.leanflow/workflow-state/prover/${cell.run_id}/PLAN.md` : message.action === "log" ? "process.log" : "";
      const file = path.resolve(project, relative);
      if (!relative || !file.startsWith(project + path.sep)) throw new Error("Invalid artifact path.");
      await vscode.commands.executeCommand("vscode.open", vscode.Uri.file(file));
    } catch (error) { void vscode.window.showErrorMessage(`LeanFlow benchmark: ${String(error)}`); }
  });
  const timer = setInterval(() => void refresh(), 5000);
  panel.onDidDispose(() => { disposed = true; clearInterval(timer); receive.dispose(); });
  await refresh();
}
