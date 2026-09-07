"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useToast } from "../lib/toast";

/**
 * A switch is a claim about the server, so it is never allowed to lie.
 *
 * Every switch in the panel goes through `useToggle`, which turns "flip it"
 * into four honest states: the value last read from the server, a pending
 * state while the request is in flight (the control is locked, so a second
 * click cannot queue a second request), and then either success or failure
 * -- decided not by whether the request returned, but by reading the state
 * back and comparing. A server that answered 200 and did nothing shows as
 * "still off", with the reason in a toast and a note beside the switch.
 */

export interface Outcome {
  kind: "ok" | "err";
  text: string;
  at: number;
}

export interface ToggleOptions {
  /** The state as last read from the server; null while it is unknown. */
  value: boolean | null;
  /** Ask the server for the other state. Resolving to a boolean reports the
   *  state the server confirmed; anything else means "re-read it". */
  apply: (next: boolean) => Promise<unknown>;
  /** Read the state back afterwards. Returning a boolean lets the change be
   *  verified rather than assumed. */
  reload?: () => Promise<boolean | null | void>;
  /** What the thing is called in messages: "SecureNAT", "Listener 443". */
  noun: string;
  onWord?: string;
  offWord?: string;
  /** Skip the success toast (the change is visible where the operator is
   *  looking anyway). Failures always toast. */
  quiet?: boolean;
}

export interface Toggle {
  /** True while a change is in flight. */
  pending: boolean;
  /** The state being asked for, while pending. */
  target: boolean | null;
  outcome: Outcome | null;
  /** Flip, or set explicitly. Resolves to whether the server ended up in the
   *  requested state. Ignored while a change is already pending. */
  toggle: (next?: boolean) => Promise<boolean>;
}

const OUTCOME_MS = 6000;

export function useToggle({ value, apply, reload, noun, onWord = "on", offWord = "off", quiet }: ToggleOptions): Toggle {
  const [target, setTarget] = useState<boolean | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const { push } = useToast();
  const alive = useRef(true);
  // The latest callbacks, so a poll that re-created them mid-flight is used
  // for the read-back rather than a stale closure.
  const latest = useRef({ apply, reload, value });
  latest.current = { apply, reload, value };

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!outcome) return;
    const timer = window.setTimeout(() => setOutcome(null), OUTCOME_MS);
    return () => window.clearTimeout(timer);
  }, [outcome]);

  const toggle = useCallback(
    async (next?: boolean) => {
      const current = latest.current.value;
      if (target !== null || current === null) return false;
      const wanted = next ?? !current;
      setTarget(wanted);
      setOutcome(null);

      let fresh: boolean | null = null;
      let error: string | null = null;
      try {
        const answer = await latest.current.apply(wanted);
        if (typeof answer === "boolean") fresh = answer;
      } catch (e) {
        error = e instanceof Error ? e.message : String(e);
      }
      // Read back regardless: after a failure the control must show what
      // the server is actually in, which may or may not be what it was.
      try {
        const read = latest.current.reload ? await latest.current.reload() : undefined;
        if (typeof read === "boolean") fresh = read;
      } catch {
        /* keep whatever the apply step reported */
      }
      if (!alive.current) return false;
      setTarget(null);

      if (error) {
        const text = fresh !== null && fresh !== wanted ? `${error} — still ${fresh ? onWord : offWord}.` : error;
        setOutcome({ kind: "err", text, at: Date.now() });
        push("err", `${noun}: ${text}`);
        return false;
      }
      if (fresh !== null && fresh !== wanted) {
        const text = `The server still reports ${noun} ${fresh ? onWord : offWord}.`;
        setOutcome({ kind: "err", text, at: Date.now() });
        push("err", text);
        return false;
      }
      const text = `${noun} ${wanted ? onWord : offWord}.`;
      setOutcome({ kind: "ok", text, at: Date.now() });
      if (!quiet) push("ok", text);
      return true;
    },
    [target, noun, onWord, offWord, quiet, push],
  );

  return { pending: target !== null, target, outcome, toggle };
}

/** The control itself: a pressed track with a raised knob, the word beside it
 *  saying what state it is in -- or what state it is moving to. */
export function Switch({
  on,
  pending,
  target,
  disabled,
  onToggle,
  label,
  onWord = "on",
  offWord = "off",
  word = true,
}: {
  on: boolean;
  pending?: boolean;
  target?: boolean | null;
  disabled?: boolean;
  onToggle: () => void;
  /** The accessible name: what this switch controls. */
  label: string;
  onWord?: string;
  offWord?: string;
  /** Render the state word next to the track. */
  word?: boolean;
}) {
  const busy = Boolean(pending);
  const text = busy
    ? `turning ${target ?? !on ? onWord : offWord}…`
    : on
      ? onWord
      : offWord;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-busy={busy || undefined}
      aria-label={label}
      title={busy ? `${label}: ${text}` : `${label}: ${text} — click to turn ${on ? offWord : onWord}`}
      className={`switch${on ? " switch--on" : ""}${busy ? " switch--busy" : ""}`}
      disabled={disabled || busy}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
    >
      <span className="switch__track" aria-hidden="true">
        <span className="switch__knob">
          {busy ? (
            <span className="spin" />
          ) : on ? (
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12.5l4.5 4.5L19 7.5" />
            </svg>
          ) : null}
        </span>
      </span>
      {word && <span className="switch__word">{text}</span>}
    </button>
  );
}

/** The transient verdict beside a switch: a tick and what happened, or a
 *  cross and why not. Fades on its own. */
export function OutcomeNote({ outcome, className = "" }: { outcome: Outcome | null; className?: string }) {
  if (!outcome) return null;
  return (
    <span className={`outcome outcome--${outcome.kind} ${className}`} role="status">
      {outcome.kind === "ok" ? "✓" : "✕"} {outcome.text}
    </span>
  );
}

/**
 * A labelled switch in a pressed well: title and explanation on the left,
 * an optional status pill and the switch on the right, the outcome note
 * underneath the title while there is one.
 */
export function SwitchRow({
  label,
  hint,
  status,
  toggle,
  on,
  disabled,
  onWord,
  offWord,
  onToggle,
}: {
  label: ReactNode;
  hint?: ReactNode;
  /** Something to show beside the switch -- usually a Pill saying what the
   *  server reports. */
  status?: ReactNode;
  toggle: Toggle;
  on: boolean;
  disabled?: boolean;
  onWord?: string;
  offWord?: string;
  /** Override the click (to confirm first); defaults to `toggle.toggle()`. */
  onToggle?: () => void;
}) {
  return (
    <div className={`switchrow${on ? " switchrow--on" : ""}${disabled ? " switchrow--off" : ""}`}>
      <div className="switchrow__m">
        <span className="t">{label}</span>
        {hint && <span className="s">{hint}</span>}
        <OutcomeNote outcome={toggle.outcome} className="switchrow__note" />
      </div>
      <div className="switchrow__side">
        {status}
        <Switch
          on={on}
          pending={toggle.pending}
          target={toggle.target}
          disabled={disabled}
          onToggle={onToggle ?? (() => void toggle.toggle())}
          label={typeof label === "string" ? label : "Switch"}
          onWord={onWord}
          offWord={offWord}
        />
      </div>
    </div>
  );
}
