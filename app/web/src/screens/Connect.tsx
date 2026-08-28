"use client";

import { useEffect, useState } from "react";
import { ErrorAlert, Field, PageHead } from "../components/bits";
import { api } from "../lib/api";
import { navigate } from "../lib/router";
import { useServer } from "../lib/server";
import { useToast } from "../lib/toast";
import { IconCheck } from "../ui/Icon";

/**
 * Connecting the panel to the SoftEther instance on this machine: the
 * management port and the administrator password, tested before saved.
 * The host stays editable for the odd deployment where SoftEther listens
 * somewhere unusual, but 127.0.0.1 is the design.
 */
export function Connect() {
  const { refresh } = useServer();
  const { push } = useToast();
  const [host, setHost] = useState("127.0.0.1");
  const [port, setPort] = useState(5555);
  const [password, setPassword] = useState("");
  const [configured, setConfigured] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);

  useEffect(() => {
    void api
      .connection()
      .then((c) => {
        setHost(c.host);
        setPort(c.port);
        setConfigured(c.configured);
      })
      .catch(() => {});
  }, []);

  const test = async () => {
    setBusy("test");
    setError(null);
    setTestResult(null);
    try {
      const r = await api.testConnection({ host, port, password });
      setTestResult(`${r.version} answered on ${host}:${port}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    setBusy("save");
    setError(null);
    try {
      await api.saveConnection({
        host,
        port,
        password: password || (configured ? null : ""),
      });
      push("ok", "Connected.");
      await refresh();
      navigate("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="page">
      <PageHead
        title="Connect to SoftEther"
        sub="The management port and administrator password of the instance on this machine."
      />
      <div className="card" style={{ padding: "var(--s5)", maxWidth: 560 }}>
        {error && <ErrorAlert>{error}</ErrorAlert>}
        {testResult && (
          <div className="alert alert--ok">
            <IconCheck size={14} /> {testResult}
          </div>
        )}
        <div className="row2">
          <Field label="Host" hint="127.0.0.1 unless SoftEther deliberately lives elsewhere.">
            <input className="input mono" value={host} onChange={(e) => setHost(e.target.value)}
              autoCapitalize="none" spellCheck={false} inputMode="url" />
          </Field>
          <Field label="Management port" hint="5555 on a stock install; 443 and 992 also answer.">
            <input className="input mono" type="number" min={1} max={65535} value={port}
              onChange={(e) => setPort(Number(e.target.value))} inputMode="numeric" />
          </Field>
        </div>
        <Field
          label={configured ? "Administrator password (empty keeps the stored one)" : "Administrator password"}
        >
          <input className="input" type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} autoComplete="off" />
        </Field>
        <div style={{ display: "flex", gap: "var(--s2)", marginTop: "var(--s2)" }}>
          <button className="btn" onClick={test} disabled={busy !== null || (!password && !configured)}>
            {busy === "test" && <span className="spin" />} Test
          </button>
          <button className="btn btn--primary" onClick={save} disabled={busy !== null || (!password && !configured)}>
            {busy === "save" && <span className="spin" />} Save & connect
          </button>
        </div>
        <p className="hint" style={{ marginTop: "var(--s3)" }}>
          No SoftEther on this machine yet? Install it first — the panel manages an existing
          instance; it does not install one.
        </p>
      </div>
    </div>
  );
}
