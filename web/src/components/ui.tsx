import type { ReactNode } from "react";

import { type Verdict, verdictLabel } from "@/lib/tape";

const VERDICT_TONE: Record<Verdict, string> = {
  landed: "border-success/50 bg-success/10 text-success",
  quarantined: "border-warning/60 bg-warning/10 text-warning",
  "not-jpeg-png": "border-base-600 bg-base-700 text-text-secondary",
  duplicate: "border-info/50 bg-info/10 text-info",
  failed: "border-error/50 bg-error/10 text-error",
  skipped: "border-base-600 bg-base-700 text-text-secondary",
};

const VERDICT_MARK: Record<Verdict, string> = {
  landed: "✓",
  quarantined: "✕",
  "not-jpeg-png": "–",
  duplicate: "=",
  failed: "!",
  skipped: "↺",
};

/** A verdict as a word with a mark; the colour is never the only signal. */
export function VerdictBadge({ verdict, className = "" }: { verdict: Verdict; className?: string }) {
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 py-0.5 text-caption font-semibold ${VERDICT_TONE[verdict]} ${className}`}>
      <span aria-hidden="true">{VERDICT_MARK[verdict]}</span>
      {verdictLabel(verdict)}
    </span>
  );
}

export function Card({ children, className = "", as: Tag = "section" }: { children: ReactNode; className?: string; as?: "section" | "div" | "article" | "aside" }) {
  return <Tag className={`min-w-0 rounded-lg border border-base-600/60 bg-base-800 p-4 ${className}`}>{children}</Tag>;
}

export function SectionTitle({ children, className = "", id }: { children: ReactNode; className?: string; id?: string }) {
  return (
    <h2 id={id} className={`mb-3 font-display text-small font-semibold uppercase tracking-wide text-text-secondary ${className}`}>
      {children}
    </h2>
  );
}

/** A label and its value. Long values (URLs, hashes) wrap anywhere rather than widening the page. */
export function Kv({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="grid grid-cols-1 gap-x-4 gap-y-0.5 border-b border-base-600/30 py-2 last:border-b-0 sm:grid-cols-[11rem_minmax(0,1fr)]">
      <dt className="text-small text-text-muted">{label}</dt>
      <dd className={`min-w-0 text-small text-text-primary [overflow-wrap:anywhere] ${mono ? "font-mono text-caption sm:text-small" : ""}`}>{children}</dd>
    </div>
  );
}

export const LINK = "text-interactive underline decoration-interactive/40 underline-offset-2 hover:decoration-interactive";

/** `href` if it is an absolute http(s) URL, else `null`: tape URLs come from third-party metadata,
 * and a `javascript:` or `data:` link would run script in this origin. */
export function safeHref(href: string): string | null {
  try {
    const url = new URL(href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

export function Ext({ href, children, className = "" }: { href: string; children: ReactNode; className?: string }) {
  const safe = safeHref(href);
  if (safe === null) return <span className={className}>{children}</span>;
  return (
    <a href={safe} className={`${LINK} ${className}`} rel="noopener noreferrer">
      {children}
    </a>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="rounded-sm bg-base-700 px-1 py-0.5 font-mono text-[0.9em] [overflow-wrap:anywhere]">{children}</code>;
}
