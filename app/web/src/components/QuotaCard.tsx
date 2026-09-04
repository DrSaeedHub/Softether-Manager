"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Quota, type QuotaMetric, type QuotaUnit } from "../lib/api";
import { useToast } from "../lib/toast";
import { formatBytes, formatDate } from "../lib/util";
import { Field, usePoll } from "./bits";
import { Sheet } from "../ui/Sheet";
import { IconTrash } from "../ui/Icon";
import { Pill } from "../ui/Status";

/**
 * The traffic ceiling on a hub or on one user's config.
 *
 * A quota is three decisions and no more: how much, which direction counts,
 * and whether it is armed. The card leads with the meter -- the operator's
 * actual question is "how close is this?" -- and puts the form under it.
 *
 * The meter is drawn against the metric's own figure, not the combined
 * total: a download-only limit that reads 90% means 90% of the download
 * allowance, and the two direction figures underneath say where that came
 * from. Once a quota bites, the subject is genuinely cut off -- access denied
 * for a user, offline for a hub -- so the card says so in those words rather
 * than colouring a bar red and leaving the operator to guess.
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

/** The bar itself — shared with the compact form used in the user tables. */
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
    <span className="qcell" title={`${METRIC_WORD[quota.metric]} · ${formatBytes(quota.used_bytes)} of ${formatBytes(quota.limit_bytes)}`}>
      <span className="qcell__t mono">
        {formatBytes(quota.used_bytes)}
        <span className="muted"> / {formatBytes(quota.limit_bytes)}</span>
      </span>
      <QuotaMeter quota={quota} />
      {quota.blocked ? <Pill kind="err" label="over" /> : !quota.enabled ? <Pill kind="idle" label="off" /> : null}
    </span>
  );
}

export function QuotaCard({
  subject,
  hub,
  name = "",
  onChanged,
}: {
  subject: "hub" | "user";
  hub: string;
  /** The user's name; ignored for a hub quota. */
  name?: string;
  /** Called after anything that could have changed the subject's own state. */
  onChanged?: () => void;
}) {
  const [quota, setQuota] = useState<Quota | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [limit, setLimit] = useState("50");
  const [unit, setUnit] = useState<QuotaUnit>("GB");
  const [metric, setMetric] = useState<QuotaMetric>("total");
  const [enabled, setEnabled] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const { guard, push } = useToast();

  const adopt = useCallback((next: Quota | null) => {
    setQuota(next);
    if (next) {
      setLimit(String(next.limit));
      setUnit(next.unit);
      setMetric(next.metric);
      setEnabled(next.enabled);
    }
    setDirty(false);
  }, []);

  const load = useCallback(async () => {
    try {
      adopt(subject === "hub" ? await api.hubQuota(hub) : await api.userQuota(hub, name));
    } catch (e) {
      // 404 is the ordinary answer for "no ceiling here"; anything else is
      // a real failure and the card simply stays in its unset state.
      if (!(e instanceof ApiError) || e.status !== 404) push("err", e instanceof Error ? e.message : String(e));
      adopt(null);
    } finally {
      setLoaded(true);
    }
  }, [subject, hub, name, adopt, push]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    const amount = Number(limit);
    if (!isFinite(amount) || amount <= 0) {
      push("err", "Give the limit a number greater than zero.");
      return;
    }
    setSaving(true);
    const body = { limit: amount, unit, metric, enabled };
    const ok = await guard(async () => {
      adopt(
        subject === "hub"
          ? await api.setHubQuota(hub, body)
          : await api.setUserQuota(hub, name, body),
      );
    }, "Traffic limit saved.");
    setSaving(false);
    if (ok) onChanged?.();
  };

  const reset = () =>
    guard(async () => {
      adopt(subject === "hub" ? await api.resetHubQuota(hub) : await api.resetUserQuota(hub, name));
      onChanged?.();
    }, "Counter reset — a new cycle starts now.");

  const remove = () =>
    guard(async () => {
      if (subject === "hub") await api.deleteHubQuota(hub);
      else await api.deleteUserQuota(hub, name);
      adopt(null);
      setRemoving(false);
      onChanged?.();
    }, "Traffic limit removed.");

  const subjectWord = subject === "hub" ? "hub" : "config";
  const set = <T,>(setter: (v: T) => void) => (value: T) => {
    setter(value);
    setDirty(true);
  };

  if (!loaded) return null;

  return (
    <div className="card" style={{ padding: "var(--s4)", maxWidth: 640 }}>
      {quota ? (
        <div className="quota">
          <div className="quota__head">
            <span className="quota__used mono">{formatBytes(quota.used_bytes)}</span>
            <span className="quota__of micro">of {formatBytes(quota.limit_bytes)} {METRIC_WORD[quota.metric]}</span>
            {/* Worst state first. "spent" without a block means the ceiling
                was crossed but the cut-off has not landed yet -- the tick is
                due, or it could not reach the server -- which is worth saying
                rather than showing the same pill as a block that took. */}
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
      ) : (
        <p className="lede" style={{ marginBottom: "var(--s3)" }}>
          No traffic limit on this {subjectWord}. Set one and the panel counts what moves — from
          SoftEther&rsquo;s own counters, so it survives a restart — and{" "}
          {subject === "hub" ? "takes the hub offline" : "denies access and cuts the sessions"} the
          moment the ceiling is reached.
        </p>
      )}

      <div className="row2">
        <Field label="Limit" hint="How much may move before the ceiling bites.">
          <div className="qsplit">
            <input
              className="input mono"
              type="number"
              min={0}
              step="any"
              inputMode="decimal"
              value={limit}
              onChange={(e) => set(setLimit)(e.target.value)}
            />
            <select className="select" value={unit} onChange={(e) => set(setUnit)(e.target.value as QuotaUnit)}>
              {UNITS.map((u) => (
                <option key={u} value={u}>{u}</option>
              ))}
            </select>
          </div>
        </Field>
        <Field label="Counts" hint={METRIC_OPTIONS.find((m) => m.value === metric)?.hint}>
          <div className="seg" role="radiogroup" aria-label="What the limit counts">
            {METRIC_OPTIONS.map((m) => (
              <button
                key={m.value}
                role="radio"
                aria-checked={metric === m.value}
                className={metric === m.value ? "on" : ""}
                onClick={() => set(setMetric)(m.value)}
              >
                {m.label}
              </button>
            ))}
          </div>
        </Field>
      </div>

      {quota && (
        <label className="checkrow">
          <input type="checkbox" checked={enabled} onChange={(e) => set(setEnabled)(e.target.checked)} />
          <span style={{ minWidth: 0 }}>
            <span className="t">Enforce this limit</span>
            <span className="s">
              Off, the counter keeps running and the meter keeps filling, but nothing is ever cut
              off — useful for watching what a {subjectWord} would use before committing to a ceiling.
            </span>
          </span>
        </label>
      )}

      <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap", marginTop: "var(--s3)" }}>
        {(dirty || !quota) && (
          <button className="btn btn--primary" onClick={() => void save()} disabled={saving}>
            {saving && <span className="spin" />} {quota ? "Save limit" : "Set the limit"}
          </button>
        )}
        {dirty && quota && (
          <button className="btn" onClick={() => adopt(quota)}>Discard</button>
        )}
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
          subtitle={subject === "hub" ? hub : `${name} · ${hub}`}
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
                {" "}
                Because this {subjectWord} is currently cut off <b>by</b> the limit, removing it also
                lifts the block: {subject === "hub" ? "the hub goes back online" : "access is restored"}.
              </>
            )}
          </p>
        </Sheet>
      )}
    </div>
  );
}
