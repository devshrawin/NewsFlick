// Minimal PWA shell cache. Static file, never touched by the pipeline --
// branch-based GitHub Pages republishes the whole docs/ dir on every push,
// so this just rides along unchanged.
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
// a fresher copy online should always win over whatever's cached. The
// cache under "./" gets overwritten with every successful fetch, not just
// populated once at install -- without that, an offline visitor always
// sees the page exactly as it was the day they installed the PWA,
// presented as current (relTime() would cheerfully render "3d ago" on
// everything). Only fall back to the cached copy when the network
// request fails outright (offline).
self.addEventListener("fetch", function (event) {
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (cache) { cache.put("./", copy); });
        return res;
      }).catch(function () {
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
