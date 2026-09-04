"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Quota, type QuotaMetric, type QuotaUnit } from "../lib/api";
import { useToast } from "../lib/toast";
import { formatBytes, formatDate } from "../lib/util";
import { CheckRow, Field, usePoll } from "./bits";
import { Sheet } from "../ui/Sheet";
import { IconTrash } from "../ui/Icon";
import { Pill } from "../ui/Status";

/**
 * The traffic ceiling: how much may move, which direction counts, and whether
 * it is armed. Three decisions and no more.
 *
 * The pieces are split by who owns the Save button. A **config's** limit is
 * part of its profile, so the fields ride inside the Edit-profile sheet and
 * are saved with everything else -- one form, one commit. A **hub's** limit
 * has no such form to join, so it keeps a card of its own here.
 *
 * The meter is drawn against the metric's own figure, not the combined total:
 * a download-only limit reading 90% means 90% of the download allowance, and
 * the two direction figures underneath say where that came from. Once a quota
 * bites, the subject is genuinely cut off -- access denied for a config,
 * offline for a hub -- so the summary says so in those words rather than
 * colouring a bar red and leaving the operator to guess.
 */

export const METRIC_OPTIONS: { value: QuotaMetric; label: string; hint: string }[] = [
  { value: "total", label: "Both", hint: "Download and upload added together." },
  { value: "download", label: "Download", hint: "Only what the subject pulls down." },
  { value: "upload", label: "Upload", hint: "Only what the subject pushes up." },
];

const UNITS: QuotaUnit[] = ["MB", "GB", "TB"];

/** What the metric is called in a sentence. */
export const METRIC_WORD: Record<QuotaMetric, string> = {
  total: "download + upload",
  download: "download",
  upload: "upload",
};

/** A pair of raw or net byte counters, in the panel's own words. */
export interface Bytes {
  /** What the subject uploaded. */
  send: number;
  /** What the subject downloaded. */
  recv: number;
}

/**
 * The Transfer the panel shows: SoftEther's lifetime counter, less whatever a
 * reset put behind us.
 *
 * SoftEther has no way to zero a counter, so a reset is a baseline this panel
 * keeps and every figure subtracts. Passing the raw counters through here is
 * what makes the Transfer column and the quota meter beside it the same
 * number, computed from the same reading, rather than two figures a tick
 * apart.
 */
export function netBytes(raw: Bytes, quota?: Quota | null): Bytes {
  if (!quota) return raw;
  return {
    send: Math.max(0, raw.send - quota.base_send_bytes),
    recv: Math.max(0, raw.recv - quota.base_recv_bytes),
  };
}

/** The one of those figures a metric measures. */
export function meteredBytes(net: Bytes, metric: QuotaMetric): number {
  if (metric === "upload") return net.send;
  if (metric === "download") return net.recv;
  return net.send + net.recv;
}

/** How far into its ceiling a subject is, 0-100. */
function percentOf(used: number, limitBytes: number): number {
  if (limitBytes <= 0) return 0;
  return Math.max(0, Math.min(100, (used / limitBytes) * 100));
}

/** Neutral until it is worth looking at, warning near the end, spent at it. */
export function quotaTone(quota: Quota, percent = quota.percent): "accent" | "warn" | "err" {
  if (!quota.enabled) return "accent";
  if (percent >= 100) return "err";
  if (percent >= 80) return "warn";
  return "accent";
}

const TONE_COLOR = {
  accent: "var(--accent)",
  warn: "var(--warn)",
  err: "var(--err)",
} as const;

/** The bar itself — shared by the summary and by the table cell. */
export function QuotaMeter({ quota, used }: { quota: Quota; used?: number }) {
  const spent = used ?? quota.used_bytes;
  const percent = percentOf(spent, quota.limit_bytes);
  return (
    <div
      className="meter"
      role="meter"
      aria-valuenow={Math.round(percent)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`${formatBytes(spent)} of ${formatBytes(quota.limit_bytes)} used`}
    >
      <div
        className="meter__fill"
        style={{ width: `${percent}%`, background: TONE_COLOR[quotaTone(quota, percent)] }}
      />
    </div>
  );
}

// --- the draft a form edits -------------------------------------------------------

/** A limit being edited. `limit` stays a string so typing into it is normal. */
export interface QuotaDraft {
  /** Off means "no ceiling on this config" — saving with it off removes one. */
  on: boolean;
  limit: string;
  unit: QuotaUnit;
  metric: QuotaMetric;
  enforce: boolean;
  /** Zero the config's transfer when this draft is saved. Independent of the
   *  ceiling: a transfer is worth resetting with or without one. */
  reset: boolean;
}

export const EMPTY_DRAFT: QuotaDraft = {
  on: false,
  limit: "50",
  unit: "GB",
  metric: "total",
  enforce: true,
  reset: false,
};

/** A saved quota, back in editable form. A record with no ceiling — one that
 *  exists only to remember a reset — edits as "no limit". */
export function draftOf(quota: Quota | null): QuotaDraft {
  if (!quota || !quota.has_limit) return { ...EMPTY_DRAFT, on: false };
  return {
    on: true,
    limit: String(quota.limit),
    unit: quota.unit,
    metric: quota.metric,
    enforce: quota.enabled,
    reset: false,
  };
}

/** The amount, validated. Throws the message the operator should read. */
export function draftAmount(draft: QuotaDraft): number {
  const amount = Number(draft.limit);
  if (!isFinite(amount) || amount <= 0) {
    throw new Error("Give the traffic limit a number greater than zero.");
  }
  return amount;
}

/**
 * Apply a draft to one config, as part of whatever save is in progress.
 *
 * The ceiling first, the reset second, so a reset always lands on the limit
 * the operator just chose. Removing a ceiling keeps the record if it still
 * holds a baseline — that is the backend's call, and it is what lets a reset
 * survive an operator who later decides against a limit.
 */
export async function saveUserQuotaDraft(
  hub: string,
  name: string,
  draft: QuotaDraft,
  existed: boolean,
): Promise<void> {
  if (draft.on) {
    await api.setUserQuota(hub, name, {
      limit: draftAmount(draft),
      unit: draft.unit,
      metric: draft.metric,
      enabled: draft.enforce,
    });
  } else if (existed) {
    await api.deleteUserQuota(hub, name);
  }
  // Creates the record when there is none: resetting a transfer never depends
  // on a limit having been set first.
  if (draft.reset) await api.resetUserTransfer(hub, name);
}

/** The amount, the unit and what they count. The rows every quota form shares. */
export function QuotaFields({
  draft,
  onChange,
}: {
  draft: QuotaDraft;
  onChange: (next: QuotaDraft) => void;
}) {
  const set = (patch: Partial<QuotaDraft>) => onChange({ ...draft, ...patch });
  return (
    <div className="row2">
      <Field label="Limit" hint="How much may move before the ceiling bites.">
        <div className="qsplit">
          <input
            className="input mono"
            type="number"
            min={0}
            step="any"
            inputMode="decimal"
            value={draft.limit}
            onChange={(e) => set({ limit: e.target.value })}
          />
          <select
            className="select"
            aria-label="Unit"
            value={draft.unit}
            onChange={(e) => set({ unit: e.target.value as QuotaUnit })}
          >
            {UNITS.map((u) => (
              <option key={u} value={u}>{u}</option>
            ))}
          </select>
        </div>
      </Field>
      <Field label="Counts" hint={METRIC_OPTIONS.find((m) => m.value === draft.metric)?.hint}>
        <div className="seg" role="radiogroup" aria-label="What the limit counts">
          {METRIC_OPTIONS.map((m) => (
            <button
              key={m.value}
              role="radio"
              aria-checked={draft.metric === m.value}
              className={draft.metric === m.value ? "on" : ""}
              onClick={() => set({ metric: m.value })}
            >
              {m.label}
            </button>
          ))}
        </div>
      </Field>
    </div>
  );
}

/**
 * Read-only: where the subject has got to, and what that has cost it.
 *
 * `net` overrides the cached figures with the counters the caller already
 * holds, which is how this and the Transfer beside it agree to the byte.
 */
export function QuotaSummary({
  quota,
  subject,
  net,
}: {
  quota: Quota;
  subject: "hub" | "user";
  net?: Bytes;
}) {
  const moved: Bytes = net ?? { send: quota.upload_bytes, recv: quota.download_bytes };
  const used = meteredBytes(moved, quota.metric);
  const percent = percentOf(used, quota.limit_bytes);
  const left = quota.has_limit ? Math.max(0, quota.limit_bytes - used) : null;
  return (
    <div className="quota">
      <div className="quota__head">
        <span className="quota__used mono">{formatBytes(used)}</span>
        <span className="quota__of micro">
          {quota.has_limit
            ? `of ${formatBytes(quota.limit_bytes)} ${METRIC_WORD[quota.metric]}`
            : "moved — no ceiling set"}
        </span>
        {/* Worst state first. "spent" without a block means the ceiling was
            crossed but the cut-off has not landed yet -- the tick is due, or it
            could not reach the server -- which is worth saying rather than
            showing the same pill as a block that took. */}
        {!quota.has_limit ? null : quota.blocked ? (
          <Pill kind="err" label={subject === "hub" ? "offline — over limit" : "cut off — over limit"} />
        ) : !quota.enabled ? (
          <Pill kind="idle" label="not enforced" />
        ) : percent >= 100 ? (
          <Pill kind="busy" label="spent — cutting off" />
        ) : percent >= 80 ? (
          <Pill kind="warn" label="nearly spent" />
        ) : (
          <Pill kind="ok" label="within limit" />
        )}
      </div>
      {quota.has_limit && <QuotaMeter quota={quota} used={used} />}
      <div className="quota__facts">
        <span className="chip"><i>↓ down</i>{formatBytes(moved.recv)}</span>
        <span className="chip"><i>↑ up</i>{formatBytes(moved.send)}</span>
        {left != null && <span className="chip"><i>left</i>{formatBytes(left)}</span>}
        <span className="chip"><i>counting since</i>{formatDate(quota.cycle_start)}</span>
      </div>
      {quota.blocked && (
        <div className="alert alert--warn">
          {subject === "hub" ? (
            <>
              This hub was taken offline when the limit was reached, and every session in it was
              dropped. Raise the limit or reset the transfer to bring it back — the panel puts it
              online again by itself.
            </>
          ) : (
            <>
              Access for this config was denied when the limit was reached, and its sessions were
              cut. Raise the limit or reset the transfer and the panel restores exactly the policy
              it found.
            </>
          )}
        </div>
      )}
    </div>
  );
}

// --- a config's limit, inside its profile form ------------------------------------

/**
 * The config's ceiling as a block of an existing form: a switch, the fields it
 * reveals, and -- once there is something to reset -- the option to start a
 * new cycle with the save. Nothing here talks to the server; the sheet that
 * owns the Save button does, through `saveUserQuotaDraft`.
 */
export function UserQuotaBlock({
  quota,
  draft,
  onChange,
  raw,
}: {
  /** The config's traffic record, or null when it has neither limit nor reset. */
  quota: Quota | null;
  draft: QuotaDraft;
  onChange: (next: QuotaDraft) => void;
  /** The config's raw lifetime counters, when the caller has them. */
  raw?: Bytes;
}) {
  const set = (patch: Partial<QuotaDraft>) => onChange({ ...draft, ...patch });
  const moved = raw
    ? netBytes(raw, quota)
    : quota
      ? { send: quota.upload_bytes, recv: quota.download_bytes }
      : null;
  const transfer = moved ? moved.send + moved.recv : 0;
  return (
    <>
      <div className="tpl__group">Traffic limit</div>
      <CheckRow
        checked={draft.on}
        onChange={(v) => set({ on: v })}
        label="Limit how much this config may move"
        hint={
          moved
            ? `Measured against the transfer the panel shows for this config — ${formatBytes(transfer)} right now — so a limit applies to what it has already used, not only to what it uses next.`
            : "Measured against the transfer the panel shows for this config, so a limit applies to what it has already used, not only to what it uses next."
        }
      />
      {draft.on && (
        <>
          {quota?.has_limit && <QuotaSummary quota={quota} subject="user" net={moved ?? undefined} />}
          <QuotaFields draft={draft} onChange={onChange} />
          <CheckRow
            checked={draft.enforce}
            onChange={(v) => set({ enforce: v })}
            label="Enforce this limit"
            hint="Off, the transfer still counts and the meter still fills, but nothing is ever cut off — useful for watching what a config would use before committing to a ceiling."
          />
        </>
      )}
      {transfer > 0 && (
        <CheckRow
          checked={draft.reset}
          onChange={(v) => set({ reset: v })}
          label={`Reset this config's transfer to zero (now ${formatBytes(transfer)})`}
          hint="SoftEther counts forever and cannot be told to stop, so the panel keeps its own zero and every figure subtracts it. Saving with this ticked moves that zero to today: the Transfer column and any limit start again together."
        />
      )}
      {!draft.on && quota?.blocked && (
        <div className="alert alert--warn">
          This config is cut off by its limit right now. Saving with the limit removed restores its
          access.
        </div>
      )}
    </>
  );
}

// --- tables -----------------------------------------------------------------------

/** How a row finds its own quota. Usernames are case-folded because
 *  SoftEther matches them without case, exactly as the backend keys them. */
export const quotaKey = (subject: "hub" | "user", hub: string, username = "") =>
  `${subject}:${hub}:${username.toLowerCase()}`;

/**
 * Every quota on the server, keyed, in one request rather than one per row.
 *
 * The user tables render a meter per row; asking per row would be one call
 * per user on every poll, which is exactly the shape of request the panel
 * avoids everywhere else.
 */
export function useQuotaIndex(deps: unknown[] = []) {
  const [index, setIndex] = useState<Map<string, Quota>>(new Map());
  const load = useCallback(async () => {
    const rows = await api.quotas().catch(() => null);
    if (rows) setIndex(new Map(rows.map((q) => [quotaKey(q.subject, q.hub, q.username), q])));
  }, []);
  usePoll(load, "list", deps);
  return index;
}

/** A row's place in its quota, for sorting: unlimited sorts below everything
 *  limited, so the configs with a ceiling group together. */
export function quotaSortValue(quota: Quota | undefined, net?: Bytes): number {
  if (!quota?.has_limit) return -1;
  const moved = net ?? { send: quota.upload_bytes, recv: quota.download_bytes };
  return percentOf(meteredBytes(moved, quota.metric), quota.limit_bytes);
}

/** Used / limit with a bar, for a table cell. `net` is the row's own
 *  Transfer, so the two columns can never disagree. */
export function QuotaCell({ quota, net }: { quota: Quota | undefined; net?: Bytes }) {
  if (!quota?.has_limit) return <span className="micro">—</span>;
  const moved: Bytes = net ?? { send: quota.upload_bytes, recv: quota.download_bytes };
  const used = meteredBytes(moved, quota.metric);
  return (
    <span
      className="qcell"
      title={`${METRIC_WORD[quota.metric]} · ${formatBytes(used)} of ${formatBytes(quota.limit_bytes)}`}
    >
      <span className="qcell__t mono">
        {formatBytes(used)}
        <span className="muted"> / {formatBytes(quota.limit_bytes)}</span>
      </span>
      <QuotaMeter quota={quota} used={used} />
      {quota.blocked ? <Pill kind="err" label="over" /> : !quota.enabled ? <Pill kind="idle" label="off" /> : null}
    </span>
  );
}

// --- a hub's limit, on its own -----------------------------------------------------

/**
 * The hub's ceiling. Unlike a config's, it has no profile form to join, so it
 * carries its own Save, its own Reset and its own Remove.
 */
export function QuotaCard({ hub, onChanged }: { hub: string; onChanged?: () => void }) {
  const [quota, setQuota] = useState<Quota | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [draft, setDraft] = useState<QuotaDraft>(EMPTY_DRAFT);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const { guard, push } = useToast();

  const adopt = useCallback((next: Quota | null) => {
    setQuota(next);
    setDraft(next ? draftOf(next) : { ...EMPTY_DRAFT });
    setDirty(false);
  }, []);

  const load = useCallback(async () => {
    try {
      adopt(await api.hubQuota(hub));
    } catch (e) {
      // 404 is the ordinary answer for "no ceiling here"; anything else is a
      // real failure and the card simply stays in its unset state.
      if (!(e instanceof ApiError) || e.status !== 404) {
        push("err", e instanceof Error ? e.message : String(e));
      }
      adopt(null);
    } finally {
      setLoaded(true);
    }
  }, [hub, adopt, push]);

  useEffect(() => {
    void load();
  }, [load]);

  const change = (next: QuotaDraft) => {
    setDraft(next);
    setDirty(true);
  };

  const save = async () => {
    let amount: number;
    try {
      amount = draftAmount(draft);
    } catch (e) {
      push("err", e instanceof Error ? e.message : String(e));
      return;
    }
    setSaving(true);
    const ok = await guard(async () => {
      adopt(
        await api.setHubQuota(hub, {
          limit: amount,
          unit: draft.unit,
          metric: draft.metric,
          enabled: draft.enforce,
        }),
      );
    }, "Traffic limit saved.");
    setSaving(false);
    if (ok) onChanged?.();
  };

  const reset = () =>
    guard(async () => {
      adopt(await api.resetHubTransfer(hub));
      onChanged?.();
    }, "Transfer reset — the hub counts from zero again.");

  const remove = () =>
    guard(async () => {
      await api.deleteHubQuota(hub);
      adopt(null);
      setRemoving(false);
      onChanged?.();
    }, "Traffic limit removed.");

  if (!loaded) return null;

  return (
    <div className="card" style={{ padding: "var(--s4)", maxWidth: 640 }}>
      {quota?.has_limit ? (
        <QuotaSummary quota={quota} subject="hub" />
      ) : (
        <p className="lede" style={{ marginBottom: "var(--s3)" }}>
          No traffic limit on this hub. Set one and the panel counts what moves — from
          SoftEther&rsquo;s own counters, so it survives a restart — and takes the hub offline the
          moment the ceiling is reached.
        </p>
      )}

      <QuotaFields draft={draft} onChange={change} />

      {quota?.has_limit && (
        <CheckRow
          checked={draft.enforce}
          onChange={(v) => change({ ...draft, enforce: v })}
          label="Enforce this limit"
          hint="Off, the counter keeps running and the meter keeps filling, but the hub is never taken offline — useful for watching what it would use before committing to a ceiling."
        />
      )}

      <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap", marginTop: "var(--s3)" }}>
        {(dirty || !quota?.has_limit) && (
          <button className="btn btn--primary" onClick={() => void save()} disabled={saving}>
            {saving && <span className="spin" />} {quota?.has_limit ? "Save limit" : "Set the limit"}
          </button>
        )}
        {dirty && quota?.has_limit && <button className="btn" onClick={() => adopt(quota)}>Discard</button>}
        {quota && !dirty && (
          <>
            <button className="btn" onClick={() => void reset()}>Reset transfer</button>
            {quota.has_limit && (
              <button className="btn btn--ghost" onClick={() => setRemoving(true)}>
                <IconTrash size={14} /> Remove limit
              </button>
            )}
          </>
        )}
      </div>

      {removing && (
        <Sheet
          title="Remove the traffic limit?"
          subtitle={hub}
          onClose={() => setRemoving(false)}
          footer={
            <>
              <button className="btn" onClick={() => setRemoving(false)}>Keep it</button>
              <button className="btn btn--danger" onClick={() => void remove()}>Remove limit</button>
            </>
          }
        >
          <p className="lede">
            The ceiling and everything counted against it are dropped.
            {quota?.blocked && (
              <>
                {" "}Because this hub is offline <b>because of</b> the limit, removing it also brings
                the hub back online.
              </>
            )}
          </p>
        </Sheet>
      )}
    </div>
  );
}
