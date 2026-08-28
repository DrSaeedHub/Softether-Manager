"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Usage } from "../lib/api";
import { formatBytes } from "../lib/util";

/**
 * Throughput over time: two 2px lines with a soft area fill, upload and
 * download, on a recessive grid. One y-axis. Colour follows the series
 * (--chart-send / --chart-recv, validated for CVD separation); identity is
 * also carried by the legend, so colour never stands alone. Hovering raises
 * a crosshair with both values at that sample.
 *
 * The SVG's viewBox tracks the element's real width (one unit = one CSS
 * pixel), so axis text renders at its set size instead of scaling up with
 * the chart.
 */

const H = 190;
const PAD = { top: 12, right: 10, bottom: 24, left: 64 };

export function TrafficChart({ usage, emptyLabel }: { usage: Usage; emptyLabel?: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const [width, setWidth] = useState(720);
  const holder = useRef<HTMLDivElement>(null);
  const points = usage.points;

  useEffect(() => {
    const el = holder.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect.width ?? 0);
      if (w > 80) setWidth(w);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const model = useMemo(() => {
    if (points.length < 2) return null;
    const innerW = width - PAD.left - PAD.right;
    const innerH = H - PAD.top - PAD.bottom;
    const t0 = new Date(points[0].t).getTime();
    const t1 = new Date(points[points.length - 1].t).getTime();
    const span = Math.max(1, t1 - t0);
    const max = Math.max(1, ...points.map((p) => Math.max(p.send, p.recv)));
    const x = (t: string) => PAD.left + ((new Date(t).getTime() - t0) / span) * innerW;
    const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
    const line = (key: "send" | "recv") =>
      points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join("");
    const area = (key: "send" | "recv") =>
      `${line(key)}L${x(points[points.length - 1].t).toFixed(1)},${PAD.top + innerH}L${x(
        points[0].t,
      ).toFixed(1)},${PAD.top + innerH}Z`;
    const ticks = [0.25, 0.5, 0.75, 1].map((f) => ({ v: max * f, y: y(max * f) }));
    return { x, y, max, line, area, ticks, innerH, t0, t1 };
  }, [points, width]);

  if (!model) {
    return (
      <div className="chart chart--empty micro" ref={holder}>
        {emptyLabel ?? "Not enough samples yet — the panel records traffic every few minutes."}
      </div>
    );
  }

  const onMove = (e: React.PointerEvent) => {
    const svg = holder.current?.querySelector("svg");
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const px = e.clientX - rect.left;
    let best = 0;
    let bestDistance = Infinity;
    points.forEach((p, i) => {
      const d = Math.abs(model.x(p.t) - px);
      if (d < bestDistance) {
        bestDistance = d;
        best = i;
      }
    });
    setHover(best);
  };

  const h = hover != null ? points[hover] : null;

  return (
    <div className="chart" ref={holder}>
      <div className="chart__legend">
        <span className="chart__key">
          <i style={{ background: "var(--chart-recv)" }} /> Download
          <b className="mono">{formatBytes(usage.total_recv)}</b>
        </span>
        <span className="chart__key">
          <i style={{ background: "var(--chart-send)" }} /> Upload
          <b className="mono">{formatBytes(usage.total_send)}</b>
        </span>
        {h && (
          <span className="chart__hoverv mono">
            {new Date(h.t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
            {" · ↓"}
            {formatBytes(h.recv)} · ↑{formatBytes(h.send)}
          </span>
        )}
      </div>
      <svg
        viewBox={`0 0 ${width} ${H}`}
        width={width}
        height={H}
        className="chart__svg"
        role="img"
        aria-label={`Traffic over the last ${usage.hours} hours: ${formatBytes(usage.total_recv)} downloaded, ${formatBytes(usage.total_send)} uploaded.`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        {model.ticks.map((t) => (
          <g key={t.y}>
            <line x1={PAD.left} x2={width - PAD.right} y1={t.y} y2={t.y} className="chart__grid" />
            <text x={PAD.left - 8} y={t.y + 3.5} className="chart__tick" textAnchor="end">
              {formatBytes(t.v)}
            </text>
          </g>
        ))}
        <line
          x1={PAD.left}
          x2={width - PAD.right}
          y1={PAD.top + model.innerH}
          y2={PAD.top + model.innerH}
          className="chart__axis"
        />
        {[0, 0.5, 1].map((f) => {
          const t = new Date(model.t0 + (model.t1 - model.t0) * f);
          return (
            <text
              key={f}
              x={PAD.left + (width - PAD.left - PAD.right) * f}
              y={H - 7}
              className="chart__tick"
              textAnchor={f === 0 ? "start" : f === 1 ? "end" : "middle"}
            >
              {t.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
            </text>
          );
        })}
        <path d={model.area("recv")} fill="var(--chart-recv)" opacity={0.12} />
        <path d={model.area("send")} fill="var(--chart-send)" opacity={0.12} />
        <path d={model.line("recv")} fill="none" stroke="var(--chart-recv)" strokeWidth={2} strokeLinejoin="round" />
        <path d={model.line("send")} fill="none" stroke="var(--chart-send)" strokeWidth={2} strokeLinejoin="round" />
        {h && (
          <g>
            <line x1={model.x(h.t)} x2={model.x(h.t)} y1={PAD.top} y2={PAD.top + model.innerH} className="chart__cross" />
            <circle cx={model.x(h.t)} cy={model.y(h.recv)} r={4} fill="var(--chart-recv)" stroke="var(--glass)" strokeWidth={2} />
            <circle cx={model.x(h.t)} cy={model.y(h.send)} r={4} fill="var(--chart-send)" stroke="var(--glass)" strokeWidth={2} />
          </g>
        )}
      </svg>
    </div>
  );
}

/** The range picker every usage chart shares. */
export function RangeSeg({ hours, onChange }: { hours: number; onChange: (h: number) => void }) {
  const options = [
    { h: 6, label: "6h" },
    { h: 24, label: "24h" },
    { h: 24 * 7, label: "7d" },
    { h: 24 * 30, label: "30d" },
  ];
  return (
    <div className="seg" role="tablist" aria-label="Time range">
      {options.map((o) => (
        <button
          key={o.h}
          role="tab"
          aria-selected={hours === o.h}
          className={hours === o.h ? "on" : ""}
          onClick={() => onChange(o.h)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
