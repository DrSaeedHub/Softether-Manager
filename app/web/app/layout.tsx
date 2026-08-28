import type { Metadata, Viewport } from "next";
import "../src/styles.css";

export const metadata: Metadata = {
  title: "SoftEther Manager",
  description: "Management panel for SoftEther VPN servers.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#16181d" },
    { media: "(prefers-color-scheme: light)", color: "#e3e7ee" },
  ],
};

/**
 * Fonts are vendored files in public/, declared here with *relative* URLs so
 * they resolve wherever the panel is mounted -- the CSS bundle would rewrite
 * them to absolute /_next paths, which a secret-prefix install cannot serve.
 */
const fontFaces = `
@font-face{font-family:"Manrope";font-style:normal;font-display:swap;font-weight:200 800;
src:url("./fonts/manrope-latin-wght-normal.woff2") format("woff2-variations")}
@font-face{font-family:"Geist Mono";font-style:normal;font-display:swap;font-weight:100 900;
src:url("./fonts/geist-mono-latin-wght-normal.woff2") format("woff2-variations")}
`;

/**
 * Resolve the stored theme before first paint, so a light-theme user never
 * sees a dark flash (or the reverse). Mirrors src/ui/theme.tsx.
 */
const themeScript = `
try{var t=localStorage.getItem("sem_theme");
if(t==="light"||t==="dark"){document.documentElement.setAttribute("data-theme",t);
document.documentElement.style.colorScheme=t;}}catch(e){}
document.documentElement.classList.add("preload");
requestAnimationFrame(function(){requestAnimationFrame(function(){
document.documentElement.classList.remove("preload")})});
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta name="color-scheme" content="dark light" />
        <meta name="mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
        <meta name="apple-mobile-web-app-title" content="SoftEther" />
        <style dangerouslySetInnerHTML={{ __html: fontFaces }} />
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
