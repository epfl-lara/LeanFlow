/**
 * Inline monochrome icons.
 *
 * Bundled as components rather than loaded as assets: the webview's CSP blocks
 * external requests, and `currentColor` lets every icon inherit whatever the
 * user's theme sets for the surrounding text.
 */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 16, children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

/**
 * The LeanFlow mark: a turnstile ⊢ closing on a QED square ∎ — from assumptions
 * to a discharged obligation.
 *
 * Deliberately three elements. It must stay identical to media/leanflow.svg
 * (activity bar) and media/leanflow-{light,dark}.svg (editor tab), which VS
 * Code paints as a flat 24px mask in the activity bar; anything busier turns
 * to mush at that size.
 */
export function Mark({ size = 16, ...rest }: IconProps) {
  return (
    <Svg size={size} strokeWidth={2.2} {...rest}>
      <path d="M4.6 3.4v17.2" />
      <path d="M4.6 12h8.2" />
      <rect x="15" y="8.2" width="7.6" height="7.6" rx="1.2" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function PlayIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6.5 4.8 19 12 6.5 19.2z" />
    </Svg>
  );
}

export function PulseIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2.5 12h4l2.5-6.5L13 18.5l2.4-6.5h6.1" />
    </Svg>
  );
}

export function LogsIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 6h10M4 12h16M4 18h7" />
    </Svg>
  );
}

export function KnobsIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 7h9M17 7h3M4 17h3M11 17h9" />
      <circle cx="15" cy="7" r="2.2" />
      <circle cx="9" cy="17" r="2.2" />
    </Svg>
  );
}

export function SweepIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.2" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="1.2" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.2" />
      <rect
        x="13.5"
        y="13.5"
        width="7"
        height="7"
        rx="1.2"
        fill="currentColor"
        stroke="none"
      />
    </Svg>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M20 11a8 8 0 1 0-.9 4.5" />
      <path d="M20 4.5V11h-6" />
    </Svg>
  );
}

export function StopIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="6" y="6" width="12" height="12" rx="1.6" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m15.5 15.5 5 5" />
    </Svg>
  );
}

export function ChevronIcon({ open, ...props }: IconProps & { open?: boolean }) {
  return (
    <Svg {...props} style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .12s ease" }}>
      <path d="m9 5 7 7-7 7" />
    </Svg>
  );
}
