/**
 * The smallest stand-in for x.com that Starling's content script accepts.
 *
 * Served for `https://x.com/*` and `https://twitter.com/*` through request interception; the
 * real sites are never contacted. The markup carries exactly the hooks
 * `extension/entrypoints/content.ts` relies on:
 *
 * | Hook | Used by the extension for |
 * |---|---|
 * | `nav[aria-label="Primary"]` with `a[href="/home"]` | where the "Starling" nav item goes |
 * | `[data-testid="SideNav_AccountSwitcher_Button"]` (name, then `@handle`) | the signed-in handle |
 * | `[data-testid="sidebarColumn"]`, `SearchBox_Search_Input`, a "Today's News" heading | where the dock mounts |
 * | `article[data-testid="tweet"]`, `User-Name`, `tweetText`, `a[href*="/status/"] > time` | reading a post |
 * | `[role="group"]` holding `[data-testid="reply"]`, `retweet`, `like` | where the ✦ control goes; the counts |
 * | `[data-testid^="tweetTextarea_"][contenteditable]`, `toolBar`, `tweetButton(Inline)` | X's composer and its Post button |
 * | `[role="dialog"]` containing "Replying to @handle" | the reply box X opens from a post |
 * | `[data-testid="SideNav_NewTweet_Button"]`, `retweet` + a `[role="menuitem"]` "Quote" | the post and quote surfaces |
 *
 * The page's own script does two things only. It opens the composer X would open — the reply
 * dialog, the repost menu and its quote draft, the new-post modal — and every Post/Reply submit
 * button records the click in `window.__xFixture.submitClicks` and does nothing else: the
 * journeys assert that list stays empty, because Starling must never press it.
 *
 * X IS A SINGLE-PAGE APP, and the fixture has to be one too where Starling clicks. X's own
 * "Post" control is an `<a href="/compose/post">` whose click X intercepts to open a modal. A
 * fixture that let that anchor navigate turned a `post` decision into a full page load: the
 * dock, the conversation and the operation were destroyed mid-flight and the draft went
 * nowhere, which looked exactly like a hung request. Every control Starling is allowed to
 * click therefore calls `preventDefault()` and mounts its composer in place.
 */

export const SIGNED_IN = { name: "Fixture Writer", handle: "fixturewriter" };

const MINUTES = 60_000;

/**
 * Posts on the home timeline: three by other authors, one by the signed-in account.
 *
 * `ageMinutes` is not decoration. The engine judges the reply window by the post's age
 * (`backend/engine/app/analyzer.py`: strong inside 30 minutes, viable to 90, "needs proven
 * movement" to 240, closed after that), so a two-hour-old post is answered with a decision to
 * post something of your own instead of replying. The first post is minutes old and moving,
 * which is what a post worth replying to looks like on a real timeline; the last one is hours
 * old on purpose, so a journey can see the other side of the same rule.
 */
export const POSTS = [
  {
    id: "1900000000000000001",
    name: "A Developer",
    handle: "adeveloper",
    text: "We cut our build time in half by deleting two steps nobody ran.",
    replies: 3, reposts: 2, likes: 40, views: 5200, ageMinutes: 12,
  },
  {
    id: "1900000000000000002",
    name: "Pat Shipwright",
    handle: "patships",
    text: "Shipped the retry rewrite today. The only setting that mattered was the timeout, not the backoff.",
    replies: 5, reposts: 1, likes: 61, views: 8100, ageMinutes: 21,
  },
  {
    id: "1900000000000000003",
    name: SIGNED_IN.name,
    handle: SIGNED_IN.handle,
    text: "Spent the week replacing our queue. The boring option won again.",
    replies: 1, reposts: 0, likes: 12, views: 900, ageMinutes: 26,
  },
  {
    id: "1900000000000000004",
    name: "Quiet Compiler",
    handle: "quietcompiler",
    text: "Small release notes are a feature. Nobody reads the long ones and everybody needs the short ones.",
    replies: 2, reposts: 4, likes: 95, views: 12000, ageMinutes: 420,
  },
  {
    // A public claim about a release "today" that links nothing: the one kind of post the engine
    // checks with a web search (journey 5). The same claim as the API journey's web search test.
    id: "1900000000000000005",
    name: "AI Watcher",
    handle: "aiwatcher",
    text: "Example Labs released Model X today with a 400k context window.",
    replies: 4, reposts: 3, likes: 52, views: 6400, ageMinutes: 9,
  },
];

export const STATUS_POST = POSTS[0];
export const OWN_POST = POSTS[2];
export const OLD_POST = POSTS[3];
export const CLAIM_POST = POSTS[4];
export const HOME_PATH = "/home";
export const PROFILE_PATH = `/${SIGNED_IN.handle}`;
/** The signed-in account's own profile: three posts of its own and one repost of somebody else's,
 *  which "Add my posts from this page" must leave out (journey 5). */
export const PROFILE_POSTS = [
  {
    id: "1900000000000000101", name: SIGNED_IN.name, handle: SIGNED_IN.handle,
    text: "Shipping small beats shipping big. Every release note I wrote this year fits on one screen.",
    replies: 2, reposts: 1, likes: 30, views: 1500, ageMinutes: 90,
  },
  {
    id: "1900000000000000102", name: SIGNED_IN.name, handle: SIGNED_IN.handle,
    text: "The retry rewrite taught me one thing: a timeout is a product decision, not a config value.",
    replies: 4, reposts: 2, likes: 52, views: 2600, ageMinutes: 300,
  },
  {
    id: "1900000000000000103", name: SIGNED_IN.name, handle: SIGNED_IN.handle,
    text: "Spent the week replacing our queue. The boring option won again.",
    replies: 1, reposts: 0, likes: 12, views: 900, ageMinutes: 26,
  },
  POSTS[0],
];
export const STATUS_PATH = `/${STATUS_POST.handle}/status/${STATUS_POST.id}`;
export const MESSAGES_PATH = "/messages";
export const SETTINGS_PATH = "/settings/account";
export const COMPOSE_PATH = "/compose/post";

function escapeHtml(value) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value).replace(/[&<>"']/g, ch => entities[ch]);
}

function compact(count) {
  return count >= 1000 ? `${(count / 1000).toFixed(1).replace(/\.0$/, "")}K` : String(count);
}

function ageLabel(minutes) {
  return minutes < 60 ? `${Math.round(minutes)}m` : `${Math.round(minutes / 60)}h`;
}

function article(post, now) {
  const permalink = `/${post.handle}/status/${post.id}`;
  const stamp = new Date(now - post.ageMinutes * MINUTES).toISOString();
  const age = ageLabel(post.ageMinutes);
  return `
      <article data-testid="tweet" role="article" tabindex="0" data-post-id="${post.id}" aria-labelledby="name-${post.id} text-${post.id}">
        <div class="post-head" data-testid="User-Name" id="name-${post.id}">
          <a href="/${post.handle}" role="link"><span>${escapeHtml(post.name)}</span></a>
          <a href="/${post.handle}" role="link" tabindex="-1"><span>@${post.handle}</span></a>
          <a href="${permalink}" role="link" aria-label="${age} ago"><time datetime="${stamp}">${age}</time></a>
        </div>
        <div class="post-text" data-testid="tweetText" lang="en" id="text-${post.id}">${escapeHtml(post.text)}</div>
        <div class="post-actions" role="group" aria-label="${post.replies} replies, ${post.reposts} reposts, ${post.likes} likes, ${post.views} views">
          <button type="button" data-testid="reply" aria-label="${post.replies} Replies. Reply">&#128172; <span>${compact(post.replies)}</span></button>
          <button type="button" data-testid="retweet" aria-label="${post.reposts} reposts. Repost">&#128257; <span>${compact(post.reposts)}</span></button>
          <button type="button" data-testid="like" aria-label="${post.likes} Likes. Like">&#9825; <span>${compact(post.likes)}</span></button>
          <a href="${permalink}/analytics" role="link" aria-label="${post.views} views. View post analytics">&#128200; <span>${compact(post.views)}</span></a>
        </div>
      </article>`;
}

const STYLE = `
  * { box-sizing: border-box; }
  html, body { margin: 0; background: #ffffff; color: #0f1419; font: 15px/1.35 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  a { color: inherit; text-decoration: none; }
  #layout { display: grid; grid-template-columns: 230px minmax(0, 600px) 350px; gap: 0 24px; max-width: 1240px; margin: 0 auto; background: #ffffff; }
  header[role="banner"] { position: sticky; top: 0; align-self: start; padding: 12px 8px; display: flex; flex-direction: column; gap: 12px; min-height: 100vh; background: #ffffff; }
  nav[aria-label="Primary"] { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
  nav[aria-label="Primary"] a { padding: 10px 14px; border-radius: 999px; font-size: 19px; }
  .new-post { display: inline-block; padding: 12px 28px; border-radius: 999px; background: #0f1419; color: #ffffff; font-weight: 700; }
  [data-testid="SideNav_AccountSwitcher_Button"] { margin-top: auto; border: 0; background: #ffffff; text-align: left; padding: 10px; border-radius: 999px; font: inherit; }
  [data-testid="SideNav_AccountSwitcher_Button"] div { display: block; }
  main[role="main"] { display: contents; }
  [data-testid="primaryColumn"] { border-left: 1px solid #eff3f4; border-right: 1px solid #eff3f4; min-height: 100vh; background: #ffffff; }
  [data-testid="sidebarColumn"] { padding: 8px 0; background: #ffffff; }
  [data-testid="sidebarColumn"] input { width: 100%; padding: 10px 16px; border-radius: 999px; border: 1px solid #cfd9de; font: inherit; }
  .module { border: 1px solid #eff3f4; border-radius: 16px; padding: 12px 16px; margin: 12px 0; background: #ffffff; }
  .module h2 { margin: 0 0 8px; font-size: 20px; }
  h1.column-title { margin: 0; padding: 14px 16px; font-size: 20px; border-bottom: 1px solid #eff3f4; }
  .composer { padding: 12px 16px; border-bottom: 1px solid #eff3f4; background: #ffffff; }
  .composer-label { color: #536471; font-size: 13px; margin: 0 0 6px; }
  [contenteditable="true"] { min-height: 52px; padding: 10px 0; font-size: 20px; outline: none; white-space: pre-wrap; overflow-wrap: anywhere; }
  [contenteditable="true"]:empty::before { content: attr(data-placeholder); color: #536471; }
  [data-testid="toolBar"] { display: flex; flex-direction: row; align-items: center; gap: 8px; height: 52px; }
  [data-testid="toolBar"] .grow { flex: 1 1 auto; }
  [data-testid^="tweetButton"] { border: 0; border-radius: 999px; padding: 0 18px; height: 36px; background: #0f1419; color: #ffffff; font: inherit; font-weight: 700; cursor: pointer; }
  [data-testid^="tweetButton"][aria-disabled="true"] { opacity: .5; }
  article { padding: 12px 16px; border-bottom: 1px solid #eff3f4; background: #ffffff; }
  .post-head { display: flex; gap: 6px; align-items: baseline; color: #536471; }
  .post-head a:first-child { color: #0f1419; font-weight: 700; }
  .post-text { margin: 4px 0 8px; }
  .post-actions { display: flex; flex-direction: row; align-items: center; justify-content: space-between; max-width: 425px; height: 36px; color: #536471; }
  .post-actions button { border: 0; background: transparent; color: inherit; font: inherit; cursor: pointer; padding: 4px 8px; border-radius: 999px; }
  .dialog-mask { position: fixed; inset: 0; background: rgba(0, 0, 0, .4); display: flex; align-items: flex-start; justify-content: center; padding-top: 5vh; z-index: 50; }
  [role="dialog"] { width: 600px; max-width: 92vw; background: #ffffff; border-radius: 16px; padding: 12px 16px; }
  [role="dialog"] .quoted { color: #536471; border-left: 2px solid #cfd9de; margin: 4px 0 10px 8px; padding-left: 12px; }
  .menu-mask { position: fixed; inset: 0; z-index: 60; }
  [role="menu"] { position: absolute; top: 30%; left: 38%; min-width: 180px; background: #ffffff; border: 1px solid #eff3f4; border-radius: 12px; padding: 6px 0; box-shadow: 0 0 14px rgba(0, 0, 0, .15); }
  [role="menuitem"] { padding: 10px 16px; cursor: pointer; }
  .dm-list { padding: 16px; }
  @media (max-width: 1000px) { #layout { grid-template-columns: 80px minmax(0, 1fr); } [data-testid="sidebarColumn"] { display: none; } }
`;

/** Runs in the page. Kept dependency free and tiny on purpose. */
function pageScript() {
  const fixture = window.__xFixture = { submitClicks: [], dialogs: [], replyDialogs: 0, loadedAt: Date.now() };

  function syncSubmit(composer, submit) {
    const empty = !(composer.innerText || "").replace(/​/g, "").trim();
    if (empty) submit.setAttribute("aria-disabled", "true");
    else submit.removeAttribute("aria-disabled");
  }

  function wireComposer(root) {
    const composer = root.querySelector('[data-testid^="tweetTextarea_"]');
    const submit = root.querySelector('[data-testid^="tweetButton"]');
    if (!composer || !submit) return;
    composer.addEventListener("input", () => syncSubmit(composer, submit));
    syncSubmit(composer, submit);
    // The one thing a Post/Reply button does here: remember that it was pressed, and by what.
    submit.addEventListener("click", event => {
      event.preventDefault();
      fixture.submitClicks.push({
        testid: submit.getAttribute("data-testid"),
        label: submit.textContent.trim(),
        trusted: event.isTrusted,
        text: composer.innerText,
        at: Date.now(),
      });
    });
  }

  function closeLayers() {
    for (const layer of document.querySelectorAll(".dialog-mask, .menu-mask")) layer.remove();
  }

  /** The modal X opens for a reply, a quote or a new post. `kind` names which. */
  function openComposerDialog(kind, article) {
    closeLayers();
    const quoted = article?.querySelector('[data-testid="tweetText"]')?.textContent ?? "";
    const handle = article?.querySelector('[data-testid="User-Name"] a[tabindex="-1"]')?.textContent.trim() ?? "";
    const isReply = kind === "reply";
    const mask = document.createElement("div");
    mask.className = "dialog-mask";
    mask.innerHTML =
      '<div role="dialog" aria-modal="true" aria-label="' + (isReply ? "Reply" : "Post") + '">'
      + '<button type="button" aria-label="Close" data-testid="app-bar-close">&times;</button>'
      + '<p class="composer-label"></p>'
      + '<div data-testid="tweetTextarea_0" role="textbox" contenteditable="true" aria-multiline="true"'
      + ' aria-label="Post text" data-placeholder="'
      + (isReply ? "Post your reply" : kind === "quote" ? "Add a comment" : "What's happening?") + '"></div>'
      + '<div class="quoted"></div>'
      + '<div data-testid="toolBar"><span class="grow"></span>'
      + '<button type="button" data-testid="tweetButton">' + (isReply ? "Reply" : "Post") + "</button></div>"
      + "</div>";
    // "Replying to @handle" is what the extension looks for to know a reply box opened, and it
    // must never appear above a plain new-post composer.
    const label = mask.querySelector(".composer-label");
    if (isReply && handle) label.innerHTML = 'Replying to <a href="/' + handle.slice(1) + '">' + handle + "</a>";
    else label.remove();
    // A quote carries the quoted post BELOW the editor, as X does; a reply shows it above.
    const quotedEl = mask.querySelector(".quoted");
    if (kind === "post" || !quoted) quotedEl.remove();
    else {
      quotedEl.textContent = quoted;
      if (isReply) mask.querySelector("[data-placeholder]").before(quotedEl);
    }
    mask.querySelector('[aria-label="Close"]').addEventListener("click", () => mask.remove());
    document.getElementById("layers").append(mask);
    wireComposer(mask);
    fixture.dialogs.push({ kind, post: article?.dataset.postId ?? null, at: Date.now() });
    if (isReply) fixture.replyDialogs += 1;
    mask.querySelector("[contenteditable]").focus();
    return mask;
  }

  /** X's repost menu: Repost and Quote, as `[role="menuitem"]`. */
  function openRepostMenu(article) {
    closeLayers();
    const mask = document.createElement("div");
    mask.className = "menu-mask";
    mask.innerHTML =
      '<div role="menu" aria-label="Repost">'
      + '<div role="menuitem" tabindex="0">Repost</div>'
      + '<div role="menuitem" tabindex="0">Quote</div>'
      + "</div>";
    for (const item of mask.querySelectorAll('[role="menuitem"]')) {
      item.addEventListener("click", () => {
        const quote = item.textContent.trim() === "Quote";
        mask.remove();
        if (quote) openComposerDialog("quote", article);
      });
    }
    mask.addEventListener("click", event => { if (event.target === mask) mask.remove(); });
    document.getElementById("layers").append(mask);
    return mask;
  }

  document.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target.closest("a, button, [role='menuitem']") : null;
    if (!target) return;
    const article = target.closest("article");
    const testId = target.getAttribute("data-testid") || "";
    if (testId === "reply" && article) return openComposerDialog("reply", article);
    if ((testId === "retweet" || testId === "unretweet") && article) return openRepostMenu(article);
    // X's own new-post control is a link whose click X handles itself. Letting the browser
    // follow it would reload the page and take the dock, the conversation and any operation
    // in flight with it.
    if (testId === "SideNav_NewTweet_Button" || target.matches("a[href^='/compose/post']")) {
      event.preventDefault();
      openComposerDialog("post", null);
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeLayers();
  });
  for (const composer of document.querySelectorAll(".composer")) wireComposer(composer);
}

function shell(title, column) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)} / X (local fixture)</title>
<style>${STYLE}</style>
</head>
<body>
<div id="react-root">
  <div id="layout">
    <header role="banner">
      <nav aria-label="Primary" role="navigation">
        <a href="/home" data-testid="AppTabBar_Home_Link" aria-label="Home">Home</a>
        <a href="/explore" data-testid="AppTabBar_Explore_Link" aria-label="Search and explore">Explore</a>
        <a href="/messages" data-testid="AppTabBar_DirectMessage_Link" aria-label="Direct Messages">Messages</a>
        <a href="/${SIGNED_IN.handle}" data-testid="AppTabBar_Profile_Link" aria-label="Profile">Profile</a>
      </nav>
      <a class="new-post" href="/compose/post" data-testid="SideNav_NewTweet_Button" aria-label="Post">Post</a>
      <button type="button" data-testid="SideNav_AccountSwitcher_Button" aria-label="Account menu">
        <div>${escapeHtml(SIGNED_IN.name)}</div>
        <div>@${SIGNED_IN.handle}</div>
      </button>
    </header>
    <main role="main">
      <div data-testid="primaryColumn">${column}</div>
      <div data-testid="sidebarColumn">
        <form role="search"><input type="text" data-testid="SearchBox_Search_Input" aria-label="Search query" placeholder="Search"></form>
        <section class="module" aria-label="Today's News"><h2>Today's News</h2><p>Nothing here: this is a local fixture.</p></section>
        <section class="module" aria-label="Who to follow"><h2>Who to follow</h2><p>Nobody.</p></section>
      </div>
    </main>
  </div>
  <div id="layers"></div>
</div>
<script>(${pageScript.toString()})();</script>
</body>
</html>`;
}

function homePage(now) {
  return shell("Home", `
      <h1 class="column-title">Home</h1>
      <div class="composer" data-fixture="home-composer">
        <div data-testid="tweetTextarea_0" role="textbox" contenteditable="true" aria-multiline="true" aria-label="Post text" data-placeholder="What's happening?"></div>
        <div data-testid="toolBar"><span class="grow"></span><button type="button" data-testid="tweetButtonInline">Post</button></div>
      </div>
      <section aria-label="Timeline: Your Home Timeline">${POSTS.map(post => article(post, now)).join("")}
      </section>`);
}

function statusPage(now) {
  const replies = [
    { id: "1900000000000000011", name: "Pat Shipwright", handle: "patships", text: "Which two steps? Asking for a build that takes nine minutes.", replies: 0, reposts: 0, likes: 3, views: 210, ageMinutes: 6 },
    { id: "1900000000000000012", name: "Quiet Compiler", handle: "quietcompiler", text: "Deleting steps is the most underrated optimisation.", replies: 0, reposts: 0, likes: 7, views: 340, ageMinutes: 4 },
  ];
  return shell("Post", `
      <h1 class="column-title">Post</h1>
      <section aria-label="Timeline: Conversation">${article(STATUS_POST, now)}${replies.map(reply => article(reply, now)).join("")}
      </section>`);
}

/** Direct messages and settings: routes Starling must leave completely alone, however post-like they look. */
function privatePage(title) {
  return shell(title, `
      <h1 class="column-title">${escapeHtml(title)}</h1>
      <div class="dm-list" data-testid="dm-inbox">
        <p>A private page. Starling has no business here.</p>
        <article data-testid="tweet" role="article" tabindex="0">
          <div class="post-head" data-testid="User-Name"><a href="/adeveloper"><span>A Developer</span></a><a href="/adeveloper" tabindex="-1"><span>@adeveloper</span></a><a href="/adeveloper/status/1900000000000000001"><time datetime="2026-01-01T00:00:00.000Z">1h</time></a></div>
          <div class="post-text" data-testid="tweetText" lang="en">A post shared inside a direct message.</div>
          <div class="post-actions" role="group"><button type="button" data-testid="reply" aria-label="0 Replies. Reply">&#128172;</button></div>
        </article>
        <div class="composer">
          <div data-testid="tweetTextarea_0" role="textbox" contenteditable="true" aria-label="Start a new message" data-placeholder="Start a new message"></div>
          <div data-testid="toolBar"><span class="grow"></span><button type="button" data-testid="tweetButtonInline">Post</button></div>
        </div>
      </div>`);
}

/** X serves `/compose/post` as a real page too, for a link opened in a new tab. */
function composePage() {
  return shell("Compose", `
      <h1 class="column-title">Compose post</h1>
      <div class="composer" data-fixture="compose-page">
        <div data-testid="tweetTextarea_0" role="textbox" contenteditable="true" aria-multiline="true" aria-label="Post text" data-placeholder="What's happening?"></div>
        <div data-testid="toolBar"><span class="grow"></span><button type="button" data-testid="tweetButton">Post</button></div>
      </div>`);
}

function profilePage(now) {
  return shell(SIGNED_IN.name, `
      <h1 class="column-title">${escapeHtml(SIGNED_IN.name)}</h1>
      <section aria-label="Timeline: ${escapeHtml(SIGNED_IN.name)}'s posts">${PROFILE_POSTS.map(post => article(post, now)).join("")}
      </section>`);
}

function notFoundPage(pathname) {
  return shell("Not found", `<h1 class="column-title">This fixture has no page for ${escapeHtml(pathname)}</h1>`);
}

/** The response for one x.com or twitter.com URL. */
export function xFixtureResponse(rawUrl, now = Date.now()) {
  const url = new URL(rawUrl);
  const pathname = url.pathname.replace(/\/+$/, "") || "/";
  const html = (body, status = 200) => ({ status, contentType: "text/html; charset=utf-8", body });
  if (pathname === "/favicon.ico") return { status: 204, contentType: "image/x-icon", body: "" };
  if (pathname === "/" || pathname === HOME_PATH) return html(homePage(now));
  if (pathname === STATUS_PATH) return html(statusPage(now));
  if (pathname === PROFILE_PATH) return html(profilePage(now));
  if (pathname === COMPOSE_PATH) return html(composePage());
  if (pathname === MESSAGES_PATH || pathname.startsWith(`${MESSAGES_PATH}/`)) return html(privatePage("Messages"));
  if (pathname === "/settings" || pathname.startsWith("/settings/")) return html(privatePage("Settings"));
  return html(notFoundPage(pathname), 404);
}

export function isXHost(hostname) {
  return /^(?:[a-z0-9-]+\.)*(?:x|twitter)\.com$/i.test(hostname);
}
