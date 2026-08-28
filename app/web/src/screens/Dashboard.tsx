"use client";

import { useCallback, useMemo, useState } from "react";
import { CpuCard, DiskCard, MemoryCard, NetworkCard, SwapCard } from "../components/ResourceCards";
import { Empty, ErrorAlert, Field, LoadingBlock, SectionTitle, usePoll } from "../components/bits";
import { api, type Wire } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useServer } from "../lib/server";
import { Link, navigate, seg } from "../lib/router";
import { useToast } from "../lib/toast";
import { HUB_TYPES } from "../lib/se";
import { formatBytes, formatCount, formatDuration } from "../lib/util";
import { Sheet } from "../ui/Sheet";
import {
  IconChevron,
  IconLogs,
  IconPlus,
  IconPulse,
  IconSettings,
  IconTerminal,
} from "../ui/Icon";
import { OnlinePill } from "../ui/Status";

/**
 * The home screen, laid out as a deck: a greeting, a row of KPIs, the hubs
 * as a feed, and — on the light hero card — the server itself with its
 * uptime and the two actions an operator reaches for first.
 */
export function Dashboard() {
  const { probe, health, refresh } = useServer();
  const { user } = useAuth();
  const [resources, setResources] = useState<Wire | null>(null);
  const [overview, setOverview] = useState<Wire | null>(null);
  const [creatingHub, setCreatingHub] = useState(false);

  const loadResources = useCallback(async () => {
    setResources(await api.resources().catch(() => null));
  }, []);
  usePoll(loadResources, "live", []);

  const loadOverview = useCallback(async () => {
    if (health === "online") setOverview(await api.overview().catch(() => null));
  }, [health]);
  usePoll(loadOverview, "detail", [health]);

  const history = useMemo(() => {
    const points = (resources?.history as Wire[]) ?? [];
    return {
      cpu: points.map((p) => Number(p.cpu) || 0),
      memory: points.map((p) => Number(p.memory) || 0),
      rx: points.map((p) => Number(p.rx) || 0),
      tx: points.map((p) => Number(p.tx) || 0),
    };
  }, [resources]);

  const section = (key: string): Wire | null =>
    overview?.[key]?.ok ? (overview[key].data as Wire) : null;
  const status = section("status");
  const hubs = section("hubs")?.HubList as Wire[] | undefined;
  const listeners = section("listeners")?.ListenerList as Wire[] | undefined;
  const ipsec = section("ipsec");
  const openvpn = section("openvpn");
  const azure = section("azure");
  const ddns = section("ddns");

  const uptimeSeconds = useMemo(() => {
    if (!status?.StartTime_dt || !status?.CurrentTime_dt) return null;
    return (new Date(status.CurrentTime_dt).getTime() - new Date(status.StartTime_dt).getTime()) / 1000;
  }, [status]);

  const hour = new Date().getHours();
  const daypart = hour < 5 ? "night" : hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";

  return (
    <div className="page">
      <div className="hello">
        <div style={{ minWidth: 0 }}>
          <h1 className="hello__t">Good {daypart}{user ? `, ${user}` : ""}</h1>
          <div className="hello__s">
            {probe?.online ? (
              <>Your VPN server is healthy. <span className="mono">{probe.hostname} · {probe.version}</span></>
            ) : health === "offline" ? (
              "The VPN server is not answering."
            ) : (
              "Here's an overview of this machine and its VPN server."
            )}
          </div>
        </div>
      </div>

      {health === "unconfigured" ? (
        <Empty
          title="not connected to SoftEther yet"
          action={
            <Link to="/connect" className="btn btn--primary">
              Connect the server
            </Link>
          }
        >
          Tell the panel the management port and administrator password of the SoftEther
          instance on this machine, and everything else lights up.
        </Empty>
      ) : health === "offline" ? (
        <ErrorAlert>
          <div className="alert__t">The VPN server is not answering.</div>
          {probe?.error} — <Link to="/connect" className="linkish">check the connection</Link>
        </ErrorAlert>
      ) : (
        <div className="dash">
          {/* ── main column ─────────────────────────────────────────────── */}
          <div className="dash__main">
            <div className="kpis stagger">
              <Kpi label="Sessions" value={formatCount(status?.NumSessionsTotal_u32)} sub="across all hubs" />
              <Kpi label="Users" value={formatCount(status?.NumUsers_u32)} sub="accounts on the server" />
              <Kpi
                label="Received"
                value={formatBytes((status?.["Recv.UnicastBytes_u64"] ?? 0) + (status?.["Recv.BroadcastBytes_u64"] ?? 0))}
                sub="since the server started"
              />
              <Kpi
                label="Sent"
                value={formatBytes((status?.["Send.UnicastBytes_u64"] ?? 0) + (status?.["Send.BroadcastBytes_u64"] ?? 0))}
                sub="since the server started"
              />
            </div>

            <div className="card panelcard">
              <div className="panelcard__h">
                <div style={{ minWidth: 0 }}>
                  <div className="panelcard__t">Virtual hubs</div>
                  <div className="panelcard__s">
                    {hubs?.length
                      ? `${hubs.length} hub${hubs.length === 1 ? "" : "s"} on this server`
                      : "where users, sessions and access control live"}
                  </div>
                </div>
                <button className="btn btn--sm" onClick={() => setCreatingHub(true)}>
                  <IconPlus size={14} /> New hub
                </button>
              </div>
              {!hubs ? (
                <LoadingBlock label="loading hubs" />
              ) : hubs.length === 0 ? (
                <p className="lede" style={{ margin: 0 }}>
                  A Virtual Hub is where users, sessions and access control live. Create the
                  first one.
                </p>
              ) : (
                <div className="feed">
                  {hubs.map((h) => {
                    const name = String(h.HubName_str ?? "");
                    const online = Boolean(h.Online_bool);
                    return (
                      <button key={name} className="feed__i" onClick={() => navigate(`/hub/${seg(name)}`)}>
                        <span className={`feed__ava${online ? " feed__ava--on" : ""}`}>{name.slice(0, 1)}</span>
                        <span className="feed__m">
                          <span className="feed__t">{name}</span>
                          <span className="feed__s">
                            {formatCount(h.NumUsers_u32)} users · {formatCount(h.NumSessions_u32)} sessions ·{" "}
                            {h.HubType_u32 !== undefined ? HUB_TYPES[h.HubType_u32 as number]?.split(" ")[0] : "—"}
                          </span>
                        </span>
                        <span className="feed__side">
                          <OnlinePill online={online} />
                          <IconChevron size={15} style={{ color: "var(--text-faint)" }} />
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* the machine under it all */}
            {resources === null ? null : resources.available === false ? (
              <div className="alert alert--info" style={{ marginBottom: 0 }}>
                <div>
                  <div className="alert__t">Resource monitoring is asleep.</div>
                  {String(resources.reason ?? "It runs where /proc exists — a Linux host.")}
                </div>
              </div>
            ) : (
              <>
                <SectionTitle>This machine</SectionTitle>
                <div className="metrics" style={{ marginBottom: 0 }}>
                  <CpuCard snapshot={resources} history={history.cpu} />
                  <MemoryCard snapshot={resources} history={history.memory} />
                  <SwapCard snapshot={resources} />
                  <DiskCard snapshot={resources} />
                  <NetworkCard snapshot={resources} rxHistory={history.rx} txHistory={history.tx} />
                </div>
              </>
            )}
          </div>

          {/* ── side column ─────────────────────────────────────────────── */}
          <div className="dash__side">
            <div className="inverse hero">
              <div className="hero__k">This server</div>
              <div className="hero__name mono truncate" title={probe?.hostname ?? ""}>
                {probe?.hostname ?? "—"}
              </div>
              <div className="hero__v">{uptimeSeconds ? formatDuration(uptimeSeconds) : "—"}</div>
              <div className="hero__s">VPN uptime · {probe?.version ?? "version unknown"}</div>
              <div className="hero__actions">
                <button className="btn btn--sm" onClick={() => setCreatingHub(true)}>
                  <IconPlus size={14} /> New hub
                </button>
                <Link to="/server-settings" className="btn btn--sm btn--ghost">
                  <IconSettings size={14} /> Server settings
                </Link>
              </div>
            </div>

            <div className="card panelcard">
              <div className="panelcard__h">
                <div>
                  <div className="panelcard__t">Access protocols</div>
                  <div className="panelcard__s">ways into this server</div>
                </div>
              </div>
              <div className="feed">
                <ProtocolRow label="SoftEther" on detail={`${(listeners ?? []).filter((l) => l.Enables_bool).map((l) => l.Ports_u32).join(", ") || "no listeners"}`} />
                <ProtocolRow label="L2TP/IPsec" on={Boolean(ipsec?.L2TP_IPsec_bool)} detail={ipsec ? (ipsec.L2TP_IPsec_bool ? `default hub ${ipsec.L2TP_DefaultHub_str || "—"}` : "off") : "—"} />
                <ProtocolRow label="OpenVPN" on={Boolean(openvpn?.EnableOpenVPN_bool)} detail={openvpn?.EnableOpenVPN_bool ? `udp ${openvpn.OpenVPNPortList_str}` : "off"} />
                <ProtocolRow label="SSTP" on={Boolean(openvpn?.EnableSSTP_bool)} detail={openvpn?.EnableSSTP_bool ? "enabled" : "off"} />
                <ProtocolRow label="VPN Azure" on={Boolean(azure?.IsEnabled_bool)} detail={azure?.IsEnabled_bool ? (ddns?.CurrentHostName_str ? `${ddns.CurrentHostName_str}.vpnazure.net` : "enabled") : "off"} />
                <ProtocolRow label="DDNS" on={Boolean(ddns?.CurrentHostName_str)} detail={ddns?.CurrentFqdn_str || "—"} />
              </div>
            </div>

            <div className="card panelcard">
              <div className="panelcard__h">
                <div>
                  <div className="panelcard__t">Tools</div>
                </div>
              </div>
              <div className="feed">
                <ToolRow to="/connections" icon={<IconPulse size={16} />} title="Connections" sub="TCP connections into the server, live" />
                <ToolRow to="/logs" icon={<IconLogs size={16} />} title="Logs" sub="server, hub security and packet logs" />
                <ToolRow to="/console" icon={<IconTerminal size={16} />} title="API console" sub="every RPC method, callable raw" />
                <ToolRow to="/server-settings" icon={<IconSettings size={16} />} title="Server settings" sub="listeners, certificate, bridges…" />
              </div>
            </div>
          </div>
        </div>
      )}

      {creatingHub && (
        <CreateHubSheet
          onClose={() => setCreatingHub(false)}
          onCreated={() => {
            setCreatingHub(false);
            void loadOverview();
            void refresh();
          }}
        />
      )}
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div className="kpi">
      <div className="kpi__l">{label}</div>
      <div className="kpi__v">{value}</div>
      {sub && <div className="kpi__s">{sub}</div>}
    </div>
  );
}

function ProtocolRow({ label, on, detail }: { label: string; on: boolean; detail: string }) {
  return (
    <div className="feed__i" style={{ cursor: "default" }}>
      <span
        className="lamp__dot"
        style={{ marginInline: 6 }}
        data-on={on || undefined}
        aria-hidden="true"
      />
      <span className="feed__m">
        <span className="feed__t" style={{ fontSize: "var(--t-small)" }}>{label}</span>
        <span className="feed__s mono" title={detail}>{detail}</span>
      </span>
    </div>
  );
}

function ToolRow({ to, icon, title, sub }: { to: string; icon: React.ReactNode; title: string; sub: string }) {
  return (
    <Link to={to} className="feed__i">
      <span className="feed__ava" style={{ color: "var(--accent)" }}>{icon}</span>
      <span className="feed__m">
        <span className="feed__t" style={{ fontSize: "var(--t-small)" }}>{title}</span>
        <span className="feed__s">{sub}</span>
      </span>
      <IconChevron size={15} style={{ color: "var(--text-faint)" }} />
    </Link>
  );
}

function CreateHubSheet({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [online, setOnline] = useState(true);
  const [busy, setBusy] = useState(false);
  const { guard } = useToast();

  const create = async () => {
    setBusy(true);
    const body: Wire = { HubName_str: name.trim(), Online_bool: online, HubType_u32: 0 };
    if (password) body.AdminPasswordPlainText_str = password;
    const ok = await guard(() => api.createHub(body), `Hub ${name.trim()} created.`);
    setBusy(false);
    if (ok) onCreated();
  };

  return (
    <Sheet
      title="New Virtual Hub"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={create} disabled={busy || !name.trim()}>
            {busy && <span className="spin" />} Create hub
          </button>
        </>
      }
    >
      <Field label="Name" hint="Letters, digits, - and _ are safe.">
        <input className="input mono" value={name} onChange={(e) => setName(e.target.value)} autoFocus autoCapitalize="none" spellCheck={false} />
      </Field>
      <Field label="Hub admin password" hint="Optional — lets someone administer only this hub.">
        <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off" />
      </Field>
      <label className="checkrow">
        <input type="checkbox" checked={online} onChange={(e) => setOnline(e.target.checked)} />
        <span><span className="t">Bring it online now</span></span>
      </label>
    </Sheet>
  );
}
