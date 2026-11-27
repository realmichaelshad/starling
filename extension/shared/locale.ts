/**
 * Reading X's numbers in any language.
 *
 * English "1,717" and German "1.717" are the same number, but a naive
 * parseFloat reads the German one as 1.717 and rounds it to 2 — so a
 * 1.7K-reply thread looked EMPTY to every non-English user, and the crowd
 * and thread-temperature reads that depend on it were silently wrong.
 */

/**
 * Magnitude words as X localizes them. A WHITELIST on purpose: capturing any
 * trailing letters would read "1.717 Antworten" as a suffixed number, and an
 * unknown word simply means "no magnitude" — the safe reading.
 */
const MAGNITUDES: Record<string, number> = {
  k: 1e3, tsd: 1e3, mil: 1e3, tys: 1e3, tis: 1e3, dus: 1e3,
  m: 1e6, mio: 1e6, mln: 1e6, mn: 1e6,
  b: 1e9, mrd: 1e9, bn: 1e9, bil: 1e9,
};

/**
 * Parse a count out of an aria-label or metric string, locale-independently.
 *
 * Rules, in order:
 *  - spaces (including NBSP/narrow NBSP) are grouping separators: strip them
 *  - with BOTH `,` and `.` present, the LAST one is the decimal separator
 *  - with only one separator, three trailing digits and no magnitude suffix
 *    mean grouping ("1.717"); otherwise it is a decimal ("1,7 Tsd.")
 */
export function parseCompactCount(raw: string): number | undefined {
  const match = String(raw || "").match(/(\d[\d.,   \s]*)\s*([A-Za-zÀ-ÿ]{1,4})?\.?/);
  if (!match) return undefined;
  let digits = match[1].replace(/[\s  ]/g, "");
  const word = (match[2] || "").toLowerCase();
  const suffix = word in MAGNITUDES ? word : "";
  const lastComma = digits.lastIndexOf(",");
  const lastDot = digits.lastIndexOf(".");
  if (lastComma >= 0 && lastDot >= 0) {
    const decimal = lastComma > lastDot ? "," : ".";
    const grouping = decimal === "," ? "." : ",";
    digits = digits.split(grouping).join("").replace(decimal, ".");
  } else if (lastComma >= 0 || lastDot >= 0) {
    const index = Math.max(lastComma, lastDot);
    const tail = digits.slice(index + 1);
    digits = /^\d{3}$/.test(tail) && !suffix
      ? digits.slice(0, index) + tail          // grouping: 1.717 -> 1717
      : digits.slice(0, index) + "." + tail;   // decimal:  1,7K  -> 1.7K
  }
  const count = parseFloat(digits);
  if (!Number.isFinite(count)) return undefined;
  const scale = MAGNITUDES[suffix] ?? 1;
  return Math.round(count * scale);
}
