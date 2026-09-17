// Terminal colours, and keeping them in step with the app's light/dark
// mode.
//
// xterm paints to a canvas, so it cannot inherit a CSS custom property
// the way every other surface in this app does - it needs concrete
// colours handed to it, and handed to it again whenever the mode changes.
// That is the whole reason this file exists, and it is worth being
// explicit about: the last time something in this UI rendered on a
// stubbornly white background, the cause was exactly this class of
// problem - a component whose colours came from somewhere the theme
// switch could not reach.
//
// Cloudscape signals dark mode by putting `awsui-dark-mode` on <body>
// (applyMode in @cloudscape-design/global-styles), so that class is the
// single source of truth here too.

const DARK = {
  background: "#0f1b2a",
  foreground: "#d1d5db",
  cursor: "#7aa7d8",
  cursorAccent: "#0f1b2a",
  selectionBackground: "#2a4a6b",
  black: "#16191f", red: "#ff7b72", green: "#7ee787", yellow: "#f0c674",
  blue: "#79b8ff", magenta: "#d2a8ff", cyan: "#76e3ea", white: "#d1d5db",
  brightBlack: "#687078", brightRed: "#ffa198", brightGreen: "#9ff0a8", brightYellow: "#ffd98a",
  brightBlue: "#a5d6ff", brightMagenta: "#e2c5ff", brightCyan: "#a2f0f5", brightWhite: "#ffffff",
};

const LIGHT = {
  background: "#ffffff",
  foreground: "#16191f",
  cursor: "#0972d3",
  cursorAccent: "#ffffff",
  selectionBackground: "#b8d7f5",
  black: "#16191f", red: "#d13212", green: "#037f0c", yellow: "#8d6605",
  blue: "#0972d3", magenta: "#8b3fc9", cyan: "#00697a", white: "#414d5c",
  brightBlack: "#5f6b7a", brightRed: "#eb5f07", brightGreen: "#1d8102", brightYellow: "#a8720a",
  brightBlue: "#2074d5", brightMagenta: "#a555e8", brightCyan: "#1a8fa3", brightWhite: "#000000",
};

export function isDarkMode() {
  return typeof document !== "undefined" && document.body.classList.contains("awsui-dark-mode");
}

export function terminalTheme() {
  return isDarkMode() ? DARK : LIGHT;
}

// Calls back whenever the app's visual mode changes, so a terminal that
// is already open re-themes instead of staying the colour it was born.
export function watchMode(onChange) {
  if (typeof MutationObserver === "undefined") return () => {};
  let last = isDarkMode();
  const observer = new MutationObserver(() => {
    const now = isDarkMode();
    if (now !== last) {
      last = now;
      onChange(terminalTheme());
    }
  });
  observer.observe(document.body, { attributes: true, attributeFilter: ["class"] });
  return () => observer.disconnect();
}
