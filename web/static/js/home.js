/* Starling landing page: progressive enhancement only. The page is complete without this file
   (see the notes at the top of css/home.css).
   - entrance: the header, the hero and the words of the headline arrive in sequence, starting
     as soon as the page is ready and over in about 1.2s
   - [data-reveal] elements rise in as they scroll into view
   - the flowing menu: the cream marquee enters and leaves by the edge the pointer crossed
   - "How it works": while the section is pinned, the scroll position scrubs the mock through
     its four states (reverse the scroll and the letters un-type); otherwise the mock stays in
     its finished state
   - the directional edge glow on cards
   - endless animations park while their section is off screen (site.js parks them for a hidden tab)
   Motion is skipped entirely when the visitor prefers reduced motion. No HTML strings are
   parsed here and no style attributes are written: styles change through the CSSOM only. */
(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;
  /* From here on the page is ours to animate: this switches off the fail-safe in the stylesheet
     and turns on the pinned layout and the pointer-aware marquee. */
  root.classList.add("home-live");

  function media(query) {
    return window.matchMedia ? window.matchMedia(query) : { matches: false };
  }
  function listen(mq, fn) {
    if (mq.addEventListener) mq.addEventListener("change", fn);
    else if (mq.addListener) mq.addListener(fn);
  }
  function clamp01(v) { return Math.max(0, Math.min(1, v)); }

  var reduced = media("(prefers-reduced-motion: reduce)");
  var finePointer = media("(hover: hover) and (pointer: fine)");

  /* ---------- headline: one span per word, so the words can rise in one by one ---------- */
  function splitHeadline() {
    var h1 = document.querySelector(".hero h1[data-words]");
    if (!h1) return;
    var text = (h1.textContent || "").replace(/\s+/g, " ").trim();
    if (!text) return;
    var spoken = document.createElement("span");
    spoken.className = "visually-hidden";
    spoken.textContent = text;
    var line = document.createElement("span");
    line.className = "hw-line";
    line.setAttribute("aria-hidden", "true");
    text.split(" ").forEach(function (word, index, all) {
      var span = document.createElement("span");
      span.className = "hw";
      span.textContent = word;
      line.appendChild(span);
      if (index < all.length - 1) line.appendChild(document.createTextNode(" "));
    });
    h1.textContent = "";
    h1.appendChild(spoken);
    h1.appendChild(line);
    h1.classList.add("is-split");
  }

  /* ---------- entrance ----------
     The hero must start arriving the moment the page is ready; the whole sequence is over in
     about 0.8s, and the header is not part of it (it is there from the first paint). The switch has to wait for one frame of the hidden state, or the browser has
     nothing to transition from and the hero simply appears. Two frames guarantee that, but a tab
     opened in the background is never given a frame at all, and the hero would then sit invisible
     until the visitor looked at it. So a short timer races the frames: on screen the frames always
     win and the sequence plays; in a hidden tab the timer wins and the hero is simply there,
     finished, when the visitor arrives. */
  var entered = false;
  function goLive() {
    if (entered) return;
    entered = true;
    body.classList.add("is-live");
  }
  function setupEntrance() {
    splitHeadline();
    if (reduced.matches || !window.requestAnimationFrame) { goLive(); return; }
    window.requestAnimationFrame(function () { window.requestAnimationFrame(goLive); });
    window.setTimeout(goLive, 200);
  }

  /* ---------- off-screen sections stop animating ----------
     The grain, the marquees, the glints and the dots in the mock all loop forever. Left alone
     they drain a laptop battery and the page never settles. Each section that owns a loop is
     marked [data-anim]; it gets .is-idle whenever it is well out of view. site.js handles the
     other half of this (html.anim-paused while the tab is hidden). */
  function setupIdleSections() {
    var sections = Array.prototype.slice.call(document.querySelectorAll("[data-anim]"));
    if (!sections.length || !("IntersectionObserver" in window)) return;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        entry.target.classList.toggle("is-idle", !entry.isIntersecting);
      });
    }, { rootMargin: "200px 0px" });
    sections.forEach(function (section) {
      section.classList.add("is-idle");
      observer.observe(section);
    });
  }

  /* ---------- reveals ---------- */
  function setupReveals() {
    var targets = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
    if (!targets.length) return;
    if (reduced.matches || !("IntersectionObserver" in window)) {
      targets.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.14 });
    targets.forEach(function (el) { observer.observe(el); });
  }

  /* ---------- how it works ---------- */
  function setupHow() {
    var how = document.querySelector(".how");
    var mock = document.getElementById("mock");
    var mtype = document.getElementById("mtype");
    var mline = document.getElementById("mline");
    var mpill = document.getElementById("mpill");
    var mpillText = document.getElementById("mpill-text");
    var mring = document.getElementById("mring");
    var mfoot = document.getElementById("mfoot");
    if (!how || !mock || !mtype || !mline || !mpill || !mpillText || !mring || !mfoot) return;

    var steps = Array.prototype.slice.call(how.querySelectorAll(".hstep"));
    /* the template holds the finished state; its words are the script */
    var DRAFT = (mtype.textContent || "").trim();
    var FOOT_READY = mfoot.getAttribute("data-ready") || mfoot.textContent;
    var FOOT_USED = mfoot.getAttribute("data-used") || mfoot.textContent;
    var RING = 56.5;
    /* the status lines the extension shows while a reply is on its way */
    var LINES = [
      "Reading this post…",
      "Sending to Starling…",
      "Opening X's reply box…",
      "Typing the reply into X…"
    ];
    var pinned = media("(min-width: 901px) and (min-height: 640px) and (prefers-reduced-motion: no-preference)");
    var lastStep = 0;
    var ticking = false;

    function setText(node, value) { if (node.textContent !== value) node.textContent = value; }
    function ring(chars) { mring.style.strokeDashoffset = (RING * (1 - Math.min(chars, 280) / 280)).toFixed(1); }
    function pill(state, label) {
      mpill.classList.toggle("working", state === "working" || state === "typing");
      mpill.classList.toggle("typing", state === "typing");
      mpill.classList.toggle("done", state === "done");
      setText(mpillText, label);
    }

    function render(step, sub) {
      if (step !== lastStep) {
        lastStep = step;
        mock.setAttribute("data-step", String(step));
        steps.forEach(function (el, index) { el.classList.toggle("on", index === step - 1); });
      }
      var connecting = step === 1 && sub < 0.35;
      mock.classList.toggle("is-connecting", connecting);
      setText(mfoot, connecting ? "Connecting" : step === 4 ? FOOT_USED : FOOT_READY);
      if (step === 3) {
        var writing = sub >= 0.4;
        var typed = Math.round(clamp01((sub - 0.4) / 0.56) * DRAFT.length);
        setText(mtype, DRAFT.slice(0, typed));
        ring(typed);
        setText(mline, sub < 0.13 ? LINES[0] : sub < 0.26 ? LINES[1] : sub < 0.4 ? LINES[2] : LINES[3]);
        pill(writing ? "typing" : "working", writing ? "Writing reply" : "Creating reply");
      } else if (step === 4) {
        setText(mtype, DRAFT);
        ring(DRAFT.length);
        setText(mline, LINES[3]);
        pill("done", "Reply ready");
      } else {
        setText(mtype, "");
        ring(0);
      }
    }

    function showFinished() {
      lastStep = 0;
      render(4, 1);
      steps.forEach(function (el) { el.classList.add("on"); });
      lastStep = 0;
    }

    function update() {
      ticking = false;
      if (!pinned.matches) return;
      var rect = how.getBoundingClientRect();
      var travel = rect.height - window.innerHeight;
      if (travel <= 0) { showFinished(); return; }
      if (rect.bottom < -window.innerHeight || rect.top > 2 * window.innerHeight) return;
      var progress = clamp01(-rect.top / travel);
      var segment = Math.min(3.9999, progress * 4);
      var step = 1 + Math.floor(segment);
      render(step, segment - (step - 1));
    }
    function requestUpdate() {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(update);
    }
    function modeChanged() {
      if (pinned.matches) { lastStep = 0; requestUpdate(); }
      else showFinished();
    }

    window.addEventListener("scroll", requestUpdate, { passive: true });
    window.addEventListener("resize", requestUpdate);
    listen(pinned, modeChanged);
    modeChanged();
  }

  /* ---------- flowing menu: the marquee band rolls in from the nearest edge ---------- */
  function setupFlowingMenu() {
    var enhanced = media("(min-width: 901px) and (hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)");
    var EXPO = "cubic-bezier(.19,1,.22,1)";
    Array.prototype.forEach.call(document.querySelectorAll(".fm-item"), function (item) {
      var band = item.querySelector(".fm-marquee");
      var inner = item.querySelector(".fm-inner");
      var link = item.querySelector(".fm-link");
      if (!band || !inner || !band.animate) return;
      function nearestEdge(event) {
        var rect = item.getBoundingClientRect();
        return (event.clientY - rect.top) < rect.height / 2 ? -101 : 101;
      }
      function slide(el, from, to) {
        el.animate([{ transform: "translateY(" + from + "%)" }, { transform: "translateY(" + to + "%)" }],
          { duration: 600, easing: EXPO, fill: "forwards" });
      }
      function show(side) { slide(band, side, 0); slide(inner, -side, 0); }
      function hide(side) { slide(band, 0, side); slide(inner, 0, -side); }
      function keyboardFocused() { return !!link && link.matches(":focus-visible"); }
      item.addEventListener("mouseenter", function (event) {
        if (enhanced.matches && !keyboardFocused()) show(nearestEdge(event));
      });
      item.addEventListener("mouseleave", function (event) {
        if (enhanced.matches && !keyboardFocused()) hide(nearestEdge(event));
      });
      /* the keyboard gets the same band, rising from below */
      item.addEventListener("focusin", function () {
        if (enhanced.matches && keyboardFocused() && !item.matches(":hover")) show(101);
      });
      item.addEventListener("focusout", function () {
        if (enhanced.matches && !item.matches(":hover")) hide(101);
      });
    });
  }

  /* ---------- directional edge glow on cards ---------- */
  function setupGlow() {
    if (!finePointer.matches) return;
    Array.prototype.forEach.call(document.querySelectorAll(".glow-card"), function (card) {
      var glow = document.createElement("span");
      glow.className = "edge-light";
      glow.setAttribute("aria-hidden", "true");
      card.insertBefore(glow, card.firstChild);
      card.addEventListener("pointermove", function (event) {
        var rect = card.getBoundingClientRect();
        var cx = rect.width / 2;
        var cy = rect.height / 2;
        var dx = (event.clientX - rect.left) - cx;
        var dy = (event.clientY - rect.top) - cy;
        var kx = dx ? cx / Math.abs(dx) : Infinity;
        var ky = dy ? cy / Math.abs(dy) : Infinity;
        var edge = Math.min(Math.max(1 / Math.min(kx, ky), 0), 1);
        var degrees = Math.atan2(dy, dx) * 180 / Math.PI + 90;
        if (degrees < 0) degrees += 360;
        card.style.setProperty("--edge-proximity", (edge * 100).toFixed(2));
        card.style.setProperty("--cursor-angle", degrees.toFixed(2) + "deg");
      }, { passive: true });
    });
  }

  function start() {
    setupEntrance();
    setupReveals();
    setupHow();
    setupFlowingMenu();
    setupGlow();
    setupIdleSections();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
