"use client";

import { useCallback, useMemo, useState } from "react";
import {
  Empty,
  LoadingBlock,
  SearchBox,
  SectionTitle,
  SortTh,
  sortRows,
  usePoll,
  useSort,
} from "../../components/bits";
import { QuotaCell, quotaKey, quotaSortValue, useQuotaIndex } from "../../components/QuotaCard";
import { UserSheet } from "../../components/UserSheet";
import { VpnFileSheet } from "../../components/VpnFileSheet";
import { api, type Quota, type Wire } from "../../lib/api";
import { navigate, seg } from "../../lib/router";
import { AUTH_TYPES, isNever, userBytes, userSortValue } from "../../lib/se";
import { formatBytes, formatCount, formatDate, timeAgo } from "../../lib/util";
import { IconChevron, IconDownload, IconPlus, IconUsers } from "../../ui/Icon";
import { Pill } from "../../ui/Status";

/**
 * The hub's users: the screen this panel exists for.
 *
 * The table answers the operator's questions in column order -- who is this,
 * can they connect, how do they authenticate, when were they last here, how
 * much have they moved. Everything about ONE user (policy, usage chart, live
 * sessions) is the user page, one tap deeper.
 */
export function HubUsers({ hub }: { hub: string }) {
  const [users, setUsers] = useState<Wire[] | null>(null);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [vpnFor, setVpnFor] = useState<string | null>(null);
  // Case-folded, because SoftEther matches usernames without case.
  const [online, setOnline] = useState<Set<string>>(new Set());
  const quotas = useQuotaIndex([hub]);
  const { sort, toggle } = useSort();
  const quotaOf = (u: Wire) => quotas.get(quotaKey("user", hub, String(u.Name_str)));

  const load = useCallback(async () => {
    const result = await api.users(hub).catch(() => null);
    if (result) setUsers((result.UserList as Wire[]) ?? []);
    const live = await api.hubUsersOnline(hub).catch(() => null);
    if (live) setOnline(new Set(live.usernames ?? []));
  }, [hub]);
  usePoll(load, "list", [hub]);

  const filtered = useMemo(() => {
    if (!users) return null;
    const q = query.trim().toLowerCase();
    const matched = !q
      ? users
      : users.filter((u) =>
          [u.Name_str, u.Realname_utf, u.GroupName_str, u.Note_utf]
            .filter(Boolean)
            .some((v) => String(v).toLowerCase().includes(q)),
        );
    // Liveness comes from a separate call, so the status column sorts by the
    // same set the pill renders from rather than the stale Online_bool.
    return sortRows(matched, sort, (u, key) =>
      key === "quota"
        ? quotaSortValue(quotas.get(quotaKey("user", hub, String(u.Name_str))))
        : userSortValue(u, key, online.has(String(u.Name_str).toLowerCase())),
    );
  }, [users, query, sort, online, quotas, hub]);

  const open = (name: string) => navigate(`/hub/${seg(hub)}/user/${seg(name)}`);

  return (
    <>
      <SectionTitle
        count={users?.length}
        actions={
          <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap" }}>
            <SearchBox value={query} onChange={setQuery} placeholder="name, group, note…" />
            <button className="btn btn--primary btn--sm" onClick={() => setCreating(true)}>
              <IconPlus size={14} /> New user
            </button>
          </div>
        }
      >
        Users
      </SectionTitle>

      {filtered === null ? (
        <LoadingBlock label="loading users" />
      ) : filtered.length === 0 ? (
        <Empty
          title={query ? "nothing matches" : "no users yet"}
          action={
            !query && (
              <button className="btn btn--primary" onClick={() => setCreating(true)}>
                <IconPlus size={15} /> Create the first user
              </button>
            )
          }
        >
          {query
            ? "No user, group or note matches that search."
            : "A user is one VPN identity: a name, a way to authenticate, and the policy that binds it."}
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
                    {filtered.map((u) => (
                      <UserRow
                        key={String(u.Name_str)}
                        user={u}
                        quota={quotaOf(u)}
                        online={online.has(String(u.Name_str).toLowerCase())}
                        onOpen={() => open(String(u.Name_str))}
                        onVpnFile={() => setVpnFor(String(u.Name_str))}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          {/* mobile cards */}
          <div className="rows only-mobile-b">
            {filtered.map((u) => {
              const bytes = userBytes(u);
              const quota = quotaOf(u);
              return (
                <button key={String(u.Name_str)} className="row" onClick={() => open(String(u.Name_str))}>
                  <div className="row__main">
                    <div className="row__name">
                      <span className="mono">{String(u.Name_str)}</span>
                      <UserStatePill user={u} online={online.has(String(u.Name_str).toLowerCase())} />
                    </div>
                    <div className="spec">
                      <span className="chip"><i>group</i>{String(u.GroupName_str || "—")}</span>
                      <span className="chip"><i>auth</i>{AUTH_TYPES[Number(u.AuthType_u32)] ?? "?"}</span>
                      <span className="chip"><i>data</i>{formatBytes(bytes.send + bytes.recv)}</span>
                      <span className="chip"><i>seen</i>{timeAgo(u.LastLoginTime_dt as string)}</span>
                      {quota && (
                        <span className={`chip${quota.blocked ? "" : " chip--brand"}`}>
                          <i>limit</i>
                          {formatBytes(quota.used_bytes)} / {formatBytes(quota.limit_bytes)}
                        </span>
                      )}
                    </div>
                    {u.Note_utf ? <div className="row__note truncate">{String(u.Note_utf)}</div> : null}
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

      {vpnFor && <VpnFileSheet hub={hub} name={vpnFor} onClose={() => setVpnFor(null)} />}
      {creating && (
        <UserSheet
         
          hub={hub}
          onClose={() => setCreating(false)}
          onSaved={(name) => {
            setCreating(false);
            void load();
            open(name);
          }}
        />
      )}
    </>
  );
}

export function UserStatePill({ user, online }: { user: Wire; online?: boolean }) {
  // Denied and expired win: they are why the account cannot be used, which
  // matters more than whether a session is still up at this instant.
  if (user.DenyAccess_bool) return <Pill kind="err" label="denied" />;
  const expires = user.Expires_dt ?? user.ExpireTime_dt;
  if (!isNever(expires as string) && new Date(expires as string).getTime() < Date.now())
    return <Pill kind="warn" label="expired" />;
  const connected = online ?? Boolean(user.Online_bool);
  if (connected) return <Pill kind="ok" label="online" />;
  return <Pill kind="idle" label="offline" />;
}

function UserRow({ user: u, quota, online, onOpen, onVpnFile }: { user: Wire; quota?: Quota; online: boolean; onOpen: () => void; onVpnFile: () => void }) {
  const bytes = userBytes(u);
  const expires = u.Expires_dt ?? u.ExpireTime_dt;
  return (
    <tr className="clickable" onClick={onOpen}>
      <td>
        <span className="tname mono">{String(u.Name_str)}</span>
        {u.Realname_utf ? <span className="tsub">{String(u.Realname_utf)}</span> : null}
      </td>
      <td>
        <UserStatePill user={u} online={online} />
      </td>
      <td className="tmono">{String(u.GroupName_str || "—")}</td>
      <td>{AUTH_TYPES[Number(u.AuthType_u32)] ?? "?"}</td>
      <td className="tmono">{timeAgo(u.LastLoginTime_dt as string)}</td>
      <td className="tmono">{formatCount(Number(u.NumLogin_u32))}</td>
      <td className="tmono" title={`↑ ${formatBytes(bytes.send)} · ↓ ${formatBytes(bytes.recv)}`}>
        {formatBytes(bytes.send + bytes.recv)}
      </td>
      <td><QuotaCell quota={quota} /></td>
      <td className="tmono">{isNever(expires as string) ? "never" : formatDate(expires as string)}</td>
      <td className="tact">
        <button
          className="btn btn--sm btn--ghost"
          title="Download .vpn connection file"
          aria-label="Download .vpn connection file"
          onClick={(e) => {
            e.stopPropagation();
            onVpnFile();
          }}
        >
          <IconDownload size={14} />
        </button>
        <span className="pcard__go">
          <IconChevron size={15} />
        </span>
      </td>
    </tr>
  );
}
