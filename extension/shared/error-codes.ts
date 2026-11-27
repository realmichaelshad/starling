/**
 * Every error code the extension can show: the gateway's (CONTRACTS section
 * 6.3) and the failures only the browser can observe.
 *
 * Kept apart from shared/transport.ts so the content script and the dock copy
 * can name codes without pulling network code into x.com's renderer.
 */

export const SERVER_ERROR_CODES = [
  "bad_request", "signed_out", "limit_reached", "payment_required", "fair_use_exceeded",
  "email_unverified", "consent_required", "account_suspended", "not_found",
  "busy", "duplicate_request", "idempotency_mismatch", "payload_too_large",
  "extension_outdated", "rate_limited", "server_error", "ai_unavailable", "service_paused",
] as const;

/** Failures only the browser can observe; the gateway never sends these as HTTP errors. */
export const CLIENT_ERROR_CODES = [
  "offline", "network", "timeout", "cancelled", "interrupted", "protocol_error", "not_configured",
] as const;

export type ServerErrorCode = typeof SERVER_ERROR_CODES[number];
export type ClientErrorCode = typeof CLIENT_ERROR_CODES[number];
export type ErrorCode = ServerErrorCode | ClientErrorCode;

export const ERROR_CODES: readonly ErrorCode[] = [...SERVER_ERROR_CODES, ...CLIENT_ERROR_CODES];

export function isErrorCode(value: unknown): value is ErrorCode {
  return typeof value === "string" && (ERROR_CODES as readonly string[]).includes(value);
}
