"use client";

import { useCallback, useEffect, useState } from "react";
import { ConfirmSheet, Empty, KV, LoadingBlock, SearchBox, SectionTitle, usePoll } from "../../components/bits";
import { api, type Wire } from "../../lib/api";
import { navigate, seg } from "../../lib/router";
import { SESSION_TYPE } from "../../lib/se";
import { useToast } from "../../lib/toast";
import { formatBytes, formatCount, formatDate, timeAgo } from "../../lib/util";
import { IconClose } from "../../ui/Icon";
import { Sheet } from "../../ui/Sheet";
import { Pill } from "../../ui/Status";

/**
 * Live sessions: who is on the wire right now. Polled every few seconds --
 * this is the screen an operator keeps open during an incident, and the one
 * whose kill button has to work the moment it is pressed.
 */
export function HubSessions({ hub }: { hub: string }) {
  const [sessions, setSessions] = useState<Wire[] | null>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [killing, setKilling] = useState<Wire | null>(null);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.sessions(hub).catch(() => null);
    if (r) setSessions((r.SessionList as Wire[]) ?? []);
  }, [hub]);
  usePoll(load, "live", [hub]);

  const q = query.trim().toLowerCase();
  const filtered = sessions?.filter(
    (s) =>
      !q ||
      [s.Name_str, s.Username_str, s.Hostname_str, s.ClientIP_ip]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q)),
  );

  return (
    <>
      <SectionTitle
        count={sessions?.length}
        actions={<SearchBox value={query} onChange={setQuery} placeholder="user, host, IP…" />}
      >
        Sessions
      </SectionTitle>

      {filtered == null ? (
        <LoadingBlock label="loading sessions" />
      ) : filtered.length === 0 ? (
        <Empty title={q ? "nothing matches" : "nobody is connected"}>
          {q ? "No session matches that search." : "Sessions appear here live as clients connect."}
        </Empty>
      ) : (
        <SessionsTable
          sessions={filtered}
          userLink={(u) => navigate(`/hub/${seg(hub)}/user/${seg(u)}`)}
          onOpen={(s) => setOpen(String(s.Name_str))}
          onKill={(s) => setKilling(s)}
        />
      )}

      {open && <SessionSheet hub={hub} name={open} onClose={() => setOpen(null)} />}
      {killing && (
        <ConfirmSheet
          title="Disconnect this session?"
          verb="Disconnect"
          body={
            <>
              <span className="mono">{String(killing.Name_str)}</span>
              {killing.Username_str ? <> — user <b>{String(killing.Username_str)}</b></> : null} is cut
              immediately. The client may simply reconnect unless the user is disabled first.
            </>
          }
          onClose={() => setKilling(null)}
          onConfirm={async () => {
            await api.killSession(hub, String(killing.Name_str));
            await load();
          }}
        />
      )}
    </>
  );
}

/** Shared by the hub page and the user page. */
export function SessionsTable({
  sessions,
  onOpen,
  onKill,
  userLink,
}: {
  sessions: Wire[];
  onOpen: (s: Wire) => void;
  onKill: (s: Wire) => void | Promise<unknown>;
  userLink?: (username: string) => void;
}) {
  return (
    <>
      {/* desktop table */}
      <div className="only-desktop-b">
        <div className="card tcard">
          <div className="tscroll">
            <table className="dtable">
              <thead>
                <tr>
                  <th>Session</th>
                  <th>User</th>
                  <th>Kind</th>
                  <th>Source</th>
                  <th style={{ width: 80 }}>TCP</th>
                  <th>Packets</th>
                  <th>Transfer</th>
                  <th>Started</th>
                  <th className="tact" style={{ width: 120 }} aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => {
                  const kind = SESSION_TYPE(s);
                  const system = kind !== "client";
                  return (
                    <tr key={String(s.Name_str)} className="clickable" onClick={() => onOpen(s)}>
                      <td className="tmono">{String(s.Name_str)}</td>
                      <td>
                        {s.Username_str && userLink && !system ? (
                          <button
                            className="linkish mono"
                            onClick={(e) => {
                              e.stopPropagation();
                              userLink(String(s.Username_str));
                            }}
                          >
                            {String(s.Username_str)}
                          </button>
                        ) : (
                          <span className="tmono">{String(s.Username_str || "—")}</span>
                        )}
                      </td>
                      <td>
                        <Pill kind={system ? "idle" : "ok"} label={kind} />
                      </td>
                      <td className="tmono">
                        {String(s.Hostname_str || s.ClientIP_ip || "—")}
                        {s.ClientIP_ip && s.Hostname_str ? <span className="tsub">{String(s.ClientIP_ip)}</span> : null}
                      </td>
                      <td className="tmono">{s.CurrentNumTcp_u32 != null ? `${s.CurrentNumTcp_u32}/${s.MaxNumTcp_u32}` : "—"}</td>
                      <td className="tmono">{formatCount(Number(s.PacketNum_u64))}</td>
                      <td className="tmono">{formatBytes(Number(s.PacketSize_u64))}</td>
                      <td className="tmono">{timeAgo(s.CreatedTime_dt as string)}</td>
                      <td className="tact">
                        {!system && (
                          <button
                            className="btn btn--sm btn--danger"
                            onClick={(e) => {
                              e.stopPropagation();
                              void onKill(s);
                            }}
                          >
                            <IconClose size={13} /> Kill
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* mobile cards */}
      <div className="rows only-mobile-b">
        {sessions.map((s) => {
          const kind = SESSION_TYPE(s);
          const system = kind !== "client";
          return (
            <button key={String(s.Name_str)} className="row" onClick={() => onOpen(s)}>
              <div className="row__main">
                <div className="row__name">
                  <span className="mono truncate">{String(s.Username_str || s.Name_str)}</span>
                  <Pill kind={system ? "idle" : "ok"} label={kind} />
                </div>
                <div className="spec">
                  {s.ClientIP_ip ? <span className="chip"><i>from</i>{String(s.ClientIP_ip)}</span> : null}
                  <span className="chip"><i>data</i>{formatBytes(Number(s.PacketSize_u64))}</span>
                  <span className="chip"><i>since</i>{timeAgo(s.CreatedTime_dt as string)}</span>
                </div>
              </div>
              <div className="row__side">
                {!system && (
                  <button
                    className="btn btn--sm btn--danger"
                    onClick={(e) => {
                      e.stopPropagation();
                      void onKill(s);
                    }}
                  >
                    Kill
                  </button>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </>
  );
}

/** Everything the server knows about one session, in a sheet. */
export function SessionSheet({ hub,
  name,
  onClose,
}: {
    hub: string;
  name: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<Wire | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .sessionStatus(hub, name)
      .then((d) => alive && setDetail(d))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [hub, name]);

  return (
    <Sheet title="Session" subtitle={name} onClose={onClose} wide>
      {error && <div className="alert alert--err">{error}</div>}
      {!detail && !error && <LoadingBlock />}
      {detail && (
        <div style={{ display: "grid", gap: "var(--s4)" }}>
          <KV
            rows={[
              ["user", String(detail.Username_str || "—")],
              ["client IP", String(detail.Client_Ip_Address_ip || "—")],
              ["client host", String(detail.SessionStatus_ClientHostName_str || detail.ClientHostname_str || "—")],
              ["protocol", String(detail.UnderlayProtocol_str || "—")],
              ["cipher", String(detail.CipherName_str || "plain")],
              ["UDP accel", detail.IsUsingUdpAcceleration_bool ? "in use" : detail.IsUdpAccelerationEnabled_bool ? "enabled" : "off"],
              ["TCP connections", `${detail.NumTcpConnections_u32 ?? 0} / ${detail.MaxTcpConnections_u32 ?? 0}`],
              ["half connection", detail.HalfConnection_bool ? "yes" : "no"],
              ["compress", detail.UseCompress_bool ? "yes" : "no"],
              ["started", formatDate(detail.StartTime_dt as string)],
              ["client", `${detail.ClientProductName_str || "—"} ${detail.ClientProductVer_u32 ?? ""}`],
              ["client OS", String(detail.ClientOsName_str || "—")],
              ["sent", formatBytes(Number(detail.TotalSendSizeReal_u64 ?? detail.TotalSendSize_u64))],
              ["received", formatBytes(Number(detail.TotalRecvSizeReal_u64 ?? detail.TotalRecvSize_u64))],
            ]}
          />
        </div>
      )}
    </Sheet>
  );
}
