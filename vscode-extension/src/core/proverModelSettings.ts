/** Describe saved role selections and job settings without consulting today's provider configuration. */
import type { ProverJob, ProverSnapshot } from "./prover";

export interface RoleSetting {
  role: "prover" | "orchestrator";
  label: string;
  provider: string;
  model: string;
  reasoning_effort: string;
  contextTokens: number | null;
  compression: boolean | null;
  callsPerStage: number | null;
  stageLabel: string;
}

/** Follow the runtime's role inheritance: model and effort belong to their backend. */
export function roleSettings(snapshot: ProverSnapshot): RoleSetting[] {
  const config = snapshot.config;
  const provider = config.provider || snapshot.provider;
  const orchestratorProvider = config.orchestrator_provider || provider;
  const sameBackend = orchestratorProvider === provider;
  const effort = config.native_backend ? config.reasoning_effort : snapshot.reasoning_effort;
  return [
    {
      role: "prover", label: "Prover", provider, model: config.model, reasoning_effort: effort,
      contextTokens: config.context_tokens, compression: config.compression,
      callsPerStage: config.job_api_calls,
      stageLabel: "per prover pass, retained across local decomposition",
    },
    {
      role: "orchestrator", label: "Orchestrator, review, and research", provider: orchestratorProvider,
      model: config.orchestrator_model || (sameBackend ? config.model : ""),
      reasoning_effort: config.orchestrator_reasoning_effort || (
        sameBackend && (!config.orchestrator_model || config.orchestrator_model === config.model) ? effort : ""
      ),
      contextTokens: config.orchestrator_context_tokens, compression: config.orchestrator_compression,
      callsPerStage: config.orchestrator_api_calls,
      stageLabel: "per planning, review, or research stage; each stage is a fresh context with its own allowance",
    },
  ];
}

/** Explain an empty native override without implying that no model is running. */
export function modelSelection(model: string, native: boolean): string {
  return model || (native ? "Native default" : "Not recorded");
}

export interface JobSettings {
  provider: string;
  model: string;
  reasoning_effort: string;
  context_tokens: number | null;
  compression: boolean | null;
  /** "job" when the job recorded its own settings, otherwise the run's shared ones. */
  source: "job" | "shared";
}

/** Prefer job settings; a recorded native default must survive later run overrides. */
export function jobSettings(job: ProverJob, snapshot: ProverSnapshot): JobSettings {
  const orchestrator = job.role !== "prover" && job.role !== "negation";
  const role = roleSettings(snapshot)[orchestrator ? 1 : 0];
  const shared: JobSettings = {
    provider: role.provider,
    model: role.model,
    reasoning_effort: role.reasoning_effort,
    context_tokens: role.contextTokens,
    compression: role.compression,
    source: "shared",
  };
  const recorded = job.provider || job.model || job.reasoning_effort || job.context_tokens !== null || job.compression !== null;
  if (!recorded) return shared;
  // Native jobs always save their backend, including when model/effort are
  // intentionally empty. Filling those from a resumed run invents history.
  const nativeRecord = snapshot.config.native_backend && Boolean(job.provider);
  const sameBackend = !job.provider || job.provider === shared.provider;
  return {
    provider: job.provider || shared.provider,
    model: job.model || (!nativeRecord && sameBackend ? shared.model : ""),
    reasoning_effort: job.reasoning_effort || (!nativeRecord && sameBackend ? shared.reasoning_effort : ""),
    context_tokens: job.context_tokens ?? shared.context_tokens,
    compression: job.compression ?? shared.compression,
    source: "job",
  };
}
