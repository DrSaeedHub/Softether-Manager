"use client";

import { useCallback, useState } from "react";
import { CheckRow, ConfirmSheet, Empty, ErrorAlert, Field, LoadingBlock, SectionTitle, usePoll } from "../../components/bits";
import { api, type Wire } from "../../lib/api";
import { ACCESS_PROTOCOLS } from "../../lib/se";
import { useToast } from "../../lib/toast";
import { IconPlus, IconShield, IconTrash } from "../../ui/Icon";
import { Sheet } from "../../ui/Sheet";
import { Pill } from "../../ui/Status";

/**
 * Access control: the hub's packet filter, and the source-IP limit list.
 *
 * A rule reads the way a person says it: "pass/discard <protocol> from
 * <source> to <destination>". The editor asks in that order, and the list
 * renders each rule as that sentence.
 */
export function HubAccess({ hub }: { hub: string }) {
  const [rules, setRules] = useState<Wire[] | null>(null);
  const [editing, setEditing] = useState<Wire | "new" | null>(null);
  const [deleting, setDeleting] = useState<Wire | null>(null);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.accessList(hub).catch(() => null);
    if (r) setRules((r.AccessList as Wire[]) ?? []);
  }, [hub]);
  usePoll(load, "list", [hub]);

  return (
    <>
      <SectionTitle
        count={rules?.length}
        actions={
          <button className="btn btn--primary btn--sm" onClick={() => setEditing("new")}>
            <IconPlus size={14} /> New rule
          </button>
        }
      >
        Access list
      </SectionTitle>

      {rules === null ? (
        <LoadingBlock label="loading rules" />
      ) : rules.length === 0 ? (
        <Empty
          title="no rules — everything passes"
          action={
            <button className="btn btn--primary" onClick={() => setEditing("new")}>
              <IconPlus size={15} /> Add the first rule
            </button>
          }
        >
          Access rules filter packets inside the hub, by protocol, address, port and user. Lower
          priority numbers run first; the first match decides.
        </Empty>
      ) : (
        <div className="rows">
          {[...rules]
            .sort((a, b) => Number(a.Priority_u32) - Number(b.Priority_u32))
            .map((r) => (
              <div key={String(r.Id_u32)} className="row" style={{ cursor: "pointer" }} onClick={() => setEditing(r)}>
                <div className="row__main">
                  <div className="row__name">
                    <span className="mono micro" style={{ color: "var(--text-faint)" }}>#{String(r.Priority_u32)}</span>
                    <Pill kind={r.Discard_bool ? "err" : "ok"} label={r.Discard_bool ? "discard" : "pass"} />
                    {!r.Active_bool && <Pill kind="idle" label="disabled" />}
                    <span>{ruleSentence(r)}</span>
                  </div>
                  {r.Note_utf ? <div className="row__note">{String(r.Note_utf)}</div> : null}
                </div>
                <div className="row__side">
                  <button
                    className="btn btn--sm btn--ghost"
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleting(r);
                    }}
                    aria-label="Delete rule"
                  >
                    <IconTrash size={14} />
                  </button>
                </div>
              </div>
            ))}
        </div>
      )}

      <AcList hub={hub} />

      {editing && (
        <AccessRuleSheet
         
          hub={hub}
          rule={editing === "new" ? null : editing}
          allRules={rules ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      )}
      {deleting && (
        <ConfirmSheet
          title="Delete this rule?"
          verb="Delete rule"
          body={<>{ruleSentence(deleting)}</>}
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await api.deleteAccess(hub, Number(deleting.Id_u32));
            void load();
          }}
        />
      )}
    </>
  );
}

function ruleSentence(r: Wire): string {
  const proto = ACCESS_PROTOCOLS[Number(r.Protocol_u32)] ?? `proto ${r.Protocol_u32}`;
  const v6 = r.IsIPv6_bool;
  const src = r.SrcUsername_str
    ? `user ${r.SrcUsername_str}`
    : (v6 ? "" : (r.SrcIpAddress_ip && r.SrcIpAddress_ip !== "0.0.0.0" ? `${r.SrcIpAddress_ip}/${r.SrcSubnetMask_ip}` : "")) || "anywhere";
  const dst = r.DestUsername_str
    ? `user ${r.DestUsername_str}`
    : (v6 ? "" : (r.DestIpAddress_ip && r.DestIpAddress_ip !== "0.0.0.0" ? `${r.DestIpAddress_ip}/${r.DestSubnetMask_ip}` : "")) || "anywhere";
  const ports =
    Number(r.DestPortStart_u32) > 0
      ? ` port ${r.DestPortStart_u32}${Number(r.DestPortEnd_u32) > Number(r.DestPortStart_u32) ? `–${r.DestPortEnd_u32}` : ""}`
      : "";
  return `${proto}${v6 ? " (IPv6)" : ""} from ${src} to ${dst}${ports}`;
}

function AccessRuleSheet({ hub,
  rule,
  allRules,
  onClose,
  onSaved,
}: {
    hub: string;
  rule: Wire | null;
  allRules: Wire[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const editing = Boolean(rule);
  const [draft, setDraft] = useState<Wire>(() =>
    rule
      ? { ...rule }
      : {
          Active_bool: true,
          Discard_bool: false,
          Priority_u32: (Math.max(0, ...allRules.map((r) => Number(r.Priority_u32))) || 0) + 100,
          Protocol_u32: 0,
          IsIPv6_bool: false,
          Note_utf: "",
        },
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { push } = useToast();
  const set = (k: string, v: unknown) => setDraft((d) => ({ ...d, [k]: v }));

  const save = async () => {
    setError(null);
    setBusy(true);
    try {
      if (editing) {
        // The API has no "edit one rule": the list is replaced with this rule
        // swapped in, atomically.
        const next = allRules.map((r) => (r.Id_u32 === rule?.Id_u32 ? draft : r));
        await api.setAccessList(hub, next);
      } else {
        await api.addAccess(hub, draft);
      }
      push("ok", editing ? "Rule saved." : "Rule added.");
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const tcpish = Number(draft.Protocol_u32) === 6 || Number(draft.Protocol_u32) === 17;

  return (
    <Sheet
      title={editing ? "Edit rule" : "New access rule"}
      subtitle={`hub ${hub}`}
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={save} disabled={busy}>
            {busy && <span className="spin" />}
            {editing ? "Save rule" : "Add rule"}
          </button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <div style={{ display: "grid", gap: "var(--s1)" }}>
        <div className="seg">
          <button className={!draft.Discard_bool ? "on" : ""} onClick={() => set("Discard_bool", false)}>Pass</button>
          <button className={draft.Discard_bool ? "on" : ""} onClick={() => set("Discard_bool", true)}>Discard</button>
        </div>
        <div className="row2">
          <Field label="Priority" hint="Lower runs first.">
            <input className="input mono" type="number" min={1} value={Number(draft.Priority_u32)} onChange={(e) => set("Priority_u32", Number(e.target.value))} />
          </Field>
          <Field label="Protocol">
            <select className="select" value={Number(draft.Protocol_u32)} onChange={(e) => set("Protocol_u32", Number(e.target.value))}>
              {Object.entries(ACCESS_PROTOCOLS).map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Note">
          <input className="input" value={String(draft.Note_utf ?? "")} onChange={(e) => set("Note_utf", e.target.value)} />
        </Field>

        <SectionTitle>Source</SectionTitle>
        <div className="row2">
          <Field label="IPv4 address" hint="Empty matches any source.">
            <input className="input mono" placeholder="10.0.0.0" value={String(draft.SrcIpAddress_ip ?? "")} onChange={(e) => set("SrcIpAddress_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
          </Field>
          <Field label="Subnet mask">
            <input className="input mono" placeholder="255.255.255.0" value={String(draft.SrcSubnetMask_ip ?? "")} onChange={(e) => set("SrcSubnetMask_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
          </Field>
        </div>
        <Field label="Or source user" hint="Matches packets sent by this user's sessions.">
          <input className="input mono" value={String(draft.SrcUsername_str ?? "")} onChange={(e) => set("SrcUsername_str", e.target.value)} spellCheck={false} autoCapitalize="none" />
        </Field>

        <SectionTitle>Destination</SectionTitle>
        <div className="row2">
          <Field label="IPv4 address" hint="Empty matches any destination.">
            <input className="input mono" placeholder="10.0.0.8" value={String(draft.DestIpAddress_ip ?? "")} onChange={(e) => set("DestIpAddress_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
          </Field>
          <Field label="Subnet mask">
            <input className="input mono" placeholder="255.255.255.255" value={String(draft.DestSubnetMask_ip ?? "")} onChange={(e) => set("DestSubnetMask_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
          </Field>
        </div>
        <Field label="Or destination user">
          <input className="input mono" value={String(draft.DestUsername_str ?? "")} onChange={(e) => set("DestUsername_str", e.target.value)} spellCheck={false} autoCapitalize="none" />
        </Field>
        {tcpish && (
          <div className="row2">
            <Field label="Destination ports" hint="Start – end; same number for one port.">
              <div style={{ display: "flex", gap: "var(--s2)" }}>
                <input className="input mono" type="number" min={0} max={65535} placeholder="from" value={Number(draft.DestPortStart_u32 ?? 0) || ""} onChange={(e) => set("DestPortStart_u32", Number(e.target.value))} />
                <input className="input mono" type="number" min={0} max={65535} placeholder="to" value={Number(draft.DestPortEnd_u32 ?? 0) || ""} onChange={(e) => set("DestPortEnd_u32", Number(e.target.value))} />
              </div>
            </Field>
            <Field label="Source ports">
              <div style={{ display: "flex", gap: "var(--s2)" }}>
                <input className="input mono" type="number" min={0} max={65535} placeholder="from" value={Number(draft.SrcPortStart_u32 ?? 0) || ""} onChange={(e) => set("SrcPortStart_u32", Number(e.target.value))} />
                <input className="input mono" type="number" min={0} max={65535} placeholder="to" value={Number(draft.SrcPortEnd_u32 ?? 0) || ""} onChange={(e) => set("SrcPortEnd_u32", Number(e.target.value))} />
              </div>
            </Field>
          </div>
        )}

        <SectionTitle>Simulation (optional)</SectionTitle>
        <div className="row2">
          <Field label="Delay" hint="ms added to matching packets — for testing bad links.">
            <input className="input mono" type="number" min={0} max={10000} value={Number(draft.Delay_u32 ?? 0)} onChange={(e) => set("Delay_u32", Number(e.target.value))} />
          </Field>
          <Field label="Loss" hint="% of matching packets dropped.">
            <input className="input mono" type="number" min={0} max={100} value={Number(draft.Loss_u32 ?? 0)} onChange={(e) => set("Loss_u32", Number(e.target.value))} />
          </Field>
        </div>
        <CheckRow checked={Boolean(draft.Active_bool)} onChange={(v) => set("Active_bool", v)} label="Rule enabled" />
      </div>
    </Sheet>
  );
}

/* ── the source-IP limit list ─────────────────────────────────────────────── */

function AcList({ hub }: { hub: string }) {
  const [rules, setRules] = useState<Wire[] | null>(null);
  const [adding, setAdding] = useState(false);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.acList(hub).catch(() => null);
    if (r) setRules((r.ACList as Wire[]) ?? []);
  }, [hub]);
  usePoll(load, "list", [hub]);

  const remove = (rule: Wire) =>
    guard(async () => {
      const next = (rules ?? []).filter((r) => r.Id_u32 !== rule.Id_u32);
      await api.setAcList(hub, next);
      await load();
    }, "Entry removed.");

  return (
    <>
      <SectionTitle
        count={rules?.length}
        actions={
          <button className="btn btn--sm" onClick={() => setAdding(true)}>
            <IconPlus size={14} /> Add entry
          </button>
        }
      >
        Source-IP limits
      </SectionTitle>
      {rules === null ? (
        <LoadingBlock />
      ) : rules.length === 0 ? (
        <Empty title="no limits — clients may connect from any address">
          Entries here allow or deny the act of connecting to this hub by the client's source IP,
          before authentication is even attempted.
        </Empty>
      ) : (
        <div className="rows">
          {[...rules]
            .sort((a, b) => Number(a.Priority_u32) - Number(b.Priority_u32))
            .map((r, i) => (
              <div key={i} className="row">
                <div className="row__main">
                  <div className="row__name">
                    <span className="mono micro" style={{ color: "var(--text-faint)" }}>#{String(r.Priority_u32)}</span>
                    <Pill kind={r.Deny_bool ? "err" : "ok"} label={r.Deny_bool ? "deny" : "allow"} />
                    <span className="mono">
                      {String(r.IpAddress_ip)}
                      {r.Masked_bool ? `/${r.SubnetMask_ip}` : ""}
                    </span>
                  </div>
                </div>
                <div className="row__side">
                  <button className="btn btn--sm btn--ghost" onClick={() => void remove(r)} aria-label="Remove">
                    <IconTrash size={14} />
                  </button>
                </div>
              </div>
            ))}
        </div>
      )}
      {adding && (
        <AcSheet
          existing={rules ?? []}
          onClose={() => setAdding(false)}
          onSave={async (entry) => {
            await api.setAcList(hub, [...(rules ?? []), entry]);
            setAdding(false);
            await load();
          }}
        />
      )}
    </>
  );
}

function AcSheet({ existing, onClose, onSave }: { existing: Wire[]; onClose: () => void; onSave: (e: Wire) => Promise<void> }) {
  const [deny, setDeny] = useState(false);
  const [ip, setIp] = useState("");
  const [masked, setMasked] = useState(false);
  const [mask, setMask] = useState("255.255.255.0");
  const [priority, setPriority] = useState((Math.max(0, ...existing.map((r) => Number(r.Priority_u32))) || 0) + 100);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave({
        Priority_u32: priority,
        Deny_bool: deny,
        Masked_bool: masked,
        IpAddress_ip: ip.trim(),
        SubnetMask_ip: masked ? mask.trim() : "255.255.255.255",
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <Sheet
      title="Source-IP entry"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={save} disabled={busy || !ip.trim()}>
            {busy && <span className="spin" />} Add
          </button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <div className="seg" style={{ marginBottom: "var(--s3)" }}>
        <button className={!deny ? "on" : ""} onClick={() => setDeny(false)}>Allow</button>
        <button className={deny ? "on" : ""} onClick={() => setDeny(true)}>Deny</button>
      </div>
      <Field label="IP address">
        <input className="input mono" value={ip} onChange={(e) => setIp(e.target.value)} placeholder="203.0.113.7" spellCheck={false} inputMode="decimal" />
      </Field>
      <CheckRow checked={masked} onChange={setMasked} label="Whole subnet" hint="Apply to a network rather than one address." />
      {masked && (
        <Field label="Subnet mask">
          <input className="input mono" value={mask} onChange={(e) => setMask(e.target.value)} spellCheck={false} inputMode="decimal" />
        </Field>
      )}
      <Field label="Priority" hint="Lower runs first.">
        <input className="input mono" type="number" min={1} value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
      </Field>
    </Sheet>
  );
}
