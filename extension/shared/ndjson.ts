/**
 * Newline-delimited JSON, as the gateway streams it (CONTRACTS section 6.4).
 *
 * Operates on text: the caller decodes bytes with a streaming TextDecoder
 * (`response.body.pipeThrough(new TextDecoderStream())`), which already keeps a
 * multibyte character that is split across two network chunks intact. This
 * file only has to find line boundaries, tolerate CRLF, refuse a runaway line,
 * and hand back a final line that arrived without its newline.
 */

/** A final response can carry an image preview, so allow a generous line. */
export const MAX_NDJSON_LINE_CHARS = 8 * 1024 * 1024;

export type NdjsonErrorCode = "line_too_large" | "invalid_json";

export class NdjsonError extends Error {
  readonly code: NdjsonErrorCode;

  constructor(code: NdjsonErrorCode, message: string) {
    super(message);
    this.name = "NdjsonError";
    this.code = code;
  }
}

export interface LineSplitter {
  /** Feed decoded text; returns every complete, non-blank line. */
  push(chunk: string): string[];
  /** The stream ended: return the last line if it had no trailing newline. */
  flush(): string[];
}

function tidy(line: string): string | null {
  const trimmed = line.endsWith("\r") ? line.slice(0, -1) : line;
  return trimmed.trim() ? trimmed : null;
}

export function createLineSplitter(maxLineChars = MAX_NDJSON_LINE_CHARS): LineSplitter {
  let pending = "";
  return {
    push(chunk: string): string[] {
      const lines: string[] = [];
      let start = 0;
      let newline = chunk.indexOf("\n");
      while (newline !== -1) {
        const line = pending + chunk.slice(start, newline);
        pending = "";
        if (line.length > maxLineChars) {
          throw new NdjsonError("line_too_large", `A stream line exceeded ${maxLineChars} characters.`);
        }
        const kept = tidy(line);
        if (kept !== null) lines.push(kept);
        start = newline + 1;
        newline = chunk.indexOf("\n", start);
      }
      pending += chunk.slice(start);
      if (pending.length > maxLineChars) {
        pending = "";
        throw new NdjsonError("line_too_large", `A stream line exceeded ${maxLineChars} characters.`);
      }
      return lines;
    },
    flush(): string[] {
      const rest = tidy(pending);
      pending = "";
      return rest === null ? [] : [rest];
    },
  };
}

export function parseNdjsonLine(line: string): unknown {
  try {
    return JSON.parse(line);
  } catch {
    throw new NdjsonError("invalid_json", "A stream line was not valid JSON.");
  }
}

/** Read every JSON value from a decoded text stream, in order. */
export async function readNdjson(
  reader: ReadableStreamDefaultReader<string>,
  onValue: (value: unknown) => void,
  maxLineChars = MAX_NDJSON_LINE_CHARS,
): Promise<void> {
  const splitter = createLineSplitter(maxLineChars);
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    for (const line of splitter.push(value)) onValue(parseNdjsonLine(line));
  }
  for (const line of splitter.flush()) onValue(parseNdjsonLine(line));
}
