// Shared between SyslogPage (all devices) and ConsolePage's per-device
// Syslog tab, so the two views stay in lockstep.
export const CATEGORY_OPTIONS = [
  { label: "All categories", value: "" },
  { label: "Auth", value: "auth" },
  { label: "Interface", value: "interface" },
  { label: "Spanning tree", value: "spanning-tree" },
  { label: "Hardware", value: "hardware" },
  { label: "Routing", value: "routing" },
  { label: "Other", value: "other" },
];

export function severityType(severityNum) {
  if (severityNum == null) return "info";
  if (severityNum <= 3) return "error";
  if (severityNum === 4) return "warning";
  return "info";
}

export function formatTime(iso) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
