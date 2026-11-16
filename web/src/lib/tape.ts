// The recorded tape (public/tape/tape.json, written by scripts/record_tape.py build) and the helpers the pages use.
// Pages import it at build time; the browser never fetches anything but the exported pages and thumbnails.
import raw from "../../public/tape/tape.json";

export type Verdict = "landed" | "quarantined" | "not-jpeg-png" | "duplicate" | "failed" | "skipped";

export interface LicenceCheck {
  raw: string;
  normalised: "PD" | "CC0" | null;
  accepted: boolean;
  matched: string | null;
}

export interface TapeRecord {
  id: string;
  subject: string;
  category: string;
  query: string | null;
  rank: number | null;
  title: string;
  verdict: Verdict;
  status: string;
  reason: string;
  licence: LicenceCheck;
  provenance: {
    source: string;
    source_url: string | null;
    remote_url: string | null;
    attribution: string | null;
    photographer: string | null;
    year: number | null;
    mime: string | null;
    commons_width: number | null;
    commons_height: number | null;
    commons_bytes: number | null;
    downloaded_bytes: number | null;
    sha256: string | null;
    width: number | null;
    height: number | null;
    file_size: number | null;
    path: string | null;
    fetched_from: string | null;
    db_status: string | null;
    db_licence: string | null;
    downloaded_at: string | null;
  };
  image: { src: string; width: number; height: number } | null;
}

export interface Tape {
  format: string;
  version: number;
  recording: {
    started_at: string;
    finished_at: string;
    seconds: number;
    versions: { argus_quarry: string; commit: string; src_modified: boolean; python: string; packages: Record<string, string | null> };
    settings: {
      source: string;
      limit_per_subject: number;
      user_agent: string;
      rate_per_sec: number;
      burst: number;
      max_megapixels: number;
      max_total_gb: number;
      thumb_px: number;
      accepted_tokens: string[];
    };
    subjects_file: string;
    commands: string[];
    equivalent_cli: string;
    excluded: { reason: string; count: number }[];
    rerun: Record<string, number>;
  };
  summary: {
    total: number;
    images: number;
    by_verdict: Record<Verdict, number>;
    by_category: Record<string, number>;
    by_licence: { licence: string; count: number; accepted: boolean }[];
    by_subject: { name: string; folder: string; category: string; search: string; total: number; by_verdict: Partial<Record<Verdict, number>> }[];
  };
  records: TapeRecord[];
}

export const tape = raw as unknown as Tape;

export const VERDICTS: { key: Verdict; label: string; blurb: string }[] = [
  { key: "landed", label: "Landed", blurb: "PD or CC0 licence; downloaded, checked and stored with its provenance." },
  { key: "quarantined", label: "Quarantined", blurb: "Licence is not PD or CC0. Recorded, never downloaded." },
  { key: "not-jpeg-png", label: "Not JPEG/PNG", blurb: "Skipped by the Commons downloader before the licence check." },
  { key: "duplicate", label: "Duplicate", blurb: "Same SHA256 as a file already in the pool; the copy is deleted." },
  { key: "failed", label: "Failed", blurb: "Licence passed, but the download or decode failed." },
  { key: "skipped", label: "Already recorded", blurb: "Quarry already had a row for this file, so it did nothing." },
];

export const verdictLabel = (v: Verdict) => VERDICTS.find((d) => d.key === v)?.label ?? v;

export const CATEGORIES = ["wardrobe", "setting", "concept"] as const;

/** The list filter, as carried in the URL: `?status=<verdict>&category=<category>`. Unknown values mean "all". */
export interface Filter {
  status: Verdict | null;
  category: string | null;
}

export const ALL: Filter = { status: null, category: null };

export function parseFilter(params: { get(name: string): string | null } | null | undefined): Filter {
  const status = params?.get("status") ?? null;
  const category = params?.get("category") ?? null;
  return {
    status: VERDICTS.some((v) => v.key === status) ? (status as Verdict) : null,
    category: category && tape.records.some((r) => r.category === category) ? category : null,
  };
}

export function filterQuery(filter: Filter): string {
  const q = new URLSearchParams();
  if (filter.status) q.set("status", filter.status);
  if (filter.category) q.set("category", filter.category);
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** The fields the list and the step nav need: small enough to hand to a client component. */
export interface RecordSummary {
  id: string;
  title: string;
  subject: string;
  category: string;
  verdict: Verdict;
  licence: string;
  image: TapeRecord["image"];
}

export const summaries = (records: TapeRecord[] = tape.records): RecordSummary[] =>
  records.map((r) => ({ id: r.id, title: r.title, subject: r.subject, category: r.category, verdict: r.verdict, licence: r.licence.raw, image: r.image }));

export const matches = (r: { verdict: Verdict; category: string }, f: Filter) =>
  (!f.status || r.verdict === f.status) && (!f.category || r.category === f.category);

export const recordHref = (id: string, filter: Filter = ALL) => `/record/${id}/${filterQuery(filter)}`;
export const listHref = (filter: Filter = ALL) => `/records/${filterQuery(filter)}`;

/** Where `id` sits in the filtered list, and its neighbours (wrapping at both ends). Falls back to the whole tape
 *  when the record is not in the filter (a hand-edited URL), so Prev and Next always go somewhere sensible. */
export function position(items: { id: string; verdict: Verdict; category: string }[], id: string, filter: Filter) {
  let used = filter;
  let list = items.filter((r) => matches(r, filter));
  if (!list.some((r) => r.id === id)) {
    used = ALL;
    list = items;
  }
  const at = list.findIndex((r) => r.id === id);
  if (at < 0) return null;
  const n = list.length;
  return { filter: used, n: at + 1, total: n, prev: list[(at - 1 + n) % n].id, next: list[(at + 1) % n].id };
}

export const subjectName = (folder: string) => folder.replace(/_/g, " ");

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

export const dims = (w: number | null | undefined, h: number | null | undefined) => (w && h ? `${w} × ${h}` : "—");

/** The title without the file extension, for headings. */
export const displayTitle = (title: string) => title.replace(/\.(jpe?g|png|tiff?|gif|webp)$/i, "");
