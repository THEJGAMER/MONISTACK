// The in-app pager tone.
//
// A service worker cannot play audio, and browsers largely ignore the
// Notification `sound` option, so the OS handles the sound for a closed
// app (sw.js sets silent:false and a vibration pattern) and this handles
// it for an open tab: sw.js posts every push to the page, and the page
// plays a synthesised two-tone burst - no audio asset to ship, no network.
//
// Audio needs a user gesture before it may start; the context is created
// lazily and resumed on first use, and a blocked play simply does nothing
// rather than throwing into the alarm handling.
const PREF_KEY = "switchboard-pager-sound";

export function isPagerSoundEnabled() {
  try {
    const v = localStorage.getItem(PREF_KEY);
    return v === null ? true : v === "1";
  } catch {
    return true;
  }
}

export function setPagerSoundEnabled(on) {
  try {
    localStorage.setItem(PREF_KEY, on ? "1" : "0");
  } catch {
    /* storage unavailable: preference just does not persist */
  }
}

let ctx = null;
function audio() {
  if (typeof window === "undefined") return null;
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (!ctx) ctx = new AC();
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  return ctx;
}

// bursts × (high tone, low tone) - the classic pager cadence. Critical
// is longer and more insistent; a resolve/ack is a single soft chirp.
const PATTERN = {
  critical: { bursts: 3, high: 1480, low: 1180, on: 0.18, gap: 0.09, rest: 0.35, gain: 0.35 },
  warning: { bursts: 2, high: 1320, low: 1050, on: 0.16, gap: 0.08, rest: 0.3, gain: 0.28 },
  info: { bursts: 1, high: 1175, low: 990, on: 0.14, gap: 0.07, rest: 0.2, gain: 0.22 },
  ok: { bursts: 1, high: 880, low: 1175, on: 0.1, gap: 0.05, rest: 0.1, gain: 0.15 },
};

export function playPagerTone(severity = "warning") {
  const c = audio();
  if (!c) return false;
  const p = PATTERN[severity] || PATTERN.warning;
  let t = c.currentTime + 0.02;
  for (let b = 0; b < p.bursts; b += 1) {
    for (const f of [p.high, p.low]) {
      const osc = c.createOscillator();
      const g = c.createGain();
      osc.type = "square";
      osc.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(p.gain, t + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t + p.on);
      osc.connect(g).connect(c.destination);
      osc.start(t);
      osc.stop(t + p.on + 0.02);
      t += p.on + p.gap;
    }
    t += p.rest;
  }
  return true;
}

// Wire once from the app root: every push the service worker receives is
// posted here; play the tone unless the user turned it off or the push is
// a resolve (those are quiet).
export function listenForPages(onPage) {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return () => {};
  const handler = (e) => {
    const msg = e.data || {};
    if (msg.type !== "switchboard-page") return;
    const payload = msg.payload || {};
    if (!payload.quiet && isPagerSoundEnabled()) playPagerTone(payload.severity);
    if (onPage) onPage(payload);
  };
  navigator.serviceWorker.addEventListener("message", handler);
  return () => navigator.serviceWorker.removeEventListener("message", handler);
}
