/* Service worker for Hermes WebUI.
 *
 * Its whole job is to make the app installable and to survive a dropped
 * connection long enough to read what is already on screen. It is deliberately
 * small, and two rules keep it that way.
 *
 * 1. API RESPONSES ARE NEVER CACHED. Health, sessions, turn records and the
 *    chat stream are the live state of an agent running right now; a stale one
 *    is worse than an error, because it looks like the truth. /api/* is passed
 *    straight through and never even inspected.
 *
 * 2. NAVIGATIONS ARE NETWORK-FIRST. The whole app is one HTML file served with
 *    a per-request CSP nonce, so a cache-first shell would eventually serve a
 *    page whose inline scripts no longer match the header they arrived with.
 *    Network first also means an upgrade is picked up the moment the server
 *    has it, instead of after some invisible cache expiry. The cached copy is
 *    the OFFLINE FALLBACK, nothing else.
 *
 * The version is substituted by the server (see /sw.js in server.py) from the
 * app's own mtimes, so a deploy invalidates the cache without anyone
 * remembering to bump a constant here.
 */
const CACHE = "hermes-shell-__VERSION__";
const SHELL = ["/", "/manifest.json", "/icon.svg"];

self.addEventListener("install", (e) => {
  // Take over immediately: waiting for every tab to close is a long time on a
  // phone, and there is no cross-version state to protect.
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // never touch anything remote
  if (url.pathname.startsWith("/api/")) return;      // rule 1

  if (req.mode === "navigate") {                     // rule 2
    e.respondWith(
      fetch(req)
        .then((res) => {
          // Only a real 200 is worth keeping. Caching an error page would
          // hand it back forever the next time the network is down.
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put("/", copy));
          }
          return res;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // Static odds and ends (icon, manifest): cache, but still refresh from the
  // network when it is there.
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res && res.status === 200) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return res;
    }))
  );
});
