// Minimal service worker: makes the app installable and keeps the app shell
// available if the computer is briefly unreachable. API calls always go to
// the network (they need the computer anyway).
const SHELL = "meetnotes-shell-v1";
const FILES = ["/", "/style.css", "/app.js", "/icon.svg", "/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname === "/manifest.webmanifest") return;
  const path = url.pathname;
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok && FILES.includes(path)) {
      const copy = res.clone();
      caches.open(SHELL).then((c) => c.put(path, copy));
    }
    return res;
  }).catch(() => caches.match(path)));
});
