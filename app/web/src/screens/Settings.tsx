"use client";

import { useCallback, useEffect, useState } from "react";
import {
  CheckRow,
  ErrorAlert,
  Field,
  KV,
  LoadingBlock,
  MoreRows,
  PageHead,
  SectionTitle,
  useReveal,
} from "../components/bits";
import { api, type Wire } from "../lib/api";
import { Link } from "../lib/router";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { useUpdate } from "../lib/update";
import { timeAgo } from "../lib/util";
import { IconRefresh } from "../ui/Icon";
import { Pill } from "../ui/Status";

/**
 * The panel about itself: the account, where it serves, its sampler, its
 * updates, and the audit trail of what every administrator did.
 */
export function Settings({ section }: { section?: string }) {
  useEffect(() => {
    if (section) {
      requestAnimationFrame(() =>
        document.getElementById(`s-${section}`)?.scrollIntoView({ behavior: "smooth", block: "start" }),
      );
    }
  }, [section]);

  return (
    <div className="page">
      <PageHead title="Settings" sub="The panel itself — account, address, sampling, updates." />
      <div className="setdoc">
        <AccountCard />
        <ConnectionCard />
        <PanelCard />
        <MonitoringCard />
        <VpnFileTemplateCard />
        <UpdatesCard />
        <AuditCard />
        <AboutCard />
      </div>
    </div>
  );
}

function AccountCard() {
  const { user, logout } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  const change = async () => {
    setError(null);
    if (next !== confirm) {
      setError("The two passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await api.changePassword(current, next);
      push("ok", "Password changed.");
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section id="s-account">
      <SectionTitle>Account</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 560 }}>
        <KV rows={[["signed in as", user ?? "—"]]} />
        <div className="section__t" style={{ margin: "var(--s4) 0 var(--s3)" }}>Change password</div>
        {error && <ErrorAlert>{error}</ErrorAlert>}
        <Field label="Current password">
          <input className="input" type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
        </Field>
        <div className="row2">
          <Field label="New password">
            <input className="input" type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" minLength={1} />
          </Field>
          <Field label="Confirm">
            <input className="input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </Field>
        </div>
        <div style={{ display: "flex", gap: "var(--s2)" }}>
          <button className="btn btn--primary" onClick={change} disabled={busy || !current || !next}>
            {busy && <span className="spin" />} Change password
          </button>
          <button className="btn" onClick={logout}>Sign out</button>
        </div>
      </div>
    </section>
  );
}

function ConnectionCard() {
  const [info, setInfo] = useState<Wire | null>(null);
  useEffect(() => {
    void api.connection().then(setInfo).catch(() => {});
  }, []);
  return (
    <section id="s-connection">
      <SectionTitle>VPN server</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 560 }}>
        {!info ? (
          <LoadingBlock />
        ) : (
          <KV
            rows={[
              ["management address", `${info.host}:${info.port}`],
              ["state", info.configured ? "connected" : "not connected"],
            ]}
          />
        )}
        <div style={{ marginTop: "var(--s3)" }}>
          <Link to="/connect" className="btn">
            Edit connection
          </Link>
        </div>
        <p className="hint" style={{ marginTop: "var(--s2)" }}>
          The SoftEther administrator password itself is changed under Server settings →
          Encryption; changing it there keeps this connection in step automatically.
        </p>
      </div>
    </section>
  );
}

function PanelCard() {
  const [settings, setSettings] = useState<Wire | null>(null);
  const [dirty, setDirty] = useState(false);
  const [restartNeeded, setRestartNeeded] = useState(false);
  const { guard, push } = useToast();

  useEffect(() => {
    void api.settings().then(setSettings).catch(() => {});
  }, []);

  if (!settings) return null;
  const set = (k: string, v: unknown) => {
    setSettings((s) => ({ ...(s ?? {}), [k]: v }));
    setDirty(true);
  };

  const save = () =>
    guard(async () => {
      const out = await api.saveSettings({ web_path: String(settings.web_path ?? "") });
      setDirty(false);
      if ((out as Wire).restart_required) setRestartNeeded(true);
    }, "Settings saved.");

  return (
    <section id="s-panel">
      <SectionTitle>Panel</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 560 }}>
        <Field
          label="Web path"
          hint="The secret prefix the panel serves under; empty serves at the root. Takes effect after a restart."
        >
          <input className="input mono" value={String(settings.web_path ?? "")} onChange={(e) => set("web_path", e.target.value)} autoCapitalize="none" spellCheck={false} placeholder="(root)" />
        </Field>
        <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap" }}>
          {dirty && (
            <button className="btn btn--primary" onClick={() => void save()}>Save</button>
          )}
          {restartNeeded && (
            <button className="btn" onClick={() => void guard(() => api.restartPanel(), "Restarting — the panel will be back on the new path in a few seconds.")}>
              <IconRefresh size={14} /> Restart panel now
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

/** The per-user placeholders the generated document carries. */
const VPN_PARAMS: [string, string][] = [
  ["{host}", "the server address chosen in the download dialog"],
  ["{port}", "the SoftEther port picked in the dialog"],
  ["{hub}", "the user's Virtual Hub"],
  ["{username}", "the VPN user the file signs in as"],
  ["{account_name}", "the connection's display name, from the naming template below"],
  ["{auth_type}", "from the user's auth type: 0 anonymous, 1 password (hashed), 2 RADIUS/NT, 3 certificate"],
  ["{hashed_password}", "when embedded: SoftEther's stored form, SHA-0(password + UPPERCASE(username)), base64"],
  ["{plain_password}", "plaintext — RADIUS/NT users only; “$” otherwise"],
];

const PROXY_TYPES: [number, string][] = [
  [0, "Direct connection (no proxy)"],
  [1, "HTTP proxy"],
  [2, "SOCKS4 proxy"],
  [3, "SOCKS5 proxy"],
];

const RETRY_FOREVER = 4294967295;

/**
 * The .vpn editor: every option the SoftEther client reads from a
 * connection file, shaped once here and applied to every download. The
 * per-user values stay as {placeholders}, visible in the preview.
 */
function VpnFileTemplateCard() {
  const [state, setState] = useState<Wire | null>(null);
  const [dirty, setDirty] = useState(false);
  const [preview, setPreview] = useState(false);
  const { guard } = useToast();

  useEffect(() => {
    void api.vpnTemplate().then(setState).catch(() => {});
  }, []);
  if (!state) return null;

  const opt = (state.options ?? {}) as Wire;
  const setTop = (k: string, v: unknown) => {
    setState((s) => ({ ...(s ?? {}), [k]: v }));
    setDirty(true);
  };
  const setOpt = (k: string, v: unknown) => {
    setState((s) => ({ ...(s ?? {}), options: { ...((s?.options as Wire) ?? {}), [k]: v } }));
    setDirty(true);
  };

  const save = () =>
    guard(async () => {
      const out = await api.saveVpnTemplate({
        options: state.options,
        account_name_template: String(state.account_name_template ?? ""),
        filename_template: String(state.filename_template ?? ""),
        embed_password_default: Boolean(state.embed_password_default),
      });
      setState(out);
      setDirty(false);
    }, "Connection file template saved.");

  const resetDefaults = () => {
    setState((s) => ({ ...(s ?? {}), options: { ...((s?.defaults as Wire) ?? {}) } }));
    setDirty(true);
  };

  const num = (k: string) => Number(opt[k]) || 0;
  const boolOpt = (k: string) => Boolean(opt[k]);
  const proxied = num("ProxyType") !== 0;
  const retryForever = num("NumRetry") >= RETRY_FOREVER;

  // Light up the {parameters} so they stand out from the fixed document.
  const highlighted = (text: string) =>
    text.split(/(\{[a-z_]+\})/g).map((part, i) =>
      part.startsWith("{") ? <span key={i} className="tpl__ph">{part}</span> : part,
    );

  const numberField = (label: string, key: string, hint?: string, min = 0, max = RETRY_FOREVER) => (
    <Field label={label} hint={hint}>
      <input
        className="input mono"
        type="number"
        min={min}
        max={max}
        value={num(key)}
        onChange={(e) => setOpt(key, Number(e.target.value))}
      />
    </Field>
  );

  return (
    <section id="s-vpnfile">
      <SectionTitle
        actions={
          <>
            {dirty && <button className="btn btn--sm btn--primary" onClick={() => void save()}>Save</button>}
            <button className="btn btn--sm" onClick={resetDefaults}>Reset to defaults</button>
          </>
        }
      >
        Connection files (.vpn)
      </SectionTitle>
      <div className="card" style={{ padding: "var(--s4)" }}>
        <p className="lede" style={{ marginBottom: "var(--s3)" }}>
          Every <span className="mono">.vpn</span> file the panel hands out is built from this template.
          Options set here apply to all downloads; the per-user values are filled in at download time.
        </p>

        <div className="tpl__group">Naming</div>
        <div className="row2">
          <Field
            label="File name"
            hint={<>Variables: <span className="mono">{"{hub} {username} {host} {port}"}</span> — “.vpn” is added.</>}
          >
            <input className="input mono" value={String(state.filename_template ?? "")} onChange={(e) => setTop("filename_template", e.target.value)} spellCheck={false} />
          </Field>
          <Field label="Connection name in the client" hint="What the user sees in their VPN Client's connection list.">
            <input className="input mono" value={String(state.account_name_template ?? "")} onChange={(e) => setTop("account_name_template", e.target.value)} spellCheck={false} />
          </Field>
        </div>
        <CheckRow
          checked={Boolean(state.embed_password_default)}
          onChange={(v) => setTop("embed_password_default", v)}
          label="Embed the user's password by default"
          hint="The download dialog starts with embedding on. The credential goes in as SoftEther's own hash — taken from what the panel has stored, or recovered from the server's configuration."
        />

        <div className="tpl__group">Tunnel</div>
        <CheckRow checked={boolOpt("UseEncrypt")} onChange={(v) => setOpt("UseEncrypt", v)} label="Encrypt with SSL" hint="Off sends VPN traffic in the clear — only for links that are already protected." />
        <CheckRow checked={boolOpt("UseCompress")} onChange={(v) => setOpt("UseCompress", v)} label="Compress data" />
        <CheckRow checked={boolOpt("NoUdpAcceleration")} onChange={(v) => setOpt("NoUdpAcceleration", v)} label="Disable UDP acceleration" />
        <CheckRow checked={boolOpt("HalfConnection")} onChange={(v) => setOpt("HalfConnection", v)} label="Half-duplex connections" hint="Each TCP connection carries one direction only; needs at least two connections." />
        <div className="row2">
          {numberField("TCP connections", "MaxConnection", "1–32; more can help on lossy links.", 1, 32)}
          {numberField("UDP port", "PortUDP", "0 lets the client pick.", 0, 65535)}
        </div>

        <div className="tpl__group">Reliability</div>
        <CheckRow checked={retryForever} onChange={(v) => setOpt("NumRetry", v ? RETRY_FOREVER : 10)} label="Retry forever" hint="Keep redialling until the connection succeeds." />
        <div className="row2">
          {!retryForever && numberField("Retry count", "NumRetry", undefined, 0, 4096)}
          {numberField("Retry interval (s)", "RetryInterval", undefined, 5, 3600)}
          {numberField("Extra connection interval (s)", "AdditionalConnectionInterval", "Delay between the additional TCP connections.", 1, 3600)}
          {numberField("Reconnect lifetime (s)", "ConnectionDisconnectSpan", "0 keeps connections until they drop on their own.")}
        </div>

        <div className="tpl__group">Security</div>
        <CheckRow checked={boolOpt("CheckServerCert")} onChange={(v) => setOpt("CheckServerCert", v)} label="Verify the server certificate" hint="On, the client refuses a server whose certificate it cannot trust." />
        <CheckRow checked={boolOpt("NoTls1")} onChange={(v) => setOpt("NoTls1", v)} label="Disable TLS 1.0" />

        <div className="tpl__group">Proxy</div>
        <Field label="Connect via">
          <select className="select" value={num("ProxyType")} onChange={(e) => setOpt("ProxyType", Number(e.target.value))}>
            {PROXY_TYPES.map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
          </select>
        </Field>
        {proxied && (
          <>
            <div className="row2">
              <Field label="Proxy host">
                <input className="input mono" value={String(opt.ProxyName ?? "")} onChange={(e) => setOpt("ProxyName", e.target.value)} spellCheck={false} />
              </Field>
              {numberField("Proxy port", "ProxyPort", undefined, 0, 65535)}
            </div>
            <div className="row2">
              <Field label="Proxy username" hint="Leave empty when the proxy needs no sign-in.">
                <input className="input mono" value={String(opt.ProxyUsername ?? "")} onChange={(e) => setOpt("ProxyUsername", e.target.value)} spellCheck={false} />
              </Field>
              <Field label="Proxy password" hint="Stored scrambled in the file, the way the client itself stores it.">
                <input className="input" type="password" value={String(opt.ProxyPassword ?? "")} onChange={(e) => setOpt("ProxyPassword", e.target.value)} autoComplete="off" />
              </Field>
            </div>
          </>
        )}

        <div className="tpl__group">Client behaviour</div>
        <CheckRow checked={boolOpt("StartupAccount")} onChange={(v) => setOpt("StartupAccount", v)} label="Connect at client startup" />
        <CheckRow checked={boolOpt("HideStatusWindow")} onChange={(v) => setOpt("HideStatusWindow", v)} label="Hide the status window while connecting" />
        <CheckRow checked={boolOpt("HideNicInfoWindow")} onChange={(v) => setOpt("HideNicInfoWindow", v)} label="Hide the adapter info window" />
        <CheckRow checked={boolOpt("NoRoutingTracking")} onChange={(v) => setOpt("NoRoutingTracking", v)} label="Disable routing tracking" hint="Stops the client adjusting the routing table while connected." />
        <CheckRow checked={boolOpt("DisableQoS")} onChange={(v) => setOpt("DisableQoS", v)} label="Disable VoIP/QoS handling" />
        <CheckRow checked={boolOpt("RequireMonitorMode")} onChange={(v) => setOpt("RequireMonitorMode", v)} label="Monitoring mode" hint="Asks for a promiscuous session; needs hub permission." />
        <CheckRow checked={boolOpt("RequireBridgeRoutingMode")} onChange={(v) => setOpt("RequireBridgeRoutingMode", v)} label="Bridge / router mode" hint="Needed when the client side bridges or routes for other machines." />
        <div className="row2">
          <Field label="Virtual adapter name" hint="The client-side NIC the connection binds to.">
            <input className="input mono" value={String(opt.DeviceName ?? "")} onChange={(e) => setOpt("DeviceName", e.target.value)} spellCheck={false} />
          </Field>
          <Field label="Custom HTTP header" hint="Sent during HTTP-proxy connect; rarely needed.">
            <input className="input mono" value={String(opt.CustomHttpHeader ?? "")} onChange={(e) => setOpt("CustomHttpHeader", e.target.value)} spellCheck={false} />
          </Field>
        </div>

        <div style={{ display: "flex", gap: "var(--s2)", marginTop: "var(--s2)", flexWrap: "wrap" }}>
          {dirty && (
            <button className="btn btn--primary" onClick={() => void save()}>Save template</button>
          )}
          <button className="btn" onClick={() => setPreview((p) => !p)} aria-expanded={preview}>
            {preview ? "Hide the document" : "Preview the document"}
          </button>
        </div>
        {preview && (
          <div style={{ marginTop: "var(--s3)" }}>
            {dirty && <p className="hint">The preview shows the last saved template — save to refresh it.</p>}
            <pre className="tpl">{highlighted(String(state.template ?? ""))}</pre>
            <div className="tpl__params" style={{ marginTop: "var(--s3)", marginBottom: 0 }}>
              {VPN_PARAMS.map(([name, what]) => (
                <div key={name} className="tpl__param">
                  <span className="mono tpl__ph">{name}</span>
                  <span className="micro">{what}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * Every monitor the panel runs: what it reads, how often, and whether it
 * reads at all. Each is a thread or a tick that costs something -- one RPC
 * per hub, one per live session -- so each is switchable on its own.
 */
function MonitoringCard() {
  const [settings, setSettings] = useState<Wire | null>(null);
  const [dirty, setDirty] = useState(false);
  const { guard } = useToast();

  useEffect(() => {
    void api.settings().then(setSettings).catch(() => {});
  }, []);
  if (!settings) return null;

  const set = (k: string, v: unknown) => {
    setSettings((s) => ({ ...(s ?? {}), [k]: v }));
    setDirty(true);
  };
  const num = (k: string, fallback: number) => Number(settings[k]) || fallback;
  const on = (k: string) => Boolean(settings[k]);

  const save = () =>
    guard(async () => {
      setSettings(
        await api.saveSettings({
          resource_monitor_enabled: on("resource_monitor_enabled"),
          resource_interval_seconds: num("resource_interval_seconds", 3),
          resource_history_points: num("resource_history_points", 100),
          traffic_monitor_enabled: on("traffic_monitor_enabled"),
          sample_interval_minutes: num("sample_interval_minutes", 5),
          sample_retention_days: num("sample_retention_days", 90),
          session_monitor_enabled: on("session_monitor_enabled"),
          session_interval_seconds: num("session_interval_seconds", 60),
          session_traffic_enabled: on("session_traffic_enabled"),
          session_history_retention_days: num("session_history_retention_days", 30),
          quota_enforcement_enabled: on("quota_enforcement_enabled"),
          quota_interval_seconds: num("quota_interval_seconds", 60),
          ui_live_seconds: num("ui_live_seconds", 5),
          ui_detail_seconds: num("ui_detail_seconds", 15),
          ui_list_seconds: num("ui_list_seconds", 30),
        }),
      );
      setDirty(false);
    }, "Monitoring settings saved.");

  const numberField = (
    label: string,
    key: string,
    fallback: number,
    min: number,
    max: number,
    hint?: string,
  ) => (
    <Field label={label} hint={hint}>
      <input
        className="input mono"
        type="number"
        min={min}
        max={max}
        value={num(key, fallback)}
        onChange={(e) => set(key, Number(e.target.value))}
      />
    </Field>
  );

  return (
    <section id="s-monitoring">
      <SectionTitle
        actions={
          dirty ? (
            <button className="btn btn--sm btn--primary" onClick={() => void save()}>Save</button>
          ) : undefined
        }
      >
        Monitoring
      </SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 720 }}>
        <p className="lede" style={{ marginBottom: "var(--s3)" }}>
          Turning a monitor off stops it reading; what it has already recorded stays until its
          retention window prunes it, which keeps running either way.
        </p>

        <div className="tpl__group">Host resources</div>
        <CheckRow
          checked={on("resource_monitor_enabled")}
          onChange={(v) => set("resource_monitor_enabled", v)}
          label="Monitor this machine"
          hint="CPU, memory, swap, disks and network on the dashboard. Read from /proc, so it costs nothing outside this host."
        />
        {on("resource_monitor_enabled") && (
          <div className="row2">
            {numberField("Read every (seconds)", "resource_interval_seconds", 3, 1, 3600, "How often the machine's counters are read.")}
            {numberField("History points", "resource_history_points", 100, 10, 2000, "How many readings the sparklines keep.")}
          </div>
        )}

        <div className="tpl__group">Traffic (hubs and users)</div>
        <CheckRow
          checked={on("traffic_monitor_enabled")}
          onChange={(v) => set("traffic_monitor_enabled", v)}
          label="Record hub and user traffic"
          hint="What the usage charts are derived from. One RPC per hub, plus one for its user list, every tick."
        />
        {on("traffic_monitor_enabled") && (
          <div className="row2">
            {numberField("Sample every (minutes)", "sample_interval_minutes", 5, 1, 1440)}
            {numberField("Keep samples (days)", "sample_retention_days", 90, 1, 3650)}
          </div>
        )}

        <div className="tpl__group">Sessions</div>
        <CheckRow
          checked={on("session_monitor_enabled")}
          onChange={(v) => set("session_monitor_enabled", v)}
          label="Record session history"
          hint="Who connected, from what address, when it started and ended. Ticks on its own clock, below."
        />
        {on("session_monitor_enabled") && (
          <>
            <CheckRow
              checked={on("session_traffic_enabled")}
              onChange={(v) => set("session_traffic_enabled", v)}
              label="Record each session's traffic over time"
              hint="What a session's usage chart is drawn from. Costs one extra RPC per live session per tick — the first thing to turn off on a busy server."
            />
            <div className="row2">
              {numberField("Sample every (seconds)", "session_interval_seconds", 60, 5, 3600, "How often the live session list is read. A session shorter than this can be missed entirely.")}
              {numberField("Keep session history (days)", "session_history_retention_days", 30, 1, 3650, "Applies to the logins and their traffic series alike.")}
            </div>
          </>
        )}

        <div className="tpl__group">Traffic limits</div>
        <CheckRow
          checked={on("quota_enforcement_enabled")}
          onChange={(v) => set("quota_enforcement_enabled", v)}
          label="Enforce hub and config traffic limits"
          hint="Counts what each limited hub and config moves and cuts it off at its ceiling. Costs nothing while no limit is set; while some are, one RPC per limited hub per tick."
        />
        {on("quota_enforcement_enabled") && (
          <div className="row2">
            {numberField("Check every (seconds)", "quota_interval_seconds", 60, 10, 3600, "How late a ceiling can bite. Traffic that moves between two checks is still counted — it is only the cut-off that waits.")}
          </div>
        )}

        <div className="tpl__group">Page refresh</div>
        <p className="hint" style={{ marginTop: 0 }}>
          How often an open page re-reads, by the kind of thing it is watching. This costs a request
          per open tab, not per server.
        </p>
        <div className="row2">
          {numberField("Live views (seconds)", "ui_live_seconds", 5, 1, 3600, "Dashboard, sessions, connections.")}
          {numberField("Detail screens (seconds)", "ui_detail_seconds", 15, 1, 3600, "A hub, a user, the address tables.")}
          {numberField("Lists (seconds)", "ui_list_seconds", 30, 1, 3600, "User and group lists, server settings.")}
        </div>

        {dirty && (
          <div style={{ marginTop: "var(--s3)" }}>
            <button className="btn btn--primary" onClick={() => void save()}>Save monitoring settings</button>
          </div>
        )}
      </div>
    </section>
  );
}

function UpdatesCard() {
  const { status, check, isChecking, open } = useUpdate();
  const checkInfo = status?.check;
  return (
    <section id="s-updates">
      <SectionTitle>Updates</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 560 }}>
        <KV
          rows={[
            ["running", String(checkInfo?.current_version ?? "—")],
            ["latest release", String(checkInfo?.latest?.version || "unknown")],
            ["source", String(checkInfo?.source ?? "—")],
            ["last check", checkInfo?.checked_at ? timeAgo(String(checkInfo.checked_at)) : "never"],
          ]}
        />
        {checkInfo?.update_available ? (
          <div className="alert alert--info" style={{ marginTop: "var(--s3)" }}>
            <div className="alert__t">{String(checkInfo.latest.version)} is available.</div>
            Open the updater from the version pill, or the button below.
          </div>
        ) : null}
        {checkInfo?.note ? <div className="hint">{String(checkInfo.note)}</div> : null}
        {checkInfo?.error ? <div className="hint hint--err">{String(checkInfo.error)}</div> : null}
        <div style={{ display: "flex", gap: "var(--s2)", marginTop: "var(--s3)" }}>
          <button className="btn" onClick={() => void check()} disabled={isChecking}>
            {isChecking && <span className="spin" />} Check now
          </button>
          <button className="btn btn--primary" onClick={open}>Open updater</button>
        </div>
      </div>
    </section>
  );
}

function AuditCard() {
  const [rows, setRows] = useState<Wire[] | null>(null);
  const [exhausted, setExhausted] = useState(false);
  // The trail grows without bound; the page shows its head and lets an
  // operator walk back through it rather than rendering months of it.
  const { visible, reveal } = useReveal(5);

  const load = useCallback(async (beforeId = 0) => {
    const batch = await api.audit(beforeId).catch(() => []);
    setRows((current) => (beforeId ? [...(current ?? []), ...batch] : batch));
    if (batch.length < 100) setExhausted(true);
  }, []);
  useEffect(() => void load(), [load]);

  const shown = rows ? rows.slice(0, visible) : [];

  return (
    <section id="s-audit">
      <SectionTitle count={rows?.length}>Audit log</SectionTitle>
      {rows === null ? (
        <LoadingBlock />
      ) : rows.length === 0 ? (
        <div className="card" style={{ padding: "var(--s4)" }}>
          <span className="micro">Every state-changing action lands here — nothing yet.</span>
        </div>
      ) : (
        <div className="card tcard">
          <div className="tscroll">
            <table className="dtable">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={String(r.id)}>
                    <td className="tmono">{timeAgo(String(r.created_date))}</td>
                    <td className="tmono">{String(r.username)}</td>
                    <td><Pill kind={String(r.action).includes("delete") || String(r.action).includes("crash") ? "warn" : "idle"} label={String(r.action)} /></td>
                    <td className="tmono">{[r.target_type, r.target_key].filter(Boolean).join(" · ")}</td>
                    <td className="micro">{String(r.detail || "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <MoreRows
            shown={shown.length}
            loaded={rows.length}
            exhausted={exhausted}
            noun="entries"
            onReveal={reveal}
            onLoad={() => load(Number(rows[rows.length - 1].id))}
          />
        </div>
      )}
    </section>
  );
}

function AboutCard() {
  const [info, setInfo] = useState<Wire | null>(null);
  useEffect(() => {
    void api.systemInfo().then(setInfo).catch(() => {});
  }, []);
  return (
    <section id="s-about">
      <SectionTitle>About</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 560 }}>
        {!info ? (
          <LoadingBlock />
        ) : (
          <KV
            rows={[
              ["version", String(info.version ?? "—")],
              ["release repo", String(info.release_repo ?? "—")],
              ["service", `${(info.service as Wire)?.unit ?? "—"} (${(info.service as Wire)?.active || "not under systemd"})`],
              ["environment file", String(info.env_file ?? "—")],
            ]}
          />
        )}
        <p className="micro" style={{ marginTop: "var(--s3)" }}>
          SoftEther Manager — a self-hosted panel over the SoftEther VPN Server JSON-RPC API.
        </p>
      </div>
    </section>
  );
}
