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

/** Neutral until it is worth looking at, warning near the end, spent at it. */
export function quotaTone(quota: Quota): "accent" | "warn" | "err" {
  if (!quota.enabled) return "accent";
  if (quota.percent >= 100) return "err";
  if (quota.percent >= 80) return "warn";
  return "accent";
}

const TONE_COLOR = {
  accent: "var(--accent)",
  warn: "var(--warn)",
  err: "var(--err)",
} as const;

/** The bar itself — shared by the summary and by the table cell. */
export function QuotaMeter({ quota }: { quota: Quota }) {
  const percent = Math.max(0, Math.min(100, quota.percent));
  return (
    <div
      className="meter"
      role="meter"
      aria-valuenow={Math.round(percent)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`${formatBytes(quota.used_bytes)} of ${formatBytes(quota.limit_bytes)} used`}
    >
      <div
        className="meter__fill"
        style={{ width: `${percent}%`, background: TONE_COLOR[quotaTone(quota)] }}
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
  /** Start a new cycle when this draft is saved. */
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

/** A saved quota, back in editable form. */
export function draftOf(quota: Quota | null): QuotaDraft {
  if (!quota) return { ...EMPTY_DRAFT };
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
 * Ordering matters: the ceiling is written before the counter is zeroed, so a
 * reset always lands on the limit the operator just chose.
 */
export async function saveUserQuotaDraft(
  hub: string,
  name: string,
  draft: QuotaDraft,
  existed: boolean,
): Promise<void> {
  if (!draft.on) {
    if (existed) await api.deleteUserQuota(hub, name);
    return;
  }
  await api.setUserQuota(hub, name, {
    limit: draftAmount(draft),
    unit: draft.unit,
    metric: draft.metric,
    enabled: draft.enforce,
  });
  if (draft.reset) await api.resetUserQuota(hub, name);
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

/** Read-only: where the subject has got to, and what that has cost it. */
export function QuotaSummary({ quota, subject }: { quota: Quota; subject: "hub" | "user" }) {
  return (
    <div className="quota">
      <div className="quota__head">
        <span className="quota__used mono">{formatBytes(quota.used_bytes)}</span>
        <span className="quota__of micro">
          of {formatBytes(quota.limit_bytes)} {METRIC_WORD[quota.metric]}
        </span>
        {/* Worst state first. "spent" without a block means the ceiling was
            crossed but the cut-off has not landed yet -- the tick is due, or it
            could not reach the server -- which is worth saying rather than
            showing the same pill as a block that took. */}
        {quota.blocked ? (
          <Pill kind="err" label={subject === "hub" ? "offline — over limit" : "cut off — over limit"} />
        ) : !quota.enabled ? (
          <Pill kind="idle" label="not enforced" />
        ) : quota.percent >= 100 ? (
          <Pill kind="busy" label="spent — cutting off" />
        ) : quota.percent >= 80 ? (
          <Pill kind="warn" label="nearly spent" />
        ) : (
          <Pill kind="ok" label="within limit" />
        )}
      </div>
      <QuotaMeter quota={quota} />
      <div className="quota__facts">
        <span className="chip"><i>↓ down</i>{formatBytes(quota.download_bytes)}</span>
        <span className="chip"><i>↑ up</i>{formatBytes(quota.upload_bytes)}</span>
        <span className="chip">
          <i>left</i>
          {quota.remaining_bytes == null ? "—" : formatBytes(quota.remaining_bytes)}
        </span>
        <span className="chip"><i>since</i>{formatDate(quota.cycle_start)}</span>
      </div>
      {quota.blocked && (
        <div className="alert alert--warn">
          {subject === "hub" ? (
            <>
              This hub was taken offline when the limit was reached, and every session in it was
              dropped. Raise the limit or reset the counter to bring it back — the panel puts it
              online again by itself.
            </>
          ) : (
            <>
              Access for this config was denied when the limit was reached, and its sessions were
              cut. Raise the limit or reset the counter and the panel restores exactly the policy
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
}: {
  /** What is stored today, or null when the config has no ceiling yet. */
  quota: Quota | null;
  draft: QuotaDraft;
  onChange: (next: QuotaDraft) => void;
}) {
  const set = (patch: Partial<QuotaDraft>) => onChange({ ...draft, ...patch });
  const spent = quota ? quota.used_bytes > 0 : false;
  return (
    <>
      <div className="tpl__group">Traffic limit</div>
      <CheckRow
        checked={draft.on}
        onChange={(v) => set({ on: v })}
        label="Limit how much this config may move"
        hint={
          quota
            ? "Off, the ceiling and everything counted against it are removed when you save."
            : "The panel counts what moves — from SoftEther's own counters, so it survives a restart — and denies access and cuts the sessions the moment the ceiling is reached."
        }
      />
      {draft.on && (
        <>
          {quota && <QuotaSummary quota={quota} subject="user" />}
          <QuotaFields draft={draft} onChange={onChange} />
          <CheckRow
            checked={draft.enforce}
            onChange={(v) => set({ enforce: v })}
            label="Enforce this limit"
            hint="Off, the counter keeps running and the meter keeps filling, but nothing is ever cut off — useful for watching what a config would use before committing to a ceiling."
          />
          {spent && (
            <CheckRow
              checked={draft.reset}
              onChange={(v) => set({ reset: v })}
              label="Reset the counter with this save"
              hint="Starts a new cycle from zero. If the config is cut off by its limit, this is what lets it back on."
            />
          )}
        </>
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
export function quotaSortValue(quota: Quota | undefined): number {
  return quota ? quota.percent : -1;
}

/** Used / limit with a bar, for a table cell. */
export function QuotaCell({ quota }: { quota: Quota | undefined }) {
  if (!quota) return <span className="micro">—</span>;
  return (
    <span
      className="qcell"
      title={`${METRIC_WORD[quota.metric]} · ${formatBytes(quota.used_bytes)} of ${formatBytes(quota.limit_bytes)}`}
    >
      <span className="qcell__t mono">
        {formatBytes(quota.used_bytes)}
        <span className="muted"> / {formatBytes(quota.limit_bytes)}</span>
      </span>
      <QuotaMeter quota={quota} />
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
      adopt(await api.resetHubQuota(hub));
      onChanged?.();
    }, "Counter reset — a new cycle starts now.");

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
      {quota ? (
        <QuotaSummary quota={quota} subject="hub" />
      ) : (
        <p className="lede" style={{ marginBottom: "var(--s3)" }}>
          No traffic limit on this hub. Set one and the panel counts what moves — from
          SoftEther&rsquo;s own counters, so it survives a restart — and takes the hub offline the
          moment the ceiling is reached.
        </p>
      )}

      <QuotaFields draft={draft} onChange={change} />

      {quota && (
        <CheckRow
          checked={draft.enforce}
          onChange={(v) => change({ ...draft, enforce: v })}
          label="Enforce this limit"
          hint="Off, the counter keeps running and the meter keeps filling, but the hub is never taken offline — useful for watching what it would use before committing to a ceiling."
        />
      )}

      <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap", marginTop: "var(--s3)" }}>
        {(dirty || !quota) && (
          <button className="btn btn--primary" onClick={() => void save()} disabled={saving}>
            {saving && <span className="spin" />} {quota ? "Save limit" : "Set the limit"}
          </button>
        )}
        {dirty && quota && <button className="btn" onClick={() => adopt(quota)}>Discard</button>}
        {quota && !dirty && (
          <>
            <button className="btn" onClick={() => void reset()}>Reset counter</button>
            <button className="btn btn--ghost" onClick={() => setRemoving(true)}>
              <IconTrash size={14} /> Remove limit
            </button>
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
