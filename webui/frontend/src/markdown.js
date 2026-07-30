export function resultToMarkdown({ deviceName, host, command, summary, output }) {
  const lines = [`## ${deviceName} — \`${command}\``, "", `**Device:** ${deviceName} (${host})  `, `**Command:** \`${command}\`  `];
  if (summary) lines.push("", `**Summary:** ${summary}`);
  lines.push("", "```text", output || "(no output)", "```", "");
  return lines.join("\n");
}
