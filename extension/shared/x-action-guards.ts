export type NativePurpose = "reply" | "repost_menu" | "quote" | "compose";

export interface XControlDescriptor {
  testId?: string | null;
  role?: string | null;
  label?: string | null;
  composeLink?: boolean;
}

export type ComposerIntent = "reply" | "quote" | "post";

export interface ComposerCandidateDescriptor {
  /** The element existed before Starling opened a native X flow. */
  wasVisibleBefore: boolean;
  /** Only reply composers nested in the chosen post are safe to reuse. */
  belongsToTargetPost?: boolean;
}

// This is intentionally an allowlist. Any unfamiliar or final-submit control
// fails closed, which is safer than attempting to keep up with X DOM changes.
const FORBIDDEN = /(?:tweetButton|postButton|replyButton|delete|like|unlike|follow|unfollow|message|dm|mediaUpload|addMedia)/i;

export function isAllowedNativeClick(control: XControlDescriptor, purpose: NativePurpose): boolean {
  const testId = control.testId || "";
  const label = (control.label || "").trim();
  if (FORBIDDEN.test(testId)) return false;
  if (purpose === "reply") return testId === "reply";
  if (purpose === "repost_menu") return /^(retweet|unretweet)$/i.test(testId);
  if (purpose === "quote") return control.role === "menuitem" && /^quote$/i.test(label);
  // X implements its Quote menu item as an <a href="/compose/post" role="menuitem">.
  // That is a quote action, not the global new-post control; let the caller's
  // quote branch claim it instead of silently binding the modal as a post.
  return testId === "SideNav_NewTweet_Button" || (control.composeLink === true && control.role !== "menuitem");
}

/**
 * Never fall back to an arbitrary composer that was already on the page.
 * A fresh composer is causally tied to the exact allowlisted control we just
 * opened. The one exception is a reply box visibly nested in the selected
 * post, which is already an unambiguous matching destination.
 */
export function isSafeComposerCandidate(
  intent: ComposerIntent,
  candidate: ComposerCandidateDescriptor
): boolean {
  if (intent === "reply" && candidate.belongsToTargetPost) return true;
  return !candidate.wasVisibleBefore;
}

/**
 * If more than one composer could plausibly belong to one native X action,
 * refuse to guess. The caller can wait for the ambiguity to disappear, but it
 * must never bind an enhancement control to an arbitrary draft.
 */
export function selectSingleSafeComposerCandidate<T extends ComposerCandidateDescriptor>(
  intent: ComposerIntent,
  candidates: readonly T[]
): T | null {
  const safe = candidates.filter(candidate => isSafeComposerCandidate(intent, candidate));
  return safe.length === 1 ? safe[0] : null;
}

/** X's own compose route. Every modal composer opens at it: a new post, a quote and, on X in 2026, a reply. */
export function isXComposeRoute(pathname: string): boolean {
  return /^\/compose\/post\/?$/.test(pathname);
}

/**
 * X opens its composer modal by changing the SPA route to /compose/post: for a
 * quote, for a new post and (checked on x.com, 30 Oct 2026) for a reply too,
 * including the Reply button of the post on its own page. That navigation is
 * part of the exact allowlisted action; clearing the pending intent in between
 * leaves the newly opened box without the post it answers. Retain the intent
 * for this one expected transition only. Every other navigation still drops it,
 * so a later unrelated draft can never inherit it.
 */
export function shouldRetainComposerIntentOnNavigation(
  intent: ComposerIntent,
  fromUrl: string,
  toUrl: string
): boolean {
  try {
    const from = new URL(fromUrl);
    const to = new URL(toUrl);
    return !isXComposeRoute(from.pathname) && isXComposeRoute(to.pathname)
      && (intent === "post" || intent === "quote" || intent === "reply");
  } catch {
    return false;
  }
}

export interface ComposerPlacement {
  /** The composer sits inside an X dialog (the modal composer). */
  inDialog: boolean;
  /** An ancestor is `aria-hidden="true"`: X hides the page this way while its modal is open. */
  ariaHidden: boolean;
  /** The page is on X's compose route (`isXComposeRoute`). */
  composeRoute: boolean;
}

/**
 * Is this the page's own box, left mounted BEHIND X's modal composer?
 *
 * Found on x.com (30 Oct 2026): pressing Reply on a post opens X's modal at
 * /compose/post, and X re-mounts the post page's inline "Post your reply" box
 * behind it, as a brand-new element, about 50 ms before the modal's own box
 * exists. A brand-new reply box under the very post was exactly what Starling
 * looked for, so it bound that hidden box: the draft was written (and used one)
 * while the box the person was looking at stayed empty. While the modal is up,
 * only a box inside it is the one being written in.
 */
export function isBehindXModal({ inDialog, ariaHidden, composeRoute }: ComposerPlacement): boolean {
  if (inDialog) return false;
  return ariaHidden || composeRoute;
}

/**
 * A reply click waits this long for X's modal before a box that was already
 * open under the post may take the reply. X answers a Reply click in about
 * 150 ms; Starling's first look comes at about the same moment, which is how a
 * reply box about to be thrown away could win the race.
 */
export const REPLY_MODAL_GRACE_MS = 1_200;

/**
 * May a reply box that was already on the page (under the post) take a reply
 * that was just clicked? Only once X has had time to open its modal and has
 * not: a modal box, when there is one, is where the person is writing.
 */
export function mayReuseOpenReplyBox(intentAgeMs: number, modalComposerOpen: boolean): boolean {
  return !modalComposerOpen && intentAgeMs >= REPLY_MODAL_GRACE_MS;
}

export interface LauncherComposerCandidate {
  /** Composer lives inside the launcher's own collapsed surface. */
  insideSurface: boolean;
  /** The composer's nearest submit button is the launcher's submit button. */
  submitMatches: boolean;
  /** The composer was already visible when the launcher was clicked. */
  existedBefore: boolean;
}

/**
 * Binding for FIRST-PARTY launcher clicks (the user pressed Starling's own
 * "Create reply/post" pill). Unlike a native X click, the intent here is
 * certain — action, surface, and submit button are all known — so a composer
 * that is directly tied to that surface/submit binds outright even if it was
 * already mounted (X pre-mounts inline reply boxes BESIDE the post, which is
 * exactly the shape the strict native matrix fails closed on). When X swaps
 * the whole subtree instead, exactly one FRESH composer is accepted; any
 * remaining ambiguity still refuses to guess.
 */
export function selectLauncherComposer<T extends LauncherComposerCandidate>(
  candidates: readonly T[]
): T | null {
  const direct = candidates.filter(candidate => candidate.insideSurface || candidate.submitMatches);
  if (direct.length === 1) return direct[0];
  if (direct.length > 1) return null;
  const fresh = candidates.filter(candidate => !candidate.existedBefore);
  return fresh.length === 1 ? fresh[0] : null;
}
