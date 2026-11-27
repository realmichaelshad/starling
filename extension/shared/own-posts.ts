/**
 * "Add my posts from this page": which of the posts X has loaded are the signed-in person's own
 * writing, to add as writing samples in one press.
 *
 * A voice Starling can match needs the person's own posts, and typing samples one at a time is where
 * most people stop. On their own profile the posts are already on screen. Only posts whose author IS
 * the signed-in account count (a repost shows the original author, so it stays out), each once, long
 * enough to carry a voice, cut to the engine's per-sample limit. Nothing is read or sent until the
 * person presses the button, and it only adds writing samples, which the consent notice covers.
 */

export const OWN_POST_MIN_CHARS = 20;
/** backend/engine/app/own_material.py MAX_TEXT_CHARS */
export const OWN_POST_MAX_CHARS = 2_000;
/** Well under the engine's 60 unmetered requests a minute per account. */
export const OWN_POSTS_PER_PRESS = 40;

export interface VisiblePost {
  handle: string | null;
  text: string;
}

function normal(handle: string): string {
  return handle.trim().replace(/^@/, "").toLowerCase();
}

export function ownPostTexts(posts: VisiblePost[], signedIn: string, limit = OWN_POSTS_PER_PRESS): string[] {
  const me = normal(signedIn);
  if (!me) return [];
  const seen = new Set<string>();
  const texts: string[] = [];
  for (const post of posts) {
    if (texts.length >= limit) break;
    if (!post.handle || normal(post.handle) !== me) continue;
    const text = (post.text || "").replace(/\r\n/g, "\n").trim();
    if (text.length < OWN_POST_MIN_CHARS) continue;
    const key = text.replace(/\s+/g, " ").toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    texts.push(text.slice(0, OWN_POST_MAX_CHARS));
  }
  return texts;
}

/** The signed-in account's own profile (its posts, replies, highlights or articles tab), never another account or a single post. */
export function isOwnProfilePath(pathname: string, signedIn: string): boolean {
  const me = normal(signedIn);
  const parts = pathname.split("/").filter(Boolean);
  if (!me || !parts.length || parts[0].toLowerCase() !== me) return false;
  return parts.length === 1 || (parts.length === 2 && ["with_replies", "highlights", "articles"].includes(parts[1].toLowerCase()));
}

/** Refusals that mean "not yet" rather than "no": the account's one request slot is taken (a new
 * account's engine is still starting, or another press is running), the engine is not answering yet,
 * or the per-minute limit was reached. */
const RETRYABLE = new Set(["busy", "ai_unavailable", "rate_limited", "network_error"]);
export const IMPORT_RETRIES = 4;

/** How long to wait before trying one sample again, or null to give up on it: the server's own
 * Retry-After when it sends one (capped at 20 s), otherwise 2, 4, 8 and 16 seconds. */
export function importRetryDelayMs(code: string | null | undefined, retryAfterS: number | null | undefined,
                                   attempt: number): number | null {
  if (!code || !RETRYABLE.has(code) || attempt >= IMPORT_RETRIES) return null;
  if (typeof retryAfterS === "number" && Number.isFinite(retryAfterS) && retryAfterS > 0) {
    return Math.min(retryAfterS, 20) * 1000;
  }
  return 2000 * 2 ** attempt;
}

export function importSummary(added: number, already: number, refused: number): string {
  if (!added && !already && !refused) {
    return "No posts of yours are loaded on this page yet. Scroll down a little, then press again.";
  }
  const parts: string[] = [];
  if (added) parts.push(`Added ${added} of your posts.`);
  if (already) parts.push(`${already} ${already === 1 ? "was" : "were"} already there.`);
  if (refused) parts.push(`${refused} couldn't be added right now; press again in a minute.`);
  if (added) parts.push("Starling learns how you write from them.");
  return parts.join(" ");
}
