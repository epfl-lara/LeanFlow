/** Collapse one Formalization subsection while its summary keeps counts and blockers visible. */
import { useEffect, useState, type ReactNode } from "react";

export interface SectionStatus { label: string; tone?: "ok" | "warn" | "bad" | "running" }

export function FormalizationSection({ id, title, status = [], attention = false, defaultOpen = false, revealKey = 0, children }: {
  id: string; title: string; status?: SectionStatus[]; attention?: boolean; defaultOpen?: boolean; revealKey?: number; children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen || attention);
  // Open when a blocker appears or the user asks for this section again, but never collapse it for them.
  useEffect(() => { if (attention) setOpen(true); }, [attention]);
  useEffect(() => { if (revealKey) setOpen(true); }, [revealKey]);
  return <details id={id} className="card formalization-section" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><strong>{title}</strong>{status.map((entry, index) => <span key={index}> <span className={entry.tone ? `pill ${entry.tone}` : "pill"}>{entry.label}</span></span>)}</summary>
    {children}
  </details>;
}

/** Expand a collapsed section before in-page navigation scrolls to it. */
export function revealFormalizationSection(id: string): void {
  const target = document.getElementById(id);
  if (target?.tagName === "DETAILS") (target as HTMLDetailsElement).open = true;
}
