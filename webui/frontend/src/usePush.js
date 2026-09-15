// Browser-side Web Push: permission, subscription lifecycle, registering
// with the app. Adapted from PROXMON's usePush.ts.
//
// The one thing to know: a subscription is bound to this browser on this
// device *and* to the server's VAPID public key. It is not a user setting
// - the same account on a second phone subscribes separately, and the
// Account page lists every device that has.
import { useCallback, useEffect, useState } from "react";
import { getPushConfig, subscribePush, unsubscribePush, testPush } from "./api.js";

export function isBrave() {
  const b = navigator.brave;
  return !!b && typeof b.isBrave === "function";
}

// The browser's own failure text is terse; say what to actually do.
export function explainPushError(err) {
  const msg = err instanceof Error ? err.message : String(err);
  if (/push service error|AbortError/i.test(msg)) {
    if (isBrave()) {
      return `${msg}. Brave disables Google's push service by default: open brave://settings/privacy, enable "Use Google services for push messaging", restart Brave and try again.`;
    }
    return `${msg}. The browser could not reach its push service (Chromium uses Google's, Firefox uses Mozilla's, Safari uses Apple's). Check it is not blocked by an extension or the network.`;
  }
  if (/permission|denied/i.test(msg)) return `${msg}. Allow notifications for this site in the browser's site settings.`;
  if (/not enabled on the server/i.test(msg)) return msg;
  return msg;
}

function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export function isStandalone() {
  return window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;
}

// The install prompt is only ever offered by the browser once, before
// the page asks for it - so it has to be caught early (main.jsx) and
// held until a button wants it.
let deferredPrompt = null;
const promptListeners = new Set();
export function initPwa() {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("service worker registration failed", e));
    });
  }
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredPrompt = e;
    promptListeners.forEach((l) => l());
  });
  window.addEventListener("appinstalled", () => {
    deferredPrompt = null;
    promptListeners.forEach((l) => l());
  });
}
export function canInstall() {
  return deferredPrompt !== null;
}
export async function promptInstall() {
  if (!deferredPrompt) return "unavailable";
  await deferredPrompt.prompt();
  const { outcome } = await deferredPrompt.userChoice;
  if (outcome === "accepted") deferredPrompt = null;
  return outcome;
}
export function useInstallPrompt() {
  const [available, setAvailable] = useState(canInstall());
  useEffect(() => {
    const l = () => setAvailable(canInstall());
    promptListeners.add(l);
    return () => promptListeners.delete(l);
  }, []);
  return { available, promptInstall, standalone: isStandalone() };
}

export function usePush() {
  const supported = typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const secure = typeof window !== "undefined" && window.isSecureContext;
  const [config, setConfig] = useState(null); // {enabled, public_key, subscriptions}
  const [state, setState] = useState({
    supported,
    secure,
    permission: supported ? Notification.permission : "unsupported",
    subscribed: false,
    endpoint: null,
    busy: false,
    error: null,
  });

  const refresh = useCallback(async () => {
    try {
      setConfig(await getPushConfig());
    } catch (e) {
      setState((s) => ({ ...s, error: e.message }));
    }
    if (!supported || !secure) return;
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      setState((s) => ({ ...s, permission: Notification.permission, subscribed: !!sub, endpoint: sub?.endpoint ?? null }));
    } catch (e) {
      setState((s) => ({ ...s, error: e.message }));
    }
  }, [supported, secure]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const subscribe = useCallback(
    async ({ minSeverity, notifyResolved }) => {
      if (!config?.public_key) throw new Error("push is not enabled on the server");
      setState((s) => ({ ...s, busy: true, error: null }));
      try {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") throw new Error("notification permission was not granted");
        const reg = await navigator.serviceWorker.ready;
        const sub =
          (await reg.pushManager.getSubscription()) ??
          (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(config.public_key) }));
        await subscribePush({
          subscription: sub.toJSON(),
          min_severity: minSeverity,
          notify_resolved: notifyResolved,
          label: navigator.userAgent.slice(0, 120),
        });
        setState((s) => ({ ...s, busy: false, permission, subscribed: true, endpoint: sub.endpoint }));
        await refresh();
      } catch (e) {
        const explained = explainPushError(e);
        setState((s) => ({ ...s, busy: false, error: explained }));
        throw new Error(explained);
      }
    },
    [config?.public_key, refresh]
  );

  const unsubscribe = useCallback(async () => {
    setState((s) => ({ ...s, busy: true, error: null }));
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await unsubscribePush(sub.endpoint).catch(() => {});
        await sub.unsubscribe();
      }
      setState((s) => ({ ...s, busy: false, subscribed: false, endpoint: null }));
      await refresh();
    } catch (e) {
      setState((s) => ({ ...s, busy: false, error: e.message }));
      throw e;
    }
  }, [refresh]);

  const test = useCallback(async () => {
    if (!state.endpoint) throw new Error("this browser is not subscribed");
    return testPush(state.endpoint);
  }, [state.endpoint]);

  return { ...state, config, subscribe, unsubscribe, test, refresh };
}
