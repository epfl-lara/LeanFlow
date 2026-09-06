/**
 * Types shared by the extension host and the webview.
 *
 * The webview cannot import `vscode`, so anything that crosses the postMessage
 * boundary lives here and stays free of host-only imports.
 */

// ---------------------------------------------------------------- CLI shapes
import type { ProverSnapshot } from "./prover";
// These mirror the JSON emitted by `leanflow flags`, `leanflow runs`, and
// `leanflow workflow --dry-run --json`. Keep them in step with
// leanflow_cli/flags/spec.py and leanflow_cli/cli/runs_command.py.

export type FlagKind = "feature" | "tuning" | "runtime" | "internal";
export type FlagValueType = "bool" | "int" | "float" | "string" | "enum" | "path" | "csv";

export interface FlagSpec {
  name: string;
  kind: FlagKind;
  value_type: FlagValueType;
  default: string;
  group: string;
  summary: string;
  editable: boolean;
  /** False when only a trusted terminal may set this knob. */
  extension_editable: boolean;
  /** The knob can redirect/expose sensitive data or weaken an authority boundary. */
  sensitive: boolean;
  ablatable: boolean;
  read_in: string[];
  choices?: string[];
  minimum?: number;
  maximum?: number;
}

export interface FlagGroup {
  name: string;
  flags: FlagSpec[];
}

export interface FlagCatalog {
  version: number;
  count: number;
  groups: FlagGroup[];
}

export interface FlagProfile {
  name: string;
  summary: string;
  builtin: boolean;
  overrides: Record<string, string>;
}

export interface ProfileCatalog {
  version: number;
  count: number;
  search_paths: string[];
  profiles: FlagProfile[];
}

export interface ProfileDiffRow {
  name: string;
  left: string;
  right: string;
  group: string;
  summary: string;
  known: boolean;
}

export interface ActivityEvent {
  event_id: string;
  timestamp: string;
  type: string;
  run_id: string;
  agent_id: string;
  task_label: string;
  run_scope: string;
  message: string;
  details: Record<string, unknown>;
}

export interface RunSummary {
  run_id: string;
  label: string;
  event_count: number;
  started_at: string;
  updated_at: string;
  last_event_type: string;
  last_message: string;
  workflow_kind: string;
  workflow_command: string;
  active_skill: string;
  project_root: string;
  parent_run_id: string;
  run_scope: string;
  process_id: number;
  path: string;
  stream_source: "hot" | "final-result" | "retained";
  stream_integrity_complete: boolean;
  final_snapshot_integrity: string;
  /** True only when the exact stream contains its durable runner-exit event. */
  terminal: boolean;
  /** True when an immutable run-result envelope exists for this exact id. */
  final_snapshot_recorded: boolean;
  finalized_at: string;
  exit_code: number | null;
  terminal_phase: string;
  terminal_status:
    | "running"
    | "succeeded"
    | "paused"
    | "disproved"
    | "interrupted"
    | "failed"
    | "exited"
    | "unknown";
}

/** Complete retained source-root run history used for experiment overlap audits. */
export interface RunHistorySnapshot {
  version: number;
  complete: boolean;
  completenessIssues: string[];
  limit: number;
  totalCount: number;
  truncated: boolean;
  archiveAudit: {
    complete?: boolean;
    catalog_status?: string;
    catalog_runs?: number;
    verified_runs?: number;
    verified_events?: number;
    matched_events?: number;
    issue_counts?: Record<string, number>;
    issue_samples?: unknown[];
  };
  runs: RunSummary[];
}

/**
 * The live status snapshot. The runtime writes many more keys than this; only
 * the ones the UI renders are typed, and the rest stay reachable through the
 * index signature so a runtime addition does not require an extension release.
 */
export interface LiveStatus {
  phase?: string;
  workflow_kind?: string;
  workflow_command?: string;
  active_file?: string;
  active_file_label?: string;
  active_skill?: string;
  target_symbol?: string;
  provider?: string;
  model?: string;
  base_url?: string;
  sorry_count?: number;
  project_sorry_count?: number;
  proof_solved?: boolean;
  current_blocker?: string;
  current_queue_item?: string;
  declaration_queue_summary?: string;
  declaration_queue_total?: number;
  declaration_scope?: string;
  parallel_agents?: number;
  held_locks?: number;
  diagnostics?: string;
  goals?: string;
  last_activity_type?: string;
  last_activity_message?: string;
  latest_checkpoint_label?: string;
  checkpoint_count?: number;
  updated_at?: string;
  runtime_heartbeat_at?: string;
  process_id?: number;
  stale_snapshot?: boolean;
  agent_capacity?: Record<string, number>;
  [key: string]: unknown;
}

/** One declaration's outcome, from the persisted proof graph. */
export interface DeclarationOutcome {
  name: string;
  file: string;
  kind: string;
  status: string;
  closed: boolean;
  proved: boolean;
  attempts: number;
  api_steps: number;
  notes: string;
}

/** What is needed to reproduce a run later. */
export interface Provenance {
  leanflow_version: string;
  project_root: string;
  provenance_complete: boolean;
  git_executable_available: boolean;
  git_marker_present: boolean;
  git_repository: boolean;
  git_evidence_status:
    | "complete"
    | "incomplete"
    | "not-a-git-worktree"
    | "git-unavailable";
  git_evidence_complete: boolean;
  git_commands: Record<string, boolean>;
  source_tree_complete: boolean;
  git_commit: string;
  git_branch: string;
  git_dirty: boolean;
  git_dirty_files: number;
  git_status_sha256: string;
  git_diff_sha256: string;
  git_diff_bytes: number;
  git_untracked_files: number;
  git_untracked_index_sha256: string;
  git_submodules: string;
  git_submodules_sha256: string;
  project_manifest_sha256: string;
  workflow_guidance_sha256: string;
  runtime_source_sha256: string;
  runtime_content_sha256: string;
  python_runtime_sha256: string;
  python_runtime: {
    implementation: string;
    implementation_version: string;
    cache_tag: string;
    python_version: string;
    python_full_version: string;
    compiler: string;
    platform_system: string;
    platform_release: string;
    platform_machine: string;
  };
  installed_distributions: Record<string, string>;
  python_runtime_complete: boolean;
  python_runtime_issues: string[];
  selected_skills_sha256: string;
  behavior_config_sha256: string;
  dependency_manifest_sha256: string;
  source_identity_scope: string;
  source_tree_sha256: string;
  source_file_count: number;
  source_identity_sha256: string;
  lean_toolchain: string;
  dependencies: Record<string, string>;
  dependency_sources: Record<string, Record<string, string>>;
}

/** `leanflow runs metrics` — totals over the complete recorded stream. */
export interface RunMetrics {
  version: number;
  scope: {
    requested_run_id: string;
    resolved_run_id: string;
    run_found: boolean;
    stream_source: "hot" | "retained";
    stream_integrity_complete: boolean;
    terminal_event_seen: boolean;
    single_run_lifecycle: boolean;
    final_snapshot: string;
    exact: boolean;
    missing: string[];
    archive_audit: {
      complete?: boolean;
      catalog_status?: string;
      catalog_runs?: number;
      verified_runs?: number;
      verified_events?: number;
      matched_events?: number;
      issue_counts?: Record<string, number>;
      issue_samples?: unknown[];
    };
  };
  run: {
    run_id: string;
    workflow_kind: string;
    workflow_command: string;
    active_skill: string;
    started_at: string;
    updated_at: string;
    duration_s: number | null;
  };
  launch: {
    captured_at: string | null;
    context: Record<string, unknown>;
    environment: {
      scope: string;
      values: Record<string, string>;
      sha256: string;
      runtime: {
        base_url: string;
        model: string;
        provider: string;
        reasoning_effort: string;
        toolset: string;
        active_skill: string;
        additional_skills: string;
        requested_target: string;
      };
    };
  } | null;
  events: { total: number; complete: boolean; by_type: Record<string, number> };
  activity: {
    tool_calls: number;
    tool_results: number;
    api_requests: number;
    assistant_responses: number;
    dispatch_jobs: number;
  };
  failures: Record<string, number>;
  journal_rejections: Record<string, number>;
  journal_rejections_scope: string | null;
  usage: {
    api_calls: number | null;
    input_tokens: number | null;
    output_tokens: number | null;
    cost_usd: number | null;
    aggregation_scope: string;
    recorded_api_requests: number;
    recorded_api_request_events: number;
    recorded_api_call_events: number;
    unmetered_api_attempts: number;
    unmetered_reason_counts: Record<string, number>;
    command_expert_attempts: number;
    descendant_dispatch_events: number;
    descendant_usage_complete: boolean;
    api_request_coverage_complete: boolean;
    api_calls_complete: boolean;
    tokens_complete: boolean;
    cost_complete: boolean;
    complete: boolean;
    source: string;
    cost_source: string;
    conversation_end_events: number;
    agent_sessions: number;
    models: string[];
    providers: string[];
  };
  declarations: DeclarationOutcome[];
  declaration_status_counts: Record<string, number>;
  outcome: {
    phase: string | null;
    exit_code: number | null;
    terminal_status: string | null;
    reason: string | null;
    proof_solved: boolean | null;
    sorry_count: number | null;
    project_sorry_count: number | null;
    model: string | null;
    provider: string | null;
    declarations_total: number | null;
    declarations_proved: number | null;
  };
  provenance?: Provenance | null;
  provenance_final?: Provenance | null;
  source_changed_during_run?: boolean | null;
  runtime_source_changed?: boolean | null;
  selected_skills_changed?: boolean | null;
  behavior_config_changed?: boolean | null;
  project_configuration_changed?: boolean | null;
  python_runtime_changed?: boolean | null;
}

export interface LaunchPlanPreview {
  version: number;
  deferred: string[];
  summary: Record<string, string>;
  argv: string[];
  cwd: string;
  project: { label: string; root: string; lean_root: string };
  workflow: {
    kind: string;
    canonical_command: string;
    backend_command: string;
    args: string;
    parallel_agents: number;
    research_mode: boolean;
    research_workers: number;
    clean_room: boolean;
    clean_room_labels: string[];
    human_review: boolean;
    allowed_axioms: string;
    explicit_goal: string;
  };
  runtime: Record<string, string>;
  active_skill: string;
  additional_skills: string[];
  toolset: string;
  env_delta: Record<string, string>;
  /** Complete credential-safe effective LEANFLOW_* child environment. */
  env_effective: Record<string, string>;
}

// ------------------------------------------------------------- launch inputs

export type WorkflowKind = "prove" | "formalize" | "review" | "refactor" | "golf" | "draft";

export const WORKFLOW_KINDS: { id: WorkflowKind; label: string; hint: string }[] = [
  { id: "prove", label: "prove", hint: "Repair proofs until Lean agrees. No file means whole project." },
  { id: "formalize", label: "formalize", hint: "Turn a .tex or .pdf source into a buildable Lean draft." },
  { id: "review", label: "review", hint: "Read-only review of Lean proofs." },
  { id: "refactor", label: "refactor", hint: "Leverage mathlib, extract helpers, simplify strategies." },
  { id: "golf", label: "golf", hint: "Shorten proofs without changing semantics." },
  { id: "draft", label: "draft", hint: "Draft declaration skeletons from informal claims." },
];

/**
 * Length ceilings for the launcher's free-text fields.
 *
 * The host rejects any message that exceeds these, so the form must cap the
 * same values: a dropped request would otherwise look like a control that did
 * nothing. Shared here because `src/core/types.ts` is the one module both the
 * host validator and the webview may import.
 */
export const LAUNCH_FIELD_LIMITS = {
  target: 1024,
  provider: 128,
  model: 256,
  axioms: 2048,
  prompt: 8192,
  profile: 128,
  profileSummary: 1024,
  additionalSkill: 4096,
  additionalSkillCount: 256,
  overrideName: 256,
  overrideValue: 4096,
  overrideCount: 512,
} as const;

/** Everything the launcher form collects. Maps 1:1 onto CLI flags. */
export interface LaunchRequest {
  kind: WorkflowKind;
  target: string;
  provider: string;
  model: string;
  agents: number;
  research: boolean;
  researchWorkers: number | null;
  noParallel: boolean;
  cleanRoom: boolean;
  humanReview: boolean;
  axioms: string;
  prompt: string;
  additionalSkills: string[];
  /** Name of the knob profile applied on top of the launch, or "" for none. */
  profile: string;
  /** One-off knob overrides layered over the profile. */
  overrides: Record<string, string>;
}

export function emptyLaunchRequest(): LaunchRequest {
  return {
    kind: "prove",
    target: "",
    provider: "",
    model: "",
    agents: 1,
    research: false,
    researchWorkers: null,
    noParallel: false,
    cleanRoom: false,
    humanReview: false,
    axioms: "",
    prompt: "",
    additionalSkills: [],
    profile: "",
    overrides: {},
  };
}

// ------------------------------------------------------------- tracked runs

export type TrackedRunStatus = "starting" | "running" | "finished" | "failed" | "stopped";

export interface TrackedRun {
  /** Stable id assigned by the extension, independent of the runtime's run_id. */
  id: string;
  /** The runtime's run id, once its activity stream appears. */
  runId: string;
  label: string;
  status: TrackedRunStatus;
  pid: number | null;
  exitCode: number | null;
  startedAt: string;
  finishedAt: string | null;
  request: LaunchRequest;
  command: string;
  projectRoot: string;
  /**
   * Knob environment actually applied, after profile resolution.
   *
   * Only catalogued, editable `LEANFLOW_*` names can appear here — the launcher
   * rejects anything else — so this record is safe to persist and to show. It
   * can never hold a credential.
   */
  appliedOverrides: { set: Record<string, string>; unset: string[] };
  /** Set when the run belongs to an experiment sweep. */
  experimentId: string | null;
  experimentCell: string | null;
  error: string | null;
}

// -------------------------------------------------------------- experiments

export interface ExperimentAxisValue {
  id: string;
  label: string;
}

/** One sweep: the cross product of targets, profiles, and models. */
export interface ExperimentMatrix {
  id: string;
  name: string;
  kind: WorkflowKind;
  targets: string[];
  profiles: string[];
  models: string[];
  /** Repeats per cell, for variance. */
  repeats: number;
  /** Base launch settings every cell inherits. */
  base: LaunchRequest;
  /** Seed used to randomize cell order once, before any run begins. */
  randomizationSeed?: string;
  createdAt: string;
}

export type ExperimentCellStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "unscored"
  | "blocked"
  | "skipped";

/** Immutable Git source shared by every cell in an experiment. */
export interface ExperimentBaseline {
  strategy: "detached-local-clone";
  /** Canonical path of the source repository used only to create clones. */
  repositoryRoot: string;
  /** Project location within the repository, or "." for its root. */
  projectSubdirectory: string;
  /** Remote URL when configured, otherwise the canonical repository path. */
  source: string;
  commit: string;
  tree: string;
  /** SHA-256 of the ignored project manifest copied into every clone. */
  manifestSha256: string;
  capturedAt: string;
}

/** Canonical provider runtime resolved before the first paid sweep cell. */
export interface ExperimentRuntimeIdentity {
  /** Normalized selector passed by the experiment, such as `codex` or `rcp`. */
  requestedProvider: string;
  /** Provider identity the managed child records, such as `openai-codex` or `custom`. */
  provider: string;
  model: string;
  /** Credential-free endpoint identity; noncredential query settings are retained, fragments are not. */
  baseUrl: string;
  reasoningEffort: string;
}

/** Digest-only identity for one sealed launch-environment value. */
export interface ExperimentSealedValueIdentity {
  sha256: string;
  chars: number;
}

/** Effective workflow and knob contract frozen from the dry-run child plan. */
export interface ExperimentLaunchContract {
  version: 1;
  workflow: {
    kind: string;
    parallelAgents: number;
    researchMode: boolean;
    researchWorkers: number;
    cleanRoom: boolean;
    humanReview: boolean;
  };
  /** Exact safe environment values, retained only as digest/length pairs. */
  set: Record<string, ExperimentSealedValueIdentity>;
  /** Explicit knob clears that must remain absent in the managed child. */
  unset: string[];
  /** Boolean controls whose effective false form may be absent or any canonical false spelling. */
  falseOrAbsent: string[];
  /** Text controls whose effective empty form may be absent or the empty string. */
  emptyOrAbsent: string[];
  /** Ordered requested/resolved skills; formalization may append its generated blueprint skill. */
  additionalSkills: ExperimentSealedValueIdentity[];
}

/** Launch inputs that must stay identical across every experiment condition. */
export interface ExperimentLaunchInvariant {
  gitCommit: string;
  gitSubmodulesSha256: string;
  sourceTreeSha256: string;
  projectManifestSha256: string;
  workflowGuidanceSha256: string;
  runtimeContentSha256: string;
  runtimeSourceSha256: string;
  pythonRuntimeSha256: string;
  behaviorConfigSha256: string;
  dependencyManifestSha256: string;
  leanToolchain: string;
}

/** Compact content identity retained in editor state; full evidence stays in the run snapshot. */
export type ExperimentProvenanceIdentity = Pick<
  Provenance,
  | "provenance_complete"
  | "git_commit"
  | "git_dirty"
  | "git_status_sha256"
  | "git_diff_sha256"
  | "git_untracked_index_sha256"
  | "source_tree_complete"
  | "source_tree_sha256"
  | "source_identity_sha256"
  | "lean_toolchain"
>;

export interface ExperimentCell {
  id: string;
  matrixId: string;
  target: string;
  /** Filesystem-canonical project-relative identity sealed before any cell launches. */
  targetIdentity: string | null;
  profile: string;
  model: string;
  repeat: number;
  /** Stable randomized position chosen when the matrix is expanded. */
  plannedOrder: number;
  /** Actual sequential launch position; terminal rows are never retried in place. */
  executionOrder: number | null;
  status: ExperimentCellStatus;
  runId: string | null;
  trackedRunId: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  /** Isolated project clone used for this cell and for its durable metrics. */
  projectRoot: string | null;
  /** Preview-resolved runtime frozen before this experiment's first launch. */
  expectedRuntime: ExperimentRuntimeIdentity | null;
  /** Digest-only effective launch/knob contract frozen before the first paid cell. */
  expectedLaunchContract: ExperimentLaunchContract | null;
  metrics: ExperimentMetrics | null;
  error: string | null;
}

/** Scored outcome of one cell, read back from the run's recorded state. */
export interface ExperimentMetrics {
  /** True only for a verified run stream joined to its immutable final snapshot. */
  scopeExact: boolean;
  solved: boolean | null;
  sorryCount: number | null;
  projectSorryCount: number | null;
  durationSeconds: number | null;
  apiCalls: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  costUsd: number | null;
  phase: string;
  toolCalls: number | null;
  verificationFailures: number | null;
  /**
   * True when the counts came from the CLI's durable aggregate over the whole
   * stream, rather than from the UI's bounded buffer.
   */
  countsAreComplete: boolean;
  usageComplete: boolean;
  apiCallsComplete: boolean;
  tokensComplete: boolean;
  costComplete: boolean;
  usageSource: string;
  declarationsTotal: number | null;
  declarationsProved: number | null;
  /** Failure taxonomy, keyed by bucket. */
  failures: Record<string, number>;
  /** Why this row is missing; empty for a measured row. */
  missingReason: string;
  /** Compact launch identity; full provenance is re-read into research exports. */
  provenance: ExperimentProvenanceIdentity | null;
  /** Source identity after the run, sealed with the final outcome. */
  provenanceFinal: ExperimentProvenanceIdentity | null;
  /** Whether source content changed between launch and finalization. */
  sourceChangedDuringRun: boolean | null;
}

export interface ExperimentRun {
  matrix: ExperimentMatrix;
  cells: ExperimentCell[];
  /** Whether an encrypted frozen prompt is required to launch pending cells. */
  promptPresent: boolean;
  /** Content identity only; raw prompt bytes live exclusively in SecretStorage. */
  promptSha256: string | null;
  /** One clean Git baseline captured before the first cell is launched. */
  baseline: ExperimentBaseline | null;
  /** Launch source identity every measured cell must share. */
  launchSourceIdentitySha256: string | null;
  /** Component-wise launch inputs that no experiment axis is allowed to vary. */
  launchInvariant: ExperimentLaunchInvariant | null;
  /** Selected-skill content may differ by target, but never within one target. */
  selectedSkillsSha256ByTarget: Record<string, string>;
  /** Profile definitions frozen before the first cell, keyed by profile name. */
  profileSnapshot: Record<string, Record<string, string>>;
  status: "idle" | "running" | "paused" | "blocked" | "done";
  /** Experiment-level setup failure, such as a dirty or non-Git baseline. */
  error: string | null;
}

// --------------------------------------------------------------- app state

export interface CliStatus {
  ok: boolean;
  path: string;
  version: string;
  error: string;
}

export interface ProjectInfo {
  found: boolean;
  root: string;
  label: string;
  stateRoot: string;
}

export interface AppState {
  project: ProjectInfo;
  cli: CliStatus;
  catalog: FlagCatalog | null;
  profiles: ProfileCatalog | null;
  runs: TrackedRun[];
  history: RunSummary[];
  selectedRunId: string | null;
  liveStatus: LiveStatus | null;
  eventTypes: { type: string; count: number }[];
  experiments: ExperimentRun[];
  leanFiles: string[];
  busy: boolean;
}

// ---------------------------------------------------------- message protocol

export type HostMessage =
  | { type: "state"; state: AppState }
  | { type: "events"; runId: string; events: ActivityEvent[]; reset: boolean }
  | { type: "preview"; requestId: string; plan: LaunchPlanPreview | null; error: string }
  | { type: "diff"; requestId: string; rows: ProfileDiffRow[]; error: string }
  | { type: "runLog"; runId: string; text: string }
  | { type: "proverState"; runId: string; snapshot: ProverSnapshot | null; error: string }
  | { type: "proverMessageResult"; runId: string; requestId: string; success: boolean; error: string }
  | { type: "targetPicked"; path: string }
  | { type: "notify"; level: "info" | "warn" | "error"; message: string };

export type WebviewMessage =
  | { type: "ready" }
  | { type: "refresh" }
  | { type: "launch"; request: LaunchRequest }
  | { type: "preview"; requestId: string; request: LaunchRequest }
  | { type: "stopRun"; id: string }
  | { type: "selectRun"; id: string | null }
  | { type: "loadEvents"; runId: string }
  | { type: "loadRunLog"; runId: string }
  | { type: "loadProver"; runId: string }
  | { type: "proverMessage"; runId: string; agentId: string; message: string; requestId?: string }
  | { type: "openProverFile"; runId: string; path: string; baselinePath?: string; line?: number }
  | { type: "saveProfile"; profile: FlagProfile }
  | { type: "deleteProfile"; name: string }
  | { type: "diffProfiles"; requestId: string; left: string; right: string }
  | { type: "createExperiment"; matrix: ExperimentMatrix }
  | { type: "startExperiment"; matrixId: string }
  | { type: "stopExperiment"; matrixId: string }
  | { type: "deleteExperiment"; matrixId: string }
  | { type: "exportExperiment"; matrixId: string }
  | { type: "pickTarget"; kind: WorkflowKind }
  | { type: "openDashboard" }
  | { type: "openCliSettings" }
  | { type: "openPath"; path: string }
  | { type: "openExternalDoc"; topic: string };
