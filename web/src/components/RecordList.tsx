"use client";

/* eslint-disable @next/next/no-img-element -- static export: thumbnails are already 640 px WebP files on this site */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ALL, CATEGORIES, type Filter, filterQuery, listHref, matches, parseFilter, type RecordSummary, recordHref, subjectName, VERDICTS } from "@/lib/tape";

import { VerdictBadge } from "./ui";

/** The candidates, filtered by `?status=` and `?category=`. The prerendered page shows them all; the query is read in the browser. */
export function RecordList({ items }: { items: RecordSummary[] }) {
  return (
    <Suspense fallback={<List items={items} filter={ALL} />}>
      <FromQuery items={items} />
    </Suspense>
  );
}

function FromQuery({ items }: { items: RecordSummary[] }) {
  return <List items={items} filter={parseFilter(useSearchParams())} />;
}

const PILL = "inline-flex items-center gap-1 rounded-md px-2.5 py-1.5 text-small";
const ON = "bg-interactive text-on-accent";
const OFF = "bg-base-800 text-text-secondary hover:bg-base-700 hover:text-text-primary";

function List({ items, filter }: { items: RecordSummary[]; filter: Filter }) {
  const shown = items.filter((r) => matches(r, filter));
  const verdicts = VERDICTS.filter((v) => items.some((r) => r.verdict === v.key));
  const count = (f: Filter) => items.filter((r) => matches(r, f)).length;
  return (
    <>
      <div className="mb-5 space-y-2">
        <div role="group" aria-label="Filter by verdict" className="flex flex-wrap gap-1">
          {[{ key: null, label: "All" }, ...verdicts].map((v) => {
            const f = { ...filter, status: v.key };
            return (
              <Link key={v.label} href={listHref(f)} aria-current={filter.status === v.key ? "page" : undefined} className={`${PILL} ${filter.status === v.key ? ON : OFF}`}>
                {v.label} <span className="opacity-75">{count(f)}</span>
              </Link>
            );
          })}
        </div>
        <div role="group" aria-label="Filter by category" className="flex flex-wrap gap-1">
          {[null, ...CATEGORIES].map((c) => {
            const f = { ...filter, category: c };
            return (
              <Link key={c ?? "all"} href={listHref(f)} aria-current={filter.category === c ? "page" : undefined} className={`${PILL} ${filter.category === c ? ON : OFF}`}>
                {c ?? "Every category"} <span className="opacity-75">{count(f)}</span>
              </Link>
            );
          })}
        </div>
      </div>
      <p className="mb-3 text-small text-text-muted" aria-live="polite">
        {shown.length === items.length ? `All ${items.length} candidates, in the order quarry saw them.` : `${shown.length} of ${items.length} candidates.`}
        {filterQuery(filter) ? (
          <>
            {" "}
            <Link href={listHref()} className="underline underline-offset-2 hover:text-text-primary">
              Clear filters
            </Link>
          </>
        ) : null}
      </p>
      {shown.length === 0 ? <p className="text-small text-text-muted">No candidate matches both filters.</p> : null}
      <ul className="grid grid-cols-1 gap-4 min-[480px]:grid-cols-2 md:grid-cols-3 xl:grid-cols-4">
        {shown.map((r, i) => (
          <li key={r.id} className="min-w-0">
            <Link href={recordHref(r.id, filter)} className="group block h-full overflow-hidden rounded-lg border border-base-600/60 bg-base-800 hover:border-interactive">
              <div className={`flex items-center justify-center bg-base-900 ${r.image ? "aspect-[4/3]" : "h-24 min-[480px]:aspect-[4/3] min-[480px]:h-auto"}`}>
                {r.image ? (
                  <img
                    src={`/tape/${r.image.src}`}
                    width={r.image.width}
                    height={r.image.height}
                    alt={`Thumbnail of ${r.title}`}
                    {...(i < 4 ? { fetchPriority: "high" as const } : { loading: "lazy" as const })}
                    className="h-full w-full object-contain"
                  />
                ) : (
                  <div className="px-4 text-center">
                    <p className="text-small font-medium text-text-secondary">{r.licence}</p>
                    <p className="mt-1 text-caption text-text-muted">Not downloaded, not shown</p>
                  </div>
                )}
              </div>
              <div className="space-y-1.5 p-3">
                <p className="line-clamp-2 text-small font-medium text-text-primary [overflow-wrap:anywhere]" title={r.title}>
                  {r.title}
                </p>
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <VerdictBadge verdict={r.verdict} />
                  <span className="text-caption text-text-muted">
                    {subjectName(r.subject)} · {r.category}
                  </span>
                </div>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}
