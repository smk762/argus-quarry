"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** Keys go to a field the user is typing in, not to the page. */
const typing = (t: EventTarget | null) => t instanceof HTMLElement && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName));

const LINK = "inline-flex h-8 items-center rounded-md border border-base-600 px-2.5 text-small text-text-secondary hover:bg-base-700 hover:text-text-primary";

/**
 * Previous and Next for a record page, through the filtered list it was opened from (wrapping at both ends): two
 * links, "n of N", and the arrow keys. The pattern is argus-assay's StepNav.
 */
export function StepNav({ prev, next, n, total, label }: { prev: string; next: string; n: number; total: number; label: string }) {
  const router = useRouter();
  useEffect(() => {
    if (total < 2) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.defaultPrevented || typing(e.target)) return;
      const by = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : 0;
      if (!by) return;
      e.preventDefault();
      router.push(by < 0 ? prev : next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router, prev, next, total]);
  return (
    <nav aria-label={label} className="flex items-center gap-1.5">
      {total > 1 ? (
        <Link href={prev} className={LINK} aria-keyshortcuts="ArrowLeft" rel="prev">
          ← Prev
        </Link>
      ) : null}
      <span className="whitespace-nowrap px-1 text-caption text-text-muted" aria-live="polite">
        {n} of {total}
      </span>
      {total > 1 ? (
        <Link href={next} className={LINK} aria-keyshortcuts="ArrowRight" rel="next">
          Next →
        </Link>
      ) : null}
    </nav>
  );
}
