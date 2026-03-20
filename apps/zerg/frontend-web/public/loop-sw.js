const CACHE_NAME = "loop-shell-v2";
const SHELL_URLS = ["/loop", "/site.webmanifest", "/apple-touch-icon.png?v=2", "/maskable-icon-192.png", "/maskable-icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

function shouldHandle(request, url) {
  if (request.method !== "GET") return false;
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.startsWith("/api/")) return false;

  return (
    url.pathname === "/loop" ||
    url.pathname.startsWith("/loop/") ||
    url.pathname.startsWith("/assets/") ||
    url.pathname === "/site.webmanifest" ||
    url.pathname.endsWith(".png") ||
    url.pathname.endsWith(".ico")
  );
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (!shouldHandle(request, url)) return;

  if (request.mode === "navigate" && (url.pathname === "/loop" || url.pathname.startsWith("/loop/"))) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const cloned = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put("/loop", cloned));
          return response;
        })
        .catch(async () => {
          const cached = await caches.match("/loop");
          return cached || Response.error();
        }),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      const networkFetch = fetch(request)
        .then((response) => {
          const cloned = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, cloned));
          return response;
        })
        .catch(() => cached);
      return cached || networkFetch;
    }),
  );
});

self.addEventListener("push", (event) => {
  if (!event.data) return;

  let payload = {};
  try {
    payload = event.data.json();
  } catch {
    payload = { title: "Loop Inbox", body: event.data.text() };
  }

  const title = typeof payload.title === "string" && payload.title.trim() ? payload.title.trim() : "Loop Inbox";
  const body = typeof payload.body === "string" ? payload.body : "A coding turn needs your attention.";
  const url = typeof payload.url === "string" && payload.url.trim() ? payload.url.trim() : "/loop";
  const tag = typeof payload.tag === "string" && payload.tag.trim() ? payload.tag.trim() : "loop-card";

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      badge: "/maskable-icon-192.png",
      icon: "/apple-touch-icon.png?v=2",
      data: {
        url,
        cardId: payload.cardId ?? null,
        sessionId: payload.sessionId ?? null,
      },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = typeof event.notification.data?.url === "string" ? event.notification.data.url : "/loop";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (clients) => {
      const absoluteTargetUrl = new URL(targetUrl, self.location.origin).toString();

      for (const client of clients) {
        if (!client.url.startsWith(self.location.origin)) continue;
        if ("navigate" in client) {
          await client.navigate(absoluteTargetUrl);
        }
        return client.focus();
      }

      if (self.clients.openWindow) {
        return self.clients.openWindow(absoluteTargetUrl);
      }
      return undefined;
    }),
  );
});
