/** Record explicit open-claim proof permission independently of review and proof evidence. */
import { useState } from "react";
import { proofAuthorizationLabel, type FormalizationAction, type FormalizationItem } from "../../src/core/formalization";

export function FormalizationProofAuthorization({ item, disabled, submit }: {
  item: FormalizationItem; disabled: boolean; submit: (action: FormalizationAction) => void;
}) {
  const [reason, setReason] = useState("");
  const authorized = item.proof_authorization?.authorized === true;
  const requiresPermission = ["conjectured", "open_question", "unknown", "unclassified", "open", "conjecture"].includes(item.claim_status || "unclassified");
  return <section aria-label="Proof attempt permission">
    <h4>Proof attempt permission</h4>
    <p>{proofAuthorizationLabel(item)}</p>
    {item.proof_authorization?.reason && <p>Recorded reason: {item.proof_authorization.reason}</p>}
    {(requiresPermission || authorized) && <>
      <p>An open or unclassified claim needs your explicit permission before it can be assigned to the prover. Changes create a working revision that must pass the acceptance gates.</p>
      <label>Reason for proof permission<textarea aria-label="Proof permission reason" value={reason} maxLength={16000} onChange={event => setReason(event.target.value)} /></label>
      <button className="btn" disabled={disabled || !reason.trim()} onClick={() => submit({ type: "authorize_proof", item_id: item.id, authorized: !authorized, reason })}>
        {authorized ? "Revoke proof attempt permission" : "Allow proof attempt for this open claim"}
      </button>
    </>}
  </section>;
}
