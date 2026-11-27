/**
 * The web pages behind a draft or an answer, ready to show as links.
 *
 * OpenAI's web search terms: "When displaying web results or information contained in web results
 * to end users, inline citations must be made clearly visible and clickable in your user interface."
 * So every source the writer used becomes a link the person can open, labelled with its site so they
 * know where it goes before they click. Only http and https addresses become links; anything else,
 * and any address carrying a user name or password, is dropped rather than shown.
 */

export interface SourceLink {
  href: string;
  /** The page's title when it has one, otherwise its site. */
  label: string;
  /** The site without "www.", e.g. "theverge.com". */
  host: string;
}

export const MAX_SOURCES = 3;
const MAX_TITLE_CHARS = 90;

/**
 * Several source lists as one, in order: a draft's own `sources` first (the pages whose facts reached
 * the writer), then a chat answer's `research.sources`. `sourceLinks` drops the repeats, so a page in
 * both is shown once. Anything that is not a list is skipped.
 */
export function mergeSources(...lists: unknown[]): unknown[] {
  return lists.flatMap(list => (Array.isArray(list) ? list : []));
}

export function sourceLinks(raw: unknown, limit = MAX_SOURCES): SourceLink[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const links: SourceLink[] = [];
  for (const item of raw) {
    if (links.length >= limit) break;
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const url = typeof record.url === "string" ? record.url.trim() : "";
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      continue;
    }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") continue;
    if (parsed.username || parsed.password || !parsed.hostname) continue;
    const href = parsed.toString();
    if (seen.has(href)) continue;
    seen.add(href);
    const host = parsed.hostname.replace(/^www\./, "");
    let title = typeof record.title === "string" ? record.title.replace(/\s+/g, " ").trim() : "";
    if (title.length > MAX_TITLE_CHARS) title = title.slice(0, MAX_TITLE_CHARS - 1).trimEnd() + "…";
    links.push({ href, label: title || host, host });
  }
  return links;
}
