const CACHE = "lingi-v1";
const SHELL = ["/", "/static/icons/icon-192.png", "/manifest.json"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  if (e.request.url.includes("/session") || e.request.url.includes("/auth") ||
      e.request.url.includes("/payment") || e.request.url.includes("/translate") ||
      e.request.url.includes("/user")) {
    e.respondWith(fetch(e.request).catch(() => new Response("Offline", {status: 503})));
    return;
  }
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
      if (res && res.status === 200 && res.type === "basic") {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return res;
    }))
  );
});
