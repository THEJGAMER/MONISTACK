/* Switchboard service worker: Web Push paging + a minimal app shell.
 *
 * Served from the site root (see app.py's /sw.js route) so its scope
 * covers the whole app - a worker served under /static/ could only
 * control /static/, and then no push would ever reach it.
 *
 * Adapted from PROXMON's sw.js, then extended for paging: an Acknowledge
 * action on the notification itself, which POSTs the ack using the
 * browser's own session cookie - so a page can be acknowledged from the
 * lock screen without opening the app - and a per-alarm tag so a re-fire
 * replaces its notification rather than stacking a new one.
 */
const CACHE = "switchboard-shell-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) =>
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  )
);

// Hashed build assets are immutable: cache first. Everything else -
// including every /api/ call - goes straight to the network; a stale
// alarm list is worse than no alarm list.
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/static/assets/") || url.pathname.startsWith("/icons/")) {
    e.respondWith(
      caches.open(CACHE).then(
        async (c) =>
          (await c.match(e.request)) ??
          fetch(e.request).then((r) => {
            if (r.ok) c.put(e.request, r.clone());
            return r;
          })
      )
    );
  }
});

self.addEventListener("push", (e) => {
  let data = {};
  try {
    data = e.data ? e.data.json() : {};
  } catch {
    data = { title: "Switchboard", body: e.data && e.data.text() };
  }
  const critical = data.severity === "critical";
  e.waitUntil(
    self.registration.showNotification(data.title || "Switchboard", {
      body: data.body || "",
      tag: data.tag || "switchboard",
      renotify: true,
      icon: "/icons/icon-192.png",
      badge: "/icons/badge-96.png",
      timestamp: data.ts || Date.now(),
      // A critical page stays on screen until someone deals with it; a
      // resolve or an info-level note can go away on its own.
      requireInteraction: critical,
      vibrate: critical ? [200, 100, 200, 100, 200] : [100],
      actions: data.actions || [],
      data: { url: data.url || "/", occurrence_id: data.occurrence_id || null },
    })
  );
});

async function focusOrOpen(url) {
  const list = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const c of list) {
    if ("focus" in c) {
      try {
        await c.navigate(url);
      } catch {
        /* cross-origin or navigation refused: fall through to focus */
      }
      return c.focus();
    }
  }
  return self.clients.openWindow(url);
}

self.addEventListener("notificationclick", (e) => {
  const { url, occurrence_id } = e.notification.data || {};
  if (e.action === "ack" && occurrence_id) {
    // Same-origin fetch from the worker carries the session cookie, so
    // this is authenticated as whoever is logged in on this device. If
    // that session has expired the ack 401s and the app opens to log in.
    e.waitUntil(
      fetch(`/api/alarms/${occurrence_id}/ack`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: "Acknowledged from notification" }),
        credentials: "same-origin",
      })
        .then((r) => {
          if (r.ok) {
            e.notification.close();
            return self.registration.showNotification("Acknowledged", {
              body: e.notification.title,
              tag: e.notification.tag,
              icon: "/icons/icon-192.png",
              badge: "/icons/badge-96.png",
            });
          }
          return focusOrOpen(url || "/");
        })
        .catch(() => focusOrOpen(url || "/"))
    );
    return;
  }
  e.notification.close();
  e.waitUntil(focusOrOpen(url || "/"));
});

// The browser rotated the subscription: re-subscribe with the same key
// and tell the app, or this device silently stops being paged.
self.addEventListener("pushsubscriptionchange", (e) => {
  e.waitUntil(
    (async () => {
      const old = e.oldSubscription;
      const sub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: old && old.options ? old.options.applicationServerKey : undefined,
      });
      await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subscription: sub.toJSON() }),
        credentials: "same-origin",
      });
    })()
  );
});
