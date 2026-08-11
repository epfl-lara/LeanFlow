/** Keep resumable experiment prompts out of workspace metadata and exports. */
import * as crypto from "node:crypto";

import type { ExperimentMatrix } from "./types";

export interface ExperimentPromptEnvelope {
  promptPresent: boolean;
  promptSha256: string | null;
}

export interface SecretStore {
  get(key: string): Thenable<string | undefined>;
  store(key: string, value: string): Thenable<void>;
  delete(key: string): Thenable<void>;
}

function canonicalPrompt(prompt: unknown): string {
  return typeof prompt === "string" ? prompt.trim() : "";
}

/** Return the non-secret metadata persisted with one frozen prompt. */
export function experimentPromptEnvelope(prompt: string): ExperimentPromptEnvelope {
  const canonical = canonicalPrompt(prompt);
  return canonical
    ? {
        promptPresent: true,
        promptSha256: crypto.createHash("sha256").update(canonical).digest("hex"),
      }
    : { promptPresent: false, promptSha256: null };
}

/** Split one untrusted matrix into ephemeral prompt bytes and prompt-safe metadata. */
export function scrubExperimentMatrixPrompt(matrix: ExperimentMatrix): {
  matrix: ExperimentMatrix;
  prompt: string;
  envelope: ExperimentPromptEnvelope;
} {
  const prompt = canonicalPrompt(matrix.base.prompt);
  return {
    matrix: {
      ...matrix,
      base: { ...matrix.base, prompt: "" },
    },
    prompt,
    envelope: experimentPromptEnvelope(prompt),
  };
}

/** Return a bounded opaque SecretStorage key for an untrusted matrix id. */
export function experimentPromptSecretKey(matrixId: string): string {
  const identity = crypto.createHash("sha256").update(matrixId).digest("hex");
  return `leanflow.experiment-prompt.${identity}`;
}

/** Store and read back a prompt before any metadata claims it is recoverable. */
export async function storeExperimentPrompt(
  secrets: SecretStore,
  matrixId: string,
  prompt: string,
): Promise<ExperimentPromptEnvelope> {
  const canonical = canonicalPrompt(prompt);
  const envelope = experimentPromptEnvelope(canonical);
  const key = experimentPromptSecretKey(matrixId);
  if (!envelope.promptPresent) {
    await secrets.delete(key);
    return envelope;
  }
  await secrets.store(key, canonical);
  const recovered = await secrets.get(key);
  if (recovered !== canonical) {
    throw new Error("VS Code SecretStorage did not preserve the experiment prompt.");
  }
  return envelope;
}

/** Recover a prompt only when its stored bytes match the persisted digest. */
export async function readExperimentPrompt(
  secrets: SecretStore,
  matrixId: string,
  envelope: ExperimentPromptEnvelope,
): Promise<string> {
  if (!envelope.promptPresent) {
    if (envelope.promptSha256 !== null) {
      throw new Error("The prompt marker is inconsistent with its content digest.");
    }
    return "";
  }
  if (!envelope.promptSha256 || !/^[0-9a-f]{64}$/.test(envelope.promptSha256)) {
    throw new Error("The experiment prompt digest is missing or invalid.");
  }
  const prompt = await secrets.get(experimentPromptSecretKey(matrixId));
  if (prompt === undefined) {
    throw new Error(
      "The encrypted experiment prompt is missing. Create a new sweep to run pending cells.",
    );
  }
  const actual = experimentPromptEnvelope(prompt);
  if (actual.promptSha256 !== envelope.promptSha256) {
    throw new Error(
      "The encrypted experiment prompt does not match the frozen content digest.",
    );
  }
  return canonicalPrompt(prompt);
}

/** Remove the encrypted prompt associated with a deleted experiment. */
export async function deleteExperimentPrompt(
  secrets: SecretStore,
  matrixId: string,
): Promise<void> {
  await secrets.delete(experimentPromptSecretKey(matrixId));
}
