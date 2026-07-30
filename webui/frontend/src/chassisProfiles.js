// Illustration profiles for the Front Panel tab. "s4048-on" is traced
// against a real reference photo of this switch family's faceplate
// (S4048-ON.webp) - port numbering, 3-groups-of-16 spacing, and the
// staggered 3x2 QSFP+ uplink layout all match what's visible in that
// photo, not a guess. The "generic-*" profiles are an honest fallback for
// any other device - a plain sequential grid, not staggered, since we have
// no documented layout to go on for hardware we don't know.
export const CHASSIS_PROFILES = {
  "s4048-on": {
    id: "s4048-on",
    label: "Dell EMC S4048-ON (accurate)",
    mainPrefix: "Te",
    mainCount: 48,
    staggered: true,
    groupSize: 16,
    uplinkPrefix: "Fo",
    uplinkCount: 6,
    uplinkStaggered: true,
    uplinkStart: 49,
  },
  "generic-48": {
    id: "generic-48",
    label: "Generic 48-port switch",
    mainPrefix: "Te",
    mainCount: 48,
    staggered: false,
    groupSize: 12,
    uplinkPrefix: null,
    uplinkCount: 0,
    uplinkStart: 0,
  },
  "generic-16": {
    id: "generic-16",
    label: "Generic 16-port switch",
    mainPrefix: "Te",
    mainCount: 16,
    staggered: false,
    groupSize: 8,
    uplinkPrefix: null,
    uplinkCount: 0,
    uplinkStart: 0,
  },
};

export function defaultProfileId(device) {
  const model = (device?.model || "").toLowerCase();
  if (model.includes("s4048")) return "s4048-on";
  return "generic-48";
}
