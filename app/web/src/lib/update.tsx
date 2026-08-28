"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, api, type Wire } from "./api";
import { useAuth } from "./auth";
import { useToast } from "./toast";

/**
 * Watching the panel update itself.
 *
 * The awkward part is the middle: applying an update restarts the panel, so
 * the page watching it loses the server it is watching. That is not an error
 * and is deliberately not reported as one -- the poll simply fails while the
 * panel is down, and the panel coming back with a finished run is what ends
 * the wait. What the run did is read back from the transient systemd unit the
 * installer ran in, which outlives the restart.
 *
 * A finished run then reloads the page: the panel that just replaced
 * itself is serving new frontend assets, and the tab watching it is still
 * running the old ones.
 */

export interface UpdateStatus {
  check: Wire; // /system/update: current_version, latest{...}, update_available...
  state: Wire; // /system/update/state: stage, error, log[]...
}

interface UpdateApi {
  status: UpdateStatus | null;
  error: string | null;
  isChecking: boolean;
  isStarting: boolean;
  startError: string | null;
  applying: boolean;
  /** A finished update is about to reload the page. */
  reloading: boolean;
  isOpen: boolean;
  open: () => void;
  close: () => void;
  check: () => Promise<void>;
  start: (version?: string) => Promise<void>;
}

const UpdateContext = createContext<UpdateApi>(null as unknown as UpdateApi);

const IDLE_POLL_MS = 5 * 60 * 1000;

/** The version this tab loaded against, remembered for as long as the
 *  module is: a remounted provider must not forget which build is on
 *  screen, or the reload below never fires. A real page load re-evaluates
 *  the module and clears it. */
let loadedVersion = "";
/** Set once the reload is on the clock, so it is never scheduled twice. */
let reloadScheduled = false;
const BUSY_POLL_MS = 4000;

/** The release this tab has already announced, remembered for as long as the
 *  module is: the idle poll comes round every five minutes, and a toast on
 *  every one of them would be a nuisance rather than news. */
let announcedVersion = "";

export function UpdateProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const { push } = useToast();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isChecking, setChecking] = useState(false);
  const [isStarting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [isOpen, setOpen] = useState(false);

  const stage = String(status?.state?.stage ?? "idle");
  const applying = isStarting || stage === "running";
  const applyingRef = useRef(applying);
  applyingRef.current = applying;

  const poll = useCallback(async () => {
    try {
      const [check, state] = await Promise.all([api.updateStatus(), api.updateState()]);
      setStatus({ check, state });
      setError(null);
    } catch (err) {
      // While an update is running the panel is expected to disappear for a
      // while; that is the update working, not failing.
      if (!applyingRef.current) {
        setError(err instanceof ApiError ? err.message : "The panel could not be reached");
      }
    }
  }, []);

  useEffect(() => {
    if (user) void poll();
  }, [poll, user]);

  useEffect(() => {
    if (!user) return;
    const every = applying ? BUSY_POLL_MS : IDLE_POLL_MS;
    const timer = window.setInterval(() => void poll(), every);
    return () => window.clearInterval(timer);
  }, [applying, poll, user]);

  // What the page compares is versions, not stages: the version this tab
  // loaded against the one the panel now reports. That catches an update
  // this tab started, one started from sem, and one started from another
  // tab alike -- and a "succeeded" left over from days ago, which is still
  // the reported state, moves no version and so reloads nothing.
  const [reloading, setReloading] = useState(false);
  useEffect(() => {
    const running = String(status?.check?.current_version ?? "");
    if (!running || reloadScheduled) return;
    if (!loadedVersion) {
      loadedVersion = running;
      return;
    }
    if (running === loadedVersion) return;
    reloadScheduled = true;
    setReloading(true);
    // The timer is deliberately not cleared on cleanup, and `reloading` is
    // deliberately not a dependency: this effect sets state, so listing what
    // it sets would re-run it, and a cleanup would cancel the reload it had
    // just scheduled -- which is exactly why the first two attempts at this
    // never fired. reloadScheduled is what keeps it to one.
    window.setTimeout(() => window.location.reload(), 1500);
  }, [status]);

  const check = useCallback(async () => {
    setChecking(true);
    try {
      const checkResult = await api.updateCheck();
      const state = await api.updateState();
      setStatus({ check: checkResult, state });
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The check could not be run");
    } finally {
      setChecking(false);
    }
  }, []);

  const start = useCallback(async (version = "") => {
    setStarting(true);
    setStartError(null);
    try {
      const state = await api.updateApply(version);
      setStatus((s) => ({ check: s?.check ?? {}, state }));
    } catch (err) {
      setStartError(err instanceof ApiError ? err.message : "The update could not be started");
    } finally {
      setStarting(false);
    }
  }, []);

  const open = useCallback(() => {
    setOpen(true);
    setStartError(null);
  }, []);

  // A release that lands while the tab sits open would otherwise only show as
  // a dot on the version pill, which is easy to miss. Announce it once per
  // version, with the updater on the toast so it is one click away -- and not
  // while an update is already running, when it is no longer news.
  useEffect(() => {
    if (!user || applying) return;
    if (!status?.check?.update_available) return;
    const version = String(status.check.latest?.version ?? "");
    if (!version || announcedVersion === version) return;
    announcedVersion = version;
    push("info", `Version ${version} is available.`, {
      action: { label: "Update", run: open },
      duration: 12000,
    });
  }, [status, user, applying, push, open]);

  return (
    <UpdateContext.Provider
      value={{
        status,
        error,
        isChecking,
        isStarting,
        startError,
        applying,
        reloading,
        isOpen,
        open,
        close: () => setOpen(false),
        check,
        start,
      }}
    >
      {children}
    </UpdateContext.Provider>
  );
}

export const useUpdate = () => useContext(UpdateContext);
