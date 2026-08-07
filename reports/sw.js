// Minimal PWA shell cache. Static file, never touched by the pipeline --
// upload-pages-artifact ships the whole reports/ dir every run, so this
// just rides along unchanged.
"use strict";

var CACHE = "newsdigest-shell-v1";
var SHELL = ["./", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) { return cache.addAll(SHELL); })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    })
  );
  self.clients.claim();
});

// Network-first for the page itself -- the deck rebuilds every ~45 min, so
// a fresher copy online should always win over whatever's cached. Only
// fall back to the cached shell when the network request fails outright
// (offline), rather than caching every response, so a stale page never
// silently outranks a live one.
self.addEventListener("fetch", function (event) {
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(function () {
        return caches.match("./").then(function (r) { return r || caches.match(event.request); });
      })
    );
    return;
  }
  event.respondWith(
    caches.match(event.request).then(function (cached) {
      return cached || fetch(event.request);
    })
  );
});
