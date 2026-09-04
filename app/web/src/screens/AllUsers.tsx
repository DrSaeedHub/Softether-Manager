"use client";

import { useCallback, useMemo, useState } from "react";
import {
  Empty,
  LoadingBlock,
  PageHead,
  SearchBox,
  SortTh,
  sortRows,
  usePoll,
  useSort,
} from "../components/bits";
import {
  QuotaCell,
  meteredBytes,
  netBytes,
  quotaKey,
  quotaSortValue,
  useQuotaIndex,
} from "../components/QuotaCard";
import { UserSheet } from "../components/UserSheet";
import { VpnFileSheet } from "../components/VpnFileSheet";
import { api, type Wire } from "../lib/api";
import { navigate, seg } from "../lib/router";
import { AUTH_TYPES, isNever, userBytes, userSortValue } from "../lib/se";
import { formatBytes, formatCount, formatDate, timeAgo } from "../lib/util";
import { IconChevron, IconDownload, IconPlus } from "../ui/Icon";
import { UserStatePill } from "./hub/HubUsers";

/**
 * Every user on the server, across all Virtual Hubs -- the one table an
 * operator scans when the question is "who exists here", not "what is in
 * this hub". Rows link into the user's own page; creating asks which hub
 * the user should live in.
 */
export function AllUsers() {
  const [data, setData] = useState<{ hubs: string[]; users: Wire[]; errors: { hub: string; error: string }[] } | null>(null);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [createHub, setCreateHub] = useState("");
  const [vpnFor, setVpnFor] = useState<{ hub: string; name: string } | null>(null);
  const quotas = useQuotaIndex([]);
  const { sort, toggle } = useSort();
  const quotaOf = (u: Wire) =>
    quotas.get(quotaKey("user", String(u.HubName_str), String(u.Name_str)));
  // Transfer is the lifetime counter less whatever a reset put behind us, so
  // the column and the meter next to it are the same number.
  const movedBy = (u: Wire) => netBytes(userBytes(u), quotaOf(u));

  const load = useCallback(async () => {
    setData(await api.allUsers().catch(() => null));
  }, []);
  usePoll(load, "list", []);

  const filtered = useMemo(() => {
    if (!data) return null;
    const q = query.trim().toLowerCase();
    const matched = !q
      ? data.users
      : data.users.filter((u) =>
          [u.Name_str, u.Realname_utf, u.GroupName_str, u.Note_utf, u.HubName_str]
            .filter(Boolean)
            .some((v) => String(v).toLowerCase().includes(q)),
        );
    // Sorting is applied after the poll refreshes the list, so a chosen order
    // survives every refresh rather than snapping back to the server's.
    return sortRows(matched, sort, (u, key) => {
      if (key === "quota") return quotaSortValue(quotaOf(u), movedBy(u));
      // Transfer sorts by what the panel shows, which a reset changes.
      if (key === "transfer") {
        const moved = movedBy(u);
        return moved.send + moved.recv;
      }
      return userSortValue(u, key);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, query, sort, quotas]);

  const open = (u: Wire) => navigate(`/hub/${seg(String(u.HubName_str))}/user/${seg(String(u.Name_str))}`);

  return (
    <div className="page">
      <PageHead
        title="Users"
        sub={data ? `${data.users.length} across ${data.hubs.length} hub${data.hubs.length === 1 ? "" : "s"}` : "Every user on this server."}
        actions={
          <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap" }}>
            <SearchBox value={query} onChange={setQuery} placeholder="name, hub, group, note…" />
            <button
              className="btn btn--primary"
              onClick={() => {
                setCreateHub(data?.hubs[0] ?? "");
                setCreating(true);
              }}
              disabled={!data || data.hubs.length === 0}
            >
              <IconPlus size={15} /> New user
            </button>
          </div>
        }
      />

      {data?.errors.map((e) => (
        <div key={e.hub} className="alert alert--warn">
          Hub <b>{e.hub}</b> could not be listed: {e.error}
        </div>
      ))}

      {filtered === null ? (
        <LoadingBlock label="loading users" />
      ) : filtered.length === 0 ? (
        <Empty title={query ? "nothing matches" : "no users yet"}>
          {query
            ? "No user, hub, group or note matches that search."
            : "A user is one VPN identity, living in one Virtual Hub."}
        </Empty>
      ) : (
        <>
          {/* desktop table */}
          <div className="only-desktop-b">
            <div className="card tcard">
              <div className="tscroll">
                <table className="dtable">
                  <thead>
                    <tr>
                      <SortTh sortKey="user" sort={sort} onSort={toggle}>User</SortTh>
                      <SortTh sortKey="hub" sort={sort} onSort={toggle}>Hub</SortTh>
                      <SortTh sortKey="status" sort={sort} onSort={toggle} style={{ width: 110 }}>Status</SortTh>
                      <SortTh sortKey="group" sort={sort} onSort={toggle}>Group</SortTh>
                      <SortTh sortKey="auth" sort={sort} onSort={toggle}>Auth</SortTh>
                      <SortTh sortKey="login" sort={sort} onSort={toggle}>Last login</SortTh>
                      <SortTh sortKey="logins" sort={sort} onSort={toggle} style={{ width: 90 }}>Logins</SortTh>
                      <SortTh sortKey="transfer" sort={sort} onSort={toggle}>Transfer</SortTh>
                      <SortTh sortKey="quota" sort={sort} onSort={toggle}>Limit</SortTh>
                      <SortTh sortKey="expires" sort={sort} onSort={toggle}>Expires</SortTh>
                      <th className="tact" style={{ width: 96 }} aria-label="Actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((u) => {
                      const bytes = movedBy(u);
                      const expires = u.Expires_dt ?? u.ExpireTime_dt;
                      return (
                        <tr key={`${u.HubName_str}/${u.Name_str}`} className="clickable" onClick={() => open(u)}>
                          <td>
                            <span className="tname mono">{String(u.Name_str)}</span>
                            {u.Realname_utf ? <span className="tsub">{String(u.Realname_utf)}</span> : null}
                          </td>
                          <td><span className="chip chip--brand">{String(u.HubName_str)}</span></td>
                          <td><UserStatePill user={u} /></td>
                          <td className="tmono">{String(u.GroupName_str || "—")}</td>
                          <td>{AUTH_TYPES[Number(u.AuthType_u32)] ?? "?"}</td>
                          <td className="tmono">{timeAgo(u.LastLoginTime_dt as string)}</td>
                          <td className="tmono">{formatCount(Number(u.NumLogin_u32))}</td>
                          <td className="tmono">{formatBytes(bytes.send + bytes.recv)}</td>
                          <td><QuotaCell quota={quotaOf(u)} net={bytes} /></td>
                          <td className="tmono">{isNever(expires as string) ? "never" : formatDate(expires as string)}</td>
                          <td className="tact">
                            <button
                              className="btn btn--sm btn--ghost"
                              title="Download .vpn connection file"
                              aria-label="Download .vpn connection file"
                              onClick={(e) => {
                                e.stopPropagation();
                                setVpnFor({ hub: String(u.HubName_str), name: String(u.Name_str) });
                              }}
                            >
                              <IconDownload size={14} />
                            </button>
                            <span className="pcard__go"><IconChevron size={15} /></span>
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
            {filtered.map((u) => {
              const bytes = movedBy(u);
              const quota = quotaOf(u);
              return (
                <button key={`${u.HubName_str}/${u.Name_str}`} className="row" onClick={() => open(u)}>
                  <div className="row__main">
                    <div className="row__name">
                      <span className="mono">{String(u.Name_str)}</span>
                      <UserStatePill user={u} />
                    </div>
                    <div className="spec">
                      <span className="chip chip--brand">{String(u.HubName_str)}</span>
                      <span className="chip"><i>group</i>{String(u.GroupName_str || "—")}</span>
                      <span className="chip"><i>data</i>{formatBytes(bytes.send + bytes.recv)}</span>
                      <span className="chip"><i>seen</i>{timeAgo(u.LastLoginTime_dt as string)}</span>
                      {quota?.has_limit && (
                        <span className={`chip${quota.blocked ? "" : " chip--brand"}`}>
                          <i>limit</i>
                          {formatBytes(meteredBytes(bytes, quota.metric))} /{" "}
                          {formatBytes(quota.limit_bytes)}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="row__side">
                    <IconChevron size={15} />
                  </div>
                </button>
              );
            })}
          </div>
        </>
      )}

      {vpnFor && <VpnFileSheet hub={vpnFor.hub} name={vpnFor.name} onClose={() => setVpnFor(null)} />}
      {creating && createHub && (
        <UserSheet
          hub={createHub}
          hubChoices={data?.hubs}
          onHubChange={setCreateHub}
          onClose={() => setCreating(false)}
          onSaved={(name) => {
            setCreating(false);
            void load();
            navigate(`/hub/${seg(createHub)}/user/${seg(name)}`);
          }}
        />
      )}
    </div>
  );
}
