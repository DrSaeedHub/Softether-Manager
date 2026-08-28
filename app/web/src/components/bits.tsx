"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type ThHTMLAttributes,
} from "react";
import { Sheet } from "../ui/Sheet";
import { IconRefresh, IconSearch } from "../ui/Icon";
import { useToast } from "../lib/toast";
import { useRate, type PollRate } from "../lib/rates";

/** The standard page header. */
export function PageHead({
  title,
  sub,
  actions,
}: {
  title: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="page__head">
      <div style={{ minWidth: 0 }}>
        <h1 className="page__title">{title}</h1>
        {sub && <div className="page__sub">{sub}</div>}
      </div>
      {actions && <div className="page__actions">{actions}</div>}
    </div>
  );
}

export function SectionTitle({ children, count, actions }: { children: ReactNode; count?: number; actions?: ReactNode }) {
  return (
    <div className="section">
      <div className="section__t">
        {children}
        {count !== undefined && <span className="section__n">{count}</span>}
      </div>
      {actions}
    </div>
  );
}

export function LoadingBlock({ label = "loading" }: { label?: string }) {
  return (
    <div className="loading">
      <span className="spin" /> {label}
    </div>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty__t">{title}</div>
      {children && <p className="empty__s">{children}</p>}
      {action}
    </div>
  );
}

export function ErrorAlert({ children }: { children: ReactNode }) {
  return <div className="alert alert--err">{children}</div>;
}

/** Label + control, the standard form row. */
export function Field({ label, hint, children, id }: { label: ReactNode; hint?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

/** A tappable card checkbox that tints accent when checked. */
export function CheckRow({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <label className="checkrow">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span style={{ minWidth: 0 }}>
        <span className="t">{label}</span>
        {hint && <span className="s">{hint}</span>}
      </span>
    </label>
  );
}

/** Search input with the icon, for tables. */
export function SearchBox({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <div style={{ position: "relative", flex: "1 1 220px", maxWidth: 340 }}>
      <span style={{ position: "absolute", insetInlineStart: 10, top: "50%", transform: "translateY(-50%)", color: "var(--text-faint)", display: "flex" }}>
        <IconSearch size={15} />
      </span>
      <input
        className="input"
        style={{ paddingInlineStart: 34 }}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        type="search"
      />
    </div>
  );
}

export function RefreshButton({ onClick, busy }: { onClick: () => void; busy?: boolean }) {
  return (
    <button className="btn btn--sm" onClick={onClick} disabled={busy} title="Refresh">
      {busy ? <span className="spin" /> : <IconRefresh size={14} />}
      Refresh
    </button>
  );
}

/**
 * The destructive-action gate: a sheet that states exactly what is about to
 * happen and, for the truly irreversible, requires the name typed back.
 */
export function ConfirmSheet({
  title,
  body,
  verb,
  typed,
  onClose,
  onConfirm,
  danger = true,
}: {
  title: string;
  body: ReactNode;
  verb: string;
  /** When set, the user must type this exact string to arm the button. */
  typed?: string;
  onClose: () => void;
  onConfirm: () => Promise<void> | void;
  danger?: boolean;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const armed = !typed || text === typed;
  const { guard } = useToast();

  const go = async () => {
    setBusy(true);
    const ok = await guard(async () => onConfirm());
    setBusy(false);
    if (ok) onClose();
  };

  return (
    <Sheet
      title={title}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className={`btn ${danger ? "btn--danger" : "btn--primary"}`} disabled={!armed || busy} onClick={go}>
            {busy && <span className="spin" />}
            {verb}
          </button>
        </>
      }
    >
      <div style={{ display: "grid", gap: "var(--s3)" }}>
        <div className="lede">{body}</div>
        {typed && (
          <Field label={<>Type <span className="mono">{typed}</span> to confirm</>}>
            <input
              className="input mono"
              value={text}
              onChange={(e) => setText(e.target.value)}
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </Field>
        )}
      </div>
    </Sheet>
  );
}

/* ── long lists, revealed a few rows at a time ──────────────────────────── */

/**
 * The audit trail and a user's session history both arrive a hundred rows at
 * a time and would otherwise bury the page they sit on. Only the newest few
 * are shown; the rest are one button away.
 */
export function useReveal(step = 5) {
  const [visible, setVisible] = useState(step);
  const reveal = useCallback(() => setVisible((v) => v + step), [step]);
  const reset = useCallback(() => setVisible(step), [step]);
  return { visible, reveal, reset };
}

/**
 * The foot of a revealed list: what is on screen, and the one button that
 * shows more. It reveals rows already loaded first and only asks the server
 * for the next batch once those run out, so the common click costs nothing.
 */
export function MoreRows({
  shown,
  loaded,
  exhausted,
  noun,
  onReveal,
  onLoad,
}: {
  shown: number;
  loaded: number;
  exhausted: boolean;
  noun: string;
  onReveal: () => void;
  onLoad: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const hidden = shown < loaded;
  if (!hidden && exhausted) return null;
  const click = async () => {
    if (hidden) {
      onReveal();
      return;
    }
    setBusy(true);
    await onLoad();
    setBusy(false);
    onReveal();
  };
  return (
    <div className="morebar">
      <span className="micro">
        {shown} of {loaded}
        {exhausted ? "" : "+"} {noun}
      </span>
      <button className="btn btn--sm" onClick={() => void click()} disabled={busy}>
        {busy && <span className="spin" />} Show more
      </button>
    </div>
  );
}

/* ── sortable tables ────────────────────────────────────────────────────── */

export interface SortState {
  /** "" means the order the server sent, which is meaningful in its own right. */
  key: string;
  dir: "asc" | "desc";
}

/** Click a header to sort by it; click the same one again to flip it. */
export function useSort(initial: SortState = { key: "", dir: "asc" }) {
  const [sort, setSort] = useState<SortState>(initial);
  const toggle = useCallback((key: string) => {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }, []);
  return { sort, toggle };
}

/** A sorted copy. Numbers compare numerically, text naturally (so user9 sorts
 *  before user10), and an unsorted state passes the rows straight through. */
export function sortRows<T>(
  rows: T[],
  sort: SortState,
  value: (row: T, key: string) => string | number,
): T[] {
  if (!sort.key) return rows;
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = value(a, sort.key);
    const y = value(b, sort.key);
    if (typeof x === "number" && typeof y === "number") return (x - y) * sign;
    return String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" }) * sign;
  });
}

/** A table header that sorts. The caret only appears on the sorted column
 *  (and under the pointer), so a wide header row stays quiet. */
export function SortTh({
  children,
  sortKey,
  sort,
  onSort,
  ...rest
}: {
  children: ReactNode;
  sortKey: string;
  sort: SortState;
  onSort: (key: string) => void;
} & ThHTMLAttributes<HTMLTableCellElement>) {
  const active = sort.key === sortKey;
  return (
    <th {...rest} aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" className={`th-sort${active ? " on" : ""}`} onClick={() => onSort(sortKey)}>
        <span>{children}</span>
        <svg
          className="th-sort__i"
          width="11"
          height="11"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d={active && sort.dir === "desc" ? "M6 9l6 6 6-6" : "M6 15l6-6 6 6"} />
        </svg>
      </button>
    </th>
  );
}

/** Key/value grid for detail blocks: micro labels, mono values. */
export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <div className="kv">
      {rows.map(([k, v], i) => (
        <div key={i}>
          <div className="micro">{k}</div>
          <div className="mono" style={{ fontSize: "var(--t-data)", overflowWrap: "anywhere" }}>
            {v ?? "—"}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Poll a loader on an interval while the tab is visible.
 *
 *  The interval is usually a tier -- "live", "detail", "list" -- which the
 *  operator sets in Settings; a number is for the few screens whose refresh
 *  rate is a property of the screen, not a preference. */
export function usePoll(load: () => Promise<void> | void, rate: PollRate, deps: unknown[] = []) {
  const saved = useRef(load);
  saved.current = load;
  const ms = useRate(rate);
  useEffect(() => {
    let stop = false;
    const tick = () => {
      if (!stop && document.visibilityState === "visible") void saved.current();
    };
    void saved.current();
    const timer = window.setInterval(tick, ms);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, ms]);
}
