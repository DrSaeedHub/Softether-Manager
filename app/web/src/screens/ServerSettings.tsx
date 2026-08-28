"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckRow, ConfirmSheet, Empty, ErrorAlert, Field, KV, LoadingBlock, PageHead, SectionTitle, usePoll } from "../components/bits";
import { api, type Wire } from "../lib/api";
import { Link } from "../lib/router";
import { useToast } from "../lib/toast";
import { downloadBase64, fileToBase64, formatBytes, formatDate, timeAgo } from "../lib/util";
import { IconDownload, IconPlus, IconTrash, IconUpload } from "../ui/Icon";
import { Sheet } from "../ui/Sheet";
import { OnlinePill, Pill } from "../ui/Status";

/**
 * Everything server-wide, as one scrolling document of cards -- listeners,
 * protocols, encryption, bridges, layer-3 switches, clustering, keep-alive,
 * syslog, the configuration file, and the danger zone. Tabs would hide
 * state; a document lets an operator scan all of it.
 */
export function ServerSettings({ section }: { section?: string }) {
  useEffect(() => {
    if (section) {
      requestAnimationFrame(() =>
        document.getElementById(`ss-${section}`)?.scrollIntoView({ behavior: "smooth", block: "start" }),
      );
    }
  }, [section]);

  return (
    <div className="page">
      <PageHead title="Server settings" sub={<Link to="/" className="linkish">← dashboard</Link>} />
      <div className="setdoc">
        <ListenersCard />
        <ProtocolsCard />
        <EncryptionCard />
        <BridgeCard />
        <L3Card />
        <EtherIpCard />
        <FarmCard />
        <KeepSyslogCard />
        <ConfigCard />
        <DangerZone />
      </div>
    </div>
  );
}

/* ── listeners ────────────────────────────────────────────────────────────── */

function ListenersCard() {
  const [listeners, setListeners] = useState<Wire[] | null>(null);
  const [special, setSpecial] = useState<Wire | null>(null);
  const [adding, setAdding] = useState(false);
  const [newPort, setNewPort] = useState(5555);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.listeners().catch(() => null);
    if (r) setListeners((r.ListenerList as Wire[]) ?? []);
    setSpecial(await api.specialListener().catch(() => null));
  }, []);
  usePoll(load, "list", []);

  return (
    <section id="ss-listeners">
      <SectionTitle
        count={listeners?.length}
        actions={
          <button className="btn btn--sm" onClick={() => setAdding(true)}>
            <IconPlus size={14} /> Add port
          </button>
        }
      >
        TCP listeners
      </SectionTitle>
      <div className="card" style={{ padding: "var(--s3) var(--s4)" }}>
        {listeners === null ? (
          <LoadingBlock />
        ) : (
          listeners.map((l) => (
            <div key={String(l.Ports_u32)} className="lrow">
              <span className="mono" style={{ fontSize: "var(--t-data)", fontWeight: 600 }}>{String(l.Ports_u32)}</span>
              {l.Errors_bool ? <Pill kind="err" label="error" /> : <OnlinePill online={Boolean(l.Enables_bool)} onLabel="listening" offLabel="disabled" />}
              <span style={{ flex: 1 }} />
              <button
                className="btn btn--sm"
                onClick={() => void guard(async () => {
                  await api.toggleListener(Number(l.Ports_u32), !l.Enables_bool);
                  await load();
                })}
              >
                {l.Enables_bool ? "Disable" : "Enable"}
              </button>
              <button
                className="btn btn--sm btn--ghost"
                onClick={() => void guard(async () => {
                  await api.deleteListener(Number(l.Ports_u32));
                  await load();
                }, `Listener ${l.Ports_u32} deleted.`)}
                aria-label="Delete listener"
              >
                <IconTrash size={14} />
              </button>
            </div>
          ))
        )}
        {special && (
          <div style={{ marginTop: "var(--s3)" }}>
            <CheckRow
              checked={Boolean(special.VpnOverIcmpListener_bool)}
              onChange={(v) => void guard(async () => {
                await api.setSpecialListener({ ...special, VpnOverIcmpListener_bool: v });
                await load();
              })}
              label="VPN over ICMP"
              hint="Clients tunnel in ping packets — crosses networks that allow nothing else."
            />
            <CheckRow
              checked={Boolean(special.VpnOverDnsListener_bool)}
              onChange={(v) => void guard(async () => {
                await api.setSpecialListener({ ...special, VpnOverDnsListener_bool: v });
                await load();
              })}
              label="VPN over DNS"
              hint="Tunnels in DNS queries on UDP 53. Slow, and a last resort."
            />
          </div>
        )}
      </div>
      {adding && (
        <Sheet
          title="New listener"
          onClose={() => setAdding(false)}
          footer={
            <>
              <button className="btn" onClick={() => setAdding(false)}>Cancel</button>
              <button
                className="btn btn--primary"
                onClick={() => void guard(async () => {
                  await api.createListener(newPort, true);
                  setAdding(false);
                  await load();
                }, `Listening on ${newPort}.`)}
              >
                Add
              </button>
            </>
          }
        >
          <Field label="TCP port">
            <input className="input mono" type="number" min={1} max={65535} value={newPort} onChange={(e) => setNewPort(Number(e.target.value))} inputMode="numeric" />
          </Field>
        </Sheet>
      )}
    </section>
  );
}

/* ── protocols ────────────────────────────────────────────────────────────── */

function ProtocolsCard() {
  const [ipsec, setIpsec] = useState<Wire | null>(null);
  const [openvpn, setOpenvpn] = useState<Wire | null>(null);
  const [azure, setAzure] = useState<Wire | null>(null);
  const [ddns, setDdns] = useState<Wire | null>(null);
  const [hubs, setHubs] = useState<string[]>([]);
  const [secret, setSecret] = useState("");
  const [dirty, setDirty] = useState<Record<string, boolean>>({});
  const [ddnsHost, setDdnsHost] = useState("");
  const { guard } = useToast();

  const load = useCallback(async () => {
    const [i, o, a, d, h] = await Promise.all([
      api.ipsec().catch(() => null),
      api.openvpn().catch(() => null),
      api.azure().catch(() => null),
      api.ddns().catch(() => null),
      api.hubs().catch(() => null),
    ]);
    if (!dirty.ipsec && i) { setIpsec(i); setSecret(String(i.IPsec_Secret_str ?? "")); }
    if (!dirty.openvpn && o) setOpenvpn(o);
    if (a) setAzure(a);
    if (d) setDdns(d);
    if (h) setHubs(((h.HubList as Wire[]) ?? []).map((x) => String(x.HubName_str)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  usePoll(load, "list", []);

  return (
    <section id="ss-protocols">
      <SectionTitle>Access protocols</SectionTitle>
      <div className="setgrid">
        {/* IPsec / L2TP */}
        <div className="card" style={{ padding: "var(--s4)" }}>
          <div className="section__t" style={{ marginBottom: "var(--s3)" }}>L2TP / IPsec / EtherIP</div>
          {!ipsec ? (
            <LoadingBlock />
          ) : (
            <>
              <CheckRow checked={Boolean(ipsec.L2TP_IPsec_bool)} onChange={(v) => { setIpsec({ ...ipsec, L2TP_IPsec_bool: v }); setDirty((d) => ({ ...d, ipsec: true })); }}
                label="L2TP over IPsec" hint="The built-in VPN of iOS, Android and Windows." />
              <CheckRow checked={Boolean(ipsec.L2TP_Raw_bool)} onChange={(v) => { setIpsec({ ...ipsec, L2TP_Raw_bool: v }); setDirty((d) => ({ ...d, ipsec: true })); }}
                label="Raw L2TP (no encryption)" hint="Only for equipment that cannot do IPsec." />
              <CheckRow checked={Boolean(ipsec.EtherIP_IPsec_bool)} onChange={(v) => { setIpsec({ ...ipsec, EtherIP_IPsec_bool: v }); setDirty((d) => ({ ...d, ipsec: true })); }}
                label="EtherIP / L2TPv3 over IPsec" hint="Site-to-site from routers; client IDs are defined below." />
              <Field label="IPsec pre-shared key" hint="What clients type as the 'secret'. Keep it short — some devices truncate at 9 characters.">
                <input className="input mono" value={secret} onChange={(e) => { setSecret(e.target.value); setDirty((d) => ({ ...d, ipsec: true })); }} autoCapitalize="none" spellCheck={false} />
              </Field>
              <Field label="Default hub" hint="Where L2TP users land when they do not name a hub.">
                <select className="select" value={String(ipsec.L2TP_DefaultHub_str ?? "")} onChange={(e) => { setIpsec({ ...ipsec, L2TP_DefaultHub_str: e.target.value }); setDirty((d) => ({ ...d, ipsec: true })); }}>
                  {hubs.map((h) => <option key={h} value={h}>{h}</option>)}
                </select>
              </Field>
              {dirty.ipsec && (
                <button className="btn btn--primary" onClick={() => void guard(async () => {
                  await api.setIpsec({
                    L2TP_Raw_bool: Boolean(ipsec.L2TP_Raw_bool),
                    L2TP_IPsec_bool: Boolean(ipsec.L2TP_IPsec_bool),
                    EtherIP_IPsec_bool: Boolean(ipsec.EtherIP_IPsec_bool),
                    IPsec_Secret_str: secret,
                    L2TP_DefaultHub_str: String(ipsec.L2TP_DefaultHub_str ?? ""),
                  });
                  setDirty((d) => ({ ...d, ipsec: false }));
                }, "IPsec settings saved.")}>
                  Save IPsec
                </button>
              )}
            </>
          )}
        </div>

        {/* OpenVPN / SSTP */}
        <div className="card" style={{ padding: "var(--s4)" }}>
          <div className="section__t" style={{ marginBottom: "var(--s3)" }}>OpenVPN & SSTP clone</div>
          {!openvpn ? (
            <LoadingBlock />
          ) : (
            <>
              <CheckRow checked={Boolean(openvpn.EnableOpenVPN_bool)} onChange={(v) => { setOpenvpn({ ...openvpn, EnableOpenVPN_bool: v }); setDirty((d) => ({ ...d, openvpn: true })); }}
                label="OpenVPN server" hint="Stock OpenVPN clients connect straight to this server." />
              <Field label="OpenVPN UDP ports" hint="Comma separated.">
                <input className="input mono" value={String(openvpn.OpenVPNPortList_str ?? "")} onChange={(e) => { setOpenvpn({ ...openvpn, OpenVPNPortList_str: e.target.value }); setDirty((d) => ({ ...d, openvpn: true })); }} spellCheck={false} inputMode="numeric" />
              </Field>
              <CheckRow checked={Boolean(openvpn.EnableSSTP_bool)} onChange={(v) => { setOpenvpn({ ...openvpn, EnableSSTP_bool: v }); setDirty((d) => ({ ...d, openvpn: true })); }}
                label="SSTP server" hint="Microsoft SSTP over 443. Needs the server certificate to be trusted by clients." />
              <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap" }}>
                {dirty.openvpn && (
                  <button className="btn btn--primary" onClick={() => void guard(async () => {
                    await api.setOpenvpn({
                      EnableOpenVPN_bool: Boolean(openvpn.EnableOpenVPN_bool),
                      OpenVPNPortList_str: String(openvpn.OpenVPNPortList_str ?? "1194"),
                      EnableSSTP_bool: Boolean(openvpn.EnableSSTP_bool),
                    });
                    setDirty((d) => ({ ...d, openvpn: false }));
                  }, "OpenVPN/SSTP settings saved.")}>
                    Save
                  </button>
                )}
                <button className="btn" onClick={() => void guard(async () => {
                  const r = await api.openvpnSample();
                  downloadBase64(r.filename, r.zip_base64, "application/zip");
                }, "Sample client config downloaded.")}>
                  <IconDownload size={14} /> Sample client config
                </button>
              </div>
            </>
          )}
        </div>

        {/* Azure + DDNS */}
        <div className="card" style={{ padding: "var(--s4)" }}>
          <div className="section__t" style={{ marginBottom: "var(--s3)" }}>VPN Azure & dynamic DNS</div>
          {!azure || !ddns ? (
            <LoadingBlock />
          ) : (
            <>
              <CheckRow checked={Boolean(azure.IsEnabled_bool)} onChange={(v) => void guard(async () => {
                await api.setAzure(v);
                await load();
              }, v ? "VPN Azure enabled." : "VPN Azure disabled.")}
                label="VPN Azure relay" hint="Reachable through azure even behind NAT, at <hostname>.vpnazure.net." />
              <KV rows={[
                ["DDNS hostname", String(ddns.CurrentHostName_str || "—")],
                ["FQDN", String(ddns.CurrentFqdn_str || "—")],
                ["IPv4", String(ddns.CurrentIPv4_str || "—")],
                ["last error", Number(ddns.Err_IPv4_u32 ?? 0) === 0 ? "none" : String(ddns.ErrStr_IPv4_utf || `code ${ddns.Err_IPv4_u32}`)],
              ]} />
              <Field label="Change DDNS hostname" hint="The name half of <name>.softether.net — it must be globally unused.">
                <div style={{ display: "flex", gap: "var(--s2)" }}>
                  <input className="input mono" value={ddnsHost} onChange={(e) => setDdnsHost(e.target.value)} placeholder={String(ddns.CurrentHostName_str ?? "")} autoCapitalize="none" spellCheck={false} />
                  <button className="btn" disabled={!ddnsHost.trim()} onClick={() => void guard(async () => {
                    await api.setDdnsHostname(ddnsHost.trim());
                    setDdnsHost("");
                    await load();
                  }, "DDNS hostname changed.")}>
                    Apply
                  </button>
                </div>
              </Field>
            </>
          )}
        </div>
      </div>
    </section>
  );
}

/* ── encryption & certificate ─────────────────────────────────────────────── */

function EncryptionCard() {
  const [cipher, setCipher] = useState<string | null>(null);
  const [cipherDirty, setCipherDirty] = useState(false);
  const [adminPw, setAdminPw] = useState("");
  const [regenCn, setRegenCn] = useState("");
  const [confirmingRegen, setConfirmingRegen] = useState(false);
  const { guard } = useToast();

  useEffect(() => {
    void api.cipher().then((r) => setCipher(String(r.String_str ?? ""))).catch(() => {});
  }, []);

  const uploadCertAndKey = async (certFile: File, keyFile: File) => {
    const [c, k] = await Promise.all([fileToBase64(certFile), fileToBase64(keyFile)]);
    await guard(() => api.setCert(c, k), "Certificate replaced.");
  };

  return (
    <section id="ss-encryption">
      <SectionTitle>Encryption & certificate</SectionTitle>
      <div className="setgrid">
        <div className="card" style={{ padding: "var(--s4)" }}>
          <div className="section__t" style={{ marginBottom: "var(--s3)" }}>Cipher</div>
          <Field label="TLS cipher for VPN sessions" hint="As SoftEther names them, e.g. AES128-SHA. What the server accepts depends on its build.">
            <input className="input mono" value={cipher ?? ""} onChange={(e) => { setCipher(e.target.value); setCipherDirty(true); }} spellCheck={false} />
          </Field>
          {cipherDirty && (
            <button className="btn btn--primary" onClick={() => void guard(async () => {
              await api.setCipher(cipher ?? "");
              setCipherDirty(false);
            }, "Cipher saved.")}>
              Save cipher
            </button>
          )}
          <div className="section__t" style={{ margin: "var(--s4) 0 var(--s3)" }}>Administrator password</div>
          <Field label="New password" hint="Changes the VPN server's own admin password; the panel updates its stored copy in the same motion.">
            <div style={{ display: "flex", gap: "var(--s2)" }}>
              <input className="input" type="password" value={adminPw} onChange={(e) => setAdminPw(e.target.value)} autoComplete="off" />
              <button className="btn" disabled={!adminPw} onClick={() => void guard(async () => {
                await api.setAdminPassword(adminPw);
                setAdminPw("");
              }, "Administrator password changed.")}>
                Change
              </button>
            </div>
          </Field>
        </div>

        <div className="card" style={{ padding: "var(--s4)" }}>
          <div className="section__t" style={{ marginBottom: "var(--s3)" }}>Server certificate</div>
          <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap", marginBottom: "var(--s3)" }}>
            <button className="btn" onClick={() => void guard(async () => {
              const r = await api.cert();
              downloadBase64("server.cer", String(r.cert_base64 ?? ""), "application/pkix-cert");
            })}>
              <IconDownload size={14} /> Download certificate
            </button>
            <button className="btn" onClick={() => setConfirmingRegen(true)}>Regenerate self-signed</button>
          </div>
          <CertUpload onUpload={uploadCertAndKey} />
        </div>
      </div>
      {confirmingRegen && (
        <Sheet
          title="Regenerate the server certificate"
          onClose={() => setConfirmingRegen(false)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmingRegen(false)}>Cancel</button>
              <button className="btn btn--danger" disabled={!regenCn.trim()} onClick={() => void guard(async () => {
                await api.regenerateCert(regenCn.trim());
                setConfirmingRegen(false);
              }, "Certificate regenerated.")}>
                Regenerate
              </button>
            </>
          }
        >
          <div className="lede" style={{ marginBottom: "var(--s3)" }}>
            A new self-signed certificate replaces the current one immediately. Clients that pinned
            the old one (and SSTP clients) will complain until they trust the new one.
          </div>
          <Field label="Common name (CN)" hint="Usually the server's public hostname.">
            <input className="input mono" value={regenCn} onChange={(e) => setRegenCn(e.target.value)} placeholder="vpn.example.net" spellCheck={false} autoCapitalize="none" />
          </Field>
        </Sheet>
      )}
    </section>
  );
}

function CertUpload({ onUpload }: { onUpload: (cert: File, key: File) => Promise<void> }) {
  const [cert, setCert] = useState<File | null>(null);
  const [key, setKey] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <Field label="Replace with your own (certificate + private key)">
        <input className="input" type="file" accept=".cer,.crt,.pem" onChange={(e) => setCert(e.target.files?.[0] ?? null)} />
      </Field>
      <Field label="Private key">
        <input className="input" type="file" accept=".key,.pem" onChange={(e) => setKey(e.target.files?.[0] ?? null)} />
      </Field>
      <button
        className="btn btn--primary"
        disabled={!cert || !key || busy}
        onClick={async () => {
          if (!cert || !key) return;
          setBusy(true);
          await onUpload(cert, key);
          setBusy(false);
          setCert(null);
          setKey(null);
        }}
      >
        {busy && <span className="spin" />} <IconUpload size={14} /> Install certificate
      </button>
    </>
  );
}

/* ── local bridge ─────────────────────────────────────────────────────────── */

function BridgeCard() {
  const [bridges, setBridges] = useState<Wire[] | null>(null);
  const [support, setSupport] = useState<Wire | null>(null);
  const [adding, setAdding] = useState(false);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const [b, s] = await Promise.all([
      api.bridges().catch(() => null),
      api.bridgeSupport().catch(() => null),
    ]);
    if (b) setBridges((b.LocalBridgeList as Wire[]) ?? []);
    if (s) setSupport(s);
  }, []);
  usePoll(load, "list", []);

  return (
    <section id="ss-bridge">
      <SectionTitle
        count={bridges?.length}
        actions={
          <button className="btn btn--sm" onClick={() => setAdding(true)} disabled={support ? !support.IsBridgeSupportedOs_bool : false}>
            <IconPlus size={14} /> New bridge
          </button>
        }
      >
        Local bridges
      </SectionTitle>
      {support && !support.IsBridgeSupportedOs_bool && (
        <div className="alert alert--warn">This operating system does not support local bridging.</div>
      )}
      {bridges === null ? (
        <LoadingBlock />
      ) : bridges.length === 0 ? (
        <Empty title="no local bridges">
          A local bridge joins a Virtual Hub to a physical network adapter, making VPN clients
          full members of the LAN behind this server.
        </Empty>
      ) : (
        <div className="rows">
          {bridges.map((b, i) => (
            <div key={i} className="row">
              <div className="row__main">
                <div className="row__name">
                  <span className="mono">{String(b.DeviceName_str)}</span>
                  <span className="micro">↔</span>
                  <span className="mono">{String(b.HubNameLB_str)}</span>
                  {b.TapMode_bool && <Pill kind="idle" label="tap" />}
                  <Pill kind={b.Active_bool ? "ok" : b.Online_bool ? "busy" : "idle"} label={b.Active_bool ? "operating" : b.Online_bool ? "starting" : "offline"} />
                </div>
              </div>
              <div className="row__side">
                <button className="btn btn--sm btn--ghost" aria-label="Delete bridge" onClick={() => void guard(async () => {
                  await api.deleteBridge(String(b.DeviceName_str), String(b.HubNameLB_str));
                  await load();
                }, "Bridge deleted.")}>
                  <IconTrash size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {adding && <BridgeSheet onClose={() => setAdding(false)} onSaved={() => { setAdding(false); void load(); }} />}
    </section>
  );
}

function BridgeSheet({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [devices, setDevices] = useState<Wire[] | null>(null);
  const [hubs, setHubs] = useState<string[]>([]);
  const [device, setDevice] = useState("");
  const [hub, setHub] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { push } = useToast();

  useEffect(() => {
    void api.bridgeDevices().then((r) => setDevices((r.EthList as Wire[]) ?? [])).catch(() => setDevices([]));
    void api.hubs().then((r) => setHubs(((r.HubList as Wire[]) ?? []).map((h) => String(h.HubName_str)))).catch(() => {});
  }, []);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.addBridge(device, hub);
      push("ok", "Bridge created.");
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <Sheet
      title="New local bridge"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={save} disabled={busy || !device || !hub}>
            {busy && <span className="spin" />} Create bridge
          </button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <Field label="Network adapter">
        {devices === null ? (
          <LoadingBlock />
        ) : (
          <select className="select" value={device} onChange={(e) => setDevice(e.target.value)}>
            <option value="">— choose —</option>
            {devices.map((d) => (
              <option key={String(d.DeviceName_str)} value={String(d.DeviceName_str)}>
                {String(d.DeviceName_str)}{d.NetworkConnectionName_utf ? ` (${d.NetworkConnectionName_utf})` : ""}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="Virtual Hub">
        <select className="select" value={hub} onChange={(e) => setHub(e.target.value)}>
          <option value="">— choose —</option>
          {hubs.map((h) => <option key={h} value={h}>{h}</option>)}
        </select>
      </Field>
      <div className="hint">
        Bridging the adapter the server itself uses can drop its own connectivity on some NICs;
        a dedicated adapter is the safe choice.
      </div>
    </Sheet>
  );
}

/* ── L3 switches ──────────────────────────────────────────────────────────── */

function L3Card() {
  const [switches, setSwitches] = useState<Wire[] | null>(null);
  const [name, setName] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.l3().catch(() => null);
    if (r) setSwitches((r.L3SWList as Wire[]) ?? []);
  }, []);
  usePoll(load, "list", []);

  return (
    <section id="ss-l3">
      <SectionTitle count={switches?.length}>Virtual layer-3 switches</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)" }}>
        <div className="lede" style={{ marginBottom: "var(--s3)" }}>
          An IP router between Virtual Hubs: each interface sits in one hub with one address, and
          the routing table moves packets between them.
        </div>
        {switches === null ? (
          <LoadingBlock />
        ) : (
          <>
            {switches.map((s) => (
              <div key={String(s.Name_str)} className="lrow">
                <span className="mono" style={{ fontWeight: 600 }}>{String(s.Name_str)}</span>
                <Pill kind={s.Active_bool ? "ok" : "idle"} label={s.Active_bool ? "running" : "stopped"} />
                <span className="micro">{String(s.NumInterfaces_u32 ?? 0)} if · {String(s.NumTables_u32 ?? 0)} routes</span>
                <span style={{ flex: 1 }} />
                <button className="btn btn--sm" onClick={() => setOpen(String(s.Name_str))}>Configure</button>
                <button className="btn btn--sm" onClick={() => void guard(async () => {
                  await (s.Active_bool ? api.stopL3(String(s.Name_str)) : api.startL3(String(s.Name_str)));
                  await load();
                })}>
                  {s.Active_bool ? "Stop" : "Start"}
                </button>
                <button className="btn btn--sm btn--ghost" aria-label="Delete" onClick={() => void guard(async () => {
                  await api.delL3(String(s.Name_str));
                  await load();
                }, "Switch deleted.")}>
                  <IconTrash size={14} />
                </button>
              </div>
            ))}
            <div style={{ display: "flex", gap: "var(--s2)", marginTop: "var(--s3)" }}>
              <input className="input mono" placeholder="switch name" value={name} onChange={(e) => setName(e.target.value)} style={{ maxWidth: 240 }} autoCapitalize="none" spellCheck={false} />
              <button className="btn" disabled={!name.trim()} onClick={() => void guard(async () => {
                await api.addL3(name.trim());
                setName("");
                await load();
              }, "Switch created.")}>
                <IconPlus size={14} /> Add
              </button>
            </div>
          </>
        )}
      </div>
      {open && <L3Sheet name={open} onClose={() => { setOpen(null); void load(); }} />}
    </section>
  );
}

function L3Sheet({ name, onClose }: { name: string; onClose: () => void }) {
  const [ifs, setIfs] = useState<Wire[] | null>(null);
  const [routes, setRoutes] = useState<Wire[] | null>(null);
  const [hubs, setHubs] = useState<string[]>([]);
  const [ifDraft, setIfDraft] = useState({ hub: "", ip: "", mask: "255.255.255.0" });
  const [routeDraft, setRouteDraft] = useState({ net: "", mask: "255.255.255.0", gw: "", metric: 1 });
  const { guard } = useToast();

  const load = useCallback(async () => {
    const [i, r, h] = await Promise.all([
      api.l3Ifs(name).catch(() => null),
      api.l3Routes(name).catch(() => null),
      api.hubs().catch(() => null),
    ]);
    if (i) setIfs((i.L3IFList as Wire[]) ?? []);
    if (r) setRoutes((r.L3Table as Wire[]) ?? []);
    if (h) setHubs(((h.HubList as Wire[]) ?? []).map((x) => String(x.HubName_str)));
  }, [name]);
  useEffect(() => void load(), [load]);

  return (
    <Sheet title={`Layer-3 switch ${name}`} subtitle="Stop the switch before changing it; SoftEther refuses edits while it runs." onClose={onClose} wide>
      <SectionTitle count={ifs?.length ?? undefined}>Interfaces</SectionTitle>
      {(ifs ?? []).map((i, index) => (
        <div key={index} className="lrow">
          <span className="mono">{String(i.HubName_str)}</span>
          <span className="mono">{String(i.IpAddress_ip)}/{String(i.SubnetMask_ip)}</span>
          <span style={{ flex: 1 }} />
          <button className="btn btn--sm btn--ghost" aria-label="Delete" onClick={() => void guard(async () => {
            await api.delL3If(name, { HubName_str: i.HubName_str });
            await load();
          })}>
            <IconTrash size={14} />
          </button>
        </div>
      ))}
      <div className="row2" style={{ marginTop: "var(--s2)" }}>
        <select className="select" value={ifDraft.hub} onChange={(e) => setIfDraft({ ...ifDraft, hub: e.target.value })}>
          <option value="">— hub —</option>
          {hubs.map((h) => <option key={h} value={h}>{h}</option>)}
        </select>
        <div style={{ display: "flex", gap: "var(--s2)" }}>
          <input className="input mono" placeholder="192.168.1.1" value={ifDraft.ip} onChange={(e) => setIfDraft({ ...ifDraft, ip: e.target.value })} spellCheck={false} />
          <input className="input mono" placeholder="mask" value={ifDraft.mask} onChange={(e) => setIfDraft({ ...ifDraft, mask: e.target.value })} spellCheck={false} />
        </div>
      </div>
      <button className="btn btn--sm" style={{ marginTop: "var(--s2)" }} disabled={!ifDraft.hub || !ifDraft.ip} onClick={() => void guard(async () => {
        await api.addL3If(name, { HubName_str: ifDraft.hub, IpAddress_ip: ifDraft.ip, SubnetMask_ip: ifDraft.mask });
        setIfDraft({ hub: "", ip: "", mask: "255.255.255.0" });
        await load();
      }, "Interface added.")}>
        <IconPlus size={14} /> Add interface
      </button>

      <SectionTitle count={routes?.length ?? undefined}>Routing table</SectionTitle>
      {(routes ?? []).map((r, index) => (
        <div key={index} className="lrow">
          <span className="mono">{String(r.NetworkAddress_ip)}/{String(r.SubnetMask_ip)}</span>
          <span className="micro">via</span>
          <span className="mono">{String(r.GatewayAddress_ip)}</span>
          <span className="micro">metric {String(r.Metric_u32)}</span>
          <span style={{ flex: 1 }} />
          <button className="btn btn--sm btn--ghost" aria-label="Delete" onClick={() => void guard(async () => {
            await api.delL3Route(name, {
              NetworkAddress_ip: r.NetworkAddress_ip, SubnetMask_ip: r.SubnetMask_ip,
              GatewayAddress_ip: r.GatewayAddress_ip, Metric_u32: r.Metric_u32,
            });
            await load();
          })}>
            <IconTrash size={14} />
          </button>
        </div>
      ))}
      <div className="row2" style={{ marginTop: "var(--s2)" }}>
        <div style={{ display: "flex", gap: "var(--s2)" }}>
          <input className="input mono" placeholder="10.0.0.0" value={routeDraft.net} onChange={(e) => setRouteDraft({ ...routeDraft, net: e.target.value })} spellCheck={false} />
          <input className="input mono" placeholder="mask" value={routeDraft.mask} onChange={(e) => setRouteDraft({ ...routeDraft, mask: e.target.value })} spellCheck={false} />
        </div>
        <div style={{ display: "flex", gap: "var(--s2)" }}>
          <input className="input mono" placeholder="gateway" value={routeDraft.gw} onChange={(e) => setRouteDraft({ ...routeDraft, gw: e.target.value })} spellCheck={false} />
          <input className="input mono" type="number" min={1} style={{ maxWidth: 90 }} value={routeDraft.metric} onChange={(e) => setRouteDraft({ ...routeDraft, metric: Number(e.target.value) })} />
        </div>
      </div>
      <button className="btn btn--sm" style={{ marginTop: "var(--s2)" }} disabled={!routeDraft.net || !routeDraft.gw} onClick={() => void guard(async () => {
        await api.addL3Route(name, {
          NetworkAddress_ip: routeDraft.net, SubnetMask_ip: routeDraft.mask,
          GatewayAddress_ip: routeDraft.gw, Metric_u32: routeDraft.metric,
        });
        setRouteDraft({ net: "", mask: "255.255.255.0", gw: "", metric: 1 });
        await load();
      }, "Route added.")}>
        <IconPlus size={14} /> Add route
      </button>
    </Sheet>
  );
}

/* ── EtherIP ids ──────────────────────────────────────────────────────────── */

function EtherIpCard() {
  const [ids, setIds] = useState<Wire[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [hubs, setHubs] = useState<string[]>([]);
  const [draft, setDraft] = useState({ id: "", hub: "", user: "", password: "" });
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.etherip().catch(() => null);
    if (r) setIds((r.Settings as Wire[]) ?? []);
  }, []);
  usePoll(load, "list", []);

  useEffect(() => {
    void api.hubs().then((r) => setHubs(((r.HubList as Wire[]) ?? []).map((h) => String(h.HubName_str)))).catch(() => {});
  }, []);

  return (
    <section id="ss-etherip">
      <SectionTitle
        count={ids?.length}
        actions={
          <button className="btn btn--sm" onClick={() => setAdding(true)}>
            <IconPlus size={14} /> Add client ID
          </button>
        }
      >
        EtherIP / L2TPv3 client IDs
      </SectionTitle>
      {ids === null ? (
        <LoadingBlock />
      ) : ids.length === 0 ? (
        <Empty title="no client IDs">
          Router-to-router EtherIP/L2TPv3 over IPsec needs each device's ISAKMP ID mapped to a hub
          and a user here.
        </Empty>
      ) : (
        <div className="rows">
          {ids.map((entry) => (
            <div key={String(entry.Id_str)} className="row">
              <div className="row__main">
                <div className="row__name mono">{String(entry.Id_str)}</div>
                <div className="spec">
                  <span className="chip"><i>hub</i>{String(entry.HubName_str ?? "?")}</span>
                  <span className="chip"><i>user</i>{String(entry.UserName_str ?? "?")}</span>
                </div>
              </div>
              <div className="row__side">
                <button className="btn btn--sm btn--ghost" aria-label="Delete" onClick={() => void guard(async () => {
                  await api.deleteEtherip(String(entry.Id_str));
                  await load();
                }, "Client ID removed.")}>
                  <IconTrash size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {adding && (
        <Sheet
          title="EtherIP client ID"
          onClose={() => setAdding(false)}
          footer={
            <>
              <button className="btn" onClick={() => setAdding(false)}>Cancel</button>
              <button className="btn btn--primary" disabled={!draft.id || !draft.hub || !draft.user} onClick={() => void guard(async () => {
                await api.addEtherip({
                  Id_str: draft.id.trim(), HubName_str: draft.hub, UserName_str: draft.user.trim(), Password_str: draft.password,
                });
                setAdding(false);
                setDraft({ id: "", hub: "", user: "", password: "" });
                await load();
              }, "Client ID added.")}>
                Add
              </button>
            </>
          }
        >
          <Field label="ISAKMP phase-1 ID" hint="What the device announces, e.g. its hostname or IP.">
            <input className="input mono" value={draft.id} onChange={(e) => setDraft({ ...draft, id: e.target.value })} spellCheck={false} autoCapitalize="none" />
          </Field>
          <Field label="Virtual Hub">
            <select className="select" value={draft.hub} onChange={(e) => setDraft({ ...draft, hub: e.target.value })}>
              <option value="">— choose —</option>
              {hubs.map((h) => <option key={h} value={h}>{h}</option>)}
            </select>
          </Field>
          <div className="row2">
            <Field label="Username">
              <input className="input mono" value={draft.user} onChange={(e) => setDraft({ ...draft, user: e.target.value })} spellCheck={false} autoCapitalize="none" />
            </Field>
            <Field label="Password">
              <input className="input" type="password" value={draft.password} onChange={(e) => setDraft({ ...draft, password: e.target.value })} autoComplete="off" />
            </Field>
          </div>
        </Sheet>
      )}
    </section>
  );
}

/* ── clustering ───────────────────────────────────────────────────────────── */

function FarmCard() {
  const [farm, setFarm] = useState<Wire | null>(null);
  const [members, setMembers] = useState<Wire[] | null>(null);

  const load = useCallback(async () => {
    setFarm(await api.farm().catch(() => null));
    const m = await api.farmMembers().catch(() => null);
    if (m) setMembers((m.FarmMemberList as Wire[]) ?? []);
  }, []);
  usePoll(load, "list", []);

  const TYPES: Record<number, string> = { 0: "standalone", 1: "farm controller", 2: "farm member" };

  return (
    <section id="ss-farm">
      <SectionTitle>Clustering</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)" }}>
        {!farm ? (
          <LoadingBlock />
        ) : (
          <>
            <KV rows={[
              ["role", TYPES[Number(farm.ServerType_u32)] ?? "?"],
              ["controller", Number(farm.ServerType_u32) === 2 ? `${farm.ControllerName_str}:${farm.ControllerPort_u32}` : "—"],
              ["public IP", String(farm.PublicIp_ip || "—")],
              ["weight", String(farm.Weight_u32 ?? "—")],
            ]} />
            {Number(farm.ServerType_u32) === 1 && members && (
              <>
                <div className="section__t" style={{ margin: "var(--s4) 0 var(--s2)" }}>Members</div>
                {members.map((m) => (
                  <div key={String(m.Id_u32)} className="lrow">
                    <Pill kind={m.Controller_bool ? "busy" : "ok"} label={m.Controller_bool ? "controller" : "member"} />
                    <span className="mono">{String(m.Hostname_str)}</span>
                    <span className="micro">{String(m.NumSessions_u32 ?? 0)} sessions · {String(m.NumHubs_u32 ?? 0)} hubs</span>
                  </div>
                ))}
              </>
            )}
            <div className="hint" style={{ marginTop: "var(--s3)" }}>
              Changing the clustering role restarts the VPN server and erases parts of its state;
              SoftEther means it as a provisioning-time decision. It is exposed in the API console
              (SetFarmSetting) rather than as a casual switch here.
            </div>
          </>
        )}
      </div>
    </section>
  );
}

/* ── keep-alive & syslog ──────────────────────────────────────────────────── */

function KeepSyslogCard() {
  const [keep, setKeep] = useState<Wire | null>(null);
  const [syslog, setSyslog] = useState<Wire | null>(null);
  const [dirty, setDirty] = useState<Record<string, boolean>>({});
  const { guard } = useToast();

  useEffect(() => {
    void api.keepalive().then(setKeep).catch(() => {});
    void api.syslog().then(setSyslog).catch(() => {});
  }, []);

  const SYSLOG_MODES = [
    "off",
    "server log only",
    "server + hub security logs",
    "server + hub security + packet logs",
  ];

  return (
    <section id="ss-keep">
      <SectionTitle>Keep-alive & syslog</SectionTitle>
      <div className="setgrid">
        <div className="card" style={{ padding: "var(--s4)" }}>
          <div className="section__t" style={{ marginBottom: "var(--s3)" }}>Internet keep-alive</div>
          {!keep ? (
            <LoadingBlock />
          ) : (
            <>
              <CheckRow checked={Boolean(keep.UseKeepConnect_bool)} onChange={(v) => { setKeep({ ...keep, UseKeepConnect_bool: v }); setDirty((d) => ({ ...d, keep: true })); }}
                label="Send keep-alive packets" hint="Stops an idle dial-up/NAT path from being torn down." />
              <div className="row2">
                <Field label="Host">
                  <input className="input mono" value={String(keep.KeepConnectHost_str ?? "")} onChange={(e) => { setKeep({ ...keep, KeepConnectHost_str: e.target.value }); setDirty((d) => ({ ...d, keep: true })); }} spellCheck={false} />
                </Field>
                <Field label="Port">
                  <input className="input mono" type="number" value={Number(keep.KeepConnectPort_u32) || 80} onChange={(e) => { setKeep({ ...keep, KeepConnectPort_u32: Number(e.target.value) }); setDirty((d) => ({ ...d, keep: true })); }} />
                </Field>
              </div>
              <div className="row2">
                <Field label="Protocol">
                  <select className="select" value={Number(keep.KeepConnectProtocol_u32) || 0} onChange={(e) => { setKeep({ ...keep, KeepConnectProtocol_u32: Number(e.target.value) }); setDirty((d) => ({ ...d, keep: true })); }}>
                    <option value={0}>TCP</option>
                    <option value={1}>UDP</option>
                  </select>
                </Field>
                <Field label="Interval (s)">
                  <input className="input mono" type="number" min={5} value={Number(keep.KeepConnectInterval_u32) || 50} onChange={(e) => { setKeep({ ...keep, KeepConnectInterval_u32: Number(e.target.value) }); setDirty((d) => ({ ...d, keep: true })); }} />
                </Field>
              </div>
              {dirty.keep && (
                <button className="btn btn--primary" onClick={() => void guard(async () => {
                  await api.setKeepalive({
                    UseKeepConnect_bool: Boolean(keep.UseKeepConnect_bool),
                    KeepConnectHost_str: String(keep.KeepConnectHost_str ?? ""),
                    KeepConnectPort_u32: Number(keep.KeepConnectPort_u32) || 80,
                    KeepConnectProtocol_u32: Number(keep.KeepConnectProtocol_u32) || 0,
                    KeepConnectInterval_u32: Number(keep.KeepConnectInterval_u32) || 50,
                  });
                  setDirty((d) => ({ ...d, keep: false }));
                }, "Keep-alive saved.")}>
                  Save keep-alive
                </button>
              )}
            </>
          )}
        </div>

        <div className="card" style={{ padding: "var(--s4)" }}>
          <div className="section__t" style={{ marginBottom: "var(--s3)" }}>Syslog forwarding</div>
          {!syslog ? (
            <LoadingBlock />
          ) : (
            <>
              <Field label="Mode">
                <select className="select" value={Number(syslog.SaveType_u32) || 0} onChange={(e) => { setSyslog({ ...syslog, SaveType_u32: Number(e.target.value) }); setDirty((d) => ({ ...d, syslog: true })); }}>
                  {SYSLOG_MODES.map((m, i) => <option key={i} value={i}>{m}</option>)}
                </select>
              </Field>
              <div className="row2">
                <Field label="Syslog host">
                  <input className="input mono" value={String(syslog.Hostname_str ?? "")} onChange={(e) => { setSyslog({ ...syslog, Hostname_str: e.target.value }); setDirty((d) => ({ ...d, syslog: true })); }} spellCheck={false} />
                </Field>
                <Field label="Port">
                  <input className="input mono" type="number" value={Number(syslog.Port_u32) || 514} onChange={(e) => { setSyslog({ ...syslog, Port_u32: Number(e.target.value) }); setDirty((d) => ({ ...d, syslog: true })); }} />
                </Field>
              </div>
              {dirty.syslog && (
                <button className="btn btn--primary" onClick={() => void guard(async () => {
                  await api.setSyslog({
                    SaveType_u32: Number(syslog.SaveType_u32) || 0,
                    Hostname_str: String(syslog.Hostname_str ?? ""),
                    Port_u32: Number(syslog.Port_u32) || 514,
                  });
                  setDirty((d) => ({ ...d, syslog: false }));
                }, "Syslog settings saved.")}>
                  Save syslog
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

/* ── configuration file ───────────────────────────────────────────────────── */

function ConfigCard() {
  const [restoring, setRestoring] = useState<File | null>(null);
  const { guard } = useToast();

  return (
    <section id="ss-config">
      <SectionTitle>Configuration file</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)" }}>
        <div className="lede" style={{ marginBottom: "var(--s3)" }}>
          The server's entire state, as the vpn_server.config it keeps on disk. Download it as a
          backup; restoring replaces everything and restarts the service.
        </div>
        <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap" }}>
          <button className="btn" onClick={() => void guard(async () => {
            const r = await api.getConfig();
            downloadBase64(r.filename || "vpn_server.config", r.config_base64, "text/plain");
          }, "Configuration downloaded.")}>
            <IconDownload size={14} /> Download backup
          </button>
          <label className="btn">
            <IconUpload size={14} /> Restore from file
            <input type="file" hidden onChange={(e) => setRestoring(e.target.files?.[0] ?? null)} />
          </label>
          <button className="btn" onClick={() => void guard(() => api.flush(), "Volatile state written to disk.")}>
            Flush to disk
          </button>
        </div>
      </div>
      {restoring && (
        <ConfirmSheet
          title="Restore this configuration?"
          verb="Restore & restart"
          typed="restore"
          body={
            <>
              <b>{restoring.name}</b> replaces the entire configuration of this VPN server — hubs,
              users, everything — and the server restarts to load it.
            </>
          }
          onClose={() => setRestoring(null)}
          onConfirm={async () => {
            const b64 = await fileToBase64(restoring);
            await api.setConfig(b64);
          }}
        />
      )}
    </section>
  );
}

/* ── the danger zone ──────────────────────────────────────────────────────── */

function DangerZone() {
  const [confirm, setConfirm] = useState<null | "reboot" | "crash">(null);
  return (
    <section id="ss-danger" className="danger">
      <SectionTitle>Danger zone</SectionTitle>
      <div style={{ display: "flex", gap: "var(--s2)", flexWrap: "wrap" }}>
        <button className="btn" onClick={() => setConfirm("reboot")}>Restart VPN service</button>
        <button className="btn" onClick={() => setConfirm("crash")}>Force-crash process</button>
      </div>
      {confirm === "reboot" && (
        <ConfirmSheet
          title="Restart the VPN server service?"
          verb="Restart"
          body={<>Every session drops and reconnects. Takes a few seconds.</>}
          onClose={() => setConfirm(null)}
          onConfirm={() => api.reboot().then(() => undefined)}
        />
      )}
      {confirm === "crash" && (
        <ConfirmSheet
          title="Force-crash the VPN server?"
          verb="Crash it"
          typed="crash"
          body={
            <>
              The API's most violent call: the process aborts itself without saving anything.
              Only for a server too wedged to answer a normal restart. If it runs under systemd
              it will come back on its own.
            </>
          }
          onClose={() => setConfirm(null)}
          onConfirm={() => api.crashServer().then(() => undefined)}
        />
      )}
    </section>
  );
}
