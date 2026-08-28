"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { IconClose } from "./Icon";

/**
 * One overlay, two shapes.
 *
 * Below 960px it is a bottom sheet: it rises from the edge, has a grab handle,
 * and follows your finger 1:1 when you drag it down — past a quarter of its
 * height, or fast enough, it dismisses. At and above 960px the same component
 * is a centred dialog with the chamfer that marks a plate.
 *
 * Sheets are for *decisions*. Tasks and drill-downs are pushed routes instead.
 */

const DISMISS_FRACTION = 0.25;
/** px/ms — a flick past this dismisses regardless of distance travelled. */
const DISMISS_VELOCITY = 0.11;

export function Sheet({
  title,
  subtitle,
  onClose,
  children,
  footer,
  wide,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [closing, setClosing] = useState(false);
  const drag = useRef<{ y: number; t: number; active: boolean } | null>(null);

  // Play the exit animation before unmounting, so the sheet leaves the way it
  // arrived instead of popping out.
  const dismiss = useCallback(() => {
    if (closing) return;
    setClosing(true);
    window.setTimeout(onClose, 200);
  }, [closing, onClose]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);

    // Scroll lock. `overflow: hidden` on body is unreliable on iOS, so the
    // page is pinned at its current offset and restored on close.
    const y = window.scrollY;
    const { style } = document.body;
    const prev = style.cssText;
    style.cssText = `position:fixed;top:${-y}px;left:0;right:0;overflow:hidden;width:100%`;

    return () => {
      window.removeEventListener("keydown", onKey);
      style.cssText = prev;
      window.scrollTo(0, y);
    };
  }, [dismiss]);

  // Drag-to-dismiss — mobile only; the desktop dialog has no grab handle and
  // pointer-down never starts a drag there.
  const onPointerDown = (e: React.PointerEvent) => {
    if (window.innerWidth >= 960) return;
    drag.current = { y: e.clientY, t: performance.now(), active: true };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    const el = panel.current;
    if (!d?.active || !el) return;
    const dy = Math.max(0, e.clientY - d.y);
    el.classList.add("sheet--drag");
    el.style.transform = `translateY(${dy}px)`;
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current;
    const el = panel.current;
    if (!d?.active || !el) return;
    drag.current = null;

    const dy = Math.max(0, e.clientY - d.y);
    const dt = Math.max(1, performance.now() - d.t);
    const velocity = dy / dt;
    const past = dy > el.offsetHeight * DISMISS_FRACTION;

    el.classList.remove("sheet--drag");
    el.style.transform = "";
    if (past || velocity > DISMISS_VELOCITY) dismiss();
  };

  return (
    <>
      <div className={`scrim${closing ? " scrim--out" : ""}`} onMouseDown={dismiss} />
      <div
        ref={panel}
        className={`sheet${wide ? " sheet--wide" : ""}${closing ? " sheet--out" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div
          className="sheet__grab"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        />
        <div
          className="sheet__head"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <div style={{ minWidth: 0 }}>
            <h2 className="sheet__title">{title}</h2>
            {subtitle && <p className="sheet__sub">{subtitle}</p>}
          </div>
          <button className="icon-btn" onClick={dismiss} aria-label="Close">
            <IconClose size={17} />
          </button>
        </div>
        <div className="sheet__body">{children}</div>
        {footer && <div className="sheet__foot">{footer}</div>}
      </div>
    </>
  );
}
