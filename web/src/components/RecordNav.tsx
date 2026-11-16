"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ALL, type Filter, listHref, parseFilter, position, navItems, type NavItem, recordHref, SUBJECTS, verdictLabel } from "@/lib/tape";

import { StepNav } from "./StepNav";

/** Back to the list and Prev/Next through it, keeping the filter (`?status=`, `?category=`, `?subject=`) the record was opened from. */
export function RecordNav({ id }: { id: string }) {
  // From the tape this client bundle already carries (one cached chunk), not from page props: a prop would put the
  // list into every record page's HTML and payload, a hundred times over.
  const items = navItems();
  return (
    <Suspense fallback={<Nav id={id} items={items} filter={ALL} />}>
      <FromQuery id={id} items={items} />
    </Suspense>
  );
}

function FromQuery({ id, items }: { id: string; items: NavItem[] }) {
  return <Nav id={id} items={items} filter={parseFilter(useSearchParams())} />;
}

function Nav({ id, items, filter }: { id: string; items: NavItem[]; filter: Filter }) {
  const pos = position(items, id, filter);
  const subject = SUBJECTS.find((s) => s.folder === pos?.filter.subject)?.name;
  const what = [pos?.filter.status ? verdictLabel(pos.filter.status) : null, pos?.filter.category, subject].filter(Boolean).join(", ") || "all";
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <Link href={listHref(pos?.filter ?? ALL)} className="text-small text-text-secondary underline-offset-2 hover:text-text-primary hover:underline">
        ← Candidates{what !== "all" ? ` (${what})` : ""}
      </Link>
      {pos ? <StepNav label={`Candidates: ${what}`} prev={recordHref(pos.prev, pos.filter)} next={recordHref(pos.next, pos.filter)} n={pos.n} total={pos.total} /> : null}
    </div>
  );
}
