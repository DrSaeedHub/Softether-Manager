/**
 * The icon set, drawn for this product.
 *
 * 24-grid, 1.75 stroke, round caps and joins — soft enough to sit with the
 * rounded glass without reading as engineering schematics. They inherit
 * `currentColor` and carry no colour of their own.
 */
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function S({ size = 18, children, ...rest }: P) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
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

/* ── navigation ─────────────────────────────────────────────────────────── */

/** Panels — stacked plates seen edge-on. */
export const IconPanels = (p: P) => (
  <S {...p}>
    <path d="M3 7.5 12 3l9 4.5-9 4.5-9-4.5Z" />
    <path d="M3 12.5 12 17l9-4.5" />
    <path d="M3 17 12 21.5 21 17" opacity={0.45} />
  </S>
);

/** Node — a machine with a status lamp. */
export const IconNode = (p: P) => (
  <S {...p}>
    <rect x="3" y="4" width="18" height="7" rx="2.5" />
    <rect x="3" y="13" width="18" height="7" rx="2.5" />
    <path d="M6.5 7.5h.01M6.5 16.5h.01" strokeWidth={2.4} strokeLinecap="round" />
  </S>
);

/** Settings — a patch bay of sliders. */
export const IconSettings = (p: P) => (
  <S {...p}>
    <path d="M5 3v7M5 14v7M12 3v4M12 11v10M19 3v11M19 18v3" />
    <path d="M2.5 10h5M9.5 7h5M16.5 14h5" />
  </S>
);

/* ── the machine ────────────────────────────────────────────────────────── */

/** CPU — the chip, pins out. */
export const IconCpu = (p: P) => (
  <S {...p}>
    <rect x="6.5" y="6.5" width="11" height="11" rx="2" />
    <rect x="10" y="10" width="4" height="4" rx="1" />
    <path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3" />
  </S>
);

/** Memory — a RAM stick, chips on board. */
export const IconMemory = (p: P) => (
  <S {...p}>
    <rect x="3" y="7" width="18" height="9" rx="1.5" />
    <path d="M7 10v3M11 10v3M15 10v3" opacity={0.75} />
    <path d="M5.5 16v2.5M9.5 16v2.5M13.5 16v2.5M17.5 16v2.5" />
  </S>
);

/** Swap — two lanes trading places. */
export const IconSwap = (p: P) => (
  <S {...p}>
    <path d="M4 8.5h13M14 4.5l3.5 4-3.5 4" />
    <path d="M20 15.5H7M10 11.5l-3.5 4 3.5 4" />
  </S>
);

/** Disk — a platter drive, head parked. */
export const IconDisk = (p: P) => (
  <S {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <circle cx="12" cy="12" r="2.5" />
    <path d="M14 14.5 18.5 19" />
  </S>
);

/** Network traffic — down and up. */
export const IconTraffic = (p: P) => (
  <S {...p}>
    <path d="M8 4v13M4.5 13.5 8 17.5l3.5-4" />
    <path d="M16 20V7M12.5 10.5 16 6.5l3.5 4" />
  </S>
);

/* ── actions ────────────────────────────────────────────────────────────── */

export const IconPlus = (p: P) => (
  <S {...p}>
    <path d="M12 4.5v15M4.5 12h15" />
  </S>
);

export const IconChevron = (p: P) => (
  <S {...p}>
    <path d="m9 5 7 7-7 7" />
  </S>
);

export const IconBack = (p: P) => (
  <S {...p}>
    <path d="m14 5-7 7 7 7" />
  </S>
);

export const IconClose = (p: P) => (
  <S {...p}>
    <path d="m5 5 14 14M19 5 5 19" />
  </S>
);

export const IconCheck = (p: P) => (
  <S {...p}>
    <path d="m4.5 12.5 5 5 10-11" />
  </S>
);

/** Sync — reconcile two sides. */
export const IconSync = (p: P) => (
  <S {...p}>
    <path d="M20 11a8 8 0 0 0-13.7-5.3M4 13a8 8 0 0 0 13.7 5.3" />
    <path d="M20 4.5V11h-6.5M4 19.5V13h6.5" />
  </S>
);

/** Install — current through a wire. */
export const IconBolt = (p: P) => (
  <S {...p}>
    <path d="M13 2.5 4.5 13.5H11l-1 8 8.5-11H12l1-8Z" />
  </S>
);

export const IconKey = (p: P) => (
  <S {...p}>
    <circle cx="8" cy="8" r="4" />
    <path d="m11 11 8.5 8.5M16 16l2.5-2.5M18.5 18.5 21 16" />
  </S>
);

export const IconTerminal = (p: P) => (
  <S {...p}>
    <rect x="2.5" y="4" width="19" height="16" rx="3" />
    <path d="m6.5 9 3 3-3 3M13 15h4.5" />
  </S>
);

export const IconTrash = (p: P) => (
  <S {...p}>
    <path d="M4 6.5h16M9.5 6.5V4h5v2.5" />
    <path d="M6.5 6.5 7.5 20h9l1-13.5" />
    <path d="M10.5 10v6M13.5 10v6" opacity={0.5} />
  </S>
);

export const IconMore = (p: P) => (
  <S {...p} strokeWidth={2.4} strokeLinecap="round">
    <path d="M12 5.5h.01M12 12h.01M12 18.5h.01" />
  </S>
);

export const IconCopy = (p: P) => (
  <S {...p}>
    <rect x="8" y="8" width="12" height="12" rx="3" />
    <path d="M16 5.5H4v12" opacity={0.6} />
  </S>
);

export const IconRefresh = (p: P) => (
  <S {...p}>
    <path d="M20 12a8 8 0 1 1-2.3-5.6" />
    <path d="M20 3.5V9h-5.5" />
  </S>
);

export const IconDownload = (p: P) => (
  <S {...p}>
    <path d="M12 3.5v12M7 11l5 4.5 5-4.5" />
    <path d="M4 20h16" />
  </S>
);

export const IconLink = (p: P) => (
  <S {...p}>
    <path d="M14 4.5h5.5V10" />
    <path d="M19.5 4.5 11 13" />
    <path d="M18 14v5.5H4.5V6H10" />
  </S>
);

export const IconAlert = (p: P) => (
  <S {...p}>
    <path d="M12 3.5 22 20H2L12 3.5Z" />
    <path d="M12 10v4.5M12 17.5h.01" strokeLinecap="round" />
  </S>
);

export const IconSignOut = (p: P) => (
  <S {...p}>
    <path d="M10 4.5H4.5v15H10" />
    <path d="M14 8.5 17.5 12 14 15.5M17.5 12H8" />
  </S>
);

/* ── theme ──────────────────────────────────────────────────────────────── */

export const IconSun = (p: P) => (
  <S {...p} strokeLinecap="round">
    <circle cx="12" cy="12" r="4.2" />
    <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.2 5.2l1.6 1.6M17.2 17.2l1.6 1.6M18.8 5.2l-1.6 1.6M6.8 17.2l-1.6 1.6" />
  </S>
);

export const IconMoon = (p: P) => (
  <S {...p} strokeLinejoin="round">
    <path d="M20.5 14.2A8.5 8.5 0 1 1 9.8 3.5a6.7 6.7 0 0 0 10.7 10.7Z" />
  </S>
);

/* ── brand ──────────────────────────────────────────────────────────────── */

/* ── product icons ──────────────────────────────────────────────────────── */

/** Hub — a switch seen as a ring of ports around a core. */
export const IconHub = (p: P) => (
  <S {...p}>
    <circle cx="12" cy="12" r="3" />
    <circle cx="12" cy="4" r="1.6" />
    <circle cx="12" cy="20" r="1.6" />
    <circle cx="4" cy="12" r="1.6" />
    <circle cx="20" cy="12" r="1.6" />
    <path d="M12 9V5.6M12 18.4V15M9 12H5.6M18.4 12H15" />
  </S>
);

/** Users — two heads, the second set back. */
export const IconUsers = (p: P) => (
  <S {...p}>
    <circle cx="9" cy="8" r="3.4" />
    <path d="M3.5 20c.6-3.4 2.8-5.2 5.5-5.2S13.9 16.6 14.5 20" />
    <path d="M15.5 5.2a3.4 3.4 0 0 1 0 5.9M17.4 15.1c1.7.8 2.8 2.5 3.1 4.9" opacity={0.55} />
  </S>
);

/** Sessions — a live pulse line. */
export const IconPulse = (p: P) => (
  <S {...p}>
    <path d="M2.5 12h4l2.5-6.5L13 18l2.5-6h6" />
  </S>
);

/** Shield — access control and certificates. */
export const IconShield = (p: P) => (
  <S {...p}>
    <path d="M12 3 5 5.8v5.4c0 4.4 2.9 7.6 7 9.3 4.1-1.7 7-4.9 7-9.3V5.8L12 3Z" />
    <path d="m9 11.6 2.2 2.2 3.8-4.2" />
  </S>
);

/** Deploy — an up-and-out arrow leaving a box. */
export const IconDeploy = (p: P) => (
  <S {...p}>
    <path d="M4 14v4.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V14" />
    <path d="M12 15V4M7.5 8.5 12 4l4.5 4.5" />
  </S>
);

/** NAT — packets translated across a boundary. */
export const IconNat = (p: P) => (
  <S {...p}>
    <path d="M12 3.5v17" strokeDasharray="2.4 3" opacity={0.6} />
    <path d="M3 9h11M11.2 5.8 14.5 9l-3.3 3.2" />
    <path d="M21 15H10M12.8 18.2 9.5 15l3.3-3.2" />
  </S>
);

/** Cascade — two hubs bridged. */
export const IconCascade = (p: P) => (
  <S {...p}>
    <circle cx="6" cy="7" r="3" />
    <circle cx="18" cy="17" r="3" />
    <path d="M8.3 9.1c2.6 2 5 4 7.4 5.8" />
  </S>
);

/** Table — an address table. */
export const IconTable = (p: P) => (
  <S {...p}>
    <rect x="3.5" y="5" width="17" height="14" rx="2" />
    <path d="M3.5 10h17M9.5 10v9" />
  </S>
);

/** Logs — lines on a page. */
export const IconLogs = (p: P) => (
  <S {...p}>
    <rect x="4.5" y="3.5" width="15" height="17" rx="2" />
    <path d="M8 8h8M8 12h8M8 16h5" />
  </S>
);

/** Search. */
export const IconSearch = (p: P) => (
  <S {...p}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m20.5 20.5-4.9-4.9" />
  </S>
);

/** Play / start. */
export const IconPlay = (p: P) => (
  <S {...p}>
    <path d="M8 5.5v13l10-6.5-10-6.5Z" />
  </S>
);

/** Stop. */
export const IconStop = (p: P) => (
  <S {...p}>
    <rect x="6.5" y="6.5" width="11" height="11" rx="2" />
  </S>
);

/** Upload — restore a config. */
export const IconUpload = (p: P) => (
  <S {...p}>
    <path d="M4 14v4.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V14" />
    <path d="M12 4v11M7.5 8.5 12 4l4.5 4.5" />
  </S>
);

/** Globe — DDNS, listeners, the network at large. */
export const IconGlobe = (p: P) => (
  <S {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M3.5 12h17M12 3.5c-2.4 2.4-3.6 5.2-3.6 8.5s1.2 6.1 3.6 8.5c2.4-2.4 3.6-5.2 3.6-8.5S14.4 5.9 12 3.5Z" />
  </S>
);

/**
 * The mark: a hub ring with spokes -- SoftEther's virtual hub, as a badge --
 * solid accent with white strokes, per the system.
 */
export function BrandMark({ size = 32 }: { size?: number }) {
  return (
    <span className="brandmark" aria-hidden="true">
      {/* The mark matches the deck: a lime coin with SoftEther's four-node
          ring -- machines joined into one network -- pressed into it in dark
          ink. The same figure as the app icon, drawn in the panel's theme. */}
      <svg width={size} height={size} viewBox="0 0 32 32" fill="none">
        <defs>
          <linearGradient id="bm-g" x1="4" y1="4" x2="28" y2="28" gradientUnits="userSpaceOnUse">
            <stop stopColor="var(--action-bg-hi)" />
            <stop offset="1" stopColor="var(--action-bg)" />
          </linearGradient>
        </defs>
        <rect width="32" height="32" rx="9.5" fill="url(#bm-g)" />
        <rect
          x="10.7"
          y="10.7"
          width="10.6"
          height="10.6"
          stroke="var(--action-fg)"
          strokeWidth="2"
          strokeLinejoin="round"
        />
        <circle cx="10.7" cy="10.7" r="2.8" fill="var(--action-fg)" />
        <circle cx="21.3" cy="10.7" r="2.8" fill="var(--action-fg)" />
        <circle cx="10.7" cy="21.3" r="2.8" fill="var(--action-fg)" />
        <circle cx="21.3" cy="21.3" r="2.8" fill="var(--action-fg)" />
      </svg>
    </span>
  );
}
