# Starling release QA

Run `pnpm typecheck`, `pnpm test`, the store build and `node scripts/verify-extension-build.mjs .output-store` first. The live-X rows are manual because X's rendered DOM changes often: record the X build/date and the outcome of every row before release.

## Automated coverage

| Area | Evidence |
| --- | --- |
| X action guard | `tests/x-action-guards.test.ts` and `tests/x-action-guard-matrix.test.ts` reject Post/Reply submit, Like, Follow, Delete, DM and media controls. |
| Composer safety | The same guard tests reject every pre-existing composer except the exact inline reply destination, and require one unambiguous candidate. `tests/composer-flow.test.ts` fails closed for edited, stale, hidden or disconnected composers. |
| Consent | `tests/consent.test.ts`: nothing is scanned, captured or sent before consent; passive capture needs the separate opt-in; the version is pinned to the disclosure text by hash; withdrawal returns to the no-consent state. |
| Transport | `tests/transport.test.ts`: headers, status-to-code mapping, Retry-After, timeouts, retries, the NDJSON stream, cancel with the same key, no request before consent. |
| Sign-in | `tests/auth.test.ts`: PKCE (RFC 7636 vector), state check, single use, expiry, a stale 401 never clears a newer token, sign-out clears even when the revoke hangs. |
| Account copy | `tests/usage-ui.test.ts`: dock state priority, 80% boundary, dates, every error code has a title, body and action. |
| Token isolation | `tests/no-token-in-content.test.ts`: no content-side file names the token key, reads all of storage, listens to storage changes or uses a network API. |

## Live checks (store build, test account)

| Control/state | Exercise | Pass condition |
| --- | --- | --- |
| Install | Install the build. | The onboarding page opens with the data notice, an unchecked passive-learning box, Agree and continue, and Not now. No request leaves the browser before Agree (check DevTools network for the service worker). |
| Consent in the dock | Choose Not now, open X, press ✦ Starling in X's sidebar (or the toolbar icon). | No ✦ buttons or composer pills appear, only ✦ Starling in the sidebar. The dock opens on the same notice, and Agree and continue stays in view however far it is scrolled. Typing speed and appearance still work in Settings. |
| Sign-in | Agree, then Sign in. | A Chrome sign-in window opens on the service's /connect page, shows this browser's label, and returns signed in. Closing the window instead leaves the dock on "Sign in to start drafting". |
| Draft a reply | Press ✦ on a post, and Create in a reply box. | The card is headed "✦ Post by @handle", says "Writing your reply…" with the seconds and a Cancel, then the draft is typed into X's own box and the card offers Undo. Pressing the busy ✦ (or Cancel) stops it: "Stopped. No draft used." The pill shows "✓ Done", then "Try another". Starling never presses Post. |
| Web-checked draft | Press ✦ on a post about a recent event, or one that links to an article. | Under the draft, "Source:" or "Sources:" lists at most three pages as links, each naming its site; each opens in a new tab and X stays as it was. A draft with a checkable claim and nothing behind it says "No web check on this one". |
| Add my posts | Open your own profile on X, then Settings → What Starling learns from → Add my posts from this page; press it again. | Only your own posts are added (a repost of someone else's stays out); the line says "Added N of your posts", then "N were already there" on the second press; nothing is posted. On any other page the button is absent and a line says to open your profile. |
| Better to wait | Press ✦ on a post Starling would leave alone. | "Better to wait" (or "Worth skipping"), the reason, "No draft used.", and Write a reply anyway / Quote it instead. |
| Stream and Stop | Start a chat answer and press Stop mid-stream; then press Stop before any text appears. | Streaming text stops at once; the card says Stopped and nothing in X changes. After text had appeared, a line follows within a few seconds: "This used 1 draft because part of it was shown." (the number follows the plan's cost per press), or "No draft was used." when the server released it. Stopped before any text: the card goes and nothing is used. |
| Limit reached | Use an account with no drafts left. | "You've used your N drafts for this month" with See plans and Not now; the refused request's card is one line, "Not drafted. See the note above." After paying, that card says "You have N drafts on Starter." and offers "Draft the reply to @handle". |
| Rate limited | Send requests quickly past the plan's rate; then use up the plan's daily requests. | A short limit: "One moment", counting down on the card before Try again is enabled. The daily cap: "You've reached today's limit on Free" and the time drafting comes back. Both say "No draft was used." |
| Shortcut | Press Alt+Shift+S (⌃⇧S on a Mac) on X; hover the panel's ×. | The panel opens and closes; the tooltip names the key Chrome actually bound. chrome://extensions/shortcuts can change it. |
| Update with X open | Reload the extension at chrome://extensions with an X tab open, then press ✦ in that tab. | The controls go, and the panel says "Starling was updated" with Reload X (plus a warning when an X box has text). No raw "Extension context invalidated.". |
| Turn off on a page | Settings → This page → Turn off on this page. | "Starling is off on this page." with Undo for 8 s; a dimmed "Starling (off here)" stays in the sidebar and turns it back on. |
| Offline | Turn the network off, press ✦. | The dock says "You're offline" and nothing is sent; turning the network back on refreshes the account. |
| Revoked device | Revoke this browser from the account's Devices page, then press ✦. | "You were signed out on this browser" with Sign in again. |
| Excluded pages | Open Messages, Chat, Settings and a sign-in flow on X. | No Starling controls, no capture, and the toolbar icon only shows a short notice. |
| Passive learning | Turn "Learn from my own profile and posts" on, browse your own profile, then off. | Profile and own-post snapshots are sent only while it is on. Confirming a posted draft is sent either way. |
| Withdraw consent | Settings → Data & privacy → Withdraw consent. | Controls disappear, the browser is signed out, and the dock shows the notice again. |
| Uninstall | Remove the extension. | Chrome opens the service's /extension/removed page. |
| Accessibility | Keyboard only, screen reader, 200% zoom. | Every control has a useful name; the usage meter reads as "N of M drafts used"; focus stays visible. |
| Display modes | Light, Dim, Lights out, narrow viewport, reduced motion. | Cards stay legible; nothing clips; nonessential animation stops with reduced motion. |

## Release gate

Release only when the automated checks pass, the verifier passes on the store build, and every live row is recorded as pass, or a documented X selector change fails closed. Anything that could publish, change an unrelated composer, overwrite an edited draft, send data before consent, or read direct messages blocks release.
