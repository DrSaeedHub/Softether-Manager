"use client";

import { useEffect, useMemo, useState } from "react";
import { Empty, ErrorAlert, LoadingBlock, PageHead, SearchBox } from "../components/bits";
import { api, type Wire } from "../lib/api";
import { useToast } from "../lib/toast";
import { IconPlay, IconTerminal } from "../ui/Icon";

/**
 * The API console: all 135 documented RPC methods, callable raw.
 *
 * The REST screens cover everything in a shaped way; this is the unshaped
 * way, for the operator following the SoftEther reference -- and the proof
 * that nothing the server can do is beyond the panel's reach.
 */
export function Console() {
  const [methods, setMethods] = useState<Wire[] | null>(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Wire | null>(null);
  const [params, setParams] = useState("{}");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  useEffect(() => {
    void api.rpcMethods().then(setMethods).catch(() => setMethods([]));
  }, []);

  const filtered = useMemo(() => {
    if (!methods) return null;
    const q = query.trim().toLowerCase();
    if (!q) return methods;
    return methods.filter(
      (m) => String(m.name).toLowerCase().includes(q) || String(m.title).toLowerCase().includes(q),
    );
  }, [methods, query]);

  const pick = (m: Wire) => {
    setSelected(m);
    setResult(null);
    setError(null);
    setParams(JSON.stringify(m.input ?? {}, null, 2));
  };

  const call = async () => {
    if (!selected) return;
    let parsed: Wire;
    try {
      parsed = params.trim() ? JSON.parse(params) : {};
    } catch {
      setError("The parameters are not valid JSON.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.rpcCall(String(selected.name), parsed);
      setResult(JSON.stringify(r, null, 2));
      push("ok", `${selected.name} answered.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <PageHead
        title="API console"
        sub="every documented RPC, raw"
      />
      <div className="console">
        <div className="console__list">
          {/* the box flexes horizontally; left in the column flow it would
              stretch its height instead */}
          <div style={{ display: "flex" }}>
            <SearchBox value={query} onChange={setQuery} placeholder="method name…" />
          </div>
          <div className="console__methods">
            {filtered === null ? (
              <LoadingBlock />
            ) : (
              filtered.map((m) => (
                <button
                  key={String(m.name)}
                  className={`console__m${selected?.name === m.name ? " on" : ""}`}
                  onClick={() => pick(m)}
                >
                  <span className="mono">{String(m.name)}</span>
                  <span className="micro truncate">{String(m.title)}</span>
                </button>
              ))
            )}
          </div>
        </div>

        <div className="console__work">
          {!selected ? (
            <Empty title="pick a method">
              The parameters template fills in with the documented example values; edit and run.
              Writes here are as real as anywhere else in the panel.
            </Empty>
          ) : (
            <>
              <div className="section__t" style={{ display: "flex", alignItems: "center", gap: "var(--s2)" }}>
                <IconTerminal size={16} />
                <span className="mono">{String(selected.name)}</span>
              </div>
              <p className="lede" style={{ margin: "var(--s2) 0 var(--s3)" }}>{String(selected.desc || selected.title)}</p>
              <div className="micro" style={{ marginBottom: 6 }}>parameters</div>
              <textarea
                className="textarea mono"
                rows={Math.min(16, Math.max(4, params.split("\n").length))}
                value={params}
                onChange={(e) => setParams(e.target.value)}
                spellCheck={false}
              />
              <div style={{ display: "flex", gap: "var(--s2)", margin: "var(--s3) 0" }}>
                <button className="btn btn--primary" onClick={call} disabled={busy}>
                  {busy ? <span className="spin" /> : <IconPlay size={14} />} Call {String(selected.name)}
                </button>
              </div>
              {error && <ErrorAlert>{error}</ErrorAlert>}
              {result && (
                <>
                  <div className="micro" style={{ margin: "var(--s2) 0 6px" }}>result</div>
                  <pre className="console__result mono">{result}</pre>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
