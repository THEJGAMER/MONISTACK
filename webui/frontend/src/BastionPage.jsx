// The console bastion: a real terminal on a device, and the recordings of
// every terminal anyone has opened.
//
// Two modes, and the difference is visible rather than implied. A full
// session is a raw pipe - what you type goes to the device as you type
// it, tab completion and all. A read-only session holds each line in the
// browser until you press Enter, submits it for checking, and only then
// lets the device see it; the terminal says so, and a refusal is printed
// where the output would have been.
//
// Which of those you may open comes from your role, and the server
// decides it again on the socket - the picker here only offers the
// choice.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";

import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

import { LineEditor, promptFrom } from "./bastionLine.js";
import { terminalTheme, watchMode } from "./bastionTheme.js";
import {
  bastionSocketUrl,
  closeBastionSession,
  getBastionAccess,
  getBastionSession,
  listBastionSessions,
} from "./api.js";
import { useAuth } from "./AuthContext.jsx";

const MODE_LABEL = {
  full: "Full - anything the device allows",
  readonly: "Read-only - show commands only",
};

function duration(startIso, endIso) {
  if (!startIso) return "-";
  const end = endIso ? new Date(endIso) : new Date();
  const s = Math.max(0, Math.round((end - new Date(startIso)) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function when(iso) {
  return iso ? new Date(iso).toLocaleString() : "-";
}

function newTerminal(extra = {}) {
  return new Terminal({
    theme: terminalTheme(),
    fontFamily: "Menlo, Consolas, 'DejaVu Sans Mono', monospace",
    fontSize: 13,
    cursorBlink: true,
    scrollback: 10000,
    convertEol: false,
    ...extra,
  });
}

// --- the live terminal --------------------------------------------------

function LiveTerminal({ device, mode, onEnded, pushFlash }) {
  const hostRef = useRef(null);
  const termRef = useRef(null);
  const socketRef = useRef(null);
  const editorRef = useRef(null);
  const [state, setState] = useState("connecting");
  const [ended, setEnded] = useState(null);
  // The terminal's colours have to live in React state, not be read once
  // while rendering: the light/dark toggle happens outside React (it puts
  // a class on <body>), so nothing here re-renders on its own and the
  // padding around the canvas was left painted the old mode's colour -
  // a black frame around a white terminal. xterm's canvas and this
  // wrapper must be repainted from the same value.
  const [theme, setTheme] = useState(terminalTheme);

  useEffect(() => watchMode(setTheme), []);

  useEffect(() => {
    if (termRef.current) termRef.current.options.theme = theme;
  }, [theme]);

  useEffect(() => {
    const term = newTerminal();
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current);
    try {
      fit.fit();
    } catch {
      /* the container has no size yet; the resize observer will catch up */
    }
    termRef.current = term;

    const send = (msg) => {
      if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) {
        socketRef.current.send(JSON.stringify(msg));
      }
    };

    const editor = new LineEditor({
      write: (t) => term.write(t),
      onSubmit: (line) => send({ t: "line", data: line }),
      onHelp: (partial) => send({ t: "help", data: partial }),
      onInterrupt: () => send({ t: "key", name: "ctrl-c" }),
    });
    editorRef.current = editor;

    // Read-only sessions echo locally; full sessions are a raw pipe.
    term.onData((data) => {
      if (mode === "readonly") editor.handle(data);
      else send({ t: "data", data });
    });

    // Dial only once the terminal has been measured against its real
    // container. Connecting first means the login banner arrives into a
    // terminal that is still the default 24x80 and then gets resized
    // underneath it, which leaves the first screenful stranded halfway
    // down the pane - and tells the device a window size that was never
    // true. Two frames: one for Cloudscape to lay the container out, one
    // for the fit to take effect.
    let cancelled = false;
    let socket = null;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (cancelled) return;
        try {
          fit.fit();
        } catch {
          /* still unmeasurable; the device gets the default size */
        }
        socket = openSocket(term.cols, term.rows);
      });
    });

    function openSocket(cols, rows) {
    const url = bastionSocketUrl({ deviceId: device.id, mode, cols, rows });
    const socket = new WebSocket(url);
    socketRef.current = socket;

    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.t === "ready") {
        setState("open");
        if (msg.banner) term.write(msg.banner.replace(/\n/g, "\r\n"));
        if (mode === "readonly") editor.setPrompt(promptFrom(msg.banner || ""));
        term.focus();
      } else if (msg.t === "out") {
        term.write(msg.data);
        if (mode === "readonly") editor.setPrompt(promptFrom(msg.data));
      } else if (msg.t === "refused") {
        // Printed where the output would have been, so the transcript
        // reads as one story rather than the terminal going quiet and an
        // explanation appearing somewhere else on the page.
        term.write(`\r\n\x1b[33mRefused:\x1b[0m ${msg.reason}\r\n`);
        editor.render();
      } else if (msg.t === "error") {
        term.write(`\r\n\x1b[31m${msg.message}\x1b[0m\r\n`);
      } else if (msg.t === "closed") {
        setState("closed");
        setEnded(msg.reason);
        term.write(`\r\n\x1b[90m-- session ended: ${msg.reason} --\x1b[0m\r\n`);
        editor.enabled = false;
        if (onEnded) onEnded(msg.reason);
      }
    };
    socket.onerror = () => {
      if (state !== "closed") pushFlash("error", "The terminal connection failed.");
    };
    socket.onclose = () => {
      setState((s) => (s === "closed" ? s : "closed"));
      editor.enabled = false;
    };
    return socket;
    }

    const resize = () => {
      try {
        fit.fit();
      } catch {
        return;
      }
      send({ t: "resize", cols: term.cols, rows: term.rows });
    };
    const observer = new ResizeObserver(resize);
    observer.observe(hostRef.current);

    return () => {
      cancelled = true;
      observer.disconnect();
      try {
        if (socket) socket.close();
      } catch {
        /* already gone */
      }
      term.dispose();
    };
    // One terminal per (device, mode): changing either tears this one down
    // and dials a new session, which is what closing and reopening means.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [device.id, mode]);

  return (
    <SpaceBetween size="s">
      <Box>
        {state === "connecting" ? (
          <StatusIndicator type="loading">Opening an SSH session to {device.name}</StatusIndicator>
        ) : state === "open" ? (
          <SpaceBetween size="xs" direction="horizontal">
            <StatusIndicator type="success">Connected to {device.name}</StatusIndicator>
            <Badge color={mode === "full" ? "red" : "blue"}>{mode === "full" ? "Full access" : "Read-only"}</Badge>
          </SpaceBetween>
        ) : (
          <StatusIndicator type="stopped">Session ended{ended ? `: ${ended}` : ""}</StatusIndicator>
        )}
      </Box>
      <div
        ref={hostRef}
        style={{
          height: "58vh",
          minHeight: "320px",
          padding: "8px",
          borderRadius: "8px",
          background: theme.background,
        }}
      />
      <Box color="text-body-secondary" fontSize="body-s">
        {mode === "readonly"
          ? "Each line is checked before the device sees it. Enter submits, ? asks the device for context help, "
            + "Ctrl-C aborts a running command, and the up arrow walks back through what you have typed. "
            + "Tab completion is off, because completing a command needs keystrokes to reach the device unchecked."
          : "Keystrokes go straight to the device, including tab completion and configuration mode. "
            + "Everything typed and everything printed is recorded."}
      </Box>
    </SpaceBetween>
  );
}

// --- replay -------------------------------------------------------------

function ReplayModal({ sessionId, onDismiss }) {
  const hostRef = useRef(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [playing, setPlaying] = useState(false);
  const [theme, setTheme] = useState(terminalTheme);
  const timers = useRef([]);

  useEffect(() => watchMode(setTheme), []);

  useEffect(() => {
    let live = true;
    getBastionSession(sessionId)
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [sessionId]);

  const clearTimers = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  const writeAll = useCallback((term, chunks) => {
    chunks.forEach((c) => {
      // Device output already carries its own carriage returns. Notes are
      // Switchboard's own prose and use bare newlines, which a terminal
      // reads as "down one line, same column" - so a two-line refusal
      // came out as a staircase until they were converted.
      if (c.stream === "out") term.write(c.data);
      else if (c.stream === "note") term.write(`\r\n\x1b[90m${c.data.replace(/\r?\n/g, "\r\n")}\x1b[0m\r\n`);
    });
  }, []);

  useEffect(() => {
    if (!data || !hostRef.current) return undefined;
    const term = newTerminal({ cursorBlink: false, disableStdin: true });
    term.open(hostRef.current);
    writeAll(term, data.chunks);
    hostRef.current.__term = term;
    return () => {
      clearTimers();
      term.dispose();
    };
  }, [data, writeAll]);

  useEffect(() => {
    const term = hostRef.current && hostRef.current.__term;
    if (term) term.options.theme = theme;
  }, [theme, data]);

  // Real-time playback: the gaps between chunks are the gaps the person
  // sat through, capped at three seconds so a session with a coffee break
  // in it is still watchable.
  const play = () => {
    const term = hostRef.current && hostRef.current.__term;
    if (!term || !data) return;
    clearTimers();
    term.reset();
    setPlaying(true);
    const start = new Date(data.chunks[0].at).getTime();
    let clock = 0;
    let previous = start;
    data.chunks.forEach((c, i) => {
      const at = new Date(c.at).getTime();
      clock += Math.min(at - previous, 3000);
      previous = at;
      timers.current.push(
        setTimeout(() => {
          writeAll(term, [c]);
          if (i === data.chunks.length - 1) setPlaying(false);
        }, clock),
      );
    });
  };

  const showAll = () => {
    const term = hostRef.current && hostRef.current.__term;
    if (!term || !data) return;
    clearTimers();
    setPlaying(false);
    term.reset();
    writeAll(term, data.chunks);
  };

  const commands = useMemo(() => (data ? data.chunks.filter((c) => c.stream === "in") : []), [data]);
  const session = data && data.session;

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      size="max"
      header={session ? `${session.actor} on ${session.device_name || session.device_id}` : "Session recording"}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            Close
          </Button>
        </Box>
      }
    >
      {error ? (
        <Alert type="error">{error}</Alert>
      ) : !data ? (
        <Box textAlign="center" padding="l">
          <Spinner size="large" />
        </Box>
      ) : (
        <SpaceBetween size="m">
          <KeyValuePairs
            columns={4}
            items={[
              { label: "Started", value: when(session.started_at) },
              { label: "Lasted", value: duration(session.started_at, session.ended_at) },
              { label: "Mode", value: session.mode === "full" ? "Full access" : "Read-only" },
              { label: "Ended", value: session.end_reason || "-" },
              { label: "From", value: session.client_ip || "-" },
              { label: "Commands", value: String(session.commands) },
              { label: "Refused", value: String(session.refused) },
              { label: "Recording", value: session.truncated ? "Cut short at the size limit" : "Complete" },
            ]}
          />
          <SpaceBetween size="xs" direction="horizontal">
            <Button onClick={play} disabled={playing} iconName="caret-right-filled">
              Play at the speed it happened
            </Button>
            <Button onClick={showAll}>Show the whole transcript</Button>
          </SpaceBetween>
          <div
            ref={hostRef}
            style={{ height: "45vh", padding: "8px", borderRadius: "8px", background: theme.background }}
          />
          <Table
            variant="embedded"
            header={<Header counter={`(${commands.length})`}>Commands submitted</Header>}
            items={commands}
            columnDefinitions={[
              { id: "at", header: "At", cell: (c) => when(c.at), width: 200 },
              { id: "cmd", header: "Command", cell: (c) => <Box fontFamily="monospace">{c.data}</Box> },
            ]}
            empty={<Box textAlign="center" color="text-body-secondary">Nothing was submitted.</Box>}
          />
        </SpaceBetween>
      )}
    </Modal>
  );
}

// --- the page -----------------------------------------------------------

export default function BastionPage({ devices, pushFlash }) {
  const { role } = useAuth();
  const [access, setAccess] = useState(null);
  const [error, setError] = useState(null);
  const [deviceId, setDeviceId] = useState(null);
  const [mode, setMode] = useState(null);
  const [connected, setConnected] = useState(null); // {device, mode} while a terminal is up
  const [sessions, setSessions] = useState([]);
  const [replayId, setReplayId] = useState(null);
  const [tab, setTab] = useState("terminal");

  const refreshSessions = useCallback(() => {
    listBastionSessions(100)
      .then(setSessions)
      .catch((e) => pushFlash("error", `Could not load session recordings: ${e.message}`));
  }, [pushFlash]);

  useEffect(() => {
    getBastionAccess()
      .then((a) => {
        setAccess(a);
        if (a.modes.length) setMode(a.modes[a.modes.length - 1]);
        if (a.devices.length) setDeviceId(a.devices[0].id);
      })
      .catch((e) => setError(e.message));
    refreshSessions();
  }, [refreshSessions]);

  const deviceList = (access && access.devices) || devices || [];
  const device = deviceList.find((d) => d.id === deviceId) || null;
  const modes = (access && access.modes) || [];

  if (error) {
    return <Alert type="error" header="Console bastion unavailable">{error}</Alert>;
  }
  if (!access) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }
  if (!access.enabled) {
    return (
      <Alert type="info" header="The console bastion is switched off">
        This deployment has BASTION_ENABLED=0 set, so no interactive sessions can be opened. The Console page and
        its allowlisted commands are unaffected.
      </Alert>
    );
  }
  if (!modes.length) {
    return (
      <Alert type="info" header="Your role cannot open a console session">
        The bastion is free text to a live device, so it needs the operator role at least. You have {role}.
      </Alert>
    );
  }

  const terminalTab = (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description={
              "A real SSH session to the device, opened with Switchboard's own stored credentials - nobody needs "
              + "the device's password. Everything typed and everything printed is recorded and kept for a year."
            }
            actions={
              connected ? (
                <Button onClick={() => setConnected(null)} iconName="close">
                  Disconnect
                </Button>
              ) : (
                <Button
                  variant="primary"
                  disabled={!device || !mode}
                  onClick={() => setConnected({ device, mode })}
                  iconName="caret-right-filled"
                >
                  Open a session
                </Button>
              )
            }
          >
            Console bastion
          </Header>
        }
      >
        <ColumnLayout columns={3}>
          <FormField label="Device" description="Any device Switchboard can already reach.">
            <Select
              selectedOption={device ? { value: device.id, label: `${device.name} (${device.platform})` } : null}
              options={deviceList.map((d) => ({ value: d.id, label: `${d.name} (${d.platform})`, description: d.host }))}
              onChange={(e) => setDeviceId(e.detail.selectedOption.value)}
              disabled={!!connected}
            />
          </FormField>
          <FormField
            label="Access"
            description={
              modes.length > 1
                ? "You may open either. Read-only is the safer default."
                : "Your role allows read-only sessions."
            }
          >
            <Select
              selectedOption={mode ? { value: mode, label: MODE_LABEL[mode] } : null}
              options={modes.map((m) => ({ value: m, label: MODE_LABEL[m] }))}
              onChange={(e) => setMode(e.detail.selectedOption.value)}
              disabled={!!connected || modes.length === 1}
            />
          </FormField>
          <FormField label="Limits" description="Switches have very few SSH slots.">
            <Box>
              {access.limits.per_device} session(s) per device, {access.limits.total} in total. Idle sessions close
              after {Math.round(access.limits.idle_seconds / 60)} minutes.
            </Box>
          </FormField>
        </ColumnLayout>
      </Container>

      {connected ? (
        <Container>
          <LiveTerminal
            key={`${connected.device.id}:${connected.mode}`}
            device={connected.device}
            mode={connected.mode}
            pushFlash={pushFlash}
            onEnded={refreshSessions}
          />
        </Container>
      ) : (
        <Container>
          <Box textAlign="center" color="text-body-secondary" padding="xl">
            Pick a device and open a session. Nothing is connected yet, so no SSH slot is being held.
          </Box>
        </Container>
      )}

      {mode === "full" ? (
        <Alert type="warning" header="Full access sends your keystrokes straight to the device">
          Configuration commands, reloads and clears all work. The device's own safeguards are the only thing between
          a typo and an outage, so the whole session is recorded for a year and every command lands in the audit log.
        </Alert>
      ) : null}
    </SpaceBetween>
  );

  const recordingsTab = (
    <Table
      header={
        <Header
          counter={`(${sessions.length})`}
          actions={<Button iconName="refresh" onClick={refreshSessions} ariaLabel="Refresh" />}
          description={
            role === "admin"
              ? "Every session anyone has opened. Kept for a year, the same as the audit log."
              : "The sessions you have opened. Kept for a year, the same as the audit log."
          }
        >
          Session recordings
        </Header>
      }
      items={sessions}
      trackBy="id"
      variant="container"
      resizableColumns
      wrapLines={false}
      // Widths are set rather than left to fit: nine columns of unbounded
      // text pushed the Replay button off the right edge and wrapped the
      // access badge onto two lines, tripling every row's height.
      columnDefinitions={[
        { id: "started", header: "Started", width: 160, cell: (s) => when(s.started_at), sortingField: "started_at" },
        { id: "device", header: "Device", width: 165, cell: (s) => s.device_name || s.device_id },
        { id: "actor", header: "Who", width: 170, cell: (s) => `${s.actor} (${s.role})` },
        {
          id: "mode",
          header: "Access",
          width: 120,
          // A non-breaking hyphen: "Read-only" split across two lines inside
          // the badge and made every row half again as tall.
          cell: (s) => <Badge color={s.mode === "full" ? "red" : "blue"}>{s.mode === "full" ? "Full" : "Read\u2011only"}</Badge>,
        },
        { id: "duration", header: "Lasted", width: 90, cell: (s) => duration(s.started_at, s.ended_at) },
        {
          // Commands and refusals share a column: a refusal is a command
          // that did not run, so the two numbers only mean anything beside
          // each other, and two columns for them pushed Replay off the
          // right-hand edge.
          id: "commands",
          header: "Commands",
          width: 150,
          cell: (s) =>
            s.refused ? (
              <StatusIndicator type="warning">
                {s.commands}, {s.refused} refused
              </StatusIndicator>
            ) : (
              String(s.commands)
            ),
        },
        {
          id: "state",
          header: "State",
          width: 150,
          cell: (s) =>
            s.ended_at ? (
              <Box color="text-body-secondary">{s.end_reason || "closed"}</Box>
            ) : (
              <StatusIndicator type="in-progress">live</StatusIndicator>
            ),
        },
        {
          id: "actions",
          header: "",
          width: 125,
          cell: (s) => (
            <SpaceBetween size="xs" direction="horizontal">
              <Button variant="inline-link" onClick={() => setReplayId(s.id)}>
                Replay
              </Button>
              {!s.ended_at && role === "admin" ? (
                <Button
                  variant="inline-link"
                  onClick={() =>
                    closeBastionSession(s.id)
                      .then(refreshSessions)
                      .catch((e) => pushFlash("error", e.message))
                  }
                >
                  End it
                </Button>
              ) : null}
            </SpaceBetween>
          ),
        },
      ]}
      empty={
        <Box textAlign="center" color="text-body-secondary" padding="m">
          No console sessions have been opened yet.
        </Box>
      }
    />
  );

  return (
    <>
      <Tabs
        activeTabId={tab}
        onChange={(e) => setTab(e.detail.activeTabId)}
        tabs={[
          { id: "terminal", label: "Terminal", content: terminalTab },
          { id: "recordings", label: "Session recordings", content: recordingsTab },
        ]}
      />
      {replayId ? <ReplayModal sessionId={replayId} onDismiss={() => setReplayId(null)} /> : null}
    </>
  );
}
