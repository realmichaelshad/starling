import { describe, expect, it } from "vitest";
import { parseCompactCount } from "../shared/locale";

describe("counts are read correctly in every locale", () => {
  it("reads English grouping", () => {
    expect(parseCompactCount("1,717 Replies. Reply")).toBe(1717);
  });

  it("reads German/Spanish grouping — the bug that made 1.717 look like 2", () => {
    expect(parseCompactCount("1.717 Antworten. Antworten")).toBe(1717);
    expect(parseCompactCount("12.345 respuestas")).toBe(12345);
  });

  it("reads French space grouping, including NBSP", () => {
    expect(parseCompactCount("1 717 réponses")).toBe(1717);
    expect(parseCompactCount("1\u00a0717 réponses")).toBe(1717);
  });

  it("keeps compact magnitudes in both decimal conventions", () => {
    expect(parseCompactCount("1.7K Replies")).toBe(1700);
    expect(parseCompactCount("1,7 Tsd. Antworten")).toBe(1700);
    expect(parseCompactCount("2.4M Replies")).toBe(2_400_000);
  });

  it("handles plain and absent counts", () => {
    expect(parseCompactCount("300 Replies. Reply")).toBe(300);
    expect(parseCompactCount("Reply")).toBeUndefined();
    expect(parseCompactCount("")).toBeUndefined();
  });
});
