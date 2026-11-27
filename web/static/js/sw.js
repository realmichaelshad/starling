/* Starling's service worker, served at /sw.js (web/write.py) so it may answer for the whole site.

   One job: when a page cannot load because the phone is offline, show the offline page
   (GET /offline, templates/offline.html) instead of the browser's own error. Nothing else is
   kept: no drafts, no account pages, no answers from the server. The offline page and the
   stylesheets it names are saved when the worker installs, fetched without cookies, so the saved
   copy holds nothing about the person. Everything else always goes to the network. */
"use strict";

var CACHE = "starling-offline-v1";
var OFFLINE_URL = "/offline";

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return fetch(OFFLINE_URL, { credentials: "omit", cache: "no-store" }).then(function (response) {
        if (!response.ok) throw new Error("the offline page is not available");
        return response.clone().text().then(function (html) {
          var assets = [];
          var pattern = /(?:href|src)="(\/static\/[^"]+)"/g;
          var match = pattern.exec(html);
          while (match !== null) {
            assets.push(match[1].replace(/&amp;/g, "&"));
            match = pattern.exec(html);
          }
          return cache.put(OFFLINE_URL, response).then(function () {
            return cache.addAll(assets);
          });
        });
      });
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.filter(function (name) {
        return name.indexOf("starling-") === 0 && name !== CACHE;
      }).map(function (name) {
        return caches.delete(name);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(function () {
      return caches.match(OFFLINE_URL).then(function (saved) {
        return saved || Response.error();
      });
    }));
    return;
  }
  if (url.pathname.indexOf("/static/") === 0) {
    event.respondWith(fetch(request).catch(function () {
      return caches.match(request).then(function (saved) {
        return saved || Response.error();
      });
    }));
  }
});
