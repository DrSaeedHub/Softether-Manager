"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Wire } from "../lib/api";
import { useToast } from "../lib/toast";
import { downloadText } from "../lib/util";
import { IconDownload } from "../ui/Icon";
import { Sheet } from "../ui/Sheet";
import { CheckRow, ErrorAlert, Field } from "./bits";

/**
 * Hand a user their connection: a ready-to-import SoftEther VPN Client
 * .vpn file.
 *
 * The address defaults to how *you* reached this panel -- the panel lives on
 * the VPN server, so that address usually is the server -- with the DDNS name
 * offered when the server has one. The names come from the templates in
 * Settings and stay editable here. The credential is embedded by default
 * (the panel holds SoftEther's own hash -- saved when the user was created
 * here, or recovered once from the server's configuration); typing the
 * password is only needed when neither source has it.
 */
export function VpnFileSheet({
  hub,
  name,
  onClose,
}: {
  hub: string;
  name: string;
  onClose: () => void;
}) {
  const [host, setHost] = useState(() => window.location.hostname);
  const [port, setPort] = useState<number>(443);
  const [ports, setPorts] = useState<number[]>([]);
  const [customPort, setCustomPort] = useState(false);
  const [ddnsFqdn, setDdnsFqdn] = useState("");
  const [embed, setEmbed] = useState(true);
  const [credential, setCredential] = useState<{ available: boolean } | null>(null);
  const [password, setPassword] = useState("");
  const [accountName, setAccountName] = useState("");
  const [filename, setFilename] = useState("");
  const [templates, setTemplates] = useState<{ account: string; file: string } | null>(null);
  const namesTouched = useRef({ account: false, file: false });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  useEffect(() => {
    void api
      .listeners()
      .then((r) => {
        const enabled = ((r.ListenerList as Wire[]) ?? [])
          .filter((l) => l.Enables_bool)
          .map((l) => Number(l.Ports_u32))
          .sort((a, b) => a - b);
        setPorts(enabled);
        if (enabled.length && !enabled.includes(443)) setPort(enabled[0]);
      })
      .catch(() => {});
    void api
      .ddns()
      .then((r) => setDdnsFqdn(String(r.CurrentFqdn_str || "")))
      .catch(() => {});
    void api
      .vpnTemplate()
      .then((r) => {
        setEmbed(Boolean(r.embed_password_default));
        setTemplates({
          account: String(r.account_name_template || "{hub} - {username}"),
          file: String(r.filename_template || "{hub}-{username}"),
        });
      })
      .catch(() => setTemplates({ account: "{hub} - {username}", file: "{hub}-{username}" }));
    void api
      .userCredentialState(hub, name)
      .then(setCredential)
      .catch(() => setCredential({ available: false }));
  }, [hub, name]);

  // The naming templates follow the live host/port until the operator types
  // their own name -- then their text wins.
  const render = (template: string) =>
    template
      .replaceAll("{hub}", hub)
      .replaceAll("{username}", name)
      .replaceAll("{host}", host.trim())
      .replaceAll("{port}", String(port));
  useEffect(() => {
    if (!templates) return;
    if (!namesTouched.current.account) setAccountName(render(templates.account));
    if (!namesTouched.current.file) setFilename(render(templates.file));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templates, host, port]);

  const portChoices = useMemo(() => (ports.length ? ports : [443, 992, 1194, 5555]), [ports]);
  const needsPassword = embed && credential !== null && !credential.available && !password;

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.userVpnFile(hub, name, {
        host: host.trim(),
        port,
        embed_password: embed,
        password: embed ? password || undefined : undefined,
        account_name: accountName.trim() || undefined,
        filename: filename.trim() || undefined,
      });
      downloadText(r.filename, r.content);
      push("ok", `${r.filename} downloaded.`);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      title="Download connection file"
      subtitle={`${name} · hub ${hub}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={download} disabled={busy || !host.trim() || !port}>
            {busy ? <span className="spin" /> : <IconDownload size={15} />}
            Download .vpn
          </button>
        </>
      }
    >
      <div style={{ display: "grid", gap: "var(--s1)" }}>
        {error && <ErrorAlert>{error}</ErrorAlert>}
        <div className="lede" style={{ marginBottom: "var(--s2)" }}>
          The file imports straight into SoftEther VPN Client — one double-click and the
          connection exists, pointed at this server and signed in as <b className="mono">{name}</b>.
        </div>
        <Field
          label="Server address"
          hint={
            ddnsFqdn ? (
              <>
                What the client will dial.{" "}
                <button className="linkish" onClick={() => setHost(ddnsFqdn)} type="button">
                  Use the DDNS name ({ddnsFqdn})
                </button>
              </>
            ) : (
              "What the client will dial — this machine's public address."
            )
          }
        >
          <input
            className="input mono"
            value={host}
            onChange={(e) => setHost(e.target.value)}
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            inputMode="url"
          />
        </Field>
        <Field label="Port" hint="Any listening SoftEther port; 443 crosses the most networks.">
          {customPort ? (
            <input
              className="input mono"
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(e) => setPort(Number(e.target.value))}
              inputMode="numeric"
            />
          ) : (
            <select
              className="select"
              value={String(port)}
              onChange={(e) => {
                if (e.target.value === "custom") setCustomPort(true);
                else setPort(Number(e.target.value));
              }}
            >
              {portChoices.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
              <option value="custom">other…</option>
            </select>
          )}
        </Field>
        <div className="row2">
          <Field label="File name">
            <input
              className="input mono"
              value={filename}
              onChange={(e) => {
                namesTouched.current.file = true;
                setFilename(e.target.value);
              }}
              spellCheck={false}
            />
          </Field>
          <Field label="Connection name in the client">
            <input
              className="input mono"
              value={accountName}
              onChange={(e) => {
                namesTouched.current.account = true;
                setAccountName(e.target.value);
              }}
              spellCheck={false}
            />
          </Field>
        </div>
        <CheckRow
          checked={embed}
          onChange={setEmbed}
          label="Embed the password in the file"
          hint={
            credential === null
              ? "Checking whether the panel holds this user's credential…"
              : credential.available
                ? "The panel holds this user's credential — it goes in as SoftEther's own hash, no typing needed. Anyone holding the file can connect."
                : "The panel has not seen this user's password and could not recover it from the server — type it once below and it will be remembered."
          }
        />
        {embed && (
          <Field
            label={credential?.available ? "Password (only to replace the stored one)" : "Password"}
            hint="Stored hashed, the way the client stores it — never in plain text."
          >
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="off"
            />
          </Field>
        )}
        {needsPassword && (
          <p className="hint hint--err">Without the password the download will be refused — or untick embedding to ship the file without a credential.</p>
        )}
      </div>
    </Sheet>
  );
}
