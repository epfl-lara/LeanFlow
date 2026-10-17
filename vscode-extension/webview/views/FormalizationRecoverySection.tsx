/** Collapse publication and infrastructure recovery while keeping unresolved observations counted. */
import { useEffect, useState } from "react";
import type { HostMessage } from "../../src/core/types";
import { FormalizationConflicts } from "./FormalizationConflicts";
import { FormalizationSection, type SectionStatus } from "./FormalizationSection";
import { plural } from "./formalizationProgress";

interface RecoverySummary { publication: number | null; infrastructure: number | null; running: boolean; publicationFailed: boolean; infrastructureFailed: boolean }

export function FormalizationRecoverySection({ ready, onResolved }: { ready: boolean; onResolved?: () => void }) {
  const [summary, setSummary] = useState<RecoverySummary>({ publication: null, infrastructure: null, running: false, publicationFailed: false, infrastructureFailed: false });
  useEffect(() => {
    // Mirror the cards' replies for counts only; the cards remain the authority for every recovery action.
    const receive = (event: MessageEvent<HostMessage>) => {
      const message = event.data;
      if (message.type === "formalizationConflicts") {
        setSummary(old => ({ ...old, publicationFailed: !!message.error,
          publication: message.snapshot ? message.snapshot.transactions.filter(row => row.state !== "RESOLVED").length : old.publication }));
      } else if (message.type === "formalizationPreparationRecovery") {
        setSummary(old => ({ ...old, running: message.busy === true, infrastructureFailed: !!message.error,
          infrastructure: message.snapshot ? message.snapshot.transactions.filter(row => row.resolvable ?? (row.state === "CONFLICTED" && row.active)).length : old.infrastructure }));
      }
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, []);
  const status: SectionStatus[] = [
    ...(summary.publication ? [{ label: plural(summary.publication, "publication conflict"), tone: "warn" as const }] : []),
    ...(summary.infrastructure ? [{ label: plural(summary.infrastructure, "infrastructure conflict"), tone: "warn" as const }] : []),
    ...(summary.running ? [{ label: "recovery running", tone: "running" as const }] : []),
    ...(summary.publicationFailed || summary.infrastructureFailed ? [{ label: "inspection failed", tone: "bad" as const }] : []),
  ];
  if (!status.length) status.push({ label: !ready ? "not checked" : summary.publication === null || summary.infrastructure === null ? "checking…" : "nothing to recover" });
  const attention = !!summary.publication || !!summary.infrastructure || summary.running || summary.publicationFailed || summary.infrastructureFailed;
  return <FormalizationSection id="formalization-recovery" title="Publication and preparation recovery" status={status} attention={attention}>
    <FormalizationConflicts ready={ready} onResolved={onResolved} />
  </FormalizationSection>;
}
