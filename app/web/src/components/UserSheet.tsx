"use client";

import { useEffect, useState } from "react";
import { api, type Wire } from "../lib/api";
import { AUTH_TYPE_OPTIONS, isNever } from "../lib/se";
import { useToast } from "../lib/toast";
import { fileToBase64 } from "../lib/util";
import { Sheet } from "../ui/Sheet";
import { ErrorAlert, Field } from "./bits";

/**
 * Create a user, or edit one's identity: name, group, authentication, expiry.
 * The security policy is deliberately NOT here -- it is a different decision
 * with forty knobs, and it lives on the user's own page.
 *
 * Editing merges into what the server already has (GetUser -> SetUser), so an
 * untouched field survives the round trip. The password is only ever sent
 * when a new one was typed.
 */
export function UserSheet({
  hub,
  hubChoices,
  onHubChange,
  existing,
  onClose,
  onSaved,
}: {
  hub: string;
  /** When creating from the all-users page: pick which hub the user lives in. */
  hubChoices?: string[];
  onHubChange?: (hub: string) => void;
  /** The GetUser payload when editing; absent when creating. */
  existing?: Wire;
  onClose: () => void;
  onSaved: (name: string) => void;
}) {
  const editing = Boolean(existing);
  const [name, setName] = useState(String(existing?.Name_str ?? ""));
  const [realname, setRealname] = useState(String(existing?.Realname_utf ?? ""));
  const [note, setNote] = useState(String(existing?.Note_utf ?? ""));
  const [group, setGroup] = useState(String(existing?.GroupName_str ?? ""));
  const [groups, setGroups] = useState<string[]>([]);
  const [authType, setAuthType] = useState<number>(Number(existing?.AuthType_u32 ?? 1));
  const [password, setPassword] = useState("");
  const [radiusUser, setRadiusUser] = useState(String(existing?.RadiusUsername_utf ?? ""));
  const [ntUser, setNtUser] = useState(String(existing?.NtUsername_utf ?? ""));
  const [commonName, setCommonName] = useState(String(existing?.CommonName_utf ?? ""));
  const [certBase64, setCertBase64] = useState("");
  const [expires, setExpires] = useState(() => {
    const raw = existing?.ExpireTime_dt as string | undefined;
    if (!raw || isNever(raw)) return "";
    const d = new Date(raw);
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  useEffect(() => {
    void api
      .groups(hub)
      .then((r) => setGroups(((r.GroupList as Wire[]) ?? []).map((g) => String(g.Name_str))))
      .catch(() => {});
  }, [hub]);

  const save = async () => {
    setError(null);
    setBusy(true);
    try {
      const body: Wire = {};
      if (editing) {
        // Round-trip the server's own record so unsent fields are not wiped.
        for (const [k, v] of Object.entries(existing ?? {})) {
          if (k.startsWith("Recv.") || k.startsWith("Send.") || k === "Auth_Password_str") continue;
          body[k] = v;
        }
      }
      body.Realname_utf = realname;
      body.Note_utf = note;
      body.AuthType_u32 = authType;
      body.ExpireTime_dt = expires ? new Date(expires).toISOString() : "1970-01-01T00:00:00.000";
      if (editing) body.GroupName_str = group;
      if (authType === 1 && password) body.Auth_Password_str = password;
      if (authType === 2 && certBase64) body.UserX_bin = certBase64;
      if (authType === 3) body.CommonName_utf = commonName;
      if (authType === 4) body.RadiusUsername_utf = radiusUser;
      if (authType === 5) body.NtUsername_utf = ntUser;

      const userName = editing ? String(existing?.Name_str) : name.trim();
      if (editing) {
        await api.setUser(hub, userName, body);
      } else {
        if (authType === 1 && !password) throw new Error("A password-authenticated user needs a password.");
        body.Name_str = userName;
        await api.createUser(hub, body);
        // CreateUser cannot assign a group; a follow-up SetUser does.
        if (group) {
          const fresh = await api.user(hub, userName);
          const merged: Wire = {};
          for (const [k, v] of Object.entries(fresh)) {
            if (k.startsWith("Recv.") || k.startsWith("Send.") || k === "Auth_Password_str") continue;
            merged[k] = v;
          }
          merged.GroupName_str = group;
          await api.setUser(hub, userName, merged);
        }
      }
      push("ok", editing ? "User updated." : `User ${userName} created.`);
      onSaved(userName);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      title={editing ? `Edit ${existing?.Name_str}` : "New user"}
      subtitle={`hub ${hub}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button
            className="btn btn--primary"
            onClick={save}
            disabled={busy || (!editing && !name.trim())}
          >
            {busy && <span className="spin" />}
            {editing ? "Save" : "Create user"}
          </button>
        </>
      }
    >
      <div style={{ display: "grid", gap: "var(--s1)" }}>
        {error && <ErrorAlert>{error}</ErrorAlert>}
        {!editing && hubChoices && hubChoices.length > 1 && onHubChange && (
          <Field label="Virtual Hub" hint="Where this user lives.">
            <select className="select" value={hub} onChange={(e) => onHubChange(e.target.value)}>
              {hubChoices.map((h) => (
                <option key={h} value={h}>{h}</option>
              ))}
            </select>
          </Field>
        )}
        {!editing && (
          <Field label="Username" hint="What the client authenticates as. Letters, digits, - _ . are safe.">
            <input className="input mono" value={name} onChange={(e) => setName(e.target.value)}
              autoFocus autoCapitalize="none" autoCorrect="off" spellCheck={false} />
          </Field>
        )}
        <div className="row2">
          <Field label="Full name">
            <input className="input" value={realname} onChange={(e) => setRealname(e.target.value)} />
          </Field>
          <Field label="Group" hint="Group members inherit the group's security policy.">
            <select className="select" value={group} onChange={(e) => setGroup(e.target.value)}>
              <option value="">— none —</option>
              {groups.map((g) => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Note">
          <input className="input" value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>

        <Field label="Authentication">
          <select className="select" value={authType} onChange={(e) => setAuthType(Number(e.target.value))}>
            {AUTH_TYPE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <div className="hint">{AUTH_TYPE_OPTIONS.find((o) => o.value === authType)?.hint}</div>
        </Field>

        {authType === 1 && (
          <Field label={editing ? "New password (leave empty to keep)" : "Password"}>
            <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </Field>
        )}
        {authType === 2 && (
          <Field label={editing && existing?.UserX_bin ? "Replace certificate (optional)" : "User certificate"} hint="X.509, DER or PEM.">
            <input
              className="input"
              type="file"
              accept=".cer,.crt,.pem,.der"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) setCertBase64(await fileToBase64(f));
              }}
            />
          </Field>
        )}
        {authType === 3 && (
          <Field label="Limit to common name" hint="Optional: only certificates carrying this CN. Empty accepts any trusted-signed certificate.">
            <input className="input mono" value={commonName} onChange={(e) => setCommonName(e.target.value)} spellCheck={false} />
          </Field>
        )}
        {authType === 4 && (
          <Field label="RADIUS username" hint="Empty uses the VPN username.">
            <input className="input mono" value={radiusUser} onChange={(e) => setRadiusUser(e.target.value)} spellCheck={false} autoCapitalize="none" />
          </Field>
        )}
        {authType === 5 && (
          <Field label="NT domain username" hint="Empty uses the VPN username.">
            <input className="input mono" value={ntUser} onChange={(e) => setNtUser(e.target.value)} spellCheck={false} autoCapitalize="none" />
          </Field>
        )}

        <Field label="Expires" hint="After this moment the user cannot connect. Empty means never.">
          <input className="input mono" type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </Field>
      </div>
    </Sheet>
  );
}
