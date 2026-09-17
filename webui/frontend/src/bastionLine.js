// The line editor a read-only bastion session types into.
//
// Why there is one at all: a read-only session's keystrokes never reach
// the device. The server checks whole lines, so the browser has to hold
// the line until Enter and then submit it - which means the browser also
// has to do everything a terminal would otherwise get for free, namely
// echo what you type and let you edit it.
//
// It only runs in read-only mode. A full session is a raw byte pipe and
// the device does its own echoing, completion and history, exactly as it
// would over ssh(1).
//
// Redrawing works by repainting the whole input line - clear to start of
// line, write the prompt, write the buffer, put the cursor back. The
// prompt is not ours, so it is scraped from whatever the device printed
// last (see setPrompt); if that scrape is ever wrong the worst case is a
// cosmetically odd line, never a wrong command, because what gets sent is
// the buffer and nothing else.

const HISTORY_MAX = 200;

export class LineEditor {
  constructor({ write, onSubmit, onHelp, onInterrupt }) {
    this.write = write;
    this.onSubmit = onSubmit;
    this.onHelp = onHelp;
    this.onInterrupt = onInterrupt;
    this.buffer = "";
    this.cursor = 0;
    this.prompt = "";
    this.history = [];
    this.historyAt = null; // null = editing a fresh line, not browsing
    this.draft = "";
    this.enabled = true;
  }

  setPrompt(prompt) {
    this.prompt = prompt || "";
  }

  // Repaint the input line in place.
  render() {
    const tail = this.buffer.length - this.cursor;
    this.write(`\x1b[2K\r${this.prompt}${this.buffer}${tail > 0 ? `\x1b[${tail}D` : ""}`);
  }

  // Wipe our drawing so the device's own echo can take the line over.
  clearLine() {
    this.write("\x1b[2K\r");
  }

  reset() {
    this.buffer = "";
    this.cursor = 0;
    this.historyAt = null;
  }

  remember(line) {
    if (!line.trim()) return;
    if (this.history[this.history.length - 1] === line) return;
    this.history.push(line);
    if (this.history.length > HISTORY_MAX) this.history.shift();
  }

  recall(direction) {
    if (!this.history.length) return;
    if (this.historyAt === null) {
      if (direction > 0) return; // already at the newest entry: nothing below
      this.draft = this.buffer;
      this.historyAt = this.history.length - 1;
    } else {
      const next = this.historyAt + (direction > 0 ? 1 : -1);
      if (next < 0) return;
      if (next >= this.history.length) {
        this.historyAt = null;
        this.buffer = this.draft;
        this.cursor = this.buffer.length;
        this.render();
        return;
      }
      this.historyAt = next;
    }
    this.buffer = this.history[this.historyAt];
    this.cursor = this.buffer.length;
    this.render();
  }

  insert(text) {
    this.buffer = this.buffer.slice(0, this.cursor) + text + this.buffer.slice(this.cursor);
    this.cursor += text.length;
    this.historyAt = null;
  }

  // `?` is context help on every network CLI, so it behaves that way here
  // too rather than being typed as a character - except inside quotes,
  // where it is part of a regex (`| match "e1/?"`).
  inQuotes() {
    let quote = null;
    for (const ch of this.buffer.slice(0, this.cursor)) {
      if (quote) {
        if (ch === quote) quote = null;
      } else if (ch === '"' || ch === "'") {
        quote = ch;
      }
    }
    return quote !== null;
  }

  handle(data) {
    if (!this.enabled) return;
    let i = 0;
    while (i < data.length) {
      const rest = data.slice(i);

      if (rest.startsWith("\x1b[A") || rest.startsWith("\x1bOA")) { this.recall(-1); i += 3; continue; }
      if (rest.startsWith("\x1b[B") || rest.startsWith("\x1bOB")) { this.recall(1); i += 3; continue; }
      if (rest.startsWith("\x1b[C") || rest.startsWith("\x1bOC")) {
        if (this.cursor < this.buffer.length) { this.cursor += 1; this.render(); }
        i += 3; continue;
      }
      if (rest.startsWith("\x1b[D") || rest.startsWith("\x1bOD")) {
        if (this.cursor > 0) { this.cursor -= 1; this.render(); }
        i += 3; continue;
      }
      if (rest.startsWith("\x1b[H") || rest.startsWith("\x1bOH") || rest.startsWith("\x1b[1~")) {
        this.cursor = 0; this.render(); i += rest.startsWith("\x1b[1~") ? 4 : 3; continue;
      }
      if (rest.startsWith("\x1b[F") || rest.startsWith("\x1bOF") || rest.startsWith("\x1b[4~")) {
        this.cursor = this.buffer.length; this.render(); i += rest.startsWith("\x1b[4~") ? 4 : 3; continue;
      }
      if (rest.startsWith("\x1b[3~")) { // Delete
        this.buffer = this.buffer.slice(0, this.cursor) + this.buffer.slice(this.cursor + 1);
        this.render(); i += 4; continue;
      }
      if (rest.startsWith("\x1b")) { // some other escape: swallow it whole
        const next = rest.slice(1).search(/[\x1b]/);
        i += next === -1 ? rest.length : next + 1;
        continue;
      }

      const ch = data[i];
      i += 1;

      if (ch === "\r" || ch === "\n") {
        const line = this.buffer;
        this.remember(line);
        this.clearLine();
        this.reset();
        this.onSubmit(line);
        continue;
      }
      if (ch === "\x7f" || ch === "\b") {
        if (this.cursor > 0) {
          this.buffer = this.buffer.slice(0, this.cursor - 1) + this.buffer.slice(this.cursor);
          this.cursor -= 1;
          this.render();
        }
        continue;
      }
      if (ch === "\x03") { // Ctrl-C: abandon the line and tell the device too
        this.write("^C\r\n");
        this.reset();
        this.onInterrupt();
        continue;
      }
      if (ch === "\x15") { // Ctrl-U
        this.buffer = this.buffer.slice(this.cursor);
        this.cursor = 0;
        this.render();
        continue;
      }
      if (ch === "\x0b") { // Ctrl-K
        this.buffer = this.buffer.slice(0, this.cursor);
        this.render();
        continue;
      }
      if (ch === "\x17") { // Ctrl-W
        const head = this.buffer.slice(0, this.cursor).replace(/\S+\s*$/, "");
        this.buffer = head + this.buffer.slice(this.cursor);
        this.cursor = head.length;
        this.render();
        continue;
      }
      if (ch === "\x01") { this.cursor = 0; this.render(); continue; }          // Ctrl-A
      if (ch === "\x05") { this.cursor = this.buffer.length; this.render(); continue; } // Ctrl-E
      if (ch === "\x0c") { this.write("\x1b[2J\x1b[H"); this.render(); continue; }      // Ctrl-L
      if (ch === "?" && !this.inQuotes()) {
        this.write("?\r\n");
        this.onHelp(this.buffer);
        continue;
      }
      if (ch === "\t") continue; // no completion without sending keystrokes
      if (ch >= " ") { this.insert(ch); this.render(); }
    }
  }
}

// The last line of everything the device has printed, which is its
// prompt whenever it is sitting idle waiting for input. Carriage returns
// and ANSI sequences are stripped so the prompt we redraw is the text
// only.
export function promptFrom(text) {
  const clean = text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").replace(/\r/g, "\n");
  const lines = clean.split("\n");
  return lines[lines.length - 1];
}
