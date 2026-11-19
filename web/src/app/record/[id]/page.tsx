/* eslint-disable @next/next/no-img-element -- static export: the thumbnail is already a 640 px WebP file on this site */
import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { RecordNav } from "@/components/RecordNav";
import { Card, Code, Ext, Kv, SectionTitle, VerdictBadge } from "@/components/ui";
import { bytes, dims, displayTitle, tape, type TapeRecord, subjectName } from "@/lib/tape";

export const dynamicParams = false;

export function generateStaticParams() {
  return tape.records.map((r) => ({ id: r.id }));
}

const find = (id: string) => tape.records.find((r) => r.id === id);

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }): Promise<Metadata> {
  const r = find((await params).id);
  return { title: r ? displayTitle(r.title) : "Candidate" };
}

export default async function RecordPage({ params }: { params: Promise<{ id: string }> }) {
  const r = find((await params).id);
  if (!r) notFound();
  const p = r.provenance;
  return (
    <>
      <RecordNav id={r.id} />
      <div className="mb-5 flex flex-wrap items-start gap-x-3 gap-y-2">
        <h1 className="min-w-0 flex-1 basis-[16rem] font-display text-subheading font-semibold [overflow-wrap:anywhere] sm:text-heading">{displayTitle(r.title)}</h1>
        <VerdictBadge verdict={r.verdict} className="mt-1.5 text-small" />
      </div>
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <div className="min-w-0 space-y-5">
          {r.image ? <Picture r={r} /> : <NoPicture r={r} />}
          <Decision r={r} />
        </div>
        <Card>
          <SectionTitle>Provenance record</SectionTitle>
          <dl>
            <Kv label="Subject">
              {subjectName(r.subject)} <span className="text-text-muted">({r.category})</span>
            </Kv>
            <Kv label="Found by">
              Commons search <Code>{r.query}</Code>
              {r.rank != null ? <span className="text-text-muted">, result {r.rank}</span> : null}
            </Kv>
            <Kv label="Source">Wikimedia Commons ({p.source})</Kv>
            <Kv label="Landing page">{p.source_url ? <Ext href={p.source_url}>{p.source_url}</Ext> : "—"}</Kv>
            <Kv label="Full-resolution URL" mono>
              {p.remote_url ?? "—"}
            </Kv>
            <Kv label="Licence">{r.licence.raw}</Kv>
            <Kv label="Author / credit">{p.attribution ?? "—"}</Kv>
            <Kv label="Year">{p.year ?? "—"}</Kv>
            <Kv label="On Commons">
              {p.mime ?? "—"}, {dims(p.commons_width, p.commons_height)}, {bytes(p.commons_bytes)}
            </Kv>
            {r.verdict === "landed" ? (
              <>
                <Kv label="Downloaded">{bytes(p.downloaded_bytes)}</Kv>
                <Kv label="Stored">
                  {dims(p.width, p.height)}, {bytes(p.file_size)}
                </Kv>
                <Kv label="SHA256" mono>
                  {p.sha256}
                </Kv>
                <Kv label="Pool path" mono>
                  {p.path}
                </Kv>
              </>
            ) : (
              <Kv label="Downloaded">Nothing. No file, no SHA256.</Kv>
            )}
            <Kv label="Database row">
              status <Code>{p.db_status ?? "none"}</Code>, licence <Code>{p.db_licence ?? "—"}</Code>
            </Kv>
            {p.downloaded_at ? <Kv label="Landed at">{p.downloaded_at.replace("T", " ").replace(/\+00:00$|Z$/, " UTC")}</Kv> : null}
          </dl>
        </Card>
      </div>
    </>
  );
}

function Picture({ r }: { r: TapeRecord }) {
  const img = r.image!;
  const p = r.provenance;
  return (
    <figure className="min-w-0 overflow-hidden rounded-lg border border-base-600/60 bg-base-800">
      <div className="flex justify-center bg-base-900">
        <img src={`/tape/${img.src}`} width={img.width} height={img.height} alt={displayTitle(r.title)} fetchPriority="high" className="h-auto max-h-[70vh] w-auto max-w-full object-contain" />
      </div>
      <figcaption className="space-y-1 p-3 text-caption text-text-secondary [overflow-wrap:anywhere]">
        <p>
          {p.attribution ? <>{p.attribution}. </> : null}
          {r.licence.raw}. {p.source_url ? <Ext href={p.source_url}>Wikimedia Commons</Ext> : "Wikimedia Commons"}.
        </p>
        <p className="text-text-muted">
          Shown here as a {img.width} × {img.height} WebP copy. Quarry stored {dims(p.width, p.height)}.
        </p>
      </figcaption>
    </figure>
  );
}

function NoPicture({ r }: { r: TapeRecord }) {
  return (
    <div className="flex min-h-48 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-base-600 bg-base-800 p-6 text-center">
      <p className="font-medium text-text-secondary">No image on this page</p>
      <p className="max-w-sm text-small text-text-muted">
        {r.verdict === "quarantined"
          ? `Its licence, ${r.licence.raw}, is not one quarry accepts, so quarry did not download it and this site does not show it.`
          : "Quarry did not land this file, so this site does not show it."}
      </p>
      {r.provenance.source_url ? <Ext href={r.provenance.source_url} className="text-small">See it on Wikimedia Commons</Ext> : null}
    </div>
  );
}

function Decision({ r }: { r: TapeRecord }) {
  const l = r.licence;
  return (
    <Card>
      <SectionTitle>Decision</SectionTitle>
      <p className="mb-4 text-body text-text-primary">{r.reason}</p>
      <ol className="space-y-2 text-small">
        <li className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-text-muted">1. Licence on Commons:</span>
          <Code>{l.raw}</Code>
        </li>
        <li className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-text-muted">2. Licence gate:</span>
          {l.accepted ? (
            <span>
              reads as <strong>{l.normalised}</strong>
              {l.matched ? (
                <>
                  {" "}
                  (contains <Code>{l.matched}</Code>)
                </>
              ) : null}
            </span>
          ) : (
            <span>no PD or CC0 term found</span>
          )}
        </li>
        <li className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-text-muted">3. Result:</span>
          <VerdictBadge verdict={r.verdict} />
        </li>
      </ol>
    </Card>
  );
}
