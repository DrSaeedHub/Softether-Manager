"use client";

import { useCallback, useState } from "react";
import { ConfirmSheet, Empty, ErrorAlert, Field, KV, LoadingBlock, SectionTitle, usePoll } from "../../components/bits";
import { api, type Wire } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { downloadBase64, fileToBase64, formatDate } from "../../lib/util";
import { IconDownload, IconPlus, IconShield, IconTrash } from "../../ui/Icon";
import { Sheet } from "../../ui/Sheet";

/**
 * The hub's trust: CA certificates it accepts, certificates it has revoked,
 * and the RADIUS server it can delegate authentication to.
 */
export function HubSecurity({ hub }: { hub: string }) {
  return (
    <>
      <CaSection hub={hub} />
      <CrlSection hub={hub} />
      <RadiusSection hub={hub} />
    </>
  );
}

/* ── trusted CAs ──────────────────────────────────────────────────────────── */

function CaSection({ hub }: { hub: string }) {
  const [cas, setCas] = useState<Wire[] | null>(null);
  const [deleting, setDeleting] = useState<Wire | null>(null);
  const { guard, push } = useToast();

  const load = useCallback(async () => {
    const r = await api.cas(hub).catch(() => null);
    if (r) setCas((r.CAList as Wire[]) ?? []);
  }, [hub]);
  usePoll(load, "list", [hub]);

  const upload = async (file: File) => {
    const b64 = await fileToBase64(file);
    await guard(() => api.addCa(hub, b64), "CA certificate added.");
    void load();
  };

  const download = (key: number) =>
    guard(async () => {
      const r = await api.getCa(hub, key);
      downloadBase64(`ca-${key}.cer`, String(r.Cert_bin ?? ""), "application/pkix-cert");
    });

  return (
    <>
      <SectionTitle
        count={cas?.length}
        actions={
          <label className="btn btn--sm">
            <IconPlus size={14} /> Add CA
            <input type="file" accept=".cer,.crt,.pem,.der" hidden onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
          </label>
        }
      >
        Trusted CA certificates
      </SectionTitle>
      {cas === null ? (
        <LoadingBlock />
      ) : cas.length === 0 ? (
        <Empty title="no trusted CAs">
          Users authenticating with a signed certificate need the signing CA registered here.
        </Empty>
      ) : (
        <div className="rows">
          {cas.map((c) => (
            <div key={String(c.Key_u32)} className="row">
              <div className="row__main">
                <div className="row__name">
                  <IconShield size={15} />
                  <span className="truncate">{String(c.SubjectName_utf || "certificate")}</span>
                </div>
                <div className="row__note">
                  issued by {String(c.IssuerName_utf || "?")} · expires {formatDate(c.Expires_dt as string)}
                </div>
              </div>
              <div className="row__side">
                <button className="btn btn--sm btn--ghost" onClick={() => void download(Number(c.Key_u32))} aria-label="Download">
                  <IconDownload size={14} />
                </button>
                <button className="btn btn--sm btn--ghost" onClick={() => setDeleting(c)} aria-label="Delete">
                  <IconTrash size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {deleting && (
        <ConfirmSheet
          title="Remove this CA?"
          verb="Remove"
          body={<>Users whose certificates chain to <b>{String(deleting.SubjectName_utf || "this CA")}</b> will stop authenticating.</>}
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await api.deleteCa(hub, Number(deleting.Key_u32));
            void load();
          }}
        />
      )}
    </>
  );
}

/* ── the revocation list ──────────────────────────────────────────────────── */

function CrlSection({ hub }: { hub: string }) {
  const [crls, setCrls] = useState<Wire[] | null>(null);
  const [adding, setAdding] = useState(false);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.crls(hub).catch(() => null);
    if (r) setCrls((r.CRLList as Wire[]) ?? []);
  }, [hub]);
  usePoll(load, "list", [hub]);

  return (
    <>
      <SectionTitle
        count={crls?.length}
        actions={
          <button className="btn btn--sm" onClick={() => setAdding(true)}>
            <IconPlus size={14} /> Revoke a certificate
          </button>
        }
      >
        Revoked certificates
      </SectionTitle>
      {crls === null ? (
        <LoadingBlock />
      ) : crls.length === 0 ? (
        <Empty title="nothing revoked">
          A revocation entry refuses one certificate -- by common name, serial or digest -- even
          while its CA stays trusted. The lost-laptop switch.
        </Empty>
      ) : (
        <div className="rows">
          {crls.map((c) => (
            <div key={String(c.Key_u32)} className="row">
              <div className="row__main">
                <div className="row__name mono truncate">{String(c.CrlInfo_utf || `entry ${c.Key_u32}`)}</div>
              </div>
              <div className="row__side">
                <button
                  className="btn btn--sm btn--ghost"
                  onClick={() => void guard(async () => {
                    await api.deleteCrl(hub, Number(c.Key_u32));
                    await load();
                  }, "Revocation removed.")}
                  aria-label="Delete"
                >
                  <IconTrash size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {adding && (
        <CrlSheet
          onClose={() => setAdding(false)}
          onSave={async (body) => {
            await api.addCrl(hub, body);
            setAdding(false);
            void load();
          }}
        />
      )}
    </>
  );
}

function CrlSheet({ onClose, onSave }: { onClose: () => void; onSave: (b: Wire) => Promise<void> }) {
  const [cn, setCn] = useState("");
  const [serial, setSerial] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const body: Wire = {};
      if (cn.trim()) body.CommonName_utf = cn.trim();
      if (serial.trim()) body.Serial_bin = hexToBase64(serial);
      await onSave(body);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <Sheet
      title="Revoke a certificate"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--danger" onClick={save} disabled={busy || (!cn.trim() && !serial.trim())}>
            {busy && <span className="spin" />} Revoke
          </button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <div className="lede" style={{ marginBottom: "var(--s3)" }}>
        A certificate matching <b>every</b> field you fill is refused. One precise field -- the
        serial -- is usually enough.
      </div>
      <Field label="Common name (CN)">
        <input className="input mono" value={cn} onChange={(e) => setCn(e.target.value)} spellCheck={false} />
      </Field>
      <Field label="Serial number" hint="Hex, with or without colons.">
        <input className="input mono" value={serial} onChange={(e) => setSerial(e.target.value)} spellCheck={false} placeholder="0a:1b:2c…" />
      </Field>
    </Sheet>
  );
}

function hexToBase64(hex: string): string {
  const clean = hex.replace(/[^0-9a-fA-F]/g, "");
  const bytes = clean.match(/.{1,2}/g)?.map((b) => parseInt(b, 16)) ?? [];
  return btoa(String.fromCharCode(...bytes));
}

/* ── RADIUS ───────────────────────────────────────────────────────────────── */

function RadiusSection({ hub }: { hub: string }) {
  const [config, setConfig] = useState<Wire | null>(null);
  const [secret, setSecret] = useState("");
  const [dirty, setDirty] = useState(false);
  const { guard } = useToast();

  const load = useCallback(async () => {
    const r = await api.radius(hub).catch(() => null);
    if (r) setConfig((c) => (dirty ? c : r));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hub, dirty]);
  usePoll(load, 3600_000, [hub]);

  if (!config) return null;
  const set = (k: string, v: unknown) => {
    setConfig((c) => ({ ...(c ?? {}), [k]: v }));
    setDirty(true);
  };

  const save = () =>
    guard(async () => {
      const body: Wire = {
        RadiusServerName_str: config.RadiusServerName_str ?? "",
        RadiusPort_u32: Number(config.RadiusPort_u32) || 1812,
        RadiusRetryInterval_u32: Number(config.RadiusRetryInterval_u32) || 500,
      };
      if (secret) body.RadiusSecret_str = secret;
      await api.setRadius(hub, body);
      setDirty(false);
      setSecret("");
    }, "RADIUS settings saved.");

  return (
    <>
      <SectionTitle>RADIUS authentication</SectionTitle>
      <div className="card" style={{ padding: "var(--s4)", maxWidth: 640 }}>
        <div className="lede" style={{ marginBottom: "var(--s3)" }}>
          Users set to RADIUS authentication are verified against this server. Empty hostname
          turns it off.
        </div>
        <div className="row2">
          <Field label="Server">
            <input className="input mono" value={String(config.RadiusServerName_str ?? "")} onChange={(e) => set("RadiusServerName_str", e.target.value)} spellCheck={false} placeholder="radius.example.net" />
          </Field>
          <Field label="Port">
            <input className="input mono" type="number" min={1} max={65535} value={Number(config.RadiusPort_u32) || 1812} onChange={(e) => set("RadiusPort_u32", Number(e.target.value))} />
          </Field>
        </div>
        <div className="row2">
          <Field label="Shared secret" hint="Only sent when you type a new one.">
            <input className="input" type="password" value={secret} onChange={(e) => { setSecret(e.target.value); setDirty(true); }} autoComplete="off" />
          </Field>
          <Field label="Retry interval (ms)">
            <input className="input mono" type="number" min={100} value={Number(config.RadiusRetryInterval_u32) || 500} onChange={(e) => set("RadiusRetryInterval_u32", Number(e.target.value))} />
          </Field>
        </div>
        {dirty && (
          <button className="btn btn--primary" onClick={() => void save()}>
            Save RADIUS settings
          </button>
        )}
      </div>
    </>
  );
}
