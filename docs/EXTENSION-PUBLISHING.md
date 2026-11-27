# Publishing the Chrome extension

Last reviewed: 2026-09-18. The extension is finished and customers sign in through the Starling
website. Everything below was read from the built manifest and the build verifier. During the
author's public beta in autumn 2026 the extension was installed from the website. **The store-side
steps below come from Google's documentation**, and this page records no completed Chrome Web Store
review. Anyone running their own instance publishes their own listing under their own developer
account.

## Before you start

You need all of the following:

- The production service running on your domain (`STARLING_PUBLIC_URL`), with the privacy policy
  published at `/privacy` and no placeholders left
  ([CONFIGURATION.md](CONFIGURATION.md#business-and-legal-details)).
- A Chrome Web Store developer account.
- Store graphics: icon, screenshots and promo tile.
- The store build of the extension as a zip file.

No Chrome Web Store listing comes with the repository. You create the item under your own developer
account, and the store build is compiled for your own service address.

## 1. Developer account

- Register in the Chrome Web Store developer dashboard and pay the one-time registration fee. Google
  shows the amount during registration (https://developer.chrome.com/docs/webstore/register).
- Use an email address the business controls. Google notes that the account email cannot be changed
  after registration.
- To give colleagues access, add them to your publisher with a role: Viewer, Item Manager, Editor or
  Admin (https://developer.chrome.com/docs/webstore/group-publishers).

## 2. Build the store package

From `extension/`:

```
pnpm install
pnpm typecheck
pnpm test
STARLING_STORE_BUILD=1 WXT_STARLING_ORIGIN=https://app.your-domain.example STARLING_OUT_DIR=.output-store pnpm build
node scripts/verify-extension-build.mjs .output-store
```

- **Put your own service address in `WXT_STARLING_ORIGIN`.** The build refuses the placeholder above
  as written ("still holds a placeholder host"), so a package can never be built pointing at a made-up
  address. It also refuses anything that is not a bare https origin, and a store build refuses local,
  private and IP-literal hosts.
- **On Windows PowerShell** the `NAME=value command` form does not exist. Set the variables first:

  ```
  $env:STARLING_STORE_BUILD = "1"; $env:WXT_STARLING_ORIGIN = "https://<your service host>"; $env:STARLING_OUT_DIR = ".output-store"
  pnpm build
  node scripts\verify-extension-build.mjs .output-store
  ```

  They stay set for that terminal session; close it, or `Remove-Item Env:STARLING_STORE_BUILD`, before
  an e2e build.
- **Output folder:** `.output-store/chrome-mv3`. Without `STARLING_OUT_DIR` it lands in
  `.output/chrome-mv3`; giving it a separate folder keeps the store build from overwriting an e2e
  build.
- **The zip.** There is **no zip script in this repository**. Either zip the contents of
  `.output-store/chrome-mv3` yourself (the archive's root must contain `manifest.json`, not a folder)
  or use the build tool's own `zip` command, which by default names the artifact after the package
  name and version. Upload the zip, not the folder. Whatever you produce, unzip it and run the
  release gate on the result before uploading.

What a store build satisfies, and what enforces it:

- **Service address.** `WXT_STARLING_ORIGIN` is the only origin the extension ever talks to. It is
  validated when the build configuration loads and compiled into the bundle, and it becomes both
  `homepage_url` and the third host permission. With `STARLING_STORE_BUILD=1` the build **refuses**
  any local or private host and any bare IP address. Set it to exactly your `STARLING_PUBLIC_URL`.
- **No development features.** `STARLING_STORE_BUILD=1` drops the development `version_name`. The
  configuration never emits a `key` or `update_url` field, and the verifier fails the build if either
  appears.
- **Version.** It comes from `version` in `extension/package.json`, which the build configuration
  requires to be `x.y.z`.
- **No obfuscation.** Chrome Web Store reviewers reject obfuscated code; minified code is allowed
  (https://developer.chrome.com/docs/webstore/review-process). Source maps are not emitted, and the
  verifier fails a bundle that ships one.
- **Release gate.** Run the release check on the unzipped build before uploading:
  `python scripts/check_release.py <unzipped build folder>`.

## 3. Permissions and their justifications

Enter these one-line justifications on the **Privacy practices** tab. The planned store permissions
are:

The built manifest requests exactly two permissions and three hosts. Nothing else, and no optional
permissions.

| Permission | Justification |
|---|---|
| `storage` | Saves the person's Starling settings and their Starling sign-in token on their own device. |
| `identity` | Signs the person in to their Starling account through the Starling website with `chrome.identity.launchWebAuthFlow`, using an authorization code flow with PKCE. |
| Host `https://x.com/*` | Shows Starling beside posts and X's text box, reads the post the person asks about, and places the draft in X's text box for the person to review. Starling never presses Post. |
| Host `https://twitter.com/*` | The same, for pages still served from the older twitter.com address. |
| Host `https://<your domain>/*` | Sends the person's requests to the Starling service and receives drafts. |

Notes:

- `nativeMessaging`, `alarms`, `downloads` and `clipboardWrite` are **gone**. The earlier local
  version used some of them; the hosted extension uses none, and there is no periodic background
  capture. Copy buttons use the browser's own clipboard call from a click in the focused tab, which
  needs no permission.
- The build verifier enforces the list: it fails any build whose permissions are not exactly
  `storage` and `identity`, and it scans the whole bundle for native-messaging calls.
- Chrome's identity API redirects to `https://<extension-id>.chromiumapp.org/*` and works with
  non-Google sign-in (https://developer.chrome.com/docs/extensions/reference/api/identity).
- Broad host permissions such as `<all_urls>` lengthen review. Starling asks only for X and its own
  service.

## 4. Store listing

- **Name.** The built manifest uses **"Starling - AI writing assistant for X"** (39 characters, well
  inside the store's 75-character limit), with the description "Drafts replies, quotes and posts on X
  in your voice with Starling's hosted AI. Account required. You always press Post." (128 characters,
  inside the 132 limit). Both are printable ASCII, which the verifier checks. Check the name for
  trademark conflicts before you commit to it; changing it later means changing
  `extension/wxt.config.ts`, the listing, the domain and the brand assets.
- **Description.** Describe what Starling does in plain words. Say that a Starling account is
  required and that the Free plan includes 25 drafts per month. Do not claim results you cannot
  prove, such as follower growth.
- **Category and language.** Your choice.
- **Links.** Homepage: `STARLING_PUBLIC_URL`. Support: `STARLING_SUPPORT_EMAIL` or a support page.
- **Privacy policy URL.** `<STARLING_PUBLIC_URL>/privacy`.
- **X.** Starling works on X, a third-party site. Ask counsel how to word that relationship.

## 5. Single purpose

Chrome requires each extension to have one narrow purpose, and every feature must relate to it
(https://developer.chrome.com/docs/webstore/program-policies/quality-guidelines-faq). A draft
statement:

> Starling helps a person write their own posts and replies on X. It drafts text next to the post or
> text box they are working in, and the person reviews, edits and publishes it themselves.

## 6. Privacy practices answers

**Remote code: No.**

- All code ships inside the package. The Starling service returns data (JSON and streamed text), not
  code.
- Chrome allows extensions to fetch data such as JSON from a backend
  (https://developer.chrome.com/docs/extensions/develop/migrate/remote-hosted-code).

**Data usage.** Select the categories below, using the exact names shown in the dashboard:

| Category | What Starling handles | Why |
|---|---|---|
| Personally identifiable information | The account email address. The extension shows it masked; it is entered on the website. | account, sign-in, billing and support |
| Authentication information | The device sign-in token (`stl_dev_...`) kept in extension storage | authenticates requests to the Starling service |
| Website content | Text and images of posts the person asks Starling about, the text in X's text box, the drafts, and writing samples the person saves or adds from their own profile. Short topic names worked out from those samples are sent to the AI provider's web search in the background, to find post ideas (never the text itself) | producing drafts and post ideas |
| User activity | Draft outcomes, for example whether a draft was used or edited. Also the person's own post statistics and profile counts, but only when they turn learning on. | improving drafts for that person |

Review these two categories with counsel before submitting:

- **Personal communications**: messages the person types to Starling in chat.
- **Web history**: page addresses. **Starling does send them.** With each draft request the
  extension includes the post's permalink, the parent post's permalink, the address of the X page the
  person is on, the URLs of any links in the post, and the URLs of the post's images. Passive
  captures send post and profile addresses too. All of these are x.com or twitter.com addresses, plus
  links that appear inside posts; content scripts run only on X, so no other browsing is read. Expect
  to select this category and say so in the privacy policy.

**Certifications.** The dashboard asks you to certify statements about how user data is used.
Starling's design matches Chrome's Limited Use policy: data is used only to provide Starling's
purpose, is not sold, and is not used for advertising or credit decisions
(https://developer.chrome.com/docs/webstore/program-policies/limited-use). Keep it that way. Read
each statement in the dashboard before you tick it.

**Security.** Chrome requires user data to be sent over a secure connection and stored with strong
encryption (https://developer.chrome.com/docs/webstore/program-policies/user-data-faq).

- In code today: production refuses to start unless `STARLING_PUBLIC_URL` uses https.
- Fly.io volumes are encrypted at rest by default (https://fly.io/docs/volumes/overview/).

## 7. Prominent disclosure and consent

Chrome's user data policy requires the disclosure to:

- appear inside the extension's own interface, not only in the store listing or privacy policy
- appear before any data is collected
- describe what data is collected and how it is used
- ask for a specific action that shows the person agrees

Source: https://developer.chrome.com/docs/webstore/program-policies/user-data-faq

Starling's design:

- After sign-in and before the first draft, the extension shows the disclosure and asks the person
  to agree.
- The server refuses drafts with `consent_required` until the current disclosure version is
  accepted. The version is `STARLING_CONSENT_VERSION`, currently `2026-09-22.1`.
- The same flow records a separate choice about learning from the person's own posts
  (`passive_learning`). The person can withdraw consent later (`POST /api/v1/consent/withdraw`).
- If you change what data Starling collects, update the disclosure text and raise
  `STARLING_CONSENT_VERSION` so everyone is asked again.

Where it appears, in code today:

- **On install**, on an onboarding page the extension opens itself. It shows the disclosure, an
  **unchecked** passive-learning box, and two buttons: agree and continue, or not now.
- **In the dock on x.com**, for anyone who chose "not now" and later opens Starling, and again from
  Settings so the text can be read at any time.

Both render the same disclosure constant, so the two can never drift apart. Passive learning is
**off by default** and stays off until the person ticks it. Nothing leaves the browser before the
person agrees: the extension's own tests assert that no request is made first.

The disclosure text is pinned to the version by a SHA-256 hash in the extension's source: change a
word and the test suite fails until `CONSENT_VERSION` and the hash are both updated. That is the
mechanism that stops the disclosure and `STARLING_CONSENT_VERSION` drifting apart.

Take a screenshot of the onboarding page for the reviewer notes; it is also screenshot 4 in the
list below.

## 8. Graphics and screenshots

Requirements (https://developer.chrome.com/docs/webstore/images):

| Asset | Requirement |
|---|---|
| Store icon | Required. 128×128 PNG: 96×96 artwork with 16 px transparent padding on each side. |
| Screenshots | Required. At least 1 and at most 5, at 1280×800 (preferred) or 640×400. |
| Small promo tile | Required. 440×280. |
| Marquee promo tile | Optional. 1400×560. |

Requirements re-checked against https://developer.chrome.com/docs/webstore/images on 2026-09-18 and
unchanged. Brand sources are `brand/web/starling-icon-128.png`, `brand/web/starling-wordmark.png` and
`brand/source/`. **Check the padding before you upload**: the store icon must be a 128×128 PNG whose
artwork occupies the middle 96×96, with 16 transparent pixels on each side. The icons shipped inside
the extension package are a different thing and follow no such rule.

Screenshots to capture:

1. Starling beside a post on x.com, with a finished reply draft.
2. Improving text in X's text box, before and after.
3. The chat panel answering a question.
4. The disclosure and consent screen.
5. The website account page showing the plan and drafts left, or the page where the person approves
   connecting the extension.

Use a test X account. Do not show real people's names, handles or posts without permission.

## 9. Submit and review

- Every submission is reviewed, including updates. Most reviews finish within a few days but can
  take a few weeks; contact developer support if one takes more than three weeks.
- A rejection comes by email with the reason, and the already-published version stays live.
- Source: https://developer.chrome.com/docs/webstore/review-process
- Review times re-checked on 2026-09-18: Google publishes no service level, only that review "is
  completed within a few days, but it can take up to a few weeks", and says to contact developer
  support past three weeks. Reviews take longer for new developers, new extensions and large code
  changes.
- Give reviewers test instructions and a working account. To set one up:
  1. Sign up on your own site with an address you control, and verify it.
  2. Open that customer on the owner dashboard (`/admin/customers`) and use **Grant credits** to add
     enough drafts for a review pass; a few dozen is plenty. The grant is audited, like every admin
     action.
  3. Put the address and password in the reviewer notes, together with a plain statement that an X
     account is also needed and that Starling only ever fills X's own text box: the person presses
     Post.

  Do not use your owner account for this: it can reach the dashboard.

## 10. After approval: connect the published extension id

1. Copy the extension id from the developer dashboard.
2. Add it to `STARLING_EXTENSION_IDS` (comma-separated).
   - In code today: production refuses to start while this list is empty.
   - The sign-in flow accepts only extension ids on this list, with the redirect address
     `https://<extension id>.chromiumapp.org/connect`.
3. Redeploy. Install the extension from the store, sign in, and make one draft.
4. Set `STARLING_EXTENSION_MIN_VERSION` to the oldest version you still accept. Older extensions
   receive `extension_outdated`.
5. Keep the id in your own records and in the host's environment. Nothing in the repository stores
   it: `STARLING_EXTENSION_IDS` is the only place the service reads it from.

An unpacked development build has a different id. Add that id only to development settings, never
to production.

## 11. Updates

1. Raise `version` in `extension/package.json`. It must stay `x.y.z`; the build refuses anything
   else.
2. Build, run the release check, upload and submit. Each update is reviewed.
3. If a server change would break older extensions, raise `STARLING_EXTENSION_MIN_VERSION` only after
   the new version is live in the store.
4. If you change what data is collected, update the disclosure, `STARLING_CONSENT_VERSION`, the
   privacy practices tab and the privacy policy before you publish.

## 12. Team access and item transfer

- Team members can be added to your publisher with roles (see step 1).
- Google has a form for transferring an item to another developer account
  (https://developer.chrome.com/docs/webstore/register). You would need it only if you later move or
  sell the item.

## 13. Two publisher limits to know about

Checked 2026-09-18:

- **Extension slots.** Since August 2026 a publisher account has a default limit of two extension
  slots. Already-published extensions stay published, and an increase can be requested from the
  developer dashboard, usually with an immediate answer
  (https://developer.chrome.com/blog/cws-review-updates-2026). Starling needs one slot.
- **The registration fee amount** is not published. Google's pages confirm it is one-time and that
  the amount is set at Google's discretion and shown during registration
  (https://developer.chrome.com/docs/webstore/register). Do not budget from a figure you read on a
  forum.

## What is unverified here

- During the author's public beta in autumn 2026 the extension was installed from the website. Every
  store-side step above comes from Google's documentation, and this page records no completed
  Chrome Web Store review.
- The extension has been built and checked by the verifier, and it has been exercised by unit tests
  and against the local gateway. None of those checks runs on real x.com with a person signed in to
  X, and this page records no test results from the public beta.
- The store icon's 96×96 artwork area has not been measured against the shipped brand asset.
