/** Accessible, click- and keyboard-operated explanations beside unfamiliar terms. */
import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { helpPosition } from "./helpPosition";

export function InfoHelp({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  const anchor = useRef<HTMLDetailsElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<ReturnType<typeof helpPosition> | null>(null);
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      if (!anchor.current || !popup.current) return;
      const bounds = anchor.current.getBoundingClientRect();
      const size = helpPosition(bounds, 0, window.innerWidth, window.innerHeight);
      // Reflow to the new width before measuring, including after a resize.
      popup.current.style.width = `${size.width}px`;
      popup.current.style.maxHeight = `${size.maxHeight}px`;
      setPosition(helpPosition(bounds, popup.current.getBoundingClientRect().height, window.innerWidth, window.innerHeight));
    };
    const close = () => {
      if (anchor.current) anchor.current.open = false;
      setOpen(false);
      setPosition(null);
    };
    const outside = (event: PointerEvent) => {
      if (!anchor.current?.contains(event.target as Node) && !popup.current?.contains(event.target as Node)) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        close();
        anchor.current?.querySelector("summary")?.focus();
      }
    };
    const scroll = (event: Event) => { if (!popup.current?.contains(event.target as Node)) close(); };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", scroll, true);
    window.addEventListener("pointerdown", outside, true);
    window.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", scroll, true);
      window.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("keydown", escape);
    };
  }, [open]);
  return <details className="info-help" ref={anchor} onToggle={event => { setOpen(event.currentTarget.open); if (!event.currentTarget.open) setPosition(null); }}>
    <summary aria-label={`About ${label}`} aria-controls={id} title={`About ${label}`}>ⓘ</summary>
    {open && createPortal(<div id={id} ref={popup} className="info-help-body" role="note" style={{ ...position, visibility: position ? "visible" : "hidden" }}>{children}</div>, document.body)}
  </details>;
}
