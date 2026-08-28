"use client";

import { useCallback, useState } from "react";
import { LoadingBlock, PageHead, SectionTitle, usePoll } from "../components/bits";
import { RangeSeg, TrafficChart } from "../components/TrafficChart";
import { api, type Usage, type Wire } from "../lib/api";
import { HUB_TYPES } from "../lib/se";
import { Link, navigate, seg } from "../lib/router";
import { useToast } from "../lib/toast";
import { formatBytes, formatCount, formatDate } from "../lib/util";
import { OnlinePill } from "../ui/Status";
import { HubUsers } from "./hub/HubUsers";
import { HubGroups } from "./hub/HubGroups";
import { HubSessions } from "./hub/HubSessions";
import { HubAccess } from "./hub/HubAccess";
import { HubSecurity } from "./hub/HubSecurity";
import { HubSecureNat } from "./hub/HubSecureNat";
import { HubLinks } from "./hub/HubLinks";
import { HubTables } from "./hub/HubTables";
import { HubSettings } from "./hub/HubSettings";

/**
 * One Virtual Hub. The hub is where SoftEther's day-to-day lives -- users,
 * sessions, access control -- so this page is a small application of its own:
 * a header that says how the hub is doing, and a tab strip for its rooms.
 */

const TABS: { key: string; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "users", label: "Users" },
  { key: "groups", label: "Groups" },
  { key: "sessions", label: "Sessions" },
  { key: "access", label: "Access control" },
  { key: "security", label: "Security" },
  { key: "securenat", label: "SecureNAT" },
  { key: "links", label: "Cascade" },
  { key: "tables", label: "Tables" },
  { key: "settings", label: "Hub settings" },
];

export function HubDetail({ hub, tab }: { hub: string; tab: string }) {
  const [status, setStatus] = useState<Wire | null>(null);
  const { guard } = useToast();

  const load = useCallback(async () => {
    setStatus(await api.hubStatus(hub).catch(() => null));
  }, [hub]);
  usePoll(load, "detail", [hub]);

  const online = Boolean(status?.Online_bool);

  const toggleOnline = () =>
    guard(async () => {
      await api.hubOnline(hub, !online);
      await load();
    }, online ? "Hub taken offline." : "Hub brought online.");

  return (
    <div className="page">
      <PageHead
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: "var(--s3)", minWidth: 0 }}>
            <span className="truncate">{hub}</span>
            {status && <OnlinePill online={online} />}
          </span>
        }
        sub={
          <>
            <Link to={`/`} className="linkish">← dashboard</Link>
            {status && (
              <>
                {" "}· {HUB_TYPES[Number(status.HubType_u32 ?? 0)]} hub
              </>
            )}
          </>
        }
        actions={
          status && (
            <button className={`btn ${online ? "" : "btn--primary"}`} onClick={toggleOnline}>
              {online ? "Take offline" : "Bring online"}
            </button>
          )
        }
      />

      <div className="tabs" role="tablist" aria-label="Hub sections">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            className={`tabs__i${tab === t.key ? " on" : ""}`}
            onClick={() => navigate(`/hub/${seg(hub)}${t.key === "overview" ? "" : `/${t.key}`}`)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "overview" && <HubOverview hub={hub} status={status} />}
      {tab === "users" && <HubUsers hub={hub} />}
      {tab === "groups" && <HubGroups hub={hub} />}
      {tab === "sessions" && <HubSessions hub={hub} />}
      {tab === "access" && <HubAccess hub={hub} />}
      {tab === "security" && <HubSecurity hub={hub} />}
      {tab === "securenat" && <HubSecureNat hub={hub} />}
      {tab === "links" && <HubLinks hub={hub} />}
      {tab === "tables" && <HubTables hub={hub} />}
      {tab === "settings" && <HubSettings hub={hub} onChanged={load} />}
    </div>
  );
}

function HubOverview({ hub, status }: { hub: string; status: Wire | null }) {
  const [hours, setHours] = useState(24);
  const [usage, setUsage] = useState<Usage | null>(null);

  const load = useCallback(async () => {
    setUsage(await api.hubTraffic(hub, hours).catch(() => null));
  }, [hub, hours]);
  usePoll(load, "list", [hub, hours]);

  if (!status) return <LoadingBlock label="asking the hub" />;

  const recv = (Number(status["Recv.UnicastBytes_u64"]) || 0) + (Number(status["Recv.BroadcastBytes_u64"]) || 0);
  const send = (Number(status["Send.UnicastBytes_u64"]) || 0) + (Number(status["Send.BroadcastBytes_u64"]) || 0);

  return (
    <>
      <div className="fleet stagger">
        <div className="fleet__cell">
          <div className="fleet__n">{formatCount(status.NumSessions_u32)}</div>
          <div className="micro">sessions</div>
        </div>
        <div className="fleet__cell">
          <div className="fleet__n">{formatCount(status.NumUsers_u32)}</div>
          <div className="micro">users</div>
        </div>
        <div className="fleet__cell">
          <div className="fleet__n">{formatCount(status.NumGroups_u32)}</div>
          <div className="micro">groups</div>
        </div>
        <div className="fleet__cell">
          <div className="fleet__n">{formatCount(status.NumMacTables_u32)}</div>
          <div className="micro">MAC entries</div>
        </div>
        <div className="fleet__cell">
          <div className="fleet__n">{formatBytes(send + recv)}</div>
          <div className="micro">lifetime traffic</div>
        </div>
      </div>

      <SectionTitle actions={<RangeSeg hours={hours} onChange={setHours} />}>Throughput</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)" }}>
        {usage ? <TrafficChart usage={usage} /> : <LoadingBlock label="loading samples" />}
      </div>

      <SectionTitle>Details</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)" }}>
        <div className="kv">
          <div><div className="micro">created</div><div className="mono">{formatDate(status.CreatedTime_dt as string)}</div></div>
          <div><div className="micro">last comm</div><div className="mono">{formatDate(status.LastCommTime_dt as string)}</div></div>
          <div><div className="micro">last login</div><div className="mono">{formatDate(status.LastLoginTime_dt as string)}</div></div>
          <div><div className="micro">logins</div><div className="mono">{formatCount(status.NumLogin_u32 as number)}</div></div>
          <div><div className="micro">IP entries</div><div className="mono">{formatCount(status.NumIpTables_u32 as number)}</div></div>
          <div><div className="micro">SecureNAT</div><div className="mono">{status.SecureNATEnabled_bool ? "enabled" : "off"}</div></div>
        </div>
      </div>
    </>
  );
}
