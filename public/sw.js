const CACHE = "lingi-coach-v1";
const PRECACHE = [
  "/",
  "/index.html",
  "/manifest.json",
  "/static/icons/icon-192.png" // Added icons to precache for faster offline loading
];

// 1. INSTALL: Precache assets
self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE))
  );
  self.skipWaiting();
});

// 2. ACTIVATE: Cleanup old caches
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// 3. FETCH: Strategy logic
self.addEventListener("fetch", (e) => {
  const url = e.request.url;

  // --- THE FIX: FILTER NON-HTTP REQUESTS ---
  // This stops chrome-extension:// and other internal browser schemes from crashing the cache
  if (!url.startsWith('http')) return;

  // Strategy A: Network-first for API, Auth, and Session calls
  // We want real-time data for Gemini and Payments
  if (url.includes("/api/") || url.includes("/session/") || url.includes("/auth/") || url.includes("/user/")) {
    e.respondWith(
      fetch(e.request).catch(() => {
        // If offline during an API call, return a clean JSON error
        return new Response(JSON.stringify({ error: "You are offline" }), {
          status: 503,
          headers: { "Content-Type": "application/json" }
        });
      })
    );
    return;
  }

  // Strategy B: Cache-first for App Shell (HTML, CSS, JS, Images)
  e.respondWith(
    caches.match(e.request).then((cached) => {
      if (cached) return cached;

      return fetch(e.request).then((res) => {
        // Only cache successful GET requests
        if (e.request.method === "GET" && res && res.status === 200 && res.type === "basic") {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return res;
      }).catch(() => {
        // Fallback to index if network fails (useful for PWA navigation)
        if (e.request.mode === 'navigate') {
          return caches.match("/index.html");
        }
      });
    })
  );
});
