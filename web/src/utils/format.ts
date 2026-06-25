export function fmtTs(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return String(ts);
  }
}

export function clip(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + "…" : s;
}

/** Strip common Markdown noise so previews read as plain prose. */
export function stripMd(s: string): string {
  return (s || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/[*_~>`]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** One clean line, markdown stripped and clipped — for card titles / bodies. */
export function preview(s: string, n = 96): string {
  return clip(stripMd(s), n);
}
