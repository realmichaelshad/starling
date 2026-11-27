/**
 * Which gateway route each background message becomes (CONTRACTS section 6.1).
 *
 * One literal table: a payload decides what is SENT, never where it goes, and a
 * kind the hosted build no longer supports has no route at all.
 */
import { isBrowserAssistRequest } from "./assist-contract";
import { isSitePage, type SitePage } from "./config";
import type { ConsentNeed } from "./consent";
import { MESSAGE_KINDS } from "./messages";
import { TIMEOUT_MS, type RetryClass } from "./transport";

export type HttpMethod = "GET" | "POST" | "DELETE";

export interface ApiRoute {
  method: HttpMethod;
  /** Relative to API_BASE. */
  path: string;
  body?: unknown;
  timeoutMs: number;
  retry: RetryClass;
  /** Which consent the request needs before it may leave the browser. */
  consent: ConsentNeed;
  /** Every route carries the device token except the token exchange. */
  auth: "device" | "none";
}

export type RouteResult =
  | { ok: true; route: ApiRoute }
  | { ok: false; code: "bad_request" | "unknown_kind"; message: string };

export { BROADCAST_KINDS, MESSAGE_KINDS, REMOVED_MESSAGE_KINDS } from "./messages";

export const OWN_MATERIAL_ID = /^[A-Za-z0-9_-]{1,64}$/;

const MATERIAL_KINDS = new Set(["voice", "background"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function bad(message: string): RouteResult {
  return { ok: false, code: "bad_request", message };
}

function route(method: HttpMethod, path: string, extra: Partial<ApiRoute> = {}): RouteResult {
  return {
    ok: true,
    route: { method, path, timeoutMs: TIMEOUT_MS.standard, retry: "none", consent: "main", auth: "device", ...extra },
  };
}

export const SESSION_ROUTE: ApiRoute = {
  method: "GET", path: "/session", timeoutMs: TIMEOUT_MS.standard, retry: "session", consent: "main", auth: "device",
};

export interface RoutableMessage {
  kind: string;
  payload?: unknown;
  /** For metric snapshots: "publication" confirms a draft the user posted; anything else is passive. */
  purpose?: unknown;
}

/** The network route for a message, or why it has none. */
export function apiRouteFor(message: RoutableMessage): RouteResult {
  const payload = message.payload;
  switch (message.kind) {
    case MESSAGE_KINDS.getBridgeState:
    case MESSAGE_KINDS.refreshSession:
      return { ok: true, route: SESSION_ROUTE };

    case MESSAGE_KINDS.assist: {
      if (!isBrowserAssistRequest(payload)) return bad("Starling could not read that request.");
      if (payload.mode !== "context" && payload.mode !== "schedule") {
        return bad("That kind of request has to be streamed.");
      }
      return route("POST", "/assist", { body: payload, timeoutMs: TIMEOUT_MS.assist, retry: "assist" });
    }

    case MESSAGE_KINDS.captureProfile:
      if (!isRecord(payload)) return bad("Nothing to capture.");
      return route("POST", "/snapshots/profile", { body: payload, retry: "capture", consent: "passive" });

    case MESSAGE_KINDS.captureMetric:
      if (!isRecord(payload) || typeof payload.post_id !== "string") return bad("Nothing to capture.");
      return route("POST", "/snapshots/metrics", {
        body: payload, retry: "capture", consent: message.purpose === "publication" ? "main" : "passive",
      });

    case MESSAGE_KINDS.recordOutcome:
      if (!isRecord(payload) || typeof payload.generation_id !== "string") return bad("Nothing to record.");
      return route("POST", "/outcomes", { body: payload, retry: "capture" });

    case MESSAGE_KINDS.imageFeedback:
      if (!isRecord(payload) || typeof payload.generation_id !== "string") return bad("Nothing to record.");
      return route("POST", "/images/feedback", { body: payload, retry: "capture" });

    case MESSAGE_KINDS.confirmPreference:
      if (!isRecord(payload)) return bad("Nothing to save.");
      return route("POST", "/preferences/confirm", { body: payload });

    case MESSAGE_KINDS.ownMaterial:
      return ownMaterialRoute(payload);

    case MESSAGE_KINDS.consent: {
      const action = parseConsentAction(payload);
      if (!action || action.action === "get") return bad("Consent is read locally.");
      if (action.action === "withdraw") return route("POST", "/consent/withdraw");
      return bad("Consent is sent with its version by the background.");
    }

    case MESSAGE_KINDS.signOut:
      return route("POST", "/devices/current/revoke", { timeoutMs: TIMEOUT_MS.revoke });

    default:
      return { ok: false, code: "unknown_kind", message: `No route for ${message.kind}.` };
  }
}

function ownMaterialRoute(payload: unknown): RouteResult {
  if (!isRecord(payload)) return bad("Unknown settings action.");
  const body = isRecord(payload.body) ? payload.body : {};
  switch (payload.action) {
    case "list":
      return route("GET", "/own-material", { retry: "session" });
    case "add": {
      const text = typeof body.text === "string" ? body.text.trim() : "";
      if (!MATERIAL_KINDS.has(String(body.kind)) || !text || text.length > 4_000) {
        return bad("Add a short piece of writing or a note.");
      }
      return route("POST", "/own-material", { body: { kind: body.kind, text } });
    }
    case "remove": {
      const id = typeof body.id === "string" ? body.id : "";
      if (!OWN_MATERIAL_ID.test(id)) return bad("That item can't be removed.");
      return route("DELETE", `/own-material/${id}`);
    }
    default:
      return bad("Unknown settings action.");
  }
}

/** `POST /consent`: the version and the passive-learning choice. */
export function consentRoute(body: { version: string; passive_learning: boolean }): ApiRoute {
  return { method: "POST", path: "/consent", body, timeoutMs: TIMEOUT_MS.standard, retry: "none", consent: "main", auth: "device" };
}

/** `POST /devices/token`: the only route without a device token. */
export function tokenExchangeRoute(body: unknown): ApiRoute {
  return { method: "POST", path: "/devices/token", body, timeoutMs: TIMEOUT_MS.standard, retry: "none", consent: "main", auth: "none" };
}

/** Streamed modes: chat and enhance_composer. */
export function assistStreamRoute(payload: unknown): RouteResult {
  if (!isBrowserAssistRequest(payload)) return bad("Starling could not read that request.");
  if (payload.mode !== "chat" && payload.mode !== "enhance_composer") {
    return bad("That kind of request is not streamed.");
  }
  return route("POST", "/assist/stream", { body: payload, timeoutMs: TIMEOUT_MS.streamTotal });
}

const IDEMPOTENCY_KEY = /^[A-Za-z0-9-]{8,64}$/;

export function cancelRoute(key: string): ApiRoute | null {
  return IDEMPOTENCY_KEY.test(key)
    ? { method: "POST", path: `/requests/${key}/cancel`, timeoutMs: TIMEOUT_MS.standard, retry: "none", consent: "main", auth: "device" }
    : null;
}

export function replayRoute(key: string): ApiRoute | null {
  return IDEMPOTENCY_KEY.test(key)
    ? { method: "GET", path: `/requests/${key}`, timeoutMs: TIMEOUT_MS.standard, retry: "none", consent: "main", auth: "device" }
    : null;
}

/** `starling:open-site {page}` opens only allowlisted website pages. */
export function openSiteTarget(payload: unknown): SitePage | null {
  return isRecord(payload) && isSitePage(payload.page) ? payload.page : null;
}

export type ConsentAction =
  | { action: "get" }
  | { action: "accept"; passive_learning: boolean }
  | { action: "set-passive"; passive_learning: boolean }
  | { action: "withdraw" };

export function parseConsentAction(payload: unknown): ConsentAction | null {
  if (!isRecord(payload)) return null;
  switch (payload.action) {
    case "get":
      return { action: "get" };
    case "withdraw":
      return { action: "withdraw" };
    case "accept":
    case "set-passive":
      return typeof payload.passive_learning === "boolean"
        ? { action: payload.action, passive_learning: payload.passive_learning }
        : null;
    default:
      return null;
  }
}
