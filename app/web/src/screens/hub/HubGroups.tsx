"use client";

import { useCallback, useState } from "react";
import { ConfirmSheet, Empty, ErrorAlert, Field, LoadingBlock, SectionTitle, usePoll } from "../../components/bits";
import { PolicyEditor, extractPolicy } from "../../components/PolicyEditor";
import { api, type Wire } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { formatCount } from "../../lib/util";
import { IconPlus, IconUsers } from "../../ui/Icon";
import { Sheet } from "../../ui/Sheet";

/**
 * Groups: a name, and a policy its members inherit. Users point at groups
 * from their own page; here the group itself is managed.
 */
export function HubGroups({ hub }: { hub: string }) {
  const [groups, setGroups] = useState<Wire[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await api.groups(hub).catch(() => null);
    if (r) setGroups((r.GroupList as Wire[]) ?? []);
  }, [hub]);
  usePoll(load, "list", [hub]);

  return (
    <>
      <SectionTitle
        count={groups?.length}
        actions={
          <button className="btn btn--primary btn--sm" onClick={() => setCreating(true)}>
            <IconPlus size={14} /> New group
          </button>
        }
      >
        Groups
      </SectionTitle>

      {groups === null ? (
        <LoadingBlock label="loading groups" />
      ) : groups.length === 0 ? (
        <Empty
          title="no groups"
          action={
            <button className="btn btn--primary" onClick={() => setCreating(true)}>
              <IconPlus size={15} /> Create a group
            </button>
          }
        >
          A group carries one security policy for many users: bandwidth tiers, an access cut-off
          for a whole team, one switch instead of fifty.
        </Empty>
      ) : (
        <div className="grid hubgrid">
          {groups.map((g) => (
            <button key={String(g.Name_str)} className="card hubtile" onClick={() => setEditing(String(g.Name_str))}>
              <div className="hubtile__head">
                <span className="hubtile__icon"><IconUsers size={17} /></span>
                <span className="hubtile__name truncate mono">{String(g.Name_str)}</span>
              </div>
              {g.Realname_utf ? <div className="micro truncate">{String(g.Realname_utf)}</div> : null}
              <div className="hubtile__stats">
                <span className="stat"><span className="stat__n">{formatCount(Number(g.NumUsers_u32))}</span><span className="micro">members</span></span>
              </div>
            </button>
          ))}
        </div>
      )}

      {(creating || editing) && (
        <GroupSheet
         
          hub={hub}
          name={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            setCreating(false);
            setEditing(null);
            void load();
          }}
          onDelete={(n) => {
            setEditing(null);
            setDeleting(n);
          }}
        />
      )}
      {deleting && (
        <ConfirmSheet
          title={`Delete group ${deleting}?`}
          verb="Delete group"
          body={<>Members are not deleted; they simply stop belonging to any group.</>}
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await api.deleteGroup(hub, deleting);
            void load();
          }}
        />
      )}
    </>
  );
}

function GroupSheet({ hub,
  name,
  onClose,
  onSaved,
  onDelete,
}: {
    hub: string;
  name: string | null;
  onClose: () => void;
  onSaved: () => void;
  onDelete: (name: string) => void;
}) {
  const editing = Boolean(name);
  const [groupName, setGroupName] = useState(name ?? "");
  const [realname, setRealname] = useState("");
  const [note, setNote] = useState("");
  const [policy, setPolicy] = useState<Wire>({ UsePolicy_bool: false });
  const [loaded, setLoaded] = useState(!editing);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  usePoll(
    async () => {
      if (!editing || loaded) return;
      const g = await api.group(hub, name as string).catch(() => null);
      if (g) {
        setRealname(String(g.Realname_utf ?? ""));
        setNote(String(g.Note_utf ?? ""));
        setPolicy(extractPolicy(g));
        setLoaded(true);
      }
    },
    3600_000,
    [name],
  );

  const save = async () => {
    setError(null);
    setBusy(true);
    try {
      const body: Wire = {
        Realname_utf: realname,
        Note_utf: note,
        ...policy,
      };
      if (editing) {
        await api.setGroup(hub, name as string, body);
      } else {
        body.Name_str = groupName.trim();
        await api.createGroup(hub, body);
      }
      push("ok", editing ? "Group saved." : `Group ${groupName.trim()} created.`);
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      title={editing ? `Group ${name}` : "New group"}
      subtitle={`hub ${hub}`}
      onClose={onClose}
      wide
      footer={
        <>
          {editing && (
            <button className="btn btn--danger" onClick={() => onDelete(name as string)} style={{ marginInlineEnd: "auto" }}>
              Delete
            </button>
          )}
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={save} disabled={busy || (!editing && !groupName.trim()) || !loaded}>
            {busy && <span className="spin" />}
            {editing ? "Save" : "Create group"}
          </button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {!loaded ? (
        <LoadingBlock />
      ) : (
        <div style={{ display: "grid", gap: "var(--s1)" }}>
          {!editing && (
            <Field label="Group name">
              <input className="input mono" value={groupName} onChange={(e) => setGroupName(e.target.value)}
                autoFocus autoCapitalize="none" spellCheck={false} />
            </Field>
          )}
          <div className="row2">
            <Field label="Display name">
              <input className="input" value={realname} onChange={(e) => setRealname(e.target.value)} />
            </Field>
            <Field label="Note">
              <input className="input" value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
          </div>
          <PolicyEditor value={policy} onChange={setPolicy} subject="group" />
        </div>
      )}
    </Sheet>
  );
}
