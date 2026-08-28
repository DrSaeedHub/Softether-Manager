/**
 * Machine state, rendered.
 *
 * Colour reinforces; it never carries. Every pill has a text label and a glyph
 * whose *silhouette* differs, so the states survive protanopia, deuteranopia
 * and a black-and-white screenshot.
 *
 * Only genuinely transient states animate, and the animation is keyed on the
 * status string rather than the poll response, so a refresh that returns the
 * same value does not restart it and make the dot stutter.
 */

export type Kind = "ok" | "warn" | "err" | "busy" | "idle";

/** Liveness of a managed SoftEther server, as the probe reports it. */
const SERVER_KIND: Record<string, Kind> = {
  online: "ok",
  probing: "busy",
  offline: "err",
  unknown: "idle",
};

/** An install job's lifecycle. */
const JOB_KIND: Record<string, Kind> = {
  pending: "busy",
  running: "busy",
  succeeded: "ok",
  failed: "err",
  canceled: "warn",
};

/** The data-state value that drives a row's signal rail. */
export function serverState(status: string): string {
  const k = SERVER_KIND[status] ?? "idle";
  return k === "ok" ? "connected" : k === "err" ? "error" : k === "busy" ? "busy" : "disabled";
}

export function Pill({ kind, label }: { kind: Kind; label: string }) {
  const live = kind === "busy";
  return (
    <span className={`pill pill--${kind}${live ? " pill--live" : ""}`}>
      {/* A healthy state gets a check, everything else a dot -- the label
          always carries the word, so colour never stands alone. */}
      {kind === "ok" ? (
        <span className="pill__i" aria-hidden="true">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="9" strokeWidth="2" />
            <path d="m8.5 12.5 2.5 2.5 4.5-5" />
          </svg>
        </span>
      ) : (
        <span className="pill__g" aria-hidden="true" />
      )}
      {label}
    </span>
  );
}

export function ServerStatusPill({ status }: { status: string }) {
  return <Pill kind={SERVER_KIND[status] ?? "idle"} label={status} />;
}

export function JobStatusPill({ status }: { status: string }) {
  return <Pill kind={JOB_KIND[status] ?? "idle"} label={status} />;
}

/** Online / offline for hubs, sessions, links -- anything with a boolean life. */
export function OnlinePill({ online, onLabel = "online", offLabel = "offline" }: {
  online: boolean;
  onLabel?: string;
  offLabel?: string;
}) {
  return <Pill kind={online ? "ok" : "idle"} label={online ? onLabel : offLabel} />;
}
