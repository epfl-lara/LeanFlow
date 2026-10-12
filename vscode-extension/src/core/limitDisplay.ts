/** Identify settings whose explicit empty value means no cap in editor displays. */
import { EMPTY_UNLIMITED_KNOBS } from "./launch";

export const OPTIONAL_LIMIT_KNOBS = new Set([
  ...EMPTY_UNLIMITED_KNOBS,
  ...["AGENT_CALLS", "TOKENS", "WALL_TIME_S", "PLAN_REFINEMENTS", "MAX_DECOMPOSITIONS", "MAX_RESTARTS"].map(suffix => `LEANFLOW_PROVER_${suffix}`),
]);
