import type { Metadata } from "next";

import { RecordList } from "@/components/RecordList";
import { summaries, tape } from "@/lib/tape";

export const metadata: Metadata = { title: "Candidates" };

export default function RecordsPage() {
  return (
    <>
      <h1 className="mb-1 font-display text-heading font-semibold">Candidates</h1>
      <p className="mb-5 max-w-3xl text-small text-text-secondary">
        Every file Commons returned for the {tape.summary.by_subject.length} subjects, and what quarry did with it. Only landed files are shown as images.
        Everything else is text and a link to its Commons page.
      </p>
      <RecordList items={summaries()} />
    </>
  );
}
