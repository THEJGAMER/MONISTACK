/**
 * Alarms - the operational log, one entry per *occurrence*.
 *
 * An occurrence is a single fired-to-resolved episode with its own id, its
 * own acknowledgement and its own discussion. Four flaps of the same port
 * are four entries here, deliberately: they are four separate things that
 * happened, and collapsing them would lose who handled which one.
 *
 * Occurrences of the same alarm are linked rather than merged - each one
 * lists its predecessors, the way a ticketing system opens a new ticket
 * and references prior ones instead of reopening something closed. Every
 * occurrence has its own URL (`#/alarms/<id>`) so it can be pasted to a
 * colleague and open on exactly that episode.
 */
import React, { useCallback, useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import FormField from "@cloudscape-design/components/form-field";
import Tabs from "@cloudscape-design/components/tabs";
import Pagination from "@cloudscape-design/components/pagination";
import TextFilter from "@cloudscape-design/components/text-filter";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Textarea from "@cloudscape-design/components/textarea";
import Badge from "@cloudscape-design/components/badge";
import Popover from "@cloudscape-design/components/popover";
import Modal from "@cloudscape-design/components/modal";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";
import SegmentedControl from "@cloudscape-design/components/segmented-control";

import { useClientPagination } from "./useClientPagination.js";
import MiniMarkdown from "./MiniMarkdown.jsx";
import {
  ackAlarm,
  addComment,
  delayPage,
  deleteComment,
  enablePaging,
  getAlarm,
  getAlarms,
  nargAlarm,
  pageNow,
  resolveAlarm,
  unackAlarm,
} from "./api.js";

// An occurrence is open until a resolve closes it. "pending" is logged the
// moment a condition is first seen - inside Prometheus's for: window, or
// an interface's delayed-mode countdown - not only once it actually fires,
// so a flap that recovers before ever firing still leaves a record instead
// of vanishing with nothing to investigate later. "expired" means it was
// never resolved - the alarm aged out of Alertmanager instead of clearing,
// which is the one state that says the pipeline dropped something.
const STATE_LABELS = {
  open: "open",
  pending: "pending (not yet confirmed)",
  resolved: "resolved",
  expired: "expired (never resolved)",
};

function stateType(state) {
  if (state === "open") return "error";
  if (state === "pending") return "in-progress";
  if (state === "resolved") return "success";
  if (state === "expired") return "warning";
  return "pending";
}

function severityType(sev) {
  if (sev === "critical") return "error";
  if (sev === "warning") return "warning";
  return "info";
}

// Occurrence ids are shown with a prefix so they read as a reference you
// can quote to someone ("ALM-42") rather than a bare number.
const alarmRef = (id) => `ALM-${id}`;

function duration(from, to) {
  if (!from) return "-";
  const ms = (to ? new Date(to) : new Date()) - new Date(from);
  if (ms < 0) return "-";
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  return hrs < 24 ? `${hrs}h ${mins % 60}m` : `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
}

/**
 * Countdown to the moment this alarm pages someone.
 *
 * Ticks locally every second so it reads like a clock, but the *target* is
 * always the server's `page_at` - the parent re-fetches every 5s and passes
 * a fresh one in. Local ticking alone would drift against the server and,
 * worse, would keep counting confidently after someone else pressed "page
 * now" or NARG from another browser; re-syncing means the worst case is
 * being up to 5s stale rather than silently wrong.
 */
function PageCountdown({ pageAt }) {
  const remaining = () => Math.max(0, Math.round((new Date(pageAt) - Date.now()) / 1000));
  const [left, setLeft] = useState(remaining);

  useEffect(() => {
    setLeft(remaining());
    const t = setInterval(() => setLeft(remaining()), 1000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageAt]);

  if (left <= 0) return <StatusIndicator type="in-progress">paging now…</StatusIndicator>;
  const mins = Math.floor(left / 60);
  const secs = String(left % 60).padStart(2, "0");
  // Under a minute is the point where someone might actually act on it.
  return (
    <StatusIndicator type={left <= 60 ? "warning" : "pending"}>
      pages in {mins}:{secs}
    </StatusIndicator>
  );
}

function PagingStatus({ alarm }) {
  if (alarm.paging_disabled) return <StatusIndicator type="stopped">paging off (NARG)</StatusIndicator>;
  if (alarm.paged_at) {
    return <StatusIndicator type="success">paged {new Date(alarm.paged_at).toLocaleTimeString()}</StatusIndicator>;
  }
  if (alarm.page_at) return <PageCountdown pageAt={alarm.page_at} />;
  return <Box color="text-body-secondary">-</Box>;
}

const EVENT_TYPES = {
  fired: "error",
  resolved: "success",
  acknowledged: "info",
  unacknowledged: "warning",
  "manually resolved": "success",
  comment: "pending",
  "comment removed": "stopped",
};

function AckCell({ ack }) {
  if (!ack) return <StatusIndicator type="warning">unacknowledged</StatusIndicator>;
  const when = new Date(ack.acked_at).toLocaleString();
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="medium"
      triggerType="text"
      content={
        <SpaceBetween size="xxs">
          <Box>Acknowledged by {ack.acked_by}</Box>
          <Box>{when}</Box>
          {ack.note && <Box>Note: {ack.note}</Box>}
        </SpaceBetween>
      }
    >
      <StatusIndicator type="success">
        {ack.acked_by} · {when}
      </StatusIndicator>
    </Popover>
  );
}

function EventTable({ events, emptyText }) {
  return (
    <Table
      variant="embedded"
      items={[...events].reverse()}
      columnDefinitions={[
        { id: "ts", header: "Time", minWidth: 190, cell: (e) => new Date(e.ts).toLocaleString() },
        {
          id: "kind",
          header: "Event",
          minWidth: 170,
          cell: (e) => <StatusIndicator type={EVENT_TYPES[e.kind] || "info"}>{e.kind}</StatusIndicator>,
        },
        { id: "actor", header: "By", minWidth: 110, cell: (e) => e.actor },
        { id: "detail", header: "Detail", cell: (e) => e.summary || e.description },
      ]}
      empty={<Box textAlign="center">{emptyText}</Box>}
    />
  );
}

// Comments are shown through MiniMarkdown by default rather than plain
// text, specifically so a Shift+Enter line break survives to the screen:
// plain HTML text flow collapses newlines unless something turns them
// into real block boundaries, which is exactly what was happening before
// this - a comment typed as several lines rendered back as one run-on
// paragraph. MiniMarkdown already splits on "\n" and gives each line its
// own block, which fixes that as a side effect of also rendering
// **bold**/`code`/## headings - the same renderer already used for Saved
// Results output, not a second markdown implementation.
function CommentBody({ text }) {
  const [view, setView] = useState("markdown");
  return (
    <SpaceBetween size="xs">
      <SegmentedControl
        selectedId={view}
        onChange={({ detail }) => setView(detail.selectedId)}
        options={[
          { id: "markdown", text: "Markdown" },
          { id: "raw", text: "Raw" },
        ]}
      />
      {view === "markdown" ? (
        <div className="markdown-box">
          <MiniMarkdown source={text} codeStyle="snippet" />
        </div>
      ) : (
        // Plain source text, not a fake terminal window - reported live as
        // wrong for this context (a comment isn't device CLI output, so a
        // black terminal box with title-bar dots read as bizarre here).
        // Reuses .markdown-code-block, the same plain theme-aware style
        // Markdown's own code fences now use, so Raw and rendered-fence
        // text look consistent with each other rather than one being a
        // terminal and the other not.
        <Box variant="code" display="block" className="markdown-code-block">
          {text}
        </Box>
      )}
    </SpaceBetween>
  );
}

function CommunicationTab({ alarm, onChanged, pushFlash }) {
  const [body, setBody] = useState("");
  const [composerView, setComposerView] = useState("write");
  const [busy, setBusy] = useState(false);
  const comments = alarm.comments || [];

  async function post() {
    if (!body.trim()) return;
    setBusy(true);
    try {
      await addComment(alarm.id, body.trim());
      setBody("");
      setComposerView("write");
      await onChanged();
    } catch (e) {
      pushFlash("error", `Could not post comment: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id) {
    try {
      await deleteComment(alarm.id, id);
      await onChanged();
    } catch (e) {
      pushFlash("error", `Could not delete comment: ${e.message}`);
    }
  }

  return (
    <Container
      header={
        <Header
          variant="h3"
          description="Discussion on this occurrence only. Separate from the audit log: this is conversation you can correct, that is a tamper-evident record of what was done."
        >
          Communication
        </Header>
      }
    >
      <SpaceBetween size="m">
        <FormField
          label="Add a comment"
          description="Visible to anyone with access to this alarm's link. Shift+Enter for a new line; ## headings, **bold** and `code` are supported."
          secondaryControl={
            <SegmentedControl
              selectedId={composerView}
              onChange={({ detail }) => setComposerView(detail.selectedId)}
              options={[
                { id: "write", text: "Write" },
                { id: "preview", text: "Preview" },
              ]}
            />
          }
        >
          {composerView === "write" ? (
            <Textarea
              value={body}
              onChange={({ detail }) => setBody(detail.value)}
              placeholder="What did you find? What's the plan? Who's picking this up?"
              rows={5}
            />
          ) : (
            <div className="markdown-box">
              {body.trim() ? (
                <MiniMarkdown source={body} codeStyle="snippet" />
              ) : (
                <Box color="text-body-secondary">Nothing to preview yet.</Box>
              )}
            </div>
          )}
        </FormField>
        <Box>
          <Button variant="primary" loading={busy} disabled={!body.trim()} onClick={post}>
            Post comment
          </Button>
        </Box>

        {comments.length === 0 && <Box color="text-body-secondary">No comments yet. Start the thread above.</Box>}
        {comments.map((c) => (
          <Container
            key={c.id}
            variant="stacked"
            header={
              <Header
                variant="h3"
                actions={
                  (c.author === alarm.current_user || alarm.current_role === "admin") && (
                    <Button variant="inline-link" onClick={() => remove(c.id)}>
                      Delete
                    </Button>
                  )
                }
              >
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  <Badge color="blue">{c.author}</Badge>
                  <Box fontSize="body-s" color="text-body-secondary">
                    {new Date(c.created_at).toLocaleString()}
                  </Box>
                </SpaceBetween>
              </Header>
            }
          >
            <CommentBody text={c.body} />
          </Container>
        ))}
      </SpaceBetween>
    </Container>
  );
}

function PreviousOccurrences({ previous, total, onNavigate }) {
  return (
    <Container
      header={
        <Header
          variant="h3"
          counter={total ? `(${total} total)` : undefined}
          description="Earlier occurrences of this same alarm. Each is its own record with its own acknowledgement and discussion - linked here, not merged into this one."
        >
          Previous occurrences
        </Header>
      }
    >
      <Table
        variant="embedded"
        items={previous}
        columnDefinitions={[
          {
            id: "id",
            header: "Alarm",
            minWidth: 120,
            cell: (o) => (
              <Button variant="inline-link" onClick={() => onNavigate(`#/alarms/${o.id}`)}>
                {alarmRef(o.id)}
              </Button>
            ),
          },
          {
            id: "state",
            header: "Status",
            minWidth: 180,
            cell: (o) => <StatusIndicator type={stateType(o.state)}>{STATE_LABELS[o.state] || o.state}</StatusIndicator>,
          },
          { id: "started", header: "Started", minWidth: 190, cell: (o) => new Date(o.started_at).toLocaleString() },
          { id: "lasted", header: "Lasted", minWidth: 110, cell: (o) => duration(o.started_at, o.resolved_at) },
          { id: "owner", header: "Owner", minWidth: 190, cell: (o) => <AckCell ack={o.ack} /> },
        ]}
        empty={<Box textAlign="center">This is the first recorded occurrence of this alarm.</Box>}
      />
    </Container>
  );
}

function AlarmDetail({ alarmId, pushFlash, onNavigate }) {
  const [alarm, setAlarm] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [ackNote, setAckNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmResolve, setConfirmResolve] = useState(false);
  const [confirmNarg, setConfirmNarg] = useState(false);
  const [nargReason, setNargReason] = useState("");
  const [copied, setCopied] = useState(false);
  const [tab, setTab] = useState("timeline");

  const load = useCallback(async () => {
    try {
      setAlarm(await getAlarm(alarmId));
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [alarmId]);

  // 5s while a page is still pending (the countdown needs to stay in sync
  // with the server, and someone else may press page-now or NARG), 30s
  // once there is nothing counting down.
  const pagePending = alarm && !alarm.paged_at && !alarm.paging_disabled && alarm.page_at;
  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);
  useEffect(() => {
    const t = setInterval(load, pagePending ? 5000 : 30000);
    return () => clearInterval(t);
  }, [load, pagePending]);

  async function run(fn, message) {
    setBusy(true);
    try {
      await fn();
      pushFlash("success", message);
      setAckNote("");
      await load();
    } catch (e) {
      pushFlash("error", e.message);
    } finally {
      setBusy(false);
      setConfirmResolve(false);
      setConfirmNarg(false);
    }
  }

  function copyLink() {
    const url = `${window.location.origin}${window.location.pathname}#/alarms/${alarmId}`;
    navigator.clipboard?.writeText(url).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2500);
      },
      () => pushFlash("error", "Could not copy - the URL in your address bar is the link."),
    );
  }

  if (loading && !alarm) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error) {
    return (
      <Alert
        type="error"
        header="Could not load this alarm"
        action={<Button onClick={() => onNavigate("#/alarms")}>Back to the alarm log</Button>}
      >
        {error} — the id in this link may be wrong, or this record may have been removed.
      </Alert>
    );
  }

  const labels = alarm.labels || {};

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description={alarm.summary}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName={copied ? "status-positive" : "copy"} onClick={copyLink}>
                  {copied ? "Link copied" : "Copy link"}
                </Button>
                <Button onClick={() => onNavigate("#/alarms")}>Alarm log</Button>
              </SpaceBetween>
            }
          >
            {alarmRef(alarm.id)} · {alarm.alertname}
          </Header>
        }
      >
        <SpaceBetween size="m">
          <KeyValuePairs
            columns={4}
            items={[
              {
                label: "Status",
                value: (
                  <StatusIndicator type={stateType(alarm.state)}>{STATE_LABELS[alarm.state] || alarm.state}</StatusIndicator>
                ),
              },
              {
                label: "Severity",
                value: <StatusIndicator type={severityType(alarm.severity)}>{alarm.severity || "-"}</StatusIndicator>,
              },
              { label: "Owner", value: <AckCell ack={alarm.ack} /> },
              { label: "Paging", value: <PagingStatus alarm={alarm} /> },
              { label: "Lasted", value: duration(alarm.started_at, alarm.resolved_at) },
              { label: "Started", value: alarm.started_at ? new Date(alarm.started_at).toLocaleString() : "-" },
              {
                label: "Resolved",
                value: alarm.resolved_at ? new Date(alarm.resolved_at).toLocaleString() : "not yet",
              },
              {
                label: "Labels",
                value: (
                  <SpaceBetween direction="horizontal" size="xxs">
                    {Object.entries(labels).map(([k, v]) => (
                      <Badge key={k}>
                        {k}={v}
                      </Badge>
                    ))}
                  </SpaceBetween>
                ),
              },
              {
                label: "Occurrence",
                value: (
                  <Box fontSize="body-s">
                    {alarm.occurrences_for_signature > 1
                      ? `${alarm.occurrences_for_signature} recorded for this alarm`
                      : "first recorded"}
                  </Box>
                ),
              },
            ]}
          />

          <SpaceBetween direction="horizontal" size="xs">
            {alarm.ack ? (
              <Button loading={busy} onClick={() => run(() => unackAlarm(alarm.id), "Acknowledgement removed.")}>
                Un-acknowledge
              </Button>
            ) : (
              <Button
                variant="primary"
                loading={busy}
                onClick={() => run(() => ackAlarm(alarm.id, ackNote.trim() || null), "Acknowledged.")}
              >
                Acknowledge
              </Button>
            )}
            {alarm.state === "open" && (
              <Button loading={busy} onClick={() => setConfirmResolve(true)}>
                Resolve
              </Button>
            )}
          </SpaceBetween>

          {alarm.state === "open" && (
            <Container
              variant="stacked"
              header={
                <Header
                  variant="h3"
                  description="Paging is held briefly so an alarm can be looked at before it wakes anyone. Holding is done with an Alertmanager silence, so if Switchboard is down the alarm pages immediately rather than being lost."
                >
                  Paging control
                </Header>
              }
            >
              <SpaceBetween direction="horizontal" size="xs">
                {!alarm.paged_at && !alarm.paging_disabled && (
                  <>
                    <Button variant="primary" loading={busy} onClick={() => run(() => pageNow(alarm.id), "Paging now.")}>
                      Page now
                    </Button>
                    <Button loading={busy} onClick={() => run(() => delayPage(alarm.id, 300), "Paging delayed 5 minutes.")}>
                      Delay 5m
                    </Button>
                    <Button loading={busy} onClick={() => run(() => delayPage(alarm.id, 900), "Paging delayed 15 minutes.")}>
                      Delay 15m
                    </Button>
                  </>
                )}
                {alarm.paging_disabled ? (
                  <Button loading={busy} onClick={() => run(() => enablePaging(alarm.id), "Paging re-enabled.")}>
                    Re-enable paging
                  </Button>
                ) : (
                  <Button loading={busy} onClick={() => setConfirmNarg(true)}>
                    NARG (stop paging)
                  </Button>
                )}
              </SpaceBetween>
            </Container>
          )}
          {!alarm.ack && (
            <FormField label="Acknowledgement note" description="Optional - why you're taking this, or what you already know.">
              <Textarea value={ackNote} onChange={({ detail }) => setAckNote(detail.value)} />
            </FormField>
          )}
        </SpaceBetween>
      </Container>

      <Tabs
        activeTabId={tab}
        onChange={({ detail }) => setTab(detail.activeTabId)}
        tabs={[
          {
            id: "timeline",
            label: "Timeline",
            content: (
              <Container
                header={
                  <Header variant="h3" description="Everything that happened during this occurrence - system events and operator actions interleaved.">
                    Full timeline
                  </Header>
                }
              >
                <EventTable events={alarm.events || []} emptyText="No events recorded yet." />
              </Container>
            ),
          },
          {
            id: "communication",
            label: `Communication${alarm.comments?.length ? ` (${alarm.comments.length})` : ""}`,
            content: <CommunicationTab alarm={alarm} onChanged={load} pushFlash={pushFlash} />,
          },
          {
            id: "events",
            label: "Event log",
            content: (
              <Container
                header={
                  <Header variant="h3" description="What the alerting system did during this occurrence: the firing and resolved notifications Alertmanager sent.">
                    Event log
                  </Header>
                }
              >
                <EventTable events={alarm.system_events || []} emptyText="No system events yet." />
              </Container>
            ),
          },
          {
            id: "audit",
            label: "Audit log",
            content: (
              <Container
                header={
                  <Header variant="h3" description="What people did to this occurrence: acknowledgements, manual resolves and comments, with who and when.">
                    Audit log
                  </Header>
                }
              >
                <EventTable events={alarm.operator_events || []} emptyText="No operator actions on this occurrence yet." />
              </Container>
            ),
          },
          {
            id: "previous",
            label: `Previous${alarm.previous_occurrences?.length ? ` (${alarm.previous_occurrences.length})` : ""}`,
            content: (
              <PreviousOccurrences
                previous={alarm.previous_occurrences || []}
                total={alarm.occurrences_for_signature}
                onNavigate={onNavigate}
              />
            ),
          },
        ]}
      />

      <Modal
        visible={confirmNarg}
        onDismiss={() => setConfirmNarg(false)}
        header="Turn off paging for this alarm (NARG)"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setConfirmNarg(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={busy}
                disabled={!nargReason.trim()}
                onClick={() => run(() => nargAlarm(alarm.id, nargReason.trim()), "Paging turned off for this alarm.")}
              >
                Turn off paging
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            Stops {alarmRef(alarm.id)} from paging anyone. The alarm stays recorded, stays visible here, and still resolves
            normally — only the pager is stopped.
          </Box>
          <StatusIndicator type="info">
            The hold lapses after 24 hours, so an alarm can&apos;t be lost permanently by turning paging off and forgetting
            about it.
          </StatusIndicator>
          <FormField label="Reason" description="Required — recorded in the audit log so it's clear why this stopped paging.">
            <Textarea
              value={nargReason}
              onChange={({ detail }) => setNargReason(detail.value)}
              placeholder="e.g. known issue, change window in progress, waiting on vendor"
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={confirmResolve}
        onDismiss={() => setConfirmResolve(false)}
        header="Manually resolve alarm"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setConfirmResolve(false)}>
                Cancel
              </Button>
              <Button variant="primary" loading={busy} onClick={() => run(() => resolveAlarm(alarm.id), "Resolved.")}>
                Resolve
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            Closes {alarmRef(alarm.id)} and sends a resolve notification to every receiver, closing the matching PagerDuty
            incident.
          </Box>
          <StatusIndicator type="warning">
            If the underlying condition is still true, the alarm fires again on the next check — as a new occurrence, which is
            the honest record. Use a maintenance window to suppress a known-ongoing problem.
          </StatusIndicator>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}

function AlarmLog({ pushFlash, onNavigate }) {
  const [alarms, setAlarms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState("");

  async function refresh() {
    setLoading(true);
    try {
      setAlarms(await getAlarms(200));
    } catch (e) {
      pushFlash("error", `Could not load the alarm log: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // 5s, matching the alarm detail page: the log shows live paging
    // countdowns, and a stale one is worse than a slightly chatty poll.
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = alarms.filter((a) => {
    const q = filterText.toLowerCase();
    if (!q) return true;
    return (
      (a.alertname || "").toLowerCase().includes(q) ||
      (a.summary || "").toLowerCase().includes(q) ||
      (a.ack?.acked_by || "").toLowerCase().includes(q) ||
      alarmRef(a.id).toLowerCase().includes(q)
    );
  });
  const { pageItems, paginationProps } = useClientPagination(filtered, 15);

  return (
    <Container
      header={
        <Header
          variant="h1"
          counter={alarms.length ? `(${alarms.length})` : undefined}
          description="One entry per occurrence - a single fired-to-resolved episode. The same alarm firing again creates a new entry rather than reopening the old one, so each is separately owned, discussed and auditable. Every entry has its own shareable link."
          actions={
            <Button iconName="refresh" loading={loading} onClick={refresh}>
              Refresh
            </Button>
          }
        >
          Alarm log
        </Header>
      }
    >
      <Table
        variant="embedded"
        loading={loading}
        items={pageItems}
        trackBy="id"
        filter={
          <TextFilter
            filteringText={filterText}
            onChange={({ detail }) => setFilterText(detail.filteringText)}
            filteringPlaceholder="Search id, alarm, summary or owner..."
          />
        }
        pagination={<Pagination {...paginationProps} />}
        columnDefinitions={[
          {
            id: "ref",
            header: "Alarm",
            minWidth: 120,
            cell: (a) => (
              <Button variant="inline-link" onClick={() => onNavigate(`#/alarms/${a.id}`)}>
                {alarmRef(a.id)}
              </Button>
            ),
          },
          { id: "name", header: "Name", minWidth: 170, cell: (a) => a.alertname },
          {
            id: "state",
            header: "Status",
            minWidth: 180,
            cell: (a) => <StatusIndicator type={stateType(a.state)}>{STATE_LABELS[a.state] || a.state}</StatusIndicator>,
          },
          {
            id: "severity",
            header: "Severity",
            minWidth: 110,
            cell: (a) => <StatusIndicator type={severityType(a.severity)}>{a.severity || "-"}</StatusIndicator>,
          },
          { id: "summary", header: "Summary", minWidth: 250, cell: (a) => a.summary || "-" },
          { id: "started", header: "Started", minWidth: 190, cell: (a) => new Date(a.started_at).toLocaleString() },
          { id: "lasted", header: "Lasted", minWidth: 110, cell: (a) => duration(a.started_at, a.resolved_at) },
          { id: "owner", header: "Owner", minWidth: 190, cell: (a) => <AckCell ack={a.ack} /> },
          { id: "paging", header: "Paging", minWidth: 170, cell: (a) => <PagingStatus alarm={a} /> },
          { id: "comments", header: "Comments", minWidth: 100, cell: (a) => a.comments || "-" },
        ]}
        empty={<Box textAlign="center">No alarms recorded yet.</Box>}
      />
    </Container>
  );
}

export default function AlarmsPage({ fingerprint, pushFlash, onNavigate }) {
  // `fingerprint` is the route parameter - now an occurrence id.
  return fingerprint ? (
    <AlarmDetail alarmId={fingerprint} pushFlash={pushFlash} onNavigate={onNavigate} />
  ) : (
    <AlarmLog pushFlash={pushFlash} onNavigate={onNavigate} />
  );
}
