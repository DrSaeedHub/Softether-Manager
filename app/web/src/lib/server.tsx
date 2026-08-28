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
import { api, type Probe, type Wire } from "./api";
import { useAuth } from "./auth";

/**
 * The managed server, held once for the whole app.
 *
 * The sidebar's hub tree, the health chip and the dashboard all read the same
 * answer -- one probe and one hub enumeration on a timer, instead of three
 * components polling separately.
 */

export type ServerHealth = "probing" | "online" | "offline" | "unconfigured";

interface ServerApi {
  probe: Probe | null;
  hubs: Wire[] | null;
  health: ServerHealth;
  refresh: () => Promise<void>;
}

const ServerContext = createContext<ServerApi>(null as unknown as ServerApi);

const BACKGROUND_REFRESH_MS = 30_000;

export function ServerProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [probe, setProbe] = useState<Probe | null>(null);
  const [hubs, setHubs] = useState<Wire[] | null>(null);
  const aliveRef = useRef(true);

  const refresh = useCallback(async () => {
    let next: Probe;
    try {
      next = await api.probe();
    } catch {
      return; // signed out, or the panel itself is unreachable; keep what we have
    }
    if (!aliveRef.current) return;
    setProbe(next);
    if (next.online) {
      const list = await api.hubs().catch(() => null);
      if (aliveRef.current && list) setHubs((list.HubList as Wire[]) ?? []);
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    if (!user) {
      setProbe(null);
      setHubs(null);
      return;
    }
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, BACKGROUND_REFRESH_MS);
    return () => {
      aliveRef.current = false;
      window.clearInterval(timer);
    };
  }, [user, refresh]);

  const health: ServerHealth =
    probe === null
      ? "probing"
      : !probe.configured
        ? "unconfigured"
        : probe.online
          ? "online"
          : "offline";

  return (
    <ServerContext.Provider value={{ probe, hubs, health, refresh }}>
      {children}
    </ServerContext.Provider>
  );
}

export const useServer = () => useContext(ServerContext);
