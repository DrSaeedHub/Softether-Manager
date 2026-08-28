"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import { useAuth } from "./auth";

/**
 * How often the open page re-reads, by tier.
 *
 * Screens do not each carry a number: they say what kind of thing they are
 * watching -- something live, a detail screen, a list -- and the operator
 * sets what those mean in Settings. Anything genuinely fixed (a config
 * screen that refreshes hourly) still passes its own milliseconds.
 */
export type PollRate = number | "live" | "detail" | "list";

const DEFAULTS: Record<"live" | "detail" | "list", number> = {
  live: 5000,
  detail: 15000,
  list: 30000,
};

const RatesContext = createContext<typeof DEFAULTS>(DEFAULTS);

export function RatesProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [rates, setRates] = useState(DEFAULTS);

  useEffect(() => {
    if (!user) return;
    void api
      .settings()
      .then((s) => {
        const seconds = (value: unknown, fallback: number) => {
          const n = Number(value);
          return Number.isFinite(n) && n >= 1 ? n * 1000 : fallback;
        };
        setRates({
          live: seconds(s.ui_live_seconds, DEFAULTS.live),
          detail: seconds(s.ui_detail_seconds, DEFAULTS.detail),
          list: seconds(s.ui_list_seconds, DEFAULTS.list),
        });
      })
      .catch(() => {});
  }, [user]);

  return <RatesContext.Provider value={rates}>{children}</RatesContext.Provider>;
}

/** Resolve a tier to milliseconds; a number passes straight through. */
export function useRate(rate: PollRate): number {
  const rates = useContext(RatesContext);
  return useMemo(() => (typeof rate === "number" ? rate : rates[rate]), [rate, rates]);
}
