import Link from "next/link";
import type { ReactNode } from "react";

import { tape } from "@/lib/tape";

import { NavLinks } from "./NavLinks";
import { ThemeToggle } from "./ThemeToggle";

export const REPO_URL = "https://github.com/smk762/argus-quarry";

/** Header, main pane and footer. */
export function Shell({ children }: { children: ReactNode }) {
  const { recording } = tape;
  const title = `Recorded from argus-quarry ${recording.versions.argus_quarry} on ${recording.started_at.slice(0, 10)}. Nothing runs while you browse.`;
  return (
    <div className="mx-auto flex min-h-dvh w-full max-w-[1280px] flex-col">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-base-600/60 px-4 py-3 sm:px-6">
        <Link href="/" className="flex items-center gap-2 font-display text-subheading font-semibold text-text-primary">
          Argus Quarry
        </Link>
        <span className="dh-badge shrink-0" title={title}>
          Recorded
        </span>
        <div className="ml-auto flex items-center gap-1">
          <NavLinks />
          <ThemeToggle />
        </div>
      </header>
      <main id="main" className="min-w-0 flex-1 px-4 py-5 sm:px-6">
        {children}
      </main>
      <footer className="space-y-1 border-t border-base-600/60 px-4 py-4 text-caption text-text-muted sm:px-6">
        <p>
          A replay of one real argus-quarry run against Wikimedia Commons, recorded on {recording.started_at.slice(0, 10)}. The images are 640 px copies of
          public-domain and CC0 files; each one names its author and links to its Commons page. Commons itself is not contacted while you browse.
        </p>
        <p>
          Source and docs:{" "}
          <a href={REPO_URL} className="underline decoration-base-600 underline-offset-2 hover:text-text-secondary">
            github.com/smk762/argus-quarry
          </a>
          . Quarry is a command-line tool; this page is a replay, not its interface.
        </p>
      </footer>
    </div>
  );
}
