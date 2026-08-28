"use client";

import { useCallback, useState } from "react";
import { CheckRow, ConfirmSheet, Empty, Field, KV, LoadingBlock, SectionTitle, usePoll } from "../../components/bits";
import { api, type Wire } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { formatBytes, formatCount, formatDate, timeAgo } from "../../lib/util";
import { Pill } from "../../ui/Status";

/**
 * SecureNAT: the hub's built-in router. One switch, a NAT/DHCP options form,
 * and the two live tables (NAT sessions, DHCP leases) that show what it is
 * actually doing.
 */
export function HubSecureNat({ hub }: { hub: string }) {
  const [data, setData] = useState<Wire | null>(null);
  const [options, setOptions] = useState<Wire | null>(null);
  const [dirty, setDirty] = useState(false);
  const [confirming, setConfirming] = useState<null | boolean>(null);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.securenat(hub).catch(() => null);
    if (r) {
      setData(r);
      setOptions((current) => (dirty ? current : (r.options as Wire)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hub, dirty]);
  usePoll(load, "detail", [hub]);

  if (!data || !options) return <LoadingBlock label="asking the hub" />;

  const status = data.status as Wire;
  const running = Number(status?.NumTcpSessions_u32 ?? -1) >= 0 && data.enabled !== false;
  // GetSecureNATStatus answers even when disabled; the honest "is it on"
  // comes from the hub status flag, fetched separately by the parent -- so
  // here the enable/disable buttons are always offered.

  const set = (k: string, v: unknown) => {
    setOptions((o) => ({ ...(o ?? {}), [k]: v }));
    setDirty(true);
  };

  const save = () =>
    guard(async () => {
      const body = { ...options };
      delete body.RpcHubName_str;
      await api.setSecurenatOptions(hub, body);
      setDirty(false);
      await load();
    }, "SecureNAT options saved.");

  return (
    <>
      <SectionTitle
        actions={
          <div style={{ display: "flex", gap: "var(--s2)" }}>
            <button className="btn btn--sm btn--primary" onClick={() => setConfirming(true)}>
              Enable
            </button>
            <button className="btn btn--sm btn--danger" onClick={() => setConfirming(false)}>
              Disable
            </button>
          </div>
        }
      >
        SecureNAT
      </SectionTitle>

      <div className="fleet">
        <div className="fleet__cell"><div className="fleet__n">{formatCount(Number(status?.NumTcpSessions_u32))}</div><div className="micro">TCP sessions</div></div>
        <div className="fleet__cell"><div className="fleet__n">{formatCount(Number(status?.NumUdpSessions_u32))}</div><div className="micro">UDP sessions</div></div>
        <div className="fleet__cell"><div className="fleet__n">{formatCount(Number(status?.NumIcmpSessions_u32))}</div><div className="micro">ICMP</div></div>
        <div className="fleet__cell"><div className="fleet__n">{formatCount(Number(status?.NumDhcpClients_u32))}</div><div className="micro">DHCP clients</div></div>
        <div className="fleet__cell"><div className="fleet__n">{status?.IsKernelMode_bool ? "kernel" : status?.IsRawIpMode_bool ? "raw IP" : "user"}</div><div className="micro">NAT mode</div></div>
      </div>

      <SectionTitle>Virtual router</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 720 }}>
        <div className="row2">
          <Field label="Virtual IP" hint="The router's own address inside the hub.">
            <input className="input mono" value={String(options.Ip_ip ?? "")} onChange={(e) => set("Ip_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
          </Field>
          <Field label="Subnet mask">
            <input className="input mono" value={String(options.Mask_ip ?? "")} onChange={(e) => set("Mask_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
          </Field>
        </div>
        <div className="row2">
          <Field label="MTU">
            <input className="input mono" type="number" min={576} max={1500} value={Number(options.Mtu_u32 ?? 1500)} onChange={(e) => set("Mtu_u32", Number(e.target.value))} />
          </Field>
          <div />
        </div>

        <CheckRow checked={Boolean(options.UseNat_bool)} onChange={(v) => set("UseNat_bool", v)}
          label="Virtual NAT" hint="Clients reach the internet through the hub, translated to the host's address." />
        {Boolean(options.UseNat_bool) && (
          <div className="row2">
            <Field label="TCP timeout (s)">
              <input className="input mono" type="number" min={1} value={Number(options.NatTcpTimeout_u32 ?? 1800)} onChange={(e) => set("NatTcpTimeout_u32", Number(e.target.value))} />
            </Field>
            <Field label="UDP timeout (s)">
              <input className="input mono" type="number" min={1} value={Number(options.NatUdpTimeout_u32 ?? 60)} onChange={(e) => set("NatUdpTimeout_u32", Number(e.target.value))} />
            </Field>
          </div>
        )}

        <CheckRow checked={Boolean(options.UseDhcp_bool)} onChange={(v) => set("UseDhcp_bool", v)}
          label="Virtual DHCP server" hint="Hands addresses to clients as they connect." />
        {Boolean(options.UseDhcp_bool) && (
          <>
            <div className="row2">
              <Field label="Lease range start">
                <input className="input mono" value={String(options.DhcpLeaseIPStart_ip ?? "")} onChange={(e) => set("DhcpLeaseIPStart_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
              </Field>
              <Field label="Lease range end">
                <input className="input mono" value={String(options.DhcpLeaseIPEnd_ip ?? "")} onChange={(e) => set("DhcpLeaseIPEnd_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
              </Field>
            </div>
            <div className="row2">
              <Field label="Gateway to push" hint="Usually the virtual IP above.">
                <input className="input mono" value={String(options.DhcpGatewayAddress_ip ?? "")} onChange={(e) => set("DhcpGatewayAddress_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
              </Field>
              <Field label="Lease time (s)">
                <input className="input mono" type="number" min={60} value={Number(options.DhcpExpireTimeSpan_u32 ?? 7200)} onChange={(e) => set("DhcpExpireTimeSpan_u32", Number(e.target.value))} />
              </Field>
            </div>
            <div className="row2">
              <Field label="DNS server">
                <input className="input mono" value={String(options.DhcpDnsServerAddress_ip ?? "")} onChange={(e) => set("DhcpDnsServerAddress_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
              </Field>
              <Field label="DNS server 2">
                <input className="input mono" value={String(options.DhcpDnsServerAddress2_ip ?? "")} onChange={(e) => set("DhcpDnsServerAddress2_ip", e.target.value)} spellCheck={false} inputMode="decimal" />
              </Field>
            </div>
            <Field label="Domain name">
              <input className="input mono" value={String(options.DhcpDomainName_str ?? "")} onChange={(e) => set("DhcpDomainName_str", e.target.value)} spellCheck={false} />
            </Field>
            <Field label="Static routes to push" hint="Comma-separated network/mask/gateway triples, e.g. 192.168.5.0/255.255.255.0/10.0.0.2">
              <input className="input mono" value={String(options.DhcpPushRoutes_str ?? "")} onChange={(e) => { set("DhcpPushRoutes_str", e.target.value); set("ApplyDhcpPushRoutes_bool", true); }} spellCheck={false} />
            </Field>
          </>
        )}
        <CheckRow checked={Boolean(options.SaveLog_bool)} onChange={(v) => set("SaveLog_bool", v)}
          label="Log SecureNAT activity" />

        {dirty && (
          <button className="btn btn--primary" onClick={() => void save()}>Save options</button>
        )}
      </div>

      <NatTables hub={hub} />

      {confirming !== null && (
        <ConfirmSheet
          title={confirming ? "Enable SecureNAT?" : "Disable SecureNAT?"}
          verb={confirming ? "Enable" : "Disable"}
          danger={!confirming}
          body={
            confirming ? (
              <>
                The virtual NAT and DHCP server start inside <b>{hub}</b>. Never enable it on a hub
                bridged to a network that already has a DHCP server — the two will fight.
              </>
            ) : (
              <>Clients relying on the virtual router lose their internet path immediately.</>
            )
          }
          onClose={() => setConfirming(null)}
          onConfirm={async () => {
            await api.securenatEnable(hub, confirming);
            await load();
          }}
        />
      )}
    </>
  );
}

function NatTables({ hub }: { hub: string }) {
  const [tab, setTab] = useState<"nat" | "dhcp">("nat");
  const [nat, setNat] = useState<Wire[] | null>(null);
  const [dhcp, setDhcp] = useState<Wire[] | null>(null);

  const load = useCallback(async () => {
    if (tab === "nat") {
      const r = await api.natTable(hub).catch(() => null);
      if (r) setNat((r.NatTable as Wire[]) ?? []);
    } else {
      const r = await api.dhcpTable(hub).catch(() => null);
      if (r) setDhcp((r.DhcpTable as Wire[]) ?? []);
    }
  }, [hub, tab]);
  usePoll(load, "detail", [hub, tab]);

  const PROTO: Record<number, string> = { 0: "UDP", 1: "TCP", 2: "DNS", 3: "ICMP" };

  return (
    <>
      <SectionTitle
        actions={
          <div className="seg">
            <button className={tab === "nat" ? "on" : ""} onClick={() => setTab("nat")}>NAT sessions</button>
            <button className={tab === "dhcp" ? "on" : ""} onClick={() => setTab("dhcp")}>DHCP leases</button>
          </div>
        }
      >
        Live tables
      </SectionTitle>

      {tab === "nat" ? (
        nat === null ? (
          <LoadingBlock />
        ) : nat.length === 0 ? (
          <Empty title="no NAT sessions">Translation entries appear as clients talk through the virtual NAT.</Empty>
        ) : (
          <div className="card tcard">
            <div className="tscroll">
              <table className="dtable">
                <thead>
                  <tr>
                    <th>Proto</th><th>Source</th><th>Destination</th><th>Transfer</th><th>Last activity</th>
                  </tr>
                </thead>
                <tbody>
                  {nat.slice(0, 200).map((n) => (
                    <tr key={String(n.Id_u32)}>
                      <td>{PROTO[Number(n.Protocol_u32)] ?? n.Protocol_u32}</td>
                      <td className="tmono">{String(n.SrcIp_ip)}:{String(n.SrcPort_u32)}</td>
                      <td className="tmono">{String(n.DestHost_str || n.DestIp_ip)}:{String(n.DestPort_u32)}</td>
                      <td className="tmono">{formatBytes(Number(n.SendSize_u64) + Number(n.RecvSize_u64))}</td>
                      <td className="tmono">{timeAgo(n.LastCommTime_dt as string)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {nat.length > 200 && <div className="micro" style={{ padding: "var(--s2) var(--s4)" }}>showing the first 200 of {nat.length}</div>}
          </div>
        )
      ) : dhcp === null ? (
        <LoadingBlock />
      ) : dhcp.length === 0 ? (
        <Empty title="no leases">Leases appear as the virtual DHCP server hands out addresses.</Empty>
      ) : (
        <div className="card tcard">
          <div className="tscroll">
            <table className="dtable">
              <thead>
                <tr><th>IP</th><th>MAC</th><th>Hostname</th><th>Leased</th><th>Expires</th></tr>
              </thead>
              <tbody>
                {dhcp.map((l) => (
                  <tr key={String(l.Id_u32)}>
                    <td className="tmono">{String(l.IpAddress_ip)}</td>
                    <td className="tmono">{formatMac(String(l.MacAddress_bin ?? ""))}</td>
                    <td className="tmono">{String(l.Hostname_str || "—")}</td>
                    <td className="tmono">{timeAgo(l.LeasedTime_dt as string)}</td>
                    <td className="tmono">{formatDate(l.ExpireTime_dt as string)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}

/** Wire MACs are base64 bytes; people read colon-hex. */
export function formatMac(b64: string): string {
  try {
    return Array.from(atob(b64))
      .map((c) => c.charCodeAt(0).toString(16).padStart(2, "0"))
      .join(":");
  } catch {
    return b64 || "—";
  }
}
