"use client";

import { useCallback, useState } from "react";
import { ConfirmSheet, Empty, KV, LoadingBlock, PageHead, usePoll } from "../components/bits";
import { api, type Wire } from "../lib/api";
import { useToast } from "../lib/toast";
import { formatDate, timeAgo } from "../lib/util";
import { IconClose } from "../ui/Icon";
import { Sheet } from "../ui/Sheet";
import { Pill } from "../ui/Status";

/**
 * Raw TCP connections into the VPN server -- including half-open clients,
 * management links, and this very panel. The lowest-level live view there is.
 */

const CONNECTION_TYPES: Record<number, string> = {
  0: "client",
  1: "initializing",
  2: "login",
  3: "additional",
  4: "RPC / farm",
  5: "admin",
  6: "management",
  7: "management",
};

export function Connections() {
  const [connections, setConnections] = useState<Wire[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [killing, setKilling] = useState<Wire | null>(null);

  const load = useCallback(async () => {
    const r = await api.connections().catch(() => null);
    if (r) setConnections((r.ConnectionList as Wire[]) ?? []);
  }, []);
  usePoll(load, "live", []);

  return (
    <div className="page">
      <PageHead title="Connections" sub="live TCP connections into the VPN server" />
      {connections === null ? (
        <LoadingBlock label="loading connections" />
      ) : connections.length === 0 ? (
        <Empty title="no connections">Which would be odd, since this panel is one.</Empty>
      ) : (
        <div className="card tcard">
          <div className="tscroll">
            <table className="dtable">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Kind</th>
                  <th>Source</th>
                  <th>Connected</th>
                  <th className="tact" style={{ width: 130 }} aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {connections.map((c) => (
                  <tr key={String(c.Name_str)} className="clickable" onClick={() => setOpen(String(c.Name_str))}>
                    <td className="tmono">{String(c.Name_str)}</td>
                    <td><Pill kind="idle" label={CONNECTION_TYPES[Number(c.Type_u32)] ?? `type ${c.Type_u32}`} /></td>
                    <td className="tmono">
                      {String(c.Hostname_str || c.Ip_ip)}:{String(c.Port_u32)}
                      {c.Hostname_str ? <span className="tsub">{String(c.Ip_ip)}</span> : null}
                    </td>
                    <td className="tmono">{timeAgo(c.ConnectedTime_dt as string)}</td>
                    <td className="tact">
                      <button
                        className="btn btn--sm btn--danger"
                        onClick={(e) => {
                          e.stopPropagation();
                          setKilling(c);
                        }}
                      >
                        <IconClose size={13} /> Disconnect
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {open && <ConnectionSheet name={open} onClose={() => setOpen(null)} />}
      {killing && (
        <ConfirmSheet
          title="Disconnect this connection?"
          verb="Disconnect"
          body={
            <>
              <span className="mono">{String(killing.Name_str)}</span> from{" "}
              <span className="mono">{String(killing.Ip_ip)}</span> is cut. Cutting a management
              connection may sign out an administrator — possibly you.
            </>
          }
          onClose={() => setKilling(null)}
          onConfirm={async () => {
            await api.disconnectConnection(String(killing.Name_str));
            await load();
          }}
        />
      )}
    </div>
  );
}

function ConnectionSheet({ name, onClose }: { name: string; onClose: () => void }) {
  const [detail, setDetail] = useState<Wire | null>(null);
  const [error, setError] = useState<string | null>(null);

  usePoll(
    async () => {
      try {
        setDetail(await api.connectionInfo(name));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    3600_000,
    [name],
  );

  return (
    <Sheet title="Connection" subtitle={name} onClose={onClose}>
      {error && <div className="alert alert--err">{error}</div>}
      {!detail && !error && <LoadingBlock />}
      {detail && (
        <KV
          rows={[
            ["source", `${detail.Hostname_str || detail.Ip_ip}:${detail.Port_u32}`],
            ["connected", formatDate(detail.ConnectedTime_dt as string)],
            ["client", `${detail.ClientStr_str ?? "—"} ${detail.ClientVer_u32 ?? ""}`],
            ["server", `${detail.ServerStr_str ?? "—"} ${detail.ServerVer_u32 ?? ""}`],
          ]}
        />
      )}
    </Sheet>
  );
}
