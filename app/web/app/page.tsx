"use client";

import { useEffect, useState } from "react";
import App from "../src/App";

/**
 * The whole panel is one exported page: routing is hash-based (see
 * src/lib/router.tsx), which is what lets the static export be served under
 * any secret prefix without a rebuild.
 *
 * App is imported statically -- a next/dynamic chunk would be fetched through
 * webpack's absolute /_next public path, which a prefixed install cannot
 * serve. The mounted gate keeps the export prerender (and hydration) away
 * from code that reads localStorage and location.hash.
 */
export default function Page() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return <div className="loading" aria-hidden="true" />;
  return <App />;
}
