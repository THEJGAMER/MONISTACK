/* Switchboard service worker: Web Push for events + a minimal app shell.
 *
 * Served from the site root (see app.py's /sw.js route) so its scope
 * covers the whole app - a worker served under /static/ could only
 * control /static/, and then no push would ever reach it.
 *
 * - a raised event notifies with sound and a vibration pattern by
 *   severity; critical stays on screen until dismissed;
 * - the resolve of the same event reuses its tag with `quiet`, so it
 *   replaces the raise silently rather than piling up;
 * - every push is also posted to any open tab so the page can play the
 *   in-app tone (a worker cannot play audio itself);
 * - no acknowledge: actioning belongs to the ticketing system.
 */
const CACHE = "switchboard-shell-v3";

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

const VIBRATE = {
  critical: [300, 100, 300, 100, 300, 100, 300],
  warning: [200, 100, 200],
  info: [120],
  ok: [60],
};

async function tellOpenTabs(payload) {
  const list = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const c of list) c.postMessage({ type: "switchboard-page", payload });
}

self.addEventListener("push", (e) => {
  let data = {};
  try {
    data = e.data ? e.data.json() : {};
  } catch {
    data = { title: "Switchboard", body: e.data && e.data.text() };
  }
  const severity = data.severity || "warning";
  const critical = severity === "critical";
  const tag = data.tag || "switchboard";

  e.waitUntil(
    (async () => {
      if (data.quiet) {
        // The event resolved: replace its notification silently.
        const open = await self.registration.getNotifications({ tag });
        for (const n of open) n.close();
        await self.registration.showNotification(data.title || "Resolved", {
          body: data.body || "",
          tag,
          renotify: false,
          silent: true,
          icon: "/icons/icon-192.png",
          badge: "/icons/badge-96.png",
          requireInteraction: false,
          data: { url: data.url || "/", event_id: data.event_id || null, quiet: true },
        });
        await tellOpenTabs(data);
        return;
      }
      await self.registration.showNotification(data.title || "Switchboard", {
        body: data.body || "",
        tag,
        renotify: true,
        silent: false,
        icon: "/icons/icon-192.png",
        badge: "/icons/badge-96.png",
        timestamp: data.ts || Date.now(),
        requireInteraction: critical,
        vibrate: VIBRATE[severity] || VIBRATE.warning,
        actions: data.actions || [],
        data: { url: data.url || "/", event_id: data.event_id || null, severity },
      });
      await tellOpenTabs(data);
    })()
  );
});

async function focusOrOpen(url) {
  const list = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const c of list) {
    if ("focus" in c) {
      try {
        await c.navigate(url);
      } catch {
        /* navigation refused: fall through to focus */
      }
      return c.focus();
    }
  }
  return self.clients.openWindow(url);
}

self.addEventListener("notificationclick", (e) => {
  const { url } = e.notification.data || {};
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
