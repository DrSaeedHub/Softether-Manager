"use client";

import { useCallback, useState } from "react";
import { CheckRow, ConfirmSheet, Empty, ErrorAlert, Field, KV, LoadingBlock, SectionTitle, usePoll } from "../../components/bits";
import { api, type Wire } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { formatBytes, formatDate, timeAgo } from "../../lib/util";
import { IconCascade, IconPlus, IconTrash } from "../../ui/Icon";
import { Sheet } from "../../ui/Sheet";
import { Pill } from "../../ui/Status";

/**
 * Cascade connections: this hub dialing out to a hub on another server and
 * bridging the two into one Ethernet segment. Each link is a stored client
 * account with its own lifecycle (offline / connecting / established).
 */
export function HubLinks({ hub }: { hub: string }) {
  const [links, setLinks] = useState<Wire[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [statusOf, setStatusOf] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.links(hub).catch(() => null);
    if (r) setLinks((r.LinkList as Wire[]) ?? []);
  }, [hub]);
  usePoll(load, "detail", [hub]);

  const toggle = (link: Wire) =>
    guard(async () => {
      await api.linkOnline(hub, String(link.AccountName_utf), !link.Online_bool);
      await load();
    }, link.Online_bool ? "Link taken offline." : "Link brought online.");

  return (
    <>
      <SectionTitle
        count={links?.length}
        actions={
          <button className="btn btn--primary btn--sm" onClick={() => setCreating(true)}>
            <IconPlus size={14} /> New cascade
          </button>
        }
      >
        Cascade connections
      </SectionTitle>

      {links === null ? (
        <LoadingBlock />
      ) : links.length === 0 ? (
        <Empty
          title="no cascades"
          action={
            <button className="btn btn--primary" onClick={() => setCreating(true)}>
              <IconPlus size={15} /> Create a cascade
            </button>
          }
        >
          A cascade bridges this hub to a hub on another SoftEther server, making the two one
          layer-2 network. Site-to-site VPN, in one object.
        </Empty>
      ) : (
        <div className="rows">
          {links.map((l) => {
            const name = String(l.AccountName_utf);
            return (
              <div key={name} className="row" style={{ cursor: "pointer" }} onClick={() => setStatusOf(name)}>
                <div className="row__main">
                  <div className="row__name">
                    <IconCascade size={15} />
                    <span className="mono">{name}</span>
                    {l.Online_bool ? (
                      l.Connected_bool ? (
                        <Pill kind="ok" label="established" />
                      ) : (
                        <Pill kind="busy" label="connecting" />
                      )
                    ) : (
                      <Pill kind="idle" label="offline" />
                    )}
                  </div>
                  <div className="spec">
                    <span className="chip"><i>to</i>{String(l.Hostname_str)}</span>
                    <span className="chip"><i>hub</i>{String(l.TargetHubName_str)}</span>
                    {l.Connected_bool ? <span className="chip"><i>since</i>{timeAgo(l.ConnectedTime_dt as string)}</span> : null}
                  </div>
                </div>
                <div className="row__side" onClick={(e) => e.stopPropagation()}>
                  <button className="btn btn--sm" onClick={() => void toggle(l)}>
                    {l.Online_bool ? "Offline" : "Online"}
                  </button>
                  <button className="btn btn--sm btn--ghost" onClick={() => setDeleting(name)} aria-label="Delete">
                    <IconTrash size={14} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {creating && (
        <LinkSheet
         
          hub={hub}
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false);
            void load();
          }}
        />
      )}
      {statusOf && <LinkStatusSheet hub={hub} name={statusOf} onClose={() => setStatusOf(null)} />}
      {deleting && (
        <ConfirmSheet
          title={`Delete cascade ${deleting}?`}
          verb="Delete"
          body={<>The bridge to the other network is torn down and the stored account removed.</>}
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await api.deleteLink(hub, deleting);
            void load();
          }}
        />
      )}
    </>
  );
}

function LinkSheet({ hub, onClose, onSaved }: { hub: string; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState(443);
  const [targetHub, setTargetHub] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [online, setOnline] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { push } = useToast();

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.createLink(hub, {
        AccountName_utf: name.trim() || `${host}-${targetHub}`,
        Hostname_str: host.trim(),
        Port_u32: port,
        HubName_str: targetHub.trim(),
        Username_str: username.trim(),
        // Wire auth type 2 = plain password; the server hashes it itself.
        AuthType_u32: 2,
        PlainPassword_str: password,
        UseEncrypt_bool: true,
        MaxConnection_u32: 8,
        AdditionalConnectionInterval_u32: 1,
        ConnectionDisconnectSpan_u32: 0,
      });
      const account = name.trim() || `${host}-${targetHub}`;
      if (online) await api.linkOnline(hub, account, true);
      push("ok", "Cascade created.");
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <Sheet
      title="New cascade"
      subtitle={`from hub ${hub}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={save} disabled={busy || !host.trim() || !targetHub.trim() || !username.trim()}>
            {busy && <span className="spin" />} Create
          </button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <Field label="Name" hint="What this cascade is called here. Empty derives one.">
        <input className="input mono" value={name} onChange={(e) => setName(e.target.value)} autoCapitalize="none" spellCheck={false} />
      </Field>
      <div className="row2">
        <Field label="Destination server">
          <input className="input mono" value={host} onChange={(e) => setHost(e.target.value)} placeholder="vpn2.example.net" spellCheck={false} autoCapitalize="none" inputMode="url" />
        </Field>
        <Field label="Port">
          <input className="input mono" type="number" min={1} max={65535} value={port} onChange={(e) => setPort(Number(e.target.value))} />
        </Field>
      </div>
      <Field label="Destination hub">
        <input className="input mono" value={targetHub} onChange={(e) => setTargetHub(e.target.value)} autoCapitalize="none" spellCheck={false} />
      </Field>
      <div className="row2">
        <Field label="Username" hint="A user on the destination hub.">
          <input className="input mono" value={username} onChange={(e) => setUsername(e.target.value)} autoCapitalize="none" spellCheck={false} />
        </Field>
        <Field label="Password">
          <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off" />
        </Field>
      </div>
      <CheckRow checked={online} onChange={setOnline} label="Connect immediately" />
    </Sheet>
  );
}

function LinkStatusSheet({ hub, name, onClose }: { hub: string; name: string; onClose: () => void }) {
  const [status, setStatus] = useState<Wire | null>(null);
  const [error, setError] = useState<string | null>(null);

  usePoll(
    async () => {
      try {
        setStatus(await api.linkStatus(hub, name));
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    5000,
    [hub, name],
  );

  return (
    <Sheet title="Cascade status" subtitle={name} onClose={onClose} wide>
      {error && <ErrorAlert>{error} — the link is probably offline.</ErrorAlert>}
      {!status && !error && <LoadingBlock />}
      {status && (
        <KV
          rows={[
            ["state", status.Connected_bool ? "established" : status.Active_bool ? "connecting" : "offline"],
            ["server", `${status.ServerName_str ?? "—"}:${status.ServerPort_u32 ?? ""}`],
            ["product", `${status.ServerProductName_str ?? "—"}`],
            ["cipher", String(status.CipherName_str || "—")],
            ["protocol", String(status.UnderlayProtocol_str || "—")],
            ["TCP connections", `${status.NumTcpConnections_u32 ?? 0} / ${status.MaxTcpConnections_u32 ?? 0}`],
            ["started", formatDate(status.StartTime_dt as string)],
            ["sent", formatBytes(Number(status.TotalSendSizeReal_u64 ?? status.TotalSendSize_u64))],
            ["received", formatBytes(Number(status.TotalRecvSizeReal_u64 ?? status.TotalRecvSize_u64))],
          ]}
        />
      )}
    </Sheet>
  );
}
