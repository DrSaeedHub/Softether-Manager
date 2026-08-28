"use client";

import { useMemo, useState } from "react";
import type { Wire } from "../lib/api";
import { POLICY_FIELDS, POLICY_GROUPS } from "../lib/se";
import { formatBytes } from "../lib/util";
import { CheckRow, Field } from "./bits";

/**
 * The security policy: SoftEther's forty knobs, arranged so a person can
 * find the one they mean. Booleans are tappable rows; numbers show what
 * their zero means and, for the bandwidth pair, what the number amounts to
 * in human units.
 *
 * The editor works on a draft; the caller saves. `UsePolicy_bool` is the
 * master switch -- off, the user rides the hub defaults (or their group's
 * policy) and everything here is moot, so the form says that instead of
 * pretending the fields still bite.
 */
export function PolicyEditor({
  value,
  onChange,
  subject,
}: {
  value: Wire;
  onChange: (next: Wire) => void;
  subject: "user" | "group";
}) {
  const enabled = Boolean(value.UsePolicy_bool);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({ Access: true, "Bandwidth & sessions": true });

  const set = (key: string, v: unknown) => onChange({ ...value, [key]: v });

  const byGroup = useMemo(() => {
    const map = new Map<string, typeof POLICY_FIELDS>();
    for (const g of POLICY_GROUPS) map.set(g, []);
    for (const f of POLICY_FIELDS) map.get(f.group)?.push(f);
    return map;
  }, []);

  return (
    <div>
      <CheckRow
        checked={enabled}
        onChange={(v) => set("UsePolicy_bool", v)}
        label="Apply a security policy"
        hint={
          subject === "user"
            ? "Off, this user follows the hub defaults (or their group's policy, if the group has one)."
            : "Off, members of this group follow the hub defaults."
        }
      />

      {enabled &&
        POLICY_GROUPS.map((groupName) => {
          const fields = byGroup.get(groupName) ?? [];
          if (!fields.length) return null;
          const open = openGroups[groupName] ?? false;
          const activeCount = fields.filter((f) =>
            f.kind === "bool" ? (groupName === "Access" && f.key === "policy:Access_bool" ? !value[f.key] : Boolean(value[f.key])) : Number(value[f.key]) > 0,
          ).length;
          return (
            <div key={groupName} className="polgroup">
              <button
                className="polgroup__h"
                onClick={() => setOpenGroups((g) => ({ ...g, [groupName]: !open }))}
                aria-expanded={open}
              >
                <span>{groupName}</span>
                {activeCount > 0 && <span className="chip chip--brand">{activeCount} set</span>}
                <span className={`rail__chev${open ? " open" : ""}`} style={{ marginInlineStart: "auto" }}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"><path d="m9 6 6 6-6 6" /></svg>
                </span>
              </button>
              {open && (
                <div className="polgroup__b">
                  {fields.map((f) =>
                    f.kind === "bool" ? (
                      <CheckRow
                        key={f.key}
                        checked={Boolean(value[f.key])}
                        onChange={(v) => set(f.key, v)}
                        label={f.label}
                        hint={f.help}
                      />
                    ) : (
                      <Field
                        key={f.key}
                        label={
                          <>
                            {f.label}
                            {f.unit ? <span className="micro"> · {f.unit}</span> : null}
                          </>
                        }
                        hint={
                          <>
                            {f.help} 0 = {f.zero}.
                            {(f.key === "policy:MaxUpload_u32" || f.key === "policy:MaxDownload_u32") &&
                              Number(value[f.key]) > 0 && (
                                <>
                                  {" "}Currently <b>{formatBytes(Number(value[f.key]))}/s ≈ {((Number(value[f.key]) * 8) / 1_000_000).toFixed(1)} Mbps</b>.
                                </>
                              )}
                          </>
                        }
                      >
                        <input
                          className="input mono"
                          type="number"
                          min={0}
                          value={Number(value[f.key] ?? 0)}
                          onChange={(e) => set(f.key, Math.max(0, Number(e.target.value)))}
                          inputMode="numeric"
                        />
                      </Field>
                    ),
                  )}
                </div>
              )}
            </div>
          );
        })}
    </div>
  );
}

/** Pull only the policy fields out of a GetUser/GetGroup payload. */
export function extractPolicy(payload: Wire): Wire {
  const out: Wire = { UsePolicy_bool: Boolean(payload.UsePolicy_bool) };
  for (const f of POLICY_FIELDS) out[f.key] = payload[f.key] ?? (f.kind === "bool" ? false : 0);
  return out;
}
