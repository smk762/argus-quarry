import { describe, expect, it } from "vitest";

import { ALL, filterQuery, matches, parseFilter, position, recordHref, summaries, tape } from "./tape";

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

  it("counts every verdict", () => {
    const total = Object.values(tape.summary.by_verdict).reduce((a, b) => a + b, 0);
    expect(total).toBe(tape.records.length);
  });
});

describe("filters in the URL", () => {
  it("parses known values and drops unknown ones", () => {
    expect(parseFilter(params("status=landed&category=concept"))).toEqual({ status: "landed", category: "concept" });
    expect(parseFilter(params("status=nope&category=identity"))).toEqual(ALL);
    expect(parseFilter(null)).toEqual(ALL);
  });

  it("round-trips through the query string", () => {
    expect(filterQuery(ALL)).toBe("");
    expect(filterQuery({ status: "quarantined", category: null })).toBe("?status=quarantined");
    expect(recordHref("abc", { status: "landed", category: "setting" })).toBe("/record/abc/?status=landed&category=setting");
  });
});

describe("prev / next", () => {
  const items = summaries();
  it("steps through the filtered list and wraps", () => {
    const f = { status: "quarantined" as const, category: null };
    const list = items.filter((r) => matches(r, f));
    const first = position(items, list[0].id, f)!;
    expect(first.n).toBe(1);
    expect(first.total).toBe(list.length);
    expect(first.prev).toBe(list[list.length - 1].id);
    expect(first.next).toBe(list[1].id);
    expect(first.filter).toEqual(f);
  });

  it("falls back to the whole tape when the record is not in the filter", () => {
    const landed = items.find((r) => r.verdict === "landed")!;
    const pos = position(items, landed.id, { status: "quarantined", category: null })!;
    expect(pos.filter).toEqual(ALL);
    expect(pos.total).toBe(items.length);
  });
});
