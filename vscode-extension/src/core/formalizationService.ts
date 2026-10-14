/** Read controller-owned inventories and submit revision-bound repairs without editing state files. */
import {
  parseFormalizationSnapshot,
  type FormalizationAction,
  type FormalizationArtifact,
  type FormalizationSnapshot,
} from "./formalization";

type Transport = (args: string[], options: { cwd: string }) => Promise<string>;

export class FormalizationService {
  constructor(private readonly projectRoot: () => string, private readonly transport: Transport) {}

  private async call(args: string[]): Promise<unknown> {
    const cwd = this.projectRoot();
    if (!cwd) throw new Error("Open a LeanFlow project to inspect formalization sources.");
    return JSON.parse(await this.transport(["formalization", ...args, "--json"], { cwd }));
  }

  /** Return the backend's current document list, without reading its private registry. */
  async list(): Promise<FormalizationSnapshot[]> {
    const value = await this.call(["list"]) as { schema_version?: number; documents?: unknown[] };
    if (value.schema_version !== 1 || !Array.isArray(value.documents)) throw new Error("Invalid formalization document list.");
    return value.documents.map(parseFormalizationSnapshot);
  }

  async show(documentId: string): Promise<FormalizationSnapshot> {
    const snapshot = parseFormalizationSnapshot(await this.call(["show", documentId]));
    if (snapshot.document_id !== documentId) throw new Error("The formalization service returned a different document.");
    return snapshot;
  }

  /** Intake freezes source evidence but never launches a model or writes Lean proposals. */
  async inspect(source: string, companionPdf?: string): Promise<FormalizationSnapshot> {
    return parseFormalizationSnapshot(await this.call(["inspect", source, ...(companionPdf ? ["--companion-pdf", companionPdf] : [])]));
  }

  async action(documentId: string, revisionId: string, action: FormalizationAction): Promise<FormalizationSnapshot> {
    const snapshot = parseFormalizationSnapshot(await this.call([
      "action", documentId, "--expected-revision", revisionId, `--action-json=${JSON.stringify(action)}`,
    ]));
    if (snapshot.document_id !== documentId) throw new Error("The repair returned a different source inventory.");
    return snapshot;
  }

  /** Ask the controller to resolve an allowlisted artifact; paths never come from the webview. */
  async artifact(documentId: string, revisionId: string, artifactId: string): Promise<FormalizationArtifact> {
    const artifact = await this.call([
      "artifact", documentId, artifactId, "--expected-revision", revisionId,
    ]) as FormalizationArtifact;
    if (artifact.id !== artifactId || typeof artifact.path !== "string" ||
        !/^(?:sha256:)?[a-f0-9]{64}$/i.test(artifact.sha256)) {
      throw new Error("The source artifact has no valid controller identity.");
    }
    return artifact;
  }
}
