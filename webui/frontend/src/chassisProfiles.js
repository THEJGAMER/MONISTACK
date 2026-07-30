// Illustration profiles for the Front Panel tab. "s4048-on" is traced
// against a real reference photo of this switch family's faceplate
// (S4048-ON.webp) - port numbering, 3-groups-of-16 spacing, and the
// staggered 3x2 QSFP+ uplink layout all match what's visible in that
// photo, not a guess. "ex3300-48p" is traced the same way against real
// photos (img_5c7d30bd92eae48P.webp, 1862015.webp): 48x RJ45 in 4 visible
// groups of 12 (2 rows x 6 columns), a single link LED per port column
// (not Dell's 4-arrow link/activity pair), an EX3300 label + LCD status
// display instead of Dell's icon grid, and 4 individually-numbered SFP+
// uplinks in one row (not staggered 3x2 like the Dell QSFP+ bank) -
// confirmed live that only 2 of those 4 are populated on this specific
// unit (`show chassis hardware`: Xcvr 2/3 present, 0/1 absent). Port
// *numbering* (which physical port is "top" vs "bottom" in each column)
// follows Juniper's documented even-top/odd-bottom convention - the
// silkscreened numbers themselves weren't legible at the reference
// photos' resolution to verify pixel-by-pixel, unlike everything else
// here. The "generic-*" profiles are an honest fallback for any other
// device - a plain sequential grid, not staggered, since we have no
// documented layout to go on for hardware we don't know.
//
// `portRegex` has two capture groups (prefix, number) so port names in
// completely different shapes (Dell's "Te 1/1" vs Junos's "ge-0/0/0")
// both parse the same way; `mainStart`/`uplinkStart` is the first real
// port number for each bank (Dell's main bank is 1-indexed, Junos's is
// 0-indexed).
const DELL_PORT_REGEX = /^(\S+)\s*1\/(\d+)$/;
const JUNOS_MAIN_PORT_REGEX = /^(ge)-0\/0\/(\d+)$/;
const JUNOS_UPLINK_PORT_REGEX = /^(xe)-0\/1\/(\d+)$/;

export const CHASSIS_PROFILES = {
  "s4048-on": {
    id: "s4048-on",
    label: "Dell EMC S4048-ON (accurate)",
    chassisType: "dell",
    portRegex: DELL_PORT_REGEX,
    mainPrefix: "Te",
    mainCount: 48,
    mainStart: 1,
    staggered: true,
    ledStyle: "arrows",
    groupSize: 16,
    uplinkPrefix: "Fo",
    uplinkCount: 6,
    uplinkStaggered: true,
    uplinkStart: 49,
  },
  "ex3300-48p": {
    id: "ex3300-48p",
    label: "Juniper EX3300-48P (accurate)",
    chassisType: "juniper",
    portRegex: JUNOS_MAIN_PORT_REGEX,
    mainPrefix: "ge",
    mainCount: 48,
    mainStart: 0,
    staggered: true,
    ledStyle: "single",
    groupSize: 12,
    uplinkPrefix: "xe",
    uplinkPortRegex: JUNOS_UPLINK_PORT_REGEX,
    uplinkCount: 4,
    uplinkStaggered: false,
    uplinkStart: 0,
  },
  "generic-48": {
    id: "generic-48",
    label: "Generic 48-port switch",
    chassisType: "generic",
    portRegex: DELL_PORT_REGEX,
    mainPrefix: "Te",
    mainCount: 48,
    mainStart: 1,
    staggered: false,
    ledStyle: "single",
    groupSize: 12,
    uplinkPrefix: null,
    uplinkCount: 0,
    uplinkStart: 0,
  },
  "generic-16": {
    id: "generic-16",
    label: "Generic 16-port switch",
    chassisType: "generic",
    portRegex: DELL_PORT_REGEX,
    mainPrefix: "Te",
    mainCount: 16,
    mainStart: 1,
    staggered: false,
    ledStyle: "single",
    groupSize: 8,
    uplinkPrefix: null,
    uplinkCount: 0,
    uplinkStart: 0,
  },
};

export function defaultProfileId(device) {
  const model = (device?.model || "").toLowerCase();
  if (model.includes("s4048")) return "s4048-on";
  if (model.includes("ex3300")) return "ex3300-48p";
  return "generic-48";
}
