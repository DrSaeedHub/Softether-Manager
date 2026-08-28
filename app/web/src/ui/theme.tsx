"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

/**
 * Light / dark, with a "system" default that follows the OS.
 *
 * The choice is written to `data-theme` on the root element and remembered in
 * localStorage. "system" writes nothing and lets `prefers-color-scheme`
 * decide, so a machine that flips to dark at night flips the panel with it.
 * A head script applies the stored choice before first paint; this provider
 * only has to keep it current afterwards.
 */
type ThemeChoice = "light" | "dark" | "system";

const STORAGE_KEY = "sem_theme";

interface ThemeApi {
  choice: ThemeChoice;
  /** What is actually showing right now, after resolving "system". */
  resolved: "light" | "dark";
  setChoice: (c: ThemeChoice) => void;
  /** Cycle light -> dark -> light (from whatever is currently showing). */
  toggle: () => void;
}

const ThemeContext = createContext<ThemeApi>(null as unknown as ThemeApi);

function readStored(): ThemeChoice {
  if (typeof window === "undefined") return "system";
  const v = localStorage.getItem(STORAGE_KEY);
  return v === "light" || v === "dark" || v === "system" ? v : "system";
}

function systemDark(): boolean {
  if (typeof window === "undefined") return true;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function apply(choice: ThemeChoice): "light" | "dark" {
  const resolved = choice === "system" ? (systemDark() ? "dark" : "light") : choice;
  if (typeof document === "undefined") return resolved;
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
  root.style.colorScheme = resolved;
  return resolved;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(() => readStored());
  const [resolved, setResolved] = useState<"light" | "dark">(() => apply(readStored()));

  useEffect(() => {
    setResolved(apply(choice));
    if (choice === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, choice);
  }, [choice]);

  // While on "system", follow the OS changing under us.
  useEffect(() => {
    if (choice !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setResolved(apply("system"));
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [choice]);

  const toggle = () => setChoiceState(resolved === "dark" ? "light" : "dark");

  return (
    <ThemeContext.Provider value={{ choice, resolved, setChoice: setChoiceState, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeContext);
