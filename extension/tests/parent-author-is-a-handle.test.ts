import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * `parent_author` must carry the @handle, never the display name — in BOTH
 * writers.
 *
 * There are two places that fill this field and they disagreed for months:
 *
 *   :1195  the PASSIVE snapshot writer  — repaired long ago, uses `authorHandle`
 *   :1028  the VERIFICATION writer      — took `proof.context.author`, the NAME
 *
 * Measured on the real store: the only values ever written were 'Grok',
 * 'Sam Altman' and 'Tibo'. Every consumer reads the field as a handle —
 * `relationships.reply_target` calls `.lstrip("@")` on it, which is meaningless
 * on a name with a space in it — and `author_relationships.display_name` sat
 * NULL while the name it wanted was in the primary key.
 *
 * THE PART WORTH KEEPING: the repaired writer fires 0 times out of 4 on real
 * replies (its parent extraction is gated to /status/ pages) and the unrepaired
 * one fires 3 of 3. **The fix landed on the path that never runs, so the defect
 * survived its own repair.** A rebuild carrying only :1195 would have produced
 * no observable change and been recorded as confirming the fix.
 *
 * THIS IS A STRUCTURAL GUARD, NOT A BEHAVIOURAL ONE. The merge is inline in a
 * large content-script function with no seam to call, and Chrome was not
 * available to drive the real path when this was written. It asserts the shape
 * of the source, which is enough to stop the two writers diverging again — and
 * it is deliberately not claimed as proof that the field arrives correct.
 */
const SOURCE = readFileSync(
  fileURLToPath(new URL("../entrypoints/content.ts", import.meta.url)), "utf8");

/** Source with comment lines removed — this file's own prose explains the bug,
 *  and a scan that reads comments finds the thing it is checking for. That trap
 *  has caught five source scans in this project. */
const CODE = SOURCE.split("\n")
  .filter(line => !line.trim().startsWith("//") && !line.trim().startsWith("*"))
  .join("\n");

describe("parent_author carries a handle in both writers", () => {
  it("the verification writer reads author_handle, not author", () => {
    expect(CODE).toContain("parent_author: proof.context?.author_handle");
    expect(CODE).not.toContain("parent_author: proof.context?.author ");
    expect(CODE).not.toContain("parent_author: proof.context?.author\n");
  });

  it("the verification writer strips the leading @", () => {
    // `PostContext.author_handle` is built as `@${handle}`; the passive writer
    // stores it stripped. Two writers, one shape.
    const line = CODE.split("\n").find(l => l.includes("parent_author: proof.context?.author_handle"));
    expect(line).toBeDefined();
    expect(line).toContain('replace(/^@/, "")');
  });

  it("the passive writer still uses the parsed handle", () => {
    expect(CODE).toContain("parent_author: parent?.authorHandle");
  });

  it("the display name is kept beside the handle, not instead of it", () => {
    expect(CODE).toContain("parent_display_name: proof.context?.author");
  });

  it("no writer assigns a display name into parent_author", () => {
    const offenders = CODE.split("\n").filter(line =>
      /parent_author\s*:/.test(line) &&
      !/author_handle|authorHandle|snapshot\.metadata\.parent_author/.test(line));
    expect(offenders).toEqual([]);
  });
});
