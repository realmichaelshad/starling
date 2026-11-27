import type { AssistResponse, BrowserAssistRequest, ComposerAction } from "./types";

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null;
}

function isOptionalString(value: unknown, maxLength: number): boolean {
  return value === undefined || (typeof value === "string" && value.length <= maxLength);
}

function isScheduleCommand(value: unknown): boolean {
  if (!isRecord(value) || typeof value.op !== "string") return false;
  switch (value.op) {
    case "save":
      return typeof value.text === "string"
        && value.text.trim().length > 0
        && value.text.length <= 8_000
        && typeof value.target_at === "string"
        && value.target_at.length > 0
        && value.target_at.length <= 64
        && isOptionalString(value.target_label, 120)
        && isOptionalString(value.topic, 200)
        && isOptionalString(value.freshness, 16)
        && isOptionalString(value.reason, 4_000)
        && (value.image === undefined || value.image === null || isRecord(value.image));
    case "list":
    case "list_due":
      return true;
    case "mark_posted":
    case "dismiss":
      return typeof value.draft_id === "number"
        && Number.isInteger(value.draft_id)
        && value.draft_id > 0;
    default:
      return false;
  }
}

/** CONTRACTS section 6.1: the hosted service accepts chat messages up to this length. */
export const CHAT_MESSAGE_MAX_CHARS = 2_000;

/**
 * The extension's last guard before a request leaves the browser. It
 * intentionally validates only the shape that controls an action; the server
 * remains the authority for full schema and business validation.
 */
export function isBrowserAssistRequest(value: unknown): value is BrowserAssistRequest {
  if (!isRecord(value) || typeof value.mode !== "string") return false;
  if (value.context !== undefined && !isRecord(value.context)) return false;
  switch (value.mode) {
    case "context":
      return isRecord(value.context);
    case "chat":
      return typeof value.message === "string"
        && value.message.trim().length > 0
        && value.message.length <= CHAT_MESSAGE_MAX_CHARS
        && isOptionalString(value.conversation_id, 64);
    case "enhance_composer": {
      if (!isRecord(value.composer)
        || !["reply", "quote", "post"].includes(String(value.composer.action))
        || typeof value.composer.draft_text !== "string"
        || value.composer.draft_text.length > 25_000) return false;
      const intent = value.composer.intent;
      if (intent !== undefined && intent !== "create" && intent !== "enhance") return false;
      return Boolean(value.composer.draft_text.trim()) || intent === "create";
    }
    case "schedule":
      return isScheduleCommand(value.schedule);
    default:
      return false;
  }
}

/** A response that does not even carry an `ok` bit is never allowed to reach
 * action UI as if it were a real result. */
export function isAssistResponse(value: unknown): value is AssistResponse {
  return isRecord(value) && typeof value.ok === "boolean";
}

export function invalidAssistResponse(): AssistResponse {
  return { ok: false, error: "Starling couldn't read its own answer. Nothing in X changed." };
}

/** The engine's OWN words for why no draft arrived.
 *
 *  THIS IS WHERE "Starling could not finish this draft." CAME FROM. A withheld
 *  draft is not an error response: the engine returns `ok: true` with the
 *  reason written into `decision.rationale`, and sets `error` only when the
 *  request itself failed. `AssistDecision.rationale` is required by the type
 *  and `AssistResponse.error` is optional, so on every no-draft response the
 *  old `response.error || "…could not finish…"` read an absent field and fell
 *  through to the generic sentence — discarding a reason that was always
 *  present.
 *
 *  The engine had already done the work of telling those cases apart, and
 *  all four arrived here as the same eight words with no cause in them:
 *
 *    - out of clock  ("the model … answered too slowly to finish this draft")
 *    - gates rejected every candidate  ("none passed my own checks")
 *    - the model returned nothing at all
 *    - a near-duplicate of one already shown  (`_explain_if_withheld`)
 *
 *  Only the first is a machine problem, and the reader could not tell which
 *  one they had — so the same sentence sent them hunting a stopped model that
 *  was answering fine, which is the hunt this ends.
 *
 *  The mismatch case is kept separate on purpose. A draft for the wrong
 *  surface means the composer moved between the ask and the answer; the
 *  rationale on it describes a draft that DID succeed, so quoting it against
 *  an empty composer would be actively misleading.
 */
export function assistFailureReason(response: AssistResponse | undefined,
                                    expected?: ComposerAction): string {
  const explicit = response?.error?.trim();
  if (explicit) return explicit;
  const decision = response?.decision;
  const produced = decision?.native_intent;
  const draft = decision?.draft?.trim();
  // A DRAFT THAT EXISTS MAKES `rationale` THE WRONG FIELD TO QUOTE. On every
  // successful path the engine writes a note ABOUT THE DRAFT there —
  // "Written from scratch for this composer.", "Tightened your draft without
  // changing its point." — so if we are on the failure path anyway, repeating
  // it describes something the reader cannot see and reads as a non-sequitur
  // over an unchanged composer. `rationale` only explains a failure when the
  // draft was WITHHELD, which is exactly when there is no draft to describe.
  //
  // `expected` is optional so the stream reducer — which knows a response
  // failed but not which composer asked — can share this one answer to "why".
  // Two places deciding that separately is how one of them kept a sentence
  // with no cause in it long after the other was fixed.
  if (draft) {
    if (expected && produced && produced !== expected) {
      return `That draft came back as a ${produced} and this is the ${expected} box, so I left it alone. Try again from the composer you want.`;
    }
    return "A draft came back but it did not fit this composer, so I left your text alone. Try again.";
  }
  const rationale = decision?.rationale?.trim();
  if (rationale) return rationale;
  // Nothing said anything. `ok` with no draft, no rationale and no error is a
  // shape the engine is not supposed to be able to produce — every no-draft
  // path it has writes a rationale first. Name that, rather than inventing a
  // cause for a draft that was never explained.
  return "Starling came back without a draft. Try again.";
}
