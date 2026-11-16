import Link from "next/link";

import { Card, Code, Kv, LINK, SectionTitle, VerdictBadge } from "@/components/ui";
import { listHref, tape, type Verdict, VERDICTS } from "@/lib/tape";

const { recording: rec, summary } = tape;
const excluded = rec.excluded.reduce((n, e) => n + e.count, 0);
const minutes = (s: number) => (s < 90 ? `${s} seconds` : `${Math.round(s / 60)} minutes`);
const when = (iso: string) => iso.replace("T", " ").replace("Z", " UTC");

export default function Home() {
  const seen = VERDICTS.filter((v) => summary.by_verdict[v.key] > 0);
  const unseen = VERDICTS.filter((v) => summary.by_verdict[v.key] === 0);
  return (
    <div className="space-y-8">
      <section className="max-w-3xl">
        <h1 className="mb-2 font-display text-heading font-semibold sm:text-display">Watch the quarry decide</h1>
        <p className="text-body text-text-secondary">
          argus-quarry collects training images from Wikimedia Commons. It keeps a file only if its licence is public domain or CC0, and it records where
          each file came from. This page replays one real run: {summary.total} candidates across {summary.by_subject.length} subjects, the licence Commons
          gave for each, and what quarry did with it.
        </p>
      </section>

      <section aria-labelledby="counts">
        <h2 id="counts" className="sr-only">
          Counts
        </h2>
        <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-4">
          {seen.map((v) => (
            <Tile key={v.key} verdict={v.key} count={summary.by_verdict[v.key]} text={v.blurb} />
          ))}
          <Card as="div" className="flex flex-col gap-1">
            <span className="text-caption font-semibold uppercase tracking-wide text-text-muted">Images on this site</span>
            <span className="font-display text-display font-semibold leading-none">{summary.images}</span>
            <span className="text-small text-text-secondary">640 px copies of the landed files. Nothing else is shown as an image.</span>
          </Card>
          {excluded ? (
            <Card as="div" className="flex flex-col gap-1">
              <span className="text-caption font-semibold uppercase tracking-wide text-text-muted">Left off this page</span>
              <span className="font-display text-display font-semibold leading-none">{excluded}</span>
              <span className="text-small text-text-secondary">Landed photos with people posing in them. This demo shows no photos of real people.</span>
            </Card>
          ) : null}
        </div>
        {unseen.length ? (
          <p className="mt-3 text-small text-text-muted">
            Outcomes quarry has that no candidate hit this time: {unseen.map((v) => v.label).join(", ")}.
          </p>
        ) : null}
      </section>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card>
          <SectionTitle>How quarry decides</SectionTitle>
          <ol className="list-decimal space-y-2 pl-5 text-small text-text-secondary marker:text-text-muted">
            <li>
              <strong className="text-text-primary">Licence gate.</strong> Commons gives a licence string for each file. Quarry accepts it only if it contains a
              public-domain or CC0 term ({rec.settings.accepted_tokens.map((t, i) => (
                <span key={t}>
                  {i ? ", " : ""}
                  <Code>{t}</Code>
                </span>
              ))}). Anything else is quarantined: recorded, never downloaded.
            </li>
            <li>
              <strong className="text-text-primary">Download.</strong> At most {rec.settings.rate_per_sec} requests a second to Commons, with a user agent that names
              the project.
            </li>
            <li>
              <strong className="text-text-primary">Verify.</strong> The file must decode as an image. One over {rec.settings.max_megapixels} megapixels is
              stored downscaled; the full-resolution URL is kept.
            </li>
            <li>
              <strong className="text-text-primary">Deduplicate.</strong> If its SHA256 is already in the pool, the copy is deleted.
            </li>
            <li>
              <strong className="text-text-primary">Land.</strong> The file goes to <Code>images/&lt;category&gt;/&lt;subject&gt;/</Code> and its provenance
              (source, landing page, licence, author, size, SHA256) to the database.
            </li>
          </ol>
        </Card>

        <Card>
          <SectionTitle>Licences Commons gave</SectionTitle>
          <ul className="divide-y divide-base-600/30">
            {summary.by_licence.map((l) => (
              <li key={l.licence} className="flex items-center justify-between gap-3 py-2 text-small">
                <span className="min-w-0 [overflow-wrap:anywhere]">{l.licence}</span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="tabular-nums text-text-secondary">{l.count}</span>
                  <span
                    className={`w-24 rounded-sm border px-1.5 py-0.5 text-center text-caption font-semibold ${l.accepted ? "border-success/50 bg-success/10 text-success" : "border-warning/60 bg-warning/10 text-warning"}`}
                  >
                    {l.accepted ? "✓ Accepted" : "✕ Not accepted"}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card>
        <SectionTitle>Subjects</SectionTitle>
        <p className="mb-3 text-small text-text-secondary">
          Up to {rec.settings.limit_per_subject} Commons results each. No identity (people) subjects are recorded for this demo.
        </p>
        <ul className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {summary.by_subject.map((s) => (
            <li key={s.folder} className="min-w-0 rounded-md border border-base-600/40 bg-base-900/40 p-3">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <span className="font-medium">{s.name}</span>
                <Link href={listHref({ status: null, category: s.category })} className="text-caption text-text-muted underline-offset-2 hover:underline">
                  {s.category}
                </Link>
              </div>
              <p className="mt-0.5 text-caption text-text-muted [overflow-wrap:anywhere]">
                search <Code>{s.search}</Code>
              </p>
              <Bar landed={s.by_verdict.landed ?? 0} total={s.total} />
              <p className="mt-1 text-small text-text-secondary">
                {VERDICTS.filter((v) => s.by_verdict[v.key])
                  .map((v) => `${s.by_verdict[v.key]} ${v.label.toLowerCase()}`)
                  .join(", ")}
              </p>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-small">
          <Link href="/records/" className={LINK}>
            Browse all {summary.total} candidates
          </Link>
        </p>
      </Card>

      <Card>
        <SectionTitle>This recording</SectionTitle>
        <dl>
          <Kv label="Recorded">
            {when(rec.started_at)}, took {minutes(rec.seconds)}
          </Kv>
          <Kv label="argus-quarry">
            {rec.versions.argus_quarry}, run from repo commit <Code>{rec.versions.commit.slice(0, 7)}</Code> (the recorder&apos;s own commit; quarry&apos;s{" "}
            <Code>src/</Code> {rec.versions.src_modified ? "had local changes" : "was unmodified"})
          </Kv>
          <Kv label="Python and libraries">
            Python {rec.versions.python},{" "}
            {Object.entries(rec.versions.packages)
              .map(([k, v]) => `${k} ${v}`)
              .join(", ")}
          </Kv>
          <Kv label="Commands">
            <span className="block space-y-1">
              {rec.commands.map((c) => (
                <code key={c} className="block rounded-sm bg-base-700 px-2 py-1 font-mono text-caption [overflow-wrap:anywhere]">
                  {c}
                </code>
              ))}
            </span>
            <span className="mt-1 block text-caption text-text-muted">
              The recorder runs the same harvest and ingest code as <Code>{rec.equivalent_cli}</Code>, over the subjects in <Code>{rec.subjects_file}</Code>,
              and writes down each outcome.
            </span>
          </Kv>
          <Kv label="Settings">
            quarry&apos;s defaults: {rec.settings.rate_per_sec} requests/s (burst {rec.settings.burst}), {rec.settings.max_megapixels} MP cap,{" "}
            {rec.settings.max_total_gb} GB pool budget
          </Kv>
          <Kv label="User agent" mono>
            {rec.settings.user_agent}
          </Kv>
          <Kv label="Run again">
            The same harvest over the same pool, straight after:{" "}
            {Object.entries(rec.rerun)
              .map(([k, n]) => `${n} ${k}`)
              .join(", ")}
            , for all {Object.values(rec.rerun).reduce((a, b) => a + b, 0)} candidates (the {excluded} left off this page included). Nothing was downloaded twice.
          </Kv>
          {rec.excluded.map((e) => (
            <Kv key={e.reason} label="Left out">
              {e.count} landed {e.count === 1 ? "record" : "records"}. {e.reason}
            </Kv>
          ))}
        </dl>
      </Card>
    </div>
  );
}

function Tile({ verdict, count, text }: { verdict: Verdict; count: number; text: string }) {
  return (
    <Card as="div" className="flex flex-col gap-1">
      <span>
        <VerdictBadge verdict={verdict} />
      </span>
      <Link href={listHref({ status: verdict, category: null })} className="font-display text-display font-semibold leading-none hover:underline">
        {count}
      </Link>
      <span className="text-small text-text-secondary">{text}</span>
    </Card>
  );
}

/** One cell per candidate: landed first, then the rest. The counts are written out under it, so colour is never the
 *  only signal. (No inline style: the page's CSP has no 'unsafe-inline' for styles.) */
function Bar({ landed, total }: { landed: number; total: number }) {
  return (
    <div className="mt-2 flex gap-0.5" aria-hidden="true">
      {Array.from({ length: total }, (_, i) => (
        <span key={i} className={`h-2 flex-1 rounded-sm ${i < landed ? "bg-success" : "bg-warning/40"}`} />
      ))}
    </div>
  );
}
