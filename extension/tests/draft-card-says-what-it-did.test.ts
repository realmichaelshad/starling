import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * The card after a rewrite says what changed and names the bar it missed.
 *
 * HIS COMPLAINT: "the enhance post thing needs to be better, it told me to
 * review it myself, like wtf." The card's entire prose was one fixed sentence —
 * "Typed into your reply. Review it, then post it yourself." — and it appeared
 * at TWO draft-success sites, not one.
 *
 * Measured on his real corpus: of 60 candidates against real posts, **24 (40%)
 * carried `review_note` + `needs_review`**, and on every one of them the card
 * showed the same eleven words a clean draft gets. Seven were the unbacked-claim
 * form, where the note quotes the exact sentence it cannot support — in one
 * case the draft's entire opening line.
 *
 * The companion has always computed and transmitted this. Nothing read it.
 *
 * STRUCTURAL GUARD, and the boundary is the point: Chrome was disconnected when
 * this was written, so there is no rendered capture behind it. It pins the
 * source shape — one renderer, both sites, the right fields, the excluded ones
 * excluded. It does NOT prove the line appears, is legible, or is worded well.
 * That verification is one browser capture away and is recorded as OPEN.
 */
const SOURCE = readFileSync(
  fileURLToPath(new URL("../entrypoints/content.ts", import.meta.url)), "utf8");

/** Comment lines stripped — this file's own prose names the string it forbids,
 *  and a scan that reads comments finds the thing it is checking for. */
const CODE = SOURCE.split("\n")
  .filter(line => !line.trim().startsWith("//") && !line.trim().startsWith("*")
    && !line.trim().startsWith("/*"))
  .join("\n");

describe("the draft card explains itself", () => {
  it("the sentence he complained about is gone from every site", () => {
    expect(CODE).not.toContain("Review it, then post it yourself");
    expect(CODE).not.toContain("Review and post it yourself");
  });

  it("every draft-success site calls the one renderer", () => {
    const calls = CODE.split("\n").filter(l => l.includes("appendDraftReason("));
    // ✦, the composer, the chat's reply/quote draft and the chat's answer, plus the declaration.
    // The chat paths used to skip it, so a chat draft's sources and notes were never shown.
    expect(calls.length).toBe(5);
    expect(CODE).toContain("function appendDraftReason(");
    const chat = CODE.slice(CODE.indexOf("function renderChatResult("), CODE.indexOf("function renderDecisionCard("));
    expect(chat.split("\n").filter(l => l.includes("appendDraftReason(")).length).toBe(2);
    expect(chat).toContain("response.research?.sources");
  });

  it("renders the rationale and the review note", () => {
    expect(CODE).toContain("decision.rationale");
    expect(CODE).toContain("decision.review_note");
  });

  it("does not print the note twice when the rationale already carries it", () => {
    // `_rationale_text` composes [weakness, what-it-did] upstream, so an
    // equality test would not catch the overlap — it has to be containment.
    expect(CODE).toContain("!rationale.includes(note)");
  });

  it("uses needs_review as a signal, not as another sentence", () => {
    expect(CODE).toContain('decision.needs_review ? "wait-reason needs-review"');
    expect(CODE).toContain(".wait-reason.needs-review");
  });

  it("keeps the commentary fields off the card", () => {
    // The test is "does this change whether he posts?". These do not, and his
    // other complaint is that the card talks at him. `likely_reaction` is
    // required in types.ts and read by nothing but tests.
    const renderer = CODE.slice(CODE.indexOf("function appendDraftReason("),
                               CODE.indexOf("function offerDraftCopy("));
    for (const dead of ["likely_reaction", "downside", "score_band",
                        "growth_score", "thread_temperature", "image_decision"]) {
      expect(renderer).not.toContain(dead);
    }
  });

  it("says so when the draft was written without a web check", () => {
    // The one field of the five proposed for this card that changes whether he
    // posts. On his real contexts the facts reached the writer on 6% of replies
    // and 71% of quotes; the companion's own comment calls that skip "visible"
    // and nothing rendered it.
    const renderer = CODE.slice(CODE.indexOf("function appendDraftReason("),
                               CODE.indexOf("function offerDraftCopy("));
    expect(renderer).toContain("decision.web_checked === false");
    expect(renderer).toContain("No web check on this one");
  });

  it("never badges a draft as web-checked", () => {
    // A positive label credentials the draft the way a quality score would —
    // and he should check a named fact either way. Only the SKIP is shown, so
    // an identity test on the flag must not appear.
    const renderer = CODE.slice(CODE.indexOf("function appendDraftReason("),
                               CODE.indexOf("function offerDraftCopy("));
    expect(renderer).not.toContain("web_checked === true");
    expect(renderer).not.toContain("Web checked");
    expect(renderer).not.toContain("Web-checked");
  });

  it("the web note never displaces the bar the draft missed", () => {
    // `review_note` is unshifted to the front; the provenance caution is
    // pushed to the back. A reader must hit the weakness first.
    const renderer = CODE.slice(CODE.indexOf("function appendDraftReason("),
                               CODE.indexOf("function offerDraftCopy("));
    expect(renderer.indexOf("parts.unshift(note)"))
      .toBeLessThan(renderer.indexOf("No web check on this one"));
  });

  it("the Why? detail does not repeat a rationale the card is already showing", () => {
    // PANEL-1, from a product walkthrough. A non-actionable card prints the rationale
    // as the standing `wait-reason` paragraph; the Why? detail printed it a
    // second time. Both halves are pinned because either alone leaves a hole:
    // the gate stops an empty Why?, the ternary stops the doubling.
    expect(CODE).toContain("decision.reasons?.length || (actionable && decision.rationale)");
    expect(CODE).toContain("[actionable ? decision.rationale : null, reasons]");
  });

  it("an actionable card still gets its rationale in the detail", () => {
    // The asymmetry is the reason this is two edits and not a deletion: on an
    // actionable card the draft is the anchor and nothing is standing, so the
    // Why? detail is the rationale's ONLY home. A fix that dropped the
    // rationale outright would have silently emptied it there.
    const renderer = CODE.slice(CODE.indexOf("const actionable ="),
                               CODE.indexOf("function appendDraftReason("));
    expect(renderer).toContain("actionable ? decision.rationale : null");
    expect(renderer).toContain("if (!actionable && decision.rationale) {");
  });

  it("does not duplicate the already-replied explanation", () => {
    // That branch puts its own reason in the status line; adding the rationale
    // under it would say the same thing twice.
    expect(CODE).toContain("if (!decision.already_replied) appendDraftReason(");
  });
});
