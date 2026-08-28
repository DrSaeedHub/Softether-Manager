"use client";

import { useEffect, useRef, useState } from "react";

/**
 * The log, as a real terminal -- because here that is literally true.
 *
 * No fake traffic lights: drawing a window frame inside a real window is the
 * oldest tell there is. The frame is a strip naming the stream on the left
 * and its size on the right.
 *
 * Severity gets a glyph column (`·` `✓` `×` `»`) as well as a colour, so it
 * survives a colour-blind operator and a black-and-white screenshot. Auto-
 * scroll sticks to the bottom only while the reader is already there -- the
 * moment they scroll up to read something, it stops yanking them back.
 */

type Level = "info" | "ok" | "err" | "mgr";

function classify(line: string): Level {
  if (line.startsWith("* ")) return "mgr";
  if (line.startsWith("x ") || /(error|fail|denied|cannot|could not|refused)/i.test(line)) return "err";
  if (/^\$ /.test(line)) return "mgr";
  if (/(succeeded|success|installed|registered|finished|answering|reached)/i.test(line)) return "ok";
  return "info";
}

const GLYPH: Record<Level, string> = { info: "·", ok: "✓", err: "×", mgr: "»" };

export function Terminal({
  lines,
  live,
  label = "install.log",
  tall,
}: {
  lines: string[];
  live: boolean;
  label?: string;
  tall?: boolean;
}) {
  const body = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);

  useEffect(() => {
    const el = body.current;
    if (el && stick) el.scrollTop = el.scrollHeight;
  }, [lines.length, stick]);

  const onScroll = () => {
    const el = body.current;
    if (!el) return;
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
  };

  return (
    <div className={`term${tall ? " term--tall" : ""}`}>
      <div className="term__head">
        <span className="mono">{label}</span>
        <span className="mono">
          {lines.length.toLocaleString()} {lines.length === 1 ? "line" : "lines"}
          {live && " · live"}
        </span>
      </div>
      <div className="term__body" ref={body} onScroll={onScroll}>
        {lines.length === 0 && (
          <div className="term__ln term__ln--mgr">
            <span className="term__g">»</span>
            <span>waiting for output…</span>
          </div>
        )}
        {lines.map((l, i) => {
          const lv = classify(l);
          return (
            <div key={i} className={`term__ln${lv === "info" ? "" : ` term__ln--${lv}`}`}>
              <span className="term__g">{GLYPH[lv]}</span>
              <span>{l.replace(/^[*x$] /, (m) => (m === "$ " ? "$ " : ""))}</span>
            </div>
          );
        })}
        {live && <span className="term__cursor" />}
      </div>
      {!stick && (
        <button className="term__stick" onClick={() => setStick(true)}>
          jump to latest
        </button>
      )}
    </div>
  );
}
