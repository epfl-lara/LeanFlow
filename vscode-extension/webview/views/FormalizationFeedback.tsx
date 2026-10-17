/** Keep repair saves and currentness checks visible until the user has seen their outcome. */
import type { FormalizationAction, FormalizationSnapshot } from "../../src/core/formalization";
import { formalizationRequestChannel } from "../../src/core/formalizationScope";

interface Revision { documentId: string; revisionId: string }

export type FormalizationNotice =
  | Revision & { kind: "saving"; label: string; superseded: boolean }
  | Revision & { kind: "saved"; previousRevisionId: string; label: string; refreshed: boolean; refreshError: string }
  | Revision & { kind: "unsaved"; label: string; error: string }
  | Revision & { kind: "checked" }
  | Revision & { kind: "checkFailed"; error: string };

export interface FormalizationFeedback { checking: Revision | null; notice: FormalizationNotice | null }

export const NO_FORMALIZATION_FEEDBACK: FormalizationFeedback = { checking: null, notice: null };

export type FormalizationFeedbackEvent =
  | { type: "submit"; documentId: string; revisionId: string; action: FormalizationAction }
  | { type: "check"; documentId: string; revisionId: string }
  | { type: "supersede" }
  | { type: "committed"; documentId: string; previousRevisionId: string; revisionId: string }
  | { type: "state"; snapshot: FormalizationSnapshot | null; error: string; channel?: string }
  | { type: "dismiss" }
  | { type: "reset" };

const DOCUMENT_CHANNEL = formalizationRequestChannel("formalizationAction");
const CHECK_CHANNEL = formalizationRequestChannel("checkFormalization");

const ACTION_LABELS: Partial<Record<FormalizationAction["type"], string>> = {
  retry_extraction: "extraction retry",
  resolve_command: "command interpretation",
  resolve_conditional: "conditional interpretation",
  authorize_proof: "proof permission",
  record_correction_authority: "correction decision",
  transcribe_page: "page transcription",
  replace_page_transcription: "page transcription",
  set_scope: "scope change",
  classify_region: "region disposition",
  add_item: "new source item",
  update_boundary: "source boundary",
  set_mapping: "Lean mapping",
  select_revision: "revision selection",
};

/** Repairs stay locked from submission until the refreshed controller view, or a failure, arrives. */
export function formalizationSavePending(feedback: FormalizationFeedback): boolean {
  const notice = feedback.notice;
  return notice?.kind === "saving" || notice?.kind === "saved" && !notice.refreshed && !notice.refreshError;
}

export function reduceFormalizationFeedback(state: FormalizationFeedback, event: FormalizationFeedbackEvent): FormalizationFeedback {
  switch (event.type) {
    case "reset":
      return NO_FORMALIZATION_FEEDBACK;
    case "dismiss":
      return formalizationSavePending(state) ? state : { ...state, notice: null };
    case "submit":
      // A newer document request makes the transport discard any pending currentness reply.
      return { checking: null, notice: { kind: "saving", documentId: event.documentId, revisionId: event.revisionId, label: ACTION_LABELS[event.action.type] ?? "change", superseded: false } };
    case "check":
      return { ...state, checking: { documentId: event.documentId, revisionId: event.revisionId } };
    case "supersede":
      return { checking: null, notice: state.notice?.kind === "saving" ? { ...state.notice, superseded: true } : state.notice?.kind === "checked" ? null : state.notice };
    case "committed": {
      const pending = state.notice?.kind === "saving" && state.notice.documentId === event.documentId ? state.notice : null;
      return { ...state, notice: { kind: "saved", documentId: event.documentId, previousRevisionId: event.previousRevisionId, revisionId: event.revisionId, label: pending?.label ?? "change", refreshed: false, refreshError: "" } };
    }
    case "state": {
      const check = state.checking;
      const snapshot = event.snapshot;
      const fromCheck = event.channel === undefined ? undefined : event.channel === CHECK_CHANNEL;
      // Unscoped replies come from older hosts and fixtures; a checked host reply is never historical.
      if (check && (fromCheck ?? (!!event.error || !snapshot || snapshot.currentness !== "historical" || snapshot.document_id !== check.documentId || snapshot.revision_id !== check.revisionId))) {
        const outcome: FormalizationNotice | null = event.error ? { kind: "checkFailed", ...check, error: event.error }
          : snapshot?.currentness === "checked" ? { kind: "checked", documentId: snapshot.document_id, revisionId: snapshot.revision_id } : state.notice;
        return { checking: null, notice: outcome };
      }
      if (event.channel !== undefined && event.channel !== DOCUMENT_CHANNEL) return state;
      const notice = state.notice;
      if (notice?.kind === "saving") {
        // Without a commit acknowledgment, claim neither success nor a definite failure.
        return { ...state, notice: notice.superseded || !event.error ? null : { kind: "unsaved", documentId: notice.documentId, revisionId: notice.revisionId, label: notice.label, error: event.error } };
      }
      if (notice?.kind === "saved" && !notice.refreshed) {
        return { ...state, notice: event.error ? { ...notice, refreshError: event.error } : { ...notice, refreshed: true, refreshError: "" } };
      }
      return state;
    }
  }
}

const STICKY = { position: "sticky", top: 0, zIndex: 2, background: "var(--vscode-editor-background)" } as const;

/** The live region stays mounted so the first status change is announced. */
export function FormalizationFeedbackBar({ feedback, errorVisible, checkedTranslation, onDismiss }: {
  feedback: FormalizationFeedback; errorVisible: boolean; checkedTranslation?: string; onDismiss: () => void;
}) {
  const { checking, notice } = feedback;
  const failed = notice?.kind === "unsaved" || notice?.kind === "checkFailed" || notice?.kind === "saved" && !!notice.refreshError;
  const content = checking || notice;
  const details = errorVisible ? <> <a href="#formalization-error">Show error</a></> : null;
  return <div role="status" aria-live="polite" className={content ? `notice ${failed ? "warn" : "info"}` : undefined} style={content ? STICKY : { margin: 0 }}>
    {checking && <p>Checking currentness for revision <code>{checking.revisionId}</code>… Source navigation and repairs remain available.</p>}
    {notice && <p>
      {notice.kind === "saving" ? notice.superseded ? "Waiting for the refreshed inventory after your change…" : <>Saving {notice.label} for revision <code>{notice.revisionId}</code>… Repairs and launches wait for the controller.</>
        : notice.kind === "saved" ? <>Saved {notice.label} as revision <code>{notice.revisionId}</code>.{notice.refreshError ? <> The view did not refresh; use Refresh inventory to load it.{details}</> : notice.refreshed ? "" : " Loading the updated view…"}</>
        : notice.kind === "unsaved" ? <>No save was confirmed for {notice.label}.{details}</>
        : notice.kind === "checked" ? <>Currentness checked for revision <code>{notice.revisionId}</code>.{checkedTranslation ? ` Translation: ${checkedTranslation}.` : ""}</>
        : <>The currentness check did not finish.{details}</>}
      {!formalizationSavePending(feedback) && <> <button className="link" aria-label="Dismiss formalization status" onClick={onDismiss}>Dismiss</button></>}
    </p>}
  </div>;
}
