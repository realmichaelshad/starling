/* Starling checkout return page.
   The page is rendered while the subscription is still being confirmed. This script
   polls GET /billing/status (JSON: the account state from CONTRACTS 6.2, either bare
   or as {"state": ...}) and reloads the page once the plan code or plan status differs
   from what the page was rendered with, or once a payment problem appears (a declined
   card: the reloaded page says so instead of waiting for a confirmation that will never
   come). Without JavaScript a <noscript> meta refresh reloads the page instead. Nothing
   here grants anything; the server decides. */
(function () {
  "use strict";

  var holder = document.querySelector("[data-billing-poll]");
  if (!holder || !window.fetch) return;

  var url = holder.getAttribute("data-status-url") || "/billing/status";
  var initialPlan = holder.getAttribute("data-initial-plan") || "";
  var initialStatus = holder.getAttribute("data-initial-status") || "";
  var initialProblem = holder.getAttribute("data-initial-problem") === "true";
  var statusLine = document.getElementById(holder.getAttribute("data-status-id") || "");
  var startedAt = Date.now();
  /* Stripe usually confirms within a few seconds of the payment, and this is the moment people
     care most: check every second for the first 20 seconds, then back off to every 8 seconds.
     After two minutes keep checking every 15 seconds rather than giving up, and say so. */
  var fastUntilMs = 20000;
  var slowAfterMs = 120000;
  var delayMs = 800;
  var toldSlow = false;

  function say(text) {
    if (statusLine) statusLine.textContent = text;
  }

  function planFrom(body) {
    var state = body && body.state ? body.state : body;
    return state && state.plan ? state.plan : null;
  }

  function schedule() {
    var elapsed = Date.now() - startedAt;
    window.setTimeout(poll, delayMs);
    if (elapsed < fastUntilMs) {
      delayMs = 1000;
    } else if (elapsed < slowAfterMs) {
      delayMs = Math.min(Math.round(Math.max(delayMs, 2000) * 1.5), 8000);
    } else {
      delayMs = 15000;
    }
  }

  function poll() {
    if (Date.now() - startedAt > slowAfterMs && !toldSlow) {
      toldSlow = true;
      say("Still waiting for Stripe. This page keeps checking. You can also close it: your plan appears on the Billing page as soon as it is confirmed.");
    }
    if (document.hidden) {
      schedule();
      return;
    }
    fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (body) {
        var plan = planFrom(body);
        if (plan && (plan.code !== initialPlan || plan.status !== initialStatus)) {
          say("Confirmed. Updating the page.");
          window.location.reload();
          return;
        }
        if (plan && plan.payment_problem === true && !initialProblem) {
          say("The payment needs your attention. Updating the page.");
          window.location.reload();
          return;
        }
        /* The Billing page watches an unfinished first payment being paid in another tab. */
        if (plan && initialProblem && plan.payment_problem === false) {
          say("Payment received. Updating the page.");
          window.location.reload();
          return;
        }
        schedule();
      })
      .catch(function () {
        schedule();
      });
  }

  schedule();
})();
