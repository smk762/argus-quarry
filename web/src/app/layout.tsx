import type { Metadata, Viewport } from "next";

import { DemoStrip } from "@/components/DemoStrip";
import { Shell } from "@/components/Shell";

import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://quarry.dragonhound.dev"),
  title: { default: "Argus Quarry demo · Dragonhound Labs", template: "%s · Argus Quarry demo" },
  description:
    "A replay of one real argus-quarry run against Wikimedia Commons: the licence each candidate carried, whether quarry landed or quarantined it, and the provenance it recorded.",
  // Favicons: argus-cortex brand/packages/quarry/favicon/, copied unchanged. favicon.ico, icon.svg and
  // apple-icon.png sit in src/app/ and Next emits their tags; icon-192/512 in public/ are for the manifest.
  manifest: "/site.webmanifest",
};

// The browser chrome follows the page: the family's --dh-bg values (a <meta> can't read a CSS variable).
export const viewport: Viewport = {
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbfbf9" },
    { media: "(prefers-color-scheme: dark)", color: "#121416" },
  ],
};

// The family theme script (public/dh-theme.js) is added by scripts/csp.mjs, blocking and first in <head> after the
// Trusted Types policy, so the saved choice is on <html> before the first paint.
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning data-dh-site="quarry">
      <body className="min-h-dvh">
        <a href="#main" className="skip-link">
          Skip to content
        </a>
        <DemoStrip />
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
