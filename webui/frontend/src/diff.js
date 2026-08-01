// Minimal LCS-based line diff for Bulk Run's collated result view (no
// external diff library - command output here is at most a few hundred
// lines, well within reach of a plain O(n*m) LCS table).
export function lineDiff(baseline, other) {
  const a = baseline.split("\n");
  const b = other.split("\n");
  const n = a.length;
  const m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ type: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      rows.push({ type: "removed", text: a[i] });
      i++;
    } else {
      rows.push({ type: "added", text: b[j] });
      j++;
    }
  }
  while (i < n) {
    rows.push({ type: "removed", text: a[i] });
    i++;
  }
  while (j < m) {
    rows.push({ type: "added", text: b[j] });
    j++;
  }
  return rows;
}
