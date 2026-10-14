/** Keep preparation plans, progress and final source inventories distinct from translation acceptance. */
import type { FormalizationSnapshot } from "./formalization";

export type FormalizationPreparationPreset = "mathlib" | "lean";
export interface FormalizationPreparationPlan {
  plan_id?: string;
  preset?: string;
  toolchain?: string;
  layout?: { library?: string; root_file?: string; mode?: string };
  packages?: { name: string; url: string; revision: string; reason?: string; imports?: string[] }[];
  existing_packages?: { name: string; rev?: string }[];
  required_imports?: string[];
  mathematical_prerequisites?: unknown[];
  slice_source_ids?: string[];
  blockers?: string[];
  representation_notes?: string[];
}

export interface FormalizationPreparationState {
  type: "formalizationPreparation";
  documentId: string;
  revisionId: string;
  status: "planning" | "planned" | "preparing" | "prepared" | "failed" | "cancelled";
  phase?: string;
  cancellable?: boolean;
  message: string;
  error: string;
  plan?: FormalizationPreparationPlan;
  snapshot?: FormalizationSnapshot | null;
}
