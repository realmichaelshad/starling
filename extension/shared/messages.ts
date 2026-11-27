/**
 * Runtime message names between the extension's own contexts.
 *
 * Kept apart from shared/api-routes.ts (which maps them onto gateway routes and
 * imports the HTTPS transport) so content scripts can name a message without
 * pulling network code into x.com's renderer.
 */

/** Messages content scripts and the onboarding page may send the background. */
export const MESSAGE_KINDS = {
  getBridgeState: "starling:get-bridge-state",
  refreshSession: "starling:refresh-session",
  assist: "starling:assist",
  /** Stop a one-shot ✦ request by the `requestId` it was sent with; the worker also tells the server. */
  assistCancel: "starling:assist-cancel",
  captureProfile: "starling:capture-profile-snapshot",
  captureMetric: "starling:capture-metric-snapshot",
  recordOutcome: "starling:record-outcome",
  imageFeedback: "starling:image-feedback",
  confirmPreference: "starling:confirm-preference",
  ownMaterial: "starling:own-material",
  beginSignIn: "starling:begin-sign-in",
  signOut: "starling:sign-out",
  consent: "starling:consent",
  openSite: "starling:open-site",
  /** Bring an X tab forward (reloading one that has no Starling in it yet), or open x.com/home. */
  openX: "starling:open-x",
} as const;

/** Messages the background sends to content scripts (and extension pages). */
export const BROADCAST_KINDS = {
  bridgeState: "starling:bridge-state",
  toggleSummon: "starling:toggle-summon",
  pagePreferenceChanged: "starling:page-preference-changed",
  settingsChanged: "starling:settings-changed",
  /** "Is Starling running in this tab?" The content script answers `{ ok: true }`. */
  ping: "starling:ping",
} as const;

/** Kinds from the local product that no longer exist. Listed so a test can prove they stay gone. */
export const REMOVED_MESSAGE_KINDS = [
  "starling:capture-trend-snapshot",
  "starling:model-config",
  "starling:bootstrap",
  "starling:sync-now",
  "starling:sync-status",
  "starling:sync-complete",
  "starling:debug-outline",
  "starling:sync-run",
  "starling:sync-news",
  "starling:get-identity",
  "starling:can-self-update",
] as const;
