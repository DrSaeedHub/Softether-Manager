"use client";

/**
 * A hash router.
 *
 * The export is one HTML file served under a prefix chosen at install time,
 * so path routing would 404 on every deep link the server has never heard of.
 * The hash never reaches the server: `/#/server/3/hub/VPN/users` works from a
 * bookmark, a refresh, and any mount point, with zero server cooperation.
 */
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";

export interface Route {
  /** The hash path, normalised: always starts with "/", no trailing slash. */
  path: string;
  parts: string[];
}

function parse(): Route {
  if (typeof window === "undefined") return { path: "/", parts: [] };
  let raw = window.location.hash.replace(/^#/, "");
  if (!raw.startsWith("/")) raw = "/" + raw;
  raw = raw !== "/" ? raw.replace(/\/+$/, "") : raw;
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  return { path: raw, parts };
}

const RouteContext = createContext<Route>({ path: "/", parts: [] });

export function RouterProvider({ children }: { children: ReactNode }) {
  const [route, setRoute] = useState<Route>(parse);
  useEffect(() => {
    const onChange = () => setRoute(parse());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return <RouteContext.Provider value={route}>{children}</RouteContext.Provider>;
}

export const useRoute = () => useContext(RouteContext);

export function navigate(path: string) {
  window.location.hash = path.startsWith("/") ? path : "/" + path;
}

export function back() {
  window.history.back();
}

/** An anchor that stays a real link (open-in-new-tab works) but routes by hash. */
export function Link({
  to,
  className,
  children,
  onClick,
  ...rest
}: {
  to: string;
  className?: string;
  children: ReactNode;
  onClick?: (e: MouseEvent<HTMLAnchorElement>) => void;
} & Record<string, unknown>) {
  return (
    <a href={`#${to}`} className={className} onClick={onClick} {...rest}>
      {children}
    </a>
  );
}

/** Encode one path segment so hub and user names survive slashes and spaces. */
export const seg = encodeURIComponent;
