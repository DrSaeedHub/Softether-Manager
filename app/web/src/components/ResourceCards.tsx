"use client";

import { useState } from "react";
import type { Wire } from "../lib/api";
import { formatBytes } from "../lib/util";
import { IconChevron, IconCpu, IconDisk, IconMemory, IconSwap, IconTraffic } from "../ui/Icon";

/**
 * The machine's own health, Tunnel-Panel style: one card per resource with a
 * big readout, a meter whose tone crosses neutral -> warning -> critical, a
 * sparkline of the last few minutes, and the two or three figures that
 * explain the number. Everything else folds away.
 */

type Tone = "accent" | "warn" | "err";

function tone(percent: number, warn = 75, critical = 90): Tone {
  if (percent >= critical) return "err";
  if (percent >= warn) return "warn";
  return "accent";
}

const TONE_COLOR: Record<Tone, string> = {
  accent: "var(--accent)",
  warn: "var(--warn)",
  err: "var(--err)",
};

export function Meter({ value, toneOf }: { value: number; toneOf: Tone }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div className="meter" role="meter" aria-valuenow={Math.round(clamped)} aria-valuemin={0} aria-valuemax={100}>
      <div className="meter__fill" style={{ width: `${clamped}%`, background: TONE_COLOR[toneOf] }} />
    </div>
  );
}

/** A tiny area sparkline; `max` fixes the scale (100 for percentages). */
export function Spark({ values, toneOf, max, color }: { values: number[]; toneOf?: Tone; max?: number; color?: string }) {
  const W = 200;
  const H = 34;
  if (values.length < 2) return <div className="spark" aria-hidden="true" />;
  const stroke = color ?? TONE_COLOR[toneOf ?? "accent"];
  const top = max ?? Math.max(1, ...values);
  const step = W / (values.length - 1);
  const y = (v: number) => H - 2 - (Math.min(v, top) / top) * (H - 4);
  const line = values.map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const area = `${line}L${W},${H}L0,${H}Z`;
  return (
    <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
      <path d={area} fill={stroke} opacity={0.14} />
      <path d={line} fill="none" stroke={stroke} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function MetricStat({ label, value, emphasise }: { label: string; value: React.ReactNode; emphasise?: boolean }) {
  return (
    <div>
      <div className="micro">{label}</div>
      <div className="mono" style={{ fontSize: "var(--t-small)", fontWeight: 500, color: emphasise ? "var(--warn)" : undefined }}>
        {value}
      </div>
    </div>
  );
}

function MetricTitle({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="metric__label">
      <span className="metric__icon">{icon}</span>
      <span className="metric__t">{children}</span>
    </span>
  );
}

function Fold({ open, onToggle, children }: { open: boolean; onToggle: () => void; children: React.ReactNode }) {
  return (
    <>
      <button className="metric__fold" onClick={onToggle} aria-expanded={open}>
        <span className={`rail__chev${open ? " open" : ""}`} style={{ display: "inline-flex" }}>
          <IconChevron size={12} />
        </span>
        {open ? "Show less" : "Show more"}
      </button>
      {open && <div className="metric__more">{children}</div>}
    </>
  );
}

const pc = (v: unknown) => `${(Number(v) || 0).toFixed(Number(v) >= 10 ? 0 : 1)}%`;

/* ── the cards ───────────────────────────────────────────────────────────── */

export function CpuCard({ snapshot, history }: { snapshot: Wire; history: number[] }) {
  const [open, setOpen] = useState(false);
  const overall = snapshot.cpu?.overall as Wire | null;
  const cores = (snapshot.cpu?.cores as Wire[]) ?? [];
  const load = (snapshot.load as Wire) ?? {};
  const usage = Number(overall?.usage_percent) || 0;
  const t = tone(usage);

  const processes = Number(load.total) || 0;
  return (
    <div className="card metric">
      <div className="metric__head">
        <MetricTitle icon={<IconCpu size={15} />}>CPU</MetricTitle>
        <span className="metric__n mono" style={{ color: t !== "accent" ? TONE_COLOR[t] : undefined }}>{pc(usage)}</span>
      </div>
      <Meter value={usage} toneOf={t} />
      <Spark values={history} toneOf={t} max={100} />
      <div className="metric__stats">
        <MetricStat label="Load 1 min" value={(Number(load.one) || 0).toFixed(2)} />
        <MetricStat label="5 min" value={(Number(load.five) || 0).toFixed(2)} />
        <MetricStat label="15 min" value={(Number(load.fifteen) || 0).toFixed(2)} />
      </div>
      <Fold open={open} onToggle={() => setOpen((o) => !o)}>
        <div className="metric__stats" style={{ marginBottom: "var(--s2)" }}>
          <MetricStat label="User" value={pc(overall?.user_percent)} />
          <MetricStat label="System" value={pc(overall?.system_percent)} />
          <MetricStat label="I/O wait" value={pc(overall?.iowait_percent)} />
          {/* Steal is time the hypervisor gave to somebody else -- on a VPS it
              is the difference between "busy" and "being starved". */}
          <MetricStat label="Steal" value={pc(overall?.steal_percent)} emphasise={Number(overall?.steal_percent) > 1} />
        </div>
        {cores.map((core) => (
          <div key={String(core.name)} className="metric__core">
            <span className="mono micro" style={{ width: 38, flex: "none" }}>{String(core.name)}</span>
            <Meter value={Number(core.usage_percent) || 0} toneOf={tone(Number(core.usage_percent) || 0)} />
            <span className="mono micro" style={{ width: 36, textAlign: "end", flex: "none" }}>{pc(core.usage_percent)}</span>
          </div>
        ))}
      </Fold>
      {processes > 0 && (
        <div className="metric__foot micro">
          <span className="mono">{Number(load.running) || 0}</span> running of{" "}
          <span className="mono">{processes}</span> processes
        </div>
      )}
    </div>
  );
}

export function MemoryCard({ snapshot, history }: { snapshot: Wire; history: number[] }) {
  const [open, setOpen] = useState(false);
  const memory = (snapshot.memory as Wire) ?? {};
  const usage = Number(memory.used_percent) || 0;
  const t = tone(usage, 80, 92);
  return (
    <div className="card metric">
      <div className="metric__head">
        <MetricTitle icon={<IconMemory size={15} />}>Memory</MetricTitle>
        <span className="metric__n mono" style={{ color: t !== "accent" ? TONE_COLOR[t] : undefined }}>{pc(usage)}</span>
      </div>
      <Meter value={usage} toneOf={t} />
      <Spark values={history} toneOf={t} max={100} />
      <div className="metric__stats">
        <MetricStat label="Used" value={formatBytes(Number(memory.used_bytes))} />
        <MetricStat label="Available" value={formatBytes(Number(memory.available_bytes))} />
        <MetricStat label="Total" value={formatBytes(Number(memory.total_bytes))} />
      </div>
      <Fold open={open} onToggle={() => setOpen((o) => !o)}>
        <div className="metric__stats">
          <MetricStat label="Free" value={formatBytes(Number(memory.free_bytes))} />
          <MetricStat label="Buffers" value={formatBytes(Number(memory.buffers_bytes))} />
          <MetricStat label="Cached" value={formatBytes(Number(memory.cached_bytes))} />
        </div>
      </Fold>
    </div>
  );
}

export function SwapCard({ snapshot }: { snapshot: Wire }) {
  const swap = (snapshot.swap as Wire) ?? {};
  if (!swap.configured) {
    // No swap is a valid configuration, not a broken card.
    return (
      <div className="card metric" style={{ opacity: 0.65 }}>
        <div className="metric__head">
          <MetricTitle icon={<IconSwap size={15} />}>Swap</MetricTitle>
        </div>
        <p className="micro">None configured. Fine with enough memory; without it, the kernel's only relief valve is killing processes.</p>
      </div>
    );
  }
  const usage = Number(swap.used_percent) || 0;
  const t = tone(usage, 50, 80);
  return (
    <div className="card metric">
      <div className="metric__head">
        <MetricTitle icon={<IconSwap size={15} />}>Swap</MetricTitle>
        <span className="metric__n mono" style={{ color: t !== "accent" ? TONE_COLOR[t] : undefined }}>{pc(usage)}</span>
      </div>
      <Meter value={usage} toneOf={t} />
      <div className="metric__stats">
        <MetricStat label="Used" value={formatBytes(Number(swap.used_bytes))} />
        <MetricStat label="Total" value={formatBytes(Number(swap.total_bytes))} />
      </div>
    </div>
  );
}

export function DiskCard({ snapshot }: { snapshot: Wire }) {
  const disks = (snapshot.disks as Wire[]) ?? [];
  return (
    <div className="card metric">
      <div className="metric__head">
        <MetricTitle icon={<IconDisk size={15} />}>Disk</MetricTitle>
      </div>
      {disks.length === 0 && <p className="micro">No mounted filesystems reported.</p>}
      {disks.map((disk) => {
        const usage = Number(disk.used_percent) || 0;
        const t = tone(usage, 80, 92);
        return (
          <div key={String(disk.mount_point)} style={{ display: "grid", gap: 5, marginBottom: "var(--s2)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: "var(--s2)", minWidth: 0 }}>
              <span className="mono micro truncate">{String(disk.mount_point)}</span>
              <span className="mono micro" style={{ flex: "none", color: t !== "accent" ? TONE_COLOR[t] : undefined }}>
                {formatBytes(Number(disk.used_bytes))} / {formatBytes(Number(disk.total_bytes))} · {pc(usage)}
              </span>
            </div>
            <Meter value={usage} toneOf={t} />
          </div>
        );
      })}
    </div>
  );
}

export function NetworkCard({ snapshot, rxHistory, txHistory }: { snapshot: Wire; rxHistory: number[]; txHistory: number[] }) {
  const [open, setOpen] = useState(false);
  const net = (snapshot.network as Wire) ?? {};
  const interfaces = (net.interfaces as Wire[]) ?? [];
  const rx = Number(net.rx_bytes_per_second) || 0;
  const tx = Number(net.tx_bytes_per_second) || 0;
  const sparkMax = Math.max(1024, ...rxHistory, ...txHistory);
  return (
    <div className="card metric">
      <div className="metric__head">
        <MetricTitle icon={<IconTraffic size={15} />}>Network</MetricTitle>
        <span className="metric__n mono" style={{ fontSize: 13, textAlign: "end" }}>
          <span style={{ whiteSpace: "nowrap" }}>↓{formatBytes(rx)}/s</span>{" "}
          <span style={{ whiteSpace: "nowrap" }}>↑{formatBytes(tx)}/s</span>
        </span>
      </div>
      {/* the traffic pair wears the chart series colours, not status colours */}
      <div style={{ display: "grid", gap: 3 }}>
        <Spark values={rxHistory} color="var(--chart-recv)" max={sparkMax} />
        <Spark values={txHistory} color="var(--chart-send)" max={sparkMax} />
      </div>
      <div className="metric__stats">
        <MetricStat label="↓ Download" value={`${formatBytes(rx)}/s`} />
        <MetricStat label="↑ Upload" value={`${formatBytes(tx)}/s`} />
      </div>
      <Fold open={open} onToggle={() => setOpen((o) => !o)}>
        {interfaces.map((iface) => (
          <div key={String(iface.name)} style={{ display: "flex", gap: "var(--s2)", justifyContent: "space-between", marginBottom: 4, minWidth: 0 }}>
            <span className="mono micro truncate">{String(iface.name)}</span>
            <span className="mono micro" style={{ flex: "none" }}>
              ↓{formatBytes(Number(iface.rx_bytes))} · ↑{formatBytes(Number(iface.tx_bytes))} total
            </span>
          </div>
        ))}
      </Fold>
    </div>
  );
}
