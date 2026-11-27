/* The writing page (templates/write/index.html, web/write.py): Starling on a phone.

   - Reply, Quote or New post; the last choice is remembered in this browser (localStorage).
   - "Paste" reads the clipboard where the browser allows it; otherwise the person pastes by hand.
   - A link to a post on X (all the X app lets people copy) is read on the server through X's
     public embed service (POST /write/link), and the post's words land in the box. If that fails
     the link is kept with the request and the page asks for the words.
   - "Write it" asks for one draft (POST /write/draft, the same metered path as the extension).
     A phone that loses the connection, or is put away, asks GET /write/requests/{key} whether the
     draft is still being written, and then collects it with the same key: it is not charged twice.
   - Each draft has Copy (the clipboard, with a fallback) and, when X can take it there, a link
     that opens X's post box with the draft in it. The person still presses Post.
   No innerHTML anywhere: everything shown is set as text. */
(function () {
  "use strict";

  var root = document.querySelector("[data-write]");
  if (!root) return;
  var form = root.querySelector("[data-write-form]");
  if (!form) return;

  var KIND_KEY = "starling.write.kind";
  var FETCH_TIMEOUT_MS = 70000;
  var POLL_EVERY_MS = 2500;
  var POLL_FOR_MS = 90000;
  var LINK_ONLY_SLACK = 30;
  var KINDS = {
    reply: { label: "Paste the post you're replying to", hint: "Its words, or its link from X.", post: true },
    quote: { label: "Paste the post you're quoting", hint: "Its words, or its link from X.", post: true },
    post: { label: "What's it about?", hint: "A few words is enough. Leave it empty and Starling picks something from what you usually write about.", post: false }
  };
  /* A link to one post on X. Kept in step with services/world/x_post.py parse_status_url. */
  var STATUS_RE = /(?:^|[^A-Za-z0-9.-])((?:https?:\/\/)?(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com\/(?:i\/web|[A-Za-z0-9_]{1,15})\/status(?:es)?\/(\d{1,25})\S*)/i;

  var csrf = (form.querySelector("input[name='csrf_token']") || {}).value || "";
  var textBox = form.querySelector("[data-text]");
  var textLabel = form.querySelector("[data-text-label]");
  var textHint = form.querySelector("[data-text-hint]");
  var authorRow = form.querySelector("[data-author-row]");
  var authorBox = form.querySelector("[data-author]");
  var pasteButton = form.querySelector("[data-paste]");
  var linkNote = form.querySelector("[data-link-note]");
  var submit = form.querySelector("[data-write-submit]");
  var summary = form.querySelector("[data-error-summary]");
  var result = root.querySelector("[data-result]");
  var statusLine = result.querySelector("[data-status]");
  var skeleton = result.querySelector("[data-skeleton]");
  var drafts = result.querySelector("[data-drafts]");
  var errorBox = result.querySelector("[data-error]");
  var errorText = result.querySelector("[data-error-text]");
  var errorActions = result.querySelector("[data-error-actions]");
  var messageBox = result.querySelector("[data-message]");
  var messageText = result.querySelector("[data-message-text]");
  var messageReasons = result.querySelector("[data-message-reasons]");
  var messageActions = result.querySelector("[data-message-actions]");
  var anywayButton = result.querySelector("[data-anyway]");
  var resultActions = result.querySelector("[data-result-actions]");
  var againButton = result.querySelector("[data-again]");
  var template = root.querySelector("[data-draft-template]");
  var remainingFigure = document.querySelector("[data-usage-remaining]");

  var link = null;          /* {url, statusId, authorName, handle, lang} for the post being answered */
  var lookingUp = "";       /* the link being read right now */
  var busy = false;
  var lastBody = null;      /* the request behind the draft on screen, for "Try another" */
  var timers = [];

  /* ------------------------------------------------------------------ small helpers */

  function store(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* private mode: not remembered */ }
  }

  function recall(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }

  function newKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = Array.prototype.map.call(bytes, function (b) { return (b + 0x100).toString(16).slice(1); }).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }

  function timeZone() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (error) { return ""; }
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function kind() {
    var checked = form.querySelector("input[name='kind']:checked");
    return checked && KINDS[checked.value] ? checked.value : "reply";
  }

  function headers(json) {
    var values = { "Accept": "application/json", "X-CSRF-Token": csrf };
    if (json) values["Content-Type"] = "application/json";
    var zone = timeZone();
    if (zone) values["X-Starling-Time-Zone"] = zone;
    return values;
  }

  function readJson(response) {
    return response.text().then(function (text) {
      try { return text ? JSON.parse(text) : {}; } catch (error) { return {}; }
    });
  }

  function later(fn, ms) {
    timers.push(window.setTimeout(fn, ms));
  }

  function stopTimers() {
    timers.forEach(function (id) { window.clearTimeout(id); });
    timers = [];
  }

  /* ------------------------------------------------------------------ Reply, Quote, New post */

  function applyKind(value, remember) {
    var spec = KINDS[value] || KINDS.reply;
    textLabel.textContent = spec.label;
    textHint.textContent = spec.hint;
    authorRow.hidden = !spec.post;
    textBox.setAttribute("maxlength", spec.post ? "10000" : "500");
    textBox.setAttribute("rows", spec.post ? "6" : "3");
    if (!spec.post) hideLinkNote();
    else if (link) showLinkNote();
    if (remember) store(KIND_KEY, value);
  }

  Array.prototype.forEach.call(form.querySelectorAll("input[name='kind']"), function (radio) {
    radio.addEventListener("change", function () {
      clearFieldError();
      applyKind(radio.value, true);
      if (KINDS[radio.value].post) maybeReadLink();
    });
  });

  /* ------------------------------------------------------------------ the link to a post */

  function hideLinkNote() {
    linkNote.hidden = true;
    clear(linkNote);
    linkNote.classList.remove("is-problem");
  }

  function showLinkNote(text, problem) {
    clear(linkNote);
    var words = text;
    if (!words && link) {
      var who = link.authorName && link.handle ? link.authorName + " (@" + link.handle + ")"
        : link.handle ? "@" + link.handle : link.authorName || "";
      words = link.read ? "From X" + (who ? ": " + who : "") + "." : "Link kept with your request.";
    }
    linkNote.appendChild(el("span", "", words || ""));
    if (link) {
      var remove = el("button", "link-button", "Remove link");
      remove.type = "button";
      remove.addEventListener("click", function () {
        link = null;
        hideLinkNote();
        textBox.focus();
      });
      linkNote.appendChild(remove);
    }
    linkNote.classList.toggle("is-problem", Boolean(problem));
    linkNote.hidden = false;
  }

  function linkIn(text) {
    var match = STATUS_RE.exec(text || "");
    if (!match) return null;
    return { raw: match[1], statusId: match[2].replace(/^0+/, "") };
  }

  function withoutLink(text, found) {
    return (text || "").split(found.raw).join(" ").replace(/[ \t]+\n/g, "\n").trim();
  }

  function maybeReadLink() {
    if (!KINDS[kind()].post) return;
    var text = textBox.value;
    var found = linkIn(text);
    if (!found) return;
    /* A post's own words can contain a link to another post: only a box that holds little more
       than the link is read from X. */
    if (withoutLink(text, found).length > LINK_ONLY_SLACK) return;
    if (lookingUp === found.statusId || (link && link.statusId === found.statusId && link.read)) return;
    readLink(found);
  }

  function readLink(found) {
    lookingUp = found.statusId;
    showLinkNote("Reading the post from X…");
    fetch("/write/link", {
      method: "POST", credentials: "same-origin", headers: headers(true),
      body: JSON.stringify({ url: found.raw })
    }).then(function (response) {
      return readJson(response).then(function (data) { return { status: response.status, data: data }; });
    }).then(function (answer) {
      if (lookingUp !== found.statusId) return;
      lookingUp = "";
      var data = answer.data || {};
      if (answer.status === 403 && data.error && data.error.code === "consent_required") {
        window.location.reload();
        return;
      }
      if (answer.status >= 400) {
        var message = data.error && data.error.message ? data.error.message : "Couldn't read that post. Paste its text instead.";
        link = { url: found.raw, statusId: found.statusId, read: false };
        textBox.value = withoutLink(textBox.value, found);
        showLinkNote(message, true);
        return;
      }
      if (data.ok && data.post) {
        var post = data.post;
        link = { url: post.url, statusId: post.status_id, authorName: post.author_name || "",
                 handle: post.author_handle || "", lang: post.lang || "", read: true };
        textBox.value = post.text;
        if (!authorBox.value.trim() && post.author_handle) authorBox.value = "@" + post.author_handle;
        showLinkNote();
        return;
      }
      link = { url: data.link || found.raw, statusId: data.status_id || found.statusId, read: false };
      textBox.value = withoutLink(textBox.value, found);
      showLinkNote(data.message || "Couldn't read that post. Paste its text instead.", true);
      textBox.focus();
    }).catch(function () {
      if (lookingUp !== found.statusId) return;
      lookingUp = "";
      link = { url: found.raw, statusId: found.statusId, read: false };
      textBox.value = withoutLink(textBox.value, found);
      showLinkNote("Couldn't read that post. Paste its text instead.", true);
    });
  }

  var typing = null;
  textBox.addEventListener("input", function () {
    clearFieldError();
    window.clearTimeout(typing);
    typing = window.setTimeout(maybeReadLink, 500);
  });
  textBox.addEventListener("paste", function () {
    window.setTimeout(maybeReadLink, 0);
  });

  /* ------------------------------------------------------------------ Paste */

  if (pasteButton && navigator.clipboard && typeof navigator.clipboard.readText === "function" && window.isSecureContext) {
    pasteButton.hidden = false;
    pasteButton.addEventListener("click", function () {
      navigator.clipboard.readText().then(function (text) {
        if (!text) return;
        textBox.value = text.slice(0, 10000);
        clearFieldError();
        textBox.focus();
        maybeReadLink();
      }, function () {
        showLinkNote("Your browser did not allow that. Press and hold in the box, then choose Paste.", true);
      });
    });
  }

  /* ------------------------------------------------------------------ errors in the form */

  function clearFieldError() {
    if (!summary || summary.hidden) return;
    summary.hidden = true;
    clear(summary);
    textBox.removeAttribute("aria-invalid");
  }

  function fieldError(message) {
    clear(summary);
    summary.appendChild(el("p", "error-summary-title", "There is a problem"));
    var list = el("ul");
    var item = el("li");
    var anchor = el("a", "", message);
    anchor.href = "#field-text";
    item.appendChild(anchor);
    list.appendChild(item);
    summary.appendChild(list);
    summary.hidden = false;
    textBox.setAttribute("aria-invalid", "true");
    summary.focus();
  }

  /* ------------------------------------------------------------------ asking for a draft */

  function requestBody(again) {
    var value = kind();
    var body = { kind: value, text: textBox.value };
    if (KINDS[value].post) {
      body.author = authorBox.value.trim();
      if (link) {
        body.link = link.url;
        if (link.authorName) body.author_name = link.authorName;
        if (link.lang) body.lang = link.lang;
      }
      if (again) body.again = true;
    }
    return body;
  }

  function setBusy(on) {
    busy = on;
    submit.disabled = on;
    submit.classList.toggle("is-busy", on);
    submit.setAttribute("aria-busy", on ? "true" : "false");
    submit.textContent = on ? "Writing…" : "Write it";
    againButton.disabled = on;
    if (anywayButton) anywayButton.disabled = on;
  }

  function say(text, dots) {
    clear(statusLine);
    if (!text) return;
    if (dots) {
      var span = el("span", "dots");
      span.setAttribute("aria-hidden", "true");
      span.appendChild(el("i"));
      span.appendChild(el("i"));
      span.appendChild(el("i"));
      statusLine.appendChild(span);
    }
    statusLine.appendChild(el("span", "", text));
  }

  function resetResult() {
    clear(drafts);
    errorBox.hidden = true;
    errorActions.hidden = true;
    clear(errorActions);
    messageBox.hidden = true;
    messageReasons.hidden = true;
    clear(messageReasons);
    messageActions.hidden = true;
    if (anywayButton) anywayButton.hidden = true;
    resultActions.hidden = true;
  }

  function startWaiting() {
    resetResult();
    result.hidden = false;
    skeleton.hidden = false;
    say(kind() === "post" ? "Writing your post…" : "Reading the post and writing in your voice…", true);
    later(function () { say("Still writing. Busy moments can take up to a minute.", true); }, 15000);
    later(function () { say("Taking longer than usual. Nothing is used until a draft arrives.", true); }, 40000);
    if (typeof result.scrollIntoView === "function") {
      var smooth = window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      result.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
    }
  }

  function stopWaiting() {
    stopTimers();
    skeleton.hidden = true;
    setBusy(false);
  }

  function post(body, key) {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = controller ? window.setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS) : null;
    var options = { method: "POST", credentials: "same-origin", body: JSON.stringify(body), headers: headers(true) };
    options.headers["Idempotency-Key"] = key;
    if (controller) options.signal = controller.signal;
    return fetch("/write/draft", options).then(function (response) {
      window.clearTimeout(timer);
      return readJson(response).then(function (data) { return { status: response.status, data: data }; });
    }, function (error) {
      window.clearTimeout(timer);
      throw error;
    });
  }

  function ask(body, key) {
    if (busy) return;
    lastBody = body;
    setBusy(true);
    startWaiting();
    post(body, key).then(function (answer) {
      handle(answer, body, key);
    }, function () {
      recover(body, key, Date.now());
    });
  }

  /* The phone lost the connection (or the page was put away) before the answer arrived: ask
     whether the draft is still being written, then collect it with the same key. */
  function recover(body, key, since) {
    say("The connection dropped. Checking whether your draft is ready…", true);
    fetch("/write/requests/" + encodeURIComponent(key), {
      credentials: "same-origin", headers: headers(false), cache: "no-store"
    }).then(function (response) {
      if (response.status === 202 && Date.now() - since < POLL_FOR_MS) {
        later(function () { recover(body, key, since); }, POLL_EVERY_MS);
        return;
      }
      if (response.status === 200) {
        post(body, key).then(function (answer) { handle(answer, body, key); }, function () { lost(body, key); });
        return;
      }
      lost(body, key);
    }, function () {
      if (Date.now() - since < POLL_FOR_MS) later(function () { recover(body, key, since); }, POLL_EVERY_MS);
      else lost(body, key);
    });
  }

  function lost(body, key) {
    stopWaiting();
    say("");
    showError("The connection dropped before the draft arrived. No draft was used.", [
      button("Try again", function () { ask(body, key); })
    ]);
  }

  function handle(answer, body, key) {
    var data = answer.data || {};
    if (answer.status === 409 && data.error && data.error.code === "duplicate_request") {
      later(function () { recover(body, key, Date.now()); }, POLL_EVERY_MS);
      return;
    }
    stopWaiting();
    say("");
    if (data.error && data.error.usage) updateUsage(data.error.usage);
    if (answer.status >= 400 || !data.ok && data.error) {
      showProblem(answer.status, data.error || {});
      return;
    }
    if (data.usage) updateUsage(data.usage);
    if (data.draft) {
      showDrafts(data);
    } else {
      showMessage(data);
    }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (busy) return;
    var value = kind();
    var text = textBox.value.trim();
    if (KINDS[value].post) {
      var found = linkIn(text);
      var words = found ? withoutLink(text, found) : text;
      if (found && words.length <= LINK_ONLY_SLACK && !lookingUp) {
        readLink(found);
        return;
      }
      if (!words) {
        fieldError(link ? "Paste the post's words too. X's link alone is not enough." : "Paste the post you're answering.");
        textBox.focus();
        return;
      }
    }
    clearFieldError();
    ask(requestBody(false), newKey());
  });

  againButton.addEventListener("click", function () {
    if (lastBody) ask(Object.assign({}, lastBody, { again: undefined }), newKey());
  });

  if (anywayButton) {
    anywayButton.addEventListener("click", function () {
      if (lastBody) ask(Object.assign({}, lastBody, { again: true }), newKey());
    });
  }

  /* ------------------------------------------------------------------ showing the answer */

  function updateUsage(usage) {
    if (remainingFigure && usage && typeof usage.remaining === "number") {
      remainingFigure.textContent = Math.max(0, usage.remaining).toLocaleString("en-US");
    }
  }

  function button(label, onClick, secondary) {
    var node = el("button", secondary === false ? "btn" : "btn btn-secondary", label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  function linkButton(label, href) {
    var node = el("a", "btn btn-secondary", label);
    node.href = href;
    return node;
  }

  function showError(text, actions) {
    errorText.textContent = text;
    clear(errorActions);
    (actions || []).forEach(function (action) { errorActions.appendChild(action); });
    errorActions.hidden = !(actions && actions.length);
    errorBox.hidden = false;
  }

  function wait(seconds) {
    var s = Math.max(1, Math.round(seconds || 0));
    return s < 90 ? s + " seconds" : Math.round(s / 60) + " minutes";
  }

  function showProblem(status, error) {
    var code = error.code || "";
    var message = error.message || "";
    var again = function () { if (lastBody) ask(lastBody, newKey()); };
    switch (code) {
      case "limit_reached":
        showError("You have used all your drafts for this month. They come back when your allowance resets.", [linkButton("See plans", "/pricing")]);
        return;
      case "fair_use_exceeded":
        showError("This account reached its fair-use limit for this month. Nothing extra is charged.");
        return;
      case "payment_required":
        showError("There is a problem with your payment, so drafting is off for now.", [linkButton("Go to Billing", "/account/billing")]);
        return;
      case "service_paused":
        showError((message || "Drafting is paused for a short while.") + " Nothing was used.");
        return;
      case "ai_unavailable":
      case "server_error":
        showError("Starling's AI isn't answering right now. Try again in a moment. No draft was used.", [button("Try again", again)]);
        return;
      case "rate_limited":
        showError("That is a lot of drafts in a short time. Try again in " + wait(error.retry_after_s) + ".");
        return;
      case "busy":
      case "duplicate_request":
        showError("Starling is still writing your last draft. Wait for it, then try again.");
        return;
      case "consent_required":
        window.location.reload();
        return;
      case "email_unverified":
        showError("Confirm your email address first.", [linkButton("Enter the code", "/verify-email/sent")]);
        return;
      case "signed_out":
        showError("You were signed out. Sign in again, then come back.", [linkButton("Sign in", "/login?next=%2Fwrite")]);
        return;
      case "csrf_failed":
        showError("This page expired. Reload it and try again.", [button("Reload", function () { window.location.reload(); })]);
        return;
      case "account_suspended":
        showError(message || "This account is suspended.");
        return;
      case "payload_too_large":
        showError("That is too long. Paste a shorter post.");
        return;
      case "bad_request":
        if (message) {
          fieldError(message);
          result.hidden = true;
          return;
        }
        break;
      default:
        break;
    }
    if (status === 0 || status >= 500) {
      showError("Something went wrong on Starling's side. Try again. No draft was used.", [button("Try again", again)]);
      return;
    }
    showError(message || "Starling could not write that. Try again.", [button("Try again", again)]);
  }

  function showMessage(data) {
    messageText.textContent = data.message || "Starling came back without a draft. Try again.";
    clear(messageReasons);
    (data.reasons || []).forEach(function (reason) { messageReasons.appendChild(el("li", "", reason)); });
    messageReasons.hidden = !(data.reasons && data.reasons.length);
    if (anywayButton) anywayButton.hidden = !data.can_write_anyway;
    messageActions.hidden = !data.can_write_anyway;
    messageBox.hidden = false;
    resultActions.hidden = false;
    say(data.draft === null && data.ok === false ? "" : "No draft this time. Nothing was used.");
  }

  function safeHandoff(handoff) {
    if (!handoff || typeof handoff.url !== "string" || typeof handoff.label !== "string") return null;
    try {
      var url = new URL(handoff.url);
      if (url.protocol !== "https:" || url.hostname !== "x.com" || url.pathname !== "/intent/post") return null;
      return { url: url.href, label: handoff.label };
    } catch (error) {
      return null;
    }
  }

  function draftCard(text, label, handoff) {
    var fragment = template.content.cloneNode(true);
    var card = fragment.querySelector(".draft-card");
    var labelNode = card.querySelector("[data-draft-label]");
    if (label) {
      labelNode.textContent = label;
      labelNode.hidden = false;
    }
    var textNode = card.querySelector("[data-draft-text]");
    textNode.textContent = text;
    var copyStatus = card.querySelector("[data-copy-status]");
    card.querySelector("[data-copy-draft]").addEventListener("click", function () {
      copy(text, textNode, copyStatus);
    });
    var open = card.querySelector("[data-handoff]");
    var target = safeHandoff(handoff);
    if (target) {
      // Opened from a button: ad blockers hide links to X's posting page as share buttons.
      open.textContent = target.label;
      open.addEventListener("click", function () {
        window.open(target.url, "_blank", "noopener");
      });
      open.hidden = false;
    } else {
      open.parentNode.removeChild(open);
    }
    return card;
  }

  function showDrafts(data) {
    clear(drafts);
    var parts = data.parts && data.parts.length > 1 ? data.parts : null;
    if (parts) {
      parts.forEach(function (part, index) {
        drafts.appendChild(draftCard(part, "Part " + (index + 1) + " of " + parts.length, null));
      });
    } else {
      drafts.appendChild(draftCard(data.draft, "", data.handoff));
    }
    var notes = [];
    if (data.review_note) notes.push({ text: data.review_note, caution: true });
    if (data.search_note) notes.push({ text: data.search_note, caution: true });
    if (data.voice_note) notes.push({ text: data.voice_note, voice: true });
    if (notes.length) {
      var list = el("ul", "draft-notes");
      notes.forEach(function (note) {
        var item = el("li", note.caution ? "is-caution" : "", note.text);
        if (note.voice) {
          item.appendChild(document.createTextNode(" "));
          var teach = el("a", "", "Teach Starling your voice");
          teach.href = "/write/voice";
          item.appendChild(teach);
        }
        list.appendChild(item);
      });
      drafts.appendChild(list);
    }
    if (data.sources && data.sources.length) {
      var box = el("div", "draft-sources");
      box.appendChild(el("p", "", "Based on:"));
      var sources = el("ul");
      data.sources.forEach(function (source) {
        var item = el("li");
        var anchor = el("a", "", source.title || source.url);
        anchor.href = source.url;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        item.appendChild(anchor);
        sources.appendChild(item);
      });
      box.appendChild(sources);
      drafts.appendChild(box);
    }
    resultActions.hidden = false;
    say(parts ? "Your thread is ready. Copy each part." : "Your draft is ready. Edit it in X before you post if you like.");
  }

  /* ------------------------------------------------------------------ Copy */

  function copy(text, node, status) {
    function done() { status.textContent = "Copied. Paste it into X."; }
    function selectIt() {
      var range = document.createRange();
      range.selectNodeContents(node);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Selected. Press and hold the text, then choose Copy.";
    }
    function fallback() {
      var area = el("textarea", "visually-hidden");
      area.value = text;
      area.setAttribute("readonly", "");
      area.setAttribute("aria-hidden", "true");
      area.tabIndex = -1;
      document.body.appendChild(area);
      area.select();
      area.setSelectionRange(0, text.length);
      var copied = false;
      try { copied = document.execCommand("copy"); } catch (error) { copied = false; }
      document.body.removeChild(area);
      if (copied) done(); else selectIt();
    }
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function" && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  /* ------------------------------------------------------------------ start */

  var saved = recall(KIND_KEY);
  /* A post shared from the X app arrives as its link: that is a reply or a quote, never a new post. */
  if (saved === "post" && root.getAttribute("data-shared") === "yes" && linkIn(textBox.value)) saved = "reply";
  if (saved && KINDS[saved]) {
    var radio = form.querySelector("input[name='kind'][value='" + saved + "']");
    if (radio) radio.checked = true;
  }
  applyKind(kind(), false);
  if (textBox.value.trim()) maybeReadLink();
})();
