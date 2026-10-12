/** Accessible, click- and keyboard-operated explanations beside unfamiliar terms. */
import type { ReactNode } from "react";

export function InfoHelp({ label, children }: { label: string; children: ReactNode }) {
  return <details className="info-help">
    <summary aria-label={`About ${label}`} title={`About ${label}`}>ⓘ</summary>
    <div className="info-help-body" role="note">{children}</div>
  </details>;
}
