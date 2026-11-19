import { describe, expect, it } from "vitest";

import { ALL, filterQuery, listHref, matches, only, parseFilter, position, recordHref, SUBJECTS, summaries, tape } from "./tape";

const params = (q: string) => new URLSearchParams(q);

describe("the tape", () => {
  it("has no identity record and images only for landed PD/CC0 records", () => {
    for (const r of tape.records) {
      expect(r.category).not.toBe("identity");
      if (r.image) {
        expect(r.verdict).toBe("landed");
        expect(["PD", "CC0"]).toContain(r.licence.normalised);
      }
    }
    expect(tape.records.filter((r) => r.image).length).toBe(tape.summary.images);
  });

  it("counts every verdict and every subject", () => {
    const total = Object.values(tape.summary.by_verdict).reduce((a, b) => a + b, 0);
    expect(total).toBe(tape.records.length);
    expect(tape.summary.by_subject.reduce((n, s) => n + s.total, 0)).toBe(tape.records.length);
    for (const s of SUBJECTS) expect(tape.records.some((r) => r.subject === s.folder)).toBe(true);
  });
});

describe("filters in the URL", () => {
  const subject = SUBJECTS[0].folder;

  it("parses known values and drops unknown ones", () => {
    expect(parseFilter(params("status=landed&category=concept"))).toEqual({ status: "landed", category: "concept", subject: null });
    expect(parseFilter(params(`subject=${subject}`))).toEqual(only({ subject }));
    expect(parseFilter(params("status=nope&category=identity&subject=Nobody"))).toEqual(ALL);
    expect(parseFilter(null)).toEqual(ALL);
  });

  it("round-trips through the query string", () => {
    expect(filterQuery(ALL)).toBe("");
    expect(filterQuery(only({ status: "quarantined" }))).toBe("?status=quarantined");
    const f = { status: "landed" as const, category: "setting", subject };
    expect(parseFilter(params(filterQuery(f).slice(1)))).toEqual(f);
    expect(recordHref("abc", f)).toBe(`/record/abc/?status=landed&category=setting&subject=${subject}`);
    expect(listHref(only({ subject }))).toBe(`/records/?subject=${subject}`);
  });

  it("combines subject with the other filters", () => {
    const f = only({ subject, status: "landed" });
    const hits = summaries().filter((r) => matches(r, f));
    expect(hits.length).toBeGreaterThan(0);
    expect(hits.every((r) => r.subject === subject && r.verdict === "landed")).toBe(true);
  });
});

describe("prev / next", () => {
  const items = summaries();

  it("steps through the filtered list and wraps", () => {
    const f = only({ status: "quarantined" });
    const list = items.filter((r) => matches(r, f));
    const first = position(items, list[0].id, f)!;
    expect(first.n).toBe(1);
    expect(first.total).toBe(list.length);
    expect(first.prev).toBe(list[list.length - 1].id);
    expect(first.filter).toEqual(f);
  });

  it("keeps the subject filter", () => {
    const subject = SUBJECTS[SUBJECTS.length - 1].folder;
    const f = only({ subject });
    const list = items.filter((r) => r.subject === subject);
    const pos = position(items, list[1].id, f)!;
    expect(pos.total).toBe(list.length);
    expect(pos.prev).toBe(list[0].id);
    expect(pos.next).toBe(list[2].id);
    expect(recordHref(pos.next, pos.filter)).toBe(`/record/${list[2].id}/?subject=${subject}`);
  });

  it("falls back to the whole tape when the record is not in the filter", () => {
    const landed = items.find((r) => r.verdict === "landed")!;
    const pos = position(items, landed.id, only({ status: "quarantined" }))!;
    expect(pos.filter).toEqual(ALL);
    expect(pos.total).toBe(items.length);
  });
});
