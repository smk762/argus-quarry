"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ALL, type Filter, listHref, position, type RecordSummary, recordHref, verdictLabel } from "@/lib/tape";

import { StepNav } from "./StepNav";

/** Back to the list and Prev/Next through it, keeping the filter (`?status=`, `?category=`) the record was opened from. */
export function RecordNav({ id, items }: { id: string; items: RecordSummary[] }) {
  return (
    <Suspense fallback={<Nav id={id} items={items} filter={ALL} />}>
      <FromQuery id={id} items={items} />
    </Suspense>
  );
}

function FromQuery({ id, items }: { id: string; items: RecordSummary[] }) {
  const params = useSearchParams();
  const filter: Filter = { status: null, category: null };
  const s = params.get("status");
  const c = params.get("category");
  if (s && items.some((r) => r.verdict === s)) filter.status = s as Filter["status"];
  if (c && items.some((r) => r.category === c)) filter.category = c;
  return <Nav id={id} items={items} filter={filter} />;
}

function Nav({ id, items, filter }: { id: string; items: RecordSummary[]; filter: Filter }) {
  const pos = position(items, id, filter);
  const what = [pos?.filter.status ? verdictLabel(pos.filter.status) : null, pos?.filter.category].filter(Boolean).join(", ") || "all";
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <Link href={listHref(pos?.filter ?? ALL)} className="text-small text-text-secondary underline-offset-2 hover:text-text-primary hover:underline">
        ← Candidates{what !== "all" ? ` (${what})` : ""}
      </Link>
      {pos ? <StepNav label={`Candidates: ${what}`} prev={recordHref(pos.prev, pos.filter)} next={recordHref(pos.next, pos.filter)} n={pos.n} total={pos.total} /> : null}
    </div>
  );
}
