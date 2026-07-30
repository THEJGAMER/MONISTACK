// Static configuration for the Console page's dashboard (Cloudscape
// Board/BoardItem - the same drag/resize/reflow components AWS Console
// uses for CloudWatch dashboards). Split out from ConsolePage.jsx for the
// same reason chassisProfiles.js/syslogUtils.js are separate: static
// config/lookups, not behavior.

export const BOARD_ITEM_IDS = [
  "devices",
  "deviceSummary",
  "commands",
  "output",
  "recent",
  "syslog",
  "alarmHistory",
  "frontpanel",
  "switchStatus",
];

export const BOARD_ITEM_TITLES = {
  devices: "Devices",
  deviceSummary: "Device summary",
  commands: "Commands",
  output: "Output",
  recent: "Recent results",
  syslog: "Syslog",
  alarmHistory: "Alarm History",
  frontpanel: "Front Panel",
  switchStatus: "Switch Status",
};

// Default position/size, applied on first load and whenever "Reset
// layout" is used. columnSpan is out of Board's responsive column count
// (4 on a wide desktop viewport) - richer panels (Front Panel, Switch
// Status, Output) get the full width, the rest sit two-up. Devices/Device
// summary/Commands default to the left column, mirroring the fixed
// sidebar layout this replaced.
export const DEFAULT_BOARD_ITEMS = [
  { id: "devices", columnSpan: 1, rowSpan: 3, columnOffset: { 4: 0 } },
  { id: "deviceSummary", columnSpan: 1, rowSpan: 4, columnOffset: { 4: 0 } },
  { id: "commands", columnSpan: 1, rowSpan: 5, columnOffset: { 4: 0 } },
  { id: "output", columnSpan: 3, rowSpan: 4, columnOffset: { 4: 1 } },
  { id: "recent", columnSpan: 3, rowSpan: 4, columnOffset: { 4: 1 } },
  { id: "syslog", columnSpan: 3, rowSpan: 4, columnOffset: { 4: 1 } },
  { id: "alarmHistory", columnSpan: 3, rowSpan: 4, columnOffset: { 4: 1 } },
  { id: "frontpanel", columnSpan: 3, rowSpan: 4, columnOffset: { 4: 1 } },
  { id: "switchStatus", columnSpan: 3, rowSpan: 5, columnOffset: { 4: 1 } },
].map((item) => ({
  ...item,
  data: { title: BOARD_ITEM_TITLES[item.id] },
  definition: { minRowSpan: 2, minColumnSpan: 1, defaultRowSpan: item.rowSpan, defaultColumnSpan: item.columnSpan },
}));

export const boardI18nStrings = {
  liveAnnouncementDndStarted: (operationType) =>
    operationType === "resize" ? "Resizing" : operationType === "reorder" ? "Dragging" : "Inserting",
  liveAnnouncementDndItemReordered: (operation) => {
    const columns = `column ${operation.placement.x + 1}`;
    const rows = `row ${operation.placement.y + 1}`;
    return operation.direction === "horizontal"
      ? `Item moved to ${columns}.`
      : `Item moved to ${rows}.`;
  },
  liveAnnouncementDndItemResized: (operation) => {
    const size = operation.isMinimalColumnsReached
      ? "minimum width"
      : operation.isMinimalRowsReached
        ? "minimum height"
        : `${operation.placement.width} columns by ${operation.placement.height} rows`;
    return `Item resized to ${size}.`;
  },
  liveAnnouncementDndItemInserted: (operation) =>
    `Item inserted at column ${operation.placement.x + 1}, row ${operation.placement.y + 1}.`,
  liveAnnouncementDndCommitted: (operationType) => `${operationType} committed.`,
  liveAnnouncementDndDiscarded: (operationType) => `${operationType} discarded.`,
  liveAnnouncementItemRemoved: (op) => `Removed item ${op.item.data?.title ?? op.item.id}.`,
  navigationAriaLabel: "Board navigation",
  navigationAriaDescription: "Click on non-empty item to move focus over it.",
  navigationItemAriaLabel: (item) => (item ? item.data?.title ?? item.id : "Empty"),
};

export function boardItemI18nStrings(title) {
  return {
    dragHandleAriaLabel: `${title}, drag handle`,
    dragHandleAriaDescription: "Use arrow keys to move, space or enter to pick up and drop.",
    resizeHandleAriaLabel: `${title}, resize handle`,
    resizeHandleAriaDescription: "Use arrow keys to resize, space or enter to commit.",
    dragHandleTooltipText: "Drag to move",
    resizeHandleTooltipText: "Drag to resize",
  };
}
