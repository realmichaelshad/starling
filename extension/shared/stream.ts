import type { AssistPortEvent, AssistResponse } from "./types";
import { assistFailureReason } from "./assist-contract";

/**
 * Pure state machine for one streamed assist request.
 *
 * The transport (background port and the hosted gateway) feeds events in; the
 * UI renders from the resulting state. Keeping this free of chrome.* and DOM
 * APIs makes the whole streaming lifecycle unit-testable.
 */
export type StreamPhase = "starting" | "waiting" | "streaming" | "done" | "error";

export interface StreamState {
  requestId: string;
  phase: StreamPhase;
  /** Server-reported stage while waiting (accepted | queued | thinking | researching…). */
  stage: string;
  /** Accumulated display-only preview text. */
  preview: string;
  response?: AssistResponse;
  error?: string;
  /** An ErrorCode from shared/transport.ts, when the failure had one. */
  code?: string;
  retryAfterS?: number;
  requestRef?: string;
  /** Draft text reached the user before the failure. */
  exposed?: boolean;
  retryable: boolean;
  cancelled: boolean;
}

export function initialStreamState(requestId: string): StreamState {
  return { requestId, phase: "starting", stage: "starting", preview: "", retryable: false, cancelled: false };
}

export function reduceStream(state: StreamState, event: AssistPortEvent): StreamState {
  if (event.requestId !== state.requestId) return state;
  // Terminal states never regress: a late token after final/error is ignored.
  if (state.phase === "done" || state.phase === "error") return state;
  switch (event.type) {
    case "accepted":
      return { ...state, phase: "waiting", stage: state.stage === "starting" ? "accepted" : state.stage };
    case "status":
      return { ...state, phase: state.preview ? state.phase : "waiting", stage: event.stage };
    case "token":
      return { ...state, phase: "streaming", preview: state.preview + event.text };
    case "final": {
      const response = event.response;
      if (!response.ok) {
        return {
          ...state, phase: "error", response,
          // The server's own words, by the same rule the composer uses — a
          // failed response can still carry its reason in `decision.rationale`,
          // and this read `error` alone.
          error: assistFailureReason(response),
          retryable: !response.cancelled, cancelled: Boolean(response.cancelled),
          code: response.cancelled ? "cancelled" : state.code,
        };
      }
      return { ...state, phase: "done", response };
    }
    case "error":
      return {
        ...state, phase: "error", error: event.error, retryable: event.retryable,
        code: event.code, retryAfterS: event.retryAfterS, requestRef: event.requestRef,
        exposed: event.exposed ?? Boolean(state.preview),
        cancelled: event.code === "cancelled",
      };
    default:
      return state;
  }
}

export function markCancelled(state: StreamState): StreamState {
  if (state.phase === "done" || state.phase === "error") return state;
  return { ...state, phase: "error", error: "Stopped.", retryable: true, cancelled: true, code: "cancelled" };
}

/** Human-facing, stable copy for stream stages. Unknown stages still render as
 * an honest working state instead of leaving a blank loading card.
 *
 * The engine sends each of `reading_links`, `checking_web`, `finding_topics`, `writing` and
 * `checking` only when it is actually doing that step. `researching` is not sent any more; its
 * label stays so an older engine still reads sensibly. */
export function streamStageLabel(stage: string): string {
  const trimmed = stage.trim();
  switch (trimmed.toLowerCase()) {
    case "": return "Working…";
    case "starting":
    case "accepted": return "Sending to Starling…";
    case "queued": return "Starling is busy. You're next…";
    case "reconnecting": return "Reconnecting to Starling…";
    case "warming": return "Warming up. The first draft takes a few extra seconds…";
    case "thinking": return "Thinking through that…";
    case "researching": return "Checking the context…";
    case "reading_links": return "Reading the links…";
    case "checking_web": return "Checking the web…";
    case "finding_topics": return "Finding topics…";
    case "writing": return "Writing…";
    case "checking": return "Checking the draft…";
    default:
      // The server narrates its own substages in plain English ("trying
      // another angle (2/3)"). Show them, only tidied up; a stage name the
      // extension does not know yet ("new_stage") reads as words, not as a code.
      return trimmed[0].toUpperCase() + trimmed.slice(1).replace(/_/g, " ") + (/[.…!?]$/.test(trimmed) ? "" : "…");
  }
}

/**
 * What a stream the customer stopped after text had appeared turned out to cost, read from the one
 * event the server answers the stop with:
 *
 * - `used_after_stop`: the final says so (`used_after_stop: true`): the stop used a draft;
 * - `finished`: the draft was finished and usable before the stop reached the server, so it was used;
 * - `nothing`: any other final (cancelled, withheld, unchanged) or a server error: nothing was used.
 *
 * Null for events that settle nothing (a late token or status).
 */
export type StoppedOutcome = "used_after_stop" | "finished" | "nothing";

export function stoppedStreamOutcome(event: AssistPortEvent): StoppedOutcome | null {
  if (event.type === "error") return "nothing";
  if (event.type !== "final") return null;
  const response = event.response;
  if (response.used_after_stop === true) return "used_after_stop";
  if (response.ok !== true || response.withheld || response.unchanged || response.cancelled) return "nothing";
  const text = [response.decision?.draft, response.answer];
  return text.some(value => typeof value === "string" && value.trim().length > 0) ? "finished" : "nothing";
}

/** The draft/answer the UI should trust — ONLY from the gated final response. */
export function finalText(state: StreamState): string | undefined {
  if (state.phase !== "done" || !state.response) return undefined;
  return state.response.decision?.draft ?? state.response.answer ?? undefined;
}

let requestCounter = 0;

export function newRequestId(): string {
  requestCounter += 1;
  return `vf-${Date.now().toString(36)}-${requestCounter}`;
}
