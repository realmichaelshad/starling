/**
 * The data disclosure, and the consent that gates everything else.
 *
 * Chrome Web Store user-data policy: say plainly what is collected and get an
 * affirmative choice BEFORE anything is collected or sent. The single text
 * below is rendered by both the onboarding page and the dock, so the two can
 * never disagree, and a unit test pins CONSENT_VERSION to a SHA-256 of that
 * text: changing a word without bumping the version fails the suite.
 *
 * Pure: no chrome.*. The background worker owns the stored record.
 */

/** Bump together with DISCLOSURE_SHA256 whenever the disclosure text changes. */
export const CONSENT_VERSION = "2026-09-22.1";

/** SHA-256 (hex) of `disclosureText()` for CONSENT_VERSION. Checked by tests/consent.test.ts. */
export const DISCLOSURE_SHA256 = "6351c595e8830df0b57a00f218c2d7f0fabbadec15786fa9d1951841d2e378c8";

export interface DisclosureSection {
  heading: string;
  items: readonly string[];
}

export const DISCLOSURE = {
  title: "Before Starling starts",
  intro: "Starling drafts replies, quotes and posts on X in your voice. The drafts are written by Starling's hosted AI, so Starling has to send some information to its servers. Nothing is sent until you agree.",
  sections: [
    {
      heading: "What Starling sends",
      items: [
        "The post you ask about: its text, the author's name and handle, links, image addresses and descriptions, the counts X shows, and nearby posts in that conversation.",
        "The text in X's composer, and small copies of images attached there, when you press Create or Enhance.",
        "What you type or attach in the Starling panel, and posts of your own that you add from your X profile as writing samples.",
        "Short web searches, sent to the AI provider's web search: the subjects you write about, to find what is new in them (topic names only, never your own text), and, when a post you ask about makes a claim about recent events, a search built from that public post.",
        "Whether you used, edited or posted a draft Starling wrote.",
        "Your account and usage information.",
      ],
    },
    {
      heading: "Who receives it",
      items: [
        "Starling's servers, operated by the service owner.",
        "Starling's AI provider, which writes the drafts.",
      ],
    },
    {
      heading: "What Starling promises",
      items: [
        "Your information is not sold and is not used for advertising.",
        "You can export or delete your data at any time from the Data page of your account.",
        "Starling never presses Post, likes, follows or sends messages for you. You always press Post.",
        "Starling doesn't run on your direct messages.",
      ],
    },
  ] as readonly DisclosureSection[],
  passive: {
    label: "Also learn from my own profile and posts while I browse",
    help: "When this is on, Starling notes the counts on your own profile and your own posts as you browse X, so it can learn what works for you. You can turn it off in Settings.",
  },
  agree: "Agree and continue",
  decline: "Not now",
} as const;

/** The canonical text the version hash covers, in display order. */
export function disclosureText(): string {
  return [
    DISCLOSURE.title,
    DISCLOSURE.intro,
    ...DISCLOSURE.sections.flatMap(section => [section.heading, ...section.items]),
    DISCLOSURE.passive.label,
    DISCLOSURE.passive.help,
    DISCLOSURE.agree,
    DISCLOSURE.decline,
  ].join("\n");
}

export interface ConsentRecord {
  version: string;
  passive_learning: boolean;
  /** Epoch milliseconds. */
  accepted_at: number;
}

export function normalizeConsent(value: unknown): ConsentRecord | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.version !== "string" || !record.version) return null;
  return {
    version: record.version,
    passive_learning: record.passive_learning === true,
    accepted_at: typeof record.accepted_at === "number" && Number.isFinite(record.accepted_at) ? record.accepted_at : 0,
  };
}

/** Consent counts only for the disclosure this build shows. */
export function hasCurrentConsent(record: ConsentRecord | null | undefined): record is ConsentRecord {
  return Boolean(record && record.version === CONSENT_VERSION);
}

export function acceptConsent(passiveLearning: boolean, now: number): ConsentRecord {
  return { version: CONSENT_VERSION, passive_learning: passiveLearning, accepted_at: now };
}

/** The passive-learning switch; meaningless (null) without current consent. */
export function withPassiveLearning(record: ConsentRecord | null | undefined, on: boolean): ConsentRecord | null {
  return hasCurrentConsent(record) ? { ...record, passive_learning: on } : null;
}

/**
 * "main" covers everything the user asks for plus the post-confirmation check;
 * "passive" is capture nobody asked for in the moment (own profile snapshots,
 * own-post metrics) and needs the separate opt-in as well.
 */
export type ConsentNeed = "main" | "passive";

export function consentAllows(need: ConsentNeed, record: ConsentRecord | null | undefined): boolean {
  if (!hasCurrentConsent(record)) return false;
  return need === "main" || record.passive_learning;
}

/** Body of `POST /api/v1/consent`. */
export function consentBody(record: ConsentRecord): { version: string; passive_learning: boolean } {
  return { version: record.version, passive_learning: record.passive_learning };
}

export interface ServerConsent {
  required_version: string;
  accepted_version: string | null;
  passive_learning: boolean;
}

/** True when this browser's answer should be sent to the server again. */
export function serverConsentNeedsSync(record: ConsentRecord | null | undefined, server: ServerConsent | null | undefined): boolean {
  if (!hasCurrentConsent(record) || !server) return false;
  // A server asking for another version needs text this build cannot show.
  if (server.required_version && server.required_version !== CONSENT_VERSION) return false;
  return server.accepted_version !== CONSENT_VERSION || server.passive_learning !== record.passive_learning;
}
