"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ConfirmSheet,
  Empty,
  KV,
  LoadingBlock,
  MoreRows,
  PageHead,
  SectionTitle,
  usePoll,
  useReveal,
} from "../components/bits";
import { PolicyEditor, extractPolicy } from "../components/PolicyEditor";
import { QuotaCard } from "../components/QuotaCard";
import { RangeSeg, TrafficChart } from "../components/TrafficChart";
import { UserSheet } from "../components/UserSheet";
import { VpnFileSheet } from "../components/VpnFileSheet";
import { api, type Usage, type Wire } from "../lib/api";
import { Sheet } from "../ui/Sheet";
import { Spark } from "../components/ResourceCards";
import { Link, navigate, seg } from "../lib/router";
import { AUTH_TYPES, isNever, userBytes, SESSION_TYPE } from "../lib/se";
import { useToast } from "../lib/toast";
import { formatBytes, formatCount, formatDate, formatDuration, timeAgo } from "../lib/util";
import { BrandMark, IconDownload, IconTrash } from "../ui/Icon";
import { Pill } from "../ui/Status";
import { UserStatePill } from "./hub/HubUsers";
import { SessionSheet, SessionsTable } from "./hub/HubSessions";

/** Sessions with no client transport of their own -- SecureNAT and the like
 *  -- move bytes that SoftEther reports without a direction split. One line
 *  of what actually moved is the honest picture; two lines of zeroes are not. */
function CombinedSessionChart({ usage }: { usage: Wire }) {
  const points: Wire[] = Array.isArray(usage.combined) ? usage.combined : [];
  const values = points.map((p) => Number(p.total) || 0);
  if (!values.some((v) => v > 0)) {
    return (
      <p className="micro">
        Nothing moved during this session, or it was shorter than one sampling interval.
      </p>
    );
  }
  return (
    <div style={{ display: "grid", gap: "var(--s2)" }}>
      <div className="chart__legend">
        <span className="chart__key">
          <i style={{ background: "var(--chart-recv)" }} /> Traffic
          <b className="mono">{formatBytes(Number(usage.total_combined) || 0)}</b>
        </span>
      </div>
      <Spark values={values} color="var(--chart-recv)" />
      <p className="micro">
        This session reports no split between download and upload, so this is the total it moved
        over its life, sampled every interval.
      </p>
    </div>
  );
}

/**
 * One recorded session, charted. The series behind it is the sampler's
 * per-session snapshots, so the resolution is the sampling interval -- a
 * session shorter than one tick has totals but no curve to draw, which the
 * chart says rather than drawing a flat line that means nothing.
 */
function SessionUsageSheet({ hub, row, onClose }: { hub: string; row: Wire; onClose: () => void }) {
  const [usage, setUsage] = useState<(Usage & { session: Wire } & Wire) | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void api
      .sessionUsage(hub, Number(row.id))
      .then(setUsage)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [hub, row.id]);

  const started = new Date(String(row.started_date)).getTime();
  const endedAt = row.ended_date ? new Date(String(row.ended_date)).getTime() : null;
  const duration = endedAt ? (endedAt - started) / 1000 : (Date.now() - started) / 1000;

  return (
    <Sheet
      title="Session"
      subtitle={`${row.username} · ${formatDate(String(row.started_date))}`}
      onClose={onClose}
      wide
      footer={<button className="btn" onClick={onClose}>Close</button>}
    >
      <div style={{ display: "grid", gap: "var(--s3)" }}>
        <KV
          rows={[
            ["client IP", String(row.client_ip || "—")],
            ["client host", String(row.client_hostname || "—")],
            ["started", formatDate(String(row.started_date))],
            ["ended", row.ended_date ? formatDate(String(row.ended_date)) : "still connected"],
            ["duration", formatDuration(duration)],
            ["downloaded", formatBytes(Number(row.download_bytes))],
            ["uploaded", formatBytes(Number(row.upload_bytes))],
          ]}
        />
        {error && <div className="alert alert--err">{error}</div>}
        {!usage && !error ? (
          <LoadingBlock label="loading this session's traffic" />
        ) : usage ? (
          usage.split_available ? (
            <TrafficChart
              usage={usage}
              emptyLabel="This session was shorter than one sampling interval, so there is no curve to draw — the totals above are what it moved."
            />
          ) : (
            <CombinedSessionChart usage={usage} />
          )
        ) : null}
      </div>
    </Sheet>
  );
}

/**
 * One user, whole: identity, policy, usage, live sessions.
 *
 * The identity block renders as a card -- the thing you'd hand the user if
 * VPN accounts were physical. Everything on it is real data; nothing is
 * decoration.
 */
export function UserDetail({ hub, name }: { hub: string; name: string }) {
  const [user, setUser] = useState<Wire | null>(null);
  const [sessions, setSessions] = useState<Wire[] | null>(null);
  const [history, setHistory] = useState<Wire[] | null>(null);
  const [historyExhausted, setHistoryExhausted] = useState(false);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [hours, setHours] = useState(24);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [policy, setPolicy] = useState<Wire | null>(null);
  const [policyDirty, setPolicyDirty] = useState(false);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [openSession, setOpenSession] = useState<string | null>(null);
  const [openHistory, setOpenHistory] = useState<Wire | null>(null);
  const { guard, push } = useToast();

  const load = useCallback(async () => {
    const u = await api.user(hub, name).catch(() => null);
    if (u) {
      setUser(u);
      setPolicy((current) => (policyDirty ? current : extractPolicy(u)));
    }
    const s = await api.userSessions(hub, name).catch(() => null);
    if (s) setSessions((s.SessionList as Wire[]) ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hub, name, policyDirty]);
  usePoll(load, "detail", [hub, name]);

  const loadUsage = useCallback(async () => {
    setUsage(await api.userUsage(hub, name, hours).catch(() => null));
  }, [hub, name, hours]);
  usePoll(loadUsage, "list", [hub, name, hours]);

  // A busy account accumulates hundreds of logins; the newest few answer the
  // usual question, and the rest are one button away.
  const { visible: historyVisible, reveal: revealHistory, reset: resetHistory } = useReveal(5);

  const loadHistory = useCallback(async (beforeId = 0) => {
    const batch = await api.userSessionHistory(hub, name, beforeId).catch(() => []);
    setHistory((current) => (beforeId ? [...(current ?? []), ...batch] : batch));
    if (batch.length < 100) setHistoryExhausted(true);
  }, [hub, name]);
  useEffect(() => {
    setHistoryExhausted(false);
    resetHistory();
    void loadHistory();
  }, [loadHistory, resetHistory]);
  const shownHistory = history ? history.slice(0, historyVisible) : [];

  const savePolicy = async () => {
    if (!user || !policy) return;
    setSavingPolicy(true);
    const body: Wire = {};
    for (const [k, v] of Object.entries(user)) {
      if (k.startsWith("Recv.") || k.startsWith("Send.") || k === "Auth_Password_str") continue;
      body[k] = v;
    }
    Object.assign(body, policy);
    const ok = await guard(() => api.setUser(hub, name, body), "Policy saved.");
    setSavingPolicy(false);
    if (ok) {
      setPolicyDirty(false);
      void load();
    }
  };

  if (!user) {
    return (
      <div className="page">
        <LoadingBlock label={`loading ${name}`} />
      </div>
    );
  }

  const bytes = userBytes(user);

  return (
    <div className="page">
      <PageHead
        title={<span className="mono">{name}</span>}
        sub={
          <>
            <Link to={`/hub/${seg(hub)}/users`} className="linkish">← users</Link>
            {" "}· hub <span className="mono">{hub}</span>
          </>
        }
        actions={
          <>
            <button className="btn btn--primary" onClick={() => setDownloading(true)}>
              <IconDownload size={15} /> Download .vpn
            </button>
            <button className="btn" onClick={() => setEditing(true)}>Edit profile</button>
            <button className="btn btn--danger" onClick={() => setDeleting(true)}>
              <IconTrash size={14} /> Delete
            </button>
          </>
        }
      />

      <div className="usergrid">
        {/* the identity card */}
        <div className="idcard">
          <div className="idcard__head">
            <BrandMark size={28} />
            <span className="idcard__hub mono">{hub}</span>
            <UserStatePill user={user} online={(sessions?.length ?? 0) > 0} />
          </div>
          <div className="idcard__name mono">{name}</div>
          {user.Realname_utf ? <div className="idcard__real">{String(user.Realname_utf)}</div> : null}
          <div className="idcard__meta">
            <span className="chip"><i>auth</i>{AUTH_TYPES[Number(user.AuthType_u32)] ?? "?"}</span>
            <span className="chip"><i>group</i>{String(user.GroupName_str || "—")}</span>
            <span className="chip"><i>expires</i>{isNever(user.ExpireTime_dt as string) ? "never" : formatDate(user.ExpireTime_dt as string)}</span>
          </div>
          <div className="idcard__foot">
            <div className="stat"><span className="stat__n">{formatCount(Number(user.NumLogin_u32))}</span><span className="micro">logins</span></div>
            <div className="stat"><span className="stat__n">{formatBytes(bytes.recv)}</span><span className="micro">downloaded</span></div>
            <div className="stat"><span className="stat__n">{formatBytes(bytes.send)}</span><span className="micro">uploaded</span></div>
            <div className="stat"><span className="stat__n">{sessions?.length ?? 0}</span><span className="micro">online now</span></div>
          </div>
        </div>

        <div className="card" style={{ padding: "var(--s4)" }}>
          <KV
            rows={[
              ["created", formatDate(user.CreatedTime_dt as string)],
              ["updated", formatDate(user.UpdatedTime_dt as string)],
              ["note", String(user.Note_utf || "—")],
              ...(Number(user.AuthType_u32) === 3 && user.CommonName_utf ? [["required CN", String(user.CommonName_utf)] as [string, string]] : []),
              ...(Number(user.AuthType_u32) === 4 ? [["RADIUS user", String(user.RadiusUsername_utf || name)] as [string, string]] : []),
              ...(Number(user.AuthType_u32) === 5 ? [["NT user", String(user.NtUsername_utf || name)] as [string, string]] : []),
            ]}
          />
        </div>
      </div>

      <SectionTitle actions={<RangeSeg hours={hours} onChange={setHours} />}>Usage</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)" }}>
        {usage ? (
          <TrafficChart
            usage={usage}
            emptyLabel="No samples in this window — usage builds up as the panel's sampler runs."
          />
        ) : (
          <LoadingBlock label="loading usage" />
        )}
      </div>

      <SectionTitle count={sessions?.length}>Live sessions</SectionTitle>
      {sessions === null ? (
        <LoadingBlock />
      ) : sessions.length === 0 ? (
        <Empty title="not connected right now">
          Sessions appear here the moment this user connects, and can be cut from here too.
        </Empty>
      ) : (
        <SessionsTable
          sessions={sessions}
          onOpen={(s) => setOpenSession(String(s.Name_str))}
          onKill={(s) =>
            guard(async () => {
              await api.killSession(hub, String(s.Name_str));
              await load();
            }, "Session disconnected.")
          }
        />
      )}

      <SectionTitle count={history?.length}>Session history</SectionTitle>
      {history === null ? (
        <LoadingBlock />
      ) : history.length === 0 ? (
        <Empty title="no logins recorded yet">
          Every connection this user makes lands here — client IP, when it started and ended,
          and the bytes it moved — kept for the retention window set in Settings.
        </Empty>
      ) : (
        <div className="card tcard">
          <div className="tscroll">
            <table className="dtable">
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Ended</th>
                  <th>Duration</th>
                  <th>Client IP</th>
                  <th>Host</th>
                  <th>Transferred</th>
                </tr>
              </thead>
              <tbody>
                {shownHistory.map((h) => {
                  const started = new Date(String(h.started_date)).getTime();
                  const endedAt = h.ended_date ? new Date(String(h.ended_date)).getTime() : null;
                  const duration = endedAt ? (endedAt - started) / 1000 : (Date.now() - started) / 1000;
                  return (
                    <tr key={String(h.id)} className="clickable" onClick={() => setOpenHistory(h)}>
                      <td className="tmono">{formatDate(String(h.started_date))}</td>
                      <td className="tmono">{h.ended_date ? formatDate(String(h.ended_date)) : <Pill kind="ok" label="connected" />}</td>
                      <td className="tmono">{formatDuration(duration)}</td>
                      <td className="tmono">{String(h.client_ip || "—")}</td>
                      <td className="tmono truncate" style={{ maxWidth: 160 }}>{String(h.client_hostname || "—")}</td>
                      <td className="tmono" title={`↓ ${formatBytes(Number(h.download_bytes))} · ↑ ${formatBytes(Number(h.upload_bytes))}`}>
                        {formatBytes(Number(h.bytes_total))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <MoreRows
            shown={shownHistory.length}
            loaded={history.length}
            exhausted={historyExhausted}
            noun="logins"
            onReveal={revealHistory}
            onLoad={() => loadHistory(Number(history[history.length - 1].id))}
          />
        </div>
      )}

      <SectionTitle>Traffic limit</SectionTitle>
      <QuotaCard subject="user" hub={hub} name={name} onChanged={load} />

      <SectionTitle>Security policy</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 720 }}>
        {policy && (
          <>
            <PolicyEditor
              value={policy}
              subject="user"
              onChange={(next) => {
                setPolicy(next);
                setPolicyDirty(true);
              }}
            />
            {policyDirty && (
              <div style={{ display: "flex", gap: "var(--s2)", marginTop: "var(--s3)" }}>
                <button className="btn btn--primary" onClick={savePolicy} disabled={savingPolicy}>
                  {savingPolicy && <span className="spin" />} Save policy
                </button>
                <button
                  className="btn"
                  onClick={() => {
                    setPolicy(extractPolicy(user));
                    setPolicyDirty(false);
                  }}
                >
                  Discard
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {downloading && (
        <VpnFileSheet hub={hub} name={name} onClose={() => setDownloading(false)} />
      )}
      {editing && (
        <UserSheet
         
          hub={hub}
          existing={user}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            void load();
          }}
        />
      )}
      {deleting && (
        <ConfirmSheet
          title={`Delete ${name}?`}
          verb="Delete user"
          typed={name}
          body={
            <>
              The user is removed from hub <b>{hub}</b> and any live session it has is cut. This
              cannot be undone.
            </>
          }
          onClose={() => setDeleting(false)}
          onConfirm={async () => {
            await api.deleteUser(hub, name);
            push("ok", `${name} deleted.`);
            navigate(`/hub/${seg(hub)}/users`);
          }}
        />
      )}
      {openHistory && (
        <SessionUsageSheet hub={hub} row={openHistory} onClose={() => setOpenHistory(null)} />
      )}
      {openSession && (
        <SessionSheet
         
          hub={hub}
          name={openSession}
          onClose={() => setOpenSession(null)}
        />
      )}
    </div>
  );
}
