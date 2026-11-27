/* Starling website: progressive enhancement only. Every page works without this file.
   - the header turns to black glass once the page has scrolled
   - endless animations park while the tab is in the background
   - small-screen navigation toggle
   - moves focus to a visible form error summary so it is announced
   - rewrites server-rendered UTC dates into the reader's own time zone
   - marks a submitted button as busy, and stops a second submission of the same form
   - says so when a wide table can be scrolled sideways
   - copy-to-clipboard helpers: <button data-copy-target="ID" data-copy-status="ID" hidden>
   - "Show" on password fields: <button data-reveal="FIELD-ID" hidden> (templates/_macros.html)
   - a link that leaves for another site (Continue with Google) shows it is on its way
   - the sign-up code field sends itself once it holds six digits; "Send the email again" counts down
   - the check-your-email page carries on by itself once the address is confirmed in another browser
   - "Forgot password?" carries the address already typed to the reset page (sessionStorage, never the URL)
   - registers the service worker (/sw.js) that shows the offline page when the phone has no connection */
(function () {
  "use strict";

  document.documentElement.classList.add("js");

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  /* While the tab is in the background nothing needs to keep animating: the film grain and, on
     the landing page, the marquees and glints all park where they are. Purely a saving; the
     page looks and behaves the same when the visitor comes back. */
  function setupIdleWhenHidden() {
    function update() {
      document.documentElement.classList.toggle("anim-paused", document.hidden === true);
    }
    document.addEventListener("visibilitychange", update);
    update();
  }

  function setupHeader() {
    var header = document.querySelector("[data-site-header]");
    if (!header) return;
    var ticking = false;
    function update() {
      ticking = false;
      header.classList.toggle("scrolled", window.scrollY > 30);
    }
    window.addEventListener("scroll", function () {
      if (!ticking) {
        ticking = true;
        window.requestAnimationFrame(update);
      }
    }, { passive: true });
    update();
  }

  function setupNav() {
    var toggle = document.querySelector("[data-nav-toggle]");
    if (!toggle) return;
    var nav = document.getElementById(toggle.getAttribute("aria-controls") || "");
    if (!nav) return;
    var header = document.querySelector("[data-site-header]");

    function setOpen(open) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      nav.classList.toggle("is-open", open);
      if (header) header.classList.toggle("menu-open", open);
    }

    toggle.addEventListener("click", function () {
      setOpen(toggle.getAttribute("aria-expanded") !== "true");
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        setOpen(false);
        toggle.focus();
      }
    });
    /* a link to a section of the same page closes the menu it was picked from */
    nav.addEventListener("click", function (event) {
      var link = event.target && event.target.closest ? event.target.closest("a[href^='#']") : null;
      if (link) setOpen(false);
    });
    var wide = window.matchMedia ? window.matchMedia("(min-width: 901px)") : null;
    if (wide && wide.addEventListener) {
      wide.addEventListener("change", function (event) {
        if (event.matches) setOpen(false);
      });
    }
  }

  function focusErrorSummary() {
    var summary = document.querySelector("[data-error-summary]:not([hidden])");
    if (summary) summary.focus();
  }

  /* Dates and times are written on the server in UTC, which is not the day the reader is
     living in for part of every day. The extension shows the reader's own zone, so the two
     would disagree about the same reset. Every customer-facing date carries the machine
     value in its datetime attribute and the UTC wording as the text; here the text is
     replaced with the same wording in the reader's zone. Owner-dashboard stamps say "UTC"
     in the text and are not marked, so they stay UTC. */
  function setupLocalDates() {
    var marked = document.querySelectorAll("time[data-local-date], time[data-local-datetime]");
    if (!marked.length || !window.Intl || !Intl.DateTimeFormat) return;
    var dateFormat, timeFormat, zone;
    try {
      dateFormat = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "long", day: "numeric" });
      timeFormat = new Intl.DateTimeFormat("en-US", {
        year: "numeric", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short"
      });
      zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      return;
    }
    Array.prototype.forEach.call(marked, function (element) {
      var raw = element.getAttribute("datetime");
      if (!raw) return;
      var when = new Date(raw);
      if (isNaN(when.getTime())) return;
      var utcText = (element.textContent || "").trim();
      var local;
      try {
        if (element.hasAttribute("data-local-datetime")) {
          /* "Oct 14, 2026, 14:05 GMT+2" -> the site's own shape: "Oct 14, 2026, 14:05 GMT+2" */
          local = timeFormat.format(when).replace(/,\s*(\d{2}:\d{2})/, ", $1");
        } else {
          local = dateFormat.format(when);
        }
      } catch (error) {
        return;
      }
      if (!local) return;
      if (local !== utcText) element.textContent = local;
      if (zone) element.setAttribute("title", utcText + " UTC, shown in " + zone);
    });
  }

  /* A form that has been sent should look sent. The button keeps its name and value (a
     disabled button posts neither), so the only change is the look and a guard against a
     second submission of the same form. */
  function setupBusyForms() {
    var forms = document.querySelectorAll("form[method='post'], form[method='POST']");
    Array.prototype.forEach.call(forms, function (form) {
      /* The writing page's form never leaves the page: js/write.js runs it and shows its own state. */
      if (form.hasAttribute("data-handled-by-script")) return;
      form.addEventListener("submit", function (event) {
        if (form.hasAttribute("data-submitting")) {
          event.preventDefault();
          return;
        }
        var button = event.submitter || form.querySelector("button[type='submit'], button:not([type])");
        if (form.hasAttribute("data-opens-elsewhere")) {
          /* The form opens Stripe in a new tab and this page stays: no spinner, and a line saying
             where to look. The page's own poll notices the payment. */
          var status = document.getElementById("billing-status");
          if (status) status.textContent = "Finish the payment in the Stripe tab. This page updates by itself as soon as it goes through.";
          return;
        }
        form.setAttribute("data-submitting", "");
        if (button && (button.classList.contains("btn") || button.classList.contains("btn-google"))) {
          button.classList.add("is-busy");
          button.setAttribute("aria-busy", "true");
        }
      });
    });
    /* A form that answers with a file download never leaves the page, so nothing would ever stop
       its spinner. After a moment the button is ready again and the page says the download started. */
    Array.prototype.forEach.call(document.querySelectorAll("form[data-download]"), function (form) {
      form.addEventListener("submit", function () {
        window.setTimeout(function () {
          form.removeAttribute("data-submitting");
          Array.prototype.forEach.call(form.querySelectorAll(".btn.is-busy"), function (button) {
            button.classList.remove("is-busy");
            button.removeAttribute("aria-busy");
          });
          var status = form.querySelector("[data-download-status]");
          if (status) status.textContent = "Your download has started. Check your browser's downloads.";
        }, 3000);
      });
    });
    /* Coming back with the Back button must not leave a button spinning for ever. */
    window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      Array.prototype.forEach.call(document.querySelectorAll("form[data-submitting]"), function (form) {
        form.removeAttribute("data-submitting");
      });
      Array.prototype.forEach.call(document.querySelectorAll(".btn.is-busy, button.btn-google.is-busy"), function (button) {
        button.classList.remove("is-busy");
        button.removeAttribute("aria-busy");
      });
    });
  }

  /* A table too wide for its column scrolls inside its box. The cream edge shadows are drawn
     by the stylesheet with no JavaScript; this only adds the sentence that says so, and only
     when there really is something to scroll to. */
  function setupScrollableTables() {
    var wraps = document.querySelectorAll(".table-wrap");
    if (!wraps.length) return;
    var hints = [];
    Array.prototype.forEach.call(wraps, function (wrap) {
      var hint = document.createElement("p");
      hint.className = "table-scroll-hint";
      hint.textContent = "Scroll sideways to see every column.";
      if (wrap.parentNode) wrap.parentNode.insertBefore(hint, wrap.nextSibling);
      hints.push([wrap, hint]);
    });
    function measure() {
      hints.forEach(function (pair) {
        var scrollable = pair[0].scrollWidth - pair[0].clientWidth > 1;
        pair[0].classList.toggle("can-scroll", scrollable);
        pair[1].classList.toggle("is-on", scrollable);
      });
    }
    measure();
    if (window.ResizeObserver) {
      var observer = new ResizeObserver(measure);
      hints.forEach(function (pair) { observer.observe(pair[0]); });
    } else {
      window.addEventListener("resize", measure);
    }
  }

  /* Typing a long password blind is where sign-ups go wrong. The button flips the field between
     hidden and shown; it never changes what is sent, and the field stays spellcheck-free either way
     (the macro sets spellcheck="false"), so a shown password is never sent to a spelling service. */
  function setupPasswordReveal() {
    var buttons = document.querySelectorAll("[data-reveal]");
    Array.prototype.forEach.call(buttons, function (button) {
      var field = document.getElementById(button.getAttribute("data-reveal") || "");
      if (!field) return;
      button.hidden = false;
      button.addEventListener("click", function () {
        var show = field.getAttribute("type") === "password";
        field.setAttribute("type", show ? "text" : "password");
        button.textContent = show ? "Hide" : "Show";
        button.setAttribute("aria-pressed", show ? "true" : "false");
        button.setAttribute("aria-label", show ? "Hide password" : "Show password");
        field.focus();
      });
      button.setAttribute("aria-label", "Show password");
      /* A shown password goes back to dots before the form is sent, so the browser's password
         manager still recognises the field and offers to save it. */
      var form = field.form;
      if (form) {
        form.addEventListener("submit", function () {
          field.setAttribute("type", "password");
          button.textContent = "Show";
          button.setAttribute("aria-pressed", "false");
        });
      }
    });
  }

  /* The trip to Google takes a moment on a slow connection. Without a sign of life people press
     the button again, which starts a second sign-in that cancels the first. */
  function setupBusyLinks() {
    var links = document.querySelectorAll("a[data-busy-link]");
    Array.prototype.forEach.call(links, function (link) {
      link.addEventListener("click", function (event) {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (link.classList.contains("is-busy")) {
          event.preventDefault();
          return;
        }
        link.classList.add("is-busy");
        link.setAttribute("aria-busy", "true");
      });
    });
    window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      Array.prototype.forEach.call(document.querySelectorAll("a[data-busy-link].is-busy"), function (link) {
        link.classList.remove("is-busy");
        link.removeAttribute("aria-busy");
      });
    });
  }

  /* The 6-digit code from the sign-up email. People paste "482 915" or type it digit by digit; either
     way the form goes as soon as six digits are in, the way every code field people know works. */
  function setupCodeForms() {
    var forms = document.querySelectorAll("form[data-code-form]");
    Array.prototype.forEach.call(forms, function (form) {
      var input = form.querySelector("input[name='code']");
      if (!input) return;
      var sent = false;
      input.addEventListener("input", function () {
        var digits = (input.value || "").replace(/\D/g, "");
        if (digits.length > 6) digits = digits.slice(0, 6);
        if (input.value !== digits && digits.length === 6) input.value = digits;
        if (digits.length === 6 && !sent && !form.hasAttribute("data-submitting")) {
          sent = true;
          if (form.requestSubmit) {
            form.requestSubmit();
          } else {
            form.submit();
          }
        }
        if (digits.length < 6) sent = false;
      });
    });
  }

  /* One email a minute is what the server allows; the button says when the next one can go rather
     than letting a second press quietly do nothing. */
  function setupResendCountdown() {
    var button = document.querySelector("[data-resend-button]");
    if (!button) return;
    var label = button.textContent;
    var key = "starling.resend-at";
    var until = 0;
    try { until = parseInt(window.sessionStorage.getItem(key) || "0", 10) || 0; } catch (error) { until = 0; }
    function tick() {
      var left = Math.ceil((until - Date.now()) / 1000);
      if (left > 0) {
        button.disabled = true;
        button.textContent = "Send again in " + left + " s";
        window.setTimeout(tick, 1000);
      } else {
        button.disabled = false;
        button.textContent = label;
      }
    }
    var form = button.form;
    if (form) {
      form.addEventListener("submit", function () {
        until = Date.now() + 60000;
        try { window.sessionStorage.setItem(key, String(until)); } catch (error) { /* private mode: no countdown */ }
      });
    }
    tick();
  }

  /* Someone who signs up here and opens the email on their phone should not have to come back and
     type anything. While the page waits it asks every few seconds, and at once when the tab comes
     back into view, whether the address was confirmed somewhere else; when it was, the page sends
     its continue form and the server signs this browser in. The server decides; this only asks. */
  function setupVerifyPoll() {
    var holder = document.querySelector("[data-verify-poll]");
    var form = document.querySelector("form[data-verify-continue]");
    if (!holder || !form) return;
    function carryOn() {
      if (form.requestSubmit) {
        form.requestSubmit();
      } else {
        form.submit();
      }
    }
    if (holder.hasAttribute("data-verify-auto")) {
      carryOn();
      return;
    }
    var url = holder.getAttribute("data-status-url");
    if (!url || !window.fetch) return;
    var hint = document.querySelector("[data-verify-hint]");
    if (hint) hint.hidden = false;
    var startedAt = Date.now();
    var timer = 0;
    var asking = false;
    var done = false;
    function later() {
      window.clearTimeout(timer);
      /* every 3 seconds for the first quarter of an hour, then every 20: the email works for a day */
      timer = window.setTimeout(ask, Date.now() - startedAt < 900000 ? 3000 : 20000);
    }
    function ask() {
      if (done || asking) return;
      if (document.hidden) {
        later();
        return;
      }
      asking = true;
      fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (body) {
          asking = false;
          if (body && body.state === "confirmed") {
            done = true;
            if (hint) hint.textContent = "Your email is confirmed. One moment.";
            carryOn();
            return;
          }
          later();
        }, function () {
          asking = false;
          later();
        });
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) ask();
    });
    later();
  }

  /* The address someone typed on the sign-in page follows them to "Forgot password?", so they do
     not type it twice. It travels in this tab's sessionStorage, never in the link: an address in a
     URL ends up in history and server logs. */
  function setupCarriedEmail() {
    var key = "starling.email";
    Array.prototype.forEach.call(document.querySelectorAll("a[data-carry-email]"), function (link) {
      link.addEventListener("click", function () {
        var field = document.getElementById("field-email");
        var value = field && field.value ? field.value.trim() : "";
        try {
          if (value) window.sessionStorage.setItem(key, value);
        } catch (error) { /* storage blocked: the reset page simply starts empty */ }
      });
    });
    var form = document.querySelector("form[action='/forgot-password']");
    var field = form ? form.querySelector("#field-email") : null;
    if (field && !field.value) {
      try {
        var carried = window.sessionStorage.getItem(key);
        if (carried) field.value = carried;
      } catch (error) { /* nothing carried */ }
    }
  }

  /* A disconnect acts on its first click and cannot be undone from here, so the first click only
     asks: the button says what the second click will do, and goes back after a few seconds. */
  function setupConfirmTwice() {
    Array.prototype.forEach.call(document.querySelectorAll("form[data-confirm-twice]"), function (form) {
      var label = form.querySelector("[data-confirm-label]");
      if (!label) return;
      var original = label.textContent;
      var armed = false;
      var timer = 0;
      form.addEventListener("submit", function (event) {
        if (armed) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        armed = true;
        label.textContent = form.getAttribute("data-confirm-twice");
        label.setAttribute("aria-live", "polite");
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          armed = false;
          label.textContent = original;
        }, 4000);
      }, true);
    });
  }

  function setupCopyButtons() {
    var buttons = document.querySelectorAll("[data-copy-target]");
    Array.prototype.forEach.call(buttons, function (button) {
      var target = document.getElementById(button.getAttribute("data-copy-target") || "");
      if (!target || !navigator.clipboard || !window.isSecureContext) return;
      var status = document.getElementById(button.getAttribute("data-copy-status") || "");
      button.hidden = false;
      button.addEventListener("click", function () {
        var text = ("value" in target ? target.value : target.textContent).trim();
        navigator.clipboard.writeText(text).then(
          function () { if (status) status.textContent = "Copied."; },
          function () { if (status) status.textContent = "Copy failed. Select the text and copy it yourself."; }
        );
      });
    });
  }

  /* The installed app (the manifest in base.html): a service worker whose only job is the offline
     page. Secure origins only, which is what browsers allow anyway. */
  function registerServiceWorker() {
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
    if (!document.querySelector("link[rel='manifest']")) return;
    function register() {
      navigator.serviceWorker.register("/sw.js").catch(function () { /* the site works without it */ });
    }
    if (document.readyState === "complete") register();
    else window.addEventListener("load", register);
  }

  setupIdleWhenHidden();

  onReady(function () {
    setupHeader();
    setupNav();
    focusErrorSummary();
    setupLocalDates();
    setupBusyForms();
    setupScrollableTables();
    setupCopyButtons();
    setupPasswordReveal();
    setupBusyLinks();
    setupCodeForms();
    setupResendCountdown();
    setupCarriedEmail();
    setupConfirmTwice();
    setupVerifyPoll();
    registerServiceWorker();
  });
})();
